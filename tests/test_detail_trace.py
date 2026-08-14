import os
import tempfile
import unittest
from pathlib import Path

import detail_trace


class DetailTraceTests(unittest.TestCase):
    def test_conversations_keep_unrecorded_turns_for_stable_numbering(self):
        chat = [
            {"role": "user", "content": "older", "detail_trace": True},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "latest"},
            {"role": "assistant", "content": "not recorded"},
        ]
        turns = detail_trace.conversation_traces(chat)
        self.assertEqual([turn["prompt"] for turn in turns], ["older", "latest"])
        self.assertTrue(turns[0]["enabled"])
        self.assertFalse(turns[1]["enabled"])
        self.assertEqual(turns[1]["items"], [])

    def test_trace_preserves_tool_and_ai_chronology(self):
        chat = [
            {"role": "user", "content": "change it", "detail_trace": True},
            {"role": "assistant", "content": "checking"},
            {"role": "tool", "content": "x", "trace": {"tool": "fs.read"}},
            {"role": "assistant", "content": "finished"},
        ]
        items = detail_trace.conversation_traces(chat)[0]["items"]
        self.assertEqual([item["kind"] for item in items], ["ai", "tool", "ai"])

    def test_full_file_diff_keeps_unchanged_lines_and_marks_changes(self):
        rows = detail_trace.full_file_diff(
            "one\ntwo\nthree\n", "one\nTWO\nthree\nfour\n")
        rendered = "\n".join(row["text"] for row in rows)
        styles = [row["style"] for row in rows]
        self.assertIn("| one", rendered)
        self.assertIn("| three", rendered)
        self.assertIn("- ", rendered)
        self.assertIn("+ ", rendered)
        self.assertIn("delete", styles)
        self.assertIn("add", styles)
        self.assertIn("same", styles)

    def test_new_and_deleted_files_show_every_line_as_changed(self):
        created = detail_trace.full_file_diff(None, "one\ntwo")
        deleted = detail_trace.full_file_diff("one\ntwo", None)
        self.assertEqual([row["style"] for row in created], ["add", "add"])
        self.assertEqual([row["style"] for row in deleted], ["delete", "delete"])

    def test_mutation_capture_keeps_before_and_after_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sample.py"
            target.write_text("before\n", encoding="utf-8")
            before = detail_trace.capture_before(
                "fs.edit", {"path": "sample.py"}, directory)
            target.write_text("after\n", encoding="utf-8")
            trace = detail_trace.build_tool_trace(
                "fs.edit", "Edit", {"path": "sample.py"},
                {"ok": True, "path": str(target), "result": "edited",
                 "diff": "--- a\n+++ b\n-before\n+after\n"},
                "edited", 0.1, directory, before)
        self.assertEqual(trace["before"], "before\n")
        self.assertEqual(trace["after"], "after\n")
        self.assertEqual(trace["path"], os.path.abspath(target))

    def test_search_content_is_rendered_as_results_not_json(self):
        trace = detail_trace.build_tool_trace(
            "fs.grep", "Search", {"pattern": "needle"},
            {"ok": True, "result": [
                {"file": "a.py", "line": 4, "content": "needle = 1"},
                {"file": "b.py", "line": 9, "content": "# needle"},
            ]}, "fallback", 0.01, "/tmp")
        self.assertIn("a.py", trace["content"])
        self.assertIn("     4 | needle = 1", trace["content"])
        self.assertNotIn('"file"', trace["content"])


if __name__ == "__main__":
    unittest.main()
