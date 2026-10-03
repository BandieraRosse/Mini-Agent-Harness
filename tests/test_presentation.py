import unittest

from miniagent.presentation import ToolRecord, tool_fragments


def rendered(record, detailed=False):
    return "".join(text for _, text in tool_fragments(record, detailed))


class PresentationTests(unittest.TestCase):
    def test_read_summary_names_range_without_contents(self):
        record = ToolRecord("read_file", {"path": "中文.py"}, {
            "ok": True, "path": "中文.py", "offset": 5, "total_lines": 20,
            "content": "5: private detail\n6: second",
            "truncated": True,
        })
        text = rendered(record)
        self.assertIn("中文.py", text)
        self.assertIn("5–6 / 20", text)
        self.assertIn("截断", text)
        self.assertNotIn("private detail", text)

    def test_search_summaries_count_returned_results(self):
        for name, args, field in [("search_text", {"query": "你好English"}, "matches"),
                                  ("find_files", {"pattern": "*.py"}, "files"),
                                  ("list_directory", {"path": "src"}, "entries")]:
            with self.subTest(name=name):
                text = rendered(ToolRecord(name, args, {"ok": True, field: ["one", "two"]}))
                self.assertIn("2 ", text)
                self.assertIn("returned", text)
                self.assertIn(next(iter(args.values())), text)

    def test_success_output_preview_has_tail_and_disclosure(self):
        record = ToolRecord("run_command", {"command": "python -m unittest", "cwd": "tests"}, {
            "ok": True, "exit_code": 0, "elapsed": 1.23, "output": "one\ntwo\nthree\nfour\nfive"})
        text = rendered(record)
        self.assertIn("python -m unittest", text)
        self.assertIn("cwd: tests", text)
        self.assertIn("exit 0", text)
        self.assertIn("1.23s", text)
        self.assertNotIn("one\n", text)
        self.assertIn("three", text)
        self.assertIn("2 行已折叠", text)
        self.assertIn("Ctrl+T", text)

    def test_failed_output_shows_tail_error_exit_and_disclosure(self):
        output = "\n".join(f"failure-{index}" for index in range(8))
        record = ToolRecord("run_command", {"command": "python test.py"}, {
            "ok": False, "exit_code": 2, "output": output, "error": "could not capture"})
        text = rendered(record)
        for line in output.splitlines()[-4:]:
            self.assertIn(line, text)
        self.assertNotIn("failure-0", text)
        self.assertIn("4 行已折叠", text)
        self.assertIn("Ctrl+T", text)
        for line in output.splitlines():
            self.assertIn(line, rendered(record, True))
        self.assertIn("could not capture", text)
        self.assertIn("exit 2", text)
        self.assertTrue(any(style == "class:tool.error" for style, _ in tool_fragments(record)))

    def test_edit_summary_counts_diff_and_names_files(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n-old\n+new\n+extra\n"
        record = ToolRecord("apply_patch", {"patch": diff}, {
            "ok": True, "files": [{"path": "x.py"}], "diff": diff})
        text = rendered(record)
        self.assertIn("x.py", text)
        self.assertIn("+2 −1", text)
        self.assertNotIn("a" * 64, text)
        self.assertNotIn("+extra", text)
        fragments = tool_fragments(record, True)
        self.assertIn(diff.rstrip(), rendered(record, True).replace("    ", ""))
        self.assertIn("Edit x.py", rendered(ToolRecord("apply_patch", {"patch": diff})))
        self.assertIn(("class:diff.add", "    +extra\n"), fragments)
        self.assertIn(("class:diff.remove", "    -old\n"), fragments)

    def test_detailed_view_keeps_long_values_and_review(self):
        long_arg, long_output = "argument" * 1000, "output" * 1000
        record = ToolRecord("run_command", {"command": long_arg}, {
            "ok": True, "output": long_output, "custom": {"nested": "retained"}},
            review="--- a/x\n+++ b/x\n-old\n+new\n")
        text = rendered(record, True)
        self.assertIn(long_arg, text)
        self.assertIn(long_output, text)
        self.assertIn('"nested": "retained"', text)
        self.assertIn("Review", text)
        self.assertIn(("class:diff.add", "    +new\n"), tool_fragments(record, True))

    def test_running_background_and_truncation_are_explicit(self):
        record = ToolRecord("poll_command", {"job_id": "job-1"}, {
            "ok": True, "status": "running", "exit_code": None, "has_more": True,
            "truncated": True, "output": "partial"})
        self.assertIn("job-1", rendered(record))
        self.assertIn("分页", rendered(record))
        self.assertEqual(tool_fragments(record)[0][0], "class:tool.running")
        record.result["output_limit_reached"] = True
        self.assertIn("未保留", rendered(record, True))

    def test_incomplete_scan_and_partial_write_are_visible(self):
        record = ToolRecord("apply_patch", {}, {"ok": False, "partial_write": True,
            "applied_files": ["x.py"], "guidance": "Read before retrying", "diff_truncated": True})
        text = rendered(record)
        self.assertIn("部分文件已写入：x.py", text)
        self.assertIn("Read before retrying", text)
        self.assertIn("diff 已截断", text)
        scan = ToolRecord("search_text", {"query": "word"}, {
            "ok": True, "matches": [], "scan_truncated": True, "skipped_files": 2})
        self.assertIn("扫描上限", rendered(scan))
        self.assertIn("跳过 2", rendered(scan))


if __name__ == "__main__":
    unittest.main()
