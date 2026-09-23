"""The bounded head read, and what the retention recycler may touch.

Both optimizations trade correctness for size: the head read answers the
session walk without parsing a whole conversation, and the recycler renames
past-retention files out of the globbed namespace. Each had a way to be
silently wrong — a header that always fell back, and a rename that destroyed a
restorable session — so they are pinned here.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import paths
import session_store


def _big_live_record(**over):
    record = {
        "kind": "live",
        "id": "abc",
        "session_id": "abc",
        "instance_id": "t1",
        "terminal_id": "t1",
        "cwd": "/srv/foo",
        "timestamp": time.time(),
        "pending_continuation": True,
        "last_exit_reason": None,
        "turn_count": 3,
        "state": {"_fork_parent_session_id": "parent"},
        "tasks": [],
        # Last, as session_store writes it.
        "chat_history": [{"role": "user", "content": "x" * 200_000}],
    }
    record.update(over)
    return record


class SessionHeadReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.json"

    def write(self, record):
        self.path.write_text(json.dumps(record), encoding="utf-8")
        self.assertGreater(self.path.stat().st_size,
                           agent_loop._RESUME_HEADER_BYTES,
                           "the test record must be big enough to truncate")
        return self.path

    def test_true_valued_field_does_not_defeat_the_head_read(self):
        # `true` is 4 chars and `false` is 5: one offset for both meant every
        # record with a true boolean fell back to a full parse.
        for value in (True, False):
            with self.subTest(value=value):
                header = agent_loop._read_session_header(
                    self.write(_big_live_record(pending_continuation=value)))
                self.assertIsNotNone(header)
                self.assertIs(header["pending_continuation"], value)

    def test_literals_are_accepted_before_a_brace_or_whitespace(self):
        raw = ('{"kind": "live", "id": "a", "session_id": "a", "cwd": "/srv/foo",'
               ' "timestamp": 1.0, "pending_continuation": true }')
        self.path.write_text(raw + " " * 4, encoding="utf-8")
        header = agent_loop._read_session_header(self.path)
        self.assertIsNotNone(header)
        self.assertIs(header["pending_continuation"], True)

    def test_a_longer_token_starting_with_a_literal_is_refused(self):
        raw = '{"kind": "truthy_kind_but_unquoted", "x": trueish}'
        self.path.write_text(raw, encoding="utf-8")
        self.assertIsNone(agent_loop._read_session_header(self.path))

    def test_live_record_head_reaches_state(self):
        # The live branch requires `state`; if a writer emits chat_history
        # before it, the walk can never be answered from the head.
        header = agent_loop._read_session_header(self.write(_big_live_record()))
        self.assertIsNotNone(header)
        self.assertEqual(header["state"]["_fork_parent_session_id"], "parent")

    def test_chat_history_before_state_still_falls_back(self):
        record = _big_live_record()
        reordered = {"chat_history": record.pop("chat_history"), **record}
        self.assertIsNone(agent_loop._read_session_header(self.write(reordered)))


class SessionWriterOrderTests(unittest.TestCase):
    """The head read only works while every writer keeps the conversation last."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ("SESSIONS_DIR", "SESSION_LOCKS_DIR"):
            patch = mock.patch.object(paths, name, Path(self.tmp.name) / name.lower())
            patch.start()
            self.addCleanup(patch.stop)

    def test_create_session_puts_chat_history_last(self):
        session = session_store.create_session(
            self.tmp.name, {"_session_id": "abc"}, [{"role": "user", "content": "hi"}])
        self.assertEqual(list(session)[-1], "chat_history")

    def test_save_session_moves_a_legacy_order_back(self):
        session = session_store.create_session(self.tmp.name, {"_session_id": "abc"}, [])
        legacy = {"chat_history": session.pop("chat_history"), **session}
        session_store.save_session(legacy)
        self.assertEqual(list(legacy)[-1], "chat_history")
        on_disk = json.loads(
            (paths.SESSIONS_DIR / f"{session_store._session_key(self.tmp.name)}"
             f"_live_abc.json").read_text(encoding="utf-8"))
        self.assertEqual(list(on_disk)[-1], "chat_history")


class RetentionRecyclerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        for name in ("SESSIONS_DIR", "SESSION_LOCKS_DIR"):
            patch = mock.patch.object(paths, name, Path(self.cwd) / name.lower())
            patch.start()
            self.addCleanup(patch.stop)
        paths.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.key = agent_loop._session_key(self.cwd)
        self.stale = time.time() - agent_loop._RESUME_MAX_AGE - 10 * 86400

    def aged(self, name, record=None):
        path = paths.SESSIONS_DIR / name
        path.write_text(json.dumps(record or {"cwd": self.cwd, "session_id": "old",
                                              "kind": "checkpoint", "timestamp": self.stale}),
                        encoding="utf-8")
        import os
        os.utime(path, (self.stale, self.stale))
        return path

    def delete_something(self):
        agent_loop.delete_resume_state(
            self.cwd, {"cwd": self.cwd, "session_id": "target", "kind": "checkpoint"})

    def test_live_copies_and_the_current_pointer_are_never_recycled(self):
        # session_store._recover_latest_live applies no age cutoff, and
        # load_current_session reads a missing pointer as "intentionally
        # closed" — recycling either destroys another terminal's session.
        live = self.aged(f"{self.key}_live_other.json",
                         {"cwd": self.cwd, "session_id": "other", "kind": "live",
                          "timestamp": self.stale, "state": {}})
        pointer = self.aged(f"{self.key}_current_other.json",
                            {"cwd": self.cwd, "session_id": "other"})

        self.delete_something()

        self.assertTrue(live.exists(), "an old live copy is still restorable")
        self.assertTrue(pointer.exists(), "the current pointer is the close signal")

    def test_past_retention_checkpoints_are_recycled(self):
        old = self.aged(f"{self.key}_session_old.json")

        self.delete_something()

        self.assertFalse(old.exists())
        self.assertTrue(old.with_name(old.name + ".expired").exists())

    def test_a_recycled_sidecar_keeps_its_recovery_window(self):
        old = self.aged(f"{self.key}_session_old.json")
        self.delete_something()
        sidecar = old.with_name(old.name + ".expired")
        # rename() keeps the old mtime, which is already past retention: the
        # sidecar must be re-stamped or its recovery window is zero.
        self.assertLess(time.time() - sidecar.stat().st_mtime, 300)

        self.delete_something()
        self.assertTrue(sidecar.exists())

    def test_sidecars_are_deleted_once_past_recovery_age(self):
        sidecar = paths.SESSIONS_DIR / f"{self.key}_session_gone.json.expired"
        sidecar.write_text("{}", encoding="utf-8")
        import os
        expired = time.time() - agent_loop._RECYCLED_MAX_AGE - 3600
        os.utime(sidecar, (expired, expired))

        self.delete_something()

        self.assertFalse(sidecar.exists(), "the recycler must reclaim, not just rename")


if __name__ == "__main__":
    unittest.main()
