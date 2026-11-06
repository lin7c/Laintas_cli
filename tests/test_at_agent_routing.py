"""`@name task` hands work to another Agent; approvals reach the user.

The /agents view no longer operates Agents. Work is handed out from the
prompt (idle or on the run's append line) with ``@name``, runs in the
background, and reports back as a message behind the L> mark; a background
Agent's approval is asked by whoever owns the terminal.
"""
import threading
import time
import unittest
from unittest import mock

import agent_loop
import approval_broker
import laintas_cli
import startup_mail


class _RegistryCase(unittest.TestCase):
    def setUp(self):
        self._saved = dict(agent_loop._agent_registry)
        self._saved_current = agent_loop._current_agent_id
        agent_loop._agent_registry.clear()
        self.primary = self._agent("primary", 0, role="primary")
        agent_loop._current_agent_id = "primary"
        approval_broker.reset()

    def tearDown(self):
        agent_loop._agent_registry.clear()
        agent_loop._agent_registry.update(self._saved)
        agent_loop._current_agent_id = self._saved_current
        approval_broker.reset()

    def _agent(self, agent_id, index, role="pool", name=None, parent=None):
        info = agent_loop.AgentInfo(id=agent_id, name=name or agent_id,
                                    index=index, role=role, parent_id=parent)
        agent_loop._agent_registry[agent_id] = info
        return info


class AtRoutingTests(_RegistryCase):
    def test_unknown_name_is_prose(self):
        self.assertEqual((False, "@everyone hello"),
                         laintas_cli._route_at_agent("@everyone hello"))

    def test_foreground_agent_is_plain_input(self):
        self.assertEqual((False, "fix it"),
                         laintas_cli._route_at_agent("@primary fix it"))

    def test_idle_employee_gets_a_background_assignment(self):
        self._agent("AI-2", 1, name="scout", parent="primary")
        with mock.patch.object(laintas_cli, "start_agent_assignment",
                               return_value=(True, "", None)) as start, \
                mock.patch.object(laintas_cli, "get_loop_deps", return_value=None):
            handled, rest = laintas_cli._route_at_agent("@scout look into x", {})
        self.assertEqual((True, ""), (handled, rest))
        args, kwargs = start.call_args
        self.assertEqual(("AI-2", "look into x"), args[:2])
        self.assertTrue(callable(kwargs["on_finished"]))

    def test_busy_employee_gets_an_update_not_a_new_task(self):
        scout = self._agent("AI-2", 1, name="scout", parent="primary")
        scout.status = "running"
        with mock.patch.object(laintas_cli, "start_agent_assignment") as start:
            handled, _ = laintas_cli._route_at_agent("@scout also check y")
        self.assertTrue(handled)
        start.assert_not_called()
        self.assertEqual("also check y", scout.message_queue.get_nowait())

    def test_finished_subagent_is_refused(self):
        self._agent("AI-3", 2, role="subagent", parent="primary")
        with mock.patch.object(laintas_cli, "start_agent_assignment") as start:
            handled, _ = laintas_cli._route_at_agent("@AI-3 more")
        self.assertTrue(handled)
        start.assert_not_called()

    def test_append_line_routes_at_mentions(self):
        scout = self._agent("AI-2", 1, name="scout", parent="primary")
        scout.status = "running"
        import queue
        foreground = queue.Queue()
        self.assertTrue(laintas_cli._queue_supplementary(
            foreground, "@scout note this"))
        self.assertTrue(foreground.empty())
        self.assertEqual("note this", scout.message_queue.get_nowait())


class MentionCompletionTests(_RegistryCase):
    def test_at_completes_other_persistent_agents(self):
        from prompt_toolkit.document import Document
        self._agent("AI-2", 1, name="scout", parent="primary")
        self._agent("AI-3", 2, role="subagent", name="scratch", parent="AI-2")
        completer = laintas_cli.MetaCompleter()
        texts = [c.text for c in completer.get_completions(Document("@sc"), None)]
        self.assertEqual(["scout "], texts)
        self.assertEqual([], list(completer.get_completions(
            Document("@pri"), None)), "the foreground Agent is not offered")


class AssignmentMessageTests(_RegistryCase):
    def test_finished_assignment_leaves_a_message_that_switches(self):
        scout = self._agent("AI-2", 1, name="scout", parent="primary")
        assignment = agent_loop.AgentAssignment(
            id="job-1", task="look into x", terminal_name="temporary:AI-2",
            created_at=time.time())
        assignment.status = "completed"
        assignment.result = "found it"
        key = "agent:AI-2:job-1"
        try:
            with mock.patch.object(laintas_cli, "save_resume_state"):
                laintas_cli._user_assignment_finished(scout)(assignment)
            item = startup_mail.get(key)
            self.assertIsNotNone(item)
            self.assertIn("found it", item.body)
            with mock.patch.object(laintas_cli, "_inject_input") as inject:
                laintas_cli._open_message_action(item, None)
            self.assertEqual("/agent AI-2", inject.call_args[0][0])
        finally:
            startup_mail.delete(key, remember=False)


class BrokerTests(_RegistryCase):
    def setUp(self):
        super().setUp()
        tty = mock.patch.object(laintas_cli.sys, "stdin",
                                mock.Mock(isatty=lambda: True))
        tty.start()
        self.addCleanup(tty.stop)

    def test_no_tty_denies_without_asking(self):
        with mock.patch.object(laintas_cli.sys, "stdin",
                               mock.Mock(isatty=lambda: False)), \
                mock.patch.object(laintas_cli, "_ask_approval_here") as ask:
            self.assertEqual("no", laintas_cli._brokered_approval(
                "write", "a.py", "Apply?", allow_always=False,
                destructive=False))
        ask.assert_not_called()

    def test_claim_and_withdraw_are_exclusive(self):
        request = approval_broker.submit(approval_broker.ApprovalRequest(
            agent_id="AI-2", title="t", body="b", question="q"))
        self.assertIs(request, approval_broker.claim())
        self.assertFalse(approval_broker.withdraw(request))
        other = approval_broker.submit(approval_broker.ApprovalRequest(
            agent_id="AI-2", title="t", body="b", question="q"))
        self.assertTrue(approval_broker.withdraw(other))
        self.assertIsNone(approval_broker.claim())

    def test_background_approval_is_asked_by_the_idle_main_loop(self):
        self._agent("AI-2", 1, name="scout", parent="primary")
        answer = {}

        def worker():
            with agent_loop.thread_agent("AI-2"):
                answer["value"] = laintas_cli._brokered_approval(
                    "write", "a.py", "Apply?", allow_always=False,
                    destructive=False)

        laintas_cli._repl_prompt_idle.set()
        try:
            with mock.patch.object(laintas_cli, "_interrupt_prompt_keeping_draft"):
                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                deadline = time.time() + 2
                while not approval_broker.has_pending() and time.time() < deadline:
                    time.sleep(0.01)
                with mock.patch.object(laintas_cli, "_ask_approval_here",
                                       return_value="yes") as ask:
                    self.assertTrue(laintas_cli._process_brokered_approval())
                thread.join(2)
        finally:
            laintas_cli._repl_prompt_idle.clear()
        self.assertEqual("yes", answer.get("value"))
        self.assertEqual("write", ask.call_args[0][0])

    def test_unclaimed_request_is_asked_inline(self):
        laintas_cli._repl_prompt_idle.clear()
        with mock.patch.object(laintas_cli, "_ask_approval_here",
                               return_value="no") as ask:
            result = []
            thread = threading.Thread(target=lambda: result.append(
                laintas_cli._brokered_approval(
                    "write", "a.py", "Apply?", allow_always=False,
                    destructive=False)), daemon=True)
            thread.start()
            thread.join(2)
        self.assertEqual(["no"], result)
        ask.assert_called_once()


if __name__ == "__main__":
    unittest.main()
