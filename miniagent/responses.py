"""ChatGPT plan Responses adapter; shares transport, never executes tools.

The complete response is authoritative. Partial calls and failed/incomplete
streams cannot escape this module as executable tool requests.
"""

import copy
import json

from .api import APIError, ChatClient, _events, _validate_completion
from .messages import classify


class ResponsesClient(ChatClient):
    def __init__(self, config, auth):
        super().__init__(config, "oauth-token-not-loaded")
        self.auth = auth

    def redact(self, text):
        return self.auth.redact(text)

    def _authorize(self):
        self.check_cancelled()
        self._api_key = self.auth.access_token()
        self.check_cancelled()

    def list_models(self):
        self._authorize()
        return super().list_models()

    def _model_catalog(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise APIError("ChatGPT returned no account model catalog")
        if len(data["models"]) > 4096 or any(not isinstance(item, dict) for item in data["models"]):
            raise APIError("ChatGPT returned an invalid model catalog")
        return {"data": [{"id": item.get("slug")} for item in data["models"] if item.get("visibility") == "list"]}

    def complete(self, messages, tools=None, on_text=None, stream=True):
        self._authorize()
        # The subscription transport requires SSE, including compaction requests.
        return super().complete(messages, tools, on_text=on_text, stream=True)

    def _payload(self, messages, tools, stream):
        items = []
        for message in messages:
            role = message["role"]
            content = message.get("content")
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content})
                continue
            if role == "assistant":
                reasoning = message.get("responses_reasoning", [])
                if not isinstance(reasoning, list) or any(not isinstance(item, dict) or item.get("type") != "reasoning" for item in reasoning):
                    raise APIError("Invalid saved Responses reasoning items")
                items.extend(copy.deepcopy(reasoning))
                phase = message.get("phase")
                if message.get("completion_deferred"):
                    phase = "commentary"
                    content = "Unaccepted draft; required job results were still pending:\n" + (content or "")
                if content:
                    # Preserve explicit phase on native assistant messages.
                    item = {"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": content, "annotations": []}]}
                    if phase:
                        item["phase"] = phase
                    items.append(item)
                for call in message.get("tool_calls", []):
                    items.append({"type": "function_call", "call_id": call["id"], "namespace": "miniagent",
                                  "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
            else:
                items.append({"role": "developer" if role == "system" else role, "content": content})
        payload = {"model": self.config.model, "input": items, "store": False, "stream": True,
                   "include": ["reasoning.encrypted_content"]}
        if tools:
            functions = [{"type": "function", **tool["function"], "strict": False} for tool in tools]
            payload["tools"] = [{"type": "namespace", "name": "miniagent",
                                 "description": "MiniAgent workspace tools", "tools": functions}]
        return payload

    def _stream(self, response, on_text, progress):
        for event in _events(response, terminal="response.completed"):
            self.check_cancelled()
            try:
                chunk = json.loads(event)
            except (ValueError, UnicodeError):
                raise APIError("Responses stream contains invalid JSON") from None
            if not isinstance(chunk, dict) or not isinstance(chunk.get("type"), str):
                raise APIError("Responses stream contains an invalid event")
            kind = chunk["type"]
            if kind in {"error", "response.failed", "response.incomplete"}:
                raise APIError("ChatGPT response failed or was incomplete; no tools accepted. Check account access and usage limits.")
            if kind == "response.completed":
                result = self._completion(chunk.get("response"))
                self.check_cancelled()
                message = result["choices"][0]["message"]
                if on_text and message.get("content"):
                    prefix = f"[{message['phase']}]\n" if message.get("phase") else ""
                    on_text(prefix + message["content"])
                return result
            if kind not in {"response.created", "response.queued", "response.in_progress"}:
                # Never retry after output or an unknown potentially generative event.
                progress["generated"] = True
        raise APIError("Responses stream ended without response.completed; no tools accepted")

    def _completion(self, response):
        if (not isinstance(response, dict) or response.get("status") != "completed"
                or response.get("error") or response.get("incomplete_details")
                or not isinstance(response.get("output"), list)):
            raise APIError("Responses did not complete successfully; no tools accepted")
        calls, texts, reasoning = [], [], []
        last_phase = None
        refused = False
        for item in response["output"]:
            if not isinstance(item, dict):
                raise APIError("Malformed Responses output")
            kind = item.get("type")
            if kind == "reasoning":
                reasoning.append(copy.deepcopy(item))
                continue
            if item.get("status") != "completed":
                raise APIError("Incomplete Responses output item; no tools accepted")
            if kind == "function_call":
                if item.get("namespace") not in (None, "miniagent"):
                    raise APIError("Responses returned an unknown tool namespace")
                calls.append({"id": item.get("call_id"), "type": "function", "function": {
                    "name": item.get("name"), "arguments": item.get("arguments")}})
            elif kind == "message":
                if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                    raise APIError("Invalid Responses assistant message")
                parts = []
                for part in item["content"]:
                    if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                        raise APIError("Unsupported Responses content")
                    value = part.get("text") if part["type"] == "output_text" else part.get("refusal")
                    if not isinstance(value, str):
                        raise APIError("Responses content must be text")
                    refused = refused or part["type"] == "refusal"
                    parts.append(value)
                try:
                    msg = classify({"role": "assistant", "content": "".join(parts), "phase": item.get("phase")})
                except ValueError:
                    raise APIError("Conflicting Responses message phase") from None
                texts.append(msg["content"])
                last_phase = msg.get("phase")
            else:
                raise APIError("Unsupported Responses output item; no tools accepted")
        message = {"role": "assistant", "content": "\n".join(texts) or None}
        if not message["content"] and not calls:
            raise APIError("Responses completed without assistant text or tool calls; no usable answer received")
        if calls:
            if last_phase == "final_answer":
                raise APIError("Final Responses answer contains tool calls")
            message["tool_calls"] = calls
        if last_phase:
            message["phase"] = last_phase
        if refused and not last_phase and not calls:
            message["phase"] = "final_answer"
        if reasoning:
            message["responses_reasoning"] = reasoning
        raw_usage = response.get("usage") or {}
        if not isinstance(raw_usage, dict):
            raise APIError("Invalid Responses usage")
        usage = {"prompt_tokens": raw_usage.get("input_tokens", 0),
                 "completion_tokens": raw_usage.get("output_tokens", 0), "total_tokens": raw_usage.get("total_tokens", 0)}
        return _validate_completion({"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
                                     "usage": usage})
