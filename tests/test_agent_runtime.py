import io
import os
import queue
import re
import shlex
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import agent_contract
import agent_persistence
import agent_roles
import tools


class TrainingPrivacyDefaultsTests(unittest.TestCase):
    def test_training_content_capture_is_opt_in(self):
        self.assertIs(agent_loop._DEFAULT_CONFIG["precheck_capture"], False)
        self.assertIs(agent_loop._DEFAULT_CONFIG["redact_capture"], False)
        self.assertIs(agent_loop._DEFAULT_CONFIG["rag_capture"], False)

    def test_local_event_content_is_private(self):
        import event_log
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            self.assertGreater(event_log.append("prompt_admitted", prompt="secret"), 0)
            directory = Path(tmp) / ".laintas"
            event_file = directory / "events.jsonl"
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(event_file.stat().st_mode & 0o777, 0o600)


@contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _deps(response=None):
    payload = response or {
        "reply": "done",
        "tool_calls": [],
        "finish_reason": "stop",
        "done": False,
        "error": False,
    }
    return agent_loop.LoopDeps(
        read_file=lambda path: None,
        append_file=lambda path, content: None,
        write_file=lambda path, content: None,
        strip_ansi=lambda text: text,
        generate_prompt=lambda: "You are a test agent.",
        call_backend=lambda **kwargs: dict(payload),
        SubTerminalSession=mock.Mock,
        display_command_output=lambda *args, **kwargs: None,
        display_sub_terminal_preview=lambda *args, **kwargs: None,
        display_file_diff=lambda *args, **kwargs: None,
        console=Console(file=io.StringIO(), force_terminal=False),
        Markdown=Markdown,
    )


class _FakeInteractiveSession:
    instances = []

    def __init__(self, command, timeout=120, stream_output=False, cwd=None):
        self.command = command
        self.timeout = timeout
        self.cwd = cwd
        self.sent = []
        self.full_output = ""
        self.returncode = -1
        self.alive = False
        self.closed = False
        type(self).instances.append(self)

    def start(self):
        self.alive = True
        self.full_output += "ready\n"

    def send_keys(self, keys):
        self.sent.append(keys)
        self.full_output += keys

    def read_output(self, timeout=0.1):
        return ""

    def is_alive(self):
        return self.alive and not self.closed

    def close(self):
        self.closed = True
        self.alive = False
        self.returncode = 0


class _FiniteBackgroundSession(_FakeInteractiveSession):
    """A job that emits its final bytes and exits during the first wait read."""

    def read_output(self, timeout=0.1):
        if self.alive and "sample-final" not in self.full_output:
            self.full_output += "sample-final\n"
            self.alive = False
            self.returncode = 7
            return "sample-final\n"
        return ""


class AgentSchedulerTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        root = mock.Mock()
        root.is_alive.return_value = True
        agent_loop.register_terminal(root, "/bin/sh", 0, name="term0")
        agent_loop._max_concurrent = 8

    def tearDown(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        agent_loop._max_concurrent = 8

    def test_running_abort_releases_scheduler_lease_on_exit(self):
        child = agent_loop.register_agent(name="child", role="subagent")
        agent_loop.mark_agent_running(child.id)

        self.assertEqual(agent_loop._running_count, 1)
        self.assertTrue(agent_loop.abort_agent(child.id))
        self.assertEqual(child.status, "running")

        agent_loop.mark_agent_finished(child.id, result="stopped")

        self.assertEqual(child.status, "aborted")
        self.assertFalse(child.slot_held)
        self.assertEqual(agent_loop._running_count, 0)

    def test_abort_cascades_to_descendants(self):
        # Build parent -> child -> grandchild tree.
        parent = agent_loop.register_agent(name="parent", role="subagent")
        child = agent_loop.register_agent(
            name="child", role="subagent", parent_id=parent.id)
        grandchild = agent_loop.register_agent(
            name="grandchild", role="subagent", parent_id=child.id)
        self.assertFalse(parent.abort_event.is_set())
        self.assertFalse(child.abort_event.is_set())
        self.assertFalse(grandchild.abort_event.is_set())
        self.assertTrue(agent_loop.abort_agent(parent.id))
        self.assertTrue(parent.abort_event.is_set())
        self.assertTrue(child.abort_event.is_set(),
                        "abort_agent must cascade to children")
        self.assertTrue(grandchild.abort_event.is_set(),
                        "abort_agent must cascade to grandchildren")
        # Sibling not under parent must be untouched.
        sibling = agent_loop.register_agent(name="sibling", role="subagent")
        self.assertFalse(sibling.abort_event.is_set())

    def test_aborted_queued_agent_never_starts(self):
        agent_loop._max_concurrent = 1
        holder = agent_loop.register_agent(name="holder")
        child = agent_loop.register_agent(name="queued")
        agent_loop.mark_agent_running(holder.id)
        called = []
        done = threading.Event()

        def start(ok):
            called.append(ok)
            done.set()

        agent_loop.schedule_agent(child.id, start)
        self.assertEqual(child.status, "queued")
        agent_loop.abort_agent(child.id)
        agent_loop.mark_agent_finished(holder.id)

        self.assertTrue(done.wait(1))
        self.assertEqual(called, [False])
        self.assertEqual(child.status, "aborted")
        self.assertEqual(agent_loop._running_count, 0)

    def test_close_resets_scheduler_and_unblocks_queue(self):
        agent_loop._max_concurrent = 1
        holder = agent_loop.register_agent(name="holder")
        child = agent_loop.register_agent(name="queued")
        agent_loop.mark_agent_running(holder.id)
        done = threading.Event()
        values = []
        agent_loop.schedule_agent(
            child.id, lambda ok: (values.append(ok), done.set()))

        agent_loop.close_all_agents()

        self.assertTrue(done.wait(1))
        self.assertEqual(values, [False])
        self.assertEqual(agent_loop._running_count, 0)
        self.assertEqual(agent_loop._wait_queue, [])

    def test_named_child_never_replaces_existing_agent(self):
        parent = agent_loop.register_agent(name="parent", role="primary")
        parent.state["_session_id"] = "shared-session"
        parent.state["_task_cwd"] = "/shared/task-control"
        employee = agent_loop.register_agent(name="worker", role="pool")
        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(
                    agent_loop, "run_agent_loop",
                    return_value={"state": {"lastReply": "ok"}}):
            child_id = agent_loop.spawn_subagent(
                parent.id, "task", _deps(), name="worker")
            info = agent_loop.wait_for_agent(child_id, timeout=2)

        self.assertEqual(employee.id, "worker")
        self.assertIs(agent_loop.get_agent("worker"), employee)
        self.assertEqual(child_id, "worker-2")
        self.assertEqual(info.status, "done")
        self.assertEqual(info.state["_session_id"], "shared-session")
        self.assertEqual(info.state["_task_cwd"], "/shared/task-control")

    def test_worktree_failure_is_explicit_not_shared_cwd_fallback(self):
        parent = agent_loop.register_agent(name="parent", role="primary")
        with mock.patch("worktree_manager.is_git_repo", return_value=True), \
                mock.patch("worktree_manager.create_isolated_worktree",
                           side_effect=RuntimeError("no worktree")):
            child_id = agent_loop.spawn_subagent(parent.id, "task", _deps())

        child = agent_loop.get_agent(child_id)
        self.assertEqual(child.status, "error")
        self.assertIn("Worktree isolation failed", child.error)
        self.assertIsNone(child.thread)
        message = agent_loop.recv_from_inbox(parent.id)
        self.assertEqual(message["kind"], "child-error")

    def test_contract_failure_is_escalated_once_for_parent_decision(self):
        parent = agent_loop.register_agent(name="contract-parent", role="primary")
        contract = agent_contract.normalize({
            "outputs": [{"name": "report", "type": "string"}],
            "acceptance": [{"kind": "min_length", "output": "report",
                            "value": 20}],
        })

        def child_run(_deps, _task, _session, state, _history, **_kwargs):
            state["_submitted_outputs"] = {"report": "too short"}
            state["_capability_gaps"] = [{
                "tool": "shell.exec", "kind": "unavailable",
                "reason": "not available to reviewer",
            }]
            return {
                "success": True, "msg": "partial review",
                "exit_reason": "completed", "turn_status": "completed",
                "task_status": "completed", "completion_source": "task_complete",
            }

        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(agent_loop, "run_agent_loop",
                                  side_effect=child_run) as run:
            child_id = agent_loop.spawn_subagent(
                parent.id, "review it", _deps(), contract=contract)
            child = agent_loop.get_agent(child_id)
            child.thread.join(timeout=5)
            info = agent_loop.get_agent(child_id)

        self.assertEqual(1, run.call_count, "runtime must not auto-retry")
        self.assertFalse(child.thread.is_alive())
        self.assertEqual("error", info.status)
        message = agent_loop.recv_from_inbox(parent.id)
        self.assertEqual("child-error", message["kind"])
        self.assertEqual("contract_rejected", message["failure"]["kind"])
        self.assertEqual("parent_decides", message["retry_policy"])
        self.assertEqual({"report": "too short"}, message["outputs"])
        self.assertIn("needs 20", message["gaps"][0])
        self.assertEqual("shell.exec", message["capability_gaps"][0]["tool"])

    def test_structured_parallel_uses_canonical_spawner(self):
        parent = agent_loop.register_agent(name="parent", role="primary")
        ctx = tools.ToolCtx(
            deps=_deps(), agent_id=parent.id, session={}, events_cb=None)
        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(
                    agent_loop, "run_agent_loop",
                    return_value={"state": {"lastReply": "complete"}}):
            result = tools._bi_spawn_parallel({"tasks": [
                {"goal": "first"}, {"goal": "second", "hint": "be brief"},
            ], "wait": True}, ctx)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["child_ids"]), 2)
        children = [agent_loop.get_agent(cid) for cid in result["child_ids"]]
        self.assertTrue(all(child.status == "done" for child in children))
        self.assertEqual(len({child.group_id for child in children}), 1)

    def test_spawn_parallel_is_async_by_default(self):
        parent = agent_loop.register_agent(name="async-parent", role="primary")
        ctx = tools.ToolCtx(
            deps=_deps(), agent_id=parent.id, session={}, events_cb=None)
        release = threading.Event()

        def _blocked_until_released(*_args, **_kwargs):
            release.wait(timeout=5)
            return {"state": {"lastReply": "arrived through inbox"}}

        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(agent_loop, "run_agent_loop",
                                  side_effect=_blocked_until_released):
            started = time.time()
            result = tools._bi_spawn_parallel({
                "tasks": [{"goal": "work in background"}],
            }, ctx)
            elapsed = time.time() - started
            child = agent_loop.get_agent(result["child_ids"][0])
            try:
                self.assertTrue(result["ok"])
                self.assertFalse(result["waiting"])
                # The batch IS a branch now: one object with the budget, the
                # supervisor and the outcome ledger on it.
                self.assertTrue(result["batch_id"].startswith("b-"))
                self.assertEqual(result["batch_id"], result["branch_id"])
                self.assertLess(elapsed, 1.0)
                self.assertIn(child.status, {"queued", "running"})
            finally:
                release.set()
                child.thread.join(timeout=5)

        self.assertFalse(child.thread.is_alive())
        message = agent_loop.recv_from_inbox(parent.id)
        self.assertEqual("child-done", message["kind"])

    def test_await_spawns_can_select_one_parallel_batch(self):
        parent = agent_loop.register_agent(name="batch-parent", role="primary")
        selected = agent_loop.register_agent(
            name="selected", role="subagent", parent_id=parent.id)
        unrelated = agent_loop.register_agent(
            name="unrelated", role="subagent", parent_id=parent.id)
        selected.group_id = "group-selected"
        unrelated.group_id = "group-other"
        agent_loop.mark_agent_finished(selected.id, result="selected result")
        agent_loop.mark_agent_finished(unrelated.id, result="unrelated result")
        ctx = tools.ToolCtx(
            deps=_deps(), agent_id=parent.id, session={}, events_cb=None)

        result = tools._bi_await_spawns({"batch_id": "group-selected"}, ctx)

        self.assertTrue(result["ok"])
        self.assertIn("selected result", result["result"])
        self.assertNotIn("unrelated result", result["result"])

    def test_finished_task_children_are_bounded(self):
        for index in range(105):
            child = agent_loop.register_agent(
                name=f"finished-{index}", role="subagent")
            agent_loop.mark_agent_finished(child.id, result="done")
        retained = [a for a in agent_loop.get_all_agents()
                    if a.role == "subagent" and a.status == "done"]
        self.assertEqual(len(retained), 100)

    def test_wait_for_agent_stall_mode_follows_progress(self):
        """stall_seconds turns `timeout` from a total budget into a silence
        budget: an agent that keeps working is waited on, one that goes quiet
        is not."""
        child = agent_loop.register_agent(name="worker", role="subagent")
        child.status = "running"
        stop = threading.Event()

        def _work():
            for step in range(8):        # 8 * 0.25s = 2s, well past the 1s window
                if stop.is_set():
                    return
                child.state.setdefault("terminalHistory", []).append(
                    {"tool": "fs.read", "call_id": f"c{step}"})
                time.sleep(0.25)
            child.status = "done"

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        info = agent_loop.wait_for_agent(child.id, stall_seconds=1.0)
        stop.set()
        self.assertIsNotNone(info)       # progress kept extending the wait
        self.assertEqual(info.status, "done")

        quiet = agent_loop.register_agent(name="quiet", role="subagent")
        quiet.status = "running"
        started = time.time()
        self.assertIsNone(agent_loop.wait_for_agent(quiet.id, stall_seconds=0.5))
        self.assertLess(time.time() - started, 5.0)

    def test_wait_for_agent_holds_the_clock_while_queued(self):
        """A child with no concurrency slot cannot progress; the watchdog must
        not treat that as its fault."""
        child = agent_loop.register_agent(name="queued-child", role="subagent")
        child.status = "queued"

        def _release():
            time.sleep(2.0)              # 4x the stall window, still queued
            child.status = "done"

        threading.Thread(target=_release, daemon=True).start()
        info = agent_loop.wait_for_agent(child.id, stall_seconds=0.5)
        self.assertIsNotNone(info)
        self.assertEqual(info.status, "done")

    def test_working_child_outlives_the_stall_window(self):
        """Progress buys time. A batch has no total budget, so a child that
        keeps working runs past any fixed window — only silence is bounded."""
        parent = agent_loop.register_agent(name="parent", role="primary")
        ctx = tools.ToolCtx(
            deps=_deps(), agent_id=parent.id, session={}, events_cb=None)

        def _busy(deps, task, session, state, chat_history, **kwargs):
            info = agent_loop.get_agent(state.get("_agent_id") or kwargs.get("agent_id"))
            for step in range(8):        # 8 * 0.15s = 1.2s >> the 0.3s window
                state.setdefault("terminalHistory", []).append(
                    {"tool": "fs.read", "call_id": f"c{step}", "command": "x"})
                agent_loop._publish_live_state(info, state)
                time.sleep(0.15)
            return {"state": {"lastReply": "finished the whole review"}}

        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(tools, "SPAWN_PARALLEL_STALL_SECONDS", 0.3), \
                mock.patch.object(tools, "SPAWN_PARALLEL_WRAP_UP_LEAD_SECONDS", 0.1), \
                mock.patch.object(agent_loop, "run_agent_loop", _busy):
            result = tools._bi_spawn_parallel({
                "tasks": [{"goal": "slow but working"}], "wait": True,
            }, ctx)

        self.assertTrue(result["ok"])
        self.assertIn("finished the whole review", result["result"])
        self.assertNotIn("no observable progress", result["result"])

    def test_stalled_child_is_cut_off_with_its_partial_answer(self):
        """Silence is what gets bounded — and the partial conclusion the child
        had already produced comes back instead of a bare 'no output'."""
        parent = agent_loop.register_agent(name="parent", role="primary")
        ctx = tools.ToolCtx(
            deps=_deps(), agent_id=parent.id, session={}, events_cb=None)

        def _wedged(deps, task, session, state, chat_history, **kwargs):
            info = agent_loop.get_agent(state.get("_agent_id") or kwargs.get("agent_id"))
            state["lastReply"] = "found one leak so far"
            agent_loop._publish_live_state(info, state)
            deadline = time.time() + 10
            while time.time() < deadline:      # never progresses again
                if info is not None and info.abort_event.is_set():
                    break
                time.sleep(0.05)
            return {"state": {"lastReply": state["lastReply"]}}

        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(tools, "SPAWN_PARALLEL_STALL_SECONDS", 0.4), \
                mock.patch.object(tools, "SPAWN_PARALLEL_WRAP_UP_LEAD_SECONDS", 0.1), \
                mock.patch.object(agent_loop, "run_agent_loop", _wedged):
            result = tools._bi_spawn_parallel({
                "tasks": [{"goal": "gets stuck"}], "wait": True,
            }, ctx)

        self.assertIn("no observable progress", result["result"])
        self.assertIn("found one leak so far", result["result"])


class AgentIsolationTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_terminals()
        agent_loop.close_all_agents()
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", False)

    def tearDown(self):
        while not agent_loop.get_user_message_queue().empty():
            agent_loop.get_user_message_queue().get_nowait()
        agent_loop.close_all_terminals()
        agent_loop.close_all_agents()
        agent_loop.reset_runtime_config()

    def test_upstream_truncated_retries_twice_before_success(self):
        attempts = []
        responses = iter([
            {
                "reply": "Server Error: Upstream ended the response early",
                "tool_calls": [], "done": True, "error": True,
                "error_code": "upstream_truncated",
            },
            {
                "reply": "Server Error: Upstream ended the response early",
                "tool_calls": [], "done": True, "error": True,
                "error_code": "upstream_truncated",
            },
            {
                "reply": "recovered", "tool_calls": [],
                "finish_reason": "stop", "done": True, "error": False,
            },
        ])
        deps = _deps()

        def backend(**kwargs):
            attempts.append(kwargs)
            return next(responses)

        deps.call_backend = backend
        child = agent_loop.register_agent(
            name="truncated-retry", depth=1, role="subagent")

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(
                    agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "retry a dropped stream", {}, child.state,
                child.chat_history, depth=1, agent_id=child.id,
                events_cb=lambda events: None, max_loops_override=3)

        self.assertEqual(len(attempts), 3)
        self.assertNotEqual(
            result["exit_reason"], agent_loop.TRANSITION_BACKEND_ERROR)
        self.assertIn("Empty-response retry 2/2", result["state"]["shortTermMemory"])

    def test_upstream_truncated_stops_after_old_retry_budget(self):
        attempts = []
        truncated = {
            "reply": "Server Error: Upstream ended the response early",
            "tool_calls": [], "done": True, "error": True,
            "error_code": "upstream_truncated",
        }
        deps = _deps()

        def backend(**kwargs):
            attempts.append(kwargs)
            return dict(truncated)

        deps.call_backend = backend
        child = agent_loop.register_agent(
            name="truncated-give-up", depth=1, role="subagent")

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(
                    agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "retry a dropped stream", {}, child.state,
                child.chat_history, depth=1, agent_id=child.id,
                events_cb=lambda events: None, max_loops_override=5)

        self.assertEqual(len(attempts), 3)
        self.assertEqual(
            result["exit_reason"], agent_loop.TRANSITION_BACKEND_ERROR)

    def test_child_does_not_drain_primary_supplementary_messages(self):
        child = agent_loop.register_agent(
            name="child", depth=1, role="subagent")
        agent_loop.get_user_message_queue().put("for-primary")
        child.message_queue.put("for-child")

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(
                _deps(), "child task", {}, child.state, child.chat_history,
                depth=1, agent_id=child.id, max_loops_override=1)

        self.assertEqual(
            agent_loop.get_user_message_queue().get_nowait(), "for-primary")
        self.assertTrue(child.message_queue.empty())

    def test_agent_tools_resolve_caller_not_global_current_agent(self):
        primary = agent_loop.register_agent(name="primary", role="primary")
        child = agent_loop.register_agent(name="child", depth=1, role="subagent")
        agent_loop.set_current_agent_id(primary.id)

        ctx = tools.ToolCtx(
            agent_id=child.id,
            get_agent=agent_loop.get_agent,
            get_all_agents=agent_loop.get_all_agents,
            rename_agent=agent_loop.rename_agent,
            depth=1,
        )
        result = tools._bi_agent_rename({"name": "worker"}, ctx)

        self.assertTrue(result["ok"])
        self.assertEqual(child.name, "worker")
        self.assertEqual(primary.name, "primary")

    def test_agent_hire_tool_defines_profile_without_starting_work(self):
        primary = agent_loop.register_agent(name="primary", role="primary")
        primary.home_terminal = "term0"
        primary.deployment_terminal = "term0"
        terminal_session = mock.Mock()
        terminal_session.is_alive.return_value = True
        agent_loop.register_terminal(
            terminal_session, "/bin/sh", 0, name="term0")
        ctx = tools.ToolCtx(
            agent_id="primary",
            depth=0,
            register_agent_fn=agent_loop.register_agent,
            get_agent=agent_loop.get_agent,
            get_terminal=agent_loop.get_terminal,
            station_agent=agent_loop.station_agent,
        )
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp)):
            result = tools._bi_agent_hire({
                "name": "review-employee", "profile": "reviewer",
            }, ctx)

        employee = agent_loop.get_agent("review-employee")
        self.assertTrue(result["ok"])
        self.assertEqual(employee.profile.specialist_role, "reviewer")
        self.assertIsNone(employee.active_assignment)
        self.assertEqual(employee.status, "idle")
        self.assertEqual(employee.role, "pool")
        self.assertIsNone(employee.stationed_terminal)
        self.assertEqual(employee.home_terminal, "term0")
        self.assertNotIn(employee.id, agent_loop.get_terminal("term0").stationed_agent_ids)
        self.assertIsNone(tools.get_registry().get("agent.switch"))
        employee.state["_persisted_employee"] = False

    def test_agent_hire_verifies_and_freezes_requested_base_model(self):
        import laintas_cli

        terminal_session = mock.Mock()
        terminal_session.is_alive.return_value = True
        agent_loop.register_terminal(
            terminal_session, "/bin/sh", 0, name="term0")
        primary = agent_loop.register_agent(name="primary", role="primary")
        ctx = tools.ToolCtx(
            agent_id=primary.id, depth=0, session={"userId": "u"},
            register_agent_fn=agent_loop.register_agent,
            get_agent=agent_loop.get_agent,
            get_terminal=agent_loop.get_terminal,
            station_agent=agent_loop.station_agent,
        )
        models = [{"id": "model-x", "provider": "provider-a"}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp)), \
                mock.patch.object(
                    laintas_cli, "fetch_available_models",
                    return_value=(models, "/api/models")):
            result = tools._bi_agent_hire({
                "name": "model-worker", "model": "model-x",
            }, ctx)

        self.assertTrue(result["ok"], result)
        employee = agent_loop.get_agent("model-worker")
        self.assertEqual(employee.base_model, "model-x")
        self.assertEqual(employee.base_provider, "provider-a")
        self.assertIsNone(employee.deployment_terminal)
        employee.state["_persisted_employee"] = False

    def test_read_only_roles_cannot_escape_through_shell(self):
        for role in ("explorer", "architect", "reviewer",
                     "silent-failure-hunter", "tester"):
            self.assertFalse(
                agent_roles.is_tool_allowed_for_role("shell.exec", role), role)

    def test_employee_tool_policy_is_an_independent_allowlist(self):
        profile = agent_loop.EmployeeProfile(
            title="Reader",
            tool_policy=agent_loop.AgentToolPolicy(
                allowed_tools=["fs.read", "fs.grep"],
                denied_tools=["fs.grep"],
            ),
        )
        employee = agent_loop.register_agent(
            name="reader", role="pool", profile=profile)

        allowed = agent_loop._allowed_tool_names_for_state(
            employee.state, employee.id)

        self.assertEqual(allowed, {"fs.read"})

    def test_legacy_terminal_fields_migrate_to_deployment_terminal(self):
        employee = agent_loop.register_agent(name="legacy", role="deployed")

        agent_persistence.apply_persisted_state(employee, {
            "home_terminal": "term0", "role": "deployed",
        })

        self.assertEqual(employee.deployment_terminal, "term0")
        self.assertEqual(employee.stationed_terminal, "term0")
        self.assertEqual(employee.home_terminal, "term0")

    def test_assignment_uses_employee_prompt_and_fresh_context(self):
        prompts = []
        deps = _deps()
        deps.generate_prompt = lambda: (
            "<environment>\n"
            "Terminal: {{terminalName}} | "
            "Parent terminal: {{parentTerminal}}\n"
            "</environment>"
        )

        def backend(**kwargs):
            prompts.append(kwargs["system_prompt"])
            return {
                "reply": "assignment complete",
                "tool_calls": [],
                "finish_reason": "stop",
                "done": True,
                "error": False,
            }

        deps.call_backend = backend
        employee = agent_loop.register_agent(
            name="alice", role="pool",
            profile=agent_loop.EmployeeProfile(
                title="Backend Engineer",
                prompt="ALICE-ONLY-PROMPT",
            ),
        )
        employee.state["shortTermMemory"] = "old assignment memory"
        employee.chat_history.append({"role": "user", "content": "old task"})
        root_session = mock.Mock()
        root_session.is_alive.return_value = True
        root_session.full_output = ""
        root_session.command = "/bin/sh"
        work_session = mock.Mock()
        work_session.is_alive.return_value = True
        work_session.full_output = ""
        work_session.command = "/bin/sh"
        work_session.returncode = -1
        work_session.command_lock = threading.RLock()
        agent_loop.register_terminal(
            root_session, "/bin/sh", 0, name="term0")
        agent_loop.register_terminal(
            work_session, "/bin/sh", 0, name="alice-work",
            parent_terminal="term0")
        self.assertTrue(agent_loop.station_agent(employee.id, "alice-work"))

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            ok, _, assignment = agent_loop.start_agent_assignment(
                employee.id, "implement feature X", deps, session={})
            self.assertTrue(ok)
            employee.thread.join(timeout=5)

        self.assertFalse(employee.thread.is_alive())
        self.assertIsNone(employee.active_assignment)
        self.assertEqual(employee.status, "idle")
        self.assertEqual(employee.assignment_history[-1]["id"], assignment.id)
        self.assertNotIn("old assignment memory", employee.state.get("shortTermMemory", ""))
        self.assertNotIn(
            {"role": "user", "content": "old task"}, employee.chat_history)
        self.assertTrue(any("ALICE-ONLY-PROMPT" in prompt for prompt in prompts))
        self.assertTrue(any("implement feature X" in prompt for prompt in prompts))
        self.assertTrue(any(
            "Terminal: alice-work | Parent terminal: term0" in prompt
            for prompt in prompts))
        self.assertTrue(any(
            '<runtime_ownership authoritative="true">' in prompt
            for prompt in prompts))
        self.assertTrue(any(
            "agent_hire` creates a persistent undeployed employee"
            in prompt for prompt in prompts))

    def test_employee_profile_persistence_round_trip(self):
        profile = agent_loop.EmployeeProfile(
            title="Reviewer",
            specialist_role="reviewer",
            prompt="review carefully",
            capability_tags=["review"],
            tool_policy=agent_loop.AgentToolPolicy(
                allowed_tools=["fs.read"], denied_tools=["shell.exec"]),
        )
        employee = agent_loop.register_agent(
            name="persisted", role="pool", profile=profile)
        employee.home_terminal = "term0"
        employee.base_model = "model-x"
        employee.base_provider = "provider-a"
        employee.assignment_history.append({
            "id": "job-1", "status": "completed", "result": "done"})

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp)):
            self.assertTrue(agent_persistence.save_agent_state(employee))
            payload = agent_persistence.load_agent_state(employee.id)
            restored = agent_loop.AgentInfo(id=employee.id, name=employee.id)
            agent_persistence.apply_persisted_state(restored, payload)

        self.assertEqual(restored.profile.title, "Reviewer")
        self.assertEqual(restored.profile.specialist_role, "reviewer")
        self.assertEqual(restored.profile.tool_policy.allowed_tools, ["fs.read"])
        self.assertEqual(restored.profile.tool_policy.denied_tools, ["shell.exec"])
        self.assertEqual(restored.assignment_history[-1]["id"], "job-1")
        self.assertEqual(restored.home_terminal, "term0")
        self.assertIsNone(restored.deployment_terminal)
        self.assertEqual(restored.base_model, "model-x")
        self.assertEqual(restored.base_provider, "provider-a")


class PersistentOwnershipTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_terminals()
        agent_loop.close_all_agents()

    def tearDown(self):
        agent_loop.close_all_terminals()
        agent_loop.close_all_agents()

    @staticmethod
    def _session():
        session = mock.Mock()
        session.is_alive.return_value = True
        return session

    def _terminal(self, name, parent=None):
        session = self._session()
        agent_loop.register_terminal(
            session, "/bin/sh", 0, name=name, parent_terminal=parent)
        return session

    def test_terminal_tree_cascades_agents_and_descendants(self):
        root = self._terminal("term0")
        child = self._terminal("child", "term0")
        grandchild = self._terminal("grandchild", "child")
        alice = agent_loop.register_agent(name="alice", role="deployed")
        bob = agent_loop.register_agent(name="bob", role="deployed")
        alice.state["_persisted_employee"] = True
        bob.state["_persisted_employee"] = True
        self.assertTrue(agent_loop.station_agent(alice.id, "child"))
        self.assertTrue(agent_loop.station_agent(bob.id, "grandchild"))
        worker = agent_loop.register_agent(
            name="alice-child", role="subagent", parent_id=alice.id)
        self.assertEqual(worker.parent_terminal, "child")
        self.assertIsNone(worker.deployment_terminal)
        self.assertNotIn("alice-child", agent_loop.get_terminal("child").stationed_agent_ids)

        with mock.patch.object(
                agent_persistence, "delete_agent_state", return_value=True) as delete:
            self.assertTrue(agent_loop.unregister_terminal("child"))

        self.assertIsNotNone(agent_loop.get_terminal("term0"))
        self.assertIsNone(agent_loop.get_terminal("child"))
        self.assertIsNone(agent_loop.get_terminal("grandchild"))
        self.assertIsNone(agent_loop.get_agent("alice"))
        self.assertIsNone(agent_loop.get_agent("bob"))
        self.assertIsNone(agent_loop.get_agent("alice-child"))
        self.assertTrue(alice.abort_event.is_set())
        self.assertTrue(bob.abort_event.is_set())
        self.assertTrue(worker.abort_event.is_set())
        self.assertEqual(
            {call.args[0] for call in delete.call_args_list}, {"alice", "bob"})
        self.assertFalse(root.close.called)
        self.assertTrue(child.close.called)
        self.assertTrue(grandchild.close.called)

    def test_closing_home_terminal_reparents_undeployed_hired_employee(self):
        self._terminal("term0")
        self._terminal("child", "term0")
        employee = agent_loop.register_agent(name="idle-hire", role="pool")
        employee.home_terminal = "child"
        employee.parent_terminal = "child"
        employee.state["_persisted_employee"] = True

        with mock.patch.object(
                agent_persistence, "save_agent_state", return_value=True) as save:
            self.assertTrue(agent_loop.unregister_terminal("child"))

        self.assertIs(agent_loop.get_agent(employee.id), employee)
        self.assertEqual(employee.home_terminal, "term0")
        self.assertIsNone(employee.deployment_terminal)
        save.assert_called_with(employee)
        employee.state["_persisted_employee"] = False

    def test_one_agent_per_terminal_and_one_terminal_per_agent(self):
        self._terminal("term0")
        self._terminal("left", "term0")
        self._terminal("right", "term0")
        alice = agent_loop.register_agent(name="alice", role="deployed")
        bob = agent_loop.register_agent(name="bob", role="deployed")

        self.assertTrue(agent_loop.station_agent(alice.id, "left"))
        self.assertFalse(agent_loop.station_agent(bob.id, "left"))
        self.assertEqual(
            agent_loop.get_terminal("left").stationed_agent_ids,
            ["alice"])

        self.assertTrue(agent_loop.station_agent(alice.id, "right"))
        self.assertNotIn("alice", agent_loop.get_terminal("left").stationed_agent_ids)
        self.assertTrue(agent_loop.station_agent(bob.id, "left"))
        self.assertIn("bob", agent_loop.get_terminal("left").stationed_agent_ids)
        self.assertEqual(
            agent_loop.get_terminal("right").stationed_agent_ids, ["alice"])
        self.assertEqual(alice.stationed_terminal, "right")
        self.assertEqual(alice.home_terminal, "left")
        self.assertEqual(alice.deployment_terminal, "right")

    def test_swap_station_is_atomic(self):
        self._terminal("term0")
        self._terminal("work", "term0")
        alice = agent_loop.register_agent(name="alice", role="deployed")
        bob = agent_loop.register_agent(name="bob", role="pool")
        self.assertTrue(agent_loop.station_agent(alice.id, "work"))
        # Snapshot occupant before swap.
        self.assertEqual(
            agent_loop.get_terminal("work").stationed_agent_id, alice.id)
        # Swap.
        self.assertTrue(agent_loop.swap_station(alice.id, bob.id, "work"))
        term = agent_loop.get_terminal("work")
        # After swap: bob owns work, alice is back to pool.
        self.assertEqual(term.stationed_agent_id, bob.id)
        self.assertEqual(term.stationed_agent_ids, [bob.id])
        self.assertEqual(bob.deployment_terminal, "work")
        self.assertEqual(bob.role, "deployed")
        self.assertIsNone(alice.deployment_terminal)
        self.assertEqual(alice.role, "pool")

    def test_swap_station_rejects_invalid_inputs(self):
        self._terminal("term0")
        self._terminal("work", "term0")
        alice = agent_loop.register_agent(name="alice", role="deployed")
        bob = agent_loop.register_agent(name="bob", role="pool")
        # No terminal deployed -> nothing to swap.
        self.assertFalse(agent_loop.swap_station(bob.id, alice.id))
        self.assertTrue(agent_loop.station_agent(alice.id, "work"))
        # Self swap is a no-op.
        self.assertFalse(agent_loop.swap_station(alice.id, alice.id, "work"))
        # Missing target agent.
        self.assertFalse(
            agent_loop.swap_station(alice.id, "nonexistent", "work"))
        # Bob already deployed elsewhere.
        self._terminal("other", "term0")
        self.assertTrue(agent_loop.station_agent(bob.id, "other"))
        self.assertFalse(agent_loop.swap_station(alice.id, bob.id, "work"))

    def test_concurrent_station_claim_has_exactly_one_winner(self):
        self._terminal("term0")
        self._terminal("work", "term0")
        alice = agent_loop.register_agent(name="alice", role="pool")
        bob = agent_loop.register_agent(name="bob", role="pool")
        barrier = threading.Barrier(3)
        results = []

        def claim(agent_id):
            barrier.wait()
            results.append(agent_loop.station_agent(agent_id, "work"))

        threads = [
            threading.Thread(target=claim, args=(alice.id,)),
            threading.Thread(target=claim, args=(bob.id,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(sorted(results), [False, True])
        terminal = agent_loop.get_terminal("work")
        self.assertEqual(len(terminal.stationed_agent_ids), 1)
        self.assertEqual(terminal.stationed_agent_ids[0], terminal.stationed_agent_id)

    def test_terminal_last_cwd_inherited_by_undeployed_assignment(self):
        """Undeployed employee inherits home_terminal.last_cwd as _task_cwd."""
        self._terminal("term0")
        self._terminal("work", "term0")
        alice = agent_loop.register_agent(name="alice", role="deployed")
        bob = agent_loop.register_agent(name="bob", role="pool")
        bob.home_terminal = "work"
        self.assertTrue(agent_loop.station_agent(alice.id, "work"))
        # Simulate alice's shell.exec updating the terminal cwd.
        agent_loop.get_terminal("work").last_cwd = "/tmp/some-project"
        # Bob has no prior _task_cwd; start_agent_assignment should inherit.
        ok, msg, assignment = agent_loop.start_agent_assignment(
            bob.id, "do something", self._deps_for_assignment())
        self.assertTrue(ok, msg)
        self.assertEqual(bob.state.get("_task_cwd"), "/tmp/some-project")

    def test_terminal_last_cwd_does_not_override_explicit_task_cwd(self):
        """An explicit prior _task_cwd wins over home_terminal.last_cwd."""
        self._terminal("term0")
        self._terminal("work", "term0")
        bob = agent_loop.register_agent(name="bob", role="pool")
        bob.home_terminal = "work"
        bob.state["_task_cwd"] = "/explicit/prior"
        agent_loop.get_terminal("work").last_cwd = "/tmp/other"
        ok, msg, assignment = agent_loop.start_agent_assignment(
            bob.id, "do something", self._deps_for_assignment())
        self.assertTrue(ok, msg)
        self.assertEqual(bob.state.get("_task_cwd"), "/explicit/prior")

    @staticmethod
    def _deps_for_assignment():
        deps = mock.Mock()
        deps.SubTerminalSession = _FakeInteractiveSession
        deps.InteractiveSession = _FakeInteractiveSession
        return deps

    def test_only_a_tree_edge_carries_a_message(self):
        """Messaging follows the delegation tree, and nothing else.

        Authorization used to be the union of three graphs: agent parent/child,
        "same terminal", and "adjacent parent/child terminals". Two of those are
        not delegation — an employee hired into a terminal is not the peer of
        everything else stationed there — so who could reach whom depended on
        runtime placement rather than on the task tree. Siblings are now
        unreachable BY CONSTRUCTION: work two of them must agree on belongs to
        the parent that owns both.
        """
        self._terminal("term0")
        self._terminal("left", "term0")
        root = agent_loop.register_agent(name="root-agent", role="pool")
        left = agent_loop.register_agent(name="left-agent", role="pool",
                                         parent_id=root.id)
        right = agent_loop.register_agent(name="right-agent", role="pool",
                                          parent_id=root.id)
        grandchild = agent_loop.register_agent(name="grandchild", role="pool",
                                               parent_id=left.id)
        housemate = agent_loop.register_agent(name="housemate", role="pool")
        housemate.home_terminal = left.home_terminal = "left"

        # Tree edges, both directions.
        self.assertTrue(agent_loop.can_agents_communicate(root.id, left.id))
        self.assertTrue(agent_loop.can_agents_communicate(left.id, root.id))
        self.assertTrue(agent_loop.can_agents_communicate(left.id, grandchild.id))
        # Siblings, grandparent, and a terminal housemate: all refused.
        self.assertFalse(agent_loop.can_agents_communicate(left.id, right.id))
        self.assertFalse(agent_loop.can_agents_communicate(root.id, grandchild.id))
        self.assertFalse(agent_loop.can_agents_communicate(left.id, housemate.id))

    def test_agent_list_shows_exactly_what_can_be_messaged(self):
        """A roster listing an unreachable agent invites turns spent finding out."""
        self._terminal("term0")
        root = agent_loop.register_agent(name="root-agent", role="pool")
        left = agent_loop.register_agent(name="left-agent", role="pool",
                                         parent_id=root.id)
        right = agent_loop.register_agent(name="right-agent", role="pool",
                                          parent_id=root.id)
        child = agent_loop.register_agent(name="left-child", role="pool",
                                          parent_id=left.id)
        listing = tools._bi_agent_list({}, tools.ToolCtx(agent_id=left.id))
        self.assertTrue(listing["ok"], listing)
        self.assertIn("root-agent (parent)", listing["result"])
        self.assertIn("left-child (child)", listing["result"])
        self.assertNotIn("right-agent", listing["result"])
        for other in (right.id, root.id):
            reachable = agent_loop.can_agents_communicate(left.id, other)
            self.assertEqual(other in listing["result"].replace("<-- self", ""),
                             reachable)

    def test_registration_leaves_no_agent_outside_the_tree(self):
        """"No parent given" is a missing edge, not a second kind of agent.

        Hires were registered with no parent and reached through their terminal
        instead, so half the registry sat outside the tree that messaging,
        abort and harvest are all written against.
        """
        root = agent_loop.register_agent(name="primary", role="primary")
        hired = agent_loop.register_agent(name="hired", role="pool")
        self.assertEqual(root.id, hired.parent_id)
        self.assertIn(hired.id, agent_loop.get_agent(root.id).child_ids)
        self.assertTrue(agent_loop.can_agents_communicate(root.id, hired.id))

    def test_agents_that_predate_the_rule_are_adopted_once(self):
        """Repair path for entries restored from before the invariant."""
        root = agent_loop.register_agent(name="primary", role="primary")
        legacy = agent_loop.register_agent(name="legacy", role="pool")
        legacy.parent_id = None                      # as restored from disk
        agent_loop.get_agent(root.id).child_ids.remove(legacy.id)
        self.assertFalse(agent_loop.can_agents_communicate(root.id, legacy.id))

        self.assertEqual(1, agent_loop.adopt_orphan_agents(root.id))
        self.assertEqual(root.id, agent_loop.get_agent(legacy.id).parent_id)
        self.assertTrue(agent_loop.can_agents_communicate(root.id, legacy.id))
        self.assertEqual(0, agent_loop.adopt_orphan_agents(root.id))

    def test_dialog_focus_is_independent_from_deployed_shell_owner(self):
        self._terminal("term0")
        self._terminal("work", "term0")
        first = agent_loop.register_agent(name="first", role="pool")
        second = agent_loop.register_agent(name="second", role="pool")
        first.home_terminal = second.home_terminal = "work"

        self.assertIs(
            agent_loop.get_dialog_agent_for_terminal("work"), first)
        self.assertTrue(agent_loop.station_agent(second.id, "work"))
        self.assertEqual(
            agent_loop.get_terminal("work").stationed_agent_id, second.id)
        self.assertIs(
            agent_loop.get_dialog_agent_for_terminal("work"), first)
        self.assertTrue(agent_loop.set_dialog_agent_for_terminal(
            "work", second.id))
        self.assertIs(
            agent_loop.get_dialog_agent_for_terminal("work"), second)
        self.assertEqual(
            agent_loop.get_terminal("work").stationed_agent_id, second.id)

    def test_agent_tell_attaches_freshness_provenance(self):
        self._terminal("term0")
        sender = agent_loop.register_agent(name="sender", role="pool")
        receiver = agent_loop.register_agent(name="receiver", role="pool",
                                             parent_id=sender.id)
        sender.home_terminal = receiver.home_terminal = "term0"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = tools.ToolCtx(
                agent_id=sender.id, cwd=tmp,
                send_to_agent=agent_loop.send_to_agent)
            result = tools._bi_agent_tell({
                "agent_id": receiver.id,
                "message": "directory analysis",
            }, ctx)

        self.assertTrue(result["ok"], result)
        message = agent_loop.recv_from_inbox(receiver.id)
        self.assertEqual(message["provenance"]["terminal"], "term0")
        self.assertTrue(message["provenance"]["observed_at"] > 0)
        self.assertTrue(message["provenance"]["cwd"])

    def test_terminal_model_override_does_not_mutate_agent_base_model(self):
        self._terminal("term0")
        self._terminal("work", "term0")
        employee = agent_loop.register_agent(name="alice", role="pool")
        employee.base_model = "base-model"
        employee.base_provider = "base-provider"
        self.assertTrue(agent_loop.station_agent(employee.id, "work"))

        self.assertTrue(agent_loop.set_terminal_model_selection(
            "work", "terminal-model", "terminal-provider"))
        self.assertEqual(
            agent_loop.resolve_agent_model(employee),
            ("terminal-model", "terminal-provider"))
        self.assertEqual(employee.base_model, "base-model")
        self.assertTrue(agent_loop.set_terminal_model_selection("work", ""))
        self.assertEqual(
            agent_loop.resolve_agent_model(employee),
            ("base-model", "base-provider"))

    def test_terminal_rename_updates_children_and_agent_binding(self):
        self._terminal("term0")
        self._terminal("parent", "term0")
        self._terminal("child", "parent")
        alice = agent_loop.register_agent(name="alice", role="deployed")
        self.assertTrue(agent_loop.station_agent(alice.id, "parent"))

        self.assertTrue(agent_loop.rename_terminal("parent", "renamed"))

        self.assertEqual(
            agent_loop.get_terminal("child").parent_terminal, "renamed")
        self.assertEqual(alice.stationed_terminal, "renamed")
        self.assertEqual(alice.home_terminal, "renamed")
        self.assertEqual(alice.deployment_terminal, "renamed")
        self.assertIn(
            "alice", agent_loop.get_terminal("renamed").stationed_agent_ids)

    def test_child_terminal_requires_live_registered_parent_and_unique_name(self):
        orphan = self._session()
        with self.assertRaisesRegex(ValueError, "Parent terminal"):
            agent_loop.register_terminal(
                orphan, "/bin/sh", 0, name="orphan",
                parent_terminal="missing")
        self.assertFalse(orphan.close.called)

        original = self._terminal("term0")
        replacement = self._session()
        with self.assertRaisesRegex(ValueError, "already exists"):
            agent_loop.register_terminal(
                replacement, "/bin/sh", 0, name="term0")
        self.assertIs(agent_loop.get_terminal("term0").session, original)
        self.assertFalse(original.close.called)

    def test_ai_terminal_create_uses_cli_entry_and_callers_terminal_as_parent(self):
        self._terminal("term0")
        owner = agent_loop.register_agent(name="primary", role="primary")
        owner.home_terminal = "term0"
        deps = _deps()
        deps.SubTerminalSession = _FakeInteractiveSession
        ctx = tools.ToolCtx(
            deps=deps, agent_id=owner.id, depth=0,
            get_agent=agent_loop.get_agent,
            get_terminal=agent_loop.get_terminal,
            register_terminal=agent_loop.register_terminal,
            unregister_terminal=agent_loop.unregister_terminal,
        )

        result = tools._bi_terminal_create({"name": "worker-cli"}, ctx)

        self.assertTrue(result["ok"])
        terminal = agent_loop.get_terminal("worker-cli")
        self.assertEqual(terminal.parent_terminal, "term0")
        self.assertIn("laintas_cli.py", terminal.session.command)
        self.assertIn("--terminal-name worker-cli", terminal.session.command)
        self.assertIn("LAINTAS_TERMINAL_ID=term-", terminal.session.command)


class EphemeralSessionTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        _FakeInteractiveSession.instances = []

    def tearDown(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()

    def _ctx(self):
        deps = _deps()
        deps.InteractiveSession = _FakeInteractiveSession
        return tools.ToolCtx(deps=deps, agent_id="child", cwd="/tmp")

    def test_private_session_start_keys_read_status_and_close(self):
        ctx = self._ctx()
        before = list(agent_loop.get_all_terminals())

        started = tools._bi_session_start(
            {"command": "python", "cwd": "/tmp", "timeout": 20}, ctx)
        self.assertTrue(started["ok"])
        session = ctx.interactive_session
        self.assertEqual(session.command, "python")
        self.assertEqual(agent_loop.get_all_terminals(), before)

        raw = tools._bi_session_keys({"keys": " x ", "mode": "raw"}, ctx)
        line = tools._bi_session_keys({"keys": "print(2)", "mode": "line"}, ctx)
        self.assertTrue(raw["ok"])
        self.assertTrue(line["ok"])
        self.assertEqual(session.sent, [" x ", "print(2)\r"])

        session.full_output += "answer\n"
        read = tools._bi_session_read({}, ctx)
        self.assertEqual(read["new_output"], "answer\n")
        self.assertTrue(tools._bi_session_status({}, ctx)["alive"])

        closed = tools._bi_session_close({}, ctx)
        self.assertTrue(closed["ok"])
        self.assertTrue(session.closed)
        self.assertIsNone(ctx.interactive_session)

    def test_abort_immediately_closes_agent_private_session(self):
        child = agent_loop.register_agent(
            name="pty-owner", depth=1, role="subagent")
        session = _FakeInteractiveSession("python")
        session.start()
        child.ephemeral_session = session

        self.assertTrue(agent_loop.abort_agent(child.id))
        self.assertTrue(session.closed)
        self.assertIsNone(child.ephemeral_session)

    def test_agent_loop_auto_closes_unowned_private_session(self):
        responses = iter([
            {
                "reply": "starting",
                "tool_calls": [{
                    "name": "session.start",
                    "arguments": {"command": "python"},
                }],
                "finish_reason": "tool_calls", "done": False, "error": False,
            },
            {
                "reply": "finished",
                "tool_calls": [{
                    "name": "task.complete",
                    "arguments": {"summary": "finished"},
                }],
                "finish_reason": "tool_calls", "done": False, "error": False,
            },
        ])
        deps = _deps()
        deps.InteractiveSession = _FakeInteractiveSession
        deps.call_backend = lambda **kwargs: next(responses)
        child = agent_loop.register_agent(
            name="ephemeral-child", depth=1, role="subagent")

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "use a temporary repl", {}, child.state,
                child.chat_history, depth=1, agent_id=child.id,
                max_loops_override=2)

        self.assertEqual(len(_FakeInteractiveSession.instances), 1)
        self.assertTrue(_FakeInteractiveSession.instances[0].closed)
        self.assertIsNone(result["session"])
        self.assertEqual(agent_loop.get_all_terminals(), [])

    def test_foreground_task_changes_emit_current_agent_live_list(self):
        responses = iter([
            {
                "reply": "planning", "tool_calls": [{
                    "name": "task.create",
                    "arguments": {"subject": "verify live list"},
                }], "finish_reason": "tool_calls", "done": False,
                "error": False,
            },
            {
                "reply": "done", "tool_calls": [{
                    "name": "task.update",
                    "arguments": {"id": "s1", "status": "completed"},
                }], "finish_reason": "tool_calls", "done": False,
                "error": False,
            },
            {
                "reply": "complete", "tool_calls": [{
                    "name": "task.complete",
                    "arguments": {"summary": "verified"},
                }], "finish_reason": "tool_calls", "done": False,
                "error": False,
            },
        ])
        deps = _deps()
        deps.call_backend = lambda **kwargs: next(responses)
        rendered = []
        deps.display_task_list = lambda tasks, owner: rendered.append(
            (owner, [(task["subject"], task["status"]) for task in tasks]))
        primary = agent_loop.register_agent(name="live-root", role="primary")
        agent_loop.set_current_agent_id(primary.id)

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(
                    agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "track this work", {}, primary.state,
                primary.chat_history, depth=0, agent_id=primary.id,
                events_cb=lambda events: None, max_loops_override=3)

        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_COMPLETED)
        self.assertEqual([owner for owner, _ in rendered], [primary.id, primary.id])
        self.assertEqual(rendered[0][1], [("verify live list", "pending")])
        self.assertEqual(rendered[1][1], [("verify live list", "completed")])

    def test_shell_exec_abort_terminates_process_group(self):
        child = agent_loop.register_agent(
            name="cancel-shell", depth=1, role="subagent")
        ctx = tools.ToolCtx(
            agent_id=child.id, cwd="/tmp", get_agent=agent_loop.get_agent)
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.update(tools._bi_shell_exec(
                {"command": "sleep 10", "timeout": 20}, ctx)))
        thread.start()
        time.sleep(0.2)
        child.abort_event.set()
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertFalse(holder["ok"])
        self.assertEqual(holder["error"], "Command aborted")

    def test_shell_exec_reuses_deployment_terminal_and_persists_cwd(self):
        session = mock.Mock()
        session.is_alive.return_value = True
        session.command_lock = None
        session.raw_output = ""

        def execute_wrapped(command):
            begin = re.search(
                r"__LAINTAS_SHELL_BEGIN_[0-9a-f]+__", command).group(0)
            cwd = re.search(
                r"__LAINTAS_SHELL_CWD_[0-9a-f]+__", command).group(0)
            end = re.search(
                r"__LAINTAS_SHELL_END_[0-9a-f]+__", command).group(0)
            session.raw_output += (
                f"{begin}\n/tmp\n{cwd}:/tmp\n{end}:0\n"
            )

        session.send_keys.side_effect = execute_wrapped
        stationed = mock.Mock(session=session)
        ctx = tools.ToolCtx(
            cwd="/tmp", stationed_terminal=stationed,
            interactive_session=_FakeInteractiveSession("python"))

        result = tools._bi_shell_exec({
            "command": "cd /tmp && pwd",
        }, ctx)

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "/tmp")
        self.assertEqual(result["cwd"], "/tmp")
        self.assertEqual(result["via"], "deployment_terminal")
        session.send_keys.assert_called_once()
        self.assertEqual(ctx.interactive_session.sent, [])

    def test_shell_timeout_recovery_never_injects_printable_input(self):
        import laintas_cli

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "recovery-input.txt"
            session = laintas_cli.InteractiveSession(
                laintas_cli.DEFAULT_SHELL, timeout=0, stream_output=False,
                persistent=True, cwd=tmp)
            session.start()
            time.sleep(0.1)
            session.read_output(timeout=0.1)
            terminal = mock.Mock(session=session)
            deps = _deps()
            deps.InteractiveSession = laintas_cli.InteractiveSession
            ctx = tools.ToolCtx(
                deps=deps, cwd=tmp, stationed_terminal=terminal)
            command = (
                "IFS= read -r -n 1 ch; "
                f"printf %s \"$ch\" > {shlex.quote(str(target))}; sleep 10"
            )
            try:
                result = tools._bi_shell_exec({
                    "command": command, "timeout": 1,
                }, ctx)
                self.assertFalse(result["ok"])
                self.assertIn("terminal recovered", result["error"])
                self.assertFalse(target.exists() and target.read_text())
            finally:
                terminal.session.close()

    def test_noninteractive_environment_is_scoped_to_each_tool_command(self):
        import laintas_cli

        session = laintas_cli.InteractiveSession(
            laintas_cli.DEFAULT_SHELL, timeout=0, stream_output=False,
            persistent=True, cwd="/tmp")
        session.start()
        time.sleep(0.1)
        session.read_output(timeout=0.1)
        terminal = mock.Mock(session=session)
        ctx = tools.ToolCtx(cwd="/tmp", stationed_terminal=terminal)
        try:
            set_start = len(session.raw_output)
            session.send_keys(
                "export GIT_EDITOR=sentinel-editor "
                "GIT_TERMINAL_PROMPT=sentinel-prompt "
                "DEBIAN_FRONTEND=sentinel-debian PAGER=sentinel-pager; "
                "echo __SET_''DONE__\n"
            )
            set_deadline = time.monotonic() + 2
            while (time.monotonic() < set_deadline
                   and "__SET_DONE__" not in session.raw_output[set_start:]):
                session.read_output(timeout=0.1)
            self.assertIn("__SET_DONE__", session.raw_output[set_start:])

            forced = tools._bi_shell_exec({
                "command": (
                    "printf '%s|%s|%s|%s' \"$GIT_EDITOR\" "
                    "\"$GIT_TERMINAL_PROMPT\" \"$DEBIAN_FRONTEND\" \"$PAGER\""
                ),
            }, ctx)
            self.assertEqual(forced["result"], "true|0|noninteractive|cat")

            old_len = len(session.raw_output)
            expected = (
                "__RAW_ENV__sentinel-editor|sentinel-prompt|"
                "sentinel-debian|sentinel-pager__END__"
            )
            session.send_keys(
                "printf '__RAW''_ENV__%s|%s|%s|%s__END__\\n' "
                '"$GIT_EDITOR" "$GIT_TERMINAL_PROMPT" '
                '"$DEBIAN_FRONTEND" "$PAGER"\n'
            )
            deadline = time.monotonic() + 2
            while (time.monotonic() < deadline
                   and expected not in session.raw_output[old_len:]):
                session.read_output(timeout=0.1)
            self.assertIn(expected, session.raw_output[old_len:])

            # A command may unset the temporary values, but the next tool
            # call still receives a fresh non-interactive scope.
            tools._bi_shell_exec({
                "command": "unset GIT_PAGER PAGER GIT_EDITOR",
            }, ctx)
            again = tools._bi_shell_exec({
                "command": "printf '%s|%s' \"$GIT_EDITOR\" \"$PAGER\"",
            }, ctx)
            self.assertEqual(again["result"], "true|cat")
        finally:
            terminal.session.close()

    def test_direct_terminal_command_only_overrides_pager_variables(self):
        import laintas_cli

        session = laintas_cli.InteractiveSession(
            laintas_cli.DEFAULT_SHELL, timeout=0, stream_output=False,
            persistent=True, cwd="/tmp")
        session.start()
        time.sleep(0.1)
        session.read_output(timeout=0.1)
        try:
            start = len(session.raw_output)
            session.send_keys(
                "export GIT_EDITOR=user-editor "
                "GIT_TERMINAL_PROMPT=user-prompt "
                "DEBIAN_FRONTEND=user-debian PAGER=user-pager; "
                "echo __DIRECT_''READY__\n"
            )
            deadline = time.monotonic() + 2
            while (time.monotonic() < deadline
                   and "__DIRECT_READY__" not in session.raw_output[start:]):
                session.read_output(timeout=0.1)

            result = laintas_cli._marker_poll_exec(
                session,
                "printf '%s|%s|%s|%s' \"$GIT_EDITOR\" "
                "\"$GIT_TERMINAL_PROMPT\" \"$DEBIAN_FRONTEND\" \"$PAGER\"",
                timeout=2,
            )
            self.assertTrue(result["success"], result)
            self.assertEqual(
                result["stdout"],
                "user-editor|user-prompt|user-debian|cat",
            )
        finally:
            session.close()

    def test_unrecoverable_deployment_shell_is_restarted(self):
        import laintas_cli

        session = laintas_cli.InteractiveSession(
            laintas_cli.DEFAULT_SHELL, timeout=0, stream_output=False,
            persistent=True, cwd="/tmp")
        session.start()
        time.sleep(0.1)
        session.read_output(timeout=0.1)
        old_pid = session.pid
        terminal = mock.Mock(session=session)
        deps = _deps()
        deps.InteractiveSession = laintas_cli.InteractiveSession
        ctx = tools.ToolCtx(
            deps=deps, cwd="/tmp", stationed_terminal=terminal)
        try:
            result = tools._bi_shell_exec({
                "command": 'trap "" INT TERM; IFS= read -r line; sleep 10',
                "timeout": 1,
            }, ctx)
            self.assertFalse(result["ok"])
            self.assertTrue(result.get("terminal_restarted"), result)
            self.assertNotEqual(terminal.session.pid, old_pid)
            self.assertTrue(terminal.session.is_alive())
            health = tools._bi_shell_exec({
                "command": "printf RESTARTED", "timeout": 2,
            }, ctx)
            self.assertTrue(health["ok"], health)
            self.assertEqual(health["result"], "RESTARTED")
        finally:
            terminal.session.close()

    def test_undeployed_worker_shell_uses_one_private_temporary_terminal(self):
        import laintas_cli

        worker = agent_loop.register_agent(name="temp-worker", depth=1, role="pool")
        deps = _deps()
        deps.InteractiveSession = laintas_cli.InteractiveSession
        with tempfile.TemporaryDirectory() as tmp:
            ctx = tools.ToolCtx(
                deps=deps, agent_id=worker.id, depth=1, cwd=tmp,
                get_agent=agent_loop.get_agent)
            first = tools._bi_shell_exec({
                "command": "mkdir -p nested && cd nested && pwd",
            }, ctx)
            first_session = ctx.interactive_session
            second = tools._bi_shell_exec({"command": "pwd"}, ctx)
            try:
                self.assertTrue(first["ok"], first)
                self.assertTrue(second["ok"], second)
                self.assertEqual(first["via"], "temporary_terminal")
                self.assertEqual(second["via"], "temporary_terminal")
                self.assertTrue(second["result"].endswith("/nested"))
                self.assertIs(ctx.interactive_session, first_session)
            finally:
                if ctx.interactive_session is not None:
                    ctx.interactive_session.close()
    def test_undeployed_shell_exec_remains_isolated(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            before = os.getcwd()
            result = tools._bi_shell_exec(
                {"command": "cd / && pwd"}, tools.ToolCtx(cwd=tmp))

            self.assertTrue(result["ok"])
            self.assertEqual(result["result"], "/")
            self.assertEqual(result["via"], "subprocess")
            self.assertNotIn("cwd", result)
            self.assertEqual(os.getcwd(), before)

    def test_agent_loop_routes_deployed_shell_to_owning_terminal(self):
        responses = iter([{
            "reply": "moving",
            "tool_calls": [{
                "name": "shell.exec",
                "arguments": {"command": "cd /tmp && pwd"},
            }],
            "finish_reason": "tool_calls", "done": False, "error": False,
        }, {
            "reply": "done",
            "tool_calls": [{
                "name": "task.complete",
                "arguments": {"summary": "done"},
            }],
            "finish_reason": "tool_calls", "done": False, "error": False,
        }])
        deps = _deps()
        deps.call_backend = lambda **kwargs: next(responses)
        session = mock.Mock()
        session.is_alive.return_value = True
        session.command_lock = threading.RLock()
        session.raw_output = ""
        session.full_output = ""
        session.command = "/bin/sh"

        def execute_wrapped(command):
            begin = re.search(
                r"__LAINTAS_SHELL_BEGIN_[0-9a-f]+__", command).group(0)
            cwd = re.search(
                r"__LAINTAS_SHELL_CWD_[0-9a-f]+__", command).group(0)
            end = re.search(
                r"__LAINTAS_SHELL_END_[0-9a-f]+__", command).group(0)
            session.raw_output += (
                f"{begin}\n/tmp\n{cwd}:/tmp\n{end}:1\n"
            )
            session.full_output = session.raw_output

        session.send_keys.side_effect = execute_wrapped
        agent_loop.register_terminal(session, "/bin/sh", 0, name="term0")
        primary = agent_loop.register_agent(name="primary", role="primary")

        with _chdir(os.getcwd()):
            result = agent_loop.run_agent_loop(
                deps, "move", {}, primary.state, primary.chat_history,
                depth=0, agent_id=primary.id, max_loops_override=2)

        # The terminal really changed directory even though the compound
        # command returned non-zero, so the agent cwd must still follow it.
        self.assertEqual(result["state"]["cwd"], "/tmp")
        self.assertEqual(primary.deployment_terminal, "term0")
        self.assertTrue(any("cd /tmp && pwd" in call.args[0]
                            for call in session.send_keys.call_args_list))

    def test_failed_shell_output_is_preserved_for_the_ai(self):
        result = tools._bi_shell_exec({
            "command": "printf 'missing package' >&2; exit 1",
        }, tools.ToolCtx(cwd="/tmp"))

        formatted = agent_loop._format_tool_result_for_loop(
            "shell.exec", result, 3000)

        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 1)
        self.assertIn("[command exit 1]", formatted)
        self.assertIn("missing package", formatted)
        self.assertNotIn("no error message", formatted)

    def test_terminal_send_returns_delta_without_fake_exit_code(self):
        session = _FakeInteractiveSession("bash")
        session.start()
        session.full_output += "old output\n"
        terminal = mock.Mock(session=session)
        ctx = tools.ToolCtx(
            agent_id="primary", deps=_deps(),
            get_terminal=lambda name: terminal if name == "term0" else None)

        result = tools._bi_terminal_send({
            "name": "term0", "input": "echo new", "mode": "line",
        }, ctx)

        self.assertTrue(result["ok"])
        self.assertFalse(result["completed"])
        self.assertNotIn("returncode", result)
        self.assertNotIn("old output", result["result"])
        self.assertIn("echo new", result["new_output"])
        self.assertEqual(session.sent[-1], "echo new\r")

    def test_terminal_read_uses_per_agent_cursor(self):
        session = _FakeInteractiveSession("bash")
        session.start()
        terminal = mock.Mock(session=session)
        ctx = tools.ToolCtx(
            agent_id="reader", deps=_deps(),
            get_terminal=lambda name: terminal)

        first = tools._bi_terminal_read({"name": "term0"}, ctx)
        session.full_output += "later\n"
        second = tools._bi_terminal_read({"name": "term0"}, ctx)
        third = tools._bi_terminal_read({"name": "term0"}, ctx)

        self.assertIn("ready", first["new_output"])
        self.assertEqual(second["new_output"], "later")
        self.assertEqual(third["new_output"], "")
        self.assertFalse(second["completed"])

    def test_terminal_read_reports_completed_job_and_real_exit_code(self):
        session = _FakeInteractiveSession("finite")
        session.start()
        session.full_output += "final sample\n"
        session.alive = False
        session.returncode = 9
        terminal = agent_loop.TerminalInfo(
            name="monitor", command="finite", session=session,
            created_at=time.time(), created_by="depth=0",
            retain_completed=True)
        ctx = tools.ToolCtx(
            agent_id="reader", deps=_deps(),
            get_terminal=lambda name: terminal if name == "monitor" else None)

        result = tools._bi_terminal_read({"name": "monitor"}, ctx)

        self.assertTrue(result["ok"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["returncode"], 9)
        self.assertIn("final sample", result["new_output"])
        self.assertIsNotNone(terminal.completed_at)
        formatted = agent_loop._format_tool_result_for_loop(
            "terminal.read", result, 3000)
        self.assertIn("status=completed", formatted)
        self.assertIn("completed=true", formatted)
        self.assertIn("returncode=9", formatted)

    def test_terminal_wait_captures_final_output_without_sleep_polling(self):
        session = _FiniteBackgroundSession("monitor")
        session.start()
        terminal = agent_loop.TerminalInfo(
            name="monitor", command="monitor", session=session,
            created_at=time.time(), created_by="depth=0",
            retain_completed=True)
        ctx = tools.ToolCtx(
            agent_id="reader", deps=_deps(),
            get_terminal=lambda name: terminal if name == "monitor" else None)

        result = tools._bi_terminal_wait({
            "name": "monitor", "timeout": 1, "poll_interval": 0.05,
        }, ctx)

        self.assertTrue(result["ok"])
        self.assertTrue(result["completed"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["returncode"], 7)
        self.assertIn("sample-final", result["new_output"])

    def test_terminal_exec_running_result_has_no_fake_exit_code(self):
        root_session = mock.Mock()
        root_session.is_alive.return_value = True
        root = agent_loop.TerminalInfo(
            name="term0", command="bash", session=root_session,
            created_at=time.time(), created_by="depth=0")
        child = _FakeInteractiveSession("long-job")
        deps = _deps()
        deps.SubTerminalSession = lambda command: child
        registered = {}

        def register(session, command, depth, **kwargs):
            registered.update(kwargs)
            return kwargs["name"]

        ctx = tools.ToolCtx(
            agent_id="primary", deps=deps,
            get_agent=lambda _id: None,
            get_terminal=lambda name: root if name == "term0" else None,
            register_terminal=register,
            unregister_terminal=lambda name: True)

        result = tools._bi_terminal_exec({
            "name": "perf-monitor", "command": "vmstat 5 12",
        }, ctx)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "running")
        self.assertFalse(result["completed"])
        self.assertNotIn("returncode", result)
        self.assertTrue(registered["retain_completed"])
        formatted = agent_loop._format_tool_result_for_loop(
            "terminal.exec", result, 3000)
        self.assertIn("status=running", formatted)
        self.assertIn("returncode=pending", formatted)

    def test_employee_tool_policy_blocks_forged_tool_call_at_dispatch(self):
        calls = []
        responses = iter([
            {
                "reply": "trying write",
                "tool_calls": [{
                    "name": "fs.write",
                    "arguments": {"path": "blocked.txt", "content": "no"},
                }],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            },
            {
                "reply": "done",
                "tool_calls": [],
                "finish_reason": "stop",
                "done": True,
                "error": False,
            },
        ])
        deps = _deps()

        def backend(**kwargs):
            calls.append(kwargs)
            return next(responses)

        deps.call_backend = backend
        employee = agent_loop.register_agent(
            name="locked", depth=1, role="pool",
            profile=agent_loop.EmployeeProfile(
                tool_policy=agent_loop.AgentToolPolicy(
                    allowed_tools=["fs.read"])),
        )
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            with mock.patch.object(
                    tools.get_registry(), "invoke",
                    wraps=tools.get_registry().invoke) as invoke:
                result = agent_loop.run_agent_loop(
                    deps, "read only", {}, employee.state,
                    employee.chat_history, depth=1, agent_id=employee.id,
                    max_loops_override=2)

        invoke.assert_not_called()
        self.assertTrue(calls)
        self.assertEqual(calls[0]["allowed_tool_names"], {"fs.read"})
        self.assertIn("not available to agent", result["state"]["lastOutput"])


class TerminalTriggerTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_terminals()

    def tearDown(self):
        agent_loop.close_all_terminals()

    def test_snapshot_delta_handles_append_roll_and_clear(self):
        self.assertEqual(
            agent_loop._terminal_snapshot_delta("one\n", "one\ntwo\n"),
            "two\n")
        self.assertEqual(
            agent_loop._terminal_snapshot_delta(
                "old\nshared\n", "shared\nnew\n"),
            "new\n")
        self.assertEqual(
            agent_loop._terminal_snapshot_delta(
                "discarded\nshared\n", "shared\n"),
            "")
        self.assertEqual(
            agent_loop._terminal_snapshot_delta("old screen", "fresh screen"),
            "fresh screen")

    def test_completed_terminal_retains_final_output_and_dispatches_final_trigger(self):
        root = mock.Mock()
        root.is_alive.return_value = True
        root.read_output.return_value = ""
        root.full_output = ""
        job = mock.Mock()
        job.is_alive.return_value = False
        job.read_output.return_value = ""
        job.full_output = "last-line: READY\n"
        job.returncode = 0

        with mock.patch.object(agent_loop, "send_to_agent", return_value=True) as send:
            agent_loop.register_terminal(root, "bash", 0, name="term0")
            agent_loop.register_terminal(
                job, "probe", 0, name="probe", parent_terminal="term0",
                trigger="READY", trigger_agent_id="primary",
                retain_completed=True)
            # Wait for both events: terminal.exit (immediate) and
            # watch.trigger (flushed after debounce window).
            deadline = time.time() + 3.0
            while len(send.call_args_list) < 2 and time.time() < deadline:
                time.sleep(0.1)
            self.assertGreaterEqual(
                len(send.call_args_list), 2,
                f"expected >=2 events, got {len(send.call_args_list)}")

        info = agent_loop.get_terminal("probe")
        self.assertIsNotNone(info)
        self.assertEqual(info.returncode, 0)
        self.assertIsNotNone(info.completed_at)
        event_types = [c.args[1].get("type") for c in send.call_args_list]
        self.assertIn("watch.trigger", event_types)
        self.assertIn("terminal.exit", event_types)
        trigger_evt = next(
            c.args[1] for c in send.call_args_list
            if c.args[1].get("type") == "watch.trigger")
        self.assertEqual(trigger_evt["line"], "last-line: READY")

    def test_multiple_trigger_targets_all_notified(self):
        """One terminal can notify multiple agents simultaneously."""
        root = mock.Mock()
        root.is_alive.return_value = True
        root.read_output.return_value = ""
        root.full_output = ""
        job = mock.Mock()
        job.is_alive.return_value = True
        job.read_output.return_value = ""
        job.full_output = "hit: READY\n"
        job.returncode = 0
        delivered: list[str] = []

        def capture(agent_id, msg, *a, **kw):
            delivered.append(agent_id)
            return True

        with mock.patch.object(agent_loop, "send_to_agent", side_effect=capture):
            agent_loop.register_terminal(root, "bash", 0, name="term0")
            agent_loop.register_terminal(
                job, "probe", 0, name="probe", parent_terminal="term0",
                trigger="READY",
                trigger_agent_ids=["agent-a", "agent-b", "agent-c"],
                retain_completed=True)
            # Wait for debounce to flush.
            deadline = time.time() + 3.0
            while len(delivered) < 3 and time.time() < deadline:
                time.sleep(0.1)
        agent_loop.close_all_terminals()
        self.assertEqual(sorted(delivered), ["agent-a", "agent-b", "agent-c"])

    def test_debounce_buffers_rapid_matches(self):
        """Multiple matching lines in one scan produce a single batched event."""
        agent_loop.set_runtime_config("trigger_debounce_ms", 100.0)
        agent_loop.set_runtime_config("trigger_scan_interval", 0.1)
        root = mock.Mock()
        root.is_alive.return_value = True
        root.read_output.return_value = ""
        root.full_output = ""
        job = mock.Mock()
        job.is_alive.return_value = True
        job.read_output.return_value = ""
        job.full_output = "1: READY\n2: READY\n3: READY\n"
        job.returncode = 0
        sent: list[dict] = []

        def capture(agent_id, msg, *a, **kw):
            sent.append(msg)
            return True

        try:
            with mock.patch.object(agent_loop, "send_to_agent", side_effect=capture):
                agent_loop.register_terminal(root, "bash", 0, name="term0")
                agent_loop.register_terminal(
                    job, "probe", 0, name="probe", parent_terminal="term0",
                    trigger="READY", trigger_agent_ids=["watcher"],
                    retain_completed=True)
                deadline = time.time() + 3.0
                while not sent and time.time() < deadline:
                    time.sleep(0.1)
            agent_loop.close_all_terminals()
            self.assertTrue(sent, "no trigger event delivered")
            trigger_evts = [m for m in sent if m.get("type") == "watch.trigger"]
            self.assertEqual(len(trigger_evts), 1,
                             "rapid matches should be batched into one event")
            self.assertEqual(trigger_evts[0]["count"], 3)
        finally:
            agent_loop.reset_runtime_config()

    def test_trigger_wake_callback_fires_for_idle_agent(self):
        """A trigger event for an idle agent invokes the wake callback."""
        root = mock.Mock()
        root.is_alive.return_value = True
        root.read_output.return_value = ""
        root.full_output = ""
        job = mock.Mock()
        job.is_alive.return_value = True
        job.read_output.return_value = ""
        job.full_output = "READY\n"
        job.returncode = 0
        agent_loop.register_agent(name="idle-worker", depth=1, role="pool")
        wake_calls: list[tuple] = []

        def wake_cb(agent_id, msg):
            wake_calls.append((agent_id, msg))

        agent_loop.set_trigger_wake_callback(wake_cb)
        try:
            with mock.patch.object(agent_loop, "send_to_agent", return_value=True):
                agent_loop._try_wake_idle_agent("idle-worker", {
                    "type": "watch.trigger",
                    "terminal": "probe",
                    "line": "READY",
                })
        finally:
            agent_loop.set_trigger_wake_callback(None)
        self.assertEqual(len(wake_calls), 1)
        self.assertEqual(wake_calls[0][0], "idle-worker")

    def test_trigger_wake_skips_busy_agent(self):
        """The wake callback must not fire for an agent with an active assignment."""
        agent_loop.register_agent(name="busy-worker", depth=1, role="pool")
        agent_loop._agent_registry["busy-worker"].status = "running"
        wake_calls: list = []
        agent_loop.set_trigger_wake_callback(
            lambda aid, msg: wake_calls.append((aid, msg)))
        try:
            agent_loop._try_wake_idle_agent("busy-worker", {
                "type": "watch.trigger", "terminal": "t", "line": "x"})
        finally:
            agent_loop.set_trigger_wake_callback(None)
        self.assertEqual(wake_calls, [])


class LazySnapshotTests(unittest.TestCase):
    def setUp(self):
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("auto_snapshot", True)
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", False)

    def tearDown(self):
        agent_loop.reset_runtime_config()

    def test_conversation_does_not_snapshot_before_thinking(self):
        deps = _deps({
            "reply": "hello", "tool_calls": [], "finish_reason": "stop",
            "done": True, "error": False,
        })
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch("snapshot.create") as create:
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "hello", {}, {}, [], events_cb=lambda _events: None,
                max_loops_override=1)

        create.assert_not_called()
        self.assertEqual(result["msg"], "hello")

    def test_snapshot_is_deferred_until_before_mutating_tool(self):
        responses = iter([
            {
                "reply": "writing",
                "tool_calls": [{
                    "name": "fs.write",
                    "arguments": {"path": "result.txt", "content": "ok"},
                }],
                "finish_reason": "tool_calls", "done": False, "error": False,
            },
            {
                "reply": "done", "tool_calls": [], "finish_reason": "stop",
                "done": True, "error": False,
            },
        ])
        deps = _deps()
        deps.call_backend = lambda **_kwargs: next(responses)
        order = []

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch("snapshot.create", side_effect=lambda *_a: (
                    order.append("snapshot") or {"sha": "abc"})) as create, \
                mock.patch.object(
                    tools.get_registry(), "invoke",
                    side_effect=lambda *_a, **_kw: (
                        order.append("write") or {"ok": True, "result": "ok"})):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(
                deps, "write it", {}, {}, [], events_cb=lambda _events: None,
                max_loops_override=2)

        create.assert_called_once()
        self.assertEqual(order[:2], ["snapshot", "write"])


class ThinkingLiveTests(unittest.TestCase):
    def setUp(self):
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("auto_snapshot", False)
        agent_loop.set_runtime_config("loop_delay", 0)

    def tearDown(self):
        agent_loop.reset_runtime_config()

    def test_live_does_not_redirect_stdout_back_into_tee_console(self):
        options = {}

        class FakeLive:
            def __init__(self, *_args, **kwargs):
                options.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def refresh(self):
                pass

        deps = _deps({
            "reply": "hello", "tool_calls": [], "finish_reason": "stop",
            "done": True, "error": False,
        })
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch("rich.live.Live", FakeLive):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(
                deps, "hello", {}, {}, [], events_cb=lambda _events: None,
                max_loops_override=1)

        self.assertIs(options["redirect_stdout"], False)
        self.assertIs(options["redirect_stderr"], False)

    def test_live_conflict_falls_back_without_duplicate_backend_call(self):
        calls = []

        def backend(**kwargs):
            calls.append(kwargs)
            on_chunk = kwargs.get("on_chunk")
            if on_chunk:
                on_chunk("reply", "hello")
            return {
                "reply": "hello", "tool_calls": [], "finish_reason": "stop",
                "done": True, "error": False,
            }

        deps = _deps()
        deps.call_backend = backend
        from rich.live import Live
        from rich.text import Text

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            # Deterministically occupy the same Console. The foreground loop
            # must degrade to event-only streaming rather than raising the
            # production LiveError or retrying a billed request.
            outer = Live(Text("occupied"), console=deps.console,
                         auto_refresh=False)
            outer.start(refresh=False)
            try:
                result = agent_loop.run_agent_loop(
                    deps, "hello", {}, {}, [],
                    events_cb=lambda _events: None, max_loops_override=1)
            finally:
                outer.stop()

        self.assertEqual(result["msg"], "hello")
        self.assertEqual(len(calls), 1)

    def test_parent_and_nonblocking_child_stream_concurrently(self):
        agent_loop.close_all_agents()
        parent = agent_loop.register_agent(name="primary", role="primary")
        barrier = threading.Barrier(2, timeout=3)
        calls = []
        emitted = []

        def backend(**kwargs):
            calls.append(agent_loop.get_thread_agent_id() or "foreground")
            barrier.wait()
            on_chunk = kwargs.get("on_chunk")
            if on_chunk:
                on_chunk("reply", "ok")
            return {
                "reply": "ok", "tool_calls": [], "finish_reason": "stop",
                "done": True, "error": False,
            }

        deps = _deps()
        deps.call_backend = backend
        try:
            with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                    mock.patch("worktree_manager.is_git_repo", return_value=False):
                Path(".laintas").mkdir()
                child_id = agent_loop.spawn_subagent(
                    parent.id, "child task", deps, session={},
                    events_cb=lambda events: emitted.extend(events))
                result = agent_loop.run_agent_loop(
                    deps, "parent task", {}, parent.state,
                    parent.chat_history,
                    events_cb=lambda events: emitted.extend(events),
                    agent_id=parent.id, max_loops_override=1)
                child = agent_loop.wait_for_agent(child_id, timeout=3)
        finally:
            agent_loop.close_all_agents()

        self.assertEqual(result["msg"], "ok")
        self.assertIsNotNone(child)
        self.assertEqual(child.status, "done")
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(event.get("type") == "ai_stream"
                            for event in emitted))

    def test_parent_and_six_nonblocking_children_stream_concurrently(self):
        agent_loop.close_all_agents()
        old_max = agent_loop._max_concurrent
        agent_loop._max_concurrent = 8
        parent = agent_loop.register_agent(name="primary", role="primary")
        barrier = threading.Barrier(7, timeout=5)
        calls = []
        calls_lock = threading.Lock()

        def backend(**kwargs):
            with calls_lock:
                calls.append(agent_loop.get_thread_agent_id() or "foreground")
            barrier.wait()
            on_chunk = kwargs.get("on_chunk")
            if on_chunk:
                on_chunk("reply", "ok")
            return {
                "reply": "ok", "tool_calls": [], "finish_reason": "stop",
                "done": True, "error": False,
            }

        deps = _deps()
        deps.call_backend = backend
        children = []
        try:
            with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                    mock.patch("worktree_manager.is_git_repo", return_value=False):
                Path(".laintas").mkdir()
                for index in range(6):
                    children.append(agent_loop.spawn_subagent(
                        parent.id, f"child {index}", deps, session={},
                        events_cb=lambda _events: None,
                        name=f"stream-child-{index}"))
                result = agent_loop.run_agent_loop(
                    deps, "parent", {}, parent.state, parent.chat_history,
                    events_cb=lambda _events: None, agent_id=parent.id,
                    max_loops_override=1)
                infos = [agent_loop.wait_for_agent(cid, timeout=5)
                         for cid in children]
        finally:
            agent_loop._max_concurrent = old_max
            agent_loop.close_all_agents()

        self.assertEqual(result["msg"], "ok")
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(info is not None and info.status == "done"
                            for info in infos))
        self.assertEqual(agent_loop._running_count, 0)

    def test_depth_zero_subagent_is_still_background(self):
        agent_loop.close_all_agents()
        subagent = agent_loop.register_agent(
            name="hwo-node", role="subagent", depth=0)
        entered = []

        class ForbiddenLive:
            def __init__(self, *_args, **_kwargs):
                entered.append(True)
                raise AssertionError("background subagent tried to own Live")

        deps = _deps({
            "reply": "done", "tool_calls": [], "finish_reason": "stop",
            "done": True, "error": False,
        })
        try:
            with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                    mock.patch("rich.live.Live", ForbiddenLive):
                Path(".laintas").mkdir()
                result = agent_loop.run_agent_loop(
                    deps, "node", {}, subagent.state, subagent.chat_history,
                    events_cb=lambda _events: None, depth=0,
                    agent_id=subagent.id, max_loops_override=1)
        finally:
            agent_loop.close_all_agents()

        self.assertEqual(result["msg"], "done")
        self.assertEqual(entered, [])


class BackgroundCriticTests(unittest.TestCase):
    """The progress critic runs off the user's clock and lands a turn later."""

    def test_critic_call_is_not_serialized_into_the_turn(self):
        import critic as critic_mod

        main_calls = []
        critic_calls = []
        critic_started = threading.Event()
        release_critic = threading.Event()

        def backend(**kwargs):
            if kwargs.get("system_prompt") == critic_mod.SYSTEM_PROMPT:
                critic_calls.append(kwargs)
                critic_started.set()
                # Held open until the NEXT main call happens. If the loop
                # waited for the critic, that call could never happen and this
                # would sit here for the full timeout — which the elapsed-time
                # assertion below turns into a failure.
                release_critic.wait(timeout=6)
                return {"reply": '{"on_track": true, "score": 90, "issue": ""}',
                        "tool_calls": [], "done": False, "error": False}
            main_calls.append(kwargs)
            if len(main_calls) >= 2:
                release_critic.set()
            if len(main_calls) >= 3:
                return {"reply": "finished", "tool_calls": [],
                        "finish_reason": "stop", "done": True, "error": False}
            return {"reply": "working", "tool_calls": [
                        {"name": "fs.read", "arguments": {"path": "a.txt"}}],
                    "finish_reason": "tool_calls", "done": False, "error": False}

        deps = _deps()
        deps.call_backend = backend
        overrides = {"critic_min_loop": 0, "critic_interval": 1}
        real_config = agent_loop.get_runtime_config

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"), \
                mock.patch.object(agent_loop, "get_runtime_config",
                                  side_effect=lambda k: overrides.get(k, real_config(k))):
            Path(".laintas").mkdir()
            Path("a.txt").write_text("hello", encoding="utf-8")
            _t0 = time.monotonic()
            agent_loop.run_agent_loop(deps, "keep going", {}, {}, [],
                                      max_loops_override=3)
            _elapsed = time.monotonic() - _t0

        release_critic.set()
        self.assertTrue(critic_started.is_set(), "critic never ran")
        self.assertGreaterEqual(len(main_calls), 2)
        # Serialized, the second main call would have had to wait out the
        # critic's 6s hold. Concurrent, it goes straight through.
        self.assertLess(_elapsed, 4.0,
                        "the loop blocked on the background critic")


class BackendSignatureLadderTests(unittest.TestCase):
    """One backend entry point; older signatures degrade one rung at a time."""

    def test_older_backend_keeps_thread_and_tool_authorization(self):
        seen = []

        def legacy_backend(session, message, system_prompt, current_path,
                           history=None, on_chunk=None, lang="EN",
                           interrupt_event=None, messages=None,
                           allowed_tool_names=None, model_override=None):
            # No provider_override / task_kind / trajectory_id / context_capture.
            seen.append({"messages": messages,
                         "allowed_tool_names": allowed_tool_names})
            return {"reply": "ok", "tool_calls": [], "finish_reason": "stop",
                    "done": True, "error": False}

        deps = _deps()
        deps.call_backend = legacy_backend
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(deps, "hello", {}, {}, [],
                                      max_loops_override=2)

        self.assertTrue(seen)
        # It dropped only the fields it cannot accept: the message thread and
        # the authorization set — the two that actually change the answer —
        # still made it through.
        self.assertIsNotNone(seen[-1]["messages"])
        self.assertTrue(seen[-1]["allowed_tool_names"])


class ReadOnlyBatchGateTests(unittest.TestCase):
    """A concurrent read-only turn must still pass every gate before running."""

    def _run_turn(self, tool_policy=None, hook_denies=None):
        responses = iter([
            {"reply": "batch", "tool_calls": [
                {"name": "fs.read", "arguments": {"path": "a.txt"}},
                {"name": "web.fetch", "arguments": {"url": "http://example.com"}},
             ], "finish_reason": "tool_calls", "done": False, "error": False},
            {"reply": "done", "tool_calls": [], "finish_reason": "stop",
             "done": True, "error": False},
        ])
        deps = _deps()
        deps.call_backend = lambda **kwargs: next(responses)
        employee = agent_loop.register_agent(
            name="ro-batch", depth=1, role="pool",
            profile=agent_loop.EmployeeProfile(tool_policy=tool_policy))
        invoked = []
        real_invoke = tools.get_registry().invoke

        def spy(name, args, ctx, *a, **kw):
            invoked.append(name)
            if name == "web.fetch":
                return {"ok": True, "result": "FETCHED"}
            return real_invoke(name, args, ctx, *a, **kw)

        def trigger(event, payload):
            if (event == "pre_tool" and hook_denies
                    and payload.get("tool") in hook_denies):
                return False, {}
            return True, {}

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            Path("a.txt").write_text("hello", encoding="utf-8")
            with mock.patch.object(tools.get_registry(), "invoke", side_effect=spy), \
                    mock.patch.object(agent_loop.hooks_mod, "trigger", side_effect=trigger):
                result = agent_loop.run_agent_loop(
                    deps, "read and fetch", {}, employee.state,
                    employee.chat_history, depth=1, agent_id=employee.id,
                    max_loops_override=3)
        return invoked, result

    def test_policy_denied_call_is_not_executed_by_the_batch(self):
        # The batch used to run the whole turn up front, so a call the gates
        # were about to refuse had already hit the network by the time the
        # model was told it was blocked.
        invoked, result = self._run_turn(
            tool_policy=agent_loop.AgentToolPolicy(allowed_tools=["fs.read"]))
        self.assertIn("fs.read", invoked)
        self.assertNotIn("web.fetch", invoked)
        self.assertIn("not available to agent", result["state"]["lastOutput"])

    def test_pre_tool_hook_can_block_a_batched_call(self):
        invoked, _ = self._run_turn(hook_denies={"web.fetch"})
        self.assertIn("fs.read", invoked)
        self.assertNotIn("web.fetch", invoked)

    def test_unblocked_batch_still_runs_every_call(self):
        invoked, _ = self._run_turn()
        self.assertEqual(sorted(invoked), ["fs.read", "web.fetch"])


class FinalTurnWrapUpTests(unittest.TestCase):
    """The last allowed iteration must be tool-free (mirrors AutonomousKernel)."""

    def _run(self, max_loops):
        calls = []
        responses = iter([
            {
                "reply": "still working",
                "tool_calls": [{
                    "name": "fs.read",
                    "arguments": {"path": "a.txt"},
                }],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            },
            {
                "reply": "here is my summary",
                "tool_calls": [],
                "finish_reason": "stop",
                "done": False,
                "error": False,
            },
        ])
        deps = _deps()

        def backend(**kwargs):
            calls.append(kwargs)
            try:
                return next(responses)
            except StopIteration:
                return {"reply": "idle", "tool_calls": [], "finish_reason": "stop",
                        "done": True, "error": False}

        deps.call_backend = backend
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            Path("a.txt").write_text("hello", encoding="utf-8")
            result = agent_loop.run_agent_loop(
                deps, "read a.txt then summarize", {}, {}, [],
                max_loops_override=max_loops)
        return calls, result

    def test_last_iteration_sends_no_tools_and_asks_for_a_final_answer(self):
        calls, result = self._run(2)
        self.assertEqual(len(calls), 2)
        # First iteration: tools offered as usual.
        self.assertTrue(calls[0]["allowed_tool_names"])
        # Final iteration: no schemas at all, so the provider cannot call a tool.
        self.assertEqual(calls[1]["allowed_tool_names"], set())
        last_prompt = "\n".join(
            str(m.get("content") or "") for m in (calls[1]["messages"] or []))
        self.assertIn("<final_step>", last_prompt)
        self.assertNotIn("<final_step>", "\n".join(
            str(m.get("content") or "") for m in (calls[0]["messages"] or [])))
        # The prose answer is delivered, but the run is still marked as having
        # hit the budget so /continue stays available (session_store treats
        # max_loops_wrapup as continuable).
        self.assertIn("summary", result["state"]["lastReply"])
        self.assertEqual(result.get("exit_reason"),
                         agent_loop.TRANSITION_MAX_LOOPS_WRAPUP)
        self.assertTrue(result["state"].get("_max_loops_exhausted"))
        import session_store
        self.assertTrue(session_store.is_continuable_reason(
            agent_loop.TRANSITION_MAX_LOOPS_WRAPUP))

    def test_single_iteration_run_keeps_its_tools(self):
        # A one-iteration budget is the whole run, not a wrap-up turn: stripping
        # its tools would leave it unable to do anything at all.
        calls, _ = self._run(1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["allowed_tool_names"])
        self.assertNotIn("<final_step>", "\n".join(
            str(m.get("content") or "") for m in (calls[0]["messages"] or [])))


if __name__ == "__main__":
    unittest.main()
