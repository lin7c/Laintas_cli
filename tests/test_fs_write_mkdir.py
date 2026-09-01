"""fs.write creates its parent directories (mkdir -p), and only after the gates.

Helpwo's write has done this since fs/ensureParentDirs.ts, so the same model
running the same prompt would write "docs/notes.md" into a fresh tree
successfully in Helpwo and get a bare ENOENT here. Three such failures are in
the retained sessions, one of them the standard "new skill folder + SKILL.md"
shape.

The ordering half matters as much as the feature: directories must be created
only after policy, contract scope and the CAS check have cleared the path, so
a refused write leaves nothing behind on disk.
"""
import os
import shutil
import tempfile
import types
import unittest

import tools


def _ctx(cwd, approve=True):
    deps = types.SimpleNamespace(
        request_file_write_approval=lambda path, diff, reason: approve)
    return tools.ToolCtx(cwd=cwd, deps=deps)


class FsWriteMkdirTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fswrite-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, path, content="hello\n", approve=True):
        return tools._bi_fs_write({"path": path, "content": content},
                                  _ctx(self.root, approve))

    def test_creates_missing_parents_and_reports_them(self):
        r = self.write("a/b/c/x.md")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a/b/c/x.md")))
        self.assertEqual([os.path.relpath(d, self.root) for d in r["created_dirs"]],
                         ["a", "a/b", "a/b/c"])
        # The invented directories are named in the human-readable result too —
        # a typo'd path now succeeds, so it must not succeed silently.
        self.assertIn("[created dir: a, a/b, a/b/c]", r["result"])

    def test_existing_parent_creates_nothing(self):
        self.write("a/b/c/x.md")
        r = self.write("a/b/c/y.md")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["created_dirs"], [])
        self.assertNotIn("created dir", r["result"])

    def test_overwrite_is_untouched(self):
        self.write("a/b/x.md")
        r = self.write("a/b/x.md", "bye\n")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertTrue(r["result"].startswith("updated "))
        self.assertEqual(r["created_dirs"], [])

    def test_bare_filename_creates_nothing(self):
        r = self.write("top.txt")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["created_dirs"], [])

    def test_absolute_path_gets_parents_too(self):
        r = self.write(os.path.join(self.root, "abs/deep/z.txt"))
        self.assertTrue(r["ok"], r.get("error"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "abs/deep/z.txt")))

    def test_file_in_the_parent_chain_fails_cleanly(self):
        self.write("top.txt")
        r = self.write("top.txt/nope.md")
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "top.txt/nope.md")))

    def test_dangling_symlink_parent_fails_cleanly(self):
        os.symlink(os.path.join(self.root, "gone"),
                   os.path.join(self.root, "dangling"))
        r = self.write("dangling/q.txt")
        self.assertFalse(r["ok"])
        self.assertIn("could not create parent directory", r["error"])

    def test_blocked_write_creates_no_directories(self):
        # Stubbed rather than driven through real policy: whether this machine's
        # policy.json is in enforce or allow mode must not decide whether the
        # ordering guarantee is tested.
        blocked = {"ok": False, "error": "Blocked by policy: test"}
        original = tools._check_file_write_policy
        tools._check_file_write_policy = lambda *a, **kw: blocked
        try:
            r = self.write("should/not/exist/x.md")
        finally:
            tools._check_file_write_policy = original
        self.assertFalse(r["ok"])
        self.assertEqual(os.listdir(self.root), [])


if __name__ == "__main__":
    unittest.main()
