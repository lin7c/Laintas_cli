"""A search must fail fast instead of walking a drive.

The bug this locks out: `fs.glob {"pattern": "**/laintas-cli", "path": "/mnt/f"}`
never returned. `glob.glob(recursive=True)` collects every match before the
caller sees the first one, cannot prune a directory, and has no depth, count
or time limit -- so on a 9p-mounted Windows drive holding a pnpm store it
walked until the session died, with the whole path set in memory on the way.

Three properties keep that from recurring, and each covers a case the others
miss: prune before descending, stop at a budget, and say so when the answer is
partial.
"""
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import tools
from tools import ToolCtx


def _tree(root: Path, *, depth: int, width: int, prune_dir: str = "") -> None:
    """A deep tree, optionally with one branch that must never be entered."""
    if prune_dir:
        victim = root / prune_dir
        victim.mkdir(parents=True, exist_ok=True)
        for index in range(width):
            (victim / f"junk{index}.txt").write_text("x", encoding="utf-8")
    current = root
    for level in range(depth):
        current = current / f"level{level}"
        current.mkdir(parents=True, exist_ok=True)
        (current / f"file{level}.txt").write_text("needle", encoding="utf-8")


class BoundedWalkTests(unittest.TestCase):
    def test_a_pruned_directory_is_never_descended_into(self):
        """Filtering results still pays the cost of the walk it filters."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, depth=2, width=50, prune_dir="node_modules")
            limit = tools._WalkLimit(str(root))
            paths = [p for p, _ in tools._walk_files(str(root), limit)]
            self.assertFalse([p for p in paths if "node_modules" in p])
            # 50 junk files never counted against the budget.
            self.assertLess(limit.entries, 20)

    def test_the_walk_stops_at_its_entry_budget(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, depth=6, width=0)
            limit = tools._WalkLimit(str(root))
            limit.budget = 4
            list(tools._walk_files(str(root), limit))
            self.assertEqual("entries", limit.reason)

    def test_the_walk_stops_at_its_deadline(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, depth=6, width=0)
            limit = tools._WalkLimit(str(root))
            limit.deadline = time.monotonic() - 1
            list(tools._walk_files(str(root), limit))
            self.assertEqual("deadline", limit.reason)

    def test_depth_is_bounded_even_with_room_in_every_other_budget(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _tree(root, depth=6, width=0)
            limit = tools._WalkLimit(str(root))
            found = [p for p, is_dir in tools._walk_files(str(root), limit, max_depth=2)
                     if not is_dir]
            self.assertEqual(2, len(found))

    def test_a_symlink_loop_terminates(self):
        """Where an unbounded walk stops being slow and starts being infinite."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "loop").symlink_to(root, target_is_directory=True)
            limit = tools._WalkLimit(str(root))
            paths = list(tools._walk_files(str(root), limit))
            self.assertLess(len(paths), 10)
            self.assertEqual("", limit.reason)

    def test_the_bound_names_no_filesystem(self):
        """A wall clock measures a slow mount without being told about it.

        The first version of this tightened the budget when the path looked
        like `/mnt/<drive>`. That is one instance of "this filesystem is slow"
        and the list of instances has no end -- sshfs, NFS, SMB, fuse, a
        network drive, an overloaded machine. Every one of them spends the
        same deadline, so there is no list to keep current.
        """
        # Behaviour, not text: the incident that produced this code is still
        # described in a comment, and naming it there is a record of why the
        # bound exists, not a branch on where the files are.
        self.assertFalse(hasattr(tools, "_is_slow_root"))
        self.assertFalse(hasattr(tools, "_fs_walk_budget"))
        budgets = {
            (tools._WalkLimit(root).budget, tools._WalkLimit(root).seconds)
            for root in ("/mnt/f", "/mnt/c/Users/me", "/home/me", "/net/share",
                         "/Volumes/backup", ".")
        }
        self.assertEqual(1, len(budgets),
                         "one budget for every filesystem, known or not")


class GlobSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "deep").mkdir()
        (self.root / "top.py").write_text("needle", encoding="utf-8")
        (self.root / "pkg" / "mid.py").write_text("needle", encoding="utf-8")
        (self.root / "pkg" / "deep" / "low.py").write_text("needle", encoding="utf-8")
        self.ctx = ToolCtx(cwd=str(self.root), state={})

    def _glob(self, pattern, **extra):
        result = tools.get_registry().invoke(
            "fs.glob", {"pattern": pattern, "path": ".", **extra}, self.ctx)
        return sorted(item["path"] for item in result["result"]
                      if item["type"] == "file"), result

    def test_a_star_does_not_cross_a_separator(self):
        """`fnmatch` alone gets this wrong, which is why the pattern is
        translated rather than handed to it."""
        paths, _ = self._glob("*.py")
        self.assertEqual(["top.py"], paths)

    def test_double_star_matches_at_any_depth(self):
        paths, _ = self._glob("**/*.py")
        self.assertEqual(["pkg/deep/low.py", "pkg/mid.py", "top.py"], paths)

    def test_a_literal_prefix_roots_the_walk(self):
        """`pkg/**/x` cannot match outside `pkg/`, so nothing else is read."""
        _, deep = self._glob("pkg/**/*.py")
        _, wide = self._glob("**/*.py")
        self.assertLess(deep["entries_scanned"], wide["entries_scanned"])

    def test_a_shallow_pattern_does_not_walk_deep(self):
        with mock.patch.object(tools, "_FS_MAX_DEPTH", 12):
            _, shallow = self._glob("*.py")
        self.assertLessEqual(shallow["entries_scanned"], 3)

    def test_an_incomplete_search_says_so(self):
        """"No matches" and "gave up before reaching them" are the same empty
        list, and must not read the same way."""
        with mock.patch.object(tools, "_FS_ENTRY_BUDGET", 1):
            paths, result = self._glob("**/nothing-here")
        self.assertEqual([], paths)
        self.assertTrue(result["truncated"])
        self.assertIn("did not cover the whole tree", result["incomplete"])


class GrepStreamingTests(unittest.TestCase):
    def test_grep_stops_walking_once_it_has_enough_hits(self):
        """It used to collect and sort every path on the drive before opening
        the first file, so `max_results` could not prevent any of the work."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(40):
                branch = root / f"b{index}"
                branch.mkdir()
                (branch / "hit.txt").write_text("needle\n", encoding="utf-8")
            ctx = ToolCtx(cwd=str(root), state={})
            result = tools.get_registry().invoke(
                "fs.grep", {"pattern": "needle", "path": ".",
                            "include": "**/*.txt", "max_results": 3}, ctx)
        self.assertEqual(3, result["matches"])
        self.assertTrue(result["truncated"])
        self.assertLess(result["files_scanned"], 40)

    def test_grep_reports_a_budget_stop(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("needle\n", encoding="utf-8")
            ctx = ToolCtx(cwd=str(root), state={})
            with mock.patch.object(tools, "_FS_ENTRY_BUDGET", 0):
                result = tools.get_registry().invoke(
                    "fs.grep", {"pattern": "needle", "path": ".",
                                "include": "**/*.txt"}, ctx)
        self.assertTrue(result["truncated"])
        self.assertIn("incomplete", result)


if __name__ == "__main__":
    unittest.main()
