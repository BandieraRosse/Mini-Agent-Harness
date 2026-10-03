import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from miniagent.cli import main
from miniagent.config import Config, Preferences, key_path
from miniagent.security import Redactor
from miniagent.settings import credentials, select_source
from miniagent.ui import Terminal


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patched = patch("miniagent.config.user_directory", return_value=self.root / "profile")
        patched.start()
        self.addCleanup(patched.stop)

    def test_cancel_source_and_invalid_endpoint_leave_preferences_unchanged(self):
        preferences = Preferences()
        preferences.save(Config())
        before = preferences.path.read_bytes()
        ui = Mock()
        ui.choose.return_value = None
        self.assertIsNone(select_source(ui, preferences, Config()))
        ui.ask.side_effect = ["https://user:secret@example.com", "model"]
        with self.assertRaises(ValueError):
            select_source(ui, preferences, Config(), "openai")
        self.assertEqual(preferences.path.read_bytes(), before)

    def test_key_save_and_memory_choice_never_put_secrets_in_preferences(self):
        ui = Mock()
        redact = Redactor()
        config = Config()
        ui.choose.return_value = "memory"
        with patch("getpass.getpass", return_value="private-key"):
            key = credentials(ui, config, self.root, redact, edit=True)
        self.assertEqual(key, "private-key")
        self.assertFalse(key_path(config).exists())
        self.assertEqual(redact(key), "[REDACTED]")
        ui.choose.return_value = "save"
        with patch("getpass.getpass", return_value="saved-key"):
            credentials(ui, config, self.root, redact, edit=True)
        Preferences().save(config)
        self.assertEqual(key_path(config).read_text(encoding="utf-8").strip(), "saved-key")
        self.assertNotIn("saved-key", Preferences().path.read_text(encoding="utf-8"))

    def test_source_menu_has_only_two_options_and_gpt_accepts_url(self):
        ui = Mock(tty=True)
        ui.choose.return_value = "openai"
        ui.ask.return_value = "https://example.com:8444/"
        config = select_source(ui, Preferences(), Config())
        self.assertEqual([value for value, _ in ui.choose.call_args.args[1]], ["deepseek", "openai"])
        self.assertEqual(config.endpoint, "https://example.com:8444/v1/chat/completions")

    def test_plain_menu_accepts_number_and_blank_cancels(self):
        ui = Terminal(Redactor(), plain=True)
        ui.tty = True
        with redirect_stdout(io.StringIO()), patch("builtins.input", side_effect=["2", ""]):
            self.assertEqual(ui.choose("choose", [("a", "A"), ("b", "B")]), "b")
            self.assertIsNone(ui.choose("choose", [("a", "A")]))

    def test_startup_selects_source_key_and_advertised_model_on_every_run(self):
        original_init = Terminal.__init__

        def terminal_init(ui, *args, **kwargs):
            original_init(ui, *args, **kwargs)
            ui.tty = True

        from tests.test_api import http_server, answer
        import json
        catalog = {"body": json.dumps({"data": [{"id": "gateway-model"}]}).encode(),
                   "content_type": "application/json"}
        with http_server([catalog, {"body": answer("[final_answer]\nverified")}]) as (url, requests):
            for run in range(2):
                requests.clear()
                output = io.StringIO()
                with patch.object(Terminal, "__init__", terminal_init), \
                        patch.object(Terminal, "choose", side_effect=["openai", "memory", "gateway-model"]) as choose, \
                        patch.object(Terminal, "ask", return_value=url.removesuffix("/v1")), \
                        patch.object(Terminal, "read", side_effect=["hello", "/quit"]), \
                        patch("getpass.getpass", return_value="synthetic-startup-key"), \
                        redirect_stdout(output), redirect_stderr(output):
                    code = main(["--plain", "--no-save", "-C", str(self.root)])
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual([item[0] for item in choose.call_args_list[0].args[1]], ["deepseek", "openai"])
                self.assertEqual([r["path"] for r in requests], ["/v1/models", "/v1/chat/completions"])
                self.assertEqual(requests[1]["body"]["model"], "gateway-model")
                self.assertEqual(requests[1]["headers"]["Authorization"], "Bearer synthetic-startup-key")
                self.assertNotIn("synthetic-startup-key", output.getvalue())
                self.assertFalse(key_path(Preferences().config()).exists())

    def test_temporary_replacement_does_not_overwrite_saved_key(self):
        from miniagent.config import save_api_key, load_api_key
        config = Config(provider="openai", base_url="https://example.com/v1")
        save_api_key(config, "stored-key")
        ui = Mock()
        ui.choose.return_value = "memory"
        with patch("getpass.getpass", return_value="temporary-key"):
            self.assertEqual(credentials(ui, config, self.root, Redactor(), edit=True), "temporary-key")
        self.assertEqual(load_api_key(config, self.root), "stored-key")

    def test_interactive_start_without_key_switch_and_restart(self):
        original_init = Terminal.__init__
        def terminal_init(ui, *args, **kwargs):
            original_init(ui, *args, **kwargs)
            ui.tty = True
        prompts = ["/settings", "/provider openai", "/model persisted-model", "/status", "/quit"]
        output = io.StringIO()
        with patch.object(Terminal, "__init__", terminal_init), \
                patch.object(Terminal, "read", side_effect=prompts), \
                patch.object(Terminal, "choose", return_value=None), \
                patch.object(Terminal, "ask", return_value=""), \
                patch("miniagent.cli.load_api_key") as loader, \
                patch("miniagent.cli.credentials") as connect, \
                redirect_stdout(output), redirect_stderr(output):
            code = main(["--plain", "--no-save", "-C", str(self.root)])
        self.assertEqual(code, 0, output.getvalue())
        loader.assert_not_called()
        connect.assert_not_called()
        self.assertIn("openai/persisted-model", output.getvalue())
        self.assertNotIn("未知命令", output.getvalue())
        self.assertEqual(Preferences().config().provider, "openai")
        self.assertEqual(Preferences().config().model, "persisted-model")

    def test_switch_source_resets_conversation_and_credentials(self):
        calls = []
        class Client:
            def __init__(self, config, key):
                self.config, self.key = config, key
            def complete(self, messages, tools=None, **kwargs):
                calls.append((self.config.provider, self.key, messages))
                return {"choices": [{"message": {"role": "assistant", "content": "done",
                                                 "phase": "final_answer"}, "finish_reason": "stop"}]}
        output = io.StringIO()
        with patch("miniagent.cli.ChatClient", Client), \
                patch("miniagent.cli.load_api_key", return_value="old-key"), \
                patch("miniagent.cli.credentials", return_value="new-key"), \
                patch.object(Terminal, "ask", return_value=""), \
                patch("sys.stdin", io.StringIO("first question\n/provider openai\nsecond question\n/quit\n")), \
                redirect_stdout(output), redirect_stderr(output):
            code = main(["--plain", "--no-save", "-C", str(self.root)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual([call[:2] for call in calls], [("deepseek", "old-key"), ("openai", "new-key")])
        self.assertNotIn("first question", str(calls[1][2]))

    def test_user_directory_is_protected_even_inside_workspace(self):
        from miniagent.tools import ToolRegistry
        from miniagent.tool_errors import ToolError
        config = Config()
        from miniagent.config import save_api_key
        save_api_key(config, "private-key")
        registry = ToolRegistry(self.root, Mock(), Mock(), Redactor())
        with self.assertRaises(ToolError):
            registry._path(str(key_path(config)))

    def test_failed_credentials_keep_current_client_usable(self):
        # Editing credentials must not discard a working connection on cancellation/failure.
        output = io.StringIO()
        with patch("miniagent.cli.load_api_key", return_value="old-key"), \
                patch("miniagent.cli.credentials", side_effect=ValueError("login failed")), \
                patch.object(Terminal, "choose", return_value="credentials"), \
                patch("sys.stdin", io.StringIO("/settings\n/status\n/quit\n")), \
                redirect_stdout(output), redirect_stderr(output):
            code = main(["--plain", "--no-save", "-C", str(self.root)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("login failed", output.getvalue())
        self.assertIn("deepseek/deepseek-flash", output.getvalue())
        self.assertFalse(Preferences().path.exists())


if __name__ == "__main__":
    unittest.main()
