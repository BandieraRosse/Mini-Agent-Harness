import ssl
import socket
import urllib.error
import unittest
from unittest.mock import patch

from miniagent.api import APIError, ChatClient
from miniagent.config import Config


class APITests(unittest.TestCase):
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
