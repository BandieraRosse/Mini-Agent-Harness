"""Small terminal UI: summary transcript, live detail toggle, and Unicode input."""

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
import sys
import threading

from .input import SlashCompleter, help_text
from .presentation import ToolRecord, tool_fragments
from .security import StreamRedactor


HELP = help_text()
STYLES = {
    'tool.running': 'ansiyellow', 'tool.success': 'ansigreen bold',
    'tool.error': 'ansired bold', 'tool.muted': 'ansibrightblack',
    'tool.command': 'bold', 'diff.add': 'ansigreen', 'diff.remove': 'ansired',
    'diff.header': 'ansicyan', 'user': 'ansicyan bold', 'notice': 'ansibrightblack',
    'error': 'ansired', 'heading': 'bold', 'toolbar': 'reverse',
    'input': 'bg:ansibrightblack ansiwhite', 'input.prompt': 'ansicyan bold',
    'frame.border': 'ansicyan', 'stats': 'ansicyan',
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
    def __init__(self, redact, *, approval='trust', plain=False):
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
        self.tokens = {'prompt': 0, 'cached': 0, 'completion': 0, 'reasoning': 0}
        self.usage_missing = set()
        self.context_provider = None
        self._app = None
        self._lock = threading.RLock()
        self._cancel_event = None
        self._approval = None
        self._stopping = False
        self._scroll_line = None
        self._fragments_cache = None
        self.completer = None
        self._ui_thread = None
        self._ui_ready = threading.Event()
        self._ui_error = None
        self._request = None
        self._busy = False
        self._cancel = None
        self._closed = False
        if self.tty and not plain:
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.history import InMemoryHistory
                from prompt_toolkit.styles import Style
                self.completer = SlashCompleter()
                # Supplies terminal devices and in-memory history. Its prompt
                # application is never run; terminal_app owns all rendering.
                self.editor = PromptSession(
                    multiline=True, history=InMemoryHistory(),
                    style=Style.from_dict(STYLES if self.color else {}),
                    show_frame=True,
                )
                from prompt_toolkit.layout.controls import BufferControl
                for window in self.editor.app.layout.find_all_windows():
                    if isinstance(window.content, BufferControl) and window.content.buffer is self.editor.default_buffer:
                        window.style = 'class:input'
            except ImportError:
                self.notice('增强交互需要 prompt-toolkit：python -m pip install .；当前使用普通输入。')

    def _safe(self, text):
        return terminal_text(self.redact(text))

    @staticmethod
    def _input_lexer():
        from prompt_toolkit.lexers import SimpleLexer
        return SimpleLexer('class:input')

    def statistics(self):
        context = '上下文剩余: 未知'
        if self.context_provider is not None:
            used, window = self.context_provider()
            remaining = max(0, window - used)
            context = f'上下文剩余 ≈{remaining / window:.1%} · {remaining:,}/{window:,} tokens（估算）'
        values = []
        for key, label in [('prompt', 'input'), ('cached', '缓存 input'),
                           ('completion', 'output'), ('reasoning', 'reasoning')]:
            value = f'{self.tokens[key]:,}'
            if key in self.usage_missing:
                value += '+未知'
            values.append(f'{label} {value}')
        return context + '\n累计: ' + ' · '.join(values)

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
        record = TextRecord(self._safe(text).strip('\n') + '\n\n', style)
        self._append(record)
        if self.editor is None:
            self._write([(f'class:{style}', record.text)])

    def notice(self, text):
        self.print(f'· {text}', '2')

    def error(self, text):
        self.print(f'! {text}', '31')

    def user(self, text):
        self.print('› ' + text, '36')

    def round(self, number, model):
        self.model = model
        self._append(TextRecord(f'\n[{model} · 第 {number} 轮]\n', 'notice', True))

    def work_summary(self, elapsed):
        seconds = max(0, round(elapsed))
        minutes, seconds = divmod(seconds, 60)
        duration = f'{minutes}分{seconds:02d}秒' if minutes else f'{seconds}秒'
        self.notice(f'工作用时 {duration} · 结束于 {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}')

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
        if self.editor is None:
            self._write([('', text)])

    def end_stream(self):
        with self._lock:
            if not self.streaming:
                return
            text = self._safe(self.stream_redactor.feed('', final=True)) + '\n\n'
            self.stream_record.text += text
            self.streaming, self.stream_redactor, self.stream_record = False, None, None
            self._changed()
        if self.editor is None:
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
        if self.editor is None:
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
        if self.editor is None:
            self._write([*tool_fragments(record, detailed=self.detailed), ('', '\n')])

    def usage(self, usage):
        counts = {
            'prompt': usage.get('prompt_tokens', usage.get('input_tokens')),
            'completion': usage.get('completion_tokens', usage.get('output_tokens')),
            'cached': (usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}).get(
                'cached_tokens', usage.get('prompt_cache_hit_tokens')),
            'reasoning': (usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}).get('reasoning_tokens'),
        }
        with self._lock:
            for key, value in counts.items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.tokens[key] += value
                else:
                    counts[key] = None
                    self.usage_missing.add(key)
            labels = [('prompt', 'input'), ('cached', '缓存 input'), ('completion', 'output'), ('reasoning', 'reasoning')]
            text = '  本次调用: ' + ' · '.join(
                f'{label} {counts[key]:,}' if counts[key] is not None else f'{label} 未知'
                for key, label in labels) + '\n\n'
            self._append(TextRecord(text, 'notice'))
        if self.editor is None:
            self._write([('class:notice', text)])

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
            if self.editor is None and (self.detailed or self.current_tool is None):
                self._write([('class:tool.muted', details + '\n')])
            return True
        if not self.tty:
            self.notice('非交互模式未授权修改/命令；自动执行需显式使用 --trust。')
            return False
        if self.editor is None:
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
                    result.append(('', '\n'))
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

    def _make_app(self):
        from .terminal_app import build_app
        return build_app(self, STYLES if self.color else {})

    def run_action(self, action, *, client=None, processes=None):
        if self.editor is None:
            return action()
        from .terminal_app import ensure_app
        event = threading.Event()
        self._cancel_event, self._stopping = event, False
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

        self._busy, self._cancel = True, cancel
        try:
            ensure_app(self, STYLES if self.color else {})
            self._changed()
            result = action()
            self.check_cancelled()
            return result
        except KeyboardInterrupt:
            cancel()
            raise
        finally:
            try:
                if event.is_set() and processes is not None:
                    processes.close()
            finally:
                with self._lock:
                    if self.current_tool is not None:
                        self.current_tool.result = {'ok': False, 'error': '操作已中断；检查实际状态后再继续。'}
                        self.current_tool = None
                    self._cancel_event, self._approval, self._cancel = None, None, None
                    self._stopping, self._busy = False, False
                    self._changed()
                if client is not None:
                    client.cancel_event = None
                if processes is not None:
                    processes.cancel_event = None

    def view_history(self):
        if self.editor is not None:
            from .terminal_app import request_input
            request_input(self, STYLES if self.color else {}, 'view')

    def close(self):
        """Restore the terminal once, after all work has stopped."""
        if self._closed and self._ui_thread is None:
            return
        if self._app is not None and self._ui_thread is not None and self._ui_thread.is_alive():
            def exit_app():
                if self._app.is_running and not self._app.is_done:
                    self._app.exit()
            try:
                self._app.loop.call_soon_threadsafe(exit_app)
            except RuntimeError:
                pass
            self._ui_thread.join()
        self._app, self._ui_thread = None, None
        self._closed = True
        if self.editor is not None:
            self._write(self.fragments())


    def clear_history(self, *, clear_screen=False):
        with self._lock:
            self.events.clear()
            self.current_tool = None
            self._scroll_line = None
            self._changed()
        if clear_screen and self.tty and self.editor is None:
            print('\033[2J\033[H', end='', flush=True)

    def restore(self, messages):
        self.clear_history()
        records = {}
        for message in messages:
            role = message.get('role')
            if role == 'user':
                self.events.append(TextRecord('› ' + self._safe(message['content']) + '\n\n', 'user'))
            elif role == 'assistant':
                if message.get('completion_deferred'):
                    self.events.append(TextRecord('必需任务结果未齐，曾暂缓最终答复。\n', 'notice'))
                elif message.get('content'):
                    self.events.append(TextRecord(self._safe(message['content']) + '\n\n'))
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
        if not options:
            return None
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
        from .terminal_app import request_input
        return request_input(self, STYLES if self.color else {}, 'choose',
                             title, options=options, index=0)

    def ask(self, label, *, secret=False):
        # Configuration values never enter conversation or input history.
        if self.editor:
            from .terminal_app import request_input
            return request_input(self, STYLES if self.color else {}, 'ask', label, secret=secret)
        if secret:
            import getpass
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                try:
                    return getpass.getpass(label + ' › ').strip()
                except getpass.GetPassWarning:
                    raise ValueError('Hidden key input requires an interactive terminal') from None
        return input(label + ' › ').strip()

    def read(self):
        if self.editor:
            from .terminal_app import request_input
            return request_input(self, STYLES if self.color else {}, 'read')
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
