import io
import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import agent_ui_events
import event_log
import laintas_cli
import paths
import policy
import session_store
import task_manager


class SubTerminalSessionTests(unittest.TestCase):
    def test_tmux_exit_marker_preserves_real_status_but_is_hidden_from_output(self):
        session = laintas_cli.SubTerminalSession("printf final; exit 13")
        marker_line = f"{session._tmux_exit_marker}:13"
        captured = f"final\n{marker_line}\n"

        session._capture_tmux_returncode(captured)

        self.assertEqual(session.returncode, 13)
        self.assertEqual(session._clean_tmux_markers(captured), "final")

    def test_tmux_start_sets_remain_on_exit_before_releasing_command(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-test"}), \
                mock.patch.object(laintas_cli.subprocess, "run",
                                  side_effect=[completed, completed, completed]) as run:
            session = laintas_cli.SubTerminalSession("exit 0")
            session.start()

        self.assertTrue(session._alive)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:2], ["tmux", "new-window"])
        self.assertEqual(commands[1][:2], ["tmux", "set-window-option"])
        self.assertEqual(commands[2][:2], ["tmux", "send-keys"])
        self.assertEqual(commands[2][-1], "Enter")


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
        # Isolate compaction-mechanics tests from the background memory
        # consolidation side-effect (exercised separately below).
        agent_loop.set_runtime_config("mem_extract_on_compact", False)

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

    def test_interactive_history_preserves_assistant_tool_chronology(self):
        deps, _ = _deps([
            {
                "reply": "我先继续检查。",
                "tool_calls": [{
                    "name": "time.now", "arguments": {},
                }],
                "finish_reason": "tool_calls", "done": False,
                "error": False,
            },
            {
                "reply": "检查完成。", "tool_calls": [],
                "finish_reason": "stop", "done": False, "error": False,
            },
        ])
        history = [{
            "role": "user", "content": "检查", "input_kind": "prompt",
        }]
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "检查", {}, {}, history,
                events_cb=lambda _events: None,
                max_loops_override=3,
            )

        self.assertTrue(result["_history_recorded"])
        self.assertEqual(
            [message["role"] for message in history],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(history[1]["content"], "我先继续检查。")
        self.assertEqual(history[2]["tool_name"], "time.now")
        self.assertEqual(history[3]["content"], "检查完成。")

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

    def test_provider_done_does_not_skip_tool_result_round_trip(self):
        result, calls, _ = self._run([
            {
                "reply": "",
                "tool_calls": [{
                    "name": "time.now",
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

    def test_repeated_output_warns_without_interrupting_by_default(self):
        repeated_calls = [{
            "reply": "",
            "tool_calls": [{"name": "time.now", "arguments": {}}],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
        } for _ in range(4)]
        result, calls, _ = self._run(repeated_calls + [{
            "reply": "finished despite repeated output",
            "tool_calls": [],
            "finish_reason": "stop",
            "done": False,
            "error": False,
        }], max_loops=6)

        self.assertEqual(len(calls), 5)
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_END_TURN)

    def test_repeated_output_can_interrupt_when_configured(self):
        agent_loop.set_runtime_config("repetition_policy", "interrupt")
        repeated_calls = [{
            "reply": "",
            "tool_calls": [{"name": "time.now", "arguments": {}}],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
        } for _ in range(5)]
        result, calls, _ = self._run(repeated_calls, max_loops=6)

        self.assertEqual(len(calls), 4)
        self.assertFalse(result["success"])
        self.assertEqual(
            result["exit_reason"], agent_loop.TRANSITION_REPETITION)

    def test_repeated_failures_warn_without_hard_blocking_by_default(self):
        repeated_calls = [{
            "reply": "",
            "tool_calls": [{"name": "missing.tool", "arguments": {}}],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
        } for _ in range(4)]
        result, calls, _ = self._run(repeated_calls + [{
            "reply": "reported the failed sub-goal",
            "tool_calls": [],
            "finish_reason": "stop",
            "done": False,
            "error": False,
        }], max_loops=6)

        self.assertEqual(len(calls), 5)
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_END_TURN)

    def test_no_action_limit_warns_without_ending_the_task(self):
        ambiguous_turns = [{
            "reply": f"intermediate reasoning {index}",
            "tool_calls": [],
            "finish_reason": "unknown",
            "done": False,
            "error": False,
        } for index in range(4)]
        result, calls, _ = self._run(ambiguous_turns + [{
            "reply": "finished after ambiguous turns",
            "tool_calls": [],
            "finish_reason": "stop",
            "done": False,
            "error": False,
        }], max_loops=6)

        self.assertEqual(len(calls), 5)
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_reason"], agent_loop.TRANSITION_END_TURN)

    def test_idle_warning_does_not_preempt_silent_failure_error(self):
        agent_loop.set_runtime_config("staleness_limit", 1)
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
        self.assertEqual(
            result["exit_reason"], agent_loop.TRANSITION_SILENT_FAILURE)

    def test_short_final_reply_has_no_decorative_dot_prefix(self):
        deps, _ = _deps([{
            "reply": "你好！有什么可以帮你的？",
            "tool_calls": [],
            "finish_reason": "stop",
            "done": False,
            "error": False,
        }])
        history = [{"role": "user", "content": "你好"}]
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            agent_loop.run_agent_loop(
                deps, "你好", {}, {}, history,
                events_cb=lambda events: None,
                max_loops_override=2,
            )
        rendered = deps.console.file.getvalue()
        self.assertIn("你好！有什么可以帮你的？", rendered)
        self.assertNotIn("· 你好！有什么可以帮你的？", rendered)

    def test_manual_compaction_summarizes_head_and_keeps_recent_user_turns(self):
        deps, calls = _deps([{
            "reply": "anchored compact summary",
            "tool_calls": [],
            "finish_reason": "stop",
            "done": True,
            "error": False,
        }])
        messages = [{"role": "user", "content": "initial task"}]
        for index in range(1, 5):
            messages.extend([
                {"role": "assistant", "content": f"answer {index}"},
                {"role": "user", "content": f"follow-up {index}"},
            ])
        state = {
            "objective": "initial task",
            "_thread_messages": messages,
            "terminalHistory": [],
        }

        result = agent_loop.compact_session_context(
            deps, {}, state,
            [{"role": "user", "content": "follow-up 4"}],
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["summary_created"])
        self.assertEqual(len(calls), 1)
        compacted = state["_thread_messages"]
        # The obsolete first task is summarized instead of permanently pinned;
        # the live objective and durable rules are injected separately.
        self.assertIn("anchored compact summary", compacted[0]["content"])
        self.assertEqual(compacted[1]["role"], "user")
        self.assertLess(result["after_messages"], result["messages"])

    def test_manual_compaction_is_noop_for_short_context(self):
        deps, calls = _deps([{"reply": "must not be called"}])
        state = {
            "_thread_messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        }
        original = copy.deepcopy(state)

        result = agent_loop.compact_session_context(deps, {}, state, [])

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(state, original)
        self.assertEqual(calls, [])

    def test_manual_compaction_rolls_back_when_summary_fails(self):
        deps, calls = _deps([{"reply": "", "tool_calls": []}])
        messages = [{"role": "user", "content": "initial"}]
        for index in range(1, 5):
            messages.extend([
                {"role": "assistant", "content": f"answer {index}"},
                {"role": "user", "content": f"follow-up {index}"},
            ])
        state = {"_thread_messages": messages, "terminalHistory": []}
        original = copy.deepcopy(state)

        result = agent_loop.compact_session_context(deps, {}, state, [])

        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(state, original)

    def test_compaction_triggers_memory_consolidation(self):
        # When mem_extract_on_compact is on, a successful compaction fires a
        # background pass that mines the just-compacted context for durable
        # memories. Run the worker inline (patch Thread) for determinism.
        agent_loop.set_runtime_config("mem_extract_on_compact", True)
        deps, calls = _deps([{
            "reply": "anchored compact summary",
            "tool_calls": [], "finish_reason": "stop", "done": True, "error": False,
        }])
        messages = [{"role": "user", "content": "initial task"}]
        for index in range(1, 5):
            messages.extend([
                {"role": "assistant", "content": f"answer {index}"},
                {"role": "user", "content": f"follow-up {index}"},
            ])
        state = {"_thread_messages": messages, "terminalHistory": []}

        seen = {}

        def _inline_thread(target=None, daemon=None, **kw):
            class _T:
                def start(self_inner):
                    target()
            return _T()

        def _fake_extract(text, llm_fn, *, session=None):
            seen["text"] = text
            llm_fn([{"role": "user", "content": "x"}])  # exercise the injected llm_fn
            return ["consolidated"]

        with mock.patch.object(agent_loop.threading, "Thread", _inline_thread), \
                mock.patch.object(agent_loop.mem_extract, "extract_and_store", _fake_extract):
            result = agent_loop.compact_session_context(
                deps, {"token": "t"}, state,
                [{"role": "user", "content": "follow-up 4"}])

        self.assertTrue(result["changed"])
        self.assertIn("text", seen)                 # consolidation ran
        self.assertIn("summary", seen["text"].lower())
        self.assertEqual(len(calls), 2)             # summarizer + consolidation llm_fn

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
    def test_atomic_write_skips_semantically_identical_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_store._LAST_WRITE_FINGERPRINTS.clear()
            target = Path(tmp) / "session.json"
            first = {"session_id": "s1", "timestamp": 1, "state": {"x": 1}}
            second = {"session_id": "s1", "timestamp": 2, "state": {"x": 1}}

            self.assertTrue(
                session_store._atomic_write_json_if_changed(target, first))
            self.assertFalse(
                session_store._atomic_write_json_if_changed(target, second))
            raw = target.read_text(encoding="utf-8")
            self.assertNotIn("\n  ", raw)
            self.assertEqual(json.loads(raw)["state"]["x"], 1)

    def test_current_session_pointer_is_instance_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)):
            with mock.patch.object(paths, "TERMINAL_ID", "term-a"):
                session_a = session_store.create_session(
                    "/work", {"objective": "A"}, [])
                path_a = session_store._current_path("/work")
            with mock.patch.object(paths, "TERMINAL_ID", "term-b"):
                session_b = session_store.create_session(
                    "/work", {"objective": "B"}, [])
                path_b = session_store._current_path("/work")

            self.assertNotEqual(path_a, path_b)
            self.assertTrue(path_a.exists())
            self.assertTrue(path_b.exists())
            self.assertEqual(
                json.loads(path_a.read_text(encoding="utf-8"))["session_id"],
                session_a["session_id"],
            )
            self.assertEqual(
                json.loads(path_b.read_text(encoding="utf-8"))["session_id"],
                session_b["session_id"],
            )

    def test_legacy_current_pointer_is_claimed_by_current_instance(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)), \
                mock.patch.object(paths, "TERMINAL_ID", "term-a"):
            legacy_session = {
                "id": "legacy",
                "session_id": "legacy",
                "kind": "live",
                "cwd": "/work",
                "closed_at": None,
                "chat_history": [],
                "state": {"objective": "legacy"},
                "agent_state": {"objective": "legacy"},
            }
            legacy_path = session_store._legacy_current_path("/work")
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(
                json.dumps(legacy_session), encoding="utf-8")

            loaded = session_store.load_current_session("/work")

            self.assertEqual(loaded["session_id"], "legacy")
            self.assertFalse(legacy_path.exists())
            self.assertTrue(session_store._current_path("/work").exists())

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
            self.assertTrue(list(Path(tmp).glob("*_current_*.json.corrupt-*")))

    def test_closed_current_does_not_resurrect_older_orphan(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)):
            orphan = session_store.create_session(
                "/work", {"objective": "stale orphan"}, [])
            current = session_store.create_session(
                "/work", {"objective": "current"}, [])
            session_store.close_session(current)

            self.assertTrue(
                session_store._session_path(
                    "/work", orphan["session_id"]).exists())
            self.assertIsNone(session_store.load_current_session("/work"))


class RemoteAgentIdentityTests(unittest.TestCase):
    def test_foreground_wrapper_queues_into_existing_primary_run(self):
        agent_loop.close_all_agents()
        primary = agent_loop.register_agent(
            name="primary", depth=0, role="primary")
        agent_loop.set_current_agent_id(primary.id)
        history = [{"role": "user", "content": "follow-up"}]
        admitted, _detail = agent_loop.begin_primary_run(primary.id)
        self.assertTrue(admitted)
        try:
            with mock.patch.object(laintas_cli, "run_agent_loop") as run:
                result = laintas_cli._run_agent_loop_with_interrupt(
                    mock.Mock(), "follow-up", {}, primary.state, history)

            run.assert_not_called()
            self.assertTrue(result["_queued_for_primary"])
            self.assertEqual(primary.message_queue.get_nowait(), "follow-up")
            self.assertEqual(history, [])
        finally:
            agent_loop.finish_primary_run(primary.id)
            agent_loop.close_all_agents()

    def test_remote_chat_joins_running_primary_instead_of_starting_second_loop(self):
        agent_loop.close_all_agents()
        agent_ui_events.hub.reset()
        primary = agent_loop.register_agent(
            name="primary", depth=0, role="primary")
        primary.home_terminal = "term0"
        admitted, _detail = agent_loop.begin_primary_run(primary.id)
        self.assertTrue(admitted)
        registry = laintas_cli.AgentRegistry()
        try:
            with mock.patch.object(registry, "_push_final") as final, \
                    mock.patch.object(laintas_cli, "run_agent_loop") as run:
                registry._handle_chat(
                    "req-shared", {"message": "remote update"},
                    lambda: primary.state, lambda: primary.chat_history)

            run.assert_not_called()
            self.assertEqual(primary.message_queue.get_nowait(), "remote update")
            final.assert_called_once()
            self.assertEqual(final.call_args.args[1], "success")
            events = agent_ui_events.hub.agent_events(primary.id)
            self.assertEqual(events[-1].event_type, "user_message")
            self.assertEqual(events[-1].detail, "remote update")
        finally:
            agent_loop.finish_primary_run(primary.id)
            agent_loop.close_all_agents()
            agent_ui_events.hub.reset()
            registry._remote_executor.shutdown(wait=False, cancel_futures=True)
            registry._remote_control_executor.shutdown(wait=False, cancel_futures=True)

    def test_auto_mode_remote_approval_uses_timed_default(self):
        registry = laintas_cli.AgentRegistry()
        pushed = []
        try:
            with mock.patch.object(
                    laintas_cli.mode_manager, "get_auto_confirm_timeout",
                    return_value=0.01) as timeout, \
                    mock.patch.object(
                        registry, "_push_events",
                        side_effect=lambda events, req_id=None: pushed.extend(events)):
                decision = registry._request_approval(
                    "req-auto", "DELETE /tmp/old", "/tmp",
                    destructive=True,
                )
        finally:
            registry._remote_executor.shutdown(wait=False, cancel_futures=True)
            registry._remote_control_executor.shutdown(wait=False, cancel_futures=True)

        self.assertEqual(decision, "approve")
        timeout.assert_called_once_with(destructive=True)
        self.assertEqual(pushed[0]["meta"]["autoApproveAfter"], 0.01)

    def test_remote_poll_includes_instance_id(self):
        with mock.patch.object(paths, "PROCESS_INSTANCE_ID", "process-a"), \
                mock.patch.object(laintas_cli, "get_backend_url",
                                  return_value="https://laintas.com"), \
                mock.patch.object(laintas_cli.time, "sleep"), \
                mock.patch.object(laintas_cli.requests, "get") as get:
            registry = laintas_cli.AgentRegistry()
            registry.agent_id = "agent-1"
            registry.agent_secret = "secret-1"
            registry._running = True

            response = mock.Mock(status_code=200)
            response.json.return_value = {"inputs": []}

            def _get(*args, **kwargs):
                registry._running = False
                return response

            get.side_effect = _get
            registry._poll_loop(lambda: {}, lambda: [])

            self.assertEqual(
                get.call_args.kwargs["params"], {"instanceId": "process-a"})

    def test_remote_heartbeat_includes_instance_id(self):
        with mock.patch.object(paths, "PROCESS_INSTANCE_ID", "process-a"), \
                mock.patch.object(laintas_cli, "get_backend_url",
                                  return_value="https://laintas.com"), \
                mock.patch.object(laintas_cli, "get_all_terminals",
                                  return_value=[]), \
                mock.patch.object(laintas_cli.time, "sleep"), \
                mock.patch.object(laintas_cli.requests, "post") as post:
            registry = laintas_cli.AgentRegistry()
            registry.agent_id = "agent-1"
            registry.agent_secret = "secret-1"
            registry._running = True

            response = mock.Mock(status_code=200)

            def _post(*args, **kwargs):
                registry._running = False
                return response

            post.side_effect = _post
            registry._heartbeat_loop()

            self.assertEqual(
                post.call_args.kwargs["json"]["instanceId"], "process-a")

    def test_remote_register_events_and_unregister_include_instance_id(self):
        profile = laintas_cli.backend_profiles.BackendProfile(
            "test", "official", "https://laintas.com")
        session = {
            "headers": {"Authorization": "Bearer token"},
            "cookies": {},
            "userEmail": "user@example.com",
            "userName": "User",
        }

        with mock.patch.object(paths, "PROCESS_INSTANCE_ID", "process-a"), \
                mock.patch.object(laintas_cli, "get_backend_profile",
                                  return_value=profile), \
                mock.patch.object(laintas_cli, "get_backend_url",
                                  return_value="https://laintas.com"), \
                mock.patch.object(laintas_cli.requests, "post") as post:
            register_resp = mock.Mock(status_code=200)
            register_resp.json.return_value = {
                "agentId": "agent-1",
                "agentSecret": "secret-1",
            }
            event_resp = mock.Mock(status_code=200)
            unregister_resp = mock.Mock(status_code=200)
            post.side_effect = [register_resp, event_resp, unregister_resp]

            registry = laintas_cli.AgentRegistry()
            self.assertEqual(registry.instance_id, "process-a")
            self.assertTrue(registry.register(session, name="primary", quiet=True))
            registry._do_post_events([{"type": "user", "content": "hello"}])
            registry.unregister()

            register_payload = post.call_args_list[0].kwargs["json"]
            events_payload = post.call_args_list[1].kwargs["json"]
            unregister_payload = post.call_args_list[2].kwargs["json"]

        self.assertEqual(register_payload["instanceId"], "process-a")
        self.assertEqual(events_payload["instanceId"], "process-a")
        self.assertEqual(events_payload["state"]["instanceId"], "process-a")
        self.assertEqual(unregister_payload["instanceId"], "process-a")


@contextmanager
def _isolated_policy(root: str, mode: str = "enforce"):
    """Set up an isolated enforce-mode policy so rm triggers needs_approval."""
    root_path = Path(root)
    config_path = root_path / "policy.json"
    audit_path = root_path / "audit.log"
    cfg = copy.deepcopy(policy._DEFAULT_CONFIG)
    cfg["mode"] = mode
    cfg["allowedRoots"] = [root]
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with mock.patch.object(policy, "CONFIG_PATH", config_path), \
            mock.patch.object(policy, "AUDIT_PATH", audit_path):
        policy._config = None
        policy._config_mtime = 0.0
        try:
            yield
        finally:
            policy._config = None
            policy._config_mtime = 0.0


class UserDenialTerminationTests(unittest.TestCase):
    """Verify that denying an approval prompt terminates the agent loop."""

    def setUp(self):
        task_manager.clear_session_tasks()
        agent_loop.reset_runtime_config()
        agent_loop.set_runtime_config("auto_snapshot", False)
        agent_loop.set_runtime_config("loop_delay", 0)
        agent_loop.set_runtime_config("use_message_thread", False)

    def tearDown(self):
        agent_loop.reset_runtime_config()
        task_manager.clear_session_tasks()

    def _run_with_denial(self, responses, max_loops=5, deny_exits_loop=True):
        deps, calls = _deps(responses)
        deps.request_command_approval = lambda cmd, reason: False
        agent_loop.set_runtime_config("deny_exits_loop", deny_exits_loop)
        history = [{"role": "user", "content": "delete a file"}]
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                _isolated_policy(tmp), \
                mock.patch.object(
                    agent_loop.mode_manager, "is_tool_allowed",
                    return_value=True):
            Path(".laintas").mkdir()
            result = agent_loop.run_agent_loop(
                deps, "delete a file", {}, {}, history,
                max_loops_override=max_loops,
            )
        return result, calls

    def test_user_denial_terminates_loop(self):
        """When the user denies command approval, the loop must exit at once."""
        result, calls = self._run_with_denial([{
            "reply": "I'll delete the file.",
            "tool_calls": [{
                "name": "shell.exec",
                "arguments": {"command": "rm file.txt"},
            }],
            "finish_reason": "tool_calls",
            "done": False,
            "error": False,
        }])
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_reason"],
                         agent_loop.TRANSITION_USER_DENIED)
        self.assertEqual(result["turn_status"], "interrupted")
        self.assertEqual(len(calls), 1)

    def test_deny_exits_loop_false_keeps_old_behavior(self):
        """When deny_exits_loop is off, denial is a tool error and the loop continues."""
        result, calls = self._run_with_denial(
            [{
                "reply": "I'll delete the file.",
                "tool_calls": [{
                    "name": "shell.exec",
                    "arguments": {"command": "rm file.txt"},
                }],
                "finish_reason": "tool_calls",
                "done": False,
                "error": False,
            }, {
                "reply": "OK, I won't delete it.",
                "tool_calls": [],
                "finish_reason": "stop",
                "done": False,
                "error": False,
            }],
            deny_exits_loop=False,
        )
        self.assertNotEqual(result["exit_reason"],
                            agent_loop.TRANSITION_USER_DENIED)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
