"""Compact or full-color reader for the JSON traces produced by the harness."""

import argparse
import json
import os
import sys
from pathlib import Path


USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
COLORS = {
    "blue": "\033[34m", "cyan": "\033[36m", "green": "\033[32m",
    "magenta": "\033[35m", "yellow": "\033[33m", "red": "\033[31m",
    "dim": "\033[2m", "reset": "\033[0m",
}
PREVIEW_LENGTH = 80


def color(text, name):
    if not USE_COLOR:
        return str(text)
    return f"{COLORS[name]}{text}{COLORS['reset']}"


def preview(value, limit=PREVIEW_LENGTH):
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def show_json(label, value, color_name):
    print(color(label, color_name))
    print(color(json_text(value), color_name))


def response_message(response):
    choices = response.get("choices") or []
    return choices[0].get("message", {}) if choices else {}


def source_info(log_path, trace, key):
    filename = trace.get("fixed_context", {}).get(key, key)
    source = Path(filename)
    if not source.is_absolute():
        source = log_path.parent.parent / source
    try:
        length = len(source.read_text(encoding="utf-8"))
        return str(filename), length
    except OSError:
        return str(filename), "missing"


def fixed_message(messages, prefix):
    for message in messages:
        if message.get("role") == "system" and str(message.get("content", "")).startswith(prefix):
            return message
    return None


def history_messages(request):
    messages = request.get("messages") or []
    # api.build_payload puts Instructions, World State, and AGENTS.md first.
    return messages[3:]


def message_chars(messages):
    return sum(len(str(message.get("content") or "")) for message in messages)


def tool_call_summary(message):
    calls = message.get("tool_calls") or []
    summaries = []
    for call in calls:
        function = call.get("function", {})
        name = function.get("name", "unknown")
        try:
            arguments = json.loads(function.get("arguments", "{}"))
            args = ", ".join(
                f'{key}={preview(json.dumps(value, ensure_ascii=False))}'
                for key, value in arguments.items()
            )
        except (TypeError, json.JSONDecodeError):
            args = preview(function.get("arguments", ""))
        summaries.append(f"{name}({args})")
    return ", ".join(summaries)


def message_summary(message):
    role = message.get("role", "?")
    if role == "assistant" and message.get("tool_calls"):
        return f"{role:<10} {tool_call_summary(message)}"
    if role == "tool":
        try:
            result = json.loads(message.get("content", "{}"))
            status = "ok" if result.get("ok") else "failed"
            target = result.get("path", "")
            length = len(result.get("content", "")) if result.get("ok") else 0
            return f"{role:<10} {status} | {target} | {length} chars"
        except (TypeError, json.JSONDecodeError):
            return f"{role:<10} {preview(message.get('content'))}"
    return f"{role:<10} \"{preview(message.get('content'))}\""


def observation_summary(observation):
    result = observation.get("result") or {}
    status = "ok" if result.get("ok") else "failed"
    target = result.get("path", "")
    length = len(result.get("content", "")) if result.get("ok") else 0
    return f"{observation.get('name', 'unknown')} | {status} | {target} | {length} chars"


def usage_line(usage):
    usage = usage or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    return (
        f"prompt={usage.get('prompt_tokens', 0)} | cached={cached} | "
        f"output={usage.get('completion_tokens', 0)} | reasoning={reasoning}"
    )


def show_summary(round_data, log_path, trace):
    request = round_data.get("api_request", {})
    response = round_data.get("api_response", {})
    execution = round_data.get("execution", {})
    messages = request.get("messages") or []
    history = history_messages(request)
    message = response_message(response)
    user_messages = [item for item in history if item.get("role") == "user"]
    user = user_messages[-1] if user_messages else {}
    instructions_file, instructions_length = source_info(log_path, trace, "instructions")
    agents_file, agents_length = source_info(log_path, trace, "agents")
    tools_file, tools_length = source_info(log_path, trace, "tools")
    world = fixed_message(messages, "Current world state:")
    tools_schema = request.get("tools") or []
    tool_names = [item.get("function", {}).get("name", "unknown") for item in tools_schema]
    finish = (response.get("choices") or [{}])[0].get("finish_reason", "unknown")
    reply = message.get("content") or ""

    print(color(f"\n=== Round {round_data.get('round')} ===", "cyan"))
    print(color(f"\n[Instructions] file={instructions_file} | {instructions_length} chars", "blue"))
    print(color(f"[Project]      file={agents_file} | {agents_length} chars", "blue"))
    world_length = len(str(world.get("content", ""))) if world else 0
    print(color(f"[World State]  generated/runtime | {world_length} chars", "magenta"))
    print(color(f"[History]      {len(history)} messages | {message_chars(history)} chars", "blue"))
    print(color(f"[User]         {len(str(user.get('content') or ''))} chars | \"{preview(user.get('content'))}\"", "green"))
    print(color(f"[Tools]        file={tools_file} | {len(tools_schema)} tools | {', '.join(tool_names)} | {tools_length} chars", "yellow"))

    print(color("\nHistory:", "blue"))
    for item in history:
        print(f"  {message_summary(item)}")

    print(color("\n[Response]", "green"))
    print(f"finish={finish} | {len(reply)} chars")
    print(f'"{preview(reply)}"')
    for call in execution.get("tool_calls", []):
        print(color(f"tool call | {tool_call_summary({'tool_calls': [call]})}", "yellow"))
    for observation in execution.get("observations", []):
        print(color(f"observation | {observation_summary(observation)}", "yellow"))

    print(color("\n[Usage]", "dim"))
    print(color(usage_line(response.get("usage")), "dim"))


def show_verbose(round_data):
    request = round_data.get("api_request", {})
    response = round_data.get("api_response", {})
    execution = round_data.get("execution", {})
    messages = request.get("messages") or []
    print(color(f"\n========== Round {round_data.get('round')} ==========", "cyan"))
    print(color("REQUEST", "blue"))
    show_json("model", request.get("model"), "cyan")
    show_json("Instructions", messages[0].get("content") if len(messages) > 0 else None, "blue")
    show_json("World State", messages[1].get("content") if len(messages) > 1 else None, "magenta")
    show_json("AGENTS.md project context", messages[2].get("content") if len(messages) > 2 else None, "blue")
    show_json("History", history_messages(request), "blue")
    show_json("User input", next((item.get("content") for item in reversed(history_messages(request)) if item.get("role") == "user"), None), "green")
    show_json("Tools schema", request.get("tools"), "yellow")
    show_json("tool_choice", request.get("tool_choice"), "magenta")
    show_json("stream", request.get("stream"), "dim")
    print(color("RESPONSE", "green"))
    show_json("model response", response, "green")
    show_json("reasoning", response_message(response).get("reasoning_content"), "magenta")
    show_json("tool calls", execution.get("tool_calls"), "yellow")
    show_json("observations", execution.get("observations"), "yellow")
    show_json("usage", response.get("usage"), "dim")


def main():
    parser = argparse.ArgumentParser(description="Read a Mini Agent JSON log.")
    parser.add_argument("log", type=Path, help="Path to log/YYYY...json")
    parser.add_argument("--round", type=int, help="Only display this round")
    parser.add_argument("--verbose", action="store_true", help="Expand all logged JSON content")
    args = parser.parse_args()
    try:
        trace = json.loads(args.log.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"cannot read JSON log: {error}")

    rounds = trace.get("rounds", [])
    if args.round is not None:
        rounds = [item for item in rounds if item.get("round") == args.round]
        if not rounds:
            parser.error(f"round {args.round} was not found")
    print(color(f"LOG: {args.log}", "dim"))
    print(color(f"MODEL: {trace.get('model')}", "dim"))
    for round_data in rounds:
        if args.verbose:
            show_verbose(round_data)
        else:
            show_summary(round_data, args.log, trace)


if __name__ == "__main__":
    main()
