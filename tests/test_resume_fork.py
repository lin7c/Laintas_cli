"""Historical continuation branches without rewriting the source session."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import laintas_cli
import paths
import task_manager
import workgraph


class ResumeForkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        patch = mock.patch.object(paths, "SESSIONS_DIR", Path(tmp.name) / "sessions")
        patch.start()
        self.addCleanup(patch.stop)
        self.source = {
            "id": "old-checkpoint", "session_id": "source", "kind": "checkpoint",
            "cwd": self.cwd, "timestamp": 1, "fork_lineage": ["experiment"],
            "state": {"_session_id": "source", "objective": "original goal"},
            "chat_history": [{"role": "user", "content": "old prompt"}],
            "older_summary": "Keep the original requirements",
            "tasks": [{"id": "s1", "subject": "original task", "status": "pending",
                       "owner_agent_id": "source-agent"}],
        }

    def test_live_source_can_be_forked_twice_without_taking_its_lease(self):
        original = copy.deepcopy(self.source)
        with mock.patch("peer_coordination.acquire_session_lease") as acquire:
            first = laintas_cli._fork_resume_blob(self.source, self.cwd)
            second = laintas_cli._fork_resume_blob(self.source, self.cwd)
        acquire.assert_not_called()
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(first["session_id"], "source")
        self.assertEqual(first["parent_session_id"], "source")
        self.assertEqual(first["fork_lineage"][:-1], ["experiment"])
        self.assertEqual(self.source, original)

    def test_old_snapshot_keeps_summary_and_does_not_overwrite_new_tip(self):
        agent_loop.save_resume_state(
            {"_session_id": "source"},
            [{"role": "user", "content": "newer progress"}], self.cwd)
        before = {p: p.read_bytes() for p in paths.SESSIONS_DIR.iterdir()}
        child = laintas_cli._fork_resume_blob(self.source, self.cwd)
        history = []
        state = laintas_cli._restore_resume_blob(child, history)
        history.append({"role": "user", "content": "independent progress"})
        agent_loop.save_resume_state(state, history, self.cwd)
        source_tip = agent_loop.load_resume_state(self.cwd, "source")
        self.assertEqual(source_tip["chat_history"][0]["content"], "newer progress")
        self.assertIn("original requirements", history[0]["content"])
        self.assertEqual(child["chat_history"], self.source["chat_history"])
        # The per-cwd latest pointer may move; the source identity must not.
        for path, data in before.items():
            if path.name != agent_loop._resume_latest_path(self.cwd).name:
                self.assertEqual(path.read_bytes(), data)

    def test_task_checkout_is_independent_of_source_work(self):
        work = workgraph.create_work("source work", cwd=self.cwd, session_id="source")
        self.source["active_work_id"] = work["id"]
        task_manager.import_session_tasks(self.source["tasks"], cwd=self.cwd,
                                          session_id="source")
        child = laintas_cli._fork_resume_blob(self.source, self.cwd)
        laintas_cli._restore_resume_blob(child, [])
        child_work = workgraph.get_active_work(cwd=self.cwd, session_id=child["session_id"])
        self.assertNotEqual(child_work["id"], work["id"])
        child_tasks = task_manager.list_tasks(cwd=self.cwd, session_id=child["session_id"])
        self.assertEqual(child_tasks[0]["subject"], "original task")
        self.assertFalse(child_tasks[0].get("owner_agent_id"))
        task_manager.update_task(child_tasks[0]["id"], status="completed", cwd=self.cwd,
                                 session_id=child["session_id"])
        self.assertEqual(task_manager.list_tasks(cwd=self.cwd, session_id="source")[0]["status"],
                         "pending")

    def test_failed_fork_leaves_source_and_history_untouched(self):
        original = copy.deepcopy(self.source)
        with mock.patch.object(laintas_cli, "save_fork_state", return_value=None):
            self.assertIsNone(laintas_cli._fork_resume_blob(self.source, self.cwd))
        self.assertEqual(self.source, original)
