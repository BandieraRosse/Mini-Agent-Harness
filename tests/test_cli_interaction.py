"""Behavioral coverage for interactive commands, without a network or real TTY."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from miniagent.cli import main
from miniagent.sessions import Session
from tests.test_cli import completion


def create_call(identifier, path):
    return completion(None, [{
        "id": identifier,
        "type": "function",
        "function": {"name": "create_file", "arguments": json.dumps({"path": path, "content": "created"})},
    }])


class CLIInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def invoke(self, stdin, responses=(), arguments=()):
        queued = iter(responses)
        requests, discovered = [], []

        class Client:
            def __init__(inner, config, key):
                inner.config = config

            def complete(inner, messages, tools=None, on_text=None, stream=True):
                requests.append({"model": inner.config.model, "messages": copy.deepcopy(messages)})
                result = copy.deepcopy(next(queued))
                content = result["choices"][0]["message"].get("content")
                if on_text and content:
                    on_text(content)
                return result

            def list_models(inner):
                discovered.append(inner.config.provider)
                return ["model-before", "model-after"]

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("miniagent.cli.load_api_key", return_value="test-secret-key"), \
                patch("miniagent.cli.ChatClient", Client), patch("sys.stdin", io.StringIO(stdin)), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--workspace", str(self.workspace), "--plain", "--model", "model-before", *arguments])
        return status, stdout.getvalue() + stderr.getvalue(), requests, discovered

    def test_model_switch_changes_next_request_and_keeps_conversation(self):
        status, output, requests, discovered = self.invoke(
            "first task\n/model model-after\nsecond task\n/quit\n", [completion("first answer"), completion("second answer")])
        self.assertEqual(status, 0)
        self.assertEqual([request["model"] for request in requests], ["model-before", "model-after"])
        self.assertEqual([item["content"] for item in requests[1]["messages"] if item["role"] != "system"],
                         ["first task", "first answer", "second task"])
        self.assertEqual(discovered, [])
        files = list((self.workspace / ".miniagent" / "sessions").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["model"], "model-after")
        self.assertIn("对话上下文保留", output)

    def test_no_argument_model_picker_uses_provider_list(self):
        with patch("miniagent.cli.Terminal.choose", return_value="model-after") as choose:
            status, _, requests, discovered = self.invoke("/model\nnext task\n/quit\n", [completion()], ["--no-save"])
        self.assertEqual(status, 0)
        self.assertEqual(discovered, ["deepseek"])
        self.assertEqual(requests[0]["model"], "model-after")
        self.assertEqual([item[0] for item in choose.call_args.args[1]], ["model-before", "model-after"])

    def test_permissions_and_legacy_alias_change_actual_file_authorization(self):
        for index, (enable, disable) in enumerate((("/permissions", "/approval"), ("/approval", "/permissions"))):
            allowed, denied = f"allowed-{index}.txt", f"denied-{index}.txt"
            with self.subTest(enable=enable, disable=disable):
                status, _, requests, _ = self.invoke(
                    f"{enable} trust\nwrite allowed\n{disable} ask\nwrite denied\n/quit\n",
                    [create_call("allowed", allowed), completion("created"),
                     create_call("denied", denied), completion("permission needed")], ["--no-save"])
                self.assertEqual(status, 0)
                self.assertEqual((self.workspace / allowed).read_text(encoding="utf-8"), "created")
                self.assertFalse((self.workspace / denied).exists())
                results = [json.loads(item["content"]) for item in requests[-1]["messages"] if item["role"] == "tool"]
                self.assertTrue(results[0]["ok"])
                self.assertFalse(results[-1]["ok"])

    def test_no_argument_permissions_picker_applies_explicit_selection(self):
        with patch("miniagent.cli.Terminal.choose", return_value="trust") as choose:
            status, _, _, _ = self.invoke("/permissions\nwrite allowed\n/quit\n",
                                          [create_call("allowed", "selected.txt"), completion()], ["--no-save"])
        self.assertEqual(status, 0)
        self.assertEqual([item[0] for item in choose.call_args.args[1]], ["ask", "trust", "read-only"])
        self.assertTrue((self.workspace / "selected.txt").exists())

    def test_resume_latest_resolves_saved_session_before_saving_current(self):
        saved = Session(self.workspace, "deepseek", "model-before")
        saved.append({"role": "user", "content": "older task to resume"})
        saved.append({"role": "assistant", "content": "older answer"})
        status, output, requests, _ = self.invoke("/resume latest\ncontinue older work\n/quit\n", [completion()])
        self.assertEqual(status, 0)
        self.assertIn(f"已恢复 {saved.data['id']}", output)
        self.assertEqual([item["content"] for item in requests[0]["messages"] if item["role"] != "system"],
                         ["older task to resume", "older answer", "continue older work"])

    def test_clear_and_new_reset_context_with_distinct_screen_behavior(self):
        for command, clear_screen in (("/clear", True), ("/new", False)):
            with self.subTest(command=command), patch("miniagent.cli.Terminal.clear_history", autospec=True) as clear:
                status, _, requests, _ = self.invoke(f"first task\n{command}\nsecond task\n/quit\n",
                                                      [completion("first answer"), completion("second answer")], ["--no-save"])
                self.assertEqual(status, 0)
                clear.assert_called_once()
                self.assertEqual(clear.call_args.kwargs, {"clear_screen": clear_screen})
                self.assertEqual([item["content"] for item in requests[1]["messages"] if item["role"] != "system"], ["second task"])

    def test_status_reports_cumulative_usage_and_selected_model(self):
        first, second = completion("first answer"), completion("second answer")
        first["usage"] = {"prompt_tokens": 15, "completion_tokens": 4}
        second["usage"] = {"prompt_tokens": 25, "completion_tokens": 6}
        status, output, requests, _ = self.invoke("first\n/model model-after\nsecond\n/status\n/quit\n",
                                                 [first, second], ["--no-save"])
        self.assertEqual(status, 0)
        self.assertEqual(len(requests), 2)
        self.assertIn("模型: deepseek/model-after", output)
        self.assertIn("40 in | 10 out", output)

    def test_read_only_rejects_stale_mutating_call_until_permissions_change(self):
        status, _, requests, _ = self.invoke(
            'inspect\n/permissions trust\nwrite\n/quit\n',
            [create_call('denied', 'denied.txt'), completion('permission needed'),
             create_call('allowed', 'allowed.txt'), completion('created')], ['--read-only', '--no-save'])
        self.assertEqual(status, 0)
        self.assertFalse((self.workspace / 'denied.txt').exists())
        self.assertTrue((self.workspace / 'allowed.txt').exists())
        result = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
        self.assertEqual(json.loads(result['content'])['error_code'], 'PERMISSION_DENIED')


if __name__ == "__main__":
    unittest.main()
