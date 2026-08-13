"""Tests for peer_coordination — cross-instance file-conflict coordination.

Covers:
- file_etag: identity encoding + change detection
- registry: register / heartbeat / unregister / stale detection
- lazy activation: single instance stays off, second peer turns it on
- L1 CAS: note_read change flag, assert_unchanged refusal, note_write refresh
- L2 write log: append + per-day retention GC
- explicit `peer_coordination: off` override
"""

import os
import sys
import tempfile
import time
import unittest
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths                      # noqa: E402
import peer_coordination          # noqa: E402


class PeerCoordinationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_root = Path(self._tmp.name)
        # Redirect the module's registries into the temp tree so tests never
        # touch the real ~/.laintas.
        paths.INSTANCES_DIR = self._tmp_root / "instances"
        paths.WRITES_DIR = self._tmp_root / "writes"
        paths.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
        # Fresh coordinator + no cached override for every test.
        peer_coordination._coord = None
        peer_coordination._OFF_OVERRIDE = None
        # Deterministic cwd for all instances in this test.
        self._cwd = str(self._tmp_root / "work")
        Path(self._cwd).mkdir(parents=True, exist_ok=True)
        self._coord = peer_coordination.get_coord()

    def tearDown(self):
        peer_coordination._coord = None
        peer_coordination._OFF_OVERRIDE = None
        paths.INSTANCES_DIR = paths.LAINTAS_HOME / "instances"
        paths.WRITES_DIR = paths.LAINTAS_HOME / "writes"
        self._tmp.cleanup()

    # ── helpers ───────────────────────────────────────────────────────
    def _make_file(self, name="a.txt", content="hello"):
        p = Path(self._cwd) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def _fake_peer(self, pid=99999, days_old=0.0):
        """Drop a registration file into the same cwd hash dir, optionally
        with an artificially old mtime (to simulate a stale/crashed peer)."""
        reg_dir = paths.INSTANCES_DIR / peer_coordination._cwd_hash(self._cwd)
        reg_dir.mkdir(parents=True, exist_ok=True)
        p = reg_dir / f"peer-{pid}.json"
        p.write_text('{"instance_id": "peer-%d", "pid": %d}' % (pid, pid),
                     encoding="utf-8")
        if days_old:
            old = time.time() - days_old
            os.utime(p, (old, old))
        return p

    # ── file_etag ─────────────────────────────────────────────────────
    def test_etag_changes_when_content_changes(self):
        p = self._make_file(content="v1")
        e1 = peer_coordination.file_etag(p)
        Path(p).write_text("v2", encoding="utf-8")
        e2 = peer_coordination.file_etag(p)
        self.assertNotEqual(e1, e2)

    def test_etag_stable_without_change(self):
        p = self._make_file(content="v1")
        self.assertEqual(peer_coordination.file_etag(p),
                         peer_coordination.file_etag(p))

    def test_etag_empty_for_missing_file(self):
        self.assertEqual(peer_coordination.file_etag("/nonexistent/xyz"),
                         "")

    # ── registry lifecycle ────────────────────────────────────────────
    def test_register_creates_registration_file(self):
        self._coord.register(self._cwd)
        reg_dir = paths.INSTANCES_DIR / peer_coordination._cwd_hash(self._cwd)
        files = list(reg_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stem, self._coord._instance_id)

    def test_unregister_removes_registration_file(self):
        self._coord.register(self._cwd)
        self._coord.unregister()
        reg_dir = paths.INSTANCES_DIR / peer_coordination._cwd_hash(self._cwd)
        self.assertEqual(list(reg_dir.glob("*.json")), [])

    # ── lazy activation ───────────────────────────────────────────────
    def test_single_instance_stays_disabled(self):
        self._coord.register(self._cwd)
        self.assertFalse(self._coord.maybe_update())
        self.assertFalse(self._coord.enabled())

    def test_second_peer_enables(self):
        self._coord.register(self._cwd)
        self._fake_peer(pid=111)
        # Force a fresh scan (bypass the 5s scan-interval cache).
        self._coord._last_scan = 0.0
        self.assertTrue(self._coord.maybe_update())
        self.assertTrue(self._coord.enabled())

    def test_peer_removal_disables_again(self):
        self._coord.register(self._cwd)
        self._fake_peer(pid=111)
        self._coord._last_scan = 0.0
        self.assertTrue(self._coord.maybe_update())
        # Peer disappears → back to single-instance behavior.
        reg_dir = paths.INSTANCES_DIR / peer_coordination._cwd_hash(self._cwd)
        (reg_dir / "peer-111.json").unlink()
        self._coord._last_scan = 0.0
        self.assertFalse(self._coord.maybe_update())
        self.assertFalse(self._coord.enabled())

    def test_stale_peer_does_not_enable(self):
        self._coord.register(self._cwd)
        self._fake_peer(pid=222, days_old=peer_coordination._HEARTBEAT_STALE_SECS + 60)
        self._coord._last_scan = 0.0
        self.assertFalse(self._coord.maybe_update())
        self.assertFalse(self._coord.enabled())

    def test_explicit_off_override(self):
        self._coord.register(self._cwd)
        self._fake_peer(pid=333)
        peer_coordination._OFF_OVERRIDE = True
        self._coord._last_scan = 0.0
        self.assertFalse(self._coord.maybe_update())
        self.assertFalse(self._coord.enabled())

    # ── L1 CAS ────────────────────────────────────────────────────────
    def _activate(self):
        self._coord.register(self._cwd)
        self._fake_peer(pid=444)
        self._coord._last_scan = 0.0
        self.assertTrue(self._coord.maybe_update())

    def test_note_read_tracks_fingerprint(self):
        self._activate()
        p = self._make_file(content="v1")
        note = self._coord.note_read(p)
        self.assertFalse(note["changed"])
        self.assertIn(os.path.realpath(p), self._coord._read_fps)

    def test_note_read_reports_external_change(self):
        self._activate()
        p = self._make_file(content="v1")
        self._coord.note_read(p)
        Path(p).write_text("v2", encoding="utf-8")
        note = self._coord.note_read(p)
        self.assertTrue(note["changed"])

    def test_assert_unchanged_passes_on_clean(self):
        self._activate()
        p = self._make_file(content="v1")
        self._coord.note_read(p)
        self.assertIsNone(self._coord.assert_unchanged(p))

    def test_assert_unchanged_refuses_after_external_change(self):
        self._activate()
        p = self._make_file(content="v1")
        self._coord.note_read(p)
        Path(p).write_text("v2", encoding="utf-8")
        err = self._coord.assert_unchanged(p)
        self.assertIsNotNone(err)
        self.assertIn("changed since", err)

    def test_assert_unchanged_allows_never_read(self):
        self._activate()
        p = self._make_file(content="v1")
        # Never note_read'ed → nothing to protect → write allowed.
        self.assertIsNone(self._coord.assert_unchanged(p))

    def test_note_write_refreshes_fingerprint(self):
        self._activate()
        p = self._make_file(content="v1")
        self._coord.note_read(p)
        Path(p).write_text("v2", encoding="utf-8")
        # Simulate this instance doing the write itself → its own CAS must
        # now pass (no false positive on the file it just wrote).
        self._coord.note_write(p)
        self.assertIsNone(self._coord.assert_unchanged(p))

    def test_inactive_coordinator_never_blocks(self):
        # No peers → not enabled → note_read/assert_unchanged are no-ops.
        self._coord.register(self._cwd)
        p = self._make_file(content="v1")
        note = self._coord.note_read(p)
        self.assertFalse(note["changed"])
        self.assertIsNone(self._coord.assert_unchanged(p))
        Path(p).write_text("v2", encoding="utf-8")
        self.assertIsNone(self._coord.assert_unchanged(p))

    # ── L2 write log ──────────────────────────────────────────────────
    def test_log_write_appends_entry(self):
        self._activate()
        p = self._make_file(content="v1")
        self._coord.log_write(p, "write")
        log_dir = paths.WRITES_DIR / peer_coordination._cwd_hash(self._cwd)
        files = list(log_dir.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        # json.dumps(separators=(",", ":")) → no spaces around separators.
        self.assertIn('"op":"write"', lines[0])
        self.assertIn(self._coord._instance_id, lines[0])

    def test_log_write_retention_gc(self):
        self._activate()
        p = self._make_file(content="v1")
        self._coord.log_write(p, "write")
        log_dir = paths.WRITES_DIR / peer_coordination._cwd_hash(self._cwd)
        # Drop an old-dated file into the log dir → next write must prune it.
        old_name = "20000101.jsonl"
        (log_dir / old_name).write_text("stale\n", encoding="utf-8")
        self._coord.log_write(p, "write")
        files = sorted(f.name for f in log_dir.glob("*.jsonl"))
        self.assertNotIn(old_name, files)
        today = [f for f in files if f != old_name]
        self.assertEqual(len(today), 1)   # old pruned, today's has 2 lines
        lines = (log_dir / today[0]).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_log_write_inactive_is_noop(self):
        # Not enabled → nothing appended.
        self._coord.register(self._cwd)
        p = self._make_file(content="v1")
        self._coord.log_write(p, "write")
        log_dir = paths.WRITES_DIR / peer_coordination._cwd_hash(self._cwd)
        self.assertFalse(list(log_dir.glob("*.jsonl")))

    # ── Session Lease (single-owner) ─────────────────────────────────
    def _lease_path(self, sid="sess-1"):
        return (paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self._cwd)
                / f"{sid}.lock")

    def test_acquire_lease_creates_lock(self):
        r = peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        self.assertTrue(r["ok"])
        self.assertTrue(self._lease_path().exists())
        owner = json.loads(self._lease_path().read_text(encoding="utf-8"))
        self.assertEqual(owner["pid"], os.getpid())
        self.assertEqual(owner["instance_id"], paths.PROCESS_INSTANCE_ID)

    def test_acquire_same_lease_is_idempotent(self):
        peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        r = peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        self.assertTrue(r["ok"])
        self.assertIsNone(r.get("owner"))

    def test_release_removes_lock(self):
        peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        peer_coordination.release_session_lease(self._cwd, "sess-1")
        self.assertFalse(self._lease_path().exists())

    def test_release_all_clears_held(self):
        peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        peer_coordination.acquire_session_lease(self._cwd, "sess-2")
        peer_coordination.release_all_leases()
        self.assertFalse(self._lease_path("sess-1").exists())
        self.assertFalse(self._lease_path("sess-2").exists())

    def test_acquire_refused_when_other_live_instance_holds(self):
        # Simulate another live process owning the lease (a different pid
        # that is currently alive — use our own pid but a different
        # instance_id so it looks like a peer).
        lock_dir = paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self._cwd)
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "sess-1.lock").write_text(
            json.dumps({"instance_id": "peer-other", "pid": os.getpid()}),
            encoding="utf-8")
        r = peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["owner"]["instance_id"], "peer-other")

    def test_acquire_takes_over_stale_lock(self):
        # A lock whose owner pid is dead → broken and taken over.
        lock_dir = paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self._cwd)
        lock_dir.mkdir(parents=True, exist_ok=True)
        dead_pid = 99999999  # almost certainly no such process
        (lock_dir / "sess-1.lock").write_text(
            json.dumps({"instance_id": "peer-dead", "pid": dead_pid}),
            encoding="utf-8")
        r = peer_coordination.acquire_session_lease(self._cwd, "sess-1")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("took_over"))
        owner = json.loads(self._lease_path().read_text(encoding="utf-8"))
        self.assertEqual(owner["instance_id"], paths.PROCESS_INSTANCE_ID)

    def test_pid_alive(self):
        self.assertTrue(peer_coordination._pid_alive(os.getpid()))
        self.assertFalse(peer_coordination._pid_alive(0))
        self.assertFalse(peer_coordination._pid_alive(99999999))


if __name__ == "__main__":
    unittest.main()
