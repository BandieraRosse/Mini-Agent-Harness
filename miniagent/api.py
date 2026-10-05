"""Chat Completions with streaming, no SDK dependency, and fail-closed tool assembly."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from . import __version__
from .config import Config, _clean_key
from .messages import classify


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class APIError(RuntimeError):
    """A safe, user-facing API failure. Never attach an unredacted HTTP exception."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class _StreamEnded(APIError):
    """EOF without a terminal event; contains protocol metadata only."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even same-origin redirects can turn into cross-origin credential forwarding.
        return None


def _connection_failure(error):
    """Classify without exposing exception strings, proxy passwords or headers."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed; check system trust and proxy certificates (verification remains enabled)"
    if isinstance(reason, ssl.SSLEOFError):
        return "TLS connection closed unexpectedly (SSL EOF); check the proxy/VPN connection and retry"
    if isinstance(reason, ssl.SSLError):
        return "TLS handshake failed; check the proxy/VPN and system TLS configuration"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed; check network and proxy hostname resolution"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "Connection timed out; check the network/proxy or increase --timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "Connection refused; check that the configured proxy or endpoint is running"
    if isinstance(reason, (ConnectionResetError, http.client.RemoteDisconnected)):
        return "Connection reset or closed by the peer; check the network/proxy and retry"
    return "Connection failed; check the endpoint, network and proxy settings"


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def http_open(self, request):
        return self.do_open(lambda host, **kw: self.client._connection(http.client.HTTPConnection, host, **kw), request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def https_open(self, request):
        return self.do_open(lambda host, **kw: self.client._connection(http.client.HTTPSConnection, host, **kw), request,
                            context=self._context)


def _events(response: Any, terminal: str = "[DONE]") -> Iterator[str]:
    """Parse complete SSE events, including comments and multiline data fields."""
    data: list[str] = []
    event_bytes = total_bytes = 0
    events = 0
    while True:
        raw = response.readline(MAX_EVENT_BYTES + 1)
        if not raw:
            # Some compatible gateways close immediately after the terminal
            # line. Accept only the explicit marker, never an unfinished chunk.
            pending = "\n".join(data)
            if pending == terminal:
                yield pending
                return
            if pending:
                try:
                    parsed = json.loads(pending)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("error"):
                    yield pending
            raise _StreamEnded(
                f"API stream ended before {terminal}; no tool calls were accepted "
                f"(SSE events={events}, pending event={'yes' if data else 'no'})",
                retryable=True)
        total_bytes += len(raw)
        event_bytes += len(raw)
        if event_bytes > MAX_EVENT_BYTES or total_bytes > MAX_RESPONSE_BYTES:
            raise APIError("API response exceeded the streaming size limit")
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise APIError("API stream contains invalid UTF-8") from None
        if not line:
            if data:
                events += 1
                yield "\n".join(data)
                data = []
            event_bytes = 0
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        # SSE comments, event names, IDs, and retry fields carry no completion data.


def _validate_completion(result: Any) -> dict:
    """Reject incomplete transport/envelopes; argument semantics belong to the tool layer."""
    if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
        raise APIError("API response has no valid choices")
    if len(result["choices"]) != 1 or not isinstance(result["choices"][0], dict):
        raise APIError("API must return exactly one completion choice")
    choice = result["choices"][0]
    reason = choice.get("finish_reason")
    if reason not in ("stop", "tool_calls"):
        if reason == "length":
            raise APIError("Model output reached its token limit; no tool calls were accepted")
        raise APIError("Model response did not finish successfully; no tool calls were accepted")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise APIError("API response has no valid assistant message")
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise APIError("API assistant content must be text")
    for field in ("refusal", "reasoning_content"):
        if message.get(field) is not None and not isinstance(message[field], str):
            raise APIError("API assistant metadata must be text")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 128:
        raise APIError("API returned an invalid tool call list")
    if bool(calls) != (reason == "tool_calls"):
        raise APIError("API tool calls do not match the completion finish reason")
    ids: set[str] = set()
    for call in calls:
        if not isinstance(call, dict):
            raise APIError("API returned a malformed tool call")
        function = call.get("function")
        call_id = call.get("id")
        if (not isinstance(call_id, str) or not call_id or call_id in ids
                or call.get("type") != "function" or not isinstance(function, dict)
                or not isinstance(function.get("name"), str) or not function["name"]
                or not isinstance(function.get("arguments"), str)):
            raise APIError("API returned incomplete or duplicate tool call metadata")
        ids.add(call_id)
    try:
        choice["message"] = classify(message, choice.get("end_turn"))
    except ValueError as exc:
        raise APIError(str(exc)) from None
    return result


class ChatClient:
    def __init__(self, config: Config, api_key: str):
        self.config = config
        self._api_key = _clean_key(api_key)
        self.cancel_event: threading.Event | None = None
        self._active_socket = None
        self._socket_lock = threading.Lock()
        # An explicit empty handler disables urllib's environment/system proxy
        # discovery for this client without changing the process environment.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(), _HTTPHandler(self), _HTTPSHandler(self))

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt()

    def cancel(self) -> None:
        """Interrupt established network reads without waiting on a buffered reader lock.

        DNS, connecting and TLS negotiation still use the configured timeout. The
        caller must wait for the current operation to stop before reusing this client.
        """
        if self.cancel_event is None:
            self.cancel_event = threading.Event()
        self.cancel_event.set()
        with self._socket_lock:
            active = self._active_socket
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _release_socket(self):
        with self._socket_lock:
            active, self._active_socket = self._active_socket, None
            if active is not None:
                active.close()

    def _connection(self, kind, host, **kwargs):
        connection = kind(host, **kwargs)
        connect = connection.connect

        def tracked_connect():
            self.check_cancelled()
            connect()
            # urllib closes the connection socket after handing its makefile to
            # HTTPResponse. Keep a separate handle so shutdown still interrupts
            # that reader. A plain socket handle also works for TLS shutdown.
            sock = connection.sock
            with self._socket_lock:
                self._active_socket = socket.socket(
                    sock.family, sock.type, sock.proto, fileno=socket.dup(sock.fileno()))
            self.check_cancelled()

        connection.connect = tracked_connect
        return connection

    def list_models(self) -> list[str]:
        """Return validated model IDs advertised by this endpoint; never change models."""
        self.check_cancelled()
        endpoint = self.config.endpoint.removesuffix("/chat/completions").removesuffix("/responses") + "/models"
        request = urllib.request.Request(endpoint, headers={
            "Authorization": f"Bearer {self._api_key}", "Accept": "application/json",
            "User-Agent": f"MiniAgent/{__version__}"}, method="GET")
        try:
            with self._opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            self.check_cancelled()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise APIError("API model list exceeded the size limit")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise APIError("API model list is not valid JSON") from None
            data = self._model_catalog(data)
            if (not isinstance(data, dict) or not isinstance(data.get("data"), list)
                    or len(data["data"]) > 4096):
                raise APIError("API returned no valid model list")
            models = []
            for item in data["data"]:
                model = item.get("id") if isinstance(item, dict) else None
                if (not isinstance(model, str) or not model or len(model) > 256
                        or any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in model)
                        or self._api_key in model):
                    raise APIError("API returned an invalid model ID")
                if model not in models:
                    models.append(model)
            if not models:
                raise APIError("API returned an empty model list")
            return models
        except urllib.error.HTTPError as error:
            try:
                detail = self._error_body(error.read(16384))
            except (OSError, http.client.HTTPException):
                detail = "Unable to read the provider error"
            finally:
                error.close()
            self.check_cancelled()
            raise APIError(f"API HTTP {error.code}: {detail}", error.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            self.check_cancelled()
            raise APIError("Unable to load models: " + _connection_failure(error)) from None
        finally:
            self._release_socket()

    def redact(self, text: str) -> str:
        return text.replace(self._api_key, "[REDACTED]")

    def _model_catalog(self, data):
        return data

    def _payload(self, messages: list, tools: list | None, stream: bool) -> dict:
        clean_messages = []
        for message in messages:
            clean = dict(message)
            clean.pop("responses_reasoning", None)
            phase = clean.pop("phase", None)
            if clean.pop('completion_deferred', False):
                phase = 'commentary'
                clean['content'] = 'Unaccepted draft; required job results were still pending:\n' + (clean.get('content') or '')
            if phase and isinstance(clean.get("content"), str):
                clean["content"] = f"[{phase}]\n" + clean["content"]
            if self.config.provider == "openai":
                clean.pop("reasoning_content", None)
            clean_messages.append(clean)
        payload: dict = {"model": self.config.model, "messages": clean_messages, "stream": stream}
        limit_field = "max_completion_tokens" if self.config.provider == "openai" else "max_tokens"
        payload[limit_field] = self.config.max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _error_body(self, body: bytes) -> str:
        try:
            parsed = json.loads(body)
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            value = error.get("message", "") if isinstance(error, dict) else error
            message = str(value) if value else "The provider rejected the request"
        except (ValueError, UnicodeError):
            # HTML/proxy errors can echo headers. Their body is not useful to a CLI user.
            message = "The provider returned a non-JSON error"
        return " ".join(self.redact(message).split())[:1000]

    def complete(
        self,
        messages: list,
        tools: list | None = None,
        on_text: Callable[[str], None] | None = None,
        stream: bool = True,
    ) -> dict:
        """Return a complete response, or raise without exposing executable partial calls.

        Text callbacks are raw model fragments: the UI must redact secrets across chunk
        boundaries. A retry is allowed only before any text, reasoning, or call arrives.
        Ctrl+C propagates immediately; no partial response is returned. Text already
        delivered to the display callback may remain visible.
        """
        self.check_cancelled()
        try:
            body = json.dumps(self._payload(messages, tools, stream), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise APIError("Messages or tools cannot be encoded as JSON") from None
        for attempt in range(self.config.max_retries + 1):
            self.check_cancelled()
            progress = {"generated": False}
            request = urllib.request.Request(
                self.config.endpoint,
                data=body,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json",
                         "User-Agent": f"MiniAgent/{__version__}"},
                method="POST",
            )
            try:
                with self._opener.open(request, timeout=self.config.timeout) as response:
                    self.check_cancelled()
                    if stream:
                        return self._stream(response, on_text, progress)
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    self.check_cancelled()
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise APIError("API response exceeded the size limit")
                    try:
                        result = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise APIError("API response is not valid JSON") from None
                    if isinstance(result, dict) and result.get("error"):
                        raise APIError(self._error_body(raw))
                    result = _validate_completion(result)
                    message = result["choices"][0]["message"]
                    visible = message.get("content") or message.get("refusal")
                    if on_text and visible:
                        progress["generated"] = True
                        self.check_cancelled()
                        on_text(visible)
                    self.check_cancelled()
                    return result
            except urllib.error.HTTPError as error:
                try:
                    detail = self._error_body(error.read(16384))
                except (OSError, http.client.HTTPException):
                    detail = "Unable to read the provider error"
                finally:
                    error.close()
                failure = APIError(f"API HTTP {error.code}: {detail}", error.code,
                                   error.code in TRANSIENT_STATUSES)
            except (TimeoutError, socket.timeout):
                failure = APIError("API request timed out; try again or increase --timeout", retryable=True)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                failure = APIError("API connection failed: " + _connection_failure(error), retryable=True)
            except APIError as error:
                failure = error
            finally:
                self._release_socket()
            self.check_cancelled()
            if progress["generated"] or not failure.retryable or attempt >= self.config.max_retries:
                raise failure from None
            delay = min(0.5 * 2 ** attempt, 4.0)
            if self.cancel_event is not None:
                self.cancel_event.wait(delay)
            else:
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _stream(self, response: Any, on_text: Callable[[str], None] | None, progress: dict) -> dict:
        content: list[str] = []
        reasoning: list[str] = []
        refusal: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        phase = end_turn = None
        usage: dict = {}

        def events():
            try:
                yield from _events(response)
            except _StreamEnded as error:
                # Report enough to distinguish pre-output EOF from a lost final
                # marker without logging response text or tool arguments.
                raise APIError(
                    f"{error}; finish_reason={finish if finish in ('stop', 'tool_calls', 'length', 'content_filter') else 'missing/unknown'}, "
                    f"tool calls={len(calls)}, generated={'yes' if progress['generated'] else 'no'}",
                    retryable=True) from None

        for event in events():
            self.check_cancelled()
            if event == "[DONE]":
                break
            try:
                chunk = json.loads(event)
            except ValueError:
                raise APIError("API stream contains invalid JSON") from None
            if not isinstance(chunk, dict):
                raise APIError("API stream contains an invalid event")
            if chunk.get("error"):
                raise APIError(self._error_body(event.encode("utf-8")))
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise APIError("API stream contains invalid choices")
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    raise APIError("API stream contains an unexpected choice index")
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    raise APIError("API stream contains an invalid delta")
                if finish is not None:
                    raise APIError("API stream continued after its finish reason")
                if delta.get("role") not in (None, "assistant"):
                    raise APIError("API stream contains an unexpected role")
                if delta.get("phase") is not None:
                    if phase is not None and phase != delta["phase"]:
                        raise APIError("API stream changed assistant phase")
                    phase = delta["phase"]
                if choice.get("end_turn") is not None:
                    if end_turn is not None and end_turn != choice["end_turn"]:
                        raise APIError("API stream changed end_turn signal")
                    end_turn = choice["end_turn"]
                for name, target in (("content", content), ("reasoning_content", reasoning), ("refusal", refusal)):
                    part = delta.get(name)
                    if part is not None and not isinstance(part, str):
                        raise APIError("API stream contains non-text content")
                    if part:
                        progress["generated"] = True
                        target.append(part)
                        if name in ("content", "refusal") and on_text:
                            self.check_cancelled()
                            on_text(part)
                updates = delta.get("tool_calls") or []
                if not isinstance(updates, list):
                    raise APIError("API stream contains invalid tool calls")
                for update in updates:
                    progress["generated"] = True
                    if not isinstance(update, dict) or type(update.get("index")) is not int:
                        raise APIError("API stream contains a tool call without an index")
                    index = update["index"]
                    if not 0 <= index < 128:
                        raise APIError("API stream tool call index is out of range")
                    call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if update.get("type") not in (None, "function"):
                        raise APIError("API returned an unsupported tool call type")
                    function = update.get("function") or {}
                    if not isinstance(function, dict):
                        raise APIError("API stream contains an invalid function")
                    for source, key, target in ((update, "id", call), (function, "name", call["function"]), (function, "arguments", call["function"])):
                        value = source.get(key)
                        if value is not None:
                            if not isinstance(value, str):
                                raise APIError("API stream contains invalid tool call fragments")
                            target[key] += value
                if choice.get("finish_reason") is not None:
                    finish = choice["finish_reason"]
        self.check_cancelled()
        message: dict = {"role": "assistant", "content": "".join(content) or None}
        if phase is not None:
            message["phase"] = phase
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if refusal:
            message["refusal"] = "".join(refusal)
        if calls:
            if sorted(calls) != list(range(len(calls))):
                raise APIError("API stream omitted a tool call index")
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return _validate_completion({"choices": [{"index": 0, "message": message, "finish_reason": finish,
                                                 "end_turn": end_turn}], "usage": usage})
