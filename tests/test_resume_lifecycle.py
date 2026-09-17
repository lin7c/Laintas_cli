"""Session switching and deletion use identities, never display names."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import agent_loop
import laintas_cli
import paths
import peer_coordination
import session_lifecycle
import session_store
import workgraph


class ResumeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        reset = mock.patch.object(laintas_cli, "_reset_fresh_session_context")
        self.reset_context = reset.start()
        self.addCleanup(reset.stop)
        for name, value in (("SESSIONS_DIR", Path(self.cwd) / "sessions"),
                            ("SESSION_LOCKS_DIR", Path(self.cwd) / "locks")):
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(laintas_cli._release_live_session_lease)
        self.addCleanup(peer_coordination.release_all_leases)
        self.old_lease = dict(laintas_cli._LIVE_SESSION_LEASE)
        laintas_cli._LIVE_SESSION_LEASE.update(cwd="", session_id="")
        self.addCleanup(laintas_cli._LIVE_SESSION_LEASE.update, self.old_lease)

    def saved(self, sid, parent="", text=None):
        state = {"_session_id": sid, "objective": sid}
        if parent:
            state.update(_fork_parent_session_id=parent, _fork_lineage=["same"], _fork_name="same")
        agent_loop.save_resume_state(state, [{"role": "user", "content": text or sid}], self.cwd)
        return agent_loop.load_resume_state(self.cwd, sid)

    def tree_node(self, sid):
        return dict(agent_loop.load_resume_state(self.cwd, sid), _tree_session_node=True)

    def activate(self, sid):
        blob = self.saved(sid)
        history = []
        state = laintas_cli._restore_resume_blob(blob, history)
        live = session_store.create_session(self.cwd, state, history)
        laintas_cli._hold_live_session_lease(live, self.cwd)
        return state, history, live

    def lock(self, sid, peer=False, dead=False):
        path = paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self.cwd) / f"{sid}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"instance_id": "foreign" if peer else paths.PROCESS_INSTANCE_ID,
                                    "pid": 99999999 if dead else os.getpid()}))
        return path

    def test_switch_a_b_a_preserves_identity_and_latest_progress(self):
        state, history, live = self.activate("a")
        b = self.saved("b")
        history.append({"role": "user", "content": "unsaved progress"})
        state, live = laintas_cli._switch_resume_session(b, self.cwd, state, history, live)
        self.assertEqual(state["_session_id"], "b")
        state, live = laintas_cli._switch_resume_session(self.tree_node("a"), self.cwd, state, history, live)
        self.assertEqual(state["_session_id"], "a")
        self.assertEqual(history[-1]["content"], "unsaved progress")
        self.assertFalse(list(paths.SESSIONS_DIR.glob("*_fork_*.json")))
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "a")
        self.assertEqual(session_store.load_current_session(self.cwd)["session_id"], "a")
        self.assertEqual(self.reset_context.call_count, 2)

    def test_resume_current_is_noop_even_for_an_old_snapshot(self):
        state, history, live = self.activate("a")
        old = copy.deepcopy(self.tree_node("a"))
        history.append({"role": "user", "content": "new"})
        with mock.patch.object(laintas_cli, "_restore_resume_blob") as restore:
            self.assertIsNone(laintas_cli._switch_resume_session(old, self.cwd, state, history, live))
        restore.assert_not_called()
        self.assertEqual(history[-1]["content"], "new")

    def test_historical_row_resumes_latest_tip_without_rewinding(self):
        state, history, live = self.activate("a")
        old = self.saved("b", text="old")
        self.saved("b", text="new")
        state, live = laintas_cli._switch_resume_session(old, self.cwd, state, history, live)
        self.assertEqual(history[-1]["content"], "new")
        self.assertEqual(state["_session_id"], "b")

    def test_peer_owned_destination_leaves_source_untouched(self):
        state, history, live = self.activate("a")
        b = self.saved("b")
        self.lock("b", peer=True)
        before = copy.deepcopy((state, history, live))
        self.assertIsNone(laintas_cli._switch_resume_session(b, self.cwd, state, history, live))
        self.assertEqual((state, history, live), before)
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "a")

    def test_restore_failure_keeps_source_and_releases_destination(self):
        state, history, live = self.activate("a")
        b = self.saved("b")
        with mock.patch.object(laintas_cli, "_restore_resume_blob", side_effect=ValueError("bad state")):
            self.assertIsNone(laintas_cli._switch_resume_session(b, self.cwd, state, history, live))
        self.assertEqual(history[-1]["content"], "a")
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "a")
        lock = paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self.cwd) / "b.lock"
        self.assertFalse(lock.exists())
        self.assertEqual(session_store.load_current_session(self.cwd)["session_id"], "a")

    def test_active_root_or_descendant_blocks_entire_delete(self):
        for locked in ("root", "child", "grandchild"):
            with self.subTest(locked=locked):
                self.saved("root")
                self.saved("child", "root")
                self.saved("grandchild", "child")
                path = self.lock(locked, peer=locked == "child")
                before = {p.name: p.read_bytes() for p in paths.SESSIONS_DIR.glob("*.json")}
                with self.assertRaisesRegex(RuntimeError, "is open"):
                    agent_loop.delete_resume_state(self.cwd, self.tree_node("root"))
                self.assertEqual(before, {p.name: p.read_bytes() for p in paths.SESSIONS_DIR.glob("*.json")})
                path.unlink()

    def test_delete_root_removes_expired_descendants_and_live_pointers(self):
        root = self.saved("root")
        child = self.saved("child", "root")
        self.saved("other")
        live = session_store.create_session(self.cwd, child["state"], child["chat_history"])
        path = agent_loop._resume_session_path(self.cwd, "child")
        child["timestamp"] = time.time() - 30 * 86400
        path.write_text(json.dumps(child))
        root["_tree_session_node"] = True
        agent_loop.delete_resume_state(self.cwd, root)
        self.assertEqual({b["session_id"] for b in agent_loop.list_resume_states(self.cwd)}, {"other"})
        self.assertIsNone(session_store.load_current_session(self.cwd))
        self.assertFalse(session_store._session_path(self.cwd, "child").exists())
        self.assertIsNotNone(live)

    def test_delete_child_preserves_parent_and_sibling(self):
        self.saved("root")
        self.saved("child", "root")
        self.saved("grandchild", "child")
        self.saved("sibling", "root")
        agent_loop.delete_resume_state(self.cwd, self.tree_node("child"))
        self.assertEqual({b["session_id"] for b in agent_loop.list_resume_states(self.cwd)}, {"root", "sibling"})

    def test_stale_autosave_checkpoint_live_and_fork_cannot_resurrect_deleted_session(self):
        old = self.saved("a")
        live = session_store.create_session(self.cwd, old["state"], old["chat_history"])
        agent_loop.delete_resume_state(self.cwd, self.tree_node("a"))
        old["chat_history"].append({"role": "user", "content": "late result"})
        agent_loop.save_resume_state(old["state"], old["chat_history"], self.cwd)
        self.assertIsNone(agent_loop.save_resume_checkpoint(old["state"], old["chat_history"], self.cwd))
        session_store.save_session(live)
        agent_loop.save_session_snapshot(old["state"], old["chat_history"], self.cwd)
        self.assertIsNone(agent_loop.save_fork_state(old["state"], old["chat_history"], self.cwd, "new"))
        self.assertEqual(agent_loop.list_resume_states(self.cwd), [])
        self.assertIsNone(session_store.load_current_session(self.cwd))
        self.assertIsNone(laintas_cli._acquire_resume_lease(old))

    def test_deleted_selection_does_not_close_current_session(self):
        state, history, live = self.activate("a")
        stale = self.saved("b")
        agent_loop.delete_resume_state(self.cwd, self.tree_node("b"))
        self.assertIsNone(laintas_cli._switch_resume_session(stale, self.cwd, state, history, live))
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "a")

    def test_delete_old_snapshot_preserves_tip_and_children(self):
        root = self.saved("root")
        checkpoint = agent_loop.save_resume_checkpoint(root["state"], root["chat_history"], self.cwd)
        self.saved("root", text="new")
        self.saved("child", "root")
        checkpoint["_tree_session_node"] = False
        agent_loop.delete_resume_state(self.cwd, checkpoint)
        self.assertEqual({b["session_id"] for b in agent_loop.list_resume_states(self.cwd)}, {"root", "child"})
        self.assertEqual(agent_loop.load_resume_state(self.cwd, "root")["chat_history"][0]["content"], "new")

    def test_same_named_branches_remain_distinct(self):
        a = self.saved("a", "parent-a")
        b = self.saved("b", "parent-b")
        a["kind"] = b["kind"] = "fork"
        tips = [dict(a, kind="autosave", id="tip-a", timestamp=time.time() + 1),
                dict(b, kind="autosave", id="tip-b", timestamp=time.time() + 1)]
        rows = laintas_cli._build_fork_tree_rows([a, b] + tips)
        self.assertEqual(sum(bool(blob["_tree_session_node"]) for blob, _ in rows), 2)
        self.assertEqual(sum(prefix == "" for _, prefix in rows), 2)

    def test_dead_owner_does_not_block_delete(self):
        self.saved("a")
        self.lock("a", peer=True, dead=True)
        agent_loop.delete_resume_state(self.cwd, self.tree_node("a"))
        self.assertEqual(agent_loop.list_resume_states(self.cwd), [])

    def test_delete_does_not_trust_external_path_or_other_cwd(self):
        blob = self.saved("a")
        outside = Path(self.cwd) / "keep.json"
        outside.write_text("keep")
        foreign = paths.SESSIONS_DIR / f"{agent_loop._session_key(self.cwd)}_fork_foreign.json"
        foreign.write_text(json.dumps(dict(blob, cwd="/elsewhere")))
        blob.update(_path=str(outside), _tree_session_node=True)
        agent_loop.delete_resume_state(self.cwd, blob)
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue(foreign.exists())

    def test_failed_unlink_reports_error_and_tombstone_hides_partial_delete(self):
        blob = self.saved("a")
        real = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == agent_loop._resume_session_path(self.cwd, "a"):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", unlink):
            with self.assertRaises(PermissionError):
                agent_loop.delete_resume_state(self.cwd, dict(blob, _tree_session_node=True))
        self.assertEqual(agent_loop.list_resume_states(self.cwd), [])

    def test_branch_cycle_terminates_and_deletes_component(self):
        self.saved("a", "b")
        self.saved("b", "a")
        agent_loop.delete_resume_state(self.cwd, self.tree_node("a"))
        self.assertEqual(agent_loop.list_resume_states(self.cwd), [])

    def test_legacy_branch_delete_includes_nested_legacy_forks_not_parent(self):
        root = self.saved("root")
        legacy = []
        for name, lineage in (("one", ["one"]), ("two", ["one", "two"])):
            blob = dict(root, id=name, kind="fork", parent_session_id="root",
                        fork_lineage=lineage, fork_name=name)
            agent_loop._atomic_write_json(agent_loop._resume_fork_path(self.cwd, name), blob)
            legacy.append(blob)
        agent_loop.delete_resume_state(self.cwd, dict(legacy[0], _tree_session_node=True))
        self.assertEqual({b["session_id"] for b in agent_loop.list_resume_states(self.cwd)}, {"root"})
        self.assertFalse(list(paths.SESSIONS_DIR.glob("*_fork_*.json")))

    def test_legacy_snapshot_without_session_id_keeps_one_identity(self):
        state, history, live = self.activate("a")
        blob = {"id": "old", "cwd": self.cwd, "kind": "checkpoint",
                "state": {}, "chat_history": [{"role": "user", "content": "legacy"}]}
        state, live = laintas_cli._switch_resume_session(blob, self.cwd, state, history, live)
        self.assertEqual(state["_session_id"], "snapshot-old")
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "snapshot-old")

    def test_cross_directory_and_empty_selection_leave_source_intact(self):
        state, history, live = self.activate("a")
        b = self.saved("b")
        for invalid in (dict(b, cwd="/elsewhere"), dict(b, session_id="empty", chat_history=[])):
            self.assertIsNone(laintas_cli._switch_resume_session(invalid, self.cwd, state, history, live))
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "a")
        self.reset_context.assert_not_called()

    def test_lock_io_failure_is_not_treated_as_permission_to_resume(self):
        blob = self.saved("a")
        with mock.patch.object(peer_coordination.os, "link", side_effect=OSError("disk failure")):
            self.assertIsNone(laintas_cli._acquire_resume_lease(blob))

    def test_live_work_reference_is_scoped_to_destination_session(self):
        workgraph.create_work("unrelated global", cwd=self.cwd)
        destination = workgraph.create_work("destination", cwd=self.cwd, session_id="b")
        blob = self.saved("b")
        live = session_store.create_session(self.cwd, blob["state"], blob["chat_history"])
        self.assertEqual(live["active_work_id"], destination["id"])
        session_store.sync_runtime(live, blob["state"], blob["chat_history"], cwd=self.cwd)
        self.assertEqual(live["active_work_id"], destination["id"])

    def test_lifecycle_guard_serializes_another_process(self):
        marker = Path(self.cwd) / "entered"
        script = (
            "from pathlib import Path; import paths, session_lifecycle, sys\n"
            "paths.SESSIONS_DIR = Path(sys.argv[1])\n"
            "print('ready', flush=True)\n"
            "with session_lifecycle.guard(sys.argv[2]):\n"
            "    Path(sys.argv[3]).write_text('entered')\n")
        child = None
        try:
            with session_lifecycle.guard(self.cwd):
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", script, str(paths.SESSIONS_DIR), self.cwd, str(marker)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=.2)
                self.assertFalse(marker.exists())
            _, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error)
            self.assertEqual(marker.read_text(), "entered")
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_startup_does_not_checkpoint_over_peer_owned_session(self):
        blob = self.saved("a", text="new progress")
        live = session_store.create_session(self.cwd, blob["state"],
                                             [{"role": "user", "content": "old progress"}])
        self.lock("a", peer=True)
        with mock.patch.object(laintas_cli, "save_resume_checkpoint") as checkpoint:
            laintas_cli._archive_previous_session(self.cwd, live)
        checkpoint.assert_not_called()
        self.assertEqual(agent_loop.load_resume_state(self.cwd, "a")["chat_history"][0]["content"],
                         "new progress")
        self.assertIsNone(live["closed_at"])

    def test_execute_cannot_bypass_a_peer_lease_or_deleted_id(self):
        self.saved("a")
        lock = self.lock("a", peer=True)
        with mock.patch.object(laintas_cli.os, "getcwd", return_value=self.cwd), \
                mock.patch.object(laintas_cli, "_run_execute_mode") as execute:
            self.assertEqual(laintas_cli.run_execute_mode("task", {}, 0, "a"), 1)
            lock.unlink()
            agent_loop.delete_resume_state(self.cwd, self.tree_node("a"))
            self.assertEqual(laintas_cli.run_execute_mode("task", {}, 0, "a"), 1)
        execute.assert_not_called()

    def test_execute_new_identity_is_leased_and_released_on_failure(self):
        lock = paths.SESSION_LOCKS_DIR / peer_coordination._cwd_hash(self.cwd) / "new.lock"
        def execute(*args):
            self.assertTrue(lock.exists())
            raise RuntimeError("backend failed")
        with mock.patch.object(laintas_cli.os, "getcwd", return_value=self.cwd), \
                mock.patch.object(laintas_cli, "_run_execute_mode", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "backend failed"):
                laintas_cli.run_execute_mode("task", {}, 0, "new")
        self.assertFalse(lock.exists())

    def test_failed_source_close_keeps_source_identity_and_lease(self):
        state, history, live = self.activate("a")
        b = self.saved("b")
        close = session_store.close_session
        def failing_close(item):
            if item["session_id"] == "a":
                raise OSError("close failed")
            return close(item)
        with mock.patch.object(session_store, "close_session", side_effect=failing_close):
            self.assertIsNone(laintas_cli._switch_resume_session(b, self.cwd, state, history, live))
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], "a")
        self.assertIsNone(live["closed_at"])
        self.assertEqual(session_store.load_current_session(self.cwd)["session_id"], "a")

    def test_new_session_runtime_autosave_and_lease_share_identity(self):
        state = {}
        history = [{"role": "user", "content": "first turn"}]
        live = session_store.create_session(self.cwd, state, history)
        laintas_cli._hold_live_session_lease(live, self.cwd)
        agent_loop.save_resume_state(state, history, self.cwd)
        saved = agent_loop.load_resume_state(self.cwd)
        self.assertEqual(state["_session_id"], live["session_id"])
        self.assertEqual(saved["session_id"], live["session_id"])
        self.assertEqual(laintas_cli._LIVE_SESSION_LEASE["session_id"], live["session_id"])
        with self.assertRaisesRegex(RuntimeError, "is open"):
            agent_loop.delete_resume_state(self.cwd, dict(saved, _tree_session_node=True))
