"""Tools carry no fixed count or byte limits: results are sized by their
share of the thread budget, continue with an offset, and a command's output
is captured whole rather than read back from a bounded session buffer."""
import os
import re
import tempfile
import unittest
from unittest import mock

import laintas_cli
import tools
from tools import ToolCtx


def _grep(ctx, **params):
    return tools.get_registry().invoke("fs.grep", dict(pattern="needle", path=".", **params), ctx)


class OffsetPagingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for i in range(30):
            with open(os.path.join(self.tmp.name, f"f{i:02d}.txt"), "w") as fh:
                fh.write("".join(f"needle {i}-{j}\n" for j in range(10)))
        self.ctx = ToolCtx(cwd=self.tmp.name, state={})
        patch = mock.patch.object(tools, "_result_budget", lambda name, ctx: 3000)
        patch.start()
        self.addCleanup(patch.stop)

    def _walk_all(self, fetch):
        seen, offset, calls = [], 0, 0
        while True:
            calls += 1
            out = fetch(offset)
            seen += out["result"]
            match = re.search(r"offset=(\d+)", out.get("note") or "")
            if not match:
                return seen, calls
            offset = int(match.group(1))

    def test_grep_pages_every_match_exactly_once(self):
        seen, calls = self._walk_all(lambda off: _grep(self.ctx, offset=off))
        self.assertGreater(calls, 2)                       # it did not fit at once
        keys = [(r["file"], r["line"]) for r in seen]
        self.assertEqual(300, len(keys))
        self.assertEqual(len(keys), len(set(keys)))

    def test_glob_pages_every_file_exactly_once(self):
        glob = lambda off: tools.get_registry().invoke(
            "fs.glob", {"pattern": "*.txt", "offset": off}, self.ctx)
        with mock.patch.object(tools, "_result_budget", lambda name, ctx: 1400):
            seen, calls = self._walk_all(glob)
        self.assertGreater(calls, 1)
        self.assertEqual(sorted(r["path"] for r in seen), sorted(f"f{i:02d}.txt" for i in range(30)))

    def test_an_explicit_max_results_still_narrows(self):
        with mock.patch.object(tools, "_result_budget", lambda name, ctx: 10 ** 7):
            self.assertEqual(5, len(_grep(self.ctx, max_results=5)["result"]))
            self.assertEqual(300, len(_grep(self.ctx)["result"]))   # no 100 default


class NoFixedCountTests(unittest.TestCase):

    def test_ls_is_bounded_by_the_share_not_by_one_hundred_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(300):
                open(os.path.join(tmp, f"n{i:03d}"), "w").close()
            ctx = ToolCtx(cwd=tmp, state={})
            with mock.patch.object(tools, "_result_budget", lambda name, ctx: 10 ** 7):
                self.assertEqual(300, tools.get_registry().invoke("fs.ls", {"path": "."}, ctx)["count"])

    def test_walk_order_is_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("b", "a", "c"):
                os.mkdir(os.path.join(tmp, name))
                open(os.path.join(tmp, name, "x"), "w").close()
            paths = [os.path.relpath(p, tmp) for p, _d in tools._walk_files(tmp, tools._WalkLimit(tmp))]
            self.assertEqual(["a", "b", "c", "a/x", "b/x", "c/x"], paths)   # a level, then its children, by name


class CaptureTests(unittest.TestCase):

    def test_capture_keeps_both_ends_past_the_memory_guard_and_says_so(self):
        cap = laintas_cli.OutputCapture()
        with mock.patch.object(laintas_cli.OutputCapture, "MEMORY_GUARD", 20):
            cap.add("HEAD-" + "x" * 50 + "-TAIL")
        text = cap.text()
        self.assertTrue(text.startswith("HEAD-") and text.endswith("-TAIL"))
        self.assertIn("characters of output omitted", text)

    def test_a_long_command_keeps_its_beginning(self):
        """The session buffer holds only its recent tail; the capture holds all."""
        class Session:
            def __init__(self):
                self.captures, self.raw = [], ""
                self.output_total = 0

            def begin_capture(self):
                cap = laintas_cli.OutputCapture()
                self.captures.append(cap)
                return cap

            def end_capture(self, cap):
                self.captures.remove(cap)

            def send_keys(self, text):
                ids = re.search(r"__LAINTAS_SHELL_BEGIN_([0-9a-f]+)__", text).group(1)
                body = "FIRST LINE\n" + "filler\n" * 200_000 + "LAST LINE\n"
                out = (f"__LAINTAS_SHELL_BEGIN_{ids}__\n{body}"
                       f"__LAINTAS_SHELL_CWD_{ids}__:/tmp\n__LAINTAS_SHELL_END_{ids}__:0\n")
                for cap in self.captures:
                    cap.add(out)
                self.raw = out[-1000:]                     # what a ring buffer keeps
                self.output_total = len(out)

            def read_output(self, timeout=0.1):
                return ""

            def output_from(self, offset):
                return self.raw

        session = Session()
        result = tools._exec_in_deployed_shell("seq", session, 5, None)
        self.assertEqual(0, result["returncode"])
        self.assertTrue(result["result"].startswith("FIRST LINE"))
        self.assertTrue(result["result"].rstrip().endswith("LAST LINE"))
        self.assertEqual([], session.captures)            # released


if __name__ == "__main__":
    unittest.main()
