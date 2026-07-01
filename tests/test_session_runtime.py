import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import event_log
import laintas_cli
import paths
import session_store
import task_manager


@contextmanager
def _chdir(path):
    old = Path.cwd()
    try:
        import os
        os.chdir(path)
        yield
    finally:
        os.chdir(old)


class _Registry:
    agent_id = None

    def _push_events(self, events):
        pass


def _deps(responses):
    calls = []

    def backend(**kwargs):
        calls.append(kwargs)
        value = responses[min(len(calls) - 1, len(responses) - 1)]
        return dict(value)

    deps = agent_loop.LoopDeps(
        read_file=lambda path: None,
        append_file=lambda path, content: None,
        write_file=lambda path, content: None,
        strip_ansi=lambda text: text,
        generate_prompt=lambda: "You are a test agent.",
        call_backend=backend,
        SubTerminalSession=mock.Mock,
        display_command_output=lambda *args, **kwargs: None,
        display_sub_terminal_preview=lambda *args, **kwargs: None,
        display_file_diff=lambda *args, **kwargs: None,
        console=Console(file=io.StringIO(), force_terminal=False),
        Markdown=Markdown,
    )
    return deps, calls


class AgentTerminationTests(unittest.TestCase):
    def setUp(self):
        task_manager.clear_session_tasks()
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("auto_snapshot", False)
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", False)

    def tearDown(self):
        agent_loop.reset_runtime_config()
        task_manager.clear_session_tasks()

    def _run(self, responses, max_loops=5, state=None, prompt="do the task"):
        deps, calls = _deps(responses)
        history = [{"role": "user", "content": prompt}]
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, prompt, {}, state or {}, history,
                max_loops_override=max_loops,
            )
        return result, calls, history

    def test_truncated_prose_continues_instead_of_completing(self):
        result, calls, _ = self._run([
            {
                "reply": "partial output",
                "tool_calls": [],
                "finish_reason": "length",
                "done": False,
                "error": False,
                "_truncated": True,
            },
            {
                "reply": "final output",
                "tool_calls": [],
                "finish_reason": "stop",
                "done": False,
                "error": False,
            },
        ])
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_END_TURN)
        self.assertEqual(result["completion_source"], "provider_stop")
        self.assertIn("partial output", result["msg"])
        self.assertIn("final output", result["msg"])

    def test_empty_provider_turn_is_not_success(self):
        result, calls, _ = self._run([{
            "reply": "",
            "tool_calls": [],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
            "_billing": {},
        }], max_loops=5)
        self.assertEqual(len(calls), 3)
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_SILENT_FAILURE)
        self.assertEqual(result["turn_status"], "failed")

    def test_max_loops_is_incomplete_and_continuable(self):
        result, calls, _ = self._run([{
            "reply": "partial",
            "tool_calls": [],
            "finish_reason": "length",
            "done": False,
            "error": False,
            "_truncated": True,
        }], max_loops=2)
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_MAX_LOOPS)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertTrue(session_store.is_continuable_reason(result["exit_reason"]))

    def test_task_complete_is_explicit_task_completion(self):
        result, calls, _ = self._run([{
            "reply": "",
            "tool_calls": [{
                "name": "task.complete",
                "arguments": {"summary": "verified complete"},
            }],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
        }])
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["success"])
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(result["completion_source"], "task_complete")
        self.assertEqual(result["msg"], "verified complete")

    def test_task_complete_is_ignored_when_same_batch_contains_failure(self):
        result, calls, _ = self._run([
            {
                "reply": "",
                "tool_calls": [
                    {"name": "missing.tool", "arguments": {}},
                    {"name": "task.complete", "arguments": {"summary": "too early"}},
                ],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            },
            {
                "reply": "",
                "tool_calls": [{
                    "name": "task.complete",
                    "arguments": {"summary": "recovered"},
                }],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            },
        ])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["msg"], "recovered")
        self.assertEqual(result["task_status"], "completed")

    def test_natural_continue_restores_previous_active_objective(self):
        result, calls, _ = self._run([
            {
                "reply": "",
                "tool_calls": [{
                    "name": "session.continue",
                    "arguments": {"reason": "user asked to continue"},
                }],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            },
            {
                "reply": "",
                "tool_calls": [{
                    "name": "task.complete",
                    "arguments": {"summary": "done"},
                }],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            },
        ], state={"objective": "task A"}, prompt="continue")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["state"]["objective"], "task A")

    def test_provider_done_does_not_skip_tool_result_round_trip(self):
        result, calls, _ = self._run([
            {
                "reply": "",
                "tool_calls": [{
                    "name": "session.continue",
                    "arguments": {},
                }],
                "finish_reason": "stop",
                "done": True,
                "error": False,
            },
            {
                "reply": "finished after tool result",
                "tool_calls": [],
                "finish_reason": "stop",
                "done": False,
                "error": False,
            },
        ], state={"objective": "task A"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["completion_source"], "provider_stop")

    def test_missing_tool_calls_and_content_filter_are_not_success(self):
        missing, calls, _ = self._run([{
            "reply": "I intended to call a tool",
            "tool_calls": [],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
        }])
        self.assertEqual(len(calls), 3)
        self.assertEqual(missing["exit_reason"], agent_loop.TRANSITION_SILENT_FAILURE)

        filtered, calls, _ = self._run([{
            "reply": "partial filtered text",
            "tool_calls": [],
            "finish_reason": "content_filter",
            "done": False,
            "error": False,
        }])
        self.assertEqual(len(calls), 1)
        self.assertFalse(filtered["success"])
        self.assertEqual(filtered["exit_reason"], agent_loop.TRANSITION_PROVIDER_ERROR)
        self.assertEqual(filtered["turn_status"], "failed")

    def test_crash_continue_adds_admitted_prompt_missing_from_native_thread(self):
        agent_loop.set_runtime_config("use_message_thread", True)
        deps, calls = _deps([{
            "reply": "recovered",
            "tool_calls": [],
            "finish_reason": "stop",
            "done": False,
            "error": False,
        }])
        state = {
            "objective": "new interrupted task",
            "_thread_messages": [{"role": "user", "content": "older task"}],
        }
        history = [{"role": "user", "content": "new interrupted task"}]
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "new interrupted task", {}, state, history,
                continue_thread=True,
                max_loops_override=2,
            )
        sent = calls[0]["messages"]
        user_texts = [m.get("content") for m in sent if m.get("role") == "user"]
        self.assertIn("new interrupted task", user_texts)
        self.assertTrue(result["success"])


class ContinueStateTests(unittest.TestCase):
    def test_continue_resumes_latest_run_and_mutates_repl_state(self):
        runtime_state = {"objective": "task B", "lastOutput": "before"}
        runtime_chat = [{"role": "user", "content": "task B"}]
        live = {
            "session_id": "s1",
            "cwd": "/work",
            "objective": "task A",
            "last_original_input": "task B",
            "last_user_input": "task B",
            "pending_continuation": True,
            "state": {"objective": "stale task A"},
            "chat_history": [{"role": "user", "content": "stale"}],
        }
        deps = SimpleNamespace(Markdown=lambda text: text)
        attrs = {
            "_current_live_session": live,
            "_last_agent_state": runtime_state,
            "_last_chat_history": runtime_chat,
            "_last_original_input": "task B",
            "_last_deps": deps,
            "_last_session": {},
            "_last_events_cb": lambda events: None,
            "_last_existing_session": None,
        }
        for name, value in attrs.items():
            setattr(laintas_cli.handle_meta_command, name, value)

        captured = {}

        def run_loop(_deps, prompt, _session, _state, history, **kwargs):
            captured["prompt"] = prompt
            captured["history"] = history
            return {
                "state": {"objective": "task B", "lastOutput": "after"},
                "msg": "continued result",
                "session": None,
                "exit_reason": "end_turn",
            }

        def sync(live_session, state, history, **kwargs):
            captured["synced_state"] = state
            captured["synced_history"] = history
            return live_session

        old_console = laintas_cli.console
        laintas_cli.console = Console(file=io.StringIO(), force_terminal=False)
        try:
            with mock.patch.object(laintas_cli, "_run_agent_loop_with_interrupt", side_effect=run_loop), \
                    mock.patch.object(laintas_cli.session_store, "sync_runtime", side_effect=sync), \
                    mock.patch.object(laintas_cli.task_manager, "export_active_tasks", return_value=[]):
                self.assertFalse(laintas_cli.handle_meta_command(
                    "/continue", _Registry(), {}))
        finally:
            laintas_cli.console = old_console

        self.assertEqual(captured["prompt"], "task B")
        self.assertIs(captured["history"], runtime_chat)
        self.assertEqual(runtime_state["lastOutput"], "after")
        self.assertIs(captured["synced_state"], runtime_state)
        self.assertIs(captured["synced_history"], runtime_chat)
        self.assertEqual(runtime_chat[-1]["content"], "continued result")


class EventLogTests(unittest.TestCase):
    def test_incomplete_runs_are_matched_by_run_id_and_sequence_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            event_log._SEQ_BY_PATH.clear()
            first = event_log.append("prompt_admitted", run_id="r1", text="one")
            event_log.append("prompt_admitted", run_id="r2", text="two")
            event_log.append("turn_ended", run_id="r2", reason="end_turn")
            self.assertEqual(event_log.last_incomplete_task()["run_id"], "r1")
            event_log.acknowledge_incomplete(event_log.last_incomplete_task())
            self.assertIsNone(event_log.last_incomplete_task())

            event_log._SEQ_BY_PATH.clear()  # simulate a process restart
            after_restart = event_log.append("prompt_admitted", run_id="r3", text="three")
            self.assertGreater(after_restart, first)


class SessionStoreTests(unittest.TestCase):
    def test_explicit_empty_state_does_not_restore_stale_agent_state(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)):
            session = {
                "id": "s1",
                "session_id": "s1",
                "cwd": "/work",
                "state": {},
                "agent_state": {"stale": True},
            }
            session_store.save_session(session)
            self.assertNotIn("stale", session["state"])
            self.assertNotIn("stale", session["agent_state"])

    def test_corrupt_current_index_recovers_live_copy(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)):
            session = session_store.create_session(
                "/work", {"objective": "recover me"}, [])
            current = session_store._current_path("/work")
            current.write_text("{broken", encoding="utf-8")

            recovered = session_store.load_current_session("/work")
            warning = session_store.consume_last_error()

            self.assertEqual(recovered["session_id"], session["session_id"])
            self.assertEqual(recovered["state"]["objective"], "recover me")
            self.assertIn("recovered", warning.lower())
            self.assertTrue(list(Path(tmp).glob("*_current.json.corrupt-*")))


if __name__ == "__main__":
    unittest.main()
