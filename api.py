"""The deliberately small DeepSeek HTTP client."""

import json
import urllib.error
import urllib.request


API_URL = "https://api.deepseek.com/chat/completions"


def build_payload(model, instructions, world_state, messages, tools, tool_choice):
    # DeepSeek's Chat Completions API calls these fields system/messages.
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "system", "content": "Current world state: " + json.dumps(world_state)},
            *messages,
        ],
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": False,
    }


def chat(api_key, model, instructions, world_state, messages, tools, tool_choice):
    payload = build_payload(model, instructions, world_state, messages, tools, tool_choice)
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"DeepSeek connection failed: {error.reason}") from error
