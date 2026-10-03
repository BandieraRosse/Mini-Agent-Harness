"""A scrollback-friendly terminal UI with optional prompt_toolkit input."""

import json
import os
import re
import sys

from .security import StreamRedactor


HELP = """/help                 显示帮助
/new 或 /clear        新建会话（不修改项目文件）
/sessions             列出当前项目的会话
/resume [ID|latest]    恢复会话；省略参数则列出会话
/save                 保存当前会话
/compact              压缩较早上下文
/approval ask|trust   切换逐次确认 / 信任本次项目会话
/status               查看模型、目录和会话状态
/paste                多行输入，单独输入 /end 提交
/exit                 保存并退出

Ctrl+C 中断生成/执行或取消当前输入。Ctrl+D 退出。
增强输入：Enter 提交，Alt+Enter 或 Ctrl+J 换行，支持历史与粘贴。
普通输入：行末反斜杠续行，或使用 /paste。
信任模式下 Shell 具有当前用户权限；本程序不提供沙箱。"""


def terminal_text(value):
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159)


class Terminal:
    def __init__(self, redact, *, approval="ask", plain=False):
        self.redact, self.approval = redact, approval
        self.tty = sys.stdin.isatty()
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and not plain
        self.pending, self.streaming = "", False
        self.stream_redactor = None
        self.editor = None
        if self.tty and not plain:
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.key_binding import KeyBindings
                from prompt_toolkit.history import InMemoryHistory
                bindings = KeyBindings()

                @bindings.add("enter")
                def submit(event):
                    event.current_buffer.validate_and_handle()

                @bindings.add("escape", "enter")
                @bindings.add("c-j")
                def newline(event):
                    event.current_buffer.insert_text("\n")

                self.editor = PromptSession(multiline=True, key_bindings=bindings,
                                            history=InMemoryHistory())
            except ImportError:
                pass

    def print(self, text="", color=None):
        text = terminal_text(self.redact(text))
        if color and self.color:
            text = f"\033[{color}m{text}\033[0m"
        print(text, flush=True)

    def notice(self, text):
        self.print(f"[状态] {text}", "2")

    def error(self, text):
        self.print(f"[错误] {text}", "31")

    def round(self, number, model):
        self.print(f"\n[模型 · {model} · 第 {number} 轮]", "36")

    def stream(self, fragment):
        self.streaming = True
        if self.stream_redactor is None:
            self.stream_redactor = StreamRedactor(self.redact)
        print(terminal_text(self.stream_redactor.feed(fragment)), end="", flush=True)

    def end_stream(self):
        if self.streaming:
            print(terminal_text(self.stream_redactor.feed("", final=True)), flush=True)
        self.pending, self.streaming = "", False
        self.stream_redactor = None

    def tool(self, name):
        self.print(f"[工具] {name}", "33")

    def result(self, result):
        if not result.get("ok"):
            self.error(result.get("error", f"工具执行失败，退出码 {result.get('exit_code', 'unknown')}"))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        self.print("[结果] " + text[:1800] + ("\n… 显示已截断；会话保留工具返回内容。" if len(text) > 1800 else ""), "2")

    def usage(self, usage):
        if usage:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            self.notice(f"tokens: {usage.get('prompt_tokens', 0)} in ({cached} cached) | {usage.get('completion_tokens', 0)} out")

    def approve(self, operation, details):
        self.print(f"\n[{'命令' if operation == 'shell' else '修改'}]\n{details}", "33")
        if self.approval == "trust":
            return True
        if not self.tty:
            self.notice("非交互模式未授权修改/命令。需要授权时请显式使用 --trust。")
            return False
        return input("允许执行？[y/N] ").strip().lower() in {"y", "yes"}

    def read(self):
        if self.editor:
            return self.editor.prompt("miniagent › ").strip()
        prompt = "miniagent > " if self.tty else ""
        line = input(prompt)
        if line.strip() == "/paste":
            if self.tty:
                self.notice("多行输入：单独输入 /end 提交，Ctrl+C 取消。")
            lines = []
            while True:
                line = input("... " if self.tty else "")
                if line == "/end":
                    return "\n".join(lines).strip()
                lines.append(line)
        lines = []
        while line.endswith("\\"):
            lines.append(line[:-1])
            line = input("... " if self.tty else "")
        return "\n".join([*lines, line]).strip()
