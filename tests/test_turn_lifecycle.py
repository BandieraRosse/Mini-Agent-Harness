"""Turn completion depends on explicit phase and observed job outcomes."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from miniagent.core import Agent
from miniagent.messages import MessageDisplay, classify
from miniagent.processes import ProcessManager
from miniagent.sessions import Session, validate_and_repair
from miniagent.tools import ToolRegistry
from tests.test_core import ScriptedClient, call, completion
from tests.test_processes import python_command


class TurnLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = ProcessManager(self.root, lambda *args: True, lambda value: value)
        self.addCleanup(self.manager.close)
        self.tools = ToolRegistry(self.root, self.manager, lambda *args: True, lambda value: value)
        self.session = Session(self.root, "custom", "fake", save=False)
        self.ui = Mock()

    def agent(self, *responses, **kwargs):
        return Agent(ScriptedClient(responses), self.session, self.tools, [], self.ui, **kwargs)

    def test_text_only_commentary_continues_until_explicit_final(self):
        progress = completion("[commentary]\nChecking the repository")
        progress["choices"][0]["message"].pop("phase")
        agent = self.agent(progress, completion(None, [call("read", '{"path":"."}', "list_directory")]), completion("Finished"))
        self.assertTrue(agent.run("inspect"))
        self.assertEqual(len(agent.client.requests), 3)
        self.assertEqual(self.session.messages[1]["phase"], "commentary")
        shown = "".join(c.args[0] for c in self.ui.stream.call_args_list)
        self.assertEqual(shown, "Checking the repositoryFinished")
        self.assertNotIn("[commentary]", shown)

    def test_untagged_text_never_silently_finishes(self):
        untagged = completion("I will inspect the files")
        untagged["choices"][0]["message"].pop("phase")
        agent = self.agent(untagged, untagged, untagged)
        self.assertFalse(agent.run("work"))
        self.assertEqual(self.session.data["status"], "stalled")

    def test_finished_but_unobserved_job_blocks_final_until_poll(self):
        job = self.manager.run(python_command("raise SystemExit(3)"), yield_time_ms=0)
        self.assertTrue(self.manager._jobs[job["job_id"]].done.wait(5))
        poll = call("poll", json.dumps({"job_id": job["job_id"]}), "poll_command")
        agent = self.agent(completion("Premature success"), completion(None, [poll]), completion("Verification failed with exit 3"))
        self.assertTrue(agent.run("verify"))
        shown = "".join(c.args[0] for c in self.ui.stream.call_args_list)
        self.assertNotIn("Premature success", shown)
        self.assertIn("Verification failed", shown)
        observed = json.loads(next(m["content"] for m in self.session.messages if m["role"] == "tool"))
        self.assertEqual(observed["exit_code"], 3)
        self.assertEqual(self.manager.completion_blockers(), [])

    def test_live_required_job_blocks_but_explicit_service_does_not(self):
        task = self.manager.run(python_command("import time;time.sleep(10)"), yield_time_ms=0)
        agent = self.agent(completion("done"), max_rounds=1)
        self.assertFalse(agent.run("verify"))
        self.assertEqual(self.session.data["status"], "limit")
        self.manager.cancel(task["job_id"])
        self.manager.run(python_command("import time;time.sleep(10)"), yield_time_ms=0, purpose="service")
        self.assertTrue(self.agent(completion("Server running during this session")).run("start server"))

    def test_marker_split_and_native_end_turn_are_explicit(self):
        pieces = []
        display = MessageDisplay(pieces.append)
        for fragment in ["[com", "ment", "ary]", "hello"]:
            display.feed(fragment)
        self.assertEqual("".join(pieces), "hello")
        self.assertEqual(classify({"role": "assistant", "content": "progress"}, False)["phase"], "commentary")
        self.assertEqual(classify({"role": "assistant", "content": "done"}, True)["phase"], "final_answer")
        with self.assertRaises(ValueError):
            classify({"role": "assistant", "content": "[final_answer] done"}, False)

    def test_256k_batch_budget_pages_results_without_losing_archive_or_pairing(self):
        calls = [call(str(i), "{}", "search_text") for i in range(4)]
        self.session.messages.extend([{"role": "user", "content": "inspect"}, completion(None, calls)["choices"][0]["message"]])
        for entry in calls:
            self.session.messages.append({"role": "tool", "tool_call_id": entry["id"],
                                          "content": json.dumps({"ok": True, "output": "x" * 220000})})
        archive = copy.deepcopy(self.session.messages)
        agent = self.agent()
        self.assertEqual(agent.context_tokens, 256000)
        messages = agent.request_messages()
        self.assertLessEqual(agent._size([*messages, self.tools.schemas]), agent.input_budget)
        self.assertEqual(validate_and_repair(messages), messages)
        self.assertEqual(self.session.messages, archive)
        result = json.loads(messages[-1]["content"])
        self.assertEqual(result["result_ref"], "3")
        full = self.tools.execute("read_tool_result", {"tool_call_id": "3", "offset": 100, "limit": 200})
        self.assertEqual(full["content"], archive[-1]["content"][100:300])

    def test_parallel_reads_finish_before_mutation_and_observations_stay_ordered(self):
        barrier = threading.Barrier(2)
        events = []
        main_thread = threading.get_ident()

        def read(path, **kwargs):
            barrier.wait(timeout=3)
            events.append(path)
            return {"ok": True, "path": path}

        calls = [call("a", '{"path":"a"}', "read_file"), call("b", '{"path":"b"}', "read_file"),
                 call("write", '{"path":"out","content":"ok"}', "create_file")]
        self.tools.approve = lambda *args: len(events) == 2 and threading.get_ident() == main_thread
        with patch.object(self.tools, "_read_file", side_effect=read):
            self.assertTrue(self.agent(completion(None, calls), completion("done")).run("work"))
        self.assertEqual([m["tool_call_id"] for m in self.session.messages if m["role"] == "tool"], ["a", "b", "write"])
        self.assertEqual((self.root / "out").read_text(), "ok")

    def test_read_only_schema_and_runtime_both_exclude_mutations(self):
        self.tools.permission_mode = lambda: 'read-only'
        names = {item['function']['name'] for item in self.tools.schemas}
        self.assertIn('read_file', names)
        self.assertIn('poll_command', names)
        self.assertNotIn('run_command', names)
        for name, args in [('run_command', {'command': 'echo forbidden'}),
                           ('create_file', {'path': 'forbidden', 'content': 'bad'})]:
            result = self.tools.execute(name, args)
            self.assertEqual(result['error_code'], 'PERMISSION_DENIED')
        self.assertFalse((self.root / 'forbidden').exists())

    def test_unicode_output_reserve_is_counted_in_token_budget(self):
        from miniagent.budget import estimate_tokens
        agent = self.agent()
        self.assertEqual(agent.input_budget, 256000 - 8192 - 4096)
        self.assertGreater(estimate_tokens('\u4e2d' * 500), estimate_tokens('a' * 500))


if __name__ == "__main__":
    unittest.main()
