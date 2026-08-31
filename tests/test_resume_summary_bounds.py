"""The startup advisory's bounded view of a directory's resume blobs.

`list_resume_states` parses every live blob for a cwd so the /resume picker
can collapse duplicates. That is right for a picker and wrong for the one line
that says "there is something here": on a working machine it was 60MB across
62 files and ~2.1s in front of the first prompt. `latest_resume_summary`
answers the same question from the newest few files.
"""

import json
import time
import unittest
from unittest import mock

import agent_loop


class LatestResumeSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = __import__("pathlib").Path(self.tmp.name)
        patcher = mock.patch.object(agent_loop.paths, "SESSIONS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cwd = "/some/project"

    def _write(self, session_id, *, turns, age_seconds, mtime_age=None,
               kind="autosave", chat=None):
        ts = time.time() - age_seconds
        payload = {
            "cwd": self.cwd,
            "session_id": session_id,
            "timestamp": ts,
            "turn_count": turns,
            "kind": kind,
            "chat_history": chat if chat is not None else [
                {"role": "user", "content": "hi"}],
        }
        path = agent_loop._resume_session_path(self.cwd, session_id)
        path.write_text(json.dumps(payload), encoding="utf-8")
        stamp = time.time() - (age_seconds if mtime_age is None else mtime_age)
        import os
        os.utime(path, (stamp, stamp))
        return path

    def test_returns_none_when_the_directory_has_nothing(self):
        self.assertIsNone(agent_loop.latest_resume_summary(self.cwd))

    def test_reports_the_newest_blob(self):
        self._write("old", turns=2, age_seconds=9000)
        self._write("new", turns=7, age_seconds=60)
        summary = agent_loop.latest_resume_summary(self.cwd)
        self.assertEqual(summary["turn_count"], 7)

    def test_agrees_with_the_full_listing(self):
        for index in range(5):
            self._write(f"s{index}", turns=index + 1,
                        age_seconds=1000 * (5 - index))
        summary = agent_loop.latest_resume_summary(self.cwd)
        full = agent_loop.load_resume_state(self.cwd)
        self.assertEqual(summary["turn_count"], full["turn_count"])
        self.assertAlmostEqual(summary["timestamp"], full["timestamp"], places=3)

    def test_a_blob_with_no_conversation_is_not_something_to_resume(self):
        self._write("empty", turns=0, age_seconds=60, chat=[])
        self.assertIsNone(agent_loop.latest_resume_summary(self.cwd))

    def test_expired_blobs_are_ignored(self):
        self._write("stale", turns=3,
                    age_seconds=agent_loop._RESUME_MAX_AGE + 3600)
        self.assertIsNone(agent_loop.latest_resume_summary(self.cwd))

    def test_a_blob_for_another_directory_is_ignored(self):
        path = agent_loop._resume_session_path(self.cwd, "elsewhere")
        path.write_text(json.dumps({
            "cwd": "/other/project", "timestamp": time.time(),
            "turn_count": 4, "chat_history": [{"role": "user"}],
        }), encoding="utf-8")
        self.assertIsNone(agent_loop.latest_resume_summary(self.cwd))

    def test_only_the_probe_window_is_parsed(self):
        for index in range(agent_loop._RESUME_SUMMARY_PROBE + 6):
            self._write(f"s{index}", turns=index, age_seconds=1000 * index)
        real_read = __import__("pathlib").Path.read_text
        reads = []

        def _counting_read(self_path, *a, **kw):
            reads.append(self_path.name)
            return real_read(self_path, *a, **kw)

        with mock.patch.object(__import__("pathlib").Path, "read_text",
                               _counting_read):
            agent_loop.latest_resume_summary(self.cwd)
        self.assertLessEqual(len(reads), agent_loop._RESUME_SUMMARY_PROBE)

    def test_turn_count_is_derived_when_the_blob_does_not_store_one(self):
        path = agent_loop._resume_session_path(self.cwd, "derived")
        path.write_text(json.dumps({
            "cwd": self.cwd, "timestamp": time.time(),
            "chat_history": [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "ls", "input_kind": "shell"},
                {"role": "user", "content": "c"},
            ],
        }), encoding="utf-8")
        summary = agent_loop.latest_resume_summary(self.cwd)
        # Shell lines are not turns — same rule the /resume picker counts by.
        self.assertEqual(summary["turn_count"], 2)

    def test_unreadable_blob_does_not_sink_the_answer(self):
        bad = agent_loop._resume_session_path(self.cwd, "corrupt")
        bad.write_text("{not json", encoding="utf-8")
        self._write("good", turns=3, age_seconds=600)
        summary = agent_loop.latest_resume_summary(self.cwd)
        self.assertEqual(summary["turn_count"], 3)


class ResumeListingCostTests(unittest.TestCase):
    """list_resume_states must not read blobs it is about to discard."""

    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = __import__("pathlib").Path(self.tmp.name)
        patcher = mock.patch.object(agent_loop.paths, "SESSIONS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cwd = "/some/project"

    def test_expired_files_are_skipped_without_being_read(self):
        import os
        fresh = agent_loop._resume_session_path(self.cwd, "fresh")
        fresh.write_text(json.dumps({
            "cwd": self.cwd, "timestamp": time.time(), "turn_count": 1,
            "chat_history": [{"role": "user"}], "session_id": "fresh",
        }), encoding="utf-8")
        stale = agent_loop._resume_session_path(self.cwd, "stale")
        stale.write_text(json.dumps({
            "cwd": self.cwd, "timestamp": 0, "turn_count": 1,
            "chat_history": [{"role": "user"}], "session_id": "stale",
        }), encoding="utf-8")
        old = time.time() - (agent_loop._RESUME_MAX_AGE * 2)
        os.utime(stale, (old, old))

        real_read = __import__("pathlib").Path.read_text
        reads = []

        def _counting_read(self_path, *a, **kw):
            reads.append(self_path.name)
            return real_read(self_path, *a, **kw)

        with mock.patch.object(__import__("pathlib").Path, "read_text",
                               _counting_read):
            states = agent_loop.list_resume_states(self.cwd)
        self.assertEqual([s.get("session_id") for s in states], ["fresh"])
        self.assertNotIn(stale.name, reads)

    def test_a_session_with_one_file_is_never_fingerprinted(self):
        for index in range(4):
            path = agent_loop._resume_session_path(self.cwd, f"s{index}")
            path.write_text(json.dumps({
                "cwd": self.cwd, "timestamp": time.time() - index,
                "turn_count": 1, "session_id": f"s{index}",
                "chat_history": [{"role": "user"}],
            }), encoding="utf-8")
        with mock.patch.object(agent_loop, "_fingerprint_payload") as fp:
            states = agent_loop.list_resume_states(self.cwd)
        fp.assert_not_called()
        self.assertEqual(len(states), 4)

    def test_duplicate_snapshots_of_one_session_still_collapse(self):
        # The checkpoint and the autosave of one session carry identical
        # content; the picker must still show a single entry.
        body = {
            "cwd": self.cwd, "session_id": "dup", "turn_count": 2,
            "title": "t", "chat_history": [{"role": "user", "content": "x"}],
            "tasks": [], "state": {}, "fork_lineage": [],
        }
        auto = agent_loop._resume_session_path(self.cwd, "dup")
        auto.write_text(json.dumps(
            {**body, "timestamp": time.time(), "kind": "autosave"}),
            encoding="utf-8")
        chk = self.dir / f"{agent_loop._session_key(self.cwd)}_resume_c1.json"
        chk.write_text(json.dumps(
            {**body, "timestamp": time.time() - 5, "kind": "checkpoint",
             "id": "c1"}), encoding="utf-8")
        states = agent_loop.list_resume_states(self.cwd)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["kind"], "checkpoint")


if __name__ == "__main__":
    unittest.main()
