"""A child that hits a wall asks its caller, and resumes where it stopped.

Escalation used to happen exactly once, at the end of the run: a child blocked
on step two spent the rest of its budget working around the wall and reported
it in the post-mortem. Measured 2026-08-28, a review child died on `contract
not satisfied` after 8 tool calls because the task wanted something its tool
scope forbade — the caller held the authority to fix that and never heard about
it until the child was already gone.

The caller is asked while the child still has the context that raised the
question, and the child keeps that context while it waits.
"""
import threading
import time
import unittest

import agent_contract
import agent_loop
import tools


class HelpChannelTests(unittest.TestCase):
    def setUp(self):
        self.parent = agent_loop.register_agent(name="help-parent",
                                                role="primary")
        self.addCleanup(agent_loop.unregister_agent, self.parent.id)
        self.child = agent_loop.register_agent(name="help-child", depth=1,
                                               role="subagent",
                                               parent_id=self.parent.id)
        self.addCleanup(agent_loop.unregister_agent, self.child.id)
        self.parent.status = "running"

    def _request(self, **over):
        return {"question": "may I proceed without the shell?",
                "blocker": "shell.exec is not in my scope",
                "needed_capabilities": ["shell.exec"],
                "options": ["widen my tools", "accept a partial result"],
                **over}

    def test_the_caller_gets_the_question_and_the_child_resumes_with_the_answer(self):
        answered = threading.Event()

        def caller():
            for _ in range(100):
                msg = agent_loop.recv_from_inbox(self.parent.id, timeout=0.1)
                if msg and msg.get("kind") == "child-help":
                    self.assertEqual(self.child.id, msg["from"])
                    self.assertIn("shell.exec", msg["needed_capabilities"])
                    agent_loop.answer_child_help(
                        self.parent.id, self.child.id,
                        decision="proceed without it",
                        guidance="report what you could not verify")
                    answered.set()
                    return

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        result = agent_loop.ask_parent_for_help(self.child.id, self._request(),
                                                timeout=10)
        thread.join(timeout=5)
        self.assertTrue(answered.is_set(), "the caller never saw the question")
        self.assertTrue(result["answered"])
        self.assertEqual("proceed without it", result["decision"])
        self.assertIn("could not verify", result["guidance"])

    def test_a_waiting_child_is_in_a_state_the_watchdog_can_see(self):
        """The stall watchdog would otherwise kill exactly the children that
        escalated properly."""
        seen = {}

        def caller():
            for _ in range(100):
                msg = agent_loop.recv_from_inbox(self.parent.id, timeout=0.1)
                if msg and msg.get("kind") == "child-help":
                    seen["stage"] = agent_loop.get_agent(self.child.id).stage
                    seen["watchdog_holds"] = tools._awaiting_caller(self.child.id)
                    agent_loop.answer_child_help(self.parent.id, self.child.id,
                                                 decision="go on")
                    return

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        agent_loop.ask_parent_for_help(self.child.id, self._request(), timeout=10)
        thread.join(timeout=5)
        self.assertEqual(agent_contract.STAGE_WAITING_PARENT, seen.get("stage"))
        self.assertTrue(seen.get("watchdog_holds"))
        # ...and the stage is handed back afterwards, not left parked.
        self.assertNotEqual(agent_contract.STAGE_WAITING_PARENT,
                            agent_loop.get_agent(self.child.id).stage)

    def test_an_unanswered_child_is_released_rather_than_hung(self):
        started = time.time()
        result = agent_loop.ask_parent_for_help(self.child.id, self._request(),
                                                timeout=1)
        self.assertLess(time.time() - started, 8)
        self.assertFalse(result["answered"])
        self.assertEqual("timeout", result["reason"])
        self.assertIn("own judgement", result["guidance"])

    def test_a_caller_parked_in_a_barrier_is_not_waited_on(self):
        """It cannot read its inbox while it blocks, so the timeout would be
        spent for an answer that could never arrive."""
        self.parent.status = "waiting"
        started = time.time()
        result = agent_loop.ask_parent_for_help(self.child.id, self._request(),
                                                timeout=30)
        self.assertLess(time.time() - started, 2)
        self.assertFalse(result["answered"])
        self.assertEqual("caller_blocked", result["reason"])

    def test_the_root_agent_has_nobody_to_ask(self):
        result = agent_loop.ask_parent_for_help(self.parent.id, self._request(),
                                                timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("no caller", result["error"])

    def test_unrelated_inbox_traffic_is_preserved_while_waiting(self):
        """A message that arrives during the wait must not be swallowed."""
        def noise():
            time.sleep(0.2)
            agent_loop.send_to_agent(self.child.id,
                                     {"from": "someone", "kind": "note",
                                      "text": "unrelated"})
            time.sleep(0.2)
            agent_loop.answer_child_help(self.parent.id, self.child.id,
                                         decision="ok")

        thread = threading.Thread(target=noise, daemon=True)
        thread.start()
        result = agent_loop.ask_parent_for_help(self.child.id, self._request(),
                                                timeout=10)
        thread.join(timeout=5)
        self.assertTrue(result["answered"])
        kept = agent_loop.drain_inbox(self.child.id)
        self.assertIn("note", [m.get("kind") for m in kept])

    def test_only_the_caller_may_answer(self):
        stranger = agent_loop.register_agent(name="stranger", role="pool")
        self.addCleanup(agent_loop.unregister_agent, stranger.id)
        result = agent_loop.answer_child_help(stranger.id, self.child.id,
                                              decision="do as I say")
        self.assertFalse(result["ok"])
        self.assertIn("not your child", result["error"])


class HelpToolTests(unittest.TestCase):
    def setUp(self):
        self.parent = agent_loop.register_agent(name="tool-parent",
                                                role="primary")
        self.addCleanup(agent_loop.unregister_agent, self.parent.id)
        self.child = agent_loop.register_agent(name="tool-child", depth=1,
                                               role="subagent",
                                               parent_id=self.parent.id)
        self.addCleanup(agent_loop.unregister_agent, self.child.id)
        self.parent.status = "running"

    def test_ask_parent_returns_the_decision_to_the_child(self):
        def caller():
            for _ in range(100):
                msg = agent_loop.recv_from_inbox(self.parent.id, timeout=0.1)
                if msg and msg.get("kind") == "child-help":
                    tools._bi_agent_answer(
                        {"agent_id": self.child.id,
                         "decision": "skip the shell step",
                         "guidance": "note it in your findings"},
                        tools.ToolCtx(agent_id=self.parent.id))
                    return

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        result = tools._bi_agent_ask_parent(
            {"question": "can I skip the shell step?"},
            tools.ToolCtx(agent_id=self.child.id))
        thread.join(timeout=5)
        self.assertTrue(result["ok"])
        self.assertIn("skip the shell step", result["result"])
        self.assertIn("note it in your findings", result["result"])

    def test_answering_a_stranger_is_refused_through_the_tool(self):
        other = agent_loop.register_agent(name="tool-other", role="pool")
        self.addCleanup(agent_loop.unregister_agent, other.id)
        result = tools._bi_agent_answer(
            {"agent_id": other.id, "decision": "x"},
            tools.ToolCtx(agent_id=self.child.id))
        self.assertFalse(result["ok"])

    def test_the_question_reaches_the_parent_prompt_as_an_action_it_can_take(self):
        rendered = agent_loop._format_parallel_results([{
            "from": "rev-atlas", "kind": "child-help",
            "question": "the task wants a shell command I cannot run",
            "blocker": "shell.exec not in scope",
            "needed_capabilities": ["shell.exec"],
            "options": ["widen my tools", "accept a partial result"],
        }])
        self.assertIn("needs a decision from you", rendered)
        self.assertIn("shell.exec", rendered)
        self.assertIn("agent_answer(agent_id='rev-atlas'", rendered)
        self.assertIn("It is WAITING", rendered)


class RefusalPointsAtTheCallerTests(unittest.TestCase):
    """A refused capability should surface the move the child was not making.

    The child that hit `shell.exec is not allowed for role` had two honest
    options and was only ever shown one of them, so it worked around the wall
    for the rest of its budget and reported the wall after it was too late to
    act on.
    """

    def setUp(self):
        self.parent = agent_loop.register_agent(name="hint-parent",
                                                role="primary")
        self.addCleanup(agent_loop.unregister_agent, self.parent.id)
        self.child = agent_loop.register_agent(name="hint-child", depth=1,
                                               role="subagent",
                                               parent_id=self.parent.id)
        self.addCleanup(agent_loop.unregister_agent, self.child.id)

    def _refuse(self, state, error="shell.exec is not allowed for role reviewer"):
        block = {"ok": False, "error": error, "tool": "shell.exec"}
        agent_loop._record_capability_gap(state, "shell.exec", block)
        return agent_loop._suggest_escalation(block, state, 1, self.child)

    def test_a_refused_child_is_told_it_can_ask(self):
        state = {}
        refusal = self._refuse(state)
        self.assertIn("agent_ask_parent", refusal["error"])
        self.assertIn("not allowed for role", refusal["error"])

    def test_the_suggestion_is_made_once_per_kind_not_every_call(self):
        state = {}
        first = self._refuse(state)
        second = self._refuse(state)
        self.assertIn("agent_ask_parent", first["error"])
        self.assertNotIn("agent_ask_parent", second["error"])

    def test_the_root_agent_is_not_told_to_ask_anyone(self):
        state = {}
        block = {"ok": False, "error": "shell.exec is not allowed", "tool": "shell.exec"}
        agent_loop._record_capability_gap(state, "shell.exec", block)
        kept = agent_loop._suggest_escalation(block, state, 0, self.parent)
        self.assertNotIn("agent_ask_parent", kept["error"])

    def test_the_capability_gap_is_still_recorded_for_the_final_report(self):
        state = {}
        self._refuse(state)
        self.assertEqual([{"tool": "shell.exec", "kind": "role_denied",
                           "reason": "shell.exec is not allowed for role reviewer"}],
                         state["_capability_gaps"])

    def test_a_scope_filter_can_never_remove_the_way_to_ask(self):
        """The child most likely to need this is the one a scope filter would
        silence."""
        import agent_roles
        self.assertTrue(
            agent_roles.is_tool_allowed_for_role("agent.ask_parent", "reviewer"))
        self.assertIn("agent.ask_parent", agent_loop._PROTOCOL_TOOLS)
        self.assertTrue(agent_loop._tool_in_scope("agent.ask_parent",
                                                  ["fs.read", "fs.grep"]))


if __name__ == "__main__":
    unittest.main()
