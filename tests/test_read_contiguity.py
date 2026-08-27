"""A file read must arrive contiguous, and must never overstate what it delivered.

Both properties failed together on 2026-08-27: `runner.py` (511 lines) came back
as ~60 lines carrying `total_lines=511`, and the review written from it opened
with "I have read all four files in full". The contiguity failure is why six
follow-up reads could not reassemble the file; the metadata failure is why the
model did not know it still had holes.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_loop as al  # noqa: E402


def _read_result(n_lines: int, width: int = 60, offset: int = 1) -> dict:
    body = "\n".join(f"{offset + i:>5}→{'x' * width}" for i in range(n_lines))
    return {
        "ok": True, "result": body, "path": "/tmp/sample.py",
        "offset": offset, "lines_returned": n_lines,
        "total_lines": offset + n_lines - 1, "truncated": False,
    }


class ContiguousReadTests(unittest.TestCase):

    def test_read_is_never_cut_in_the_middle(self):
        out = al._format_tool_result_for_loop("fs.read", _read_result(511), 3000)
        self.assertNotIn("middle cut", out)

    def test_kept_lines_are_a_prefix_in_order(self):
        out = al._format_tool_result_for_loop("fs.read", _read_result(511), 3000)
        nums = [int(ln.split("→")[0]) for ln in out.splitlines()
                if "→" in ln]
        self.assertTrue(nums, "no numbered lines survived")
        self.assertEqual(nums, list(range(nums[0], nums[0] + len(nums))),
                         "delivered lines are not one contiguous run")
        self.assertEqual(nums[0], 1, "a read must start at its own offset")

    def test_footer_counts_what_was_delivered_not_what_was_found(self):
        out = al._format_tool_result_for_loop("fs.read", _read_result(511), 3000)
        delivered = sum(1 for ln in out.splitlines() if "→" in ln)
        self.assertIn(f"lines 1-{delivered}", out,
                      "footer must name the window actually delivered")
        self.assertIn("of 511", out)

    def test_a_cut_read_names_the_offset_that_resumes_it(self):
        out = al._format_tool_result_for_loop("fs.read", _read_result(511), 3000)
        delivered = sum(1 for ln in out.splitlines() if "→" in ln)
        self.assertIn(f"offset={delivered + 1}", out)

    def test_resuming_at_that_offset_yields_the_next_line(self):
        first = al._format_tool_result_for_loop("fs.read", _read_result(511), 3000)
        delivered = sum(1 for ln in first.splitlines() if "→" in ln)
        second = al._format_tool_result_for_loop(
            "fs.read", _read_result(511 - delivered, offset=delivered + 1), 3000)
        nums = [int(ln.split("→")[0]) for ln in second.splitlines()
                if "→" in ln]
        self.assertEqual(nums[0], delivered + 1,
                         "the advertised offset did not resume where the first cut stopped")

    def test_a_read_that_fits_is_returned_whole_and_says_so(self):
        out = al._format_tool_result_for_loop("fs.read", _read_result(40), 3000)
        self.assertEqual(sum(1 for ln in out.splitlines() if "→" in ln), 40)
        self.assertNotIn("NOT shown", out)

    def test_a_single_line_longer_than_the_budget_still_returns_content(self):
        res = {"ok": True, "result": "y" * 50_000, "path": "/tmp/min.js",
               "offset": 1, "lines_returned": 1, "total_lines": 1}
        out = al._format_tool_result_for_loop("fs.read", res, 3000)
        self.assertTrue(out.startswith("y"))
        self.assertIn("no lines fit", out)


class BudgetTests(unittest.TestCase):

    def test_read_gets_a_larger_budget_than_shell(self):
        self.assertGreater(al._tool_output_budget("fs.read", 3000),
                           al._tool_output_budget("shell.exec", 3000))

    def test_shell_keeps_the_base_budget(self):
        self.assertEqual(al._tool_output_budget("shell.exec", 3000), 3000)

    def test_budgets_scale_with_the_user_knob(self):
        self.assertEqual(al._tool_output_budget("fs.read", 6000),
                         2 * al._tool_output_budget("fs.read", 3000))

    def test_both_tool_taxonomies_get_the_same_budget(self):
        self.assertEqual(al._tool_output_budget("fs.read", 3000),
                         al._tool_output_budget("read", 3000))


class ShellStillCutsItsMiddleTests(unittest.TestCase):
    """The middle cut was right for unbounded output; only read was wrong."""

    def test_shell_output_keeps_both_ends(self):
        body = "\n".join(f"line {i}" for i in range(4000))
        out = al._format_tool_result_for_loop(
            "shell.exec", {"ok": True, "result": body, "returncode": 0}, 3000)
        self.assertIn("middle cut", out)
        self.assertIn("line 0", out)
        self.assertIn("line 3999", out)


if __name__ == "__main__":
    unittest.main()
