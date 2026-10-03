"""Behavioral checks for bounded reads and changes that preserve existing work."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from miniagent.tools import MAX_OUTPUT_CHARS, ToolRegistry


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeProcesses:
    def run(self, **kwargs):
        return {"ok": True, "called": "run", **kwargs}

    def poll(self, job_id, offset=0):
        return {"ok": True, "called": "poll", "job_id": job_id, "offset": offset}

    def cancel(self, job_id):
        return {"ok": True, "called": "cancel", "job_id": job_id}

    def close(self):
        pass


class ToolTests(unittest.TestCase):
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

    def test_read_paging_hash_and_redaction(self):
        original = b"one\r\nSECRET\r\nthree\r\n"
        self.write("sample.txt", original)
        first = self.call("read_file", path="sample.txt", limit=2)
        self.assertTrue(first["ok"])
        self.assertEqual(first["content"], "1: one\n2: [redacted]")
        self.assertEqual(first["sha256"], sha(original))
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

    def test_replace_requires_unique_match_and_current_hash(self):
        path = self.write("sample.txt", "one one\n")
        current = sha(path.read_bytes())
        result = self.call("replace_text", path="sample.txt", old_text="one", new_text="two", expected_sha256=current)
        self.assertFalse(result["ok"])
        self.assertIn("2 times", result["error"])
        self.assertEqual(path.read_text(), "one one\n")
        path.write_text("changed\n")
        result = self.call("replace_text", path="sample.txt", old_text="changed", new_text="two", expected_sha256=current)
        self.assertFalse(result["ok"])
        self.assertIn("changed since", result["error"])
        self.assertEqual(self.approvals, [])

    def test_replace_preserves_crlf_permissions_and_unrelated_text(self):
        path = self.write("sample.txt", b"keep\r\nold\r\nlast\r\n")
        if os.name != "nt":
            path.chmod(0o751)
        original_mode = stat.S_IMODE(path.stat().st_mode)
        result = self.call("replace_text", path="sample.txt", old_text="old\n", new_text="new\nextra\n", expected_sha256=sha(path.read_bytes()))
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_bytes(), b"keep\r\nnew\r\nextra\r\nlast\r\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_mode)
        self.assertEqual(self.approvals[0][0], "file")
        self.assertIn("+new", self.approvals[0][1])

    def test_overlapping_matches_are_ambiguous(self):
        path = self.write("overlap.txt", "aaa")
        result = self.call("replace_text", path="overlap.txt", old_text="aa", new_text="b", expected_sha256=sha(path.read_bytes()))
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b"aaa")

    def test_recheck_hash_after_user_approval(self):
        path = self.write("sample.txt", "old\n")
        def concurrently_edit(kind, detail):
            path.write_bytes(b"user edit\n")
            return True
        self.registry.approve = concurrently_edit
        result = self.call("replace_text", path="sample.txt", old_text="old", new_text="new", expected_sha256=sha(path.read_bytes()))
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b"user edit\n")
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
        result = self.call("apply_patch", patch=patch, expected_sha256={"one.txt": sha(first.read_bytes()), "two.txt": sha(second.read_bytes())})
        self.assertTrue(result["ok"], result)
        self.assertEqual(first.read_bytes(), b"a\nB\nc\nd\nE\nF\n")
        self.assertEqual(second.read_bytes(), b"x\r\nY\r\n")
        self.assertEqual(len(self.approvals), 1)

    def test_patch_invalid_later_file_cannot_change_first(self):
        first = self.write("one.txt", "old\n")
        second = self.write("two.txt", "user changed\n")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-old\n+new\n--- a/two.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-wrong\n+new\n"
        result = self.call("apply_patch", patch=patch, expected_sha256={"one.txt": sha(first.read_bytes()), "two.txt": sha(second.read_bytes())})
        self.assertFalse(result["ok"])
        self.assertEqual(first.read_bytes(), b"old\n")
        self.assertEqual(second.read_bytes(), b"user changed\n")
        self.assertEqual(self.approvals, [])

    def test_patch_invalid_later_hunk_is_atomic(self):
        path = self.write("one.txt", "a\nb\nc\n")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-a\n+A\n@@ -3 +3 @@\n-wrong\n+C\n"
        result = self.call("apply_patch", patch=patch, expected_sha256={"one.txt": sha(path.read_bytes())})
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b"a\nb\nc\n")

    def test_patch_supports_insertions_and_no_final_newline(self):
        path = self.write("one.txt", "end")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1,2 @@\n-end\n\\ No newline at end of file\n+start\n+finish\n\\ No newline at end of file\n"
        result = self.call("apply_patch", patch=patch, expected_sha256={"one.txt": sha(path.read_bytes())})
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_bytes(), b"start\nfinish")
        patch = "--- a/one.txt\n+++ b/one.txt\n@@ -0,0 +1 @@\n+top\n"
        result = self.call("apply_patch", patch=patch, expected_sha256={"one.txt": sha(path.read_bytes())})
        self.assertTrue(result["ok"], result)
        self.assertEqual(path.read_bytes(), b"top\nstart\nfinish")

    def test_patch_rejects_bad_counts_missing_hash_and_traversal(self):
        path = self.write("one.txt", "old\n")
        patches = [
            "--- a/one.txt\n+++ b/one.txt\n@@ -2 +1 @@\n-old\n+new\n",
            "--- a/one.txt\n+++ b/one.txt\n@@ -1,2 +1 @@\n-old\n+new\n",
            "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-old\n+new\n",
        ]
        for patch in patches:
            with self.subTest(patch=patch):
                result = self.call("apply_patch", patch=patch, expected_sha256={"one.txt": sha(path.read_bytes())})
                self.assertFalse(result["ok"])
                self.assertEqual(path.read_bytes(), b"old\n")
        valid = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertFalse(self.call("apply_patch", patch=valid, expected_sha256={})["ok"])

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


if __name__ == "__main__":
    unittest.main()
