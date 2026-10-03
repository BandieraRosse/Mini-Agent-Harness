"""Behavioral checks for bounded reads and changes that preserve existing work."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from miniagent.tools import MAX_OUTPUT_CHARS, ToolRegistry


class FakeProcesses:
    def run(self, **kwargs):
        return {"ok": True, "called": "run", **kwargs}

    def poll(self, job_id, offset=0, **kwargs):
        return {"ok": True, "called": "poll", "job_id": job_id, "offset": offset, **kwargs}

    def cancel(self, job_id):
        return {"ok": True, "called": "cancel", "job_id": job_id}

    def close(self):
        pass


class ToolTests(unittest.TestCase):
    def test_context_patch_locates_unique_text_and_rejects_ambiguity(self):
        target = self.write("a.txt", b"unrelated\r\nfirst\r\nold\r\nlast\r\n")
        patch_text = "*** Begin Patch\n*** Update File: a.txt\n@@\n first\n-old\n+new\n last\n*** End Patch\n"
        result = self.call("apply_patch", patch=patch_text)
        self.assertTrue(result["ok"], result)
        self.assertEqual(target.read_bytes(), b"unrelated\r\nfirst\r\nnew\r\nlast\r\n")
        self.write("a.txt", b"same\nsame\n")
        ambiguous = "*** Begin Patch\n*** Update File: a.txt\n@@\n-same\n+changed\n*** End Patch\n"
        self.assertEqual(self.call("apply_patch", patch=ambiguous)["error_code"], "PATCH_CONTEXT_MISMATCH")
        self.assertEqual(target.read_bytes(), b"same\nsame\n")

    def test_approval_time_external_edit_is_preserved(self):
        target = self.write("a.txt", b"old\n")

        def approve(*args):
            target.write_bytes(b"user changed\n")
            return True

        self.registry.approve = approve
        result = self.call("replace_text", path="a.txt", old_text="old", new_text="agent")
        self.assertEqual(result["error_code"], "EDIT_CONFLICT")
        self.assertEqual(target.read_bytes(), b"user changed\n")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.approvals = []
        self.registry = ToolRegistry(self.root, FakeProcesses(), self.approve, lambda text: text.replace("SECRET", "[redacted]"))

    def approve(self, kind, detail):
        self.approvals.append((kind, detail))
        return True

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def call(self, name, **arguments):
        return self.registry.execute(name, arguments)

    def test_read_paging_and_redaction(self):
        original = b"one\r\nSECRET\r\nthree\r\n"
        self.write("sample.txt", original)
        first = self.call("read_file", path="sample.txt", limit=2)
        self.assertTrue(first["ok"])
        self.assertEqual(first["content"], "1: one\n2: [redacted]")
        self.assertNotIn("sha256", first)
        self.assertTrue(first["truncated"])
        second = self.call("read_file", path="sample.txt", offset=first["next_offset"])
        self.assertEqual(second["content"], "3: three")
        self.assertFalse(second["truncated"])

    def test_long_line_can_be_fully_read_in_pages(self):
        content = "a" * (MAX_OUTPUT_CHARS + 30)
        self.write("long.txt", content)
        first = self.call("read_file", path="long.txt")
        self.assertTrue(first["truncated"])
        self.assertLessEqual(len(first["content"]), MAX_OUTPUT_CHARS)
        second = self.call("read_file", path="long.txt", offset=first["next_offset"], column=first["next_column"])
        self.assertFalse(second["truncated"])
        self.assertEqual(first["content"][3:] + second["content"][3:], content)

    def test_redaction_happens_before_page_and_search_excerpt_boundaries(self):
        self.write("read.txt", "a" * (MAX_OUTPUT_CHARS - 6) + "SECRET")
        first = self.call("read_file", path="read.txt")
        second = self.call("read_file", path="read.txt", offset=first["next_offset"], column=first["next_column"])
        reconstructed = first["content"][3:] + second["content"][3:]
        self.assertNotIn("SECRET", reconstructed)
        self.assertIn("[redacted]", reconstructed)
        self.write("search.txt", "x" * 998 + "SECRET")
        result = self.call("search_text", query="SECRET")
        self.assertNotIn("SE", result["matches"][1]["text"])

    def test_replace_requires_unique_match(self):
        path = self.write("sample.txt", "one one\n")
        result = self.call("replace_text", path="sample.txt", old_text="one", new_text="two")
        self.assertFalse(result["ok"])
        self.assertIn("2 times", result["error"])
        self.assertEqual(path.read_text(), "one one\n")
        path.write_text("changed\n")
        result = self.call("replace_text", path="sample.txt", old_text="changed", new_text="two")
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_text(), "two\n")
        self.assertEqual(len(self.approvals), 1)
        self.assertNotIn("sha256", result["files"][0])

    def test_replace_preserves_crlf_permissions_and_unrelated_text(self):
        path = self.write("sample.txt", b"keep\r\nold\r\nlast\r\n")
        if os.name != "nt":
            path.chmod(0o751)
        original_mode = stat.S_IMODE(path.stat().st_mode)
        result = self.call("replace_text", path="sample.txt", old_text="old\n", new_text="new\nextra\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_bytes(), b"keep\r\nnew\r\nextra\r\nlast\r\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_mode)
        self.assertEqual(self.approvals[0][0], "file")
        self.assertIn("+new", self.approvals[0][1])

    def test_overlapping_matches_are_ambiguous(self):
        path = self.write("overlap.txt", "aaa")
        result = self.call("replace_text", path="overlap.txt", old_text="aa", new_text="b")
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b"aaa")

    def test_file_edit_approval_denial_has_no_effect(self):
        path = self.write("sample.txt", "old\n")
        self.registry.approve = lambda *_: False
        result = self.call("replace_text", path="sample.txt", old_text="old", new_text="new")
        self.assertFalse(result["ok"])
        self.assertTrue(result["declined"])
        self.assertEqual(path.read_bytes(), b"old\n")
        patch = "--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-old\n+new\n"
        result = self.call("apply_patch", patch=patch)
        self.assertFalse(result["ok"])
        self.assertTrue(result["declined"])
        self.assertEqual(path.read_bytes(), b"old\n")
        self.assertEqual(list(self.root.glob(".miniagent-edit-*")), [])

    def test_create_never_overwrites_and_approval_denial_has_no_effect(self):
        existing = self.write("existing.txt", "mine")
        result = self.call("create_file", path="existing.txt", content="new")
        self.assertFalse(result["ok"])
        self.assertEqual(existing.read_text(), "mine")
        self.registry.approve = lambda *_: False
        result = self.call("create_file", path="nested/new.txt", content="new")
        self.assertFalse(result["ok"])
        self.assertTrue(result["declined"])
        self.assertFalse((self.root / "nested").exists())
        self.registry.approve = self.approve
        result = self.call("create_file", path="nested/new.txt", content="new")
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.root / "nested/new.txt").read_text(), "new")

    def test_create_rechecks_concurrent_creation(self):
        def concurrent_create(kind, detail):
            self.write("new.txt", "user content")
            return True
        self.registry.approve = concurrent_create
        result = self.call("create_file", path="new.txt", content="agent content")
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / "new.txt").read_text(), "user content")

    def test_patch_multiple_hunks_and_files(self):
        first = self.write("one.txt", "a\nb\nc\nd\ne\n")
        second = self.write("two.txt", b"x\r\ny\r\n")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -4,2 +4,3 @@\n d\n-e\n+E\n+F\n--- a/two.txt\n+++ b/two.txt\n@@ -1,2 +1,2 @@\n x\n-y\n+Y\n"
        result = self.call("apply_patch", patch=patch)
        self.assertTrue(result["ok"], result)
        self.assertEqual(first.read_bytes(), b"a\nB\nc\nd\nE\nF\n")
        self.assertEqual(second.read_bytes(), b"x\r\nY\r\n")
        self.assertEqual(len(self.approvals), 1)

    def test_patch_invalid_later_file_cannot_change_first(self):
        first = self.write("one.txt", "old\n")
        second = self.write("two.txt", "user changed\n")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-old\n+new\n--- a/two.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-wrong\n+new\n"
        result = self.call("apply_patch", patch=patch)
        self.assertFalse(result["ok"])
        self.assertEqual(first.read_bytes(), b"old\n")
        self.assertEqual(second.read_bytes(), b"user changed\n")
        self.assertEqual(self.approvals, [])

    def test_patch_invalid_later_hunk_is_atomic(self):
        path = self.write("one.txt", "a\nb\nc\n")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-a\n+A\n@@ -3 +3 @@\n-wrong\n+C\n"
        result = self.call("apply_patch", patch=patch)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b"a\nb\nc\n")

    def test_patch_supports_insertions_and_no_final_newline(self):
        path = self.write("one.txt", "end")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1,2 @@\n-end\n\\ No newline at end of file\n+start\n+finish\n\\ No newline at end of file\n"
        result = self.call("apply_patch", patch=patch)
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_bytes(), b"start\nfinish")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -0,0 +1 @@\n+top\n"
        result = self.call("apply_patch", patch=patch)
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_bytes(), b"top\nstart\nfinish")

    def test_patch_rejects_bad_counts_and_traversal(self):
        path = self.write("one.txt", "old\n")
        patches = [
            "--- a/one.txt\n+++ b/one.txt\n@@ -2 +1 @@\n-old\n+new\n",
            "--- a/one.txt\n+++ b/one.txt\n@@ -1,2 +1 @@\n-old\n+new\n",
            "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-old\n+new\n",
        ]
        for patch in patches:
            with self.subTest(patch=patch):
                result = self.call("apply_patch", patch=patch)
                self.assertFalse(result["ok"])
                self.assertEqual(path.read_bytes(), b"old\n")

    def test_protected_and_external_paths_are_unavailable(self):
        for name in (".deepseek_api_key", ".openai_api_key", ".api_key", ".env", ".env.local", ".git/config", ".miniagent/session.json"):
            self.write(name, "SECRET")
            with self.subTest(path=name):
                self.assertFalse(self.call("read_file", path=name)["ok"])
                self.assertFalse(self.call("create_file", path=name + "/new", content="x")["ok"])
        self.assertFalse(self.call("read_file", path="../outside")["ok"])
        self.assertFalse(self.call("read_file", path=str(self.root.parent / "outside"))["ok"])
        self.assertFalse(self.call("create_file", path="file.txt:stream", content="x")["ok"])

    def test_symlink_escape_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside) / "foreign.txt"
            foreign.write_text("private")
            try:
                (self.root / "linked").symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation is not available for this user.")
            self.assertFalse(self.call("read_file", path="linked/foreign.txt")["ok"])
            self.assertFalse(self.call("create_file", path="linked/new.txt", content="new")["ok"])
            self.assertFalse((Path(outside) / "new.txt").exists())

    def test_discovery_ignores_generated_secret_and_binary_files(self):
        for name in (".git/config", "build/output.txt", ".miniagent/session.json", "node_modules/pkg/a.js", ".env", ".deepseek_api_key", "image.png"):
            self.write(name, "needle")
        self.write("src/a.py", "needle\nother\nneedle\n")
        self.write("unknown.bin", b"\x00needle")
        found = self.call("find_files", pattern="*.py")
        self.assertEqual(found["files"], ["src/a.py"])
        result = self.call("search_text", query="needle", limit=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["matches"][0]["path"], "src/a.py")
        second = self.call("search_text", query="needle", offset=result["next_offset"])
        self.assertEqual(second["matches"][0]["line"], 3)
        self.assertFalse(second["truncated"])
        listing = self.call("list_directory")
        self.assertFalse(any(item["path"] in {".git", "build", ".miniagent", ".env", "image.png"} for item in listing["entries"]))

    def test_output_limits_and_binary_rejection(self):
        self.write("many.txt", ("match " + "a" * 2000 + "\n") * 500)
        result = self.call("search_text", query="match", limit=200)
        self.assertTrue(result["truncated"])
        self.assertLess(len(json.dumps(result)), MAX_OUTPUT_CHARS + 4000)
        self.write("binary", b"hello\0there")
        self.assertFalse(self.call("read_file", path="binary")["ok"])

    def test_find_files_recursive_glob_includes_zero_directory_matches(self):
        names = {"root.py", "src/main.py", "src/pkg/module.py", "src/pkg/deep/leaf.py", "other/test.py"}
        for name in names | {"src/readme.txt"}:
            self.write(name, "content")
        self.assertEqual(set(self.call("find_files", pattern="**/*.py")["files"]), names)
        self.assertEqual(set(self.call("find_files", pattern="src/**/*.py")["files"]),
                         {name for name in names if name.startswith("src/")})
        self.assertEqual(set(self.call("find_files", pattern="*.py")["files"]), names)
        self.assertEqual(self.call("find_files", pattern="./*.py")["files"], ["root.py"])

    def test_find_files_ordinary_wildcards_stay_within_path_components(self):
        for name in ("src/a.py", "src/b.py", "src/long.py", "src/pkg/a.py", "src/pkg/deep/b.py"):
            self.write(name, "content")
        expected = {
            "src/*.py": {"src/a.py", "src/b.py", "src/long.py"},
            "src/?.py": {"src/a.py", "src/b.py"},
            "src/[ab].py": {"src/a.py", "src/b.py"},
            "src/*/?.py": {"src/pkg/a.py"},
            "src/**/[ab].py": {"src/a.py", "src/b.py", "src/pkg/a.py", "src/pkg/deep/b.py"},
        }
        for pattern, paths in expected.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(set(self.call("find_files", pattern=pattern)["files"]), paths)

    def test_dispatch_rejects_invalid_arguments_and_delegates_commands(self):
        for name, args in [("unknown", {}), ("read_file", []), ("read_file", {}),
                           ("read_file", {"path": "x", "limit": True}),
                           ("find_files", {"limit": 201}), ("find_files", {"typo": 3}),
                           ("run_command", {"command": "echo hello", "timeout": float("nan")}),
                           ("replace_text", {"path": "x", "old_text": "a", "new_text": "b"})]:
            with self.subTest(name=name, args=args):
                self.assertFalse(self.registry.execute(name, args)["ok"])
        self.assertEqual(self.call("run_command", command="echo hello")["called"], "run")
        self.assertEqual(self.call("poll_command", job_id="job1", offset=20)["offset"], 20)
        self.assertEqual(self.call("cancel_command", job_id="job1")["called"], "cancel")
        self.assertFalse(self.call("run_command", command="echo hello", cwd="../")["ok"])

    def test_search_filters_single_file_context_and_unique_file_paging(self):
        self.write("root.py", b"before\nneedle\nafter SECRET\nneedle\n")
        self.write("src/one.py", b"needle\nneedle\n")
        self.write("src/skip.py", b"needle\n")
        self.write("notes.txt", b"needle\n")
        single = self.call("search_text", query="needle", path="root.py", context_lines=1)
        self.assertEqual([m["line"] for m in single["matches"]], [2, 4])
        self.assertEqual(single["matches"][0]["context"], [
            {"line": 1, "text": "before", "line_truncated": False},
            {"line": 3, "text": "after [redacted]", "line_truncated": False}])
        options = dict(query="needle", include_glob="**/*.py", exclude_glob="**/skip.py", files_only=True, limit=1)
        first = self.call("search_text", **options)
        second = self.call("search_text", offset=first["next_offset"], **options)
        self.assertEqual(first["files"] + second["files"], ["root.py", "src/one.py"])
        self.assertTrue(first["truncated"])
        self.assertFalse(second["truncated"])
        self.assertEqual(second["next_offset"], 2)

    def test_search_context_remains_bounded_and_protected_paths_stay_denied(self):
        self.write("long.py", (("SECRET" * 500 + "needle\n") * 60).encode())
        result = self.call("search_text", query="needle", context_lines=5)
        self.assertTrue(result["truncated"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertGreater(result["next_offset"], 0)
        total = sum(len(row["text"]) + sum(len(c["text"]) for c in row["context"]) for row in result["matches"])
        self.assertLessEqual(total, MAX_OUTPUT_CHARS)
        for path in [".env", "../outside", ".git/config"]:
            denied = self.call("search_text", query="x", path=path, include_glob="*")
            self.assertEqual(denied["error_code"], "PATH_DENIED")
        self.assertEqual(self.call("search_text", query="x", context_lines=6)["error_code"], "INVALID_ARGUMENT")

    def test_search_cancels_during_scan_and_next_call_can_continue(self):
        for index in range(10):
            self.write(f"{index}.py", b"needle\n")
        event = threading.Event()
        self.registry.process_manager.cancel_event = event
        original_read = self.registry._read

        def interrupting_read(path):
            result = original_read(path)
            event.set()
            return result

        with mock.patch.object(self.registry, "_read", side_effect=interrupting_read) as reader:
            with self.assertRaises(KeyboardInterrupt):
                self.call("search_text", query="needle")
            self.assertEqual(reader.call_count, 1)
        event.clear()
        self.assertEqual(len(self.call("search_text", query="needle")["matches"]), 10)

    def test_error_codes_distinguish_conflicts_denials_and_partial_writes(self):
        self.write("a.txt", b"old\n")
        self.write("b.txt", b"old\n")
        conflict = self.call("replace_text", path="a.txt", old_text="missing", new_text="new")
        self.assertEqual(conflict["error_code"], "EDIT_CONFLICT")
        mismatch = self.call("apply_patch", patch="--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-wrong\n+new\n")
        self.assertEqual(mismatch["error_code"], "PATCH_CONTEXT_MISMATCH")
        self.registry.approve = lambda *args: False
        denied = self.call("replace_text", path="a.txt", old_text="old", new_text="new")
        self.assertEqual(denied["error_code"], "APPROVAL_DENIED")
        self.assertFalse(denied["retryable"])
        self.registry.approve = self.approve
        original_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("SECRET simulated failure")
            original_replace(source, target)

        patch = "".join(f"--- a/{name}.txt\n+++ b/{name}.txt\n@@ -1 +1 @@\n-old\n+new\n" for name in ["a", "b"])
        with mock.patch("miniagent.tools.os.replace", side_effect=fail_second):
            partial = self.call("apply_patch", patch=patch)
        self.assertEqual(partial["error_code"], "PARTIAL_WRITE")
        self.assertEqual(partial["applied_files"], ["a.txt"])
        self.assertNotIn("SECRET", json.dumps(partial))
        self.assertEqual((self.root / "b.txt").read_bytes(), b"old\n")

    def test_file_approval_error_and_cancel_never_write(self):
        self.registry.approve = mock.Mock(side_effect=RuntimeError("SECRET approval failed"))
        result = self.call("create_file", path="new.txt", content="new")
        self.assertEqual(result["error_code"], "APPROVAL_FAILED")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse((self.root / "new.txt").exists())
        event = threading.Event()
        self.registry.process_manager.cancel_event = event
        self.registry.approve = lambda *args: event.set() or True
        with self.assertRaises(KeyboardInterrupt):
            self.call("create_file", path="new.txt", content="new")
        self.assertFalse((self.root / "new.txt").exists())


if __name__ == "__main__":
    unittest.main()
