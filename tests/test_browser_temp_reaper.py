"""Orphaned browser profile collection.

`BrowserSession.stop()` removes the Chrome profile and log dirs it created, but
it only runs on a graceful exit. A SIGKILL, an OOM kill or a dropped SSH
session leaves the whole profile behind — a few hundred MB each, and nothing
else ever collects them. `_reap_stale_temp_dirs` sweeps them at session start;
these tests pin the two properties that make that safe to do unattended.
"""
import os
import time
import unittest
from unittest import mock

import browser_session


class ReapStaleTempDirs(unittest.TestCase):
    def setUp(self):
        import tempfile as _t
        # Create the sandbox BEFORE patching: browser_session shares the one
        # tempfile module with everything else, so a live patch would send
        # mkdtemp itself through the mock.
        self.root = _t.mkdtemp(prefix="reaper-test-")
        self.addCleanup(browser_session.shutil.rmtree, self.root, True)
        self.tmp = self.enterContext(mock.patch.object(
            browser_session.tempfile, "gettempdir", return_value=self.root))

    def _mk(self, name, age_hours=0.0):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        if age_hours:
            old = time.time() - age_hours * 3600
            os.utime(path, (old, old))
        return path

    def test_removes_only_aged_session_dirs(self):
        stale_profile = self._mk("hwo-chrome-103-aaaa", age_hours=48)
        stale_logs = self._mk("hwo-vnc-sess-bbbb", age_hours=48)
        live_profile = self._mk("hwo-chrome-104-cccc")

        self.assertEqual(browser_session._reap_stale_temp_dirs(), 2)
        self.assertFalse(os.path.exists(stale_profile))
        self.assertFalse(os.path.exists(stale_logs))
        # A running Chrome writes into its profile constantly, so recent mtime
        # is the liveness test — reaping a live session's profile would kill
        # the browser out from under whoever is driving it.
        self.assertTrue(os.path.exists(live_profile))

    def test_leaves_foreign_directories_alone(self):
        # /tmp on a working box holds other tools' state — some of it
        # credentials, some of it much older than a day. Age alone must never
        # be enough to delete something; the prefix is what makes it ours.
        foreign = self._mk("helpwo-codex-home", age_hours=24 * 30)
        secret = os.path.join(foreign, "auth.json")
        with open(secret, "w") as fh:
            fh.write("token")

        self.assertEqual(browser_session._reap_stale_temp_dirs(), 0)
        self.assertTrue(os.path.exists(secret))

    def test_never_raises_when_the_sweep_fails(self):
        # It runs on the browser-start path. A permission error or a vanished
        # tempdir must degrade to "collected nothing", never to a session that
        # refuses to start.
        self.tmp.return_value = os.path.join(self.root, "does-not-exist")
        self.assertEqual(browser_session._reap_stale_temp_dirs(), 0)


class ReapStaleDisplays(unittest.TestCase):
    """Orphaned Xvfb/x11vnc stacks and the display numbers they strand.

    Measured on the dev box: five stacks survived 7.8 days after their sessions
    died, holding :103-:107 plus RFB 5900-5904, and four older lock files had
    already permanently retired :99-:102 from the pool.
    """

    def setUp(self):
        import tempfile as _t
        self.root = _t.mkdtemp(prefix="display-test-")
        self.addCleanup(browser_session.shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, ".X11-unix"), exist_ok=True)
        self.enterContext(mock.patch.object(browser_session, "_X_LOCK_DIR", self.root))
        self.enterContext(mock.patch.object(
            browser_session.tempfile, "gettempdir", return_value=self.root))
        self.killed = []
        self.enterContext(mock.patch.object(
            browser_session, "_kill_display_stack",
            side_effect=lambda n, pid: self.killed.append((n, pid))))

    def _lock(self, n, pid, age_hours=2.0):
        path = os.path.join(self.root, f".X{n}-lock")
        with open(path, "w") as fh:
            fh.write(f"{pid:>10}\n")
        open(os.path.join(self.root, ".X11-unix", f"X{n}"), "w").close()
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
        return path

    def _reap(self):
        return browser_session._reap_stale_displays(start=99, end=110)

    def test_drops_lock_whose_holder_is_gone(self):
        lock = self._lock(99, 4242)
        with mock.patch.object(browser_session, "_proc_stat", return_value=None):
            self.assertEqual(self._reap(), 1)
        self.assertFalse(os.path.exists(lock))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".X11-unix", "X99")))
        self.assertEqual(self.killed, [])       # nothing to kill

    def test_kills_orphaned_xvfb_and_frees_the_number(self):
        lock = self._lock(103, 5150)
        with mock.patch.object(browser_session, "_proc_stat", return_value=(1, "Xvfb")):
            self.assertEqual(self._reap(), 1)
        self.assertEqual(self.killed, [(103, 5150)])
        self.assertFalse(os.path.exists(lock))

    def test_spares_a_session_whose_owner_is_alive(self):
        # ppid != 1 means the CLI that spawned it is still running. This is the
        # false positive that would kill a browser out from under its driver.
        lock = self._lock(104, 5151)
        with mock.patch.object(browser_session, "_proc_stat", return_value=(9001, "Xvfb")):
            self.assertEqual(self._reap(), 0)
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(lock))

    def test_spares_a_young_orphan(self):
        # A session mid-startup is briefly reparented-looking; age is the guard.
        lock = self._lock(105, 5152, age_hours=0.1)
        with mock.patch.object(browser_session, "_proc_stat", return_value=(1, "Xvfb")):
            self.assertEqual(self._reap(), 0)
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(lock))

    def test_spares_an_orphan_that_still_has_its_profile(self):
        # A surviving profile means the reaper's view is incomplete; leave it
        # for the dir sweep to age out first rather than guess.
        lock = self._lock(106, 5153)
        os.makedirs(os.path.join(self.root, "hwo-chrome-106-keepme"))
        with mock.patch.object(browser_session, "_proc_stat", return_value=(1, "Xvfb")):
            self.assertEqual(self._reap(), 0)
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(lock))

    def test_spares_a_lock_held_by_something_that_is_not_ours(self):
        lock = self._lock(107, 5154)
        with mock.patch.object(browser_session, "_proc_stat", return_value=(1, "sshd")):
            self.assertEqual(self._reap(), 0)
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(lock))

    def test_freed_number_becomes_allocatable_again(self):
        self._lock(99, 4242)
        with mock.patch.object(browser_session, "_proc_stat", return_value=None):
            self._reap()
        self.assertEqual(browser_session._free_display(start=99, end=110), 99)


if __name__ == "__main__":
    unittest.main()
