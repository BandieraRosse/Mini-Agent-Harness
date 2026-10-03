"""Independent ChatGPT plan OAuth. Never reads or writes Codex credentials.

Protocol: developers.openai.com/siwc/token-sharing-open-source/sign-in
Tokens belong to an account registration; the host ID survives logout.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import __version__
from .api import _NoRedirect
from .config import _clean_key, user_directory
from .security import Redactor

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
JWKS = ISSUER + "/.well-known/jwks.json"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")
MAX_AUTH_BYTES = 1024 * 1024


class AuthError(ValueError):
    """Only fixed, credential-free messages cross the authentication boundary."""


def auth_directory() -> Path:
    return user_directory()


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    """Windows user-bound encryption, with all buffers freed by their owner."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AuthError("Unable to protect/unprotect ChatGPT credentials for this Windows user")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class CredentialStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else auth_directory()
        self.path = self.directory / "chatgpt-auth.dat"

    def _prepare(self):
        if self.directory.is_symlink():
            raise AuthError("ChatGPT credential directory must not be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            if self.directory.stat().st_uid != os.getuid():
                raise AuthError("ChatGPT credential directory belongs to another user")
            self.directory.chmod(0o700)

    @contextmanager
    def locked(self):
        """OS lock is released on crashes; serialize rotating refresh tokens."""
        self._prepare()
        path = self.directory / "chatgpt-auth.lock"
        if path.is_symlink():
            raise AuthError("ChatGPT lock must not be a symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AuthError("Another MiniAgent process is updating ChatGPT login; try again shortly") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self):
        if self.path.is_symlink():
            raise AuthError("ChatGPT credential file must not be a symlink")
        try:
            if os.name != "nt":
                info = self.path.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise AuthError("ChatGPT credential file must be owned by you with mode 0600")
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_AUTH_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()), "accounts": {}}
        if len(raw) > MAX_AUTH_BYTES:
            raise AuthError("ChatGPT credential file is too large")
        try:
            if os.name == "nt":
                if not raw.startswith(b"MA-DPAPI-1\n"):
                    raise AuthError("ChatGPT credential file is not Windows-protected")
                raw = _dpapi(raw[11:], decrypt=True)
            state = json.loads(raw)
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("host_id"), str)
                    or not state["host_id"].startswith("urn:uuid:")
                    or not isinstance(state.get("accounts"), dict)
                    or any(not isinstance(v, dict) for v in state["accounts"].values())):
                raise ValueError()
            uuid.UUID(state["host_id"][9:])
            return state
        except (ValueError, UnicodeError):
            raise AuthError("Invalid ChatGPT credential file; restore it or sign in with a fresh store") from None

    def write(self, state):
        self._prepare()
        if self.path.is_symlink():
            raise AuthError("ChatGPT credential file must not be a symlink")
        raw = json.dumps(state, ensure_ascii=True).encode("utf-8")
        if os.name == "nt":
            raw = b"MA-DPAPI-1\n" + _dpapi(raw)
        if len(raw) > MAX_AUTH_BYTES:
            raise AuthError("ChatGPT credential store exceeded the size limit")
        fd, name = tempfile.mkstemp(prefix="chatgpt-auth-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def _jwt_module():
    try:
        import jwt
        import cryptography  # noqa: F401; make the optional dependency failure actionable
    except ImportError:
        raise AuthError('ChatGPT login needs the optional dependencies: python -m pip install ".[chatgpt]"') from None
    return jwt


def _request_json(url, form=None, *, empty=False):
    # Fixed OpenAI auth origin; discovery must not redirect bearer credentials.
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or parsed.query or parsed.fragment:
        raise AuthError("Untrusted OpenAI authentication endpoint")
    request = urllib.request.Request(url, data=urlencode(form).encode("ascii") if form is not None else None,
                                     headers={"Accept": "application/json", "User-Agent": f"MiniAgent/{__version__}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_AUTH_BYTES + 1)
        if len(raw) > MAX_AUTH_BYTES:
            raise AuthError("OpenAI authentication response exceeded the size limit")
        if empty and not raw:
            return {}
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise AuthError(f"OpenAI authentication returned HTTP {status}; retry login or check account access") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise AuthError("OpenAI authentication failed; check the network and retry login") from None


def verify_identity(token, client_id, nonce=None):
    jwt = _jwt_module()
    keys = _request_json(JWKS)
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256"} or not header.get("kid"):
            raise ValueError()
        matches = [key for key in keys.get("keys", []) if key.get("kid") == header["kid"]
                   and key.get("use", "sig") == "sig" and key.get("alg", header["alg"]) == header["alg"]]
        if len(matches) != 1:
            raise ValueError()
        key = jwt.PyJWK.from_dict(matches[0], algorithm=header["alg"]).key
        claims = jwt.decode(token, key, algorithms=[header["alg"]], audience=client_id,
                            issuer=ISSUER, leeway=5, options={"require": ["sub", "iss", "aud", "exp", "iat"]})
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError()
        if nonce is not None and claims.get("nonce") != nonce:
            raise ValueError()
        if claims.get("azp", client_id) != client_id:
            raise ValueError()
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
        raise AuthError("ChatGPT identity verification failed; credentials were not replaced") from None


class ChatGPTAuth:
    def __init__(self, store=None, account="default", redact=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account):
            raise AuthError("Account label must contain 1-64 letters, digits, underscores or hyphens")
        self.store = store or CredentialStore()
        self.account = account
        self.redact = redact or Redactor()

    def _remember(self, record):
        self.redact.add(*(record.get(field, "") for field in TOKEN_FIELDS))

    def _tokens(self, response, previous, client_id, nonce=None):
        self._remember(response)
        try:
            access = _clean_key(response["access_token"])
            refresh = _clean_key(response["refresh_token"])
            expires = response["expires_in"]
            if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= 0:
                raise ValueError()
            token_type = response.get("token_type")
            if not isinstance(token_type, str) or token_type.lower() != "bearer":
                raise ValueError()
            scope = response.get("scope")
            if nonce is not None and not isinstance(scope, str):
                raise AuthError("ChatGPT token response omitted granted permissions; sign in again")
            scopes = scope.split() if isinstance(scope, str) else previous.get("scopes", [])
            if not {PLAN_SCOPE, "resource.invoke", "offline_access"}.issubset(scopes):
                raise AuthError("ChatGPT plan permission was not granted; enable app access during login")
            identity = response.get("id_token")
            if nonce is not None or identity:
                claims = verify_identity(identity, client_id, nonce)
                if previous.get("subject") and previous["subject"] != claims["sub"]:
                    raise AuthError("ChatGPT account does not match this label; use another --account label")
                subject, email = claims["sub"], claims.get("email", "")
            else:
                subject, email = previous["subject"], previous.get("email", "")
            return {"client_id": client_id, "subject": subject, "email": email if isinstance(email, str) else "",
                    "access_token": access, "refresh_token": refresh,
                    "id_token": identity or previous.get("id_token", ""), "scopes": scopes,
                    "expires_at": time.time() + expires}
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, AuthError):
                raise
            raise AuthError("Invalid ChatGPT token response; credentials were not replaced") from None

    def login(self, notify=print, *, timeout=300):
        _jwt_module()
        with self.store.locked():
            state = self.store.read()
            self.store.write(state)  # Host identity must survive an unsuccessful login.
            previous = state["accounts"].get(self.account, {})
            self._remember(previous)
            client_id = previous.get("client_id", "dynamic_agent_client")
            nonce, csrf, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            outcome = {}

            class CallbackServer(HTTPServer):
                def get_request(self):
                    connection, address = super().get_request()
                    connection.settimeout(1)
                    return connection, address

            class Callback(BaseHTTPRequestHandler):
                def do_GET(handler):
                    parsed = urlsplit(handler.path)
                    try:
                        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
                    except ValueError:
                        query = {}
                    valid = (parsed.path == "/auth/callback" and not outcome
                             and handler.headers.get("Host") == f"127.0.0.1:{server.server_port}"
                             and query.get("state") == [csrf]
                             and all(len(values) == 1 for values in query.values()))
                    handler.send_response(200 if valid else 400)
                    handler.send_header("Content-Type", "text/plain; charset=utf-8")
                    handler.send_header("Cache-Control", "no-store")
                    handler.send_header("Referrer-Policy", "no-referrer")
                    handler.end_headers()
                    if valid:
                        outcome.update({k: v[0] for k, v in query.items()})
                    try:
                        handler.wfile.write(b"Return to MiniAgent to check sign-in." if valid else b"Invalid sign-in callback.")
                    except OSError:
                        pass

                def log_message(self, *args):
                    pass  # Never log callback codes or authorization URLs.

            with CallbackServer(("127.0.0.1", 0), Callback) as server:
                server.timeout = 0.25
                redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
                parameters = {"client_id": client_id, "ext_agent_host_id": state["host_id"],
                              "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
                              "resource": RESOURCE, "state": csrf, "nonce": nonce,
                              "code_challenge_method": "S256",
                              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
                if client_id == "dynamic_agent_client":
                    parameters["agent_name_hint"] = "MiniAgent"
                # Deliberately omit ID-token hints so the fallback URL contains no tokens.
                url = AUTHORIZE + "?" + urlencode(parameters)
                notify("Continue with ChatGPT — open this URL in your local browser:\n" + url)
                try:
                    webbrowser.open(url)
                except webbrowser.Error:
                    pass
                deadline = time.monotonic() + timeout
                while not outcome and time.monotonic() < deadline:
                    server.handle_request()
            if not outcome:
                raise AuthError("ChatGPT login timed out; run login again")
            if outcome.get("error"):
                raise AuthError("ChatGPT login was denied or unavailable; no credentials were replaced")
            issued = outcome.get("client_id", client_id)
            if (not outcome.get("code") or not issued.startswith("oaiapp_")
                    or (client_id != "dynamic_agent_client" and issued != client_id)):
                raise AuthError("Invalid ChatGPT registration callback")
            response = _request_json(TOKEN, {"grant_type": "authorization_code", "client_id": issued,
                                            "code": outcome["code"], "code_verifier": verifier,
                                            "redirect_uri": redirect, "resource": RESOURCE})
            record = self._tokens(response, previous, issued, nonce)
            state["accounts"][self.account] = record
            self.store.write(state)

    def access_token(self):
        with self.store.locked():
            state = self.store.read()
            record = state["accounts"].get(self.account, {})
            self._remember(record)
            if not record.get("refresh_token") or not record.get("access_token"):
                raise AuthError("Not signed in; run miniagent login --provider chatgpt with the same --account")
            if PLAN_SCOPE not in record.get("scopes", []):
                raise AuthError("ChatGPT plan access is not enabled; sign in again")
            expiry = record.get("expires_at")
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                raise AuthError("Invalid ChatGPT token expiry; sign in again")
            if expiry <= time.time() + 60:
                response = _request_json(TOKEN, {"grant_type": "refresh_token", "client_id": record["client_id"],
                                                "refresh_token": record["refresh_token"], "resource": RESOURCE})
                record = self._tokens(response, record, record["client_id"])
                state["accounts"][self.account] = record
                self.store.write(state)
            return _clean_key(record["access_token"])

    def status(self):
        if not self.store.path.exists():
            return "No ChatGPT accounts saved."
        with self.store.locked():
            state = self.store.read()
        rows = []
        for label, record in state["accounts"].items():
            signed_in = bool(record.get("refresh_token"))
            rows.append(f"{label}: {'signed in' if signed_in else 'signed out'}")
        return "\n".join(rows) or "No ChatGPT accounts saved."

    def logout(self):
        if not self.store.path.exists():
            return True
        with self.store.locked():
            state = self.store.read()
            record = state["accounts"].get(self.account, {})
            self._remember(record)
            revoked = not record.get("refresh_token")
            try:
                if not revoked:
                    discovery = _request_json(ISSUER + "/.well-known/openid-configuration")
                    endpoint = discovery.get("revocation_endpoint", "")
                    for attempt in range(2):
                        try:
                            _request_json(endpoint, {"token": record["refresh_token"],
                                                     "token_type_hint": "refresh_token", "client_id": record["client_id"]}, empty=True)
                            revoked = True
                            break
                        except AuthError:
                            if attempt == 0:
                                time.sleep(0.5)
            except (AuthError, KeyError):
                pass
            finally:
                # Preserve registration and host identity, including on failed revocation.
                for field in (*TOKEN_FIELDS, "expires_at", "scopes"):
                    record.pop(field, None)
                if record:
                    state["accounts"][self.account] = record
                self.store.write(state)
            return revoked
