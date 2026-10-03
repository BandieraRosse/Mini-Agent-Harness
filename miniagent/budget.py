"""Provider-independent context budgeting; estimates are not exact tokenization."""

import json
import math

CONTEXT_TOKENS = 256_000


def estimate_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    ascii_bytes = len(text.encode("ascii", errors="ignore"))
    other_bytes = len(text.encode("utf-8")) - ascii_bytes
    return math.ceil(ascii_bytes / 3 + other_bytes / 2)


def preview_result(message, limit):
    """Keep call pairing and operational metadata; the archive keeps full text."""
    if message.get("role") != "tool" or len(message["content"]) <= limit:
        return message
    content = message["content"]
    try:
        value = json.loads(content)
    except ValueError:
        value = {}
    keep = ("ok", "error_code", "exit_code", "status", "complete", "job_id", "purpose",
            "partial_write", "applied_files", "uncertain", "denied", "declined", "next_offset")
    result = {key: value[key] for key in keep if isinstance(value, dict) and key in value}
    result.update(result_ref=message["tool_call_id"], context_truncated=True,
                  next_action="Use read_tool_result with result_ref as tool_call_id for the full archived result.",
                  preview=content[:limit // 2] + "\n[... omitted ...]\n" + content[-limit // 2:])
    return {**message, "content": json.dumps(result, ensure_ascii=False)}
