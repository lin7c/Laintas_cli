"""Browser stacks whose owning CLI died.

Chrome, Xvfb and x11vnc are started in their own sessions, so no kernel
mechanism ends them with the CLI: any exit that skips close() (SIGKILL, OOM,
os._exit, a test runner timing out) left the whole stack running. Measured on
the dev box: eleven stacks, 141 processes, ~1.4 GB RSS plus ~2 GB swap, two
days after their CLIs were gone. The age-based temp reaper cannot see them —
a live Chrome keeps its profile fresh.

These run real processes standing in for Chrome: the reaper finds them by the
profile path in their argv, exactly as it finds Chrome.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import browser_session


def _spawn_holder(profile):
    # Extra argv is ignored by `python -c`, but visible in /proc/<pid>/cmdline.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)",
         f"--user-data-dir={profile}"])


class ReapOrphanedBrowsers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orphan-reaper-test-")
        self.addCleanup(browser_session.shutil.rmtree, self.root, True)
        self.enterContext(mock.patch.object(
            browser_session.tempfile, "gettempdir", return_value=self.root))
        # Never let a test reach the host's real X displays.
        self.enterContext(mock.patch.object(
            browser_session, "_reap_display_of_dead_owner"))

    def _profile(self, name, owner):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        if owner is not None:
            with open(os.path.join(path, browser_session._OWNER_FILE), "w") as fh:
                json.dump(owner, fh)
        return path

    def _holder(self, profile):
        proc = _spawn_holder(profile)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        deadline = time.time() + 5
        while time.time() < deadline and not browser_session._procs_using(profile):
            time.sleep(0.05)
        return proc

    def _dead_owner(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        start = browser_session._proc_start_ticks(gone.pid)
        gone.wait()
        return {"pid": gone.pid, "start": start}

    def test_kills_stack_and_profile_of_a_dead_owner(self):
        profile = self._profile("hwo-chrome-150-dead", self._dead_owner())
        chrome = self._holder(profile)

        self.assertEqual(browser_session.reap_orphaned_browsers(), 1)
        chrome.wait(timeout=5)
        self.assertFalse(os.path.exists(profile))

    def test_leaves_a_live_owners_stack_alone(self):
        me = os.getpid()
        profile = self._profile(
            "hwo-chrome-151-live",
            {"pid": me, "start": browser_session._proc_start_ticks(me)})
        chrome = self._holder(profile)

        self.assertEqual(browser_session.reap_orphaned_browsers(), 0)
        self.assertIsNone(chrome.poll())
        self.assertTrue(os.path.exists(profile))

    def test_reused_pid_does_not_count_as_the_owner(self):
        # The owner's pid now belongs to an unrelated process (ours): the start
        # time is what tells them apart.
        me = os.getpid()
        profile = self._profile(
            "hwo-chrome-152-reused",
            {"pid": me, "start": browser_session._proc_start_ticks(me) - 1})
        chrome = self._holder(profile)

        self.assertEqual(browser_session.reap_orphaned_browsers(), 1)
        chrome.wait(timeout=5)

    def test_detached_stack_survives_its_creator(self):
        # remote_browser's `start` exits by design; `stop` owns the teardown.
        profile = self._profile("hwo-chrome-153-rb", {"detached": True})
        chrome = self._holder(profile)

        self.assertEqual(browser_session.reap_orphaned_browsers(), 0)
        self.assertIsNone(chrome.poll())

    def test_legacy_profile_without_owner_needs_init_parent(self):
        # Written by a CLI from before the owner file: our holder is still our
        # child (ppid != 1), i.e. its creator is alive, so it must stay.
        profile = self._profile("hwo-chrome-154-legacy", None)
        chrome = self._holder(profile)

        self.assertEqual(browser_session.reap_orphaned_browsers(), 0)
        self.assertIsNone(chrome.poll())

    def test_foreign_temp_dirs_are_not_considered(self):
        foreign = self._profile("helpwo-codex-home", self._dead_owner())
        holder = self._holder(foreign)

        self.assertEqual(browser_session.reap_orphaned_browsers(), 0)
        self.assertIsNone(holder.poll())
        self.assertTrue(os.path.exists(foreign))


class HardExitTeardown(unittest.TestCase):
    def test_kill_all_browser_hosts_skips_playwright(self):
        sess = mock.Mock()
        with browser_session._browser_lock:
            browser_session._browser_sessions["t"] = sess
        browser_session.kill_all_browser_hosts()
        sess.kill_host_stack.assert_called_once_with()
        sess.close.assert_not_called()
        self.assertNotIn("t", browser_session._browser_sessions)

    def test_kill_all_browser_hosts_does_not_wait_for_the_registry_lock(self):
        # register/unregister hold _browser_lock across session.close(), which
        # joins the Playwright worker. The watchdog and AgentRegistry._die call
        # this cross-thread precisely when the main thread is stuck in there,
        # so blocking here delays os._exit for as long as the teardown hangs.
        sess = mock.Mock()
        with browser_session._browser_lock:
            browser_session._browser_sessions["t"] = sess
        held = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def holder():
            with browser_session._browser_lock:
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            started = time.monotonic()
            browser_session.kill_all_browser_hosts()
            self.assertLess(time.monotonic() - started, 1.0)
            sess.kill_host_stack.assert_called_once_with()
        finally:
            release.set()
            thread.join(5)
            with browser_session._browser_lock:
                browser_session._browser_sessions.pop("t", None)


if __name__ == "__main__":
    unittest.main()
