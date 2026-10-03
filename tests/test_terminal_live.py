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
                terminal = Terminal(Redactor())
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
                content = sender.layout.current_control.create_content(width=100, height=40)
                text = "\n".join("".join(part[1] for part in content.get_line(index))
                                 for index in range(content.line_count))
                callback(sender, text)
            app.after_render += after_render
            return app

        terminal._make_app = make_app

    def wait(self, event):
        self.assertTrue(event.wait(3), "expected terminal state was not reached")

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
            self.assertIsNone(env.ui._app)
            self.assertIsNone(env.ui.current_tool)
            env.pipe.send_text("继续 next\r")
            self.assertEqual(env.ui.read(), "继续 next")

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

            def prompt_rendered(app):
                buffer = env.ui.editor.default_buffer
                if shown.is_set() and env.ui._app is None and not app.is_done:
                    if buffer.text == draft and buffer.cursor_position == cursor:
                        restored.set()
            env.ui.editor.app.after_render += prompt_rendered

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
            self.assertIsNone(env.ui._app)

    def test_picker_arrows_enter_and_escape(self):
        with self.terminal() as env:
            options = [("first", "第一个 First"), ("second", "第二个 Second")]
            env.pipe.send_text("\x1b[B\r")
            self.assertEqual(env.ui.choose("选择模型", options), "second")
            env.pipe.send_text("\x1b")
            self.assertIsNone(env.ui.choose("选择会话", options))

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
                info = app.layout.current_window.render_info
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
                for name, key in [("home", "\x1b[H"), ("middle", "\x1b[6~" * 2),
                                  ("down", "\x1b[B"), ("up", "\x1b[A"),
                                  ("pageback", "\x1b[5~"), ("end", "\x1b[F")]:
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
