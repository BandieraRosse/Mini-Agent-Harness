"""Small terminal UI: summary transcript, live detail toggle, and Unicode input."""

from dataclasses import dataclass
import json
import os
import re
import sys
import threading

from .input import SlashCompleter, help_text, register_completion_bindings, register_editing_bindings
from .presentation import ToolRecord, tool_fragments
from .security import StreamRedactor


HELP = help_text()
_VIEW = object()
STYLES = {
    'tool.running': 'ansiyellow', 'tool.success': 'ansigreen bold',
    'tool.error': 'ansired bold', 'tool.muted': 'ansibrightblack',
    'tool.command': 'bold', 'diff.add': 'ansigreen', 'diff.remove': 'ansired',
    'diff.header': 'ansicyan', 'user': 'ansicyan bold', 'notice': 'ansibrightblack',
    'error': 'ansired', 'heading': 'bold', 'toolbar': 'reverse',
    'completion-menu.completion.current': 'reverse',
    'completion-menu.meta.completion': 'ansibrightblack',
}
ANSI = {'tool.running': '33', 'tool.success': '32', 'tool.error': '31',
        'tool.muted': '2', 'tool.command': '1', 'diff.add': '32',
        'diff.remove': '31', 'diff.header': '36', 'user': '36',
        'notice': '2', 'error': '31', 'heading': '1'}


def terminal_text(value):
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(value))
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) <= 159)


@dataclass
class TextRecord:
    text: str
    style: str = ''
    detail_only: bool = False


class Terminal:
    def __init__(self, redact, *, approval='ask', plain=False):
        self.redact, self.approval = redact, approval
        self.approval_rules = set()
        self.tty = sys.stdin.isatty()
        self.color = sys.stdout.isatty() and not os.environ.get('NO_COLOR') and not plain
        self.editor = None
        self.detailed = False
        self.events = []
        self.current_tool = None
        self.stream_record = None
        self.stream_redactor = None
        self.streaming = False
        self.model = ''
        self.tokens = {'prompt': 0, 'completion': 0}
        self._app = None
        self._lock = threading.RLock()
        self._cancel_event = None
        self._approval = None
        self._stopping = False
        self._scroll_line = None
        self._fragments_cache = None
        self.completer = None
        if self.tty and not plain:
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.key_binding import KeyBindings
                from prompt_toolkit.history import InMemoryHistory
                from prompt_toolkit.styles import Style
                bindings = KeyBindings()
                self.completer = SlashCompleter()
                register_editing_bindings(bindings)
                register_completion_bindings(bindings, self.completer)

                @bindings.add('escape', 'enter')
                @bindings.add('c-j')
                def newline(event):
                    event.current_buffer.insert_text('\n')

                @bindings.add('c-t')
                def details(event):
                    event.app.exit(result=_VIEW)

                self.editor = PromptSession(
                    multiline=True, key_bindings=bindings, history=InMemoryHistory(),
                    completer=self.completer, complete_while_typing=True,
                    reserve_space_for_menu=9, style=Style.from_dict(STYLES if self.color else {}),
                    bottom_toolbar=lambda: ' / 命令  ·  Ctrl+T 工具详情  ·  Alt+Enter 换行  ·  Ctrl+C 取消',
                )
            except ImportError:
                self.notice('增强交互需要 prompt-toolkit：python -m pip install .；当前使用普通输入。')

    def _safe(self, text):
        return terminal_text(self.redact(text))

    def _changed(self):
        self._fragments_cache = None
        if self._app is not None:
            self._app.invalidate()

    def _append(self, record):
        with self._lock:
            self.events.append(record)
            self._changed()

    def _write(self, fragments):
        for style, text in fragments:
            text = self._safe(text)
            color = ANSI.get(style.removeprefix('class:'))
            if color and self.color:
                text = f'\033[{color}m{text}\033[0m'
            print(text, end='', flush=True)

    def print(self, text='', color=None):
        style = {'2': 'notice', '31': 'error', '36': 'user', '33': 'tool.running'}.get(color, '')
        record = TextRecord(self._safe(text) + '\n', style)
        self._append(record)
        if self._app is None:
            self._write([(f'class:{style}', record.text)])

    def notice(self, text):
        self.print(f'· {text}', '2')

    def error(self, text):
        self.print(f'! {text}', '31')

    def user(self, text):
        self.print('\n› ' + text, '36')

    def round(self, number, model):
        self.model = model
        self._append(TextRecord(f'\n[{model} · 第 {number} 轮]\n', 'notice', True))

    def stream(self, fragment):
        self.check_cancelled()
        with self._lock:
            if not self.streaming:
                self.streaming = True
                self.stream_redactor = StreamRedactor(self.redact)
                self.stream_record = TextRecord('')
                self.events.append(self.stream_record)
            text = self._safe(self.stream_redactor.feed(fragment))
            self.stream_record.text += text
            self._changed()
        if self._app is None:
            self._write([('', text)])

    def end_stream(self):
        with self._lock:
            if not self.streaming:
                return
            text = self._safe(self.stream_redactor.feed('', final=True)) + '\n'
            self.stream_record.text += text
            self.streaming, self.stream_redactor, self.stream_record = False, None, None
            self._changed()
        if self._app is None:
            self._write([('', text)])

    def tool(self, name, arguments=None):
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {'raw_arguments': arguments}
        if not isinstance(arguments, dict):
            arguments = {'arguments': arguments} if arguments is not None else {}
        self.current_tool = ToolRecord(self._safe(name), self.redact.value(arguments))
        self._append(self.current_tool)
        if self._app is None:
            self._write(tool_fragments(self.current_tool, detailed=self.detailed))

    def result(self, result):
        with self._lock:
            if self.current_tool is None:
                self.current_tool = ToolRecord('tool', {})
                self.events.append(self.current_tool)
            self.current_tool.result = self.redact.value(result)
            record = self.current_tool
            self.current_tool = None
            self._changed()
        if self._app is None:
            self._write(tool_fragments(record, detailed=self.detailed))

    def usage(self, usage):
        self.tokens['prompt'] += usage.get('prompt_tokens', 0)
        self.tokens['completion'] += usage.get('completion_tokens', 0)
        self._append(TextRecord(f"  tokens: {usage.get('prompt_tokens', 0)} in | {usage.get('completion_tokens', 0)} out\n", 'notice', True))

    def check_cancelled(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise KeyboardInterrupt()

    def approve(self, operation, details):
        self.check_cancelled()
        original = details
        details = self._safe(details)
        # Never remember a sanitized/redacted command: distinct commands might
        # otherwise collapse to the same display text.
        rememberable = operation == 'shell' and original == details and '[REDACTED]' not in details
        rule = (operation, details)
        with self._lock:
            if self.current_tool is not None:
                self.current_tool.review = details
                self._changed()
        if self.approval == 'read-only':
            return False
        if self.approval == 'trust' or rememberable and rule in self.approval_rules:
            if self._app is None and (self.detailed or self.current_tool is None):
                self._write([('class:tool.muted', details + '\n')])
            return True
        if not self.tty:
            self.notice('非交互模式未授权修改/命令；自动执行需显式使用 --trust。')
            return False
        if self._app is None:
            self.print(details, '33')
            answer = input('允许执行？[y/N' + ('/a 本会话记住此命令' if rememberable else '') + '] ').strip().lower()
            if answer == 'a' and rememberable:
                self.approval_rules.add(rule)
                return True
            return answer in {'y', 'yes'}
        request = {'details': details, 'event': threading.Event(), 'allowed': False,
                   'rule': rule if rememberable else None}
        with self._lock:
            self.check_cancelled()
            self._approval = request
            self._scroll_line = None
            self._changed()
        while not request['event'].wait(0.1):
            self.check_cancelled()
        self.check_cancelled()
        return request['allowed']

    def fragments(self, start=0):
        with self._lock:
            if start == 0 and self._fragments_cache is not None:
                return self._fragments_cache
            result = []
            for event in self.events[start:]:
                if isinstance(event, ToolRecord):
                    result.extend(tool_fragments(event, self.detailed))
                elif self.detailed or not event.detail_only:
                    result.append((f'class:{event.style}', event.text))
            if self._approval is not None:
                result.append(('class:tool.running', '\n需要确认的操作：\n'))
                for line in self._approval['details'].splitlines(keepends=True):
                    style = 'diff.add' if line.startswith('+') else 'diff.remove' if line.startswith('-') else 'tool.command'
                    result.append((f'class:{style}', line))
                result.append(('class:tool.running', '\n允许执行？ Y 批准 / N 拒绝\n'))
                if self._approval.get('rule') is not None:
                    result.append(('class:tool.running', 'A 本会话记住此完整命令及工作目录\n'))
            result = [(style, self._safe(text)) for style, text in result]
            if start == 0:
                self._fragments_cache = result
            return result

    def toggle_details(self):
        with self._lock:
            self.detailed = not self.detailed
            self._changed()

    def _make_app(self, cancel=None, *, viewer=False):
        from prompt_toolkit import Application
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.layout.margins import ScrollbarMargin
        from prompt_toolkit.formatted_text import fragment_list_to_text
        from prompt_toolkit.layout.screen import Char
        from prompt_toolkit.utils import get_cwidth

        wrapped = None
        rendered_fragments = []

        def content():
            # Text and cursor must use the same snapshot while a worker streams.
            nonlocal rendered_fragments
            rendered_fragments = self.fragments()
            return rendered_fragments

        def visual_rows():
            # Keep character offsets as well as logical lines: a tool argument
            # can occupy many screen rows without containing a single newline.
            nonlocal wrapped
            fragments = rendered_fragments
            width = max(1, body.render_info.window_width if body.render_info else
                        self.editor.app.output.get_size().columns - 1)
            if wrapped is None or wrapped[0] is not fragments or wrapped[1] != width:
                positions = []
                for row, line in enumerate(fragment_list_to_text(fragments).split('\n')):
                    positions.append(Point(x=0, y=row))
                    used = 0
                    for column, char in enumerate(line):
                        cells = get_cwidth(Char.display_mappings.get(char, char))
                        if used and used + cells > width:
                            positions.append(Point(x=column, y=row))
                            used = 0
                        used += cells
                wrapped = fragments, width, positions
            return wrapped[2]

        def cursor():
            rows = visual_rows()
            if self._scroll_line is None:
                return rows[-1]
            return rows[min(len(rows) - 1, max(0, self._scroll_line))]

        def scroll(event, direction):
            rows = visual_rows()
            position = len(rows) - 1 if self._scroll_line is None else self._scroll_line
            page = max(1, body.render_info.window_height - 1) if body.render_info else 12
            distance = 1 if event.key_sequence[0].key in {'up', 'down'} else page
            self._scroll_line = min(len(rows) - 1, max(0, position + direction * distance))
            event.app.invalidate()

        body = Window(FormattedTextControl(content, focusable=True, get_cursor_position=cursor),
                      wrap_lines=True, right_margins=[ScrollbarMargin(display_arrows=True)])
        bindings = KeyBindings()

        @bindings.add('c-t')
        def details(event):
            self.toggle_details()

        @bindings.add('pageup')
        @bindings.add('up')
        def previous(event):
            scroll(event, -1)

        @bindings.add('pagedown')
        @bindings.add('down')
        def following(event):
            scroll(event, 1)

        @bindings.add('home')
        def first(event):
            self._scroll_line = 0
            event.app.invalidate()

        @bindings.add('end')
        def last(event):
            self._scroll_line = None
            event.app.invalidate()

        @bindings.add('c-c')
        @bindings.add('escape')
        def stop(event):
            if viewer:
                event.app.exit()
            elif cancel is not None:
                cancel()

        @bindings.add('y')
        @bindings.add('n')
        @bindings.add('Y')
        @bindings.add('N')
        @bindings.add('a')
        @bindings.add('A')
        def answer(event):
            with self._lock:
                pending = self._approval
                if pending is not None:
                    answer = event.data.lower()
                    if answer == 'a' and pending.get('rule') is None:
                        return
                    pending['allowed'] = answer in ('y', 'a') and not self._stopping
                    if pending['allowed'] and answer == 'a':
                        self.approval_rules.add(pending['rule'])
                    self._approval = None
                    self._changed()
                    pending['event'].set()

        def footer():
            mode = '详情' if self.detailed else '摘要'
            state = '正在停止…' if self._stopping else ('查看记录' if viewer else '执行中')
            return f' {state} · {mode} · Ctrl+T 切换 · PgUp/PgDn 滚动 · End 跟随 · ' + ('Esc 返回输入' if viewer else 'Ctrl+C 中断')

        return Application(layout=Layout(HSplit([
            Window(FormattedTextControl(lambda: f' MiniAgent · {self.model}'), height=1, style='class:heading'),
            body,
            Window(FormattedTextControl(footer), height=1, style='class:toolbar'),
        ]), focused_element=body), key_bindings=bindings, full_screen=True,
            style=Style.from_dict(STYLES if self.color else {}),
            input=self.editor.app.input, output=self.editor.app.output)

    def run_action(self, action, *, client=None, processes=None):
        if self.editor is None:
            return action()
        start = len(self.events)
        outcome = {}
        event = threading.Event()
        self._cancel_event, self._stopping, self._scroll_line = event, False, None
        if client is not None:
            client.cancel_event = event
        if processes is not None:
            processes.cancel_event = event

        def cancel():
            if event.is_set():
                return
            event.set()
            self._stopping = True
            if client is not None and hasattr(client, 'cancel'):
                client.cancel()
            with self._lock:
                if self._approval is not None:
                    self._approval['event'].set()
                    self._approval = None
                self._changed()

        app = self._make_app(cancel)
        self._app = app

        def work():
            try:
                outcome['result'] = action()
            except BaseException as error:
                outcome['error'] = error
            finally:
                try:
                    if event.is_set() and processes is not None:
                        processes.close()
                except BaseException as error:
                    outcome.setdefault('error', error)
                with self._lock:
                    if self.current_tool is not None:
                        self.current_tool.result = {'ok': False, 'error': '操作已中断；检查实际状态后再继续。'}
                        self.current_tool = None
                        self._changed()
                try:
                    loop = app.loop
                    if loop is not None:
                        loop.call_soon_threadsafe(
                            lambda: app.exit() if app.is_running and not app.is_done else None)
                except RuntimeError:
                    pass  # The terminal may already have closed after an I/O error.

        worker = threading.Thread(target=work, name='miniagent-turn', daemon=True)
        try:
            app.run(pre_run=worker.start)
            worker.join()
        finally:
            # An unexpected terminal failure must stop the worker before a new turn can start.
            if worker.is_alive():
                cancel()
                worker.join()
            self._app, self._cancel_event, self._approval = None, None, None
            self._stopping = False
            if client is not None:
                client.cancel_event = None
            if processes is not None:
                processes.cancel_event = None
            self._write(self.fragments(start))
        if 'error' in outcome:
            raise outcome['error']
        if event.is_set():
            raise KeyboardInterrupt()
        return outcome.get('result')

    def view_history(self):
        if not self.events:
            return
        app = self._make_app(viewer=True)
        self._app = app
        self._scroll_line = None
        try:
            app.run()
        finally:
            self._app = None

    def clear_history(self, *, clear_screen=False):
        self.events.clear()
        self.current_tool = None
        self._changed()
        if clear_screen and self.tty:
            if self.editor is not None:
                from prompt_toolkit.shortcuts import clear
                clear()
            else:
                print('\033[2J\033[H', end='', flush=True)

    def restore(self, messages):
        self.clear_history()
        records = {}
        for message in messages:
            role = message.get('role')
            if role == 'user':
                self.events.append(TextRecord('\n› ' + self._safe(message['content']) + '\n', 'user'))
            elif role == 'assistant':
                if message.get('completion_deferred'):
                    self.events.append(TextRecord('必需任务结果未齐，曾暂缓最终答复。\n', 'notice'))
                elif message.get('content'):
                    self.events.append(TextRecord(self._safe(message['content']) + '\n'))
                for call in message.get('tool_calls', []):
                    function = call['function']
                    try:
                        args = json.loads(function['arguments'])
                    except ValueError:
                        args = {'raw_arguments': function['arguments']}
                    if not isinstance(args, dict):
                        args = {'arguments': args}
                    record = ToolRecord(function['name'], self.redact.value(args))
                    records[call['id']] = record
                    self.events.append(record)
            elif role == 'tool' and message.get('tool_call_id') in records:
                try:
                    result = json.loads(message['content'])
                except ValueError:
                    result = {'output': message['content']}
                if not isinstance(result, dict):
                    result = {'output': result}
                records[message['tool_call_id']].result = self.redact.value(result)
        self._changed()

    def choose(self, title, options):
        """Small no-side-effect picker; values are explicit choices, Escape cancels."""
        if self.editor is None:
            self.print(title)
            for index, (value, label) in enumerate(options, 1):
                self.print(f'  {index}. {label}')
            if not self.tty:
                return None
            answer = input('编号（留空取消） › ').strip()
            if not answer:
                return None
            if answer.isdecimal() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            raise ValueError('请输入菜单中的编号')
        from prompt_toolkit import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import RadioList, Label, Frame
        radio = RadioList(values=[(value, self._safe(label)) for value, label in options], select_on_focus=True)
        bindings = KeyBindings()

        @bindings.add('enter', eager=True)
        def accept(event):
            event.app.exit(result=radio.current_value)

        @bindings.add('escape', eager=True)
        @bindings.add('c-c', eager=True)
        def cancel(event):
            event.app.exit(result=None)

        app = Application(layout=Layout(HSplit([
            Frame(radio, title=self._safe(title)), Label('↑↓ 选择 · Enter 确认 · Esc 取消'),
        ]), focused_element=radio), key_bindings=bindings, full_screen=False,
            style=Style.from_dict(STYLES if self.color else {}),
            input=self.editor.app.input, output=self.editor.app.output)
        return app.run()

    def ask(self, label):
        # Separate prompt: configuration values never enter the conversation history.
        if self.editor:
            from prompt_toolkit import prompt
            return prompt(label + ' › ', input=self.editor.app.input,
                          output=self.editor.app.output).strip()
        return input(label + ' › ').strip()

    def read(self):
        if self.editor:
            from prompt_toolkit.document import Document
            draft = Document('')
            while True:
                value = self.editor.prompt('› ', default=draft)
                if value is not _VIEW:
                    return value.strip()
                draft = self.editor.default_buffer.document
                self.toggle_details()
                self.view_history()
        line = input('miniagent > ' if self.tty else '')
        if line.strip() == '/paste':
            lines = []
            while True:
                line = input('... ' if self.tty else '')
                if line == '/end':
                    return '\n'.join(lines).strip()
                lines.append(line)
        lines = []
        while line.endswith('\\'):
            lines.append(line[:-1])
            line = input('... ' if self.tty else '')
        return '\n'.join([*lines, line]).strip()
