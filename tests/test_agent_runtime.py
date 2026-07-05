import io
import os
import queue
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
import agent_persistence
import agent_roles
import tools


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


class AgentSchedulerTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop._max_concurrent = 8

    def tearDown(self):
        agent_loop.close_all_agents()
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


class AgentIsolationTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", False)

    def tearDown(self):
        while not agent_loop.get_user_message_queue().empty():
            agent_loop.get_user_message_queue().get_nowait()
        agent_loop.close_all_agents()
        agent_loop.reset_runtime_config()

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
        ctx = tools.ToolCtx(
            agent_id="primary",
            depth=0,
            register_agent_fn=agent_loop.register_agent,
            get_agent=agent_loop.get_agent,
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
        self.assertIsNone(tools.get_registry().get("agent.switch"))

    def test_agent_station_tool_uses_logical_station_on_windows(self):
        employee = agent_loop.register_agent(
            name="win-worker", depth=1, role="pool")
        deps = mock.Mock()
        ctx = tools.ToolCtx(
            deps=deps,
            agent_id=employee.id,
            depth=1,
            get_agent=agent_loop.get_agent,
            get_terminal=lambda _name: None,
            register_terminal=mock.Mock(),
            station_agent=mock.Mock(return_value=True),
        )
        with mock.patch.object(tools.os, "name", "nt"), \
                mock.patch.dict(tools.os.environ, {"COMSPEC": "cmd.exe"}):
            result = tools._bi_agent_station({"name": "win-shell"}, ctx)

        self.assertTrue(result["ok"])
        deps.SubTerminalSession.assert_not_called()
        ctx.register_terminal.assert_called_once_with(
            None, "cmd.exe", 1, name="win-shell")
        ctx.station_agent.assert_called_once_with(employee.id, "win-shell")

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

    def test_assignment_uses_employee_prompt_and_fresh_context(self):
        prompts = []
        deps = _deps()

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
        employee.stationed_terminal = "alice-work"
        employee.home_terminal = "alice-work"

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


if __name__ == "__main__":
    unittest.main()
