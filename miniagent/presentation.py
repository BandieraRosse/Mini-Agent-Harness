"""Pure tool transcript rendering; callers redact and sanitize every value first.

Compact output follows Codex's activity cells: an action, status, and a short
output preview. Expanded output retains every argument and result we received.
The tuples work with prompt_toolkit, but this module needs only the stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass
class ToolRecord:
    name: str
    args: dict
    result: dict | None = None
    review: str | None = None


def _short(value, limit=160):
    text = " ".join(str(value).splitlines())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _diff_style(line):
    if line.startswith(("--- ", "+++ ", "@@", "diff --git ")):
        return "class:diff.header"
    if line.startswith("+"):
        return "class:diff.add"
    if line.startswith("-"):
        return "class:diff.remove"
    return "class:tool.muted"


def _diff_counts(diff):
    lines = diff.splitlines()
    added = sum(line.startswith("+") and not line.startswith("+++ ") for line in lines)
    removed = sum(line.startswith("-") and not line.startswith("--- ") for line in lines)
    return added, removed


def _edit_paths(record):
    result = record.result or {}
    paths = [item.get("path", "?") if isinstance(item, dict) else str(item)
             for item in result.get("files", [])]
    if not paths:
        paths = [line[6:].split("\t", 1)[0] for line in str(record.args.get("patch", "")).splitlines()
                 if line.startswith("+++ b/")]
    return ", ".join(paths) or str(result.get("path", record.args.get("path", "files")))


def _heading(record):
    args, result = record.args, record.result or {}
    path = result.get("path", args.get("path", "."))
    name = record.name
    if name == "read_file":
        title = f"Read {_short(path)}"
        if record.result is not None and result.get("ok"):
            lines = str(result.get("content", "")).splitlines()
            start = result.get("offset", args.get("offset", 1))
            title += f" · lines {start}–{start + len(lines) - 1}" if lines else " · empty"
            if "total_lines" in result:
                title += f" / {result['total_lines']}"
        return title
    if name in {"search_text", "find_files", "list_directory"}:
        field, label, noun = {
            "search_text": ("matches", "Search", "matches"),
            "find_files": ("files", "Find", "files"),
            "list_directory": ("entries", "List", "entries"),
        }[name]
        query = args.get("query", args.get("pattern", "*"))
        title = f"{label} {_short(path)}" if name == "list_directory" else f"{label} {_short(query)} in {_short(path)}"
        if record.result is not None and result.get("ok"):
            title += f" · {len(result.get(field, []))} {noun} returned"
        return title
    if name in {"create_file", "replace_text", "apply_patch"}:
        title = f"{'Create' if name == 'create_file' else 'Edit'} {_short(_edit_paths(record))}"
        diff = record.review or result.get("diff") or args.get("patch", "")
        if diff:
            added, removed = _diff_counts(diff)
            title += f" · +{added} −{removed}"
        if result.get("changed") is False:
            title += " · no changes"
        return title
    if name == "run_command":
        return f"Run {_short(args.get('command', ''))}"
    if name in {"poll_command", "cancel_command"}:
        return f"{'Poll' if name == 'poll_command' else 'Cancel'} {args.get('job_id', result.get('job_id', '?'))}"
    return _short(name)


def _limitations(result):
    if result.get("output_limit_reached"):
        yield "输出达到捕获上限；部分输出未保留。"
    elif result.get("has_more"):
        yield "工具输出已分页；仍有后续输出，可继续 poll_command。"
    elif result.get("truncated"):
        yield "工具返回内容已截断；详细模式仅包含本次返回内容。"
    if result.get("scan_truncated"):
        yield "搜索范围达到扫描上限。"
    if result.get("diff_truncated"):
        yield "工具返回的 diff 已截断。"
    if result.get("skipped_files"):
        yield f"跳过 {result['skipped_files']} 个无法读取的文件。"
    if result.get("partial_write"):
        yield "部分文件已写入：" + ", ".join(result.get("applied_files", []))


def _block(title, text, *, diff=False, style="class:tool.muted"):
    fragments = [("class:tool.muted", f"  {title}\n")]
    for line in str(text).splitlines() or [""]:
        fragments.append((_diff_style(line) if diff else style, "    " + line + "\n"))
    return fragments


def _details(label, values):
    fragments = [("class:tool.muted", f"  {label}\n")]
    for key, value in values.items():
        if isinstance(value, str) and ("\n" in value or key in {"output", "content", "diff", "patch", "command", "old_text", "new_text"}):
            fragments.extend(_block(str(key) + ":", value, diff=key in {"diff", "patch"}))
        else:
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
            fragments.extend(_block(str(key) + ":", rendered))
    if not values:
        fragments.append(("class:tool.muted", "    {}\n"))
    return fragments


def tool_fragments(record: ToolRecord, detailed=False) -> list[tuple[str, str]]:
    """Render one stored tool call; never redact, mutate, or discard its data."""
    result = record.result or {}
    failed = record.result is not None and (result.get("ok") is False or result.get("exit_code") not in (None, 0))
    running = not failed and (record.result is None or result.get("status") == "running")
    state = "error" if failed else "running" if running else "success"
    fragments = [(f"class:tool.{state}", "• " + _heading(record))]
    status = "失败" if failed else "执行中" if running else "完成"
    if result.get("status") in {"timed_out", "cancelled"}:
        status = {"timed_out": "超时", "cancelled": "已取消"}[result["status"]]
    fragments.append((f"class:tool.{state}", f" · {status}"))
    if result.get("exit_code") is not None:
        fragments.append((f"class:tool.{state}", f" · exit {result['exit_code']}"))
    if result.get("elapsed") is not None:
        fragments.append(("class:tool.muted", f" · {result['elapsed']}s"))
    fragments.append(("", "\n"))
    if detailed:
        fragments.extend(_details("Arguments", record.args))
        if record.result is not None:
            fragments.extend(_details("Result", record.result))
        if record.review:
            fragments.extend(_block("Review", record.review, diff=True))
    else:
        if record.name in {"run_command", "poll_command", "cancel_command"}:
            cwd = result.get("cwd", record.args.get("cwd", "."))
            fragments.append(("class:tool.muted", f"  cwd: {_short(cwd)}\n"))
        if result.get("error"):
            fragments.extend(_block("错误", result["error"], style="class:tool.error"))
        if result.get("output"):
            lines = str(result["output"]).splitlines()
            visible = lines[-(4 if failed else 3):]
            omitted = len(lines) - len(visible)
            if omitted:
                fragments.append(("class:tool.muted", f"  … {omitted} 行已折叠（Ctrl+T 查看）\n"))
            style = "class:tool.error" if failed else "class:tool.muted"
            for line in visible:
                fragments.append((style, "  └ " + _short(line, 240) + "\n"))
            if any(len(line) > 240 for line in visible):
                fragments.append(("class:tool.muted", "  … 长行已折叠（Ctrl+T 查看）\n"))
        if result.get("guidance"):
            fragments.extend(_block("提示", result["guidance"]))
    for notice in _limitations(result):
        fragments.append(("class:tool.muted", "  " + notice + "\n"))
    return fragments
