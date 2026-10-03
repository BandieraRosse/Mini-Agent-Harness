"""Redaction is hygiene, not a sandbox or a secret scanner."""

import re


class Redactor:
    def __init__(self, *secrets):
        self.secrets = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def __call__(self, text):
        text = str(text)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)

    def value(self, value):
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self(str(key)): self.value(item) for key, item in value.items()}
        return value


class StreamRedactor:
    """Release ordinary text immediately; retain possible secret prefixes only."""

    def __init__(self, redact):
        self.redact = redact
        self.pending = ""

    def feed(self, text, *, final=False):
        self.pending += text
        if final:
            result, self.pending = self.redact(self.pending), ""
            return result
        for secret in self.redact.secrets:
            self.pending = self.pending.replace(secret, "[REDACTED]")
        hold = 0
        for secret in self.redact.secrets:
            for length in range(min(len(secret) - 1, len(self.pending)), 0, -1):
                if self.pending.endswith(secret[:length]):
                    hold = max(hold, length)
                    break
        generic = re.search(r"\b(?:s|sk|sk-[A-Za-z0-9_-]*)$", self.pending)
        if generic:
            hold = max(hold, len(generic.group()))
        if hold > 8192:
            self.pending = self.pending[:-hold] + "[REDACTED]"
            hold = 0
        split = len(self.pending) - hold
        result, self.pending = self.redact(self.pending[:split]), self.pending[split:]
        return result
