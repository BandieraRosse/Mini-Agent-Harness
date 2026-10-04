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


def completion(content="done", calls=None):
    message = {"role": "assistant", "content": content, "phase": "commentary" if calls else "final_answer"}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {}}


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        patched = patch("miniagent.config.user_directory", return_value=self.workspace / "profile")
        patched.start()
        self.addCleanup(patched.stop)

    def invoke(self, arguments, responses=(), stdin=""):
        queued = iter(responses)
        requests = []

        class Client:
            def __init__(inner, config, key):
                inner.config = config

            def complete(inner, messages, tools=None, on_text=None, stream=True):
                requests.append(copy.deepcopy(messages))
                result = next(queued)
                if on_text and result["choices"][0]["message"].get("content"):
                    on_text(result["choices"][0]["message"]["content"])
                return copy.deepcopy(result)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("miniagent.cli.load_api_key", return_value="test-secret-key") as keys, patch("miniagent.cli.ChatClient", Client), patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--workspace", str(self.workspace), "--plain", *arguments])
        return status, stdout.getvalue() + stderr.getvalue(), requests, keys.call_count

    def test_one_shot_saves_redacted_conversation_and_displays_answer(self):
        code, output, requests, _ = self.invoke(["-p", "test-secret-key test"], [completion("finished test-secret-key")])
        self.assertEqual(code, 0)
        self.assertIn("finished [REDACTED]", output)
        self.assertNotIn("test-secret-key", output)
        files = list((self.workspace / ".miniagent" / "sessions").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("test-secret-key", files[0].read_text(encoding="utf-8"))
        self.assertNotIn("test-secret-key", json.dumps(requests))

    def test_no_save_creates_no_session_files(self):
        code, _, _, _ = self.invoke(["--no-save", "say hello"], [completion("hello")])
        self.assertEqual(code, 0)
        self.assertFalse((self.workspace / ".miniagent").exists())

    def test_noninteractive_denies_mutation_and_shell_but_returns_observations(self):
        calls = [{"id": "file", "type": "function", "function": {"name": "create_file", "arguments": '{"path":"new.txt","content":"data"}'}},
                 {"id": "shell", "type": "function", "function": {"name": "run_command", "arguments": '{"command":"echo should-not-run"}'}}]
        code, _, requests, _ = self.invoke(["--ask", "--no-save", "do work"], [completion(None, calls), completion("permission needed")])
        self.assertEqual(code, 0)
        self.assertFalse((self.workspace / "new.txt").exists())
        results = [json.loads(item["content"]) for item in requests[1] if item["role"] == "tool"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] is False for result in results))
        self.assertTrue(results[1]["denied"])

    def test_default_trust_authorizes_reviewed_file_creation(self):
        calls = [{"id": "file", "type": "function", "function": {"name": "create_file", "arguments": '{"path":"new.txt","content":"data"}'}}]
        code, output, _, _ = self.invoke(["--no-save", "write file"], [completion(None, calls), completion("created")])
        self.assertEqual(code, 0)
        self.assertEqual((self.workspace / "new.txt").read_text(encoding="utf-8"), "data")
        self.assertIn("new.txt", output)

    def test_resume_repairs_pending_call_and_waits_for_new_prompt(self):
        saved = Session(self.workspace, "deepseek", "fake-model")
        saved.append({"role": "user", "content": "old work"})
        saved.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "old-call", "type": "function", "function": {"name": "create_file", "arguments": '{"path":"must-not-replay.txt","content":"no"}'}}]})
        code, _, requests, _ = self.invoke(["--resume", saved.data["id"]], stdin="/exit\n")
        self.assertEqual(code, 0)
        self.assertEqual(requests, [])
        self.assertFalse((self.workspace / "must-not-replay.txt").exists())
        code, _, requests, _ = self.invoke(["--resume", saved.data["id"], "-p", "inspect previous state"], [completion("needs inspection")])
        self.assertEqual(code, 0)
        result = next(item for item in requests[0] if item["role"] == "tool")
        self.assertTrue(json.loads(result["content"])["interrupted"])
        self.assertEqual(requests[0][-1]["content"], "inspect previous state")
        self.assertFalse((self.workspace / "must-not-replay.txt").exists())

    def test_list_sessions_never_prompts_for_key_or_calls_model(self):
        saved = Session(self.workspace, "deepseek", "fake-model")
        saved.append({"role": "user", "content": "previous task"})
        code, output, requests, key_count = self.invoke(["--list-sessions"])
        self.assertEqual(code, 0)
        self.assertIn("previous task", output)
        self.assertEqual(requests, [])
        self.assertEqual(key_count, 0)

    def test_interactive_clear_starts_new_memory_and_help_does_not_call_model(self):
        code, output, requests, _ = self.invoke(["--no-save"], [completion("first answer"), completion("second answer")],
                                               stdin="/help\nfirst task\n/clear\nsecond task\n/exit\n")
        self.assertEqual(code, 0)
        self.assertIn("/resume", output)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("first task", json.dumps(requests[1]))
        self.assertEqual(requests[1][-1]["content"], "second task")
        self.assertFalse((self.workspace / ".miniagent").exists())


if __name__ == "__main__":
    unittest.main()
