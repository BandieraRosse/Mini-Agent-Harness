"""Atomic checkpoints, complete tool pairs, and an inspectable conversation archive."""

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .security import Redactor


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_and_repair(messages):
    if not isinstance(messages, list):
        raise ValueError("Session messages must be an array")
    result, index, seen = [], 0, set()
    while index < len(messages):
        message = messages[index]
        if (not isinstance(message, dict) or not isinstance(message.get("role"), str)
                or message["role"] not in {"user", "assistant", "tool"}):
            raise ValueError("Invalid session message")
        if message["role"] == "tool":
            raise ValueError("Orphan tool result in session")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise ValueError("Invalid message content")
        if message["role"] == "user" and not isinstance(message.get("content"), str):
            raise ValueError("User messages must contain text")
        if message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str):
            raise ValueError("Invalid stored reasoning content")
        result.append(message)
        index += 1
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("Stored tool calls must be an array")
        if calls and message["role"] != "assistant":
            raise ValueError("Only assistants can call tools")
        ids = []
        for call in calls:
            if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                    or call.get("type") != "function" or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("name"), str) or not call["function"]["name"]
                    or not isinstance(call["function"].get("arguments"), str)):
                raise ValueError("Invalid stored tool call")
            call_id = call["id"]
            if not call_id or call_id in seen:
                raise ValueError("Duplicate stored tool call ID")
            seen.add(call_id)
            ids.append(call_id)
        outputs = {}
        while index < len(messages) and isinstance(messages[index], dict) and messages[index].get("role") == "tool":
            output = messages[index]
            call_id = output.get("tool_call_id")
            if call_id not in ids or call_id in outputs or not isinstance(output.get("content"), str):
                raise ValueError("Invalid tool result pairing")
            outputs[call_id] = output
            index += 1
        for call_id in ids:
            result.append(outputs.get(call_id, {
                "role": "tool", "tool_call_id": call_id,
                "content": json.dumps({"ok": False, "interrupted": True,
                    "error": "Execution outcome unknown after interruption. Inspect current state; do not replay automatically."}),
            }))
    return result


class Session:
    def __init__(self, workspace, provider, model, *, save=True, redact=None):
        self.workspace = Path(workspace).resolve()
        self.enabled = save
        self.redact = redact or Redactor()
        self.data = {
            "version": 1, "id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8],
            "workspace": str(self.workspace), "provider": provider, "model": model,
            "created_at": now(), "updated_at": now(), "messages": [],
            "summary": "", "context_start": 0, "status": "idle",
        }

    @property
    def messages(self):
        return self.data["messages"]

    @property
    def directory(self):
        root = self.workspace / ".miniagent"
        directory = root / "sessions"
        for path in (root, directory):
            if path.is_symlink() or path.resolve().is_relative_to(self.workspace) is False:
                raise ValueError("Session directory must be inside the project, without symlinks")
        return directory

    @property
    def path(self):
        return self.directory / (self.data["id"] + ".json")

    def save(self):
        if not self.enabled:
            return
        self.data["updated_at"] = now()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = json.dumps(self.redact.value(self.data), ensure_ascii=False, indent=2) + "\n"
        fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def append(self, message):
        self.messages.append(self.redact.value(message))
        self.save()

    def active_messages(self):
        history = self.messages[self.data["context_start"]:]
        if self.data["summary"]:
            return [{"role": "user", "content": "Memory of earlier work (inspect actual state before acting):\n"
                     + self.data["summary"]}, *history]
        return list(history)

    def repair(self):
        self.data["messages"] = validate_and_repair(self.messages)
        self.data["status"] = "interrupted"
        self.save()

    def list_saved(self):
        if not self.directory.exists():
            return []
        items = []
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size > 32_000_000:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                    continue
                title = next((m.get("content", "") for m in data["messages"]
                              if isinstance(m, dict) and m.get("role") == "user"), "")
                if not isinstance(title, str) or not isinstance(data.get("updated_at", ""), str):
                    continue
                items.append({"id": path.stem, "updated_at": data.get("updated_at", ""), "title": self.redact(title)[:90]})
            except (ValueError, OSError, AttributeError):
                continue
        return items

    def load(self, identifier):
        if identifier == "latest":
            items = self.list_saved()
            if not items:
                raise ValueError("No saved sessions in this project")
            identifier = max(items, key=lambda item: item["updated_at"])["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise ValueError("Use a session ID from /sessions")
        path = self.directory / f"{identifier}.json"
        if path.is_symlink() or path.stat().st_size > 32_000_000:
            raise ValueError("Unsafe or oversized session file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("id") != identifier:
            raise ValueError("Unsupported or invalid session")
        if not isinstance(data.get("workspace"), str) or Path(data["workspace"]).resolve() != self.workspace:
            raise ValueError("Session belongs to another workspace")
        messages = validate_and_repair(data.get("messages"))
        start = data.get("context_start", 0)
        if (type(start) is not int or not 0 <= start <= len(messages)
                or not isinstance(data.get("summary", ""), str)
                or start < len(messages) and messages[start]["role"] == "tool"):
            raise ValueError("Invalid session context boundary")
        data["messages"] = messages
        data["context_start"] = start
        data.setdefault("summary", "")
        data["status"] = "resumed"
        self.data = self.redact.value(data)
        self.save()
        return self
