"""The orphan reaper's two gates.

Sub-agent worktrees leaked because teardown is skipped whenever a merge
conflicts, a merge raises, or the CLI is killed. The reaper reclaims them, and
these tests pin the part that makes automatic reclamation safe: it deletes only
when the owning process is provably gone AND the checkout is past its grace
period, and it never discards content without saving a patch first.
"""
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

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


class OwnerIdentityTests(unittest.TestCase):
    """A pid alone does not identify the process that claimed a worktree.

    Three records in one deployment carried ``"pid": 2`` — kthreadd, a kernel
    thread that never exits — so those checkouts were immortal: the first gate
    reported the owner alive forever and the age gate was never consulted.
    """

    def test_a_kernel_thread_is_never_a_live_owner(self):
        # pid 2 is kthreadd on Linux: alive, but it owns nothing.
        self.assertTrue(worktree_manager._pid_alive(2))
        self.assertFalse(worktree_manager._owner_alive({"pid": 2}))

    def test_pid_0_and_1_are_never_owners(self):
        self.assertFalse(worktree_manager._owner_alive({"pid": 1}))
        self.assertFalse(worktree_manager._owner_alive({"pid": 0}))

    def test_this_process_is_alive_under_a_full_record(self):
        me = os.getpid()
        self.assertTrue(worktree_manager._owner_alive({
            "pid": me,
            "boot_id": worktree_manager._boot_id(),
            "starttime": worktree_manager._pid_starttime(me)}))

    def test_a_recycled_pid_does_not_inherit_the_claim(self):
        me = os.getpid()
        self.assertFalse(worktree_manager._owner_alive({
            "pid": me,
            "boot_id": worktree_manager._boot_id(),
            "starttime": "0"}))       # same pid, different process start

    def test_a_record_from_a_previous_boot_is_dead(self):
        me = os.getpid()
        self.assertFalse(worktree_manager._owner_alive({
            "pid": me,
            "boot_id": "not-this-boot",
            "starttime": worktree_manager._pid_starttime(me)}))

    def test_legacy_records_still_protect_a_live_cli(self):
        # No boot_id/starttime (older build) falls back to liveness, which
        # must still keep a running sub-agent's checkout.
        self.assertTrue(worktree_manager._owner_alive({"pid": os.getpid()}))

    def test_creation_records_boot_and_start_identity(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "repo")
        os.makedirs(root)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.invalid")
        _git(root, "config", "user.name", "T")
        with open(os.path.join(root, "a.txt"), "w") as fh:
            fh.write("hello\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "init")

        info = worktree_manager.create_isolated_worktree(root, label="probe")
        self.addCleanup(worktree_manager.remove_worktree, info)
        with open(worktree_manager._owner_path(
                root, os.path.basename(info.path))) as fh:
            owner = json.load(fh)
        self.assertEqual(os.getpid(), owner["pid"])
        self.assertEqual(worktree_manager._boot_id(), owner["boot_id"])
        self.assertEqual(worktree_manager._pid_starttime(os.getpid()),
                         owner["starttime"])


class SweepScopeTests(unittest.TestCase):
    """The sweep must cover where worktrees are, not where the shell was.

    ``reap_orphan_worktrees(os.getcwd())`` resolved no repo root when the CLI
    was launched from a plain directory, so it returned before looking at
    anything and reported success. Orphans piled up in a repo one level down
    until they held 3.6 GB.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        # paths.LAINTAS_HOME is resolved once at import time, so setting the
        # environment variable here would silently write the registry into the
        # real home whenever another test imported paths first — and this test
        # would then see the developer's own repository in the results.
        import paths
        patcher = mock.patch.object(paths, "LAINTAS_HOME",
                                    pathlib.Path(self._home.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = os.path.join(self._tmp.name, "repo")
        self.outside = os.path.join(self._tmp.name, "not-a-repo")
        os.makedirs(self.root)
        os.makedirs(self.outside)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.invalid")
        _git(self.root, "config", "user.name", "T")
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("hello\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def tearDown(self):
        self._tmp.cleanup()
        self._home.cleanup()

    def _orphan(self, label="orphan"):
        info = worktree_manager.create_isolated_worktree(self.root, label=label)
        name = os.path.basename(info.path)
        with open(worktree_manager._owner_path(self.root, name), "w") as fh:
            json.dump({"pid": 2 ** 22 - 1, "started": time.time() - 86400,
                       "boot_id": worktree_manager._boot_id(),
                       "starttime": "1"}, fh)
        return info

    def test_creating_a_worktree_registers_its_repo(self):
        info = self._orphan()
        self.addCleanup(worktree_manager.remove_worktree, info)
        self.assertIn(os.path.realpath(self.root),
                      [os.path.realpath(r)
                       for r in worktree_manager.known_worktree_roots()])

    def test_orphans_are_reaped_when_launched_outside_any_repo(self):
        info = self._orphan()
        # The old cwd-scoped sweep sees nothing from here...
        self.assertEqual(
            [], worktree_manager.reap_orphan_worktrees(self.outside)["reaped"])
        # ...while the registry-scoped sweep still finds the orphan.
        out = worktree_manager.reap_all_worktree_roots(self.outside)
        self.assertIn(info.path, out["reaped"])
        self.assertFalse(os.path.isdir(info.path))

    def test_an_unregistered_cwd_repo_is_still_swept(self):
        # Worktrees created by a build that predates the registry.
        info = self._orphan()
        worktree_manager._forget_root(self.root)
        out = worktree_manager.reap_all_worktree_roots(self.root)
        self.assertIn(info.path, out["reaped"])

    def test_a_deleted_repo_is_dropped_from_the_registry(self):
        info = self._orphan()
        worktree_manager.remove_worktree(info)
        import shutil as _shutil
        _shutil.rmtree(os.path.join(self.root, ".laintas", "worktrees"))
        worktree_manager.reap_all_worktree_roots(None)
        self.assertEqual([], worktree_manager.known_worktree_roots())

    def test_sweep_reports_roots_it_covered(self):
        info = self._orphan()
        self.addCleanup(worktree_manager.remove_worktree, info)
        out = worktree_manager.reap_all_worktree_roots(self.outside, dry_run=True)
        self.assertIn(os.path.realpath(self.root),
                      [os.path.realpath(r) for r in out["roots"]])
        self.assertEqual("", out["error"])


if __name__ == "__main__":
    unittest.main()
