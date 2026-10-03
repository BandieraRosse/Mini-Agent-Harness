import json
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from miniagent.api import APIError, ChatClient
from miniagent.config import Config


def sse(*chunks, done=True):
    wire = b": keep-alive\r\n\r\n"
    for chunk in chunks:
        wire += ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
    return wire + (b"data: [DONE]\n\n" if done else b"")


def chunk(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def answer(text="Hello"):
    return sse(chunk({"role": "assistant", "content": text}), chunk(finish="stop"),
               {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})


@contextmanager
def http_server(responses):
    """Real loopback HTTP transport; only synthetic credentials are ever used."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append({"path": self.path, "body": json.loads(raw), "headers": dict(self.headers)})
            self.respond()

        def do_GET(self):
            requests.append({"path": self.path, "body": None, "headers": dict(self.headers)})
            self.respond()

        def respond(self):
            spec = responses[min(len(requests) - 1, len(responses) - 1)]
            self.send_response(spec.get("status", 200))
            self.send_header("Content-Type", spec.get("content_type", "text/event-stream"))
            if spec.get("redirect"):
                self.send_header("Location", spec["redirect"])
            self.end_headers()
            try:
                if spec.get("prefix"):
                    self.wfile.write(spec["prefix"])
                    self.wfile.flush()
                if spec.get("wait") is not None:
                    spec["wait"].wait(5)
                time.sleep(spec.get("delay", 0))
                self.wfile.write(spec.get("body", b""))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class APITests(unittest.TestCase):
    def test_phase_markers_and_native_metadata_survive_stream_and_history(self):
        wire = sse(chunk({"content": "[comm"}), chunk({"content": "entary]\nworking"}), chunk(finish="stop"))
        with http_server([{"body": wire}]) as (url, requests):
            client = self.client(url)
            message = client.complete([])["choices"][0]["message"]
            self.assertEqual(message["phase"], "commentary")
            self.assertEqual(message["content"], "working")
            payload = client._payload([message], None, False)
            self.assertNotIn("phase", payload["messages"][0])
            self.assertEqual(payload["messages"][0]["content"], "[commentary]\nworking")
        terminal = chunk(finish="stop")
        terminal["end_turn"] = False
        with http_server([{"body": sse(chunk({"content": "progress", "phase": "commentary"}), terminal)}]) as (url, _):
            response = self.client(url).complete([])
            self.assertEqual(response["choices"][0]["message"]["phase"], "commentary")

    def client(self, url, **kwargs):
        return ChatClient(Config(base_url=url, **kwargs), "test-secret-key")

    def test_stream_text_usage_and_request_payload(self):
        with http_server([{"body": answer("你好 world")}]) as (url, requests):
            fragments = []
            result = self.client(url).complete([{"role": "user", "content": "hello"}], on_text=fragments.append)
        self.assertEqual("".join(fragments), "你好 world")
        self.assertEqual(result["choices"][0]["message"]["content"], "你好 world")
        self.assertEqual(result["usage"]["total_tokens"], 5)
        request = requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-secret-key")
        self.assertTrue(request["body"]["stream_options"]["include_usage"])
        self.assertNotIn("test-secret-key", json.dumps(request["body"]))

    def test_interleaved_tool_calls_with_fragmented_ids_names_arguments(self):
        wire = sse(
            chunk({"reasoning_content": "Consider "}), chunk({"reasoning_content": "both files"}),
            chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"b'}}]}),
            chunk({"tool_calls": [{"index": 0, "id": "call_", "function": {"name": "read_", "arguments": '{"path":'}},
                                  {"index": 1, "function": {"arguments": '.txt"}'}}]}),
            chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "file", "arguments": '"a.txt"}'}}]}),
            chunk(finish="tool_calls"),
        )
        with http_server([{"body": wire}]) as (url, _):
            result = self.client(url).complete([])
        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "Consider both files")
        self.assertEqual([x["id"] for x in message["tool_calls"]], ["call_a", "call_b"])
        self.assertEqual([json.loads(x["function"]["arguments"])["path"] for x in message["tool_calls"]], ["a.txt", "b.txt"])
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "read_file")

    def test_missing_done_never_returns_partial_tool_calls_or_retries(self):
        wire = sse(chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": '{"command":'}}]}), done=False)
        with http_server([{"body": wire}, {"body": answer()}]) as (url, requests):
            with self.assertRaisesRegex(APIError, "before \\[DONE\\]"):
                self.client(url).complete([])
        self.assertEqual(len(requests), 1)

    def test_finish_without_done_is_incomplete(self):
        with http_server([{"body": sse(chunk({"content": "hello"}, "stop"), done=False)}]) as (url, _):
            with self.assertRaises(APIError):
                self.client(url).complete([])

    def test_length_finish_is_rejected_before_any_action(self):
        call = {"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": '{"command":"whoami"}'}}
        with http_server([{"body": sse(chunk({"tool_calls": [call]}, "length"))}]) as (url, _):
            with self.assertRaisesRegex(APIError, "token limit"):
                self.client(url).complete([])

    def test_invalid_json_and_tool_indexes_rejected(self):
        bad_streams = [b"data: bad json\n\ndata: [DONE]\n\n",
                       sse(chunk({"tool_calls": [{"id": "missing-index"}]}, "tool_calls")),
                       sse(chunk({"tool_calls": [{"index": 1, "id": "gap", "function": {"name": "shell", "arguments": "{}"}}]}, "tool_calls")),
                       sse(chunk({"tool_calls": [{"index": 0, "function": {"name": "shell", "arguments": "{}"}}]}, "tool_calls")),
                       sse(chunk({"content": "hello"})),
                       sse(chunk({"content": "hello"}, "stop"), chunk({"content": "late"}))]
        for body in bad_streams:
            with self.subTest(body=body), http_server([{"body": body}]) as (url, _):
                with self.assertRaises(APIError):
                    self.client(url).complete([])

    def test_completed_bad_arguments_are_left_for_tool_validation(self):
        call = {"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": "invalid-json"}}
        with http_server([{"body": sse(chunk({"tool_calls": [call]}, "tool_calls"))}]) as (url, _):
            result = self.client(url).complete([])
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], "invalid-json")

    def test_transient_http_status_retries_before_output(self):
        response = {"status": 429, "body": b'{"error":{"message":"rate limit"}}'}
        with http_server([response, {"body": answer()}]) as (url, requests), patch("miniagent.api.time.sleep"):
            self.assertEqual(self.client(url).complete([])["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(len(requests), 2)

    def test_http_errors_redact_key_and_do_not_retry_authentication(self):
        error = {"status": 401, "body": b'{"error":{"message":"invalid test-secret-key"}}'}
        with http_server([error]) as (url, requests):
            with self.assertRaises(APIError) as caught:
                self.client(url).complete([])
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("[REDACTED]", str(caught.exception))
        self.assertNotIn("test-secret-key", str(caught.exception))
        self.assertEqual(len(requests), 1)
        self.assertIsNone(caught.exception.__cause__)

    def test_stream_error_is_redacted(self):
        with http_server([{"body": sse({"error": {"message": "test-secret-key failed"}})}]) as (url, _):
            with self.assertRaises(APIError) as caught:
                self.client(url).complete([])
        self.assertNotIn("test-secret-key", str(caught.exception))

    def test_http_redirects_are_not_followed(self):
        with http_server([{"status": 307, "redirect": "http://127.0.0.1:9/stolen", "body": b""}]) as (url, requests):
            with self.assertRaises(APIError) as caught:
                self.client(url).complete([])
        self.assertEqual(caught.exception.status, 307)
        self.assertEqual(len(requests), 1)

    def test_timeout_after_partial_text_does_not_retry(self):
        prefix = sse(chunk({"content": "partial"}), done=False)
        release = threading.Event()
        with http_server([{"prefix": prefix, "wait": release, "body": answer()}]) as (url, requests):
            parts = []
            client = self.client(url, timeout=5)

            def on_text(text):
                parts.append(text)
                # Test the stream read deadline only after actual generation;
                # connection/startup scheduling must not trigger the timeout.
                client._active_socket.settimeout(0.03)

            try:
                with self.assertRaisesRegex(APIError, "timed out"):
                    client.complete([], on_text=on_text)
            finally:
                release.set()
        self.assertEqual(parts, ["partial"])
        self.assertEqual(len(requests), 1)

    def test_timeout_before_generation_can_retry(self):
        with http_server([{"delay": 0.12}, {"body": answer()}]) as (url, requests), patch("miniagent.api.time.sleep") as sleep:
            # The server also uses time.sleep, so preserve its delay separately below.
            sleep.side_effect = lambda seconds: threading.Event().wait(seconds) if seconds < 0.2 else None
            result = self.client(url, timeout=0.03).complete([])
        self.assertEqual(result["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(len(requests), 2)

    def test_non_stream_and_openai_payload_omit_deepseek_reasoning(self):
        wire = json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}], "usage": {}}).encode()
        messages = [{"role": "assistant", "content": "earlier", "reasoning_content": "private thinking"}, {"role": "user", "content": "continue"}]
        with http_server([{"body": wire, "content_type": "application/json"}]) as (url, requests):
            parts = []
            self.client(url, provider="openai").complete(messages, stream=False, on_text=parts.append)
        payload = requests[0]["body"]
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("stream_options", payload)
        self.assertNotIn("reasoning_content", payload["messages"][0])
        self.assertIn("reasoning_content", messages[0])
        self.assertEqual(parts, ["done"])

    def test_deepseek_history_preserves_reasoning(self):
        with http_server([{"body": answer()}]) as (url, requests):
            self.client(url).complete([{"role": "assistant", "content": None, "reasoning_content": "needed for tool continuation"}])
        self.assertEqual(requests[0]["body"]["messages"][0]["reasoning_content"], "needed for tool continuation")

    def test_refusal_is_visible_and_preserved_with_both_transports(self):
        refusal = "I cannot help with that request."
        message = {"role": "assistant", "content": None, "refusal": refusal}
        for stream in (True, False):
            body = (sse(chunk({"refusal": refusal}, "stop")) if stream else
                    json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode())
            with self.subTest(stream=stream), http_server([{"body": body}]) as (url, _):
                parts = []
                result = self.client(url, provider="openai").complete([], stream=stream, on_text=parts.append)
            self.assertEqual("".join(parts), refusal)
            self.assertEqual(result["choices"][0]["message"]["refusal"], refusal)

    def test_keyboard_interrupt_propagates_without_retry(self):
        with http_server([{"body": answer()}]) as (url, requests):
            with self.assertRaises(KeyboardInterrupt):
                self.client(url).complete([], on_text=lambda text: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(len(requests), 1)

    def test_cancel_stalled_stream_unblocks_worker_and_stops_callbacks(self):
        prefix = sse(chunk({"content": "first"}), done=False)
        with http_server([{"prefix": prefix, "delay": 3, "body": answer("late")}]) as (url, requests):
            client = self.client(url, timeout=10)
            client.cancel_event = threading.Event()
            started = threading.Event()
            fragments, outcomes = [], []

            def on_text(text):
                fragments.append(text)
                started.set()

            def request():
                try:
                    outcomes.append(client.complete([], on_text=on_text))
                except BaseException as error:
                    outcomes.append(error)

            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2), "The stream should deliver its first fragment")
            before = time.monotonic()
            client.cancel()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "Cancel must interrupt a blocked socket read")
            self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(fragments, ["first"])
        self.assertEqual(len(requests), 1)
        self.assertIsInstance(outcomes[0], KeyboardInterrupt)
        self.assertNotIn("test-secret-key", str(outcomes[0]))

    def test_cancelled_request_and_model_list_never_start(self):
        with http_server([{"body": answer()}]) as (url, requests):
            client = self.client(url)
            client.cancel_event = threading.Event()
            client.cancel()
            with self.assertRaises(KeyboardInterrupt):
                client.complete([])
            with self.assertRaises(KeyboardInterrupt):
                client.list_models()
        self.assertEqual(requests, [])

    def test_cancellation_from_callback_rejects_other_buffered_chunks(self):
        with http_server([{"body": sse(chunk({"content": "first"}), chunk({"content": "second"}, "stop"))}]) as (url, requests):
            client = self.client(url)
            client.cancel_event = threading.Event()
            fragments = []

            def on_text(text):
                fragments.append(text)
                client.cancel()

            with self.assertRaises(KeyboardInterrupt):
                client.complete([], on_text=on_text)
        self.assertEqual(fragments, ["first"])
        self.assertEqual(len(requests), 1)

    def test_model_list_uses_matching_endpoint_and_deduplicates(self):
        body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}, {"id": "model-a"}]}).encode()
        with http_server([{"body": body}]) as (url, requests):
            models = self.client(url + "/chat/completions").list_models()
        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(requests[0]["path"], "/v1/models")
        self.assertIsNone(requests[0]["body"])
        self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer test-secret-key")

    def test_model_list_rejects_missing_invalid_and_secret_ids(self):
        values = [{}, {"data": []}, {"data": [{"id": "bad\nmodel"}]}, {"data": [{"id": "test-secret-key"}]}]
        for value in values:
            with self.subTest(value=value), http_server([{"body": json.dumps(value).encode()}]) as (url, _):
                with self.assertRaises(APIError) as caught:
                    self.client(url).list_models()
                self.assertNotIn("test-secret-key", str(caught.exception))

    def test_model_list_redirects_and_errors_are_safe(self):
        responses = [{"status": 307, "redirect": "http://127.0.0.1:9/stolen"},
                     {"status": 401, "body": b'{"error":{"message":"test-secret-key failed"}}'}]
        for response in responses:
            with self.subTest(status=response["status"]), http_server([response]) as (url, requests):
                with self.assertRaises(APIError) as caught:
                    self.client(url).list_models()
            self.assertEqual(caught.exception.status, response["status"])
            self.assertNotIn("test-secret-key", str(caught.exception))
            self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
