"""Child process groups must not outlive the CLI that started them.

The kernel never ends a process's children when it dies; a child in its own
session on pipes (shell.exec, local/P2P exec, hosted apps) has no terminal to
hang up on it either. These tests run real process groups — a shell leader
with a forked, silent grandchild, the shape that SIGPIPE can never reach — and
check both halves of the registry: kill_all() for exits the CLI survives long
enough to run, and reap_dead_owners() for the ones it does not (SIGKILL, OOM).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import child_registry


def _spawn_group():
    # `sh` forks `sleep` instead of exec-ing it (the trailing `wait`), so the
    # group has a leader and a descendant — what a real command looks like.
    return subprocess.Popen(["sh", "-c", "sleep 60 & wait"],
                            stdout=subprocess.DEVNULL, start_new_session=True)


def _wait_members(pgid, start, n, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(child_registry._members(pgid, start)) >= n:
            return
        time.sleep(0.02)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(prefix="child-registry-test-"))
        self.addCleanup(__import__("shutil").rmtree, self.run_dir, True)
        self.enterContext(mock.patch.object(child_registry, "RUN_DIR", self.run_dir))
        self.enterContext(mock.patch.object(child_registry, "_groups", {}))

    def _group(self):
        proc = _spawn_group()
        start = child_registry.start_ticks(proc.pid)
        _wait_members(proc.pid, start, 2)

        def _cleanup():
            for pid in child_registry._members(proc.pid, start):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            proc.wait()
        self.addCleanup(_cleanup)
        return proc, start

    def test_kill_all_ends_leader_and_descendants(self):
        proc, start = self._group()
        child_registry.register(proc.pid, "shell")
        self.assertTrue(child_registry._ledger_path().exists())

        self.assertEqual(child_registry.kill_all(), 1)
        proc.wait(timeout=5)
        self.assertEqual(child_registry._members(proc.pid, start), [])
        # Nothing left to own: the ledger goes with it.
        self.assertFalse(child_registry._ledger_path().exists())

    def test_unregistered_group_is_left_alone(self):
        proc, start = self._group()
        child_registry.register(proc.pid, "shell")
        child_registry.unregister(proc.pid)

        self.assertEqual(child_registry.kill_all(), 0)
        self.assertIsNone(proc.poll())

    def test_never_registers_its_own_group(self):
        # killpg on the CLI's own group would kill the CLI.
        child_registry.register(os.getpgrp(), "shell")
        self.assertEqual(child_registry.owned(), {})

    def _ledger(self, owner, groups):
        path = self.run_dir / ("-".join(str(p) for p in owner) + ".json")
        path.write_text(json.dumps(
            {str(pgid): {"start": start, "kind": "shell"} for pgid, start in groups}))
        return path

    def _dead_owner(self, boot=None):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        start = child_registry.start_ticks(gone.pid)
        gone.wait()
        return (child_registry._BOOT if boot is None else boot, gone.pid, start)

    def test_startup_reaps_groups_of_a_dead_cli(self):
        # What a SIGKILLed CLI leaves: a ledger whose owner is gone, and a
        # group that nothing will ever signal.
        proc, start = self._group()
        ledger = self._ledger(self._dead_owner(), [(proc.pid, start)])

        self.assertEqual(child_registry.reap_dead_owners(), 1)
        proc.wait(timeout=5)
        self.assertEqual(child_registry._members(proc.pid, start), [])
        self.assertFalse(ledger.exists())

    def test_startup_leaves_a_live_clis_groups_alone(self):
        proc, start = self._group()
        me = os.getppid()          # alive, and not this process's own ledger
        ledger = self._ledger((child_registry._BOOT, me,
                               child_registry.start_ticks(me)),
                              [(proc.pid, start)])

        self.assertEqual(child_registry.reap_dead_owners(), 0)
        self.assertIsNone(proc.poll())
        self.assertTrue(ledger.exists())

    def test_recycled_group_number_is_not_mistaken_for_ours(self):
        # The recorded group died and its number now leads a newer group: the
        # newer processes started after the recorded leader did not.
        proc, start = self._group()
        self._ledger(self._dead_owner(), [(proc.pid, start + 10**9)])

        self.assertEqual(child_registry.reap_dead_owners(), 0)
        self.assertIsNone(proc.poll())

    def test_ledger_from_another_boot_is_discarded_unsignalled(self):
        # /proc start times are ticks since boot, so a ledger that outlived a
        # reboot describes numbers that now belong to strangers. This is the
        # arbitrary-process-kill case: the recorded group matches exactly.
        proc, start = self._group()
        ledger = self._ledger(self._dead_owner(boot=child_registry._BOOT - 1),
                              [(proc.pid, start)])

        self.assertEqual(child_registry.reap_dead_owners(), 0)
        self.assertIsNone(proc.poll())
        self.assertFalse(ledger.exists())

    def test_unidentifiable_ledger_is_discarded_unsignalled(self):
        # Two-part names are what the pre-boot-id builds wrote.
        proc, start = self._group()
        ledger = self.run_dir / f"{os.getpid()}-1.json"
        ledger.write_text(json.dumps({str(proc.pid): {"start": start}}))

        self.assertEqual(child_registry.reap_dead_owners(), 0)
        self.assertIsNone(proc.poll())
        self.assertFalse(ledger.exists())

    def test_own_ledger_reap_skips_groups_registered_since_the_read(self):
        # The reaper runs in a startup thread while the CLI is still coming
        # up: a group registered after the ledger was written must survive,
        # and our own live ledger must not be unlinked.
        proc, start = self._group()
        self._ledger(child_registry._SELF, [(proc.pid, start)])
        child_registry.register(proc.pid, "shell")

        self.assertEqual(child_registry.reap_dead_owners(), 0)
        self.assertIsNone(proc.poll())
        self.assertTrue(child_registry._ledger_path().exists())

    def test_own_ledger_from_before_an_exec_restart_is_still_reaped(self):
        proc, start = self._group()
        ledger = self._ledger(child_registry._SELF, [(proc.pid, start)])

        self.assertEqual(child_registry.reap_dead_owners(), 1)
        proc.wait(timeout=5)
        self.assertFalse(ledger.exists())

    def test_descendants_are_reaped_after_their_leader_exited(self):
        # The leader is gone but a forked child kept the group alive.
        proc = subprocess.Popen(["sh", "-c", "sleep 60 &"],
                                stdout=subprocess.DEVNULL, start_new_session=True)
        start = child_registry.start_ticks(proc.pid)
        proc.wait(timeout=5)
        _wait_members(proc.pid, start, 1)
        members = child_registry._members(proc.pid, start)
        self.assertTrue(members, "the background sleep should still be alive")
        self.addCleanup(lambda: [os.kill(p, 9) for p in
                                 child_registry._members(proc.pid, start)])
        self._ledger(self._dead_owner(), [(proc.pid, start)])

        self.assertEqual(child_registry.reap_dead_owners(), 1)
        deadline = time.time() + 5
        while time.time() < deadline and child_registry._members(proc.pid, start):
            time.sleep(0.05)
        self.assertEqual(child_registry._members(proc.pid, start), [])


if __name__ == "__main__":
    unittest.main()
