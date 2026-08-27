"""fs.read must window by line, not by a byte prefix.

The old implementation read the first `max_bytes` of the file and then sliced
that prefix by line number. On any file larger than the cap every offset past
the prefix selected nothing: the caller got an empty body and a total_lines
borrowed from the prefix, which looks exactly like "your offset is out of
range". Measured on 2026-08-26: six of eleven reads in one turn came back
blank that way, each costing a full agent loop.
"""
import os
import tempfile
import unittest

import tools


def _write_lines(path, count, width=200):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, count + 1):
            f.write(f"line{i:06d}" + "x" * width + "\n")


class FsReadWindowingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "big.txt")
        # ~2MB — an order of magnitude past the 200KB default cap.
        _write_lines(self.path, 10_000)
        self.ctx = tools.ToolCtx(cwd=self.tmp.name)

    def read(self, **params):
        params.setdefault("path", self.path)
        return tools._bi_fs_read(params, self.ctx)

    def test_offset_far_past_the_byte_cap_still_returns_content(self):
        r = self.read(offset=9_000, limit=10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lines_returned"], 10)
        self.assertIn("line009000", r["result"])
        self.assertIn("line009009", r["result"])

    def test_total_lines_counts_the_file_not_the_prefix(self):
        self.assertEqual(self.read(offset=1, limit=1)["total_lines"], 10_000)
        self.assertEqual(self.read(offset=9_999, limit=5)["total_lines"], 10_000)

    def test_line_numbers_match_real_file_positions(self):
        first = self.read(offset=5_000, limit=1)["result"].split("→")[0]
        self.assertEqual(first.strip(), "5000")

    def test_offset_past_eof_is_an_error_not_an_empty_body(self):
        r = self.read(offset=99_999, limit=10)
        self.assertFalse(r["ok"])
        self.assertIn("past end of file", r["error"])
        self.assertEqual(r["total_lines"], 10_000)

    def test_byte_cap_still_bounds_the_payload(self):
        r = self.read(offset=1, limit=2_000, max_bytes=1_000)
        self.assertTrue(r["ok"])
        self.assertTrue(r["byte_truncated"])
        self.assertLessEqual(len(r["result"]), 1_200)
        self.assertGreater(r["lines_returned"], 0)

    def test_small_file_round_trip(self):
        small = os.path.join(self.tmp.name, "small.txt")
        _write_lines(small, 3, width=1)
        r = tools._bi_fs_read({"path": small}, self.ctx)
        self.assertEqual(r["lines_returned"], 3)
        self.assertEqual(r["total_lines"], 3)
        self.assertFalse(r["truncated"])

    def test_huge_file_skips_the_line_count_but_still_reads(self):
        # Counting to EOF is skipped past _COUNT_LINES_MAX_BYTES; the window
        # must still come back, with total_lines reported as unknown.
        import unittest.mock as mock
        with mock.patch.object(tools, "_COUNT_LINES_MAX_BYTES", 1_000):
            r = self.read(offset=10, limit=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["lines_returned"], 5)
        self.assertIsNone(r["total_lines"])


if __name__ == "__main__":
    unittest.main()
