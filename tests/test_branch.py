"""Branches: one delegated unit of work, supervised, with a closed loop.

Supervision used to live inside `spawn_parallel`'s blocking display loop — the
per-child stall clock, the wrap-up nudge, the cut-off, the partial rescue. When
the fan-out became asynchronous by default, deleting the loop deleted the
watchdog with it: an async child that wedged had no bound at all. These tests
pin the separation that fixes it (the supervisor runs on its own thread) and
the closed loop that finishes it (every member ends with an outcome).
"""
import threading
import time
import unittest
from unittest import mock

import agent_loop
import branch
import tools


class _FakeAgent:
    """The slice of AgentInfo a supervisor reads."""

    def __init__(self, agent_id, status="running"):
        self.id = agent_id
        self.status = status
        self.state = {}
        self.stage = "running"
        self.error = ""
        self.result = ""
        self.last_reply = ""
        self.contract = None
        self.verification = {}
        self.aborted = False
        self.inbox_messages = []


class _FakeRuntime:
    def __init__(self):
        self.agents = {}
        self.blocked = set()
        self.token = {}

    def get_agent(self, agent_id):
        return self.agents.get(agent_id)

    def abort_agent(self, agent_id):
        info = self.agents.get(agent_id)
        if info is not None:
            info.aborted = True
            info.status = "aborted"
        return True

    def send_to_agent(self, agent_id, message):
        info = self.agents.get(agent_id)
        if info is not None:
            info.inbox_messages.append(message)
        return True

    def subtree_progress_token(self, agent_id):
        return self.token.get(agent_id)

    def is_blocked_on_a_decision(self, agent_id):
        return agent_id in self.blocked


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _FakeRuntime()
        self._real = branch._runtime
        branch.bind_runtime(self.runtime)
        self.addCleanup(branch.bind_runtime, self._real)
        self.addCleanup(branch._BRANCHES.clear)
        # A branch whose owner has disappeared is drained by design, so the
        # owner has to exist for any test that is not about that rule.
        self.runtime.agents["owner"] = _FakeAgent("owner")
        self.runtime.agents["parent"] = _FakeAgent("parent")

    def _agent(self, agent_id, status="running"):
        info = _FakeAgent(agent_id, status)
        self.runtime.agents[agent_id] = info
        return info

    def _branch(self, ids, **budget):
        return branch.open_branch("owner", "parallel", ids,
                                  budget=branch.Budget(**budget))

    def _wait_until(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_wedged_member_is_stopped_even_though_nobody_is_waiting(self):
        """The failure the async default introduced: supervision was in the
        display loop, so an unwatched child had no stall bound at all."""
        self._agent("c1")
        b = self._branch(["c1"], stall_seconds=0.3)
        self.assertTrue(self._wait_until(lambda: b.status == branch.STATUS_CLOSED))
        member = b.members["c1"]
        self.assertEqual(branch.OUTCOME_ABORTED, member.outcome)
        self.assertIn("no observable progress", member.detail)
        self.assertTrue(self.runtime.agents["c1"].aborted)

    def test_a_member_that_keeps_working_is_left_alone(self):
        info = self._agent("c1")
        b = self._branch(["c1"], stall_seconds=0.6)
        for i in range(6):
            info.state["terminalHistory"] = [
                {"call_id": f"call{j}", "tool": "fs.read", "command": "x.py"}
                for j in range(i + 1)]
            time.sleep(0.15)
        self.assertEqual(branch.STATUS_OPEN, b.status)
        self.assertFalse(self.runtime.agents["c1"].aborted)
        self.assertGreaterEqual(b.members["c1"].tool_calls, 5)

    def test_the_wrap_up_nudge_arrives_before_the_cutoff(self):
        """So a member hands back a partial conclusion instead of nothing."""
        info = self._agent("c1")
        with mock.patch.object(branch, "WRAP_UP_LEAD_SECONDS", 0.25):
            b = self._branch(["c1"], stall_seconds=0.5)
            self.assertTrue(self._wait_until(lambda: info.inbox_messages))
            self.assertEqual("budget_warning", info.inbox_messages[0]["kind"])
            self.assertTrue(self._wait_until(
                lambda: b.status == branch.STATUS_CLOSED))

    def test_a_cut_off_member_keeps_the_partial_answer_it_had(self):
        info = self._agent("c1")
        info.state["lastReply"] = "found one leak so far"
        b = self._branch(["c1"], stall_seconds=0.3)
        self.assertTrue(self._wait_until(lambda: b.status == branch.STATUS_CLOSED))
        self.assertEqual("found one leak so far", b.members["c1"].partial)

    def test_waiting_on_a_person_or_a_caller_is_not_stalling(self):
        """The watchdog would otherwise fire hardest on the members that
        behaved correctly."""
        self._agent("c1")
        self.runtime.blocked.add("c1")
        b = self._branch(["c1"], stall_seconds=0.3)
        time.sleep(0.8)
        self.assertEqual(branch.STATUS_OPEN, b.status)
        self.assertFalse(self.runtime.agents["c1"].aborted)

    def test_a_delegating_member_lives_on_its_subtree_progress(self):
        """It sits on one open spawn call while its children do the work."""
        self._agent("c1")
        self.runtime.token["c1"] = "t0"
        b = self._branch(["c1"], stall_seconds=0.5)
        for i in range(5):
            self.runtime.token["c1"] = f"t{i + 1}"
            time.sleep(0.12)
        self.assertEqual(branch.STATUS_OPEN, b.status)

    def test_a_queued_member_is_not_charged_for_waiting_for_a_slot(self):
        self._agent("c1", status="queued")
        b = self._branch(["c1"], stall_seconds=0.3)
        time.sleep(0.7)
        self.assertEqual(branch.STATUS_OPEN, b.status)


    def test_a_branch_being_filled_is_not_mistaken_for_a_finished_one(self):
        """Measured live: a fan-out registers members as each child spawns, and
        the supervisor closed the branch in the millisecond between opening it
        and the first member arriving — leaving both children running outside
        any branch, unsupervised, with the owner free to walk away."""
        b = branch.open_branch("owner", "parallel", [],
                               budget=branch.Budget(stall_seconds=30))
        self.assertFalse(b.sealed)
        time.sleep(0.4)
        self.assertEqual(branch.STATUS_OPEN, b.status,
                         "an unfilled branch was closed as 'all members settled'")
        self.runtime.agents["late"] = _FakeAgent("late")
        b.members["late"] = branch.Member(agent_id="late")
        branch.seal(b.branch_id)
        self.assertTrue(b.sealed)
        branch.drain(b.branch_id, "test over")

    def test_an_unsealed_branch_is_eventually_closed_anyway(self):
        """An opener that died mid-fan-out must not block its owner forever."""
        with mock.patch.object(branch, "SEAL_TIMEOUT_SECONDS", 0.3):
            b = branch.open_branch("owner", "parallel", [],
                                   budget=branch.Budget(stall_seconds=30))
            deadline = time.time() + 3
            while time.time() < deadline and b.status != branch.STATUS_CLOSED:
                time.sleep(0.02)
        self.assertEqual(branch.STATUS_CLOSED, b.status)
        self.assertIn("never sealed", b.close_reason)

class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _FakeRuntime()
        self._real = branch._runtime
        branch.bind_runtime(self.runtime)
        self.addCleanup(branch.bind_runtime, self._real)
        self.addCleanup(branch._BRANCHES.clear)
        # A branch whose owner has disappeared is drained by design, so the
        # owner has to exist for any test that is not about that rule.
        self.runtime.agents["owner"] = _FakeAgent("owner")
        self.runtime.agents["parent"] = _FakeAgent("parent")

    def _agent(self, agent_id, status="running"):
        info = _FakeAgent(agent_id, status)
        self.runtime.agents[agent_id] = info
        return info

    def test_every_member_ends_with_one_of_three_outcomes(self):
        good = self._agent("ok")
        bad = self._agent("rejected")
        bad.verification = {"ok": False, "gaps": ["missing output 'report'"]}
        bad.stage = "rejected"
        wedged = self._agent("wedged")
        b = branch.open_branch("owner", "parallel", ["ok", "rejected", "wedged"],
                               budget=branch.Budget(stall_seconds=0.4))
        good.status = "done"
        bad.status = "done"
        deadline = time.time() + 5
        while time.time() < deadline and b.status != branch.STATUS_CLOSED:
            time.sleep(0.02)
        outcomes = {m["agent_id"]: m["outcome"] for m in b.ledger()}
        self.assertEqual({"ok": branch.OUTCOME_VERIFIED,
                          "rejected": branch.OUTCOME_REJECTED,
                          "wedged": branch.OUTCOME_ABORTED}, outcomes)
        self.assertIn("missing output 'report'", b.members["rejected"].detail)
        self.assertNotIn("running", outcomes.values())

    def test_closing_a_branch_settles_whatever_is_left(self):
        """"Still running" is not an outcome anyone can act on."""
        self._agent("c1")
        b = branch.open_branch("owner", "parallel", ["c1"], supervise=False)
        branch.close(b.branch_id, "owner walked away")
        self.assertEqual(branch.OUTCOME_ABORTED, b.members["c1"].outcome)
        self.assertIn("owner walked away", b.members["c1"].detail)
        self.assertEqual(branch.STATUS_CLOSED, b.status)
        self.assertTrue(self.runtime.agents["c1"].aborted)

    def test_an_owner_that_disappears_does_not_leave_members_running(self):
        self._agent("c1")
        b = branch.open_branch("ghost", "parallel", ["c1"],
                               budget=branch.Budget(stall_seconds=30))
        deadline = time.time() + 5
        while time.time() < deadline and b.status != branch.STATUS_CLOSED:
            time.sleep(0.02)
        self.assertEqual(branch.STATUS_CLOSED, b.status)
        self.assertEqual(branch.OUTCOME_ABORTED, b.members["c1"].outcome)

    def test_the_summary_names_what_is_running_and_what_it_is_doing(self):
        info = self._agent("c1")
        info.state["terminalHistory"] = [
            {"call_id": "x", "tool": "fs.read", "command": "agent_loop.py"}]
        b = branch.open_branch("owner", "parallel", [("c1", "review the loop")],
                               budget=branch.Budget(stall_seconds=30))
        deadline = time.time() + 3
        while time.time() < deadline and not b.members["c1"].tool_calls:
            time.sleep(0.02)
        summary = branch.summarize_open("owner")
        self.assertIn(b.branch_id, summary)
        self.assertIn("c1: running", summary)
        self.assertIn("fs.read", summary)
        branch.drain(b.branch_id, "test over")

    def test_a_closed_branch_is_no_longer_open_work(self):
        self._agent("c1")
        b = branch.open_branch("owner", "parallel", ["c1"], supervise=False)
        self.assertEqual([b], branch.open_branches("owner"))
        branch.close(b.branch_id, "done")
        self.assertEqual([], branch.open_branches("owner"))


class CompletionGateTests(unittest.TestCase):
    """A parent may not walk away from work it started."""

    def setUp(self):
        self.runtime = _FakeRuntime()
        self._real = branch._runtime
        branch.bind_runtime(self.runtime)
        self.addCleanup(branch.bind_runtime, self._real)
        self.addCleanup(branch._BRANCHES.clear)
        # A branch whose owner has disappeared is drained by design, so the
        # owner has to exist for any test that is not about that rule.
        self.runtime.agents["owner"] = _FakeAgent("owner")
        self.runtime.agents["parent"] = _FakeAgent("parent")
        info = _FakeAgent("c1")
        self.runtime.agents["c1"] = info
        self.branch = branch.open_branch("parent", "parallel", ["c1"],
                                         supervise=False)

    def test_the_first_attempt_is_refused_with_the_branch_state(self):
        state = {}
        result = tools._bi_task_complete(
            {"summary": "all done"},
            tools.ToolCtx(agent_id="parent", state=state))
        self.assertFalse(result["ok"])
        self.assertTrue(result["_advisory"])
        self.assertIn("still have delegated work running", result["error"])
        self.assertIn(self.branch.branch_id, result["error"])
        self.assertEqual(branch.STATUS_OPEN, self.branch.status)

    def test_the_second_attempt_finishes_and_drains_what_is_left(self):
        """Refusing forever would deadlock the agent the rule disciplines."""
        state = {}
        ctx = tools.ToolCtx(agent_id="parent", state=state)
        tools._bi_task_complete({"summary": "all done"}, ctx)
        result = tools._bi_task_complete({"summary": "all done"}, ctx)
        self.assertTrue(result.get("ok", True), result)
        self.assertEqual(branch.STATUS_CLOSED, self.branch.status)
        self.assertEqual(branch.OUTCOME_ABORTED,
                         self.branch.members["c1"].outcome)
        self.assertTrue(self.runtime.agents["c1"].aborted)

    def test_an_agent_with_no_branches_is_never_stopped(self):
        result = tools._bi_task_complete(
            {"summary": "nothing delegated"},
            tools.ToolCtx(agent_id="someone-else", state={}))
        self.assertTrue(result.get("ok", True), result)

    def test_a_closed_branch_does_not_block_completion(self):
        branch.close(self.branch.branch_id, "collected")
        result = tools._bi_task_complete(
            {"summary": "all done"},
            tools.ToolCtx(agent_id="parent", state={}))
        self.assertTrue(result.get("ok", True), result)


class BranchStatusToolTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _FakeRuntime()
        self._real = branch._runtime
        branch.bind_runtime(self.runtime)
        self.addCleanup(branch.bind_runtime, self._real)
        self.addCleanup(branch._BRANCHES.clear)
        # A branch whose owner has disappeared is drained by design, so the
        # owner has to exist for any test that is not about that rule.
        self.runtime.agents["owner"] = _FakeAgent("owner")
        self.runtime.agents["parent"] = _FakeAgent("parent")
        self.runtime.agents["c1"] = _FakeAgent("c1")
        self.branch = branch.open_branch("parent", "parallel",
                                         [("c1", "review it")], supervise=False)

    def test_it_reports_without_blocking(self):
        result = tools._bi_branch_status(
            {}, tools.ToolCtx(agent_id="parent", state={}))
        self.assertTrue(result["ok"])
        self.assertIn(self.branch.branch_id, result["result"])
        self.assertEqual(1, len(result["branches"]))

    def test_another_agents_branch_is_not_readable(self):
        result = tools._bi_branch_status(
            {"branch_id": self.branch.branch_id},
            tools.ToolCtx(agent_id="stranger", state={}))
        self.assertFalse(result["ok"])
        self.assertIn("another agent", result["error"])

    def test_an_unknown_branch_is_named_as_such(self):
        result = tools._bi_branch_status(
            {"branch_id": "b-nope"}, tools.ToolCtx(agent_id="parent", state={}))
        self.assertFalse(result["ok"])


class EverySpawnPathIsSupervisedTests(unittest.TestCase):
    """Coordination check: a path that spawns without a branch is a path with
    no watchdog. That is exactly how the async default lost its supervision."""

    def setUp(self):
        agent_loop.close_all_agents()
        self.addCleanup(agent_loop.close_all_agents)
        self.addCleanup(branch._BRANCHES.clear)
        self.parent = agent_loop.register_agent(name="sup-parent", role="primary")

    def _deps(self):
        import io
        from rich.console import Console
        from rich.markdown import Markdown
        return agent_loop.LoopDeps(
            read_file=lambda p: None, append_file=lambda p, c: None,
            write_file=lambda p, c: None, strip_ansi=lambda t: t,
            generate_prompt=lambda: "x",
            call_backend=lambda **k: {"reply": "done", "tool_calls": [],
                                      "done": True, "error": False},
            SubTerminalSession=mock.Mock,
            display_command_output=lambda *a, **k: None,
            display_sub_terminal_preview=lambda *a, **k: None,
            display_file_diff=lambda *a, **k: None,
            console=Console(file=io.StringIO(), force_terminal=False),
            Markdown=Markdown)

    def test_a_fan_out_is_born_with_exactly_one_branch(self):
        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(agent_loop, "run_agent_loop",
                                  return_value={"state": {"lastReply": "ok"}}):
            child_ids = agent_loop.spawn_subagents_parallel(
                self.parent.id,
                [{"task": "one"}, {"task": "two"}],
                self._deps(), session={}, events_cb=None)
        self.assertEqual(2, len(child_ids))
        group_ids = {agent_loop.get_agent(c).group_id for c in child_ids}
        self.assertEqual(1, len(group_ids), "members split across batches")
        found = branch.get(group_ids.pop())
        self.assertIsNotNone(found, "a fan-out with no branch has no watchdog")
        self.assertEqual(set(child_ids), set(found.members))

    def test_the_tool_reuses_that_branch_instead_of_opening_a_second(self):
        ctx = tools.ToolCtx(deps=self._deps(), agent_id=self.parent.id,
                            session={}, events_cb=None)
        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(agent_loop, "run_agent_loop",
                                  return_value={"state": {"lastReply": "ok"}}):
            result = tools._bi_spawn_parallel(
                {"tasks": [{"goal": "a"}, {"goal": "b"}]}, ctx)
        self.assertTrue(result["ok"])
        mine = branch.branches_for(self.parent.id)
        self.assertEqual(1, len(mine), "two branches for one fan-out")
        self.assertEqual(result["branch_id"], mine[0].branch_id)

    def test_a_single_spawn_is_a_branch_of_one_and_closes_with_its_barrier(self):
        ctx = tools.ToolCtx(deps=self._deps(), agent_id=self.parent.id,
                            session={}, events_cb=None,
                            spawn_subagent=agent_loop.spawn_subagent)
        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(agent_loop, "run_agent_loop",
                                  return_value={"state": {"lastReply": "ok"}}):
            result = tools._bi_spawn({"goal": "one errand"}, ctx)
        self.assertTrue(result["ok"], result)
        opened = branch.branches_for(self.parent.id)
        self.assertEqual(1, len(opened))
        self.assertEqual("single", opened[0].kind)
        self.assertEqual(branch.STATUS_CLOSED, opened[0].status,
                         "a branch of one must not outlive its own barrier")
        # ...and therefore must not block the caller from finishing.
        self.assertEqual([], branch.open_branches(self.parent.id))


if __name__ == "__main__":
    unittest.main()
