import contextlib
import io
import importlib.util
import unittest
from unittest.mock import patch

from miniagent.security import Redactor, StreamRedactor
from miniagent.ui import Terminal, terminal_text


class UITests(unittest.TestCase):
    def test_stream_releases_text_immediately_and_redacts_every_split(self):
        key = "sk-abcdefghijklmnop1234567890"
        for index in range(1, len(key)):
            stream = StreamRedactor(Redactor(key))
            self.assertEqual(stream.feed("hello world!"), "hello world!")
            first = stream.feed(key[:index])
            second = stream.feed(key[index:] + " done")
            result = first + second + stream.feed("", final=True)
            self.assertEqual(result, "[REDACTED] done")

    def test_stream_non_sk_key_and_incomplete_normal_text(self):
        stream = StreamRedactor(Redactor("private-key-with-prefix"))
        self.assertEqual(stream.feed("private-key"), "")
        self.assertEqual(stream.feed("-with-prefix"), "[REDACTED]")
        self.assertEqual(stream.feed("books"), "books")
        self.assertEqual(stream.feed(" s"), " ")
        self.assertEqual(stream.feed("", final=True), "s")

    def test_failed_command_displays_exit_code_and_actual_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            Terminal(Redactor(), plain=True).result({"ok": False, "exit_code": 1, "output": "AssertionError: expected 5"})
        self.assertIn("AssertionError", output.getvalue())
        self.assertIn("1", output.getvalue())

    def test_plain_multiline_and_paste(self):
        terminal = Terminal(Redactor(), plain=True)
        with patch("builtins.input", side_effect=["first\\", "second"]):
            self.assertEqual(terminal.read(), "first\nsecond")
        with patch("builtins.input", side_effect=["/paste", "first", "", "second", "/end"]):
            self.assertEqual(terminal.read(), "first\n\nsecond")

    def test_noninteractive_approval_denies_but_trust_displays_details(self):
        terminal = Terminal(Redactor(), plain=True)
        terminal.tty = False
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(terminal.approve("shell", "python test.py"))
        terminal.approval = "trust"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(terminal.approve("shell", "python test.py"))
        self.assertIn("python test.py", output.getvalue())

    def test_terminal_escape_sequences_removed(self):
        self.assertEqual(terminal_text("\x1b[31mred\x1b[0m\x07"), "red")
        self.assertEqual(terminal_text("\x1b]0;untrusted title\x07safe"), "safe")
        self.assertEqual(terminal_text("\x9b31mtext\x9c"), "31mtext")

    @unittest.skipUnless(importlib.util.find_spec("prompt_toolkit"), "Optional terminal extra is not installed")
    def test_enhanced_multiline_interrupt_and_return_to_input(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe:
            def factory(**kwargs):
                return PromptSession(input=pipe, output=DummyOutput(), **kwargs)

            with patch("sys.stdin.isatty", return_value=True), patch("prompt_toolkit.PromptSession", side_effect=factory):
                terminal = Terminal(Redactor())
            pipe.send_text("first\x1b\rsecond\r")
            self.assertEqual(terminal.read(), "first\nsecond")
            pipe.send_text("\x03")
            with self.assertRaises(KeyboardInterrupt):
                terminal.read()
            pipe.send_text("continue\r")
            self.assertEqual(terminal.read(), "continue")
            pipe.send_text("\x04")
            with self.assertRaises(EOFError):
                terminal.read()


if __name__ == "__main__":
    unittest.main()
