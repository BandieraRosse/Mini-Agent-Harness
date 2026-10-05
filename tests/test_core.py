import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from miniagent.api import APIError
from miniagent.budget import estimate_tokens
from miniagent.core import Agent
from miniagent.security import Redactor
from miniagent.sessions import Session, read_checkpoint, validate_and_repair
from miniagent.processes import ProcessManager
from miniagent.tools import ToolRegistry


def completion(content="done", calls=None):
    message = {"role": "assistant", "content": content, "phase": "commentary" if calls else "final_answer"}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {}}


def call(call_id, arguments="{}", name="example_tool"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class ScriptedClient:
    def __init__(self, responses):
        self.config = SimpleNamespace(model="fake-model")
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools=None, on_text=None, stream=True):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": tools, "stream": stream})
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        content = result["choices"][0]["message"].get("content")
        if on_text and content:
            on_text(content)
        return copy.deepcopy(result)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.session = Session(self.workspace, "deepseek", "fake-model", redact=Redactor("test-secret-key"))
        self.tools = SimpleNamespace(schemas=[], execute=Mock(return_value={"ok": True}))
        self.ui = Mock()

    def agent(self, *responses, **kwargs):
        client = ScriptedClient(responses)
        return Agent(client, self.session, self.tools, [], self.ui, **kwargs)

    def test_context_statistics_include_fixed_messages_and_tools(self):
        agent = self.agent(context_tokens=256000)
        agent.fixed = [{'role': 'system', 'content': 'fixed instructions'}]
        self.tools.schemas = [{'type': 'function', 'function': {'name': 'example_tool'}}]
        self.session.append({'role': 'user', 'content': 'current input'})
        self.assertEqual(agent.context_usage(),
                         (estimate_tokens([*agent.request_messages(), self.tools.schemas]), 256000))

    def test_usage_is_shown_after_reply_and_before_tools(self):
        response = completion('working', [call('a')])
        response['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20}
        agent = self.agent(response, completion('done'))
        observed = []
        self.ui.end_stream.side_effect = lambda: observed.append('reply')
        self.ui.usage.side_effect = lambda usage: observed.append(('usage', usage))
        self.tools.execute.side_effect = lambda *args: observed.append('tool') or {'ok': True}
        agent.run('work')
        self.assertEqual(observed[:3], ['reply', ('usage', response['usage']), 'tool'])

    def test_final_text_reaches_ui_while_request_is_still_streaming(self):
        agent = self.agent()

        def complete(messages, tools=None, on_text=None):
            for part in ('[final', '_answer]', '\n', 'hello', ' world'):
                on_text(part)
                if part == 'hello':
                    self.assertIn('hello', [entry.args[0] for entry in self.ui.stream.call_args_list])
            return completion('hello world')
        agent.client.complete = complete
        self.assertTrue(agent.run('work'))
        self.assertEqual(''.join(entry.args[0] for entry in self.ui.stream.call_args_list), 'hello world')
        self.ui.work_summary.assert_called_once()

    def test_interrupted_request_still_gets_work_summary(self):
        agent = self.agent(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            agent.run('work')
        self.ui.work_summary.assert_called_once()

    def test_multiple_tools_are_checkpointed_before_action_then_all_observed(self):
        ids_seen_on_disk = []

        def execute(name, arguments):
            checkpoint = read_checkpoint(self.session.path)
            ids_seen_on_disk.append([item["id"] for item in checkpoint["messages"][1]["tool_calls"]])
            if len(ids_seen_on_disk) == 2:
                self.assertEqual(checkpoint["messages"][-1]["tool_call_id"], "a")
            return {"ok": True, "value": arguments["value"]}

        self.tools.execute.side_effect = execute
        agent = self.agent(completion(None, [call("a", '{"value":1}'), call("b", '{"value":2}')]), completion("verified"))
        self.assertTrue(agent.run("do both"))
        self.assertEqual(ids_seen_on_disk, [["a", "b"], ["a", "b"]])
        next_messages = agent.client.requests[1]["messages"]
        self.assertEqual([item["tool_call_id"] for item in next_messages if item["role"] == "tool"], ["a", "b"])
        self.assertEqual(self.session.data["status"], "complete")

    def test_interrupt_repairs_current_and_remaining_calls_and_resume_does_not_replay(self):
        actions = []

        def execute(name, arguments):
            actions.append(arguments["value"])
            if len(actions) == 2:
                (self.workspace / "effect.txt").write_text("action happened before interruption", encoding="utf-8")
                raise KeyboardInterrupt()
            return {"ok": True}

        self.tools.execute.side_effect = execute
        agent = self.agent(completion(None, [call("a", '{"value":1}'), call("b", '{"value":2}'), call("c", '{"value":3}')]))
        with self.assertRaises(KeyboardInterrupt):
            agent.run("run tools")
        self.assertEqual(actions, [1, 2])
        results = [json.loads(item["content"]) for item in self.session.messages if item["role"] == "tool"]
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[1]["interrupted"])
        self.assertTrue(results[2]["interrupted"])
        restored = Session(self.workspace, "deepseek", "fake-model").load(self.session.data["id"])
        agent.session = restored
        agent.client = ScriptedClient([completion("Inspect effect.txt before deciding what to do next")])
        self.assertTrue(agent.run("inspect current state first"))
        self.assertEqual(actions, [1, 2])
        self.assertTrue((self.workspace / "effect.txt").exists())
        self.assertEqual(validate_and_repair(restored.messages), restored.messages)

    def test_bad_arguments_produce_tool_results_and_model_can_correct_them(self):
        agent = self.agent(
            completion(None, [call("a", "{"), call("b", "[]")]),
            completion(None, [call("c", '{"value":3}')]), completion("done"))
        self.assertTrue(agent.run("try tools"))
        first_results = [json.loads(item["content"]) for item in agent.client.requests[1]["messages"] if item["role"] == "tool"]
        self.assertEqual(len(first_results), 2)
        self.assertTrue(all(result["ok"] is False for result in first_results))
        self.tools.execute.assert_called_once_with("example_tool", {"value": 3})

    def test_real_tool_results_feed_next_request_and_persist_with_error_codes(self):
        (self.workspace / "code.py").write_text("needle\n", encoding="utf-8")
        manager = ProcessManager(self.workspace, lambda *args: True, lambda text: text)
        self.addCleanup(manager.close)
        self.tools = ToolRegistry(self.workspace, manager, lambda *args: False, lambda text: text)
        agent = self.agent(completion(None, [
            call("search", json.dumps({"query": "needle", "files_only": True}), "search_text"),
            call("denied", json.dumps({"path": "new.py", "content": "new"}), "create_file"),
            call("command", json.dumps({"command": "echo finished", "yield_time_ms": 5000, "max_output_bytes": 256}), "run_command"),
        ]), completion("done"))
        self.assertTrue(agent.run("inspect and verify"))
        outputs = {m["tool_call_id"]: json.loads(m["content"]) for m in agent.client.requests[1]["messages"] if m["role"] == "tool"}
        self.assertEqual(outputs["search"]["files"], ["code.py"])
        self.assertEqual(outputs["denied"]["error_code"], "APPROVAL_DENIED")
        self.assertTrue(outputs["command"]["complete"])
        self.assertEqual(outputs["command"]["exit_code"], 0)
        self.assertFalse((self.workspace / "new.py").exists())
        restored = Session(self.workspace, "deepseek", "fake-model").load(self.session.data["id"])
        self.assertEqual(restored.messages, self.session.messages)

    def test_partial_api_error_never_executes_tools_and_leaves_resumable_history(self):
        agent = self.agent(APIError("stream ended before DONE"))
        with self.assertRaises(APIError):
            agent.run("do something")
        self.tools.execute.assert_not_called()
        self.assertEqual(self.session.data["status"], "interrupted")
        self.assertEqual(self.session.messages, [{"role": "user", "content": "do something"}])

    def test_duplicate_call_ids_rejected_without_action(self):
        agent = self.agent(completion(None, [call("same"), call("same")]))
        with self.assertRaisesRegex(APIError, "Duplicate"):
            agent.run("do something")
        self.tools.execute.assert_not_called()
        self.assertEqual(len(self.session.messages), 1)

    def test_round_limit_leaves_complete_tool_pairs(self):
        agent = self.agent(completion(None, [call("a"), call("b")]), max_rounds=1)
        self.assertFalse(agent.run("keep working"))
        self.assertEqual(self.session.data["status"], "limit")
        self.assertEqual(validate_and_repair(self.session.messages), self.session.messages)
        self.assertEqual(len(self.session.messages), 4)

    def compaction_history(self):
        return [
            {"role": "user", "content": "old goal"},
            completion(None, [call("old")])["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "old", "content": '{"ok":true}'},
            {"role": "assistant", "content": "old result"},
            {"role": "user", "content": "current goal"},
            completion(None, [call("a"), call("b")])["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "a", "content": '{"ok":true}'},
            {"role": "tool", "tool_call_id": "b", "content": '{"ok":true}'},
            {"role": "assistant", "content": "both done"},
            {"role": "user", "content": "follow up"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "continue"},
        ]

    def test_compaction_keeps_archive_and_retains_tool_pairs_at_boundary(self):
        self.session.data["messages"] = self.compaction_history()
        archive = copy.deepcopy(self.session.messages)
        summary = "Goal: current goal\nConstraints: test-secret-key\nCompleted work: old result\nImportant findings: files\nNext steps: continue"
        agent = self.agent(completion(summary), context_chars=3000)
        self.assertTrue(agent.compact(force=True))
        self.assertEqual(self.session.data["context_start"], 5)
        self.assertEqual(self.session.messages, archive)
        active = self.session.active_messages()
        self.assertEqual(validate_and_repair(active), active)
        self.assertNotIn("test-secret-key", json.dumps(active))
        self.assertEqual(json.loads(self.session.path.read_text(encoding="utf-8"))["messages"], archive)
        self.assertIsNone(agent.client.requests[0]["tools"])
        self.assertFalse(agent.client.requests[0]["stream"])

    def test_failed_compaction_preserves_memory_and_archive(self):
        self.session.data["messages"] = self.compaction_history()
        previous = copy.deepcopy(self.session.data)
        agent = self.agent(completion("x" * 16_001))
        with self.assertRaises(ValueError):
            agent.compact(force=True)
        self.assertEqual(self.session.data, previous)


if __name__ == "__main__":
    unittest.main()
