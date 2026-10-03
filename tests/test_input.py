import importlib.util
import unittest

from miniagent.input import COMMANDS, grapheme_boundaries, help_text, previous_word_start


class TextBoundaryTests(unittest.TestCase):
    def test_mixed_chinese_latin_combining_and_emoji_boundaries(self):
        clusters = ["a", "中", "e\u0301", "👩\u200d💻", "👍🏽", "🇨🇳", "🇺🇸", "b"]
        text = "".join(clusters)
        points = grapheme_boundaries(text)
        self.assertEqual([text[start:end] for start, end in zip(points, points[1:])], clusters)
        self.assertEqual(grapheme_boundaries(""), [0])

    def test_line_breaks_do_not_absorb_combining_marks(self):
        text = "a\r\n\u0301b"
        points = grapheme_boundaries(text)
        self.assertEqual([text[start:end] for start, end in zip(points, points[1:])], ["a", "\r\n", "\u0301", "b"])

    def test_word_deletion_respects_mixed_language_boundaries(self):
        cases = {"修复bug": "修复", "fix中文": "fix中", "fix中文   ": "fix中", "fix file_name": "fix ",
                 "hello world!!!": "hello world", "hello café\u0301": "hello ", "   ": ""}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(text[:previous_word_start(text, len(text))], expected)

    def test_common_commands_are_prominent_and_legacy_commands_documented(self):
        self.assertEqual([command.name for command in COMMANDS[:5]], ["clear", "resume", "status", "model", "permissions"])
        self.assertIn("/exit", help_text())
        self.assertIn("/permissions [ask|trust]", help_text())
        self.assertNotIn("/diff", help_text())


@unittest.skipUnless(importlib.util.find_spec("prompt_toolkit"), "Optional terminal extra is not installed")
class EnhancedInputTests(unittest.TestCase):
    def choices(self, text, **kwargs):
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document
        from miniagent.input import SlashCompleter
        return list(SlashCompleter(**kwargs).get_completions(Document(text), CompleteEvent()))

    def read(self, keys, choices=None):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.output import DummyOutput
        from miniagent.input import SlashCompleter, register_completion_bindings, register_editing_bindings

        bindings = KeyBindings()
        completer = SlashCompleter(choices)
        register_editing_bindings(bindings)
        register_completion_bindings(bindings, completer)

        @bindings.add("escape", "enter")
        @bindings.add("c-j")
        def newline(event):
            event.current_buffer.insert_text("\n")

        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput(), multiline=True,
                                    completer=completer, complete_while_typing=True, key_bindings=bindings)
            pipe.send_text(keys)
            return session.prompt()

    def test_slash_menu_descriptions_and_hidden_aliases(self):
        choices = self.choices("/")
        self.assertEqual([choice.text for choice in choices[:5]], ["/clear", "/resume", "/status", "/model", "/permissions"])
        self.assertTrue(all(choice.display_meta_text for choice in choices))
        self.assertNotIn("/exit", [choice.text for choice in choices])
        self.assertEqual([choice.text for choice in self.choices("/ex")], ["/exit"])
        self.assertEqual([choice.text for choice in self.choices("/mod")], ["/model"])

    def test_argument_completion_and_dynamic_models(self):
        self.assertEqual([choice.text for choice in self.choices("/permissions tr")], ["trust"])
        self.assertEqual([choice.text for choice in self.choices("/approval a")], ["ask"])
        self.assertEqual([choice.text for choice in self.choices("/resume l")], ["latest"])
        models = {"model": lambda: ("deepseek-chat", "deepseek-reasoner")}
        self.assertEqual([choice.text for choice in self.choices("/model deepseek-r", choices=models)], ["deepseek-reasoner"])

    def test_no_completion_inside_prompts_or_multiline_paste(self):
        for text in ("hello /mo", "/tmp/file", "first\n/mo", "/model\nhello", "/ model", "/model two args"):
            with self.subTest(text=text):
                self.assertEqual(self.choices(text), [])

    def test_enter_completes_prefix_without_submitting_partial_command(self):
        self.assertEqual(self.read("/mod\r"), "/model")
        self.assertEqual(self.read("/permissions tr\r"), "/permissions trust")
        self.assertEqual(self.read("/unknown\r"), "/unknown")

    def test_tab_inserts_command_and_space_then_enter_submits_no_argument(self):
        self.assertEqual(self.read("/mod\t\r", {"model": ("deepseek-chat",)}), "/model ")
        self.assertEqual(self.read("/permissions\t\r"), "/permissions ")
        self.assertEqual(self.read("/permissions tr\t\r"), "/permissions trust ")

    def test_arrows_select_menu_and_enter_submits_selection(self):
        self.assertEqual(self.read("/\x1b[B\x1b[B\r"), "/resume")
        self.assertEqual(self.read("/\x1b[A\r"), "/quit")
        self.assertEqual(self.read("/permissions \x1b[B\x1b[B\r"), "/permissions trust")

    def test_midline_backspace_removes_one_chinese_character(self):
        self.assertEqual(self.read("a中b\x1b[D\x7f\r"), "ab")
        self.assertEqual(self.read("a中文b\x1b[D\x7f\r"), "a中b")

    def test_midline_delete_removes_one_chinese_character(self):
        self.assertEqual(self.read("a中b\x01\x1b[C\x1b[3~\r"), "ab")

    def test_backspace_handles_combining_marks_and_emoji_as_units(self):
        for cluster in ("e\u0301", "👩\u200d💻", "👍🏽", "🇨🇳"):
            with self.subTest(cluster=cluster):
                self.assertEqual(self.read("a" + cluster + "b\x1b[D\x7f\r"), "ab")

    def test_left_right_and_delete_share_grapheme_boundaries(self):
        cluster = "👨\u200d👩\u200d👧\u200d👦"
        self.assertEqual(self.read("a" + cluster + "b\x1b[D\x1b[D\x1b[3~\r"), "ab")
        self.assertEqual(self.read("a" + cluster + "b\x01\x1b[C\x1b[C\x7f\r"), "ab")

    def test_ctrl_w_preserves_other_language_and_multiline_editing(self):
        self.assertEqual(self.read("fix中文abc\x17\r"), "fix中文")
        self.assertEqual(self.read("fix中文abc\x17\x17\r"), "fix中")
        self.assertEqual(self.read("first\x1b\r中文abc\x17\r"), "first\n中文")
        self.assertEqual(self.read("中文\x0asecond\x01\x7f\r"), "中文second")


if __name__ == "__main__":
    unittest.main()
