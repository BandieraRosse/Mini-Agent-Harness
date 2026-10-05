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
from prompt_toolkit.key_binding.bindings.mouse import load_mouse_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.containers import Float, FloatContainer
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.mouse_events import MouseButton, MouseEventType, MouseModifier
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
                    multiline=reading, accept_handler=accept,
                    read_only=~available)
    control = BufferControl(buffer=buffer, lexer=terminal._input_lexer(),
                            key_bindings=ConditionalKeyBindings(editing, available),
                            # Mask only the value; adding the label first would
                            # turn the configuration prompt into stars as well.
                            input_processors=[ConditionalProcessor(PasswordProcessor(), secret),
                                BeforeInput(lambda: terminal._safe(
                                    (terminal._request or {}).get('label', '') + ' › '))])
    composer = Window(control, height=Dimension(min=1, max=8), always_hide_cursor=~available,
                      dont_extend_height=True, wrap_lines=True, style='class:input')
    snapshot = []
    rows = [Point(0, 0)]
    row_starts = [0]
    cached = None
    viewport_width = 1
    selection = {'start': None, 'end': None, 'dragging': False, 'edge': 0, 'text': ''}
    drag_task = None

    def clear_selection():
        selection.update(start=None, end=None, dragging=False, edge=0)

    def selected_text():
        if selection['start'] is None or selection['end'] is None:
            return ''
        start, end = sorted((selection['start'], selection['end']))
        return selection['text'][start:end]

    def offset(point):
        lines = selection['text'].split('\n')
        y = min(max(0, point.y), len(lines) - 1)
        return sum(len(line) + 1 for line in lines[:y]) + min(max(0, point.x), len(lines[y]))

    def content():
        nonlocal snapshot, rows, row_starts, cached
        fragments = terminal.fragments()
        width = viewport_width
        if cached != (id(fragments), width):
            snapshot = fragments
            rows = []
            row_starts = []
            for y, line in enumerate(fragment_list_to_text(snapshot).split('\n')):
                row_starts.append(len(rows))
                rows.append(Point(0, y))
                used = 0
                for x, char in enumerate(line):
                    cells = get_cwidth(Char.display_mappings.get(char, char))
                    if used and used + cells > width:
                        rows.append(Point(x, y))
                        used = 0
                    used += cells
            cached = (id(fragments), width)
        text = fragment_list_to_text(snapshot)
        if not text.startswith(selection['text']):
            clear_selection()
        selection['text'] = text
        if not selected_text():
            return snapshot
        start, end = sorted((selection['start'], selection['end']))
        highlighted, index = [], 0
        for style, text in snapshot:
            left = max(0, min(len(text), start - index))
            right = max(left, min(len(text), end - index))
            highlighted.extend([(style, text[:left]),
                                (style + ' reverse', text[left:right]),
                                (style, text[right:])])
            index += len(text)
        return highlighted

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

        def mouse_handler(self, mouse_event):
            release = selection['dragging'] and mouse_event.event_type == MouseEventType.MOUSE_UP
            if mouse_event.button != MouseButton.LEFT and not release:
                return NotImplemented
            index = offset(mouse_event.position)
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                start = selection['start'] if MouseModifier.SHIFT in mouse_event.modifiers else None
                selection.update(start=index if start is None else start, end=index, dragging=True, edge=0)
            elif selection['dragging']:
                selection['end'] = index
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    selection.update(dragging=False, edge=0)
            return None

    class TranscriptWindow(Window):
        def _scroll_when_linewrapping(self, ui_content, width, height):
            # Position the viewport directly. Making a hidden cursor visible
            # otherwise consumes the first page inside the current viewport.
            bottom = max(0, len(rows) - height)
            index = bottom if terminal._scroll_line is None else min(bottom, terminal._scroll_line)
            point = rows[index]
            self.horizontal_scroll = 0
            self.vertical_scroll = point.y
            self.vertical_scroll_2 = index - row_starts[point.y]

    body = TranscriptWindow(TranscriptControl(content, get_cursor_position=anchor,
                                             show_cursor=False), wrap_lines=True)

    def scroll_by(amount):
        info = body.render_info
        height = info.window_height if info else 13
        bottom = max(0, len(rows) - height)
        position = bottom if terminal._scroll_line is None else min(bottom, terminal._scroll_line)
        position = max(0, min(bottom, position + amount))
        terminal._scroll_line = None if position == bottom else position

    def extend_edge(direction):
        scroll_by(direction * 3)
        height = body.render_info.window_height if body.render_info else 13
        bottom = max(0, len(rows) - height)
        top = bottom if terminal._scroll_line is None else terminal._scroll_line
        point = rows[min(len(rows) - 1, top if direction < 0 else top + height)]
        selection['end'] = offset(point)

    async def drag_scroll():
        while selection['dragging'] and selection['edge']:
            extend_edge(selection['edge'])
            app.invalidate()
            await asyncio.sleep(0.1)

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
        page = max(1, body.render_info.window_height - 1) if body.render_info else 12
        scroll_by(-page if event.key_sequence[-1].key == 'pageup' else page)

    @bindings.add(Keys.ScrollUp, eager=True)
    @bindings.add(Keys.ScrollDown, eager=True)
    def wheel(event):
        # Some inputs report wheels without coordinates. Override the toolkit
        # fallback that feeds Up/Down into history and menu bindings.
        scroll_by(-3 if event.key_sequence[-1].key == Keys.ScrollUp else 3)

    mouse_bindings = load_mouse_bindings()

    @bindings.add(Keys.Vt100MouseEvent, eager=True)
    @bindings.add(Keys.WindowsMouseEvent, eager=True)
    def positioned_mouse(event):
        nonlocal drag_task
        key = event.key_sequence[-1].key
        direction = None
        if key == Keys.WindowsMouseEvent:
            event_type = event.data.split(';')[1]
            if event_type == MouseEventType.SCROLL_UP.value:
                direction = -3
            elif event_type == MouseEventType.SCROLL_DOWN.value:
                direction = 3
        else:
            # Wheel buttons 64/65, with Shift/Alt/Ctrl bits removed. Support
            # X10, urxvt and SGR packets; their coordinate encodings differ.
            data = event.data[2:]
            if data.startswith('M'):
                button = ord(data[1]) - 32
            else:
                button = int(data.lstrip('<').split(';')[0])
                if not data.startswith('<'):
                    button -= 32
            button &= ~28
            if button in {64, 65} and (data.startswith('M') or data.endswith('M')):
                direction = -3 if button == 64 else 3
        if direction is not None:
            # Route wheels before hit testing, including input borders and
            # completion menus. Leave clicks/selection to the toolkit.
            scroll_by(direction)
            return None
        # Continue a drag outside the transcript, including beyond the top or
        # bottom of its viewport. Keep scrolling until the mouse returns or
        # the button is released, so selection can span arbitrary pages.
        if selection['dragging']:
            data = event.data[2:]
            if key == Keys.WindowsMouseEvent:
                pieces = event.data.split(';')
                x, y = int(pieces[2]), int(pieces[3])
                released = pieces[1] == MouseEventType.MOUSE_UP.value
                output = event.app.output
                if hasattr(output, 'get_win32_screen_buffer_info'):
                    screen_info = output.get_win32_screen_buffer_info()
                    y -= screen_info.dwCursorPosition.Y - event.app.renderer._cursor_pos.y
            elif data.startswith('M'):
                button, x, y = (ord(c) - 32 for c in data[1:])
                released = button & 3 == 3
                y -= 1 + event.app.renderer.rows_above_layout
            else:
                button, x, y = map(int, data.lstrip('<')[:-1].split(';'))
                released = data.endswith('m') or (not data.startswith('<') and button & 3 == 3)
                y -= 1 + event.app.renderer.rows_above_layout
            position = event.app.renderer._last_screen.visible_windows_to_write_positions.get(body)
            if position:
                edge = -1 if y < position.ypos else 1 if y >= position.ypos + position.height else 0
                selection['edge'] = edge
                if edge:
                    extend_edge(edge)
                    if released:
                        selection.update(dragging=False, edge=0)
                    elif drag_task is None or drag_task.done():
                        drag_task = event.app.create_background_task(drag_scroll())
                    return None
        return mouse_bindings.get_bindings_for_keys((key,))[0].handler(event)

    @bindings.add('c-home', eager=True)
    def first(event):
        terminal._scroll_line = 0

    @bindings.add('c-end', eager=True)
    def last(event):
        terminal._scroll_line = None

    @bindings.add('c-t', eager=True)
    def details(event):
        clear_selection()
        terminal.toggle_details()

    @bindings.add('escape', 'enter', filter=reading, eager=True)
    @bindings.add('c-j', filter=reading, eager=True)
    def newline(event):
        buffer.insert_text('\n')

    @bindings.add('c-j', filter=available & ~reading, eager=True)
    def submit_configuration(event):
        buffer.validate_and_handle()

    @bindings.add('c-c', eager=True)
    @bindings.add('escape')
    def cancel(event):
        if selected_text() or selection['dragging']:
            text = selected_text()
            if text and event.key_sequence[-1].key == 'c-c':
                from .clipboard import copy_text
                event.app.clipboard.set_text(text)
                copy_text(text, event.app.output)
            clear_selection()
            return
        clear_selection()
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
        if selected_text():
            return f' 已选中 {len(selected_text())} 字符 · Ctrl+C 复制并取消选择 · Esc 取消选择 · PgUp/PgDn 跨页'
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
                      full_screen=True, mouse_support=True, style=Style.from_dict(styles),
                      min_redraw_interval=0.03,
                      input=terminal.editor.app.input, output=terminal.editor.app.output)
    terminal._input_buffer = buffer
    app.transcript_window = body
    app.composer_window = composer
    app.transcript_selection = selection
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
