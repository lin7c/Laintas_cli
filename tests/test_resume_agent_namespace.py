"""Per-agent resume namespace isolation (scout/foreman/employees vs primary).

Slice-1 acceptance: a non-primary persistent agent's autosaves/checkpoints/
forks are invisible to primary's picker (and vice versa), while primary keeps
its legacy filenames byte-for-byte.
"""

import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import paths


class ResumeAgentNamespaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        sessions = Path(self.cwd) / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        for name, value in (("SESSIONS_DIR", sessions),
                            ("SESSION_LOCKS_DIR", Path(self.cwd) / "locks")):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def _history(self, text):
        return [{"role": "user", "content": text, "input_kind": "prompt"}]

    def _state(self, sid):
        return {"_session_id": sid, "objective": "test"}

    def test_scout_autosave_invisible_to_primary(self):
        agent_loop.save_resume_state(
            self._state("scout-1"), self._history("scout job"),
            self.cwd, agent_id="scout")
        agent_loop.save_resume_state(
            self._state("prim-1"), self._history("main job"), self.cwd)

        primary_states = agent_loop.list_resume_states(self.cwd)
        scout_states = agent_loop.list_resume_states(
            self.cwd, agent_id="scout")

        self.assertEqual(["prim-1"],
                         [s["session_id"] for s in primary_states])
        self.assertEqual(["scout-1"],
                         [s["session_id"] for s in scout_states])

    def test_checkpoint_namespaces_do_not_collide(self):
        agent_loop.save_resume_checkpoint(
            self._state("prim-c"), self._history("p"), self.cwd)
        agent_loop.save_resume_checkpoint(
            self._state("scout-c"), self._history("s"), self.cwd,
            agent_id="scout")
        # Same logical session id in two namespaces must not overwrite.
        agent_loop.save_resume_checkpoint(
            self._state("shared"), self._history("p shared"), self.cwd)
        agent_loop.save_resume_checkpoint(
            self._state("shared"), self._history("s shared"), self.cwd,
            agent_id="scout")

        p = agent_loop.load_resume_state(self.cwd, "shared")
        s = agent_loop.load_resume_state(self.cwd, "shared", agent_id="scout")
        self.assertIn("p shared", json.dumps(p["chat_history"]))
        self.assertIn("s shared", json.dumps(s["chat_history"]))

    def test_primary_filenames_unchanged(self):
        agent_loop.save_resume_state(
            self._state("prim-1"), self._history("main"), self.cwd)
        agent_loop.save_resume_checkpoint(
            self._state("prim-1"), self._history("main"), self.cwd)
        names = sorted(
            p.name for p in (Path(self.cwd) / "sessions").glob("*.json"))
        key = agent_loop._session_key(self.cwd)
        for name in names:
            # Legacy pattern: {key}_session_*, {key}_resume_* — no __ag_ segment.
            self.assertNotIn("__ag_", name)
            self.assertTrue(name.startswith(key))
        self.assertIn(f"{key}_resume.json", names)

    def test_scout_fork_stays_in_scout_namespace(self):
        parent = agent_loop.save_resume_state(
            self._state("scout-1"), self._history("scout base"),
            self.cwd, agent_id="scout")
        child = agent_loop.save_fork_state(
            self._state("scout-1"), self._history("scout base"),
            self.cwd, "branch-a", child_session_id="scout-child",
            agent_id="scout")
        self.assertIsNotNone(child)
        self.assertEqual("scout", child["agent_id"])
        # Scout sees trunk + fork; primary sees nothing.
        scout_ids = {s["session_id"] for s in agent_loop.list_resume_states(
            self.cwd, agent_id="scout")}
        self.assertIn("scout-child", scout_ids)
        self.assertEqual([], agent_loop.list_resume_states(self.cwd))

    def test_fork_of_primary_snapshot_not_filed_under_scout(self):
        agent_loop.save_resume_state(
            self._state("prim-1"), self._history("main"), self.cwd)
        # Branching from a primary snapshot (e.g. via a picker that passes no
        # agent_id) must keep the child in primary's namespace.
        child = agent_loop.save_fork_state(
            self._state("prim-1"), self._history("main"), self.cwd,
            "experiment", fork_parent_session_id="prim-1",
            child_session_id="fork-of-prim")
        self.assertEqual("primary", child["agent_id"])
        self.assertEqual([], agent_loop.list_resume_states(
            self.cwd, agent_id="scout"))

    def test_load_latest_respects_namespace(self):
        agent_loop.save_resume_state(
            self._state("prim-1"), self._history("main"), self.cwd)
        agent_loop.save_resume_state(
            self._state("scout-1"), self._history("scout"), self.cwd,
            agent_id="scout")
        latest_primary = agent_loop.load_resume_state(self.cwd)
        latest_scout = agent_loop.load_resume_state(self.cwd, agent_id="scout")
        self.assertEqual("prim-1", latest_primary["session_id"])
        self.assertEqual("scout-1", latest_scout["session_id"])

    def test_latest_resume_summary_respects_namespace(self):
        agent_loop.save_resume_state(
            self._state("prim-1"), self._history("main"), self.cwd)
        agent_loop.save_resume_state(
            self._state("scout-1"), self._history("scout"), self.cwd,
            agent_id="scout")
        self.assertIsNotNone(agent_loop.latest_resume_summary(self.cwd))
        self.assertIsNotNone(agent_loop.latest_resume_summary(
            self.cwd, agent_id="scout"))
        self.assertIsNone(agent_loop.latest_resume_summary(
            self.cwd, agent_id="foreman"))

    def test_legacy_blob_without_agent_id_reads_as_primary(self):
        # A pre-namespace file on disk has no agent_id field; primary's picker
        # must still offer it and scout's must not.
        key = agent_loop._session_key(self.cwd)
        legacy_path = (Path(self.cwd) / "sessions"
                       / f"{key}_session_legacy1.json")
        legacy_path.write_text(json.dumps({
            "schema_version": 2, "id": "legacy1", "session_id": "legacy1",
            "kind": "autosave", "cwd": self.cwd,
            "timestamp": time.time(),
            "title": "old session", "turn_count": 1,
            "state": {}, "chat_history": self._history("old"),
        }), encoding="utf-8")
        p = agent_loop.list_resume_states(self.cwd)
        s = agent_loop.list_resume_states(self.cwd, agent_id="scout")
        self.assertEqual(["legacy1"], [x["session_id"] for x in p])
        self.assertEqual([], s)

    def test_delete_walk_stays_in_namespace(self):
        agent_loop.save_resume_state(
            self._state("prim-1"), self._history("main"), self.cwd)
        agent_loop.save_resume_state(
            self._state("scout-1"), self._history("scout"), self.cwd,
            agent_id="scout")
        scout_blob = agent_loop.load_resume_state(self.cwd, agent_id="scout")
        agent_loop.delete_resume_state(self.cwd, scout_blob)
        # Scout's file is gone; primary's survives untouched.
        self.assertEqual([], agent_loop.list_resume_states(
            self.cwd, agent_id="scout"))
        self.assertEqual(["prim-1"],
                         [x["session_id"]
                          for x in agent_loop.list_resume_states(self.cwd)])


if __name__ == "__main__":
    unittest.main()
