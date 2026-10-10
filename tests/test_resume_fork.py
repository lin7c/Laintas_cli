"""Historical continuation branches without rewriting the source session."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import account_store
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


class PickerCollapsedHistoryTests(unittest.TestCase):
    """The picker offers the session tip while preserving snapshots on disk."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        for patch in (mock.patch.object(paths, "SESSIONS_DIR", Path(tmp.name) / "sessions"),
                      mock.patch.object(agent_loop.workgraph, "db_path",
                                        return_value=Path(tmp.name) / "wg.db")):
            patch.start()
            self.addCleanup(patch.stop)
        state = {"_session_id": "sess-A"}
        first = [{"role": "user", "content": "first"}]
        agent_loop.save_resume_state(state, first, self.cwd)
        self.checkpoint = agent_loop.save_resume_checkpoint(state, first, self.cwd)
        agent_loop.save_resume_state(
            state, first + [{"role": "assistant", "content": "then more work"}], self.cwd)

    def test_picker_offers_only_the_latest_state_and_does_not_branch(self):
        captured = []

        def select(items, **kwargs):
            captured.extend(items)
            return ("resume", 0)

        with mock.patch.object(laintas_cli, "select_dialog", side_effect=select), \
                mock.patch.object(laintas_cli, "_fork_resume_blob") as fork:
            chosen = laintas_cli.show_resume_picker(self.cwd)
        fork.assert_not_called()
        self.assertEqual(len(captured), 1)
        self.assertEqual(chosen["session_id"], "sess-A")
        self.assertEqual(chosen["chat_history"][-1]["content"], "then more work")
        self.assertTrue(chosen["_tree_session_node"])

    def test_hidden_checkpoint_remains_available_for_an_explicit_fork(self):
        tip = agent_loop.load_resume_state(self.cwd, "sess-A")
        choices = agent_loop.list_resume_states(self.cwd)
        self.assertEqual(len(laintas_cli._build_fork_tree_rows(choices)), 1)
        checkpoint = next(blob for blob in choices if blob.get("kind") == "checkpoint")
        child = laintas_cli._fork_resume_blob(checkpoint, self.cwd)
        self.assertIsNotNone(child)
        self.assertEqual(child["parent_session_id"], "sess-A")
        self.assertNotEqual(child["session_id"], "sess-A")
        self.assertEqual([m["content"] for m in child["chat_history"]], ["first"])
        self.assertEqual(agent_loop.load_resume_state(self.cwd, "sess-A"), tip)

    def test_cancelling_the_picker_preserves_all_saved_snapshots(self):
        before = {p: p.read_bytes() for p in paths.SESSIONS_DIR.iterdir()}
        with mock.patch.object(laintas_cli, "select_dialog", return_value=None):
            self.assertIsNone(laintas_cli.show_resume_picker(self.cwd))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
