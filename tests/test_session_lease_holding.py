"""Two CLIs must not end up running one session.

The lease existed before this, but only /resume ever asked for it. An
instance that merely started and worked held nothing, so a second instance's
/resume on that same conversation was granted and both wrote the same
autosave — the bug users kept hitting with two terminals open. These tests
pin ownership to *running* the session instead of to the command that opened
it, and cover the two side doors: snapshots too old to carry a session id,
and two CLIs in one terminal sharing a current-session pointer.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import laintas_cli
import paths
import peer_coordination
import session_store


class _LeaseHome(unittest.TestCase):
    """Each test gets its own LAINTAS_HOME so locks never leak between them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.join(self.tmp.name, "work")
        os.makedirs(self.cwd, exist_ok=True)
        self.locks = paths.SESSION_LOCKS_DIR
        patched = mock.patch.object(
            paths, "SESSION_LOCKS_DIR",
            __import__("pathlib").Path(self.tmp.name) / "session_locks")
        patched.start()
        self.addCleanup(patched.stop)
        peer_coordination._held_leases.clear()
        laintas_cli._LIVE_SESSION_LEASE.update(cwd="", session_id="")
        self.addCleanup(laintas_cli._release_live_session_lease)

    def lock_path(self, session_id):
        return (paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self.cwd)
                / f"{session_id}.lock")

    def peer_holds(self, session_id):
        """A lock written by another live instance (our pid, foreign id)."""
        path = self.lock_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"instance_id": "peer-other", "pid": os.getpid()}), encoding="utf-8")


class HoldingWhileRunning(_LeaseHome):

    def test_a_running_session_is_leased_even_though_nobody_resumed(self):
        session = session_store.create_session(self.cwd, {"_session_id": "live-1"}, [])
        laintas_cli._hold_live_session_lease(session, self.cwd)
        self.assertTrue(self.lock_path("live-1").exists())
        owner = json.loads(self.lock_path("live-1").read_text(encoding="utf-8"))
        self.assertEqual(owner["instance_id"], paths.PROCESS_INSTANCE_ID)

    def test_a_second_instance_cannot_resume_what_is_being_run(self):
        self.peer_holds("live-1")
        with mock.patch.object(laintas_cli.console, "print") as printed:
            out = laintas_cli._acquire_resume_lease(
                {"session_id": "live-1", "cwd": self.cwd, "chat_history": [{}]})
        self.assertIsNone(out)
        self.assertIn("already open in another running instance",
                      " ".join(str(c.args[0]) for c in printed.call_args_list))

    def test_switching_sessions_releases_the_previous_lease(self):
        first = session_store.create_session(self.cwd, {"_session_id": "live-1"}, [])
        laintas_cli._hold_live_session_lease(first, self.cwd)
        second = session_store.create_session(self.cwd, {"_session_id": "live-2"}, [])
        laintas_cli._hold_live_session_lease(second, self.cwd)
        self.assertFalse(self.lock_path("live-1").exists())
        self.assertTrue(self.lock_path("live-2").exists())

    def test_closing_the_session_gives_the_lease_back(self):
        session = session_store.create_session(self.cwd, {"_session_id": "live-1"}, [])
        laintas_cli._hold_live_session_lease(session, self.cwd)
        laintas_cli._close_live_session(session)
        self.assertFalse(self.lock_path("live-1").exists())

    def test_a_lease_we_cannot_take_is_not_recorded_as_held(self):
        self.peer_holds("live-1")
        session = session_store.create_session(self.cwd, {"_session_id": "live-1"}, [])
        laintas_cli._hold_live_session_lease(session, self.cwd)
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "")


class SnapshotsWithoutASessionId(_LeaseHome):

    def test_an_old_snapshot_is_locked_under_its_own_id(self):
        blob = {"id": "abc123", "cwd": self.cwd, "chat_history": [{}]}
        self.assertEqual(laintas_cli._lease_id_for_blob(blob), "snapshot-abc123")
        self.assertIsNotNone(laintas_cli._acquire_resume_lease(blob))
        self.assertTrue(self.lock_path("snapshot-abc123").exists())

    def test_two_instances_cannot_restore_the_same_old_snapshot(self):
        self.peer_holds("snapshot-abc123")
        with mock.patch.object(laintas_cli.console, "print"):
            out = laintas_cli._acquire_resume_lease(
                {"id": "abc123", "cwd": self.cwd, "chat_history": [{}]})
        self.assertIsNone(out)

    def test_a_snapshot_with_no_identity_at_all_still_resumes(self):
        """Refusing here would strand the conversation; there is nothing to
        collide on either."""
        blob = {"cwd": self.cwd, "chat_history": [{}]}
        self.assertEqual(laintas_cli._lease_id_for_blob(blob), "")
        self.assertIs(laintas_cli._acquire_resume_lease(blob), blob)


class TwoClisInOneTerminal(_LeaseHome):

    def test_a_session_another_instance_runs_is_reported_as_owned(self):
        self.peer_holds("live-1")
        owner = laintas_cli._live_session_lease_owner(self.cwd, "live-1")
        self.assertIsNotNone(owner)
        self.assertEqual(owner["instance_id"], "peer-other")

    def test_our_own_lease_is_not_reported_as_a_peer(self):
        session = session_store.create_session(self.cwd, {"_session_id": "live-1"}, [])
        laintas_cli._hold_live_session_lease(session, self.cwd)
        self.assertIsNone(laintas_cli._live_session_lease_owner(self.cwd, "live-1"))

    def test_a_dead_owner_is_not_a_peer(self):
        path = self.lock_path("live-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"instance_id": "gone", "pid": 99999999}),
                        encoding="utf-8")
        self.assertIsNone(laintas_cli._live_session_lease_owner(self.cwd, "live-1"))


if __name__ == "__main__":
    unittest.main()
