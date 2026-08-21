"""The small Agent Loop: model response -> local tool -> observation -> model."""

import copy
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import api
import context
import tools


MODEL = "deepseek-v4-flash"
TOOL_CHOICE = "auto"
LOG_ROOT = Path(__file__).with_name("log")

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
COLORS = {
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "dim": "\033[2m", "reset": "\033[0m",
}


def colored(text, color):
    return f"{COLORS[color]}{text}{COLORS['reset']}" if USE_COLOR else str(text)


def usage_summary(usage):
    usage = usage or {}
    prompt = usage.get("prompt_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    return f"tokens: {prompt} in ({cached} cached) | {completion} out ({reasoning} reasoning)"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_session():
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f%z")
    LOG_ROOT.mkdir(exist_ok=True)
    log_path = LOG_ROOT / f"{timestamp}.json"
    trace = {
        "model": MODEL,
        "fixed_context": {
            "instructions": "INSTRUCTIONS.md",
            "agents": "AGENTS.md",
            "tools": "tools.json",
        },
        "rounds": [],
    }
    write_json(log_path, trace)
    return log_path, trace


def run(api_key, user_input):
    # Fixed context is loaded once. Conversation history and observations are mutable input.
    fixed = context.load_fixed_context()
    ctx = context.new_context(user_input, fixed["agents"])
    log_path, trace = make_session()
    print(colored(f"[日志] {log_path}", "dim"))

    for round_number in range(1, 100):
        # This is the exact JSON sent over HTTP, without the Authorization header/API key.
        api_request = copy.deepcopy(api.build_payload(
            MODEL, fixed["instructions"], ctx["world_state"], ctx["input"],
            tools.TOOL_SCHEMAS, TOOL_CHOICE,
        ))
        print(colored(f"[Round {round_number}] {MODEL}", "cyan"))
        response = api.chat(
            api_key, MODEL, fixed["instructions"], ctx["world_state"],
            ctx["input"], tools.TOOL_SCHEMAS, TOOL_CHOICE,
        )
        message = response["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        round_log = {
            "round": round_number,
            "api_request": api_request,
            "api_response": response,
            "execution": {"tool_calls": calls, "observations": []},
        }
        trace["rounds"].append(round_log)
        write_json(log_path, trace)

        # The assistant tool-call message must remain in history for the next request.
        assistant_message = {"role": "assistant", "content": message.get("content")}
        if message.get("reasoning_content") is not None:
            assistant_message["reasoning_content"] = message["reasoning_content"]
        if calls:
            assistant_message["tool_calls"] = calls
        ctx["input"].append(assistant_message)

        if not calls:
            print(colored(f"[完成] {message.get('content') or ''}", "green"))
            print(colored(usage_summary(response.get("usage")), "dim"))
            return

        observations = []
        for call in calls:
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"]["arguments"])
                result = tools.TOOLS[name](**arguments)
            except Exception as error:
                result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
            observation = {"tool_call_id": call["id"], "name": name, "result": result}
            observations.append(observation)
            ctx["input"].append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })
            if result.get("ok"):
                print(colored(f"[Tool] {name} -> {result.get('path', '')}", "yellow"))
            else:
                print(colored(f"[Observation] {name} failed: {result.get('error')}", "red"))
        round_log["execution"]["observations"] = observations
        write_json(log_path, trace)
        print(colored(usage_summary(response.get("usage")), "dim"))
    raise RuntimeError("tool-call loop exceeded 99 rounds")


def read_api_key():
    path = Path(__file__).with_name(".deepseek_api_key")
    if not path.exists():
        raise SystemExit(f"Missing {path}. Copy .deepseek_api_key.example and put your key there.")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"{path} is empty.")
    return key


if __name__ == "__main__":
    print("Mini Agent Harness (DeepSeek). Type a task, or Ctrl-D to exit.")
    key = read_api_key()
    while True:
        try:
            user_input = input("> ").strip()
        except EOFError:
            print()
            break
        if user_input:
            try:
                run(key, user_input)
            except (KeyError, RuntimeError, json.JSONDecodeError) as error:
                print(colored(f"\nERROR: {error}", "red"))
