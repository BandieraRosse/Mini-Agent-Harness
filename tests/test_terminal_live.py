"""Exercise keyboard input against the actual prompt_toolkit applications."""
import contextlib
import importlib.util
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from miniagent.security import Redactor
from miniagent.ui import Terminal


@unittest.skipUnless(importlib.util.find_spec("prompt_toolkit"), "prompt-toolkit is not installed")
class LiveTerminalTests(unittest.TestCase):
    @contextlib.contextmanager
    def terminal(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe, contextlib.redirect_stdout(io.StringIO()) as output:
            def factory(**kwargs):
                return PromptSession(input=pipe, output=DummyOutput(), **kwargs)

            with patch("sys.stdin.isatty", return_value=True), patch("prompt_toolkit.PromptSession", side_effect=factory):
                terminal = Terminal(Redactor(), approval='ask')
            senders, failures, releases = [], [], []
            expired = threading.Event()

            def unblock():
                for event in releases:
                    event.set()
                pipe.close()

            def expire():
                expired.set()
                unblock()

            def send(function):
                def execute():
                    try:
                        function()
                    except BaseException as error:
                        failures.append(error)
                        unblock()
                thread = threading.Thread(target=execute, daemon=True)
                senders.append(thread)
                thread.start()

            timer = threading.Timer(6, expire)
            timer.daemon = True
            timer.start()
            try:
                yield SimpleNamespace(ui=terminal, pipe=pipe, send=send,
                                      releases=releases, output=output)
            finally:
                timer.cancel()
                unblock()
                terminal.close()
                for thread in senders:
                    thread.join(timeout=1)
                self.assertFalse(any(thread.is_alive() for thread in senders), "keyboard sender did not stop")
                self.assertFalse(expired.is_set(), "terminal interaction exceeded the watchdog timeout")
                if failures:
                    raise failures[0]

    def observe(self, terminal, callback):
        """Inspect the control that the running application actually rendered."""
        original = terminal._make_app

        def make_app(*args, **kwargs):
            app = original(*args, **kwargs)

            def after_render(sender):
                content = sender.transcript_window.render_info.ui_content
                text = "\n".join("".join(part[1] for part in content.get_line(index))
                                 for index in range(content.line_count))
                callback(sender, text)
            app.after_render += after_render
            return app

        terminal._make_app = make_app

    def wait(self, event):
        self.assertTrue(event.wait(3), "expected terminal state was not reached")

    def test_mouse_selection_autoscrolls_and_ctrl_c_copies_without_cancelling_input(self):
        with self.terminal() as env:
            ui = env.ui
            ui.user('用户消息')
            ui.stream('\n'.join(f'行 {i:03d} model output' for i in range(80)))
            ui.end_stream()
            ready, home, selected, copied = (threading.Event() for _ in range(4))
            captured = []

            def rendered(app, text):
                info = app.transcript_window.render_info
                ready.set()
                if info.visible_line_to_row_col[0][0] == 0:
                    home.set()
                state = app.transcript_selection
                if state['end'] is not None and state['end'] > 600:
                    selected.set()
                if captured and state['start'] is None:
                    copied.set()
            self.observe(ui, rendered)

            def keys():
                self.wait(ready)
                env.pipe.send_text('draft\x1b[1;5H')
                self.wait(home)
                app = ui._app
                position = app.renderer._last_screen.visible_windows_to_write_positions[app.transcript_window]
                x, y = position.xpos + 1, position.ypos + 1
                env.pipe.send_text(f'\x1b[<0;{x};{y}M')
                outside = position.ypos + position.height + 1
                env.pipe.send_text(f'\x1b[<32;{x};{outside}M')
                self.wait(selected)
                env.pipe.send_text(f'\x1b[<0;{x};{outside}m\x03')
                self.wait(copied)
                self.assertIn('用户消息', captured[0])
                self.assertIn('行 020', captured[0])
                self.assertEqual(app.clipboard.get_data().text, captured[0])
                env.pipe.send_text('\r')
            with patch('miniagent.clipboard.copy_text', side_effect=lambda text, output: captured.append(text)):
                env.send(keys)
                self.assertEqual(ui.read(), 'draft')

    def test_copy_during_execution_and_buttonless_release_preserve_worker(self):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        with self.terminal() as env:
            ui = env.ui
            ui.stream('中文 answer')
            ui.end_stream()
            ready, selected, copied, release = (threading.Event() for _ in range(4))
            env.releases.append(release)

            def rendered(app, text):
                ready.set()
                state = app.transcript_selection
                if state['start'] is not None and not state['dragging']:
                    selected.set()
                if app.clipboard.get_data().text:
                    copied.set()
            self.observe(ui, rendered)

            def keys():
                self.wait(ready)
                app = ui._app

                def select():
                    control = app.transcript_window.content
                    for kind, point, button in (
                        (MouseEventType.MOUSE_DOWN, Point(2, 0), MouseButton.LEFT),
                        (MouseEventType.MOUSE_MOVE, Point(0, 0), MouseButton.LEFT),
                        (MouseEventType.MOUSE_UP, Point(0, 0), MouseButton.NONE),
                    ):
                        control.mouse_handler(MouseEvent(point, kind, button, frozenset()))
                    app.invalidate()
                app.loop.call_soon_threadsafe(select)
                self.wait(selected)
                env.pipe.send_text('\x03')
                self.wait(copied)
                self.assertEqual(app.clipboard.get_data().text, '中文')
                self.assertFalse(ui._cancel_event.is_set())
                release.set()
            with patch('miniagent.clipboard.copy_text') as copy:
                env.send(keys)
                ui.run_action(lambda: self.wait(release))
                copy.assert_called_once()

    def test_ctrl_t_during_execution_expands_previous_arguments_and_result(self):
        with self.terminal() as env:
            ui = env.ui
            argument = "参数argument" * 240
            prior_output = "first-result-marker\nsecond\nthird\nfourth\nfifth"
            ui.tool("run_command", {"command": argument})
            ui.result({"ok": True, "exit_code": 0, "output": prior_output})
            summary, expanded, release = threading.Event(), threading.Event(), threading.Event()
            env.releases.append(release)
            frames = []

            def rendered(app, text):
                frames.append(text)
                if not ui.detailed and "List current" in text:
                    summary.set()
                if ui.detailed and argument in text and "first-result-marker" in text:
                    expanded.set()
            self.observe(ui, rendered)

            def action():
                ui.tool("list_directory", {"path": "current"})
                self.wait(release)
                ui.result({"ok": True, "entries": []})
                return "finished"

            def keys():
                self.wait(summary)
                env.pipe.send_text("\x14")
                self.wait(expanded)
                release.set()
            env.send(keys)
            self.assertEqual(ui.run_action(action), "finished")
            self.assertTrue(ui.detailed)
            self.assertTrue(any("first-result-marker" not in frame and "List current" in frame for frame in frames))
            self.assertIn(argument, "\n".join(frames))

    def test_ctrl_c_cancels_worker_and_returns_to_next_input(self):
        with self.terminal() as env:
            ready, cancelled = threading.Event(), threading.Event()
            self.observe(env.ui, lambda app, text: ready.set() if "Run waiting" in text else None)

            def action():
                env.ui.tool("run_command", {"command": "waiting"})
                self.wait(env.ui._cancel_event)
                try:
                    env.ui.check_cancelled()
                except KeyboardInterrupt:
                    cancelled.set()
                    raise

            def keys():
                self.wait(ready)
                env.pipe.send_text("\x03")
            env.send(keys)
            with self.assertRaises(KeyboardInterrupt):
                env.ui.run_action(action)
            self.assertTrue(cancelled.is_set())
            app = env.ui._app
            self.assertTrue(app.is_running)
            self.assertIsNone(env.ui.current_tool)
            env.pipe.send_text("继续 next\r")
            self.assertEqual(env.ui.read(), "继续 next")
            self.assertIs(env.ui._app, app)

    def test_approval_accept_and_decline_survive_live_detail_toggle(self):
        for answer, expected in [("y", True), ("n", False)]:
            with self.subTest(answer=answer), self.terminal() as env:
                ready, expanded = threading.Event(), threading.Event()
                diff = "--- a/中文.py\n+++ b/中文.py\n@@ -1 +1 @@\n-old\n+new\n"
                frames = []

                def rendered(app, text):
                    frames.append(text)
                    if env.ui._approval is not None and "允许执行" in text:
                        (expanded if env.ui.detailed else ready).set()
                self.observe(env.ui, rendered)

                def action():
                    env.ui.tool("replace_text", {"path": "中文.py", "old_text": "old", "new_text": "new"})
                    allowed = env.ui.approve("file", diff)
                    env.ui.result({"ok": allowed, "error": "declined"} if not allowed else {"ok": True})
                    return allowed

                def keys():
                    self.wait(ready)
                    env.pipe.send_text("\x14")
                    self.wait(expanded)
                    env.pipe.send_text(answer)
                env.send(keys)
                self.assertEqual(env.ui.run_action(action), expected)
                self.assertIsNone(env.ui._approval)
                self.assertTrue(any("+new" in frame and "允许执行" in frame for frame in frames))

    def test_idle_history_escape_preserves_mixed_draft_and_cursor(self):
        with self.terminal() as env:
            env.ui.tool("read_file", {"path": "history.py"})
            env.ui.result({"ok": True, "content": "1: past", "offset": 1})
            shown, restored = threading.Event(), threading.Event()
            draft, insertion = "修复API错误 and 文档", "测试"
            cursor = len(draft) - 3
            self.observe(env.ui, lambda app, text: shown.set() if "Arguments" in text else None)

            def prompt_rendered(app, text):
                buffer = env.ui.editor.default_buffer
                if shown.is_set() and not env.ui.detailed:
                    if buffer.text == draft and buffer.cursor_position == cursor:
                        restored.set()
            original = env.ui._make_app
            def make_app():
                app = original()
                app.after_render += lambda sender: prompt_rendered(sender, '')
                return app
            env.ui._make_app = make_app

            def keys():
                env.pipe.send_text(draft + "\x1b[D" * 3 + "\x14")
                self.wait(shown)
                env.pipe.send_text("\x1b")
                self.wait(restored)
                env.pipe.send_text(insertion + "\r")
            env.send(keys)
            self.assertEqual(env.ui.read(), draft[:cursor] + insertion + draft[cursor:])
            self.assertTrue(restored.is_set())

    def test_ctrl_c_releases_pending_approval_without_performing_operation(self):
        with self.terminal() as env:
            ready = threading.Event()
            performed = []
            self.observe(env.ui, lambda app, text: ready.set() if "允许执行" in text else None)

            def action():
                env.ui.tool("create_file", {"path": "new.py", "content": "new"})
                if env.ui.approve("file", "+new"):
                    performed.append("write")

            def keys():
                self.wait(ready)
                env.pipe.send_text("\x03")
            env.send(keys)
            with self.assertRaises(KeyboardInterrupt):
                env.ui.run_action(action)
            self.assertEqual(performed, [])
            self.assertIsNone(env.ui._approval)
            self.assertTrue(env.ui._app.is_running)

    def test_picker_arrows_enter_and_escape(self):
        with self.terminal() as env:
            options = [("first", "第一个 First"), ("second", "第二个 Second")]
            env.pipe.send_text("\x1b[B\r")
            self.assertEqual(env.ui.choose("选择模型", options), "second")
            env.pipe.send_text("\x1b")
            self.assertIsNone(env.ui.choose("选择会话", options))

    def test_paging_changes_transcript_without_moving_input_cursor(self):
        with self.terminal() as env:
            env.ui.notice('\n'.join(f'记录 {i:03d} ' + '中文API' * 30 for i in range(60)))
            draft = '修复API\nand 文档'
            position = len(draft) - 2
            stages = ['draft', 'up', 'home', 'down', 'end']
            reached = {name: threading.Event() for name in stages}
            stage, frames = ['draft'], {}

            def rendered(app, text):
                buffer = env.ui.editor.default_buffer
                if buffer.text != draft or buffer.cursor_position != position:
                    return
                name = stage[0]
                info = app.transcript_window.render_info
                screen = app.renderer._last_screen
                if info is None or screen is None:
                    return
                self.assertIs(app.layout.current_window, app.composer_window)
                self.assertFalse(info.ui_content.show_cursor)
                self.assertTrue(screen.show_cursor)
                frames[name] = (buffer.document, info.vertical_scroll,
                                screen.cursor_positions[app.composer_window])
                reached[name].set()
            self.observe(env.ui, rendered)

            def keys():
                env.pipe.send_text('修复API\x1b\rand 文档' + '\x1b[D' * 2)
                self.wait(reached['draft'])
                for name, key in [('up', '\x1b[5~'), ('home', '\x1b[1;5H'),
                                  ('down', '\x1b[6~'), ('end', '\x1b[1;5F')]:
                    stage[0] = name
                    env.pipe.send_text(key)
                    self.wait(reached[name])
                env.pipe.send_text('测试\r')
            env.send(keys)
            self.assertEqual(env.ui.read(), draft[:position] + '测试' + draft[position:])
            self.assertLess(frames['up'][1], frames['draft'][1])
            self.assertEqual(frames['home'][1], 0)
            self.assertGreater(frames['down'][1], 0)
            self.assertEqual(frames['end'][1], frames['draft'][1])
            self.assertEqual(len({frame[2] for frame in frames.values()}), 1)

    def test_each_page_moves_the_visible_viewport_immediately(self):
        with self.terminal() as env:
            env.ui.notice('\n'.join(f'row {i:03d}' for i in range(200)))
            names = ['initial', 'up', 'older', 'new_output', 'down', 'end', 'follow']
            reached = {name: threading.Event() for name in names}
            stage, frames = ['initial'], {}

            def rendered(app, text):
                if env.ui.editor.default_buffer.text != 'draft':
                    return
                info = app.transcript_window.render_info
                frames[stage[0]] = (info.visible_line_to_row_col[0][0], info.window_height)
                reached[stage[0]].set()
            self.observe(env.ui, rendered)

            def keys():
                env.pipe.send_text('draft')
                self.wait(reached['initial'])
                for name, key in [('up', '\x1b[5~'), ('older', '\x1b[5~')]:
                    stage[0] = name
                    env.pipe.send_text(key)
                    self.wait(reached[name])
                stage[0] = 'new_output'
                env.ui.notice('appended output')
                self.wait(reached['new_output'])
                for name in ['down', 'end', 'follow']:
                    stage[0] = name
                    env.pipe.send_text('\x1b[6~')
                    self.wait(reached[name])
                env.pipe.send_text('\r')
            env.send(keys)
            self.assertEqual(env.ui.read(), 'draft')
            page = frames['initial'][1] - 1
            self.assertEqual(frames['up'][0], frames['initial'][0] - page)
            self.assertEqual(frames['older'][0], frames['up'][0] - page)
            self.assertEqual(frames['new_output'][0], frames['older'][0])
            self.assertEqual(frames['down'][0], frames['older'][0] + page)
            self.assertEqual(frames['end'][0], frames['down'][0] + page)
            self.assertGreater(frames['follow'][0], frames['end'][0])
            self.assertIsNone(env.ui._scroll_line)

    def test_wheel_scrolls_records_over_body_and_input_without_recalling_history(self):
        from prompt_toolkit.key_binding import KeyPress
        from prompt_toolkit.keys import Keys

        with self.terminal() as env:
            env.ui.editor.history.append_string('previous command')
            env.ui.notice('\n'.join(f'row {i:03d}' for i in range(200)))
            names = ['initial', 'body_up', 'input_up', 'input_down', 'native_up', 'native_down',
                     'x10_up', 'urxvt_down', 'ctrl_up', 'windows_down']
            reached = {name: threading.Event() for name in names}
            stage, frames = ['initial'], {}

            def rendered(app, text):
                buffer = env.ui.editor.default_buffer
                if buffer.text != 'working draft':
                    return
                self.assertEqual(buffer.cursor_position, len('working draft'))
                self.assertIs(app.layout.current_window, app.composer_window)
                info = app.transcript_window.render_info
                frames[stage[0]] = info.visible_line_to_row_col[0][0]
                reached[stage[0]].set()
            self.observe(env.ui, rendered)

            def keys():
                env.pipe.send_text('working draft')
                self.wait(reached['initial'])
                app = env.ui._app
                self.assertTrue(app.mouse_support())
                for name, window, code in [('body_up', app.transcript_window, 64),
                                           ('input_up', app.composer_window, 64),
                                           ('input_down', app.composer_window, 65)]:
                    stage[0] = name
                    position = app.renderer._last_screen.visible_windows_to_write_positions[window]
                    env.pipe.send_text(f'\x1b[<{code};{position.xpos + 1};{position.ypos + 1}M')
                    self.wait(reached[name])
                for name, key in [('native_up', Keys.ScrollUp), ('native_down', Keys.ScrollDown)]:
                    stage[0] = name
                    def feed(key=key):
                        app.key_processor.feed(KeyPress(key))
                        app.key_processor.process_keys()
                    app.loop.call_soon_threadsafe(feed)
                    self.wait(reached[name])
                for name, packet in [('x10_up', '\x1b[M`!!'), ('urxvt_down', '\x1b[97;1;1M'),
                                     ('ctrl_up', '\x1b[<80;1;1M')]:
                    stage[0] = name
                    env.pipe.send_text(packet)
                    self.wait(reached[name])
                stage[0] = 'windows_down'
                def windows_wheel():
                    app.key_processor.feed(KeyPress(Keys.WindowsMouseEvent, 'NONE;SCROLL_DOWN;0;0'))
                    app.key_processor.process_keys()
                app.loop.call_soon_threadsafe(windows_wheel)
                self.wait(reached['windows_down'])
                # A real arrow key still recalls history.
                env.pipe.send_text('\x1b[A\r')
            env.send(keys)
            self.assertEqual(env.ui.read(), 'previous command')
            self.assertEqual(frames['body_up'], frames['initial'] - 3)
            self.assertEqual(frames['input_up'], frames['body_up'] - 3)
            self.assertEqual(frames['input_down'], frames['body_up'])
            self.assertEqual(frames['native_up'], frames['input_up'])
            self.assertEqual(frames['native_down'], frames['input_down'])
            self.assertEqual(frames['x10_up'], frames['native_up'])
            self.assertEqual(frames['urxvt_down'], frames['native_down'])
            self.assertEqual(frames['ctrl_up'], frames['native_up'])
            self.assertEqual(frames['windows_down'], frames['native_down'])

    def test_wheel_does_not_change_menu_selection(self):
        from prompt_toolkit.key_binding import KeyPress
        from prompt_toolkit.keys import Keys

        with self.terminal() as env:
            env.ui.notice('\n'.join(f'row {i:03d}' for i in range(200)))
            ready, scrolled = threading.Event(), threading.Event()
            initial = []

            def rendered(app, text):
                top = app.transcript_window.render_info.visible_line_to_row_col[0][0]
                if not initial:
                    initial.append(top)
                    ready.set()
                elif top < initial[0]:
                    self.assertEqual(env.ui._request['index'], 0)
                    scrolled.set()
            self.observe(env.ui, rendered)

            def keys():
                self.wait(ready)
                app = env.ui._app
                def feed():
                    app.key_processor.feed(KeyPress(Keys.ScrollUp))
                    app.key_processor.process_keys()
                app.loop.call_soon_threadsafe(feed)
                self.wait(scrolled)
                env.pipe.send_text('\x1b[B\r')
            env.send(keys)
            self.assertEqual(env.ui.choose('choose', [('first', 'First'), ('second', 'Second')]), 'second')

    def test_execution_hides_cursor_and_reuses_app_without_reprinting_transcript(self):
        with self.terminal() as env:
            ready, release = threading.Event(), threading.Event()
            env.releases.append(release)
            apps = []

            def rendered(app, text):
                apps.append(app)
                if env.ui._busy and 'Run active' in text:
                    self.assertIs(app.layout.current_window, app.composer_window)
                    self.assertFalse(app.renderer._last_screen.show_cursor)
                    ready.set()
            self.observe(env.ui, rendered)

            def action():
                env.ui.tool('run_command', {'command': 'active'})
                self.wait(release)
                env.ui.result({'ok': True, 'exit_code': 0})

            def keys():
                self.wait(ready)
                env.pipe.send_text('\x1b[5~')
                release.set()
            env.send(keys)
            env.ui.run_action(action)
            app = env.ui._app
            env.ui.run_action(lambda: env.ui.notice('completed'))
            self.assertIs(env.ui._app, app)
            self.assertTrue(app.is_running)
            self.assertEqual(env.output.getvalue(), '')
            self.assertTrue(all(item is app for item in apps))
            env.ui.close()
            self.assertFalse(app.is_running)
            self.assertIsNone(env.ui._ui_thread)

    def test_idle_resize_reflows_records_and_keeps_input_focused(self):
        from prompt_toolkit.data_structures import Size
        with self.terminal() as env:
            size = [Size(rows=32, columns=90)]
            env.ui.editor.app.output.get_size = lambda: size[0]
            env.ui.notice('中文API' * 150)
            initial, resized = threading.Event(), threading.Event()
            draft = '未提交 draft'
            frames = []

            def rendered(app, text):
                buffer = env.ui.editor.default_buffer
                if buffer.text != draft:
                    return
                width = app.transcript_window.render_info.window_width
                self.assertIs(app.layout.current_window, app.composer_window)
                self.assertEqual(buffer.cursor_position, len(draft))
                frames.append(width)
                (resized if width == 50 else initial).set()
            self.observe(env.ui, rendered)

            def keys():
                env.pipe.send_text(draft)
                self.wait(initial)
                size[0] = Size(rows=24, columns=50)
                # No keystroke or explicit invalidate: the renderer must notice
                # a resize while its input is otherwise idle.
                self.wait(resized)
                env.pipe.send_text('\r')
            env.send(keys)
            self.assertEqual(env.ui.read(), draft)
            self.assertIn(90, frames)
            self.assertIn(50, frames)

    def test_secret_input_is_masked_and_never_saved_to_history_or_transcript(self):
        with self.terminal() as env:
            secret = 'test-private-key-123'
            shown = threading.Event()
            frames = []

            def rendered(app, text):
                screen = app.renderer._last_screen
                if screen is None:
                    return
                frame = '\n'.join(''.join(cell.char for _, cell in sorted(row.items()))
                                  for _, row in sorted(screen.data_buffer.items()))
                frames.append(frame)
                if env.ui.editor.default_buffer.text == secret and '*' * len(secret) in frame:
                    shown.set()
            self.observe(env.ui, rendered)

            def keys():
                env.pipe.send_text(secret)
                self.wait(shown)
                env.pipe.send_text('\r')
            env.send(keys)
            self.assertEqual(env.ui.ask('API key', secret=True), secret)
            self.assertTrue(shown.is_set())
            self.assertNotIn(secret, '\n'.join(frames))
            self.assertEqual(list(env.ui.editor.history.get_strings()), [])
            self.assertEqual(env.ui.events, [])
            self.assertEqual(env.ui.editor.default_buffer.text, '')

    def test_visual_scroll_reaches_wrapped_mixed_line_and_end_follows(self):
        with self.terminal() as env:
            command = "".join(f"段{index:04d}En|" for index in range(1800))
            env.ui.tool("run_command", {"command": command})
            env.ui.result({"ok": True, "output": "FOLLOW-OLD"})
            env.ui.toggle_details()
            names = ["initial", "home", "middle", "down", "up", "pageback", "end", "latest"]
            reached = {name: threading.Event() for name in names}
            stage, frames = ["initial"], {}

            def rendered(app, text):
                info = app.transcript_window.render_info
                screen = app.renderer._last_screen
                if info is None or screen is None:
                    return
                visible = {row: point for row, point in info.visible_line_to_row_col.items()
                           if 0 <= row < info.window_height}
                actual = "\n".join("".join(cell.char for _, cell in sorted(row.items()))
                                   for _, row in sorted(screen.data_buffer.items()))
                name = stage[0]
                if name == "latest" and "FOLLOW-LATEST" not in actual:
                    return
                frames[name] = (info.ui_content.cursor_position, visible, actual)
                reached[name].set()
            self.observe(env.ui, rendered)

            def keys():
                self.wait(reached["initial"])
                for name, key in [("home", "\x1b[1;5H"), ("middle", "\x1b[6~"),
                                  ("down", "\x1b[6~"), ("up", "\x1b[5~"),
                                  ("pageback", "\x1b[5~"), ("end", "\x1b[1;5F")]:
                    stage[0] = name
                    env.pipe.send_text(key)
                    self.wait(reached[name])
                stage[0] = "latest"
                env.ui.notice("FOLLOW-LATEST")
                self.wait(reached["latest"])
                env.pipe.send_text("\x1b")
            env.send(keys)
            env.ui.view_history()
            middle, visible, actual = frames["middle"]
            self.assertGreater(middle.x, 1000)
            self.assertTrue(visible)
            self.assertTrue(all(row == middle.y and column > 0 for row, column in visible.values()))
            self.assertIn("段", actual)
            self.assertIn("En|", actual)
            self.assertEqual(frames["down"][0].y, middle.y)
            self.assertGreater(frames["down"][0].x, middle.x)
            self.assertEqual(frames["up"][0], middle)
            self.assertLess(frames["pageback"][0].x, middle.x)
            self.assertIn("FOLLOW-LATEST", frames["latest"][2])
            self.assertIsNone(env.ui._scroll_line)


if __name__ == "__main__":
    unittest.main()
