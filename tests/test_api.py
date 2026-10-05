import ssl
import errno
import io
import json
import socket
import threading
from types import SimpleNamespace
import urllib.error
import urllib.response
import unittest
from unittest.mock import patch

from miniagent.api import APIError, ChatClient, _events
from miniagent.config import Config


class APITests(unittest.TestCase):
    def test_model_requests_ignore_environment_and_system_proxies(self):
        proxies = {'http': 'http://proxy.invalid:7890', 'https': 'http://proxy.invalid:7890'}
        for provider in ('deepseek', 'openai'):
            for base_url in ('http://localhost:8000/v1', 'https://api.example.test/v1'):
                for operation in ('stream', 'json', 'models'):
                    with self.subTest(provider=provider, base_url=base_url, operation=operation):
                        config = Config(provider=provider, base_url=base_url, max_retries=0)
                        targets = []

                        def respond(handler, factory, request, **kwargs):
                            targets.append(request.host)
                            self.assertFalse(request.has_proxy())
                            self.assertIsNone(request._tunnel_host)
                            if operation == 'models':
                                raw = json.dumps({'data': [{'id': 'test-model'}]}).encode()
                            elif operation == 'json':
                                raw = json.dumps({'choices': [{'message': {
                                    'role': 'assistant', 'content': '[final_answer]\nReady'},
                                    'finish_reason': 'stop'}]}).encode()
                            else:
                                raw = self.stream_chunk({'content': '[final_answer]\nReady'}, 'stop') + b'data: [DONE]\n\n'
                            response = urllib.response.addinfourl(io.BytesIO(raw), {}, request.full_url, 200)
                            response.msg = 'OK'
                            return response

                        with patch('urllib.request.getproxies', return_value=proxies) as discover, patch(
                                'urllib.request.proxy_bypass', return_value=False), patch(
                                'urllib.request.AbstractHTTPHandler.do_open', autospec=True, side_effect=respond):
                            client = ChatClient(config, 'secret-token')
                            if operation == 'models':
                                self.assertEqual(client.list_models(), ['test-model'])
                            else:
                                self.assertEqual(client.complete([], stream=operation == 'stream')[
                                    'choices'][0]['message']['phase'], 'final_answer')
                            discover.assert_not_called()
                        self.assertEqual(targets, ['localhost:8000' if base_url.startswith('http:') else 'api.example.test'])

    @staticmethod
    def stream_chunk(delta, finish=None):
        return ('data: ' + json.dumps({'choices': [{
            'index': 0, 'delta': delta, 'finish_reason': finish,
        }]}) + '\n\n').encode()

    def test_terminal_marker_at_eof_without_blank_line(self):
        for ending in (b'data: [DONE]', b'data: [DONE]\n', b'data: [DONE]\r\n'):
            with self.subTest(ending=ending):
                self.assertEqual(list(_events(io.BytesIO(ending))), ['[DONE]'])
        with self.assertRaises(APIError):
            list(_events(io.BytesIO(b'data: [DON')))

    def test_stream_eof_diagnostics_never_accept_partial_tools_or_leak_arguments(self):
        client = ChatClient(Config(max_retries=0), 'secret-token')
        call = {'index': 0, 'id': 'call-1', 'type': 'function',
                'function': {'name': 'run_command', 'arguments': '{"command":"private-command"}'}}
        for finish in (None, 'tool_calls'):
            with self.subTest(finish=finish):
                raw = self.stream_chunk({'tool_calls': [call]}, finish)
                with patch.object(client._opener, 'open', return_value=io.BytesIO(raw)):
                    with self.assertRaises(APIError) as caught:
                        client.complete([])
                message = str(caught.exception)
                self.assertIn('SSE events=1', message)
                self.assertIn('tool calls=1, generated=yes', message)
                self.assertIn('finish_reason=' + (finish or 'missing/unknown'), message)
                self.assertNotIn('private-command', message)
                self.assertNotIn('secret-token', message)

    def test_unterminated_gateway_error_is_reported_instead_of_generic_eof(self):
        client = ChatClient(Config(max_retries=0), 'secret-token')
        raw = b'data: {"error":{"message":"upstream disconnected secret-token"}}\n'
        with patch.object(client._opener, 'open', return_value=io.BytesIO(raw)):
            with self.assertRaises(APIError) as caught:
                client.complete([])
        self.assertIn('upstream disconnected [REDACTED]', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))

    def test_eof_retries_only_before_generated_output(self):
        valid = self.stream_chunk({'content': '[commentary]\nReady'}, 'stop') + b'data: [DONE]\n\n'
        for first, expected_requests in ((b': keep-alive\n\n', 2),
                                        (self.stream_chunk({'content': 'partial'}), 1)):
            with self.subTest(expected_requests=expected_requests):
                client = ChatClient(Config(max_retries=1), 'secret-token')
                with patch.object(client._opener, 'open', side_effect=[io.BytesIO(first), io.BytesIO(valid)]) as opener, patch('miniagent.api.time.sleep'):
                    if expected_requests == 2:
                        self.assertEqual(client.complete([])['choices'][0]['message']['phase'], 'commentary')
                    else:
                        with self.assertRaises(APIError):
                            client.complete([])
                    self.assertEqual(opener.call_count, expected_requests)

    def test_cancel_interrupts_response_after_urllib_closes_socket(self):
        # Some execution sandboxes forbid socket.shutdown even for local
        # socket pairs. Probe the OS capability directly, not client.cancel:
        # skip only that environment restriction, never an implementation
        # failure. Normal development/CI must still verify cancellation.
        try:
            local, peer = socket.socketpair()
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES):
                self.skipTest("Execution environment forbids local socket pairs; cancellation requires them")
            raise
        try:
            local.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES):
                self.skipTest("Execution environment forbids socket.shutdown; API cancellation cannot be tested here")
            raise
        finally:
            local.close()
            peer.close()
        for operation in ('stream', 'json', 'models'):
            with self.subTest(operation=operation):
                client = ChatClient(Config(provider="openai", max_retries=0), "secret-token")
                client.cancel_event = threading.Event()
                local, peer = socket.socketpair()
                local.settimeout(3)
                connection = SimpleNamespace(sock=local, connect=lambda: None)
                client._connection(lambda *args, **kwargs: connection, 'unused').connect()
                handle = client._active_socket
                response = local.makefile('rb')
                # Mirror urllib: the reader owns the remaining reference, while
                # the socket object originally tracked by MiniAgent is closed.
                local.close()
                entered, errors = threading.Event(), []

                class Response:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        response.close()

                    def read(self, size):
                        entered.set()
                        return response.read(size)

                    def readline(self, size):
                        entered.set()
                        return response.readline(size)

                def request():
                    try:
                        if operation == 'models':
                            client.list_models()
                        else:
                            client.complete([], stream=operation == 'stream')
                    except BaseException as error:
                        errors.append(error)

                worker = threading.Thread(target=request, daemon=True)
                try:
                    with patch.object(client._opener, 'open', return_value=Response()):
                        worker.start()
                        self.assertTrue(entered.wait(1))
                        client.cancel()
                        worker.join(1)
                        self.assertFalse(worker.is_alive(), 'cancel must wake the response reader')
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], KeyboardInterrupt)
                    self.assertIsNone(client._active_socket)
                    self.assertEqual(handle.fileno(), -1)
                finally:
                    peer.close()
                    worker.join(4)
                    response.close()
                    client._release_socket()

    def test_transport_errors_are_actionable_without_leaking_exception_details(self):
        cases = [
            (ssl.SSLEOFError(8, "secret-token proxy-password"), "SSL EOF"),
            (ssl.SSLCertVerificationError(1, "secret-token"), "certificate verification"),
            (socket.gaierror(-2, "secret-token"), "DNS lookup"),
            (ConnectionRefusedError(111, "proxy-password"), "Connection refused"),
        ]
        for reason, expected in cases:
            client = ChatClient(Config(provider="openai", max_retries=0), "secret-token")
            with self.subTest(reason=type(reason).__name__), patch.object(
                    client._opener, "open", side_effect=urllib.error.URLError(reason)):
                for operation in (client.list_models, lambda: client.complete([])):
                    with self.assertRaises(APIError) as caught:
                        operation()
                    self.assertIn(expected, str(caught.exception))
                    self.assertNotIn("secret-token", str(caught.exception))
                    self.assertNotIn("proxy-password", str(caught.exception))
