"""Synthetic OAuth credentials only; no browser account or external HTTP needed."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from miniagent.chatgpt_auth import (AuthError, ChatGPTAuth, CredentialStore, ISSUER, JWKS,
                                  PLAN_SCOPE, RESOURCE, TOKEN, _request_json, verify_identity)
from miniagent.cli import main
from miniagent.config import Config, load_api_key
from miniagent.processes import ProcessManager
from miniagent.security import Redactor
from miniagent.tools import _protected


def token_response(**changes):
    result = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "id_token": "synthetic-id",
              "token_type": "Bearer", "expires_in": 3600, "scope": PLAN_SCOPE + " resource.invoke offline_access"}
    result.update(changes)
    return result


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CredentialStore(Path(self.temp.name) / "MiniAgent")
        self.redact = Redactor()
        self.auth = ChatGPTAuth(self.store, redact=self.redact)
        if os.name == "nt":
            # Unit tests exercise the store/protocol with a deterministic codec.
            # Actual Windows encryption is covered separately below.
            codec = patch("miniagent.chatgpt_auth._dpapi", side_effect=lambda raw, decrypt=False:
                          base64.b64decode(raw) if decrypt else base64.b64encode(raw))
            codec.start()
            self.addCleanup(codec.stop)

    def seed(self, **changes):
        record = {"client_id": "oaiapp_test", "subject": "subject", "email": "test@example.invalid",
                  "access_token": "old-access", "refresh_token": "old-refresh", "id_token": "old-id",
                  "expires_at": time.time() + 3600, "scopes": [PLAN_SCOPE, "resource.invoke", "offline_access"]}
        record.update(changes)
        with self.store.locked():
            state = self.store.read()
            state["accounts"]["default"] = record
            self.store.write(state)
        return state

    def test_store_roundtrip_atomic_permissions_and_host(self):
        state = self.seed()
        self.assertEqual(self.store.read(), state)
        self.assertEqual(len(list(self.store.directory.glob("chatgpt-auth-*"))), 0)
        if os.name == "nt":
            self.assertNotIn(b"old-refresh", self.store.path.read_bytes())
            self.assertTrue(self.store.path.read_bytes().startswith(b"MA-DPAPI-1\n"))
        else:
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        with self.store.locked():
            with self.assertRaises(AuthError):
                with self.store.locked():
                    pass
        with self.store.locked():
            self.assertEqual(self.store.read()["host_id"], state["host_id"])

    def test_no_auth_fails_without_loading_key_files(self):
        with self.assertRaisesRegex(AuthError, "miniagent login"):
            self.auth.access_token()
        self.assertFalse(self.store.path.exists())
        with self.assertRaises(ValueError):
            load_api_key(Config(provider="chatgpt"), Path(self.temp.name))
        for endpoint in ("https://example.com/v1", "http://localhost:8000", "https://api.openai.com/v1/responses"):
            with self.assertRaises(ValueError):
                Config(provider="chatgpt", base_url=endpoint)

    def test_refresh_rotation_redaction_and_subprocess_environment(self):
        state = self.seed(expires_at=0)
        response = token_response()
        response.pop("id_token")
        with patch("miniagent.chatgpt_auth._request_json", return_value=response) as request:
            self.assertEqual(self.auth.access_token(), "synthetic-access")
            self.assertEqual(self.auth.access_token(), "synthetic-access")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], TOKEN)
        self.assertEqual(request.call_args.args[1], {"grant_type": "refresh_token", "client_id": "oaiapp_test",
                                                   "refresh_token": "old-refresh", "resource": RESOURCE})
        self.assertEqual(self.store.read()["host_id"], state["host_id"])
        self.assertEqual(self.store.read()["accounts"]["default"]["refresh_token"], "synthetic-refresh")
        for token in ("old-access", "old-refresh", "old-id", "synthetic-access", "synthetic-refresh"):
            self.assertEqual(self.redact(token), "[REDACTED]")
        manager = ProcessManager(Path(self.temp.name), lambda *a: False, self.redact)
        with patch.dict(os.environ, {"INNOCENT_NAME": "prefix-synthetic-refresh"}):
            self.assertNotIn("INNOCENT_NAME", manager._environment())

    def test_failed_refresh_does_not_replace_credentials_or_echo_tokens(self):
        original = self.seed(expires_at=0)
        for response in (token_response(scope="openid"), token_response(expires_in=float("nan")), token_response(token_type="wrong"), token_response(token_type=123)):
            with patch("miniagent.chatgpt_auth._request_json", return_value=response), self.assertRaises(AuthError):
                self.auth.access_token()
            self.assertEqual(self.store.read(), original)

    def test_new_authorization_requires_current_grant_even_with_saved_scopes(self):
        previous = {"scopes": [PLAN_SCOPE, "resource.invoke", "offline_access"]}
        with self.assertRaises(AuthError):
            self.auth._tokens(token_response(scope=None), previous, "oaiapp_test", "nonce")

    def test_logout_clears_only_selected_tokens_even_if_revocation_fails(self):
        original = self.seed()
        original["accounts"]["other"] = dict(original["accounts"]["default"])
        with self.store.locked():
            self.store.write(original)
        with patch("miniagent.chatgpt_auth._request_json", side_effect=AuthError("Network unavailable")):
            self.assertFalse(self.auth.logout())
        state = self.store.read()
        self.assertEqual(state["host_id"], original["host_id"])
        self.assertEqual(state["accounts"]["other"], original["accounts"]["other"])
        self.assertEqual(state["accounts"]["default"]["client_id"], "oaiapp_test")
        self.assertNotIn("refresh_token", state["accounts"]["default"])
        self.assertIn("default: signed out", self.auth.status())
        self.assertNotIn("old-", self.auth.status())

    def test_logout_revoke_uses_discovery_and_refresh_not_access_token(self):
        self.seed()
        endpoint = ISSUER + "/revoke"
        with patch("miniagent.chatgpt_auth._request_json", side_effect=[{"revocation_endpoint": endpoint}, {}]) as request:
            self.assertTrue(self.auth.logout())
        self.assertEqual(request.call_args.args, (endpoint, {"token": "old-refresh", "token_type_hint": "refresh_token", "client_id": "oaiapp_test"}))
        self.assertTrue(request.call_args.kwargs["empty"])

    def test_auth_http_rejects_redirects_and_untrusted_destinations(self):
        for url in ("http://auth.openai.com/token", "https://evil.invalid/token", "https://auth.openai.com@evil.invalid/token"):
            with self.assertRaises(AuthError):
                _request_json(url, {"token": "do-not-leak"})
        error = urllib.error.HTTPError(TOKEN, 302, "synthetic-secret", {"Location": "https://evil.invalid"}, io.BytesIO(b"synthetic-secret"))
        with patch("urllib.request.OpenerDirector.open", side_effect=error), self.assertRaises(AuthError) as caught:
            _request_json(TOKEN, {"refresh_token": "synthetic-secret"})
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def run_login(self, *, prior=False, callback_changes=None, claims=None):
        if prior:
            self.seed()
        received = {}

        def browser(url):
            # The callback runs after browser.open returns, so deliver it in a thread.
            import threading
            query = parse_qs(urlsplit(url).query)
            received.update({key: value[0] for key, value in query.items()})
            def deliver():
                callback = {"state": received["state"], "code": "synthetic-code", "client_id": "oaiapp_test"}
                callback.update(callback_changes or {})
                try:
                    with urllib.request.urlopen(received["redirect_uri"] + "?" + urlencode(callback), timeout=2) as response:
                        response.read()
                except urllib.error.HTTPError:
                    pass
            thread = threading.Thread(target=deliver)
            thread.start()
            self.addCleanup(thread.join, 3)

        def exchange(url, form):
            self.assertEqual(url, TOKEN)
            self.assertEqual(form["redirect_uri"], received["redirect_uri"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            self.assertEqual(challenge, received["code_challenge"])
            self.assertEqual(form["client_id"], "oaiapp_test")
            return token_response()

        with patch("miniagent.chatgpt_auth._jwt_module"), patch("webbrowser.open", side_effect=browser), \
             patch("miniagent.chatgpt_auth._request_json", side_effect=exchange) as request, \
             patch("miniagent.chatgpt_auth.verify_identity", return_value=claims or {"sub": "subject"}) as verify:
            self.auth.login(lambda _: None, timeout=0.6)
        self.assertEqual(verify.call_args.args[2], received["nonce"])
        return received, request.call_count

    def test_loopback_login_pkce_registration_and_reauthorization(self):
        received, count = self.run_login()
        self.assertEqual(received["client_id"], "dynamic_agent_client")
        self.assertEqual(received["agent_name_hint"], "MiniAgent")
        self.assertEqual(count, 1)
        first_host = received["ext_agent_host_id"]
        received, _ = self.run_login(prior=True)
        self.assertEqual(received["ext_agent_host_id"], first_host)
        self.assertEqual(received["client_id"], "oaiapp_test")
        self.assertNotIn("agent_name_hint", received)
        self.assertNotIn("id_token_hint", received)

    def test_callback_state_client_and_identity_fail_closed(self):
        for changes in ({"state": "wrong"}, {"error": "access_denied"}, {"client_id": "dynamic_agent_client"}):
            with self.subTest(changes=changes), self.assertRaises(AuthError):
                self.run_login(callback_changes=changes)
            self.assertEqual(self.store.read()["accounts"], {})
        with self.assertRaises(AuthError):
            self.run_login(prior=True, claims={"sub": "different-person"})
        self.assertEqual(self.store.read()["accounts"]["default"]["access_token"], "old-access")

    def test_cli_auth_commands_do_not_start_agent_or_load_api_keys(self):
        output = io.StringIO()
        with patch("miniagent.cli.ChatGPTAuth", return_value=self.auth), patch("miniagent.cli.load_api_key") as keys, \
             patch("miniagent.config.user_directory", return_value=Path(self.temp.name) / "profile"), \
             patch.object(self.auth, "login") as login, redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(main(["login", "--provider", "chatgpt"]), 0)
            self.assertEqual(main(["login-status"]), 0)
            self.assertEqual(main(["logout"]), 0)
        login.assert_called_once()
        keys.assert_not_called()
        self.assertFalse((Path(self.temp.name) / ".miniagent").exists())
        self.assertTrue(_protected(("chatgpt-auth.dat",)))
        self.assertTrue(_protected(("chatgpt-auth-abc",)))


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class WindowsStorageTests(unittest.TestCase):
    def test_real_dpapi_store_roundtrip(self):
        import ctypes
        from miniagent.chatgpt_auth import _dpapi
        try:
            protected = _dpapi(b"synthetic-dpapi-probe")
        except AuthError:
            if ctypes.get_last_error() == 2:
                self.skipTest("Windows DPAPI user profile unavailable in this execution environment")
            raise
        self.assertEqual(_dpapi(protected, decrypt=True), b"synthetic-dpapi-probe")
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(directory)
            with store.locked():
                state = store.read()
                state["accounts"]["test"] = {"refresh_token": "synthetic-dpapi-secret"}
                store.write(state)
                self.assertEqual(store.read(), state)
            self.assertNotIn(b"synthetic-dpapi-secret", store.path.read_bytes())


@unittest.skipUnless(importlib.util.find_spec("jwt") and importlib.util.find_spec("cryptography"), "install .[chatgpt] for signed JWT tests")
class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.jwt = jwt
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private.public_key()))
        cls.jwk.update(kid="test-key", use="sig", alg="RS256")

    def signed(self, **changes):
        claims = {"iss": ISSUER, "aud": "oaiapp_test", "sub": "subject", "iat": int(time.time()),
                  "exp": int(time.time()) + 600, "nonce": "expected"}
        claims.update(changes)
        return self.jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": "test-key"})

    def test_real_signature_and_required_claims(self):
        with patch("miniagent.chatgpt_auth._request_json", return_value={"keys": [self.jwk]}):
            self.assertEqual(verify_identity(self.signed(), "oaiapp_test", "expected")["sub"], "subject")
            for changes in ({"iss": "https://evil.invalid"}, {"aud": "other"}, {"exp": 1}, {"nonce": "wrong"}, {"sub": ""}, {"azp": "other"}):
                with self.subTest(changes=changes), self.assertRaises(AuthError):
                    verify_identity(self.signed(**changes), "oaiapp_test", "expected")
            token = self.signed()
            head, payload, signature = token.split(".")
            forged = head + "." + payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
            with self.assertRaises(AuthError):
                verify_identity(forged, "oaiapp_test", "expected")


if __name__ == "__main__":
    unittest.main()
