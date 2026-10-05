import contextlib
import io
import importlib.util
import unittest
from unittest.mock import patch

from miniagent.security import Redactor, StreamRedactor
from miniagent.ui import Terminal, terminal_text


class UITests(unittest.TestCase):
    def test_work_summary_shows_duration_and_local_end_timestamp(self):
        terminal = Terminal(Redactor(), plain=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            terminal.work_summary(125)
        self.assertIn('工作用时 2分05秒', output.getvalue())
        self.assertRegex(output.getvalue(), r'结束于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

    def test_usage_totals_and_visible_per_call_counts(self):
        terminal = Terminal(Redactor(), plain=True)
        terminal.context_provider = lambda: (64000, 256000)
        with contextlib.redirect_stdout(io.StringIO()):
            terminal.usage({'prompt_tokens': 100, 'completion_tokens': 30,
                            'prompt_tokens_details': {'cached_tokens': 60},
                            'completion_tokens_details': {'reasoning_tokens': 20}})
            terminal.usage({'input_tokens': 50, 'output_tokens': 10,
                            'input_tokens_details': {'cached_tokens': 0},
                            'output_tokens_details': {'reasoning_tokens': 0}})
        self.assertEqual(terminal.tokens, {'prompt': 150, 'cached': 60,
                                           'completion': 40, 'reasoning': 20})
        self.assertIn('75.0%', terminal.statistics())
        self.assertIn('192,000/256,000', terminal.statistics())
        text = ''.join(text for _, text in terminal.fragments())
        self.assertEqual(text.count('本次调用:'), 2)
        self.assertIn('reasoning 20', text)
        self.assertIn('reasoning 0', text)

    def test_missing_usage_is_unknown_and_context_cannot_be_negative(self):
        terminal = Terminal(Redactor(), plain=True)
        terminal.context_provider = lambda: (300000, 256000)
        with contextlib.redirect_stdout(io.StringIO()):
            terminal.usage({})
            terminal.usage({'prompt_tokens': 12, 'completion_tokens': 5})
        self.assertIn('0.0%', terminal.statistics())
        self.assertIn('input 12+未知', terminal.statistics())
        self.assertIn('缓存 input 未知', ''.join(text for _, text in terminal.fragments()))

    def test_message_spacing_in_live_and_restored_transcripts(self):
        terminal = Terminal(Redactor(), plain=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            terminal.user('hello')
            terminal.stream('answer')
            terminal.end_stream()
        self.assertEqual(output.getvalue(), '› hello\n\nanswer\n\n')
        terminal.restore([{'role': 'user', 'content': 'hello'},
                          {'role': 'assistant', 'content': 'answer'}])
        self.assertEqual(''.join(text for _, text in terminal.fragments()),
                         '› hello\n\nanswer\n\n')

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
        terminal = Terminal(Redactor(), approval='ask', plain=True)
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

    def test_remembered_approval_matches_full_command_and_cwd_only(self):
        terminal = Terminal(Redactor(), approval='ask', plain=True)
        terminal.tty = True
        original = 'cwd: C:/project\ncommand: python test.py'
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=['a', 'n', 'n']) as ask:
            self.assertTrue(terminal.approve('shell', original))
            self.assertTrue(terminal.approve('shell', original))
            self.assertFalse(terminal.approve('shell', original + ' --other'))
            self.assertFalse(terminal.approve('shell', original.replace('C:/project', 'C:/elsewhere')))
        self.assertEqual(ask.call_count, 3)
        terminal.approval = 'read-only'
        self.assertFalse(terminal.approve('shell', original))
        terminal.approval = 'ask'
        terminal.approval_rules.clear()
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value='n'):
            self.assertFalse(terminal.approve('shell', original))

    def test_redacted_commands_and_file_edits_cannot_be_remembered(self):
        terminal = Terminal(Redactor(), approval='ask', plain=True)
        terminal.tty = True
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value='a'):
            self.assertFalse(terminal.approve('shell', 'echo [REDACTED]'))
            self.assertFalse(terminal.approve('file', 'diff'))
        self.assertEqual(terminal.approval_rules, set())

    def test_restore_suppresses_deferred_final_draft(self):
        terminal = Terminal(Redactor(), plain=True)
        terminal.restore([{'role': 'assistant', 'phase': 'final_answer',
                           'content': 'Premature success', 'completion_deferred': True}])
        self.assertNotIn('Premature success', ''.join(text for _, text in terminal.fragments()))

    def test_restored_invalid_tool_arguments_can_be_viewed(self):
        terminal = Terminal(Redactor(), plain=True)
        terminal.restore([
            {"role": "assistant", "tool_calls": [{"id": "bad", "function": {"name": "read_file", "arguments": "[]"}}]},
            {"role": "tool", "tool_call_id": "bad", "content": '{"ok":false,"error":"arguments must be an object"}'},
        ])
        self.assertIn("arguments must be an object", "".join(text for _, text in terminal.fragments()))
        terminal.toggle_details()
        self.assertIn("arguments", "".join(text for _, text in terminal.fragments()))

    @unittest.skipUnless(importlib.util.find_spec("prompt_toolkit"), "prompt-toolkit is not installed")
    def test_enhanced_source_selection_and_configuration_input(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from miniagent.config import Config
        from miniagent.settings import select_source
        from unittest.mock import Mock

        with create_pipe_input() as pipe:
            def factory(**kwargs):
                return PromptSession(input=pipe, output=DummyOutput(), **kwargs)

            with patch("sys.stdin.isatty", return_value=True), patch(
                    "prompt_toolkit.PromptSession", side_effect=factory):
                terminal = Terminal(Redactor())
            self.addCleanup(terminal.close)
            preferences = Mock()
            preferences.config.return_value = Config(provider="openai")
            ask = terminal.ask

            def enter_url(label):
                pipe.send_text("  https://example.com:8444/  \r")
                return ask(label)

            pipe.send_text("\x1b[B\r")
            with patch.object(terminal, "ask", side_effect=enter_url):
                config = select_source(terminal, preferences, Config())
            self.assertEqual(config.endpoint, "https://example.com:8444/v1/chat/completions")
            pipe.send_text("\r")
            self.assertEqual(terminal.ask("default"), "")
            pipe.send_text("\x03")
            with self.assertRaises(KeyboardInterrupt):
                terminal.ask("cancel")
            pipe.send_text("\x04")
            with self.assertRaises(EOFError):
                terminal.ask("exit")
            self.assertEqual(list(terminal.editor.history.get_strings()), [])
            self.assertEqual(terminal.events, [])
            pipe.send_text("continue\r")
            self.assertEqual(terminal.read(), "continue")

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
            self.addCleanup(terminal.close)
            self.assertTrue(terminal.editor.show_frame)
            self.assertEqual(terminal.editor.app.layout.current_window.style, 'class:input')
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
