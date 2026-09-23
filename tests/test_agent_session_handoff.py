"""Session isolation at the UI boundaries, including background work."""
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

import agent_loop
import laintas_cli
import paths
import session_store


class AgentSessionHandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.cwd = tmp
        self.enterContext(mock.patch.object(paths, "SESSIONS_DIR", Path(tmp) / "sessions"))
        self.enterContext(mock.patch.object(paths, "SESSION_LOCKS_DIR", Path(tmp) / "locks"))
        self.enterContext(mock.patch.object(agent_loop, "_agent_registry", {}))
        self.enterContext(mock.patch.object(agent_loop, "_current_agent_id", "primary"))
        self.enterContext(mock.patch.object(laintas_cli, "_hold_live_session_lease"))
        self.output = io.StringIO()
        self.enterContext(mock.patch.object(
            laintas_cli, "console", Console(file=self.output, force_terminal=False)))

    def agent(self, name, role="pool", history=None, state=None):
        agent = agent_loop.AgentInfo(id=name, name=name, index=1, role=role)
        agent.chat_history = history or []
        agent.state = state or {}
        agent_loop._agent_registry[name] = agent
        return agent

    def history(self, text):
        return [{"role": "user", "content": text, "input_kind": "prompt"}]

    def live(self, name, text, sid=None):
        return session_store.create_session(
            self.cwd, {"_session_id": sid or name + "-session"},
            self.history(text), agent_id=name)

    def test_corrupt_scout_pointer_recovers_scout_even_with_primary_present(self):
        self.live("primary", "primary private")
        scout = self.live("scout", "scout private")
        session_store._current_path(self.cwd, "scout").write_text("{broken")
        restored = session_store.load_current_session(self.cwd, "scout")
        self.assertEqual(restored["agent_id"], "scout")
        self.assertEqual(restored["session_id"], scout["session_id"])
        self.assertEqual(restored["chat_history"], scout["chat_history"])

    def test_missing_scout_pointer_does_not_resurrect_an_older_live_copy(self):
        self.live("scout", "older", "old")
        latest = self.live("scout", "latest", "latest")
        session_store.close_session(latest)
        self.assertIsNone(session_store.load_current_session(self.cwd, "scout"))

    def test_pointer_cannot_load_another_agents_payload(self):
        self.live("primary", "private")
        wrong = session_store._current_path(self.cwd).read_bytes()
        session_store._current_path(self.cwd, "scout").write_bytes(wrong)
        self.assertIsNone(session_store.load_current_session(self.cwd, "scout"))

    def test_startup_archive_uses_payload_owner_before_registry_restore(self):
        scout = self.live("scout", "private")
        self.assertEqual(agent_loop.get_current_agent_id(), "primary")
        laintas_cli._archive_previous_session(self.cwd, scout)
        self.assertEqual(agent_loop.list_resume_states(self.cwd), [])
        saved = agent_loop.load_resume_state(self.cwd, agent_id="scout")
        self.assertEqual(saved["chat_history"], scout["chat_history"])
        self.assertEqual(saved["agent_id"], "scout")

    def test_switch_back_preserves_new_background_history_and_state(self):
        live = self.live("scout", "old task")
        history = self.history("new background job") + [
            {"role": "assistant", "content": "new result"}]
        state = {"_session_id": live["session_id"], "objective": "new job",
                 "_thread_messages": [{"role": "assistant", "content": "new result"}]}
        scout = self.agent("scout", history=history, state=state)
        before = copy.deepcopy((history, state))
        incoming = laintas_cli._handoff_agent_session(None, scout, self.cwd, None)
        self.assertIs(scout.chat_history, history)
        self.assertEqual((scout.chat_history, scout.state), before)
        self.assertEqual(incoming["chat_history"], history)
        disk = session_store.load_current_session(self.cwd, "scout")
        self.assertEqual(disk["chat_history"], history)
        self.assertEqual(disk["state"]["_thread_messages"], state["_thread_messages"])

    def test_uninitialized_agent_adopts_full_saved_conversation(self):
        live = self.live("scout", "existing task")
        live["state"].update(_fork_lineage=["experiment"],
                             _thread_messages=[{"role": "user", "content": "existing task"}])
        session_store.save_session(live)
        scout = self.agent("scout")
        laintas_cli._handoff_agent_session(None, scout, self.cwd, None)
        self.assertEqual(scout.chat_history, live["chat_history"])
        self.assertEqual(scout.state["_session_id"], live["session_id"])
        self.assertEqual(scout.state["_fork_lineage"], ["experiment"])
        self.assertEqual(scout.state["_thread_messages"], live["state"]["_thread_messages"])

    def test_empty_initialized_conversation_does_not_restore_old_history(self):
        live = self.live("scout", "old task")
        scout = self.agent("scout", state={"_session_id": live["session_id"]})
        laintas_cli._handoff_agent_session(None, scout, self.cwd, None)
        self.assertEqual(scout.chat_history, [])

    def test_handoff_does_not_write_a_subagents_history_into_primary_live(self):
        live = self.live("primary", "primary private")
        child = self.agent("child", role="subagent", history=self.history("child task"))
        scout = self.agent("scout")
        laintas_cli._handoff_agent_session(child, scout, self.cwd, live)
        self.assertEqual(session_store.load_current_session(self.cwd)["chat_history"],
                         self.history("primary private"))

    def test_handoff_failure_does_not_change_foreground_or_repl_session(self):
        self.agent("primary", role="primary")
        self.agent("scout")
        live = self.live("primary", "current")
        self.enterContext(mock.patch.object(
            laintas_cli.handle_meta_command, "_current_live_session", live, create=True))
        with mock.patch.object(laintas_cli, "_handoff_agent_session",
                               side_effect=OSError("disk failure")), \
                mock.patch.object(laintas_cli, "switch_to_agent") as switch:
            laintas_cli._cmd_agent(["/agent", "scout"], {}, None)
        switch.assert_not_called()
        self.assertIs(laintas_cli.handle_meta_command._current_live_session, live)
        self.assertEqual(agent_loop.get_current_agent_id(), "primary")
        self.assertIn("Could not switch sessions", self.output.getvalue())

    def test_working_agent_can_be_viewed_but_not_rebound_by_agent_command(self):
        self.agent("primary", role="primary")
        scout = self.agent("scout", history=self.history("running task"))
        scout.status = "running"
        with mock.patch.object(laintas_cli, "_handoff_agent_session") as handoff:
            laintas_cli._cmd_agent(["/agent", "scout"], {}, None)
        handoff.assert_not_called()
        self.assertEqual(agent_loop.get_current_agent_id(), "primary")
        self.assertIn("/agents", self.output.getvalue())

    def test_plain_list_handles_all_roles_and_prefers_current_history(self):
        self.agent("primary", role="primary", history=self.history("current"))
        self.agent("child", role="subagent")
        self.agent("other", role="legacy", history=self.history("current"))
        with mock.patch.object(laintas_cli, "latest_resume_summary") as summary:
            laintas_cli._cmd_agents_plain(["/agents"])
        summary.assert_not_called()
        text = self.output.getvalue()
        self.assertIn("Subagents", text)
        self.assertIn("Other", text)
        self.assertIn("foreground", text)
        self.assertIn("1 turn(s) in current conversation", text)


if __name__ == "__main__":
    unittest.main()
