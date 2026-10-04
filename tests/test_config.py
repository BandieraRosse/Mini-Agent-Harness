import json
import getpass
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from miniagent.config import Config, Preferences, key_path, load_api_key, save_api_key


class ConfigTests(unittest.TestCase):
    def test_hidden_prompt_callback_is_used_only_when_no_saved_key_exists(self):
        config = Config(provider='openai', base_url='https://example.com/v1')
        prompt = Mock(return_value='private-key')
        with patch('getpass.getpass') as fallback:
            self.assertEqual(load_api_key(config, self.user_dir, prompt=prompt), 'private-key')
            fallback.assert_not_called()
        prompt.assert_called_once_with('openai API key (hidden)')
        save_api_key(config, 'stored-key')
        prompt.reset_mock()
        self.assertEqual(load_api_key(config, self.user_dir, prompt=prompt), 'stored-key')
        prompt.assert_not_called()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.user_dir = Path(temporary.name) / "user"
        patched = patch("miniagent.config.user_directory", return_value=self.user_dir)
        patched.start()
        self.addCleanup(patched.stop)

    def test_provider_defaults_and_endpoint(self):
        self.assertEqual(Config().model, "deepseek-flash")
        self.assertEqual(Config().endpoint, "https://api.deepseek.com/chat/completions")
        config = Config(provider="openai")
        self.assertEqual(config.model, "gpt-6-astra")
        self.assertEqual(config.endpoint, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(Config(base_url="https://example.com/v1/chat/completions/").endpoint,
                         "https://example.com/v1/chat/completions")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits require Linux/macOS")
    def test_first_key_save_creates_private_directories_and_file(self):
        previous = os.umask(0o022)
        try:
            save_api_key(Config(), "synthetic-key")
        finally:
            os.umask(previous)
        path = key_path(Config())
        self.assertEqual(stat.S_IMODE(self.user_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_preferences_survive_restart_and_cli_overrides_do_not_persist(self):
        saved = Preferences()
        saved.save(Config(provider="openai", model="saved-model"))
        saved.save(Config(model="deep-model"))
        restarted = Preferences()
        self.assertEqual(restarted.config().model, "deep-model")
        self.assertEqual(restarted.config(provider="openai").model, "saved-model")
        self.assertEqual(restarted.config(model="temporary").model, "temporary")
        self.assertEqual(Preferences().config().model, "deep-model")
        self.assertNotIn("account", restarted.data)

    def test_user_key_wins_and_endpoint_changes_cannot_reuse_it(self):
        config = Config()
        save_api_key(config, "user-secret")
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            (workspace / ".deepseek_api_key").write_text("legacy-secret", encoding="utf-8")
            self.assertEqual(load_api_key(config, workspace), "user-secret")
            changed = Config(base_url="https://another.example/v1")
            with patch("getpass.getpass", return_value="new-secret") as prompt:
                self.assertEqual(load_api_key(changed, workspace), "new-secret")
                prompt.assert_called_once()
        self.assertNotEqual(key_path(config), key_path(changed))
        self.assertEqual(key_path(config).read_text(encoding="utf-8").strip(), "user-secret")

    def test_invalid_preferences_fail_without_echoing_contents(self):
        self.user_dir.mkdir()
        (self.user_dir / "config.json").write_text('{"provider": "secret"}', encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            Preferences()
        self.assertNotIn("secret", str(caught.exception))

    def test_only_two_providers_and_gpt_root_url(self):
        from miniagent.config import PROVIDERS
        self.assertEqual(set(PROVIDERS), {"deepseek", "openai"})
        for provider in ("chatgpt", "custom"):
            with self.assertRaises(ValueError):
                Config(provider=provider)
        config = Config(provider="openai", base_url="https://example.com:8444/")
        self.assertEqual(config.endpoint, "https://example.com:8444/v1/chat/completions")

    def test_old_preferences_migrate_without_subscription_credentials(self):
        self.user_dir.mkdir()
        path = self.user_dir / "config.json"
        path.write_text(json.dumps({"provider": "custom", "account": "old", "profiles": {
            "custom": {"base_url": "https://example.com/v1", "model": "gateway-model"},
            "chatgpt": {"model": "old"}}}), encoding="utf-8")
        preferences = Preferences()
        self.assertEqual(preferences.config().provider, "openai")
        self.assertEqual(preferences.config().model, "gateway-model")
        preferences.save(preferences.config())
        self.assertNotIn("account", json.loads(path.read_text(encoding="utf-8")))
        path.write_text('{"provider":"chatgpt","profiles":{"chatgpt":{}}}', encoding="utf-8")
        self.assertEqual(Preferences().config().provider, "deepseek")

    def test_old_custom_key_import_matches_exact_endpoint(self):
        config = Config(provider="openai", base_url="https://example.com/v1")
        legacy = key_path(config).with_name(key_path(config).name.replace("openai-", "custom-", 1))
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy-key", encoding="utf-8")
        self.assertEqual(load_api_key(config, self.user_dir), "legacy-key")
        self.assertEqual(key_path(config).read_text(encoding="utf-8").strip(), "legacy-key")

    def test_insecure_or_credential_urls_rejected_without_echo(self):
        urls = ["http://example.com", "https://user:secret@example.com", "https://example.com?api_key=secret",
                "https://example.com#secret", "https://example.com?", "file:///tmp/key", "https://",
                "https://example.com\nsecret", "https://localhost:0", "https://localhost:99999"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                Config(base_url=url)
            self.assertNotIn("secret", str(caught.exception))
        for url in ("http://127.0.0.1:1234", "http://[::1]:1234", "https://example.com:1234"):
            Config(base_url=url)

    def test_invalid_numbers_rejected(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": float("inf")},
                       {"max_retries": -1}, {"max_retries": 6}, {"max_retries": True}, {"max_tokens": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)

    def test_workspace_key_wins_and_never_enters_config(self):
        with tempfile.TemporaryDirectory() as root:
            workspace, install = Path(root) / "work", Path(root) / "install"
            workspace.mkdir()
            install.mkdir()
            (workspace / ".deepseek_api_key").write_text("\ufefftest-work-key\n", encoding="utf-8")
            (install / ".deepseek_api_key").write_text("test-install-key", encoding="utf-8")
            with patch("miniagent.config.INSTALL_ROOT", install), patch("getpass.getpass") as prompt:
                config = Config()
                self.assertEqual(load_api_key(config, workspace), "test-work-key")
                prompt.assert_not_called()
                self.assertNotIn("test-work-key", repr(config))
                self.assertEqual(key_path(config).read_text(encoding="utf-8").strip(), "test-work-key")

    def test_provider_key_isolation_and_install_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            workspace, install = Path(root) / "work", Path(root) / "install"
            workspace.mkdir()
            install.mkdir()
            (workspace / ".deepseek_api_key").write_text("test-deepseek-key", encoding="utf-8")
            (install / ".openai_api_key").write_text("test-openai-key", encoding="utf-8")
            with patch("miniagent.config.INSTALL_ROOT", install):
                self.assertEqual(load_api_key(Config(provider="openai"), workspace), "test-openai-key")
                with patch("getpass.getpass", return_value="test-custom-key") as prompt:
                    config = Config(provider="openai", model="local", base_url="http://localhost:9000/v1")
                    self.assertEqual(load_api_key(config, workspace), "test-custom-key")
                    prompt.assert_called_once()

    def test_environment_key_is_ignored_and_hidden_input_not_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            with patch("miniagent.config.INSTALL_ROOT", workspace), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-env-key"}), patch("getpass.getpass", return_value="test-prompt-key"):
                self.assertEqual(load_api_key(Config(), workspace), "test-prompt-key")
            self.assertEqual(list(workspace.iterdir()), [])

    def test_visible_getpass_fallback_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("miniagent.config.INSTALL_ROOT", Path(root)), patch("getpass.getpass", side_effect=getpass.GetPassWarning):
                with self.assertRaisesRegex(ValueError, "Hidden input"):
                    load_api_key(Config(), Path(root))

    def test_bad_key_file_fails_without_echo(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            with patch("miniagent.config.INSTALL_ROOT", workspace):
                for value in ("", "secret\nsecond-line", "secret " * 9000):
                    (workspace / ".deepseek_api_key").write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError) as caught:
                        load_api_key(Config(), workspace)
                    self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
