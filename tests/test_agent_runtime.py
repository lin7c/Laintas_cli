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

        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
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

    def test_read_only_roles_cannot_escape_through_shell(self):
        for role in ("explorer", "architect", "reviewer",
                     "silent-failure-hunter", "tester"):
            self.assertFalse(
                agent_roles.is_tool_allowed_for_role("shell.exec", role), role)


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
