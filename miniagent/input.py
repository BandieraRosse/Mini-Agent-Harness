"""Small command menu and Unicode editing helpers for the optional terminal UI.

Text positions are Python character offsets, never terminal display columns. The
cluster rules cover mixed CJK/Latin input, combining marks and common emoji; this
is deliberately not a full implementation of Unicode text segmentation.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import unicodedata

try:
    from prompt_toolkit.completion import Completer, Completion, CompleteEvent
except ImportError:
    Completer = object
    Completion = CompleteEvent = None


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    arguments: str = ""
    choices: tuple = ()
    aliases: tuple = ()
    hidden: bool = False


COMMANDS = (
    Command("clear", "清空当前对话并开始新会话"),
    Command("resume", "选择并恢复已保存的会话", "[ID|latest]", ("latest",)),
    Command("status", "查看模型、权限和会话状态"),
    Command("model", "查看或切换模型", "[名称]"),
    Command("permissions", "查看或切换操作权限", "[ask|trust|read-only|rules|reset]", ("ask", "trust", "read-only", "rules", "reset"), ("approval",)),
    Command("new", "开始新会话，保留旧会话供恢复"),
    Command("compact", "压缩较早的对话上下文"),
    Command("help", "显示命令和快捷键"),
    Command("quit", "保存会话并退出", aliases=("exit",)),
    Command("sessions", "列出已保存的会话", hidden=True),
    Command("save", "保存当前会话", hidden=True),
    Command("paste", "普通输入模式下输入多行文本", hidden=True),
)


def help_text():
    lines = []
    for command in COMMANDS:
        if not command.hidden:
            usage = f"/{command.name} {command.arguments}".rstrip()
            lines.append(f"{usage:<27} {command.description}")
    lines.append("兼容命令：/exit、/approval、/sessions、/save、/paste。")
    lines.extend([
        "",
        "增强输入：输入 / 显示菜单；↑/↓ 选择，Tab 补全，Enter 执行。",
        "Ctrl+T 切换工具摘要/详情；执行中即时切换，输入时可查看历史工具调用。",
        "Enter 提交；Alt+Enter 或 Ctrl+J 换行；支持直接粘贴多行。",
        "←/→、Backspace、Delete 按字符操作；Ctrl+W 删除一个英文单词或一个汉字。",
        "Ctrl+C 中断当前工作或取消输入；Ctrl+D 在空输入时退出。",
        "普通输入：行末反斜杠续行，或 /paste 后单独输入 /end 提交。",
    ])
    return "\n".join(lines)


class SlashCompleter(Completer):
    """Prefix menu, with optional live argument choices keyed by command name."""

    def __init__(self, choices=None):
        if Completion is None:
            raise ImportError("Install mini-agent-harness[terminal] for enhanced input")
        self.choices = choices or {}

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # A command occupies the first and only input line. Do not complete paths
        # or command names in the middle of an ordinary prompt/pasted paragraph.
        if not text.startswith("/") or "\n" in document.text or document.text_after_cursor:
            return
        if not any(char.isspace() for char in text):
            prefix = text[1:]
            for command in COMMANDS:
                names = (command.name, *command.aliases)
                for name in names:
                    if name.startswith(prefix) and (prefix or not command.hidden and name == command.name):
                        yield Completion(
                            "/" + name, start_position=-len(text),
                            display="/" + name,
                            display_meta=f"{command.arguments} {command.description}".strip(),
                        )
            return
        name, _, tail = text.partition(" ")
        if " " in tail or not name or not tail and not text.endswith(" "):
            return
        command = next((item for item in COMMANDS if name[1:] in (item.name, *item.aliases)), None)
        if command is None:
            return
        choices = self.choices.get(command.name, command.choices)
        if callable(choices):
            choices = choices()
        labels = {"ask": "每次修改或运行命令前确认", "trust": "信任当前项目会话", "latest": "恢复最近的会话"}
        for value in choices:
            if value.startswith(tail):
                yield Completion(value, start_position=-len(tail), display_meta=labels.get(value, command.description))


def register_completion_bindings(bindings, completer):
    """Codex-style Tab completes; Enter accepts a menu choice and submits."""
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.filters import Condition

    def candidates(buffer):
        if buffer.complete_state:
            return buffer.complete_state.completions
        return list(completer.get_completions(buffer.document, CompleteEvent(completion_requested=True)))

    def selected(buffer):
        state = buffer.complete_state
        if state and state.current_completion:
            return state.current_completion
        choices = candidates(buffer)
        return choices[0] if choices else None

    @bindings.add("tab")
    def complete(event):
        buffer = event.current_buffer
        completion = selected(buffer)
        if completion:
            buffer.apply_completion(completion)
            buffer.insert_text(" ")

    @Condition
    def has_menu():
        from prompt_toolkit.application.current import get_app
        return bool(candidates(get_app().current_buffer))

    @bindings.add("down", filter=has_menu)
    @bindings.add("up", filter=has_menu)
    def select(event):
        buffer = event.current_buffer
        if not buffer.complete_state:
            buffer.complete_state = CompletionState(buffer.document, candidates(buffer))
        state = buffer.complete_state
        direction = -1 if event.key_sequence[-1].key == "up" else 1
        if state.complete_index is None:
            index = len(state.completions) - 1 if direction < 0 else 0
        else:
            index = (state.complete_index + direction) % len(state.completions)
        buffer.go_to_completion(index)

    @bindings.add("enter")
    def submit(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        # An untouched argument menu must not silently choose a model/permission.
        if (state and state.current_completion) or not buffer.text.endswith(" "):
            completion = selected(buffer)
            if completion:
                buffer.apply_completion(completion)
        buffer.validate_and_handle()


def grapheme_boundaries(text):
    """Return offsets around common user-perceived characters, including emoji."""
    boundaries = [0]
    regional_run = 0
    for index, char in enumerate(text):
        code = ord(char)
        regional = 0x1F1E6 <= code <= 0x1F1FF
        extend = (unicodedata.category(char).startswith("M")
                  or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F)
        previous = text[index - 1] if index else ""
        joined = (extend or char == "\u200d" or previous == "\u200d"
                  or regional and regional_run % 2 == 1 or previous == "\r" and char == "\n")
        if previous in {"\r", "\n"} and not (previous == "\r" and char == "\n"):
            joined = False
        if char in {"\r", "\n"} and not (previous == "\r" and char == "\n"):
            joined = False
        if index and not joined:
            boundaries.append(index)
        regional_run = regional_run + 1 if regional else 0
    if text:
        boundaries.append(len(text))
    return boundaries


def previous_word_start(text, cursor):
    """Ctrl+W removes a Latin word, one CJK character, or a punctuation run."""
    points = grapheme_boundaries(text)
    index = bisect_left(points, cursor) - 1
    while index >= 0 and text[points[index]:points[index + 1]].isspace():
        index -= 1
    if index < 0:
        return 0

    def kind(cluster):
        first = cluster[0]
        code = ord(first)
        if (0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
                or 0x20000 <= code <= 0x323AF or 0x3040 <= code <= 0x30FF):
            return "cjk"
        return "word" if first.isalnum() or first == "_" else "punctuation"

    category = kind(text[points[index]:points[index + 1]])
    if category != "cjk":
        while index > 0:
            previous = text[points[index - 1]:points[index]]
            if previous.isspace() or kind(previous) != category:
                break
            index -= 1
    return points[index]


def register_editing_bindings(bindings):
    """Use common character boundaries for movement and both delete directions."""
    from prompt_toolkit.document import Document

    def remove(buffer, start, end):
        buffer.document = Document(buffer.text[:start] + buffer.text[end:], start)

    @bindings.add("left")
    @bindings.add("c-b")
    def left(event):
        buffer = event.current_buffer
        points = grapheme_boundaries(buffer.text)
        buffer.cursor_position = points[max(0, bisect_left(points, buffer.cursor_position) - 1)]

    @bindings.add("right")
    @bindings.add("c-f")
    def right(event):
        buffer = event.current_buffer
        points = grapheme_boundaries(buffer.text)
        buffer.cursor_position = points[min(len(points) - 1, bisect_right(points, buffer.cursor_position))]

    @bindings.add("backspace")
    def backspace(event):
        buffer = event.current_buffer
        if buffer.cursor_position:
            points = grapheme_boundaries(buffer.text)
            index = bisect_left(points, buffer.cursor_position)
            remove(buffer, points[index - 1], points[index])

    @bindings.add("delete")
    def delete(event):
        buffer = event.current_buffer
        if buffer.cursor_position < len(buffer.text):
            points = grapheme_boundaries(buffer.text)
            index = bisect_right(points, buffer.cursor_position)
            remove(buffer, points[index - 1], points[index])

    @bindings.add("c-w")
    @bindings.add("escape", "backspace")
    def delete_word(event):
        buffer = event.current_buffer
        if buffer.cursor_position:
            points = grapheme_boundaries(buffer.text)
            end = points[bisect_left(points, buffer.cursor_position)]
            remove(buffer, previous_word_start(buffer.text, end), end)
