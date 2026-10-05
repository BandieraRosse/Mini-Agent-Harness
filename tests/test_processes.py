"""Real subprocess checks for output, deadlines, containment, and secret handling."""

import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from miniagent import processes
from miniagent.processes import ProcessManager


def python_command(source: str) -> str:
    encoded = base64.b64encode(source.encode()).decode("ascii")
    arguments = [sys.executable, "-c", f"import base64;exec(base64.b64decode('{encoded}'))"]
    if os.name == "nt" and Path(processes.shell_description()).stem.lower() in {"pwsh", "powershell"}:
        return "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


class ProcessManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.approvals = []
        self.secret = "test-only-secret-123456789"
        self.manager = ProcessManager(self.workspace, self.approve, lambda value: value,
                                      secrets=(self.secret,))

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def approve(self, kind, details):
        self.approvals.append((kind, details))
        return True

    def wait_for(self, job_id, predicate, limit=5):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            result = self.manager.poll(job_id)
            if predicate(result):
                return result
            time.sleep(0.02)
        self.fail(f"Job did not reach expected state: {self.manager.poll(job_id)}")

    def test_foreground_output_exit_code_and_cwd(self):
        (self.workspace / "nested").mkdir()
        result = self.manager.run(python_command(
            "import os,sys;print(os.path.basename(os.getcwd()),flush=True);"
            "print('stderr-line',file=sys.stderr);sys.exit(3)"), cwd="nested")
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("nested", result["output"])
        self.assertIn("stderr-line", result["output"])
        self.assertEqual(result["cwd"], str(self.workspace / "nested"))
        self.assertTrue(result["complete"])
        self.assertEqual(self.manager._jobs[result["job_id"]].timeout, 600)
        self.assertGreaterEqual(result["elapsed"], 0)
        self.assertEqual(self.approvals[0][0], "shell")

    def test_output_paging_preserves_unicode(self):
        expected = "你好🙂" * 6000
        result = self.manager.run(python_command(
            "import sys;sys.stdout.buffer.write(('你好🙂'*6000).encode('utf-8'))"))
        self.assertTrue(result["has_more"])
        pages = [result["output"]]
        while result["has_more"]:
            result = self.manager.poll(result["job_id"], result["next_offset"])
            self.assertLessEqual(len(result["output"].encode()), processes.PAGE_BYTES)
            pages.append(result["output"])
        self.assertEqual("".join(pages), expected)
        self.assertFalse(result["truncated"])

    def test_default_python_encoding_handles_chinese(self):
        result = self.manager.run(python_command("print('中文输出🙂')"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"].strip(), "中文输出🙂")

    @unittest.skipUnless(os.name == "nt", "PowerShell is the Windows default")
    def test_powershell_encoding_and_reported_executable(self):
        self.assertEqual(self.manager.shell, processes.shell_description())
        if Path(self.manager.shell).stem.lower() not in {"pwsh", "powershell"}:
            self.skipTest("PowerShell is not installed")
        result = self.manager.run("Write-Output '中文输出🙂'; Write-Output $PSVersionTable.PSEdition")
        self.assertTrue(result["ok"], result)
        self.assertIn("中文输出🙂", result["output"])
        self.assertTrue("Core" in result["output"] or "Desktop" in result["output"])

    def test_capture_limit_is_bounded_and_explicit(self):
        result = self.manager.run(python_command("import sys;sys.stdout.write('x'*1999996+'DONE')"))
        self.assertTrue(result["output_limit_reached"])
        self.assertEqual(result["captured_bytes"], processes.MAX_CAPTURE_BYTES)
        self.assertEqual(result["dropped_bytes"], 2000000 - processes.MAX_CAPTURE_BYTES)
        self.assertLessEqual(len(result["output"]), processes.PAGE_BYTES)
        self.assertTrue(result["output_tail"].endswith("DONE"))
        self.assertEqual(result["total_bytes"], 2000000)
        omitted = result["omitted_range"]
        self.assertEqual(omitted["end"] - omitted["start"], result["dropped_bytes"])
        tail_page = self.manager.poll(result["job_id"], omitted["start"])
        self.assertEqual(tail_page["offset"], omitted["end"])
        self.assertEqual(tail_page["skipped_range"], omitted)
        final_page = self.manager.poll(result["job_id"], result["total_bytes"])
        self.assertTrue(final_page["truncated"])
        self.assertFalse(final_page["has_more"])
        self.assertEqual(final_page["output"], "")

    def test_rolling_tail_preserves_absolute_unicode_offsets_and_budget(self):
        job = processes._Job("synthetic", mock.Mock(returncode=None), self.workspace, 10, None)
        self.manager._jobs[job.job_id] = job
        job.done.set()  # No real process; avoid process cleanup in tearDown.
        job.process.returncode = 0
        expected = "HEADER\n" + "你好🙂" * 300 + self.secret + "FINAL"
        with mock.patch.object(processes, "MAX_CAPTURE_BYTES", 512):
            self.manager._append(job, expected)
            first = self.manager.poll(job.job_id, max_output_bytes=256)
            self.assertTrue(first["output"].startswith("HEADER"))
            self.assertTrue(first["output_tail"].endswith("[REDACTED]FINAL"))
            old_cursor = first["tail_start_offset"]
            self.manager._append(job, "世界🙂" * 100 + "LATEST")
            result = self.manager.poll(job.job_id, old_cursor, max_output_bytes=256)
            self.assertIn("skipped_range", result)
            source = (self.manager._safe(expected) + "世界🙂" * 100 + "LATEST").encode()
            self.assertEqual(source[result["offset"]:result["next_offset"]].decode(), result["output"])
            head = self.manager.poll(job.job_id, max_output_bytes=256)
            self.assertLessEqual(len(head["output"].encode()) + len(head["output_tail"].encode()), 256)
            self.assertLessEqual(head["captured_bytes"], 512)
            self.assertEqual(head["total_bytes"], len(source))
            self.assertNotIn(self.secret, json.dumps(head))
            self.assertNotIn("\ufffd", head["output"] + head["output_tail"])
            self.assertEqual(source[head["output_tail_offset"]:].decode(), head["output_tail"])

    def test_yield_returns_live_job_without_replaying_or_changing_deadline(self):
        command = python_command("import time;open('once','a').write('x');time.sleep(.3);print('done')")
        result = self.manager.run(command, yield_time_ms=0, timeout=5)
        self.assertEqual(result["status"], "running")
        job_id = result["job_id"]
        result = self.wait_for(job_id, lambda r: r["complete"])
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.workspace / "once").read_text(), "x")
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(self.manager._jobs[job_id].timeout, 5)

    def test_waiting_poll_returns_new_output_and_then_completion(self):
        source = ("from pathlib import Path;import time;print('ready',flush=True);"
                  "\nwhile not Path('release').exists(): time.sleep(.01)"
                  "\nprint('next',flush=True)"
                  "\nwhile not Path('finish').exists(): time.sleep(.01)")
        started = self.manager.run(python_command(source), yield_time_ms=0)
        ready = self.wait_for(started["job_id"], lambda r: "ready" in r["output"])
        timer = threading.Timer(.15, lambda: (self.workspace / "release").touch())
        timer.start()
        try:
            before = time.monotonic()
            result = self.manager.poll(started["job_id"], ready["next_offset"], wait_ms=300_000)
            self.assertGreaterEqual(time.monotonic() - before, .1)
            self.assertLess(time.monotonic() - before, 3)
            self.assertIn("next", result["output"])
            self.assertFalse(result["complete"])
            (self.workspace / "finish").touch()
            final = self.manager.poll(started["job_id"], result["next_offset"], wait_ms=300_000)
            self.assertTrue(final["complete"])
        finally:
            timer.join()

    def test_quiet_poll_waits_for_notification_without_periodic_checks(self):
        started = self.manager.run(python_command("import time;time.sleep(10)"), yield_time_ms=0)
        job = self.manager._jobs[started["job_id"]]
        with mock.patch.object(job.changed, "wait", wraps=job.changed.wait) as wait:
            result = self.manager.poll(job.job_id, wait_ms=150)
        self.assertFalse(result["complete"])
        self.assertEqual(wait.call_count, 1)

    def test_runtime_metadata_uses_original_deadline_and_stops_at_completion(self):
        started = self.manager.run(python_command("import time;time.sleep(10)"), timeout=30, yield_time_ms=0)
        job = self.manager._jobs[started["job_id"]]
        with mock.patch.object(processes.time, "monotonic", return_value=job.started + 4):
            result = self.manager._result(job, 0)
            self.assertEqual(result["timeout"], 30)
            self.assertEqual(result["elapsed"], 4)
            self.assertEqual(result["remaining"], 26)
        final = self.manager.cancel(job.job_id)
        self.assertEqual(final["remaining"], 0)
        self.assertEqual(final["timeout"], 30)
        with mock.patch.object(processes.time, "monotonic", return_value=job.started + 100):
            self.assertEqual(self.manager.poll(job.job_id)["elapsed"], final["elapsed"])

    def test_poll_wait_is_bounded_and_cancellable(self):
        started = self.manager.run(python_command("import time;time.sleep(10)"), yield_time_ms=0)
        before = time.monotonic()
        quiet = self.manager.poll(started["job_id"], wait_ms=100)
        self.assertGreaterEqual(time.monotonic() - before, .08)
        self.assertEqual(quiet["status"], "running")
        self.manager.cancel_event = threading.Event()
        timer = threading.Timer(.1, self.manager.cancel_event.set)
        timer.start()
        try:
            before = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                self.manager.poll(started["job_id"], wait_ms=300_000)
            self.assertLess(time.monotonic() - before, 3)
            self.assertEqual(self.manager._jobs[started["job_id"]].reason, "cancelled")
        finally:
            timer.join()
            self.manager.cancel_event.clear()

    def test_command_failure_codes_and_options_validation(self):
        failed = self.manager.run(python_command("raise SystemExit(4)"))
        self.assertEqual(failed["error_code"], "COMMAND_FAILED")
        self.assertFalse(failed["retryable"])
        self.assertIn("output", failed["next_action"])
        for kwargs in [{"yield_time_ms": True}, {"yield_time_ms": -1},
                       {"yield_time_ms": 60001}, {"max_output_bytes": 1}]:
            result = self.manager.run("echo never", **kwargs)
            self.assertEqual(result["error_code"], "INVALID_ARGUMENT")
        for wait_ms in [0, 60001, 300_000]:
            self.assertTrue(self.manager.poll(failed["job_id"], wait_ms=wait_ms)["complete"])
        for kwargs in [{"wait_ms": 300001}, {"wait_ms": -1}, {"wait_ms": True},
                       {"max_output_bytes": True}]:
            self.assertEqual(self.manager.poll(failed["job_id"], **kwargs)["error_code"], "INVALID_ARGUMENT")
        timeout = self.manager.run(python_command("import time;time.sleep(10)"), timeout=.1)
        self.assertEqual(timeout["error_code"], "COMMAND_TIMEOUT")
        self.assertEqual(timeout["timeout"], .1)
        self.assertEqual(timeout["remaining"], 0)
        self.assertIn("total runtime limit of 0.1 seconds", timeout["error"])
        self.assertIn("process tree was terminated", timeout["error"])

    def test_approval_denial_never_runs_command(self):
        self.manager.approve = lambda kind, details: False
        result = self.manager.run(python_command("open('should-not-exist','w').close()"))
        self.assertTrue(result["denied"])
        self.assertFalse((self.workspace / "should-not-exist").exists())

    def test_rejects_cwd_outside_workspace(self):
        result = self.manager.run("echo impossible", cwd="..")
        self.assertFalse(result["ok"])
        self.assertIn("inside the workspace", result["error"])
        self.assertEqual(self.approvals, [])

    def test_rejects_destructive_git_even_if_approved(self):
        for command in ["git reset --hard", "git -C . reset --hard HEAD", "git clean -fdx",
                        "git clean --force", "git checkout .", "git checkout -- .",
                        "git restore --source=HEAD .", "echo hi && git.exe reset --hard"]:
            with self.subTest(command=command):
                result = self.manager.run(command)
                self.assertFalse(result["ok"])
                self.assertIn("destructive Git", result["error"])
        self.assertEqual(self.approvals, [])
        self.assertFalse(self.manager._destructive_git("git status --short"))
        self.assertFalse(self.manager._destructive_git("git clean -n"))
        self.assertFalse(self.manager._destructive_git("git diff --name-only"))

    def test_child_environment_scrubs_keys_and_values(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "irrelevant-test-key",
                             "DeepSeek_Api_Key": "irrelevant-test-key",
                             "NORMAL_SECRET_HOLDER": f"prefix-{self.secret}-suffix",
                             "MINIAGENT_TEST_ALLOWED": "ordinary-value"}):
            result = self.manager.run(python_command(
                "import os,json;print(json.dumps({key:os.environ.get(key) for key in "
                "['OPENAI_API_KEY','DeepSeek_Api_Key','NORMAL_SECRET_HOLDER','MINIAGENT_TEST_ALLOWED']}))"))
        data = json.loads(result["output"])
        self.assertIsNone(data["OPENAI_API_KEY"])
        self.assertIsNone(data["DeepSeek_Api_Key"])
        self.assertIsNone(data["NORMAL_SECRET_HOLDER"])
        self.assertEqual(data["MINIAGENT_TEST_ALLOWED"], "ordinary-value")

    def test_secrets_crossing_capture_chunks_are_redacted(self):
        source = (f"import sys,time;secret={self.secret!r};"
                  "sys.stdout.write('A'*4090+secret[:4]);sys.stdout.flush();time.sleep(.05);"
                  "sys.stdout.write(secret[4:]+'!');sys.stdout.flush()")
        result = self.manager.run(python_command(source))
        self.assertTrue(result["ok"])
        self.assertEqual(result["output"], "A" * 4090 + "[REDACTED]!")
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertNotIn(self.secret.encode(), self.manager._jobs[result["job_id"]].output)

    def test_foreground_timeout(self):
        before = time.monotonic()
        result = self.manager.run(python_command("import time;time.sleep(10)"), timeout=0.15)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["complete"])
        self.assertLess(time.monotonic() - before, 4)

    def test_background_timeout_is_enforced_without_polling(self):
        result = self.manager.run(python_command("import time;time.sleep(10)"), timeout=0.15, background=True)
        job = self.manager._jobs[result["job_id"]]
        self.assertTrue(job.done.wait(4))
        completed = self.manager.poll(result["job_id"])
        self.assertEqual(completed["status"], "timed_out")
        self.assertIsNotNone(job.process.poll())

    def test_cancel_terminates_grandchild(self):
        child_source = "import time;time.sleep(.8);open('orphan-marker','w').close();time.sleep(10)"
        parent_source = (f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child_source!r}]);"
                         "print('ready',flush=True);time.sleep(10)")
        started = self.manager.run(python_command(parent_source), background=True)
        self.wait_for(started["job_id"], lambda result: "ready" in result["output"])
        cancelled = self.manager.cancel(started["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["complete"])
        time.sleep(1)
        self.assertFalse((self.workspace / "orphan-marker").exists())

    def test_foreground_cancel_event_interrupts_worker_and_terminates_tree(self):
        child_source = ("from pathlib import Path;import time;Path('child-ready').touch();"
                        "time.sleep(1);Path('orphan-marker').touch();time.sleep(10)")
        parent_source = (f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child_source!r}]);"
                         "time.sleep(10)")
        self.manager.cancel_event = threading.Event()
        outcomes = []

        def run_foreground():
            try:
                outcomes.append(self.manager.run(python_command(parent_source)))
            except BaseException as error:
                outcomes.append(error)

        worker = threading.Thread(target=run_foreground, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while not (self.workspace / "child-ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.workspace / "child-ready").exists(), "The grandchild must start before cancellation")
        before = time.monotonic()
        self.manager.cancel_event.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "Cancellation must unblock the foreground worker")
        self.assertLess(time.monotonic() - before, 2)
        self.assertIsInstance(outcomes[0], KeyboardInterrupt)
        job = next(iter(self.manager._jobs.values()))
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.reason, "cancelled")
        self.assertIsNotNone(job.process.poll())
        time.sleep(1.1)
        self.assertFalse((self.workspace / "orphan-marker").exists(), "Cancellation must also stop descendants")

    def test_close_stops_active_jobs(self):
        started = self.manager.run(python_command("import time;time.sleep(10)"), background=True)
        self.manager.close()
        result = self.manager.poll(started["job_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["complete"])
        self.assertFalse(self.manager.run("echo closed")["ok"])

    def test_shell_exit_cleans_remaining_descendants(self):
        child_source = "import time;time.sleep(.5);open('orphan-marker','w').close();time.sleep(10)"
        parent_source = (f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child_source!r}]);"
                         "print('parent-exited',flush=True)")
        before = time.monotonic()
        result = self.manager.run(python_command(parent_source), timeout=5)
        self.assertTrue(result["ok"], result)
        self.assertLess(time.monotonic() - before, 4)
        time.sleep(.7)
        self.assertFalse((self.workspace / "orphan-marker").exists())

    def test_approval_and_cwd_errors_are_redacted(self):
        result = self.manager.run("echo x", cwd="../" + self.secret)
        self.assertNotIn(self.secret, json.dumps(result))
        self.manager.approve = lambda kind, details: self.approvals.append((kind, details)) or False
        self.manager.run("echo " + self.secret)
        self.assertNotIn(self.secret, str(self.approvals))
        self.assertIn("[REDACTED]", str(self.approvals))

    def test_keyboard_interrupt_cleans_process_before_reraise(self):
        real_job = processes._Job

        def create_interrupting_job(*args, **kwargs):
            job = real_job(*args, **kwargs)
            original_wait = job.changed.wait
            calls = 0

            def interrupted_wait(*wait_args, **wait_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt
                return original_wait(*wait_args, **wait_kwargs)

            job.changed.wait = interrupted_wait
            return job

        with mock.patch.object(processes, "_Job", side_effect=create_interrupting_job):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.run(python_command("import time;time.sleep(10)"))
        job = next(iter(self.manager._jobs.values()))
        self.assertIsNotNone(job.process.poll())
        self.assertTrue(job.done.is_set())

    def test_unknown_ids_report_uncertainty(self):
        for result in [self.manager.poll("old-session-id"), self.manager.cancel("old-session-id")]:
            self.assertFalse(result["ok"])
            self.assertTrue(result["uncertain"])

    def test_completed_jobs_are_evicted_at_limit(self):
        with mock.patch.object(processes, "MAX_JOBS", 2):
            first = self.manager.run("echo first")
            self.manager.run("echo second")
            self.manager.run("echo third")
            self.assertEqual(len(self.manager._jobs), 2)
            self.assertTrue(self.manager.poll(first["job_id"])["uncertain"])

    def test_invalid_values_are_tool_errors(self):
        for arguments in [{"command": ""}, {"command": "echo x", "timeout": float("nan")},
                          {"command": "echo x", "timeout": -1},
                          {"command": "echo x", "background": "yes"}]:
            self.assertFalse(self.manager.run(**arguments)["ok"])
        job = self.manager.run("echo x")
        self.assertFalse(self.manager.poll(job["job_id"], -1)["ok"])
        self.assertFalse(self.manager.poll(job["job_id"], 100000)["ok"])


if __name__ == "__main__":
    unittest.main()
