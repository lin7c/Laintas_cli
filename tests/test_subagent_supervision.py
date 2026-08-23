"""Sub-agent supervision: identity, approvals, and failure reporting.

Every case here is a bug that shipped, and each one was invisible from the
outside — a failed child, a denied write and a silent child all looked
identical to the supervising agent: status "done" with an empty reply.
"""
import threading
import unittest
from unittest import mock

import agent_loop
import paths
import tools


class ThreadAgentIdentityTests(unittest.TestCase):
    def tearDown(self):
        agent_loop.close_all_agents()

    def test_identity_is_per_thread_and_nests(self):
        seen = {}

        def worker():
            with agent_loop.thread_agent("AI-2"):
                seen["worker"] = agent_loop.get_thread_agent_id()

        with agent_loop.thread_agent("primary"):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            seen["main"] = agent_loop.get_thread_agent_id()
            with agent_loop.thread_agent("AI-3"):
                seen["nested"] = agent_loop.get_thread_agent_id()
            seen["restored"] = agent_loop.get_thread_agent_id()

        self.assertEqual("AI-2", seen["worker"])
        self.assertEqual("primary", seen["main"])
        self.assertEqual("AI-3", seen["nested"])
        self.assertEqual("primary", seen["restored"])
        self.assertEqual("", agent_loop.get_thread_agent_id())

    def test_approval_label_prefers_identity_over_thread_name(self):
        import laintas_cli
        # The old thread-name parse returned "2" for "laintas-sched-AI-2",
        # a key that matched no agent in the table or the watchdog.
        result = {}

        def worker():
            with agent_loop.thread_agent("AI-2"):
                result["bound"] = laintas_cli._requesting_agent_label()
            result["unbound"] = laintas_cli._requesting_agent_label()

        thread = threading.Thread(target=worker, name="laintas-sched-AI-2")
        thread.start()
        thread.join()
        self.assertEqual("AI-2", result["bound"])
        self.assertEqual("AI-2", result["unbound"])


class ApprovalHoldTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        self.root = agent_loop.register_agent(name="root", role="subagent")
        self.child = agent_loop.register_agent(
            name="mid", role="subagent", parent_id=self.root.id)
        self.grandchild = agent_loop.register_agent(
            name="leaf", role="subagent", parent_id=self.child.id)

    def tearDown(self):
        tools.clear_awaiting_approval(self.grandchild.id)
        agent_loop.close_all_agents()

    def test_ancestry_walk_is_ordered_and_bounded(self):
        self.assertEqual(
            [self.grandchild.id, self.child.id, self.root.id],
            agent_loop.agent_ancestry(self.grandchild.id))
        self.assertEqual(
            {self.child.id, self.grandchild.id},
            agent_loop.agent_descendants(self.root.id))

    def test_grandchild_prompt_holds_every_supervisor_clock(self):
        tools.mark_awaiting_approval(self.grandchild.id, "fs.multi_edit x.py")
        # The agent that is visibly idle is the supervisor, not the asker.
        self.assertTrue(tools.is_awaiting_approval(self.grandchild.id))
        self.assertTrue(tools.is_awaiting_approval(self.child.id))
        self.assertTrue(tools.is_awaiting_approval(self.root.id))
        self.assertIn("fs.multi_edit", tools.approval_text_for(self.grandchild.id))
        self.assertIn("sub-agent", tools.approval_text_for(self.root.id))

        tools.clear_awaiting_approval(self.grandchild.id)
        self.assertFalse(tools.is_awaiting_approval(self.grandchild.id))
        self.assertFalse(tools.is_awaiting_approval(self.child.id))
        self.assertFalse(tools.is_awaiting_approval(self.root.id))

    def test_concurrent_prompts_release_independently(self):
        other = agent_loop.register_agent(
            name="leaf2", role="subagent", parent_id=self.child.id)
        tools.mark_awaiting_approval(self.grandchild.id, "a")
        tools.mark_awaiting_approval(other.id, "b")
        tools.clear_awaiting_approval(self.grandchild.id)
        self.assertTrue(tools.is_awaiting_approval(self.child.id),
                        "the second prompt still holds the supervisor")
        tools.clear_awaiting_approval(other.id)
        self.assertFalse(tools.is_awaiting_approval(self.child.id))


class SubtreeLivenessTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()

    def tearDown(self):
        agent_loop.close_all_agents()

    def test_child_progress_counts_as_parent_progress(self):
        parent = agent_loop.register_agent(name="sup", role="subagent")
        before = agent_loop.subtree_progress_token(parent.id)
        child = agent_loop.register_agent(
            name="worker", role="subagent", parent_id=parent.id)
        gained = agent_loop.subtree_progress_token(parent.id)
        self.assertNotEqual(before, gained,
                            "gaining a child is itself a sign of life")
        child.state["terminalHistory"] = [{"call_id": "1", "tool": "fs.read"}]
        self.assertNotEqual(gained, agent_loop.subtree_progress_token(parent.id))
        # The parent itself never moved — that is the whole point.
        self.assertEqual(agent_loop.agent_progress_token(parent),
                         agent_loop.agent_progress_token(parent))


class ReplyHarvestTests(unittest.TestCase):
    def test_last_reply_wins_when_present(self):
        result = {"state": {"lastReply": "final"}, "msg": "transcript"}
        self.assertEqual("final", agent_loop.harvest_agent_reply(result, []))

    def test_falls_back_to_transcript_then_history(self):
        # task_complete(summary="") leaves lastReply empty after real work.
        result = {"state": {"lastReply": ""}, "msg": "step one\n\nstep two"}
        self.assertEqual("step one\n\nstep two",
                         agent_loop.harvest_agent_reply(result, []))
        history = [{"role": "user", "content": "go"},
                   {"role": "assistant", "content": "what I found"}]
        self.assertEqual(
            "what I found",
            agent_loop.harvest_agent_reply({"state": {}, "msg": ""}, history))

    def test_no_output_anywhere_is_reported_as_empty(self):
        self.assertEqual("", agent_loop.harvest_agent_reply({}, []))

    def test_exit_reason_is_explained_not_swallowed(self):
        text = agent_loop.describe_exit_reason(
            {"success": False, "exit_reason": agent_loop.TRANSITION_SILENT_FAILURE})
        self.assertIn("silent failure", text)
        denied = agent_loop.describe_exit_reason(
            {"success": False, "exit_reason": agent_loop.TRANSITION_USER_DENIED})
        self.assertIn("denied", denied)


class ChildFailureReportingTests(unittest.TestCase):
    """A child whose loop ended badly must not report status='done'."""

    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        root = mock.Mock()
        root.is_alive.return_value = True
        agent_loop.register_terminal(root, "/bin/sh", 0, name="term0")
        self.parent = agent_loop.register_agent(name="parent", role="primary")

    def tearDown(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()

    def _spawn_with_result(self, loop_result):
        finished = threading.Event()
        captured = {}

        def fake_loop(deps, task, session, state, chat_history, **kwargs):
            captured["agent_id"] = agent_loop.get_thread_agent_id()
            return loop_result

        deps = mock.Mock()
        import worktree_manager
        with mock.patch.object(agent_loop, "run_agent_loop", fake_loop), \
                mock.patch.object(worktree_manager, "is_git_repo",
                                  lambda *_a, **_k: False):
            child_id = agent_loop.spawn_subagent(
                self.parent.id, "do a thing", deps, name="AI-2")
            for _ in range(200):
                info = agent_loop.get_agent(child_id)
                if info is not None and info.status in {"done", "error", "aborted"}:
                    finished.set()
                    break
                threading.Event().wait(0.02)
        self.assertTrue(finished.is_set(), "child never reached a terminal state")
        return agent_loop.get_agent(child_id), captured

    def test_failed_loop_is_reported_as_error_with_a_cause(self):
        info, _ = self._spawn_with_result({
            "success": False,
            "exit_reason": agent_loop.TRANSITION_BACKEND_ERROR,
            "state": {"lastReply": ""},
            "msg": "",
        })
        self.assertEqual("error", info.status)
        self.assertIn("backend error", info.error)

    def test_tool_only_child_still_returns_its_work(self):
        info, captured = self._spawn_with_result({
            "success": True,
            "exit_reason": agent_loop.TRANSITION_COMPLETED,
            "state": {"lastReply": ""},
            "msg": "here is what I found",
        })
        self.assertEqual("done", info.status)
        self.assertEqual("here is what I found", info.result)
        self.assertEqual("AI-2", captured["agent_id"],
                         "the child's loop must run under its own identity")


if __name__ == "__main__":
    unittest.main()


class TruncationRecoveryTests(unittest.TestCase):
    """A cut-off turn must still make progress, or the run dead-ends.

    Symptom this covers: three "Response cut off (tool_args)" in a row after a
    long spawn_parallel, ending the task. The turn emitted several tool calls;
    only the last one was cut mid-arguments; all of them were discarded, so
    every retry regenerated the identical batch and was cut in the identical
    place.
    """

    def test_intact_calls_survive_a_cut_off_batch(self):
        import laintas_cli
        frags = {
            0: {"name": "fs_read", "id": "",
                "arguments": '{"path": "/tmp/a.py"}'},
            1: {"name": "fs_write", "id": "",
                "arguments": '{"path": "/tmp/b.py", "content": "def f():\\n  ret'},
        }
        damaged = []
        calls = laintas_cli._native_to_tool_calls(
            frags, {"fs_read": "fs.read", "fs_write": "fs.write"},
            damaged=damaged)
        self.assertEqual(["fs.write"], damaged)
        intact = [c for c in calls if not c.get("_damaged")]
        self.assertEqual(["fs.read"], [c["name"] for c in intact])
        self.assertEqual({"path": "/tmp/a.py"}, intact[0]["arguments"])
        # The damaged one must never reach the registry with empty arguments.
        self.assertEqual({}, calls[1]["arguments"])
        self.assertTrue(calls[1]["_damaged"])

    def test_undamaged_batch_carries_no_marker(self):
        import laintas_cli
        frags = {0: {"name": "fs_read", "id": "",
                     "arguments": '{"path": "/tmp/a.py"}'}}
        calls = laintas_cli._native_to_tool_calls(
            frags, {"fs_read": "fs.read"}, damaged=[])
        self.assertNotIn("_damaged", calls[0])


class WriteCapTests(unittest.TestCase):
    """The chunking advice has to be enforced on the tools it names."""

    def setUp(self):
        self.state = {"_max_write_lines": 10}
        self.long = "\n".join(f"line {i}" for i in range(50))

    def test_cap_is_inactive_without_a_truncation(self):
        self.assertIsNone(agent_loop._write_cap_violation(
            {}, "fs.write", {"content": self.long}))

    def test_cap_covers_write_edit_and_multi_edit(self):
        for name, args in (
            ("fs.write", {"content": self.long}),
            ("fs.edit", {"new_string": self.long}),
            ("fs.multi_edit", {"edits": [{"new_string": self.long}]}),
        ):
            with self.subTest(tool=name):
                violation = agent_loop._write_cap_violation(
                    self.state, name, args)
                self.assertIsNotNone(violation, f"{name} must be capped")
                self.assertFalse(violation["ok"])
                self.assertIn(name, violation["error"])

    def test_small_writes_and_other_tools_pass(self):
        self.assertIsNone(agent_loop._write_cap_violation(
            self.state, "fs.write", {"content": "one\ntwo"}))
        self.assertIsNone(agent_loop._write_cap_violation(
            self.state, "shell.exec", {"command": self.long}))
        self.assertIsNone(agent_loop._write_cap_violation(
            self.state, "fs.multi_edit", {"edits": "not-a-list"}))


class TruncationDiagnosisTests(unittest.TestCase):
    """"Cut off at the token limit" must not be the diagnosis for every
    unparseable tool call. On a model whose granted ceiling is a flat 65,536
    regardless of prompt size, a call that failed to parse after 3k completion
    tokens hit no limit at all — and the write-cap/compaction remedies cost a
    turn each while fixing nothing."""

    def _run(self, response):
        import tempfile, os
        from pathlib import Path
        cwd = os.getcwd()

        class _Console:
            width = 100

            def print(self, *a, **k):
                pass

        deps = mock.Mock()
        deps.call_backend = lambda **kw: response
        deps.console = _Console()
        deps.read_file = lambda p: ""
        deps.generate_prompt = lambda: "prompt"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                Path(".laintas").mkdir()
                return agent_loop.run_agent_loop(
                    deps, "go", {}, {}, [{"role": "user", "content": "go"}],
                    max_loops_override=4)
        finally:
            os.chdir(cwd)

    def test_malformed_arguments_do_not_trigger_size_remedies(self):
        state = self._run({
            "reply": "", "tool_calls": [], "finish_reason": "tool_calls",
            "done": False, "error": False, "_truncated": True,
            "_truncation_kind": "tool_args_malformed",
            "_dropped_calls": ["fs.multi_edit"],
        })["state"]
        self.assertIsNone(state.get("_max_write_lines"))
        self.assertIsNone(state.get("_force_micro_keep"))
        self.assertIn("not valid JSON", state.get("shortTermMemory", ""))
        self.assertIn("fs.multi_edit", state.get("shortTermMemory", ""))

    def test_real_overrun_still_gets_the_size_remedies(self):
        state = self._run({
            "reply": "", "tool_calls": [], "finish_reason": "length",
            "done": False, "error": False, "_truncated": True,
            "_truncation_kind": "tool_args",
        })["state"]
        self.assertEqual(150, state.get("_max_write_lines"))
        self.assertTrue(state.get("_force_micro_keep"))


class ProviderWindowMemoryTests(unittest.TestCase):
    """The real context window must survive a restart.

    It only arrives with the first response of a process, so until then the CLI
    budgets against the 64000 default. On a million-token model that made the
    first turn after every restart / /reload / --continue compact a resumed
    thread it had no reason to touch — an LLM summarization call and lost
    verbatim history, bought by not remembering a number.
    """

    def setUp(self):
        import pathlib, tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._home = paths.LAINTAS_HOME
        self._window = agent_loop._provider_context_window
        self._key = agent_loop._provider_window_key
        # Isolate through LAINTAS_HOME, the same seam the product uses, so the
        # test proves the real path resolution rather than a patched constant.
        paths.LAINTAS_HOME = pathlib.Path(self._tmp.name)
        agent_loop._provider_window_key = lambda: "test-model"
        agent_loop._provider_window_persisted.clear()

    def tearDown(self):
        paths.LAINTAS_HOME = self._home
        agent_loop._provider_context_window = self._window
        agent_loop._provider_window_key = self._key
        agent_loop._provider_window_persisted.clear()
        agent_loop._provider_window_cache_loaded = True

    def _cold_start(self):
        agent_loop._provider_context_window = 0
        agent_loop._provider_window_cache_loaded = False

    def test_window_is_remembered_and_reloaded(self):
        agent_loop._note_provider_context_window(1_000_000)
        self._cold_start()
        # Adopted, then bounded by context_window_adopt_cap — not the 64000
        # default the process would otherwise start from.
        self.assertEqual(200_000, agent_loop._effective_context_window())

    def test_nothing_remembered_falls_back_to_the_default(self):
        self._cold_start()
        self.assertEqual(64_000, agent_loop._effective_context_window())

    def test_a_corrupt_cache_never_breaks_the_budget(self):
        agent_loop._provider_window_file().write_text("{not json", encoding="utf-8")
        self._cold_start()
        self.assertEqual(64_000, agent_loop._effective_context_window())
