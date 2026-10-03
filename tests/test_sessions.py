import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.security import Redactor
from miniagent.sessions import Session, validate_and_repair


def tool_message(*ids):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "run_command", "arguments": '{"command":"example"}'}}
        for call_id in ids]}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.session = Session(self.workspace, "deepseek", "fake-model", redact=Redactor("test-secret-key"))

    def test_crash_checkpoint_repairs_all_unrecorded_outcomes_without_replay(self):
        self.session.append({"role": "user", "content": "run three commands"})
        self.session.append(tool_message("a", "b", "c"))
        original = {"role": "tool", "tool_call_id": "a", "content": '{"ok":true,"output":"done"}'}
        self.session.append(original)
        restored = Session(self.workspace, "deepseek", "fake-model").load(self.session.data["id"])
        outputs = restored.messages[2:]
        self.assertEqual([item["tool_call_id"] for item in outputs], ["a", "b", "c"])
        self.assertEqual(outputs[0], original)
        for output in outputs[1:]:
            result = json.loads(output["content"])
            self.assertFalse(result["ok"])
            self.assertTrue(result["interrupted"])
            self.assertIn("do not replay", result["error"])
        self.assertEqual(validate_and_repair(restored.messages), restored.messages)
        self.assertEqual(Session(self.workspace, "deepseek", "fake-model").load("latest").messages, restored.messages)

    def test_no_save_and_no_save_resume_never_write(self):
        self.session.append({"role": "user", "content": "original"})
        original = self.session.path.read_bytes()
        memory = Session(self.workspace, "deepseek", "fake-model", save=False)
        memory.load(self.session.data["id"])
        memory.append({"role": "user", "content": "memory only"})
        memory.repair()
        self.assertEqual(self.session.path.read_bytes(), original)
        self.assertEqual(len(list(self.session.directory.iterdir())), 1)
        other = self.workspace / "another"
        other.mkdir()
        transient = Session(other, "deepseek", "fake-model", save=False)
        transient.append({"role": "user", "content": "hello"})
        transient.save()
        self.assertFalse((other / ".miniagent").exists())

    def test_secrets_redacted_in_messages_metadata_and_nested_tool_arguments(self):
        self.session.append({"role": "user", "content": "test-secret-key"})
        message = tool_message("call-a")
        message["tool_calls"][0]["function"]["arguments"] = '{"command":"echo test-secret-key"}'
        message["reasoning_content"] = "test-secret-key"
        self.session.append(message)
        self.session.data["summary"] = "test-secret-key"
        self.session.repair()
        text = self.session.path.read_text(encoding="utf-8")
        self.assertNotIn("test-secret-key", text)
        self.assertNotIn("test-secret-key", json.dumps(self.session.messages))
        self.assertIn("[REDACTED]", text)

    def test_atomic_save_failure_preserves_previous_checkpoint_and_cleans_temp(self):
        self.session.append({"role": "user", "content": "safe checkpoint"})
        old = self.session.path.read_bytes()
        with patch("miniagent.sessions.os.replace", side_effect=OSError("disk unavailable")):
            self.session.append({"role": "user", "content": "new journaled message"})
            with self.assertRaises(OSError):
                self.session.save()
        self.assertEqual(self.session.path.read_bytes(), old)
        self.assertEqual(list(self.session.directory.glob(".checkpoint-*")), [])
        restored = Session(self.workspace, 'deepseek', 'fake-model', save=False).load(self.session.data['id'])
        self.assertEqual(restored.messages[-1]['content'], 'new journaled message')

    def test_journal_avoids_full_rewrite_recovers_partial_tail_and_redacts(self):
        self.session.append({'role': 'user', 'content': 'initial'})
        checkpoint = self.session.path.read_bytes()
        self.session.append({'role': 'assistant', 'content': 'test-secret-key', 'phase': 'commentary'})
        self.assertEqual(self.session.path.read_bytes(), checkpoint)
        journal = self.session.path.with_suffix('.jsonl')
        self.assertNotIn(b'test-secret-key', journal.read_bytes())
        with journal.open('ab') as stream:
            stream.write(b'{"seq":2,"message":"\xe4\xb8')
        restored = Session(self.workspace, 'deepseek', 'fake-model').load(self.session.data['id'])
        self.assertEqual(len(restored.messages), 2)
        self.assertEqual(restored.messages[-1]['content'], '[REDACTED]')
        self.assertFalse(journal.exists())

    def test_journal_checkpoint_leftovers_do_not_duplicate_messages(self):
        self.session.append({'role': 'user', 'content': 'initial'})
        self.session.append({'role': 'assistant', 'content': 'answer'})
        journal = self.session.path.with_suffix('.jsonl')
        old_records = journal.read_bytes()
        self.session.save()
        journal.write_bytes(old_records)
        restored = Session(self.workspace, 'deepseek', 'fake-model').load(self.session.data['id'])
        self.assertEqual(len(restored.messages), 2)

    def test_complete_corrupt_journal_record_is_not_silently_ignored(self):
        self.session.append({'role': 'user', 'content': 'initial'})
        journal = self.session.path.with_suffix('.jsonl')
        for content in (b'bad json\n', b'{"seq":3,"state":{},"message":{}}\n'):
            journal.write_bytes(content)
            with self.assertRaises(ValueError):
                Session(self.workspace, 'deepseek', 'fake-model').load(self.session.data['id'])

    def test_periodic_snapshot_contains_all_journal_messages(self):
        for number in range(34):
            self.session.append({'role': 'user', 'content': str(number)})
        checkpoint = json.loads(self.session.path.read_text(encoding='utf-8'))
        self.assertEqual(len(checkpoint['messages']), 33)
        restored = Session(self.workspace, 'deepseek', 'fake-model', save=False).load(self.session.data['id'])
        self.assertEqual(restored.messages, self.session.messages)

    def test_malformed_pairings_rejected(self):
        messages = [[{"role": "tool", "tool_call_id": "a", "content": "{}"}],
                    [tool_message("a", "a")],
                    [tool_message("a"), {"role": "tool", "tool_call_id": "b", "content": "{}"}],
                    [{"role": "assistant", "tool_calls": 7}],
                    [{"role": [], "content": "wrong type"}],
                    [{"role": "user", "content": None}]]
        for value in messages:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_and_repair(value)

    def test_corrupt_session_files_fail_cleanly_and_do_not_replace_live_session(self):
        self.session.append({"role": "user", "content": "original"})
        original = copy.deepcopy(self.session.data)
        wrong_boundary = copy.deepcopy(original)
        wrong_boundary["messages"] = [tool_message("a"), {"role": "tool", "tool_call_id": "a", "content": "{}"}]
        wrong_boundary["context_start"] = 1
        for value in ([], {**original, "workspace": []}, wrong_boundary, {**original, "context_start": True}):
            self.session.path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.session.load(original["id"])
            self.assertEqual(self.session.data, original)

    def test_missing_optional_memory_fields_receive_defaults(self):
        self.session.append({"role": "user", "content": "old session"})
        data = copy.deepcopy(self.session.data)
        del data["summary"]
        del data["context_start"]
        self.session.path.write_text(json.dumps(data), encoding="utf-8")
        restored = Session(self.workspace, "deepseek", "fake-model").load(data["id"])
        self.assertEqual(restored.active_messages(), data["messages"])

    def test_session_listing_skips_damaged_files_and_redacts_titles(self):
        self.session.append({"role": "user", "content": "working"})
        for name, value in (("array", []), ("null-title", {"messages": [{"role": "user", "content": None}]}),
                            ("wrong-time", {"messages": [], "updated_at": None})):
            (self.session.directory / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(len(self.session.list_saved()), 1)
        self.assertEqual(self.session.list_saved()[0]["title"], "working")


if __name__ == "__main__":
    unittest.main()
