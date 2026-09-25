"""The startup sweep of ~/.laintas: what dead instances leave behind goes, what
a live instance or a recoverable session still needs stays.

Before it existed nothing removed any of this — one working machine had 2,527
registry directories, 2,875 empty lock directories and 237MB of recycled
session sidecars past their recovery window.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import paths
import peer_coordination

DAY = 86400
DEAD_PID = 2 ** 22 + 12345          # above pid_max on Linux: never alive


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


class StateGcTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.instances = root / "instances"
        self.locks = root / "session_locks"
        self.sessions = root / "sessions"
        for d in (self.instances, self.locks, self.sessions):
            d.mkdir()
        for name, value in (("INSTANCES_DIR", self.instances),
                            ("SESSION_LOCKS_DIR", self.locks),
                            ("SESSIONS_DIR", self.sessions)):
            patcher = mock.patch.object(paths, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _entry(self, root: Path, sub: str, name: str, pid: int, age: float,
               dir_age: float = 0) -> Path:
        d = root / sub
        d.mkdir(exist_ok=True)
        f = d / name
        f.write_text(json.dumps({"pid": pid}), encoding="utf-8")
        _age(f, age)
        _age(d, dir_age)
        return f

    def test_dead_registrations_and_their_old_directories_go(self):
        dead = self._entry(self.instances, "a", "pid-1.json", DEAD_PID, 3600, 2 * DAY)
        peer_coordination.gc_stale_state()
        self.assertFalse(dead.exists())
        self.assertFalse(dead.parent.exists())

    def test_a_live_instance_keeps_its_registration(self):
        live = self._entry(self.instances, "b", "me.json", os.getpid(), 3600, 2 * DAY)
        peer_coordination.gc_stale_state()
        self.assertTrue(live.exists())

    def test_a_fresh_registration_of_a_dead_pid_is_left_to_the_heartbeat_window(self):
        fresh = self._entry(self.instances, "c", "x.json", DEAD_PID, 5)
        peer_coordination.gc_stale_state()
        self.assertTrue(fresh.exists())

    def test_leases_of_dead_owners_go_live_ones_stay(self):
        dead = self._entry(self.locks, "d", "s1.lock", DEAD_PID, 3600)
        live = self._entry(self.locks, "d", "s2.lock", os.getpid(), 3600)
        peer_coordination.gc_stale_state()
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())

    def test_only_old_empty_directories_are_removed(self):
        old_empty = self.locks / "old"
        new_empty = self.locks / "new"
        old_empty.mkdir()
        new_empty.mkdir()
        _age(old_empty, 2 * DAY)
        peer_coordination.gc_stale_state()
        self.assertFalse(old_empty.exists())
        self.assertTrue(new_empty.exists())

    def test_a_lease_survives_its_directory_being_collected_mid_acquire(self):
        cwd = "/some/where"
        real_mkdir = Path.mkdir
        calls = {"n": 0}

        def racing_mkdir(self_path, *a, **k):
            real_mkdir(self_path, *a, **k)
            # The first time, another process's sweep removes it right after.
            if self_path.parent == paths.SESSION_LOCKS_DIR and calls["n"] == 0:
                calls["n"] += 1
                self_path.rmdir()

        with mock.patch.object(Path, "mkdir", racing_mkdir):
            result = peer_coordination.acquire_session_lease(cwd, "sess1")
        self.addCleanup(peer_coordination.release_session_lease, cwd, "sess1")
        self.assertTrue(result["ok"], result)

    def test_heartbeat_re_registers_a_collected_instance(self):
        coord = peer_coordination._PeerCoordinator()
        coord.register("/w")
        self.addCleanup(coord.unregister)
        coord._reg_file.unlink()
        coord._last_heartbeat = 0
        coord._heartbeat()
        self.assertTrue(coord._reg_file.exists())

    def test_recycled_sidecars_past_recovery_go_for_every_cwd(self):
        old = self.sessions / "aaaa_live_1.json.expired"
        recent = self.sessions / "bbbb_live_2.json.expired"
        live = self.sessions / "cccc_live_3.json"
        for f in (old, recent, live):
            f.write_text("{}", encoding="utf-8")
        _age(old, 8 * DAY)
        _age(live, 30 * DAY)
        self.assertEqual(1, agent_loop._purge_recycled_session_files(None))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(live.exists(), "only .expired sidecars are collected")


if __name__ == "__main__":
    unittest.main()
