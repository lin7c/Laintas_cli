"""The orphan reaper's two gates.

Sub-agent worktrees leaked because teardown is skipped whenever a merge
conflicts, a merge raises, or the CLI is killed. The reaper reclaims them, and
these tests pin the part that makes automatic reclamation safe: it deletes only
when the owning process is provably gone AND the checkout is past its grace
period, and it never discards content without saving a patch first.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest

import worktree_manager


def _git(cwd, *args):
    return subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True)


class WorktreeReaperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "repo")
        os.makedirs(self.root)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.invalid")
        _git(self.root, "config", "user.name", "T")
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("hello\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def _own(self, name, pid, started):
        with open(worktree_manager._owner_path(self.root, name), "w") as fh:
            json.dump({"pid": pid, "started": started}, fh)

    def test_creation_records_the_owning_process(self):
        info = worktree_manager.create_isolated_worktree(self.root, label="probe")
        name = os.path.basename(info.path)
        with open(worktree_manager._owner_path(self.root, name)) as fh:
            self.assertEqual(os.getpid(), json.load(fh)["pid"])

    def test_a_live_owner_is_never_reaped_even_past_the_grace_period(self):
        info = worktree_manager.create_isolated_worktree(self.root, label="live")
        out = worktree_manager.reap_orphan_worktrees(self.root, grace_seconds=0)
        self.assertIn(info.path, out["kept"])
        self.assertNotIn(info.path, out["reaped"])
        self.assertTrue(os.path.isdir(info.path))

    def test_a_dead_owner_inside_the_grace_period_is_kept(self):
        info = worktree_manager.create_isolated_worktree(self.root, label="young")
        self._own(os.path.basename(info.path), 2 ** 22, time.time() - 10)
        out = worktree_manager.reap_orphan_worktrees(self.root, grace_seconds=3600)
        self.assertIn(info.path, out["kept"])
        self.assertTrue(os.path.isdir(info.path))

    def test_a_dead_owner_past_the_grace_period_is_reaped_and_archived(self):
        info = worktree_manager.create_isolated_worktree(self.root, label="orphan")
        name = os.path.basename(info.path)
        with open(os.path.join(info.path, "a.txt"), "w") as fh:
            fh.write("work the sub-agent never merged back\n")
        self._own(name, 2 ** 22, time.time() - 99999)

        out = worktree_manager.reap_orphan_worktrees(self.root, grace_seconds=3600)

        self.assertIn(info.path, out["reaped"])
        self.assertFalse(os.path.isdir(info.path))
        self.assertTrue(out["archived"], "uncommitted work must survive as a patch")
        saved = "".join(open(p).read() for p in out["archived"])
        self.assertIn("work the sub-agent never merged back", saved)
        self.assertFalse(os.path.exists(worktree_manager._owner_path(self.root, name)))
        branches = _git(self.root, "branch", "--format=%(refname:short)").stdout
        self.assertNotIn(f"laintas/{name}", branches)

    def test_a_worktree_with_no_owner_record_falls_back_to_age(self):
        """Worktrees from before ownership existed still have to be reclaimable."""
        info = worktree_manager.create_isolated_worktree(self.root, label="legacy")
        os.remove(worktree_manager._owner_path(self.root, os.path.basename(info.path)))

        fresh = worktree_manager.reap_orphan_worktrees(self.root, grace_seconds=3600)
        self.assertIn(info.path, fresh["kept"])

        old = time.time() - 99999
        os.utime(info.path, (old, old))
        aged = worktree_manager.reap_orphan_worktrees(self.root, grace_seconds=3600)
        self.assertIn(info.path, aged["reaped"])

    def test_normal_teardown_also_clears_the_owner_record(self):
        info = worktree_manager.create_isolated_worktree(self.root, label="clean")
        name = os.path.basename(info.path)
        worktree_manager.remove_worktree(info)
        self.assertFalse(os.path.exists(worktree_manager._owner_path(self.root, name)))

    def test_dry_run_reports_without_deleting(self):
        info = worktree_manager.create_isolated_worktree(self.root, label="dry")
        self._own(os.path.basename(info.path), 2 ** 22, 0)
        out = worktree_manager.reap_orphan_worktrees(self.root, grace_seconds=1,
                                                     dry_run=True)
        self.assertIn(info.path, out["reaped"])
        self.assertTrue(os.path.isdir(info.path))

    def test_a_path_outside_any_repository_is_a_no_op(self):
        out = worktree_manager.reap_orphan_worktrees(self._tmp.name)
        self.assertEqual([], out["reaped"])
        self.assertEqual("", out["error"])

    def test_unknown_pid_states_count_as_alive(self):
        self.assertFalse(worktree_manager._pid_alive(0))
        self.assertFalse(worktree_manager._pid_alive(-1))
        self.assertFalse(worktree_manager._pid_alive("nope"))
        self.assertTrue(worktree_manager._pid_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main()
