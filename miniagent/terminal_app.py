"""One persistent renderer; transcript scrolling never owns keyboard focus."""
import asyncio
import threading

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import ConditionalCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, ConditionalKeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.containers import Float, FloatContainer
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame

from .input import register_completion_bindings, register_editing_bindings


class UnifiedApplication(Application):
    async def _poll_output_size(self):
        # The default poller samples its baseline after the first sleep. A
        # resize before that sample can be missed indefinitely in an idle UI.
        # Compare with the last frame instead; signal handlers are unavailable
        # in our terminal thread.
        interval = self.terminal_size_polling_interval
        if interval is None:
            return
        while True:
            await asyncio.sleep(interval)
            rendered_size = self.renderer._last_size
            if rendered_size is not None and self.output.get_size() != rendered_size:
                self._on_resize()


def build_app(terminal, styles):
    bindings = KeyBindings()
    editing = KeyBindings()
    register_editing_bindings(editing)
    available = Condition(lambda: terminal._request is not None and
                          terminal._request['kind'] in {'read', 'ask'})
    secret = Condition(lambda: bool(terminal._request and terminal._request.get('secret')))
    reading = Condition(lambda: bool(terminal._request and terminal._request['kind'] == 'read'))
    completer = ConditionalCompleter(terminal.completer, reading)
    register_completion_bindings(editing, completer)

    def accept(buffer):
        if available():
            request = terminal._request
            if request['kind'] == 'read':
                buffer.append_to_history()
            finish(buffer.text.strip())
        return True

    # The accept handler records only user tasks, then clears the buffer before
    # Buffer's default accept path tries to append history again.
    history = terminal.editor.history if reading() else InMemoryHistory()
    buffer = Buffer(completer=completer, history=history,
                    complete_while_typing=reading,
                    multiline=True, accept_handler=accept,
                    read_only=~available)
    control = BufferControl(buffer=buffer, lexer=terminal._input_lexer(),
                            key_bindings=ConditionalKeyBindings(editing, available),
                            input_processors=[BeforeInput(lambda: terminal._safe(
                                (terminal._request or {}).get('label', '') + ' › ')),
                                ConditionalProcessor(PasswordProcessor(), secret)])
    composer = Window(control, height=Dimension(min=1, max=8), always_hide_cursor=~available,
                      dont_extend_height=True, wrap_lines=True, style='class:input')
    snapshot = []
    rows = [Point(0, 0)]
    cached = None
    viewport_width = 1

    def content():
        nonlocal snapshot, rows, cached
        fragments = terminal.fragments()
        width = viewport_width
        if cached != (id(fragments), width):
            snapshot = fragments
            rows = []
            for y, line in enumerate(fragment_list_to_text(snapshot).split('\n')):
                rows.append(Point(0, y))
                used = 0
                for x, char in enumerate(line):
                    cells = get_cwidth(Char.display_mappings.get(char, char))
                    if used and used + cells > width:
                        rows.append(Point(x, y))
                        used = 0
                    used += cells
            cached = (id(fragments), width)
        return snapshot

    def anchor():
        index = len(rows) - 1 if terminal._scroll_line is None else terminal._scroll_line
        return rows[min(len(rows) - 1, max(0, index))]

    # This virtual cursor is only a viewport anchor. The control cannot receive
    # focus; the real terminal cursor is always rendered by the composer.
    class TranscriptControl(FormattedTextControl):
        def create_content(self, width, height):
            nonlocal viewport_width
            viewport_width = max(1, width)
            return super().create_content(width, height)

    body = Window(TranscriptControl(content, get_cursor_position=anchor,
                                       show_cursor=False), wrap_lines=True)

    def finish(value=None, error=None):
        with terminal._lock:
            request = terminal._request
            if request is None:
                return
            terminal._request = None
            request['value'], request['error'] = value, error
            # Clear before waking the caller, including Buffer's working lines
            # and completions, so secret input cannot enter history afterwards.
            buffer.reset()
            terminal._changed()
            request['event'].set()

    @bindings.add('pageup', eager=True)
    @bindings.add('pagedown', eager=True)
    def scroll(event):
        position = len(rows) - 1 if terminal._scroll_line is None else terminal._scroll_line
        page = max(1, body.render_info.window_height - 1) if body.render_info else 12
        position += -page if event.key_sequence[-1].key == 'pageup' else page
        terminal._scroll_line = None if position >= len(rows) - 1 else max(0, position)
        event.app.invalidate()

    @bindings.add('c-home', eager=True)
    def first(event):
        terminal._scroll_line = 0

    @bindings.add('c-end', eager=True)
    def last(event):
        terminal._scroll_line = None

    @bindings.add('c-t', eager=True)
    def details(event):
        terminal.toggle_details()

    @bindings.add('escape', 'enter', filter=available, eager=True)
    @bindings.add('c-j', filter=available, eager=True)
    def newline(event):
        buffer.insert_text('\n')

    @bindings.add('c-c', eager=True)
    @bindings.add('escape')
    def cancel(event):
        if terminal._busy and terminal._cancel:
            terminal._cancel()
        elif terminal._request and terminal._request['kind'] == 'read' and terminal.detailed and event.key_sequence[-1].key == 'escape':
            terminal.toggle_details()
        elif terminal._request:
            finish(error=KeyboardInterrupt() if terminal._request['kind'] in {'read', 'ask'} else None)

    @bindings.add('c-d', eager=True)
    def eof(event):
        if available() and not buffer.text:
            finish(error=EOFError())

    choosing = Condition(lambda: bool(terminal._request and terminal._request['kind'] == 'choose'))

    @bindings.add('up', filter=choosing, eager=True)
    @bindings.add('down', filter=choosing, eager=True)
    def select(event):
        request = terminal._request
        step = -1 if event.key_sequence[-1].key == 'up' else 1
        request['index'] = (request['index'] + step) % len(request['options'])

    @bindings.add('enter', filter=choosing, eager=True)
    def choose(event):
        request = terminal._request
        finish(request['options'][request['index']][0])

    approving = Condition(lambda: terminal._approval is not None)

    @bindings.add('y', filter=approving, eager=True)
    @bindings.add('Y', filter=approving, eager=True)
    @bindings.add('n', filter=approving, eager=True)
    @bindings.add('N', filter=approving, eager=True)
    @bindings.add('a', filter=approving, eager=True)
    @bindings.add('A', filter=approving, eager=True)
    def approve(event):
        with terminal._lock:
            pending = terminal._approval
            answer = event.data.lower()
            if answer == 'a' and pending.get('rule') is None:
                return
            pending['allowed'] = answer in {'y', 'a'} and not terminal._stopping
            if pending['allowed'] and answer == 'a':
                terminal.approval_rules.add(pending['rule'])
            terminal._approval = None
            terminal._changed()
            pending['event'].set()

    def menu():
        request = terminal._request
        if not request or request['kind'] != 'choose':
            return ''
        return terminal._safe(request['label'] + '\n' + '\n'.join(
            ('› ' if index == request['index'] else '  ') + label
            for index, (_, label) in enumerate(request['options'])))

    def footer():
        state = '正在停止…' if terminal._stopping else '执行中' if terminal._busy else '输入'
        if choosing():
            return ' ↑↓ 选择 · Enter 确认 · Esc 取消 · PgUp/PgDn 翻页'
        return f' {state} · Ctrl+T 摘要/详情 · PgUp/PgDn 翻页 · Ctrl+End 跟随 · Ctrl+C 取消'

    root = FloatContainer(HSplit([
        Window(FormattedTextControl(lambda: f' MiniAgent · {terminal.model}'), height=1),
        body,
        Window(FormattedTextControl(menu), dont_extend_height=True),
        Frame(composer),
        Window(FormattedTextControl(terminal.statistics), dont_extend_height=True,
               wrap_lines=True, style='class:stats'),
        Window(FormattedTextControl(footer), height=1, style='class:toolbar'),
    ]), floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=9))])
    app = UnifiedApplication(layout=Layout(root, focused_element=control), key_bindings=bindings,
                      full_screen=True, style=Style.from_dict(styles),
                      min_redraw_interval=0.03,
                      input=terminal.editor.app.input, output=terminal.editor.app.output)
    terminal._input_buffer = buffer
    app.transcript_window = body
    app.composer_window = composer
    return app


def ensure_app(terminal, styles):
    if terminal._ui_thread is not None:
        if terminal._ui_error:
            raise terminal._ui_error
        if terminal._closed:
            raise EOFError()
        return
    terminal._app = terminal._make_app()
    terminal.editor.app = terminal._app
    terminal.editor.default_buffer = terminal._input_buffer

    def run():
        try:
            terminal._app.run(pre_run=terminal._ui_ready.set, handle_sigint=False,
                              set_exception_handler=False)
        except BaseException as error:
            terminal._ui_error = error
        finally:
            terminal._closed = True
            terminal._ui_ready.set()
            with terminal._lock:
                if terminal._request:
                    terminal._request['error'] = terminal._ui_error or EOFError()
                    terminal._request['event'].set()
                if terminal._cancel:
                    terminal._cancel()

    terminal._ui_thread = threading.Thread(target=run, name='miniagent-terminal', daemon=True)
    terminal._ui_thread.start()
    terminal._ui_ready.wait()
    if terminal._ui_error:
        raise terminal._ui_error


def request_input(terminal, styles, kind, label='', **kwargs):
    request = dict(kind=kind, label=label, event=threading.Event(), error=None, value=None, **kwargs)
    def prepare():
        with terminal._lock:
            terminal._input_buffer.history = terminal.editor.history if kind == 'read' else InMemoryHistory()
            terminal._input_buffer.reset()
            terminal._request = request
            terminal._changed()
    if terminal._ui_thread is None:
        terminal._request = request
        ensure_app(terminal, styles)
    else:
        ensure_app(terminal, styles)
        terminal._app.loop.call_soon_threadsafe(prepare)
    while not request['event'].wait(0.1):
        if terminal._closed:
            raise terminal._ui_error or EOFError()
    if request['error'] is not None:
        raise request['error']
    return request['value']
