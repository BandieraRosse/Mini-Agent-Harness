"""Real subprocess checks for output, deadlines, containment, and secret handling."""

import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
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
        result = self.manager.run(python_command("import sys;sys.stdout.write('x'*2000000)"))
        self.assertTrue(result["output_limit_reached"])
        self.assertEqual(result["captured_bytes"], processes.MAX_CAPTURE_BYTES)
        self.assertEqual(result["dropped_bytes"], 2000000 - processes.MAX_CAPTURE_BYTES)
        self.assertLessEqual(len(result["output"]), processes.PAGE_BYTES)
        final_page = self.manager.poll(result["job_id"], result["captured_bytes"])
        self.assertTrue(final_page["truncated"])
        self.assertFalse(final_page["has_more"])
        self.assertEqual(final_page["output"], "")

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
            original_wait = job.done.wait
            calls = 0

            def interrupted_wait(*wait_args, **wait_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt
                return original_wait(*wait_args, **wait_kwargs)

            job.done.wait = interrupted_wait
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
