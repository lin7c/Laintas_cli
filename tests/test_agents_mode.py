import queue
import io
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.mouse_events import MouseEventType
from rich.console import Console

import agent_loop
import agent_ui_events
import agents_mode
import hwo_ui
import hwo_runner
import laintas_cli


class AgentsModeTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        agent_ui_events.hub.reset()
        self.sessions = {}
        self._terminal("term0")

    def tearDown(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        agent_ui_events.hub.reset()

    def _terminal(self, name, parent=None):
        session = mock.Mock()
        session.is_alive.return_value = True
        self.sessions[name] = session
        return agent_loop.register_terminal(
            session, "/bin/sh", len(self.sessions) - 1,
            name=name, parent_terminal=parent)

    def _agent(self, name, terminal="term0", role="pool"):
        agent = agent_loop.register_agent(name=name, role=role)
        agent.home_terminal = terminal
        return agent

    def test_select_changes_dialog_only_not_deployment(self):
        first = self._agent("first")
        second = self._agent("second")
        self.assertTrue(agent_loop.station_agent(first.id, "term0"))
        controller = agents_mode.AgentsModeController("term0", mock.Mock(), {})

        self.assertTrue(controller.select(second.id))

        terminal = agent_loop.get_terminal("term0")
        self.assertEqual(terminal.dialog_agent_id, second.id)
        self.assertEqual(terminal.stationed_agent_id, first.id)
        self.assertEqual(agent_loop.agent_deployment_terminal(second), None)

    def test_one_shot_route_does_not_change_focus(self):
        first = self._agent("first")
        second = self._agent("AI-2")
        agent_loop.set_dialog_agent_for_terminal("term0", first.id)
        controller = agents_mode.AgentsModeController("term0", mock.Mock(), {})
        with mock.patch.object(
                agent_loop, "start_agent_assignment",
                return_value=(True, "ok", object())) as start:
            controller.dispatch("@AI-2 inspect auth")

        self.assertEqual(controller.selected_id, first.id)
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0:2], (second.id, "inspect auth"))

    def test_foreground_employee_continues_via_repl_without_new_assignment(self):
        scout = self._agent("scout")
        agent_loop.set_current_agent_id(scout.id)
        scout.chat_history = [{"role": "user", "content": "existing task"}]
        submit = mock.Mock(return_value=(True, "Sent"))
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, repl_submit_cb=submit)
        controller.select(scout.id)
        with mock.patch.object(agent_loop, "start_agent_assignment") as start:
            controller.dispatch("continue that work")
        submit.assert_called_once_with("continue that work")
        start.assert_not_called()
        self.assertEqual(scout.chat_history[0]["content"], "existing task")
        self.assertEqual(controller._input_action(scout), "continue")

    def test_panel_selection_keeps_foreground_and_shows_new_task_semantics(self):
        primary = self._agent("primary", role="primary")
        scout = self._agent("scout")
        agent_loop.set_current_agent_id(primary.id)
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, repl_submit_cb=mock.Mock())
        controller.select(scout.id)
        self.assertEqual(agent_loop.get_current_agent_id(), primary.id)
        hints = "".join(text for _style, text in controller.hint_fragments())
        self.assertIn("new task", hints)
        inspector = "".join(text for _style, text in controller.inspector_fragments())
        self.assertIn("/agent scout", inspector)
        self.assertIn("/resume", inspector)
        scout.status = "running"
        self.assertEqual(controller._input_action(scout), "send update")

    def test_addressing_primary_cannot_inject_into_foreground_scout(self):
        primary = self._agent("primary", role="primary")
        scout = self._agent("scout")
        agent_loop.set_current_agent_id(scout.id)
        submit = mock.Mock()
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, repl_submit_cb=submit)
        controller.select(scout.id)
        controller.dispatch("@primary continue your task")
        submit.assert_not_called()
        self.assertIn("/agent primary", controller.notice)
        self.assertEqual(agent_loop.get_current_agent_id(), scout.id)

    def test_unsent_drafts_are_isolated_per_selected_agent(self):
        first = self._agent("first")
        second = self._agent("second")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = first.id
        controller._input_buffer = Buffer(multiline=False)
        controller._input_buffer.text = "message for first"

        self.assertTrue(controller.select(second.id))
        self.assertEqual(controller._input_buffer.text, "")
        controller._input_buffer.text = "message for second"
        self.assertTrue(controller.select(first.id))
        self.assertEqual(controller._input_buffer.text, "message for first")

    def test_stream_chunks_render_as_one_assistant_message(self):
        agent = self._agent("writer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        agent_ui_events.hub.ingest(agent.id, [
            {"type": "ai_stream", "content": "hello "},
            {"type": "ai_stream", "content": "world\nnext"},
            {"type": "ai_end"},
        ], "term0")

        rows = controller._transcript_text(agent.id)
        replies = [row for row in rows if row.startswith("● ")]

        # One assistant message, not one per chunk: the chunks join.
        self.assertEqual(replies, ["● hello world"])
        self.assertIn("  next", rows)

    def test_accepted_input_is_not_rendered_twice_as_agent_started(self):
        agent = self._agent("writer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        agent_ui_events.hub.emit(
            "user_message", agent_id=agent.id, terminal_name="term0",
            summary="hello", detail="hello")
        agent_ui_events.hub.emit(
            "agent_started", agent_id=agent.id, terminal_name="term0",
            summary="hello", status="running")
        agent_ui_events.hub.emit(
            "ai_end", agent_id=agent.id, terminal_name="term0",
            summary="ai_end")

        focus_text = "\n".join(controller._transcript_text(agent.id))

        self.assertEqual(focus_text.count("hello"), 1)
        self.assertNotIn("ai_end", focus_text)

    def test_unauthenticated_input_is_rejected_immediately_and_visibly(self):
        agent = self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {},
            execution_block_reason=(
                "Not authenticated. Exit Agents Mode and run /login."))
        controller.selected_id = agent.id
        with mock.patch.object(agent_loop, "run_agent_loop") as run:
            controller.dispatch("hello")

        run.assert_not_called()
        self.assertEqual(agent.status, "idle")
        events = agent_ui_events.hub.agent_events(agent.id)
        self.assertEqual(
            [event.event_type for event in events],
            ["user_message", "input_rejected"])
        self.assertIn("/login", controller.notice)

    def test_running_tool_is_replaced_in_place_when_it_finishes(self):
        agent = self._agent("worker")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        agent_ui_events.hub.ingest(agent.id, [{
            "type": "tool_started", "toolCallId": "call-1",
            "name": "shell.exec", "command": "pytest -q",
        }], "term0")
        agent.status = "running"
        running = controller._transcript_rows(agent.id, 80)
        heads = [row for row in running if row.text.startswith("● shell.exec")]
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0].anim, "tool")
        agent_ui_events.hub.ingest(agent.id, [{
            "type": "system", "kind": "tool", "content": "shell.exec",
            "meta": {"call_id": "call-1", "salient": "pytest -q", "ok": True},
        }], "term0")
        finished = controller._transcript_rows(agent.id, 80)
        heads = [row for row in finished if row.text.startswith("● shell.exec")]
        # Replaced in place: still one row, no longer animated as running.
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0].anim, "")
        self.assertIn("class:tool.ok", heads[0].fragments[0][0])

    def test_events_are_isolated_but_cross_terminal_message_is_visible_to_both(self):
        self._terminal("child", "term0")
        sender = self._agent("sender", "term0")
        target = self._agent("target", "child")
        agent_ui_events.hub.emit(
            "tool", agent_id=sender.id, terminal_name="term0", summary="root tool")
        self.assertTrue(agent_loop.send_to_agent(target.id, {
            "from": sender.id, "kind": "msg", "text": "handoff"}))

        root_events = agent_ui_events.hub.events("term0")
        child_events = agent_ui_events.hub.events("child")

        self.assertTrue(any(row.summary == "root tool" for row in root_events))
        self.assertFalse(any(row.summary == "root tool" for row in child_events))
        self.assertTrue(any(row.summary == "handoff" for row in root_events))
        self.assertTrue(any(row.summary == "handoff" for row in child_events))

    def test_failed_delivery_is_observable_and_does_not_emit_success(self):
        sender = self._agent("sender")
        target = self._agent("target")
        target.inbox = queue.Queue(maxsize=1)
        target.inbox.put_nowait({"occupied": True})

        self.assertFalse(agent_loop.send_to_agent(target.id, {
            "from": sender.id, "text": "blocked"}))

        kinds = [event.event_type
                 for event in agent_ui_events.hub.agent_events(target.id)]
        self.assertEqual(kinds, ["agent_message_failed"])

    def test_layout_builds_as_one_full_screen_application(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        with mock.patch.object(agents_mode.Application, "run", return_value=None):
            controller.run()
        self.assertIsNone(controller.app)

    def test_deployed_agents_excluded_from_rail_and_focus(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        self.assertEqual(controller.agents(), [])
        self.assertEqual(controller.selected_id, "")
        rail_text = "".join(text for _style, text, *_ in controller.rail_fragments())
        self.assertIn("No Agents", rail_text)
        focus_text = "".join(text for _style, text in controller.focus_fragments())
        self.assertIn("No Agents", focus_text)

    def test_pool_agents_still_visible_alongside_deployed_primary(self):
        primary = self._agent("primary", role="primary")
        worker = self._agent("worker")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        agents = controller.agents()
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].id, worker.id)
        self.assertEqual(controller.selected_id, worker.id)
        self.assertNotIn(primary.id, {a.id for a in agents})

    def test_real_application_event_loop_starts_and_quits_cleanly(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        with create_pipe_input() as pipe_input:
            pipe_input.send_bytes(b"\x11")  # Ctrl-Q
            controller.run(input=pipe_input, output=DummyOutput())
        self.assertIsNone(controller.app)

    def test_real_application_enter_dispatches_once_and_escape_exits(self):
        agent = self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {})
        controller.selected_id = agent.id
        started = threading.Event()
        calls = []

        def submit(target, text, _deps):
            calls.append(text)
            agent_ui_events.hub.emit(
                "user_message", agent_id=target.id,
                terminal_name="term0", summary=text, detail=text)
            agent_ui_events.hub.emit(
                "agent_done", agent_id=target.id,
                terminal_name="term0", summary="answer", detail="answer")
            started.set()
            return True, "Submitted"

        controller.primary_submit_cb = submit

        with create_pipe_input() as pipe_input:
            def keys():
                pipe_input.send_bytes(b"hello\r")
                started.wait(timeout=1)
                pipe_input.send_bytes(b"\x1b")

            sender = threading.Thread(target=keys)
            sender.start()
            controller.run(input=pipe_input, output=DummyOutput())
            sender.join(timeout=1)

        self.assertEqual(calls, ["hello"])
        events = agent_ui_events.hub.agent_events(agent.id)
        self.assertEqual(
            sum(event.event_type == "user_message" for event in events), 1)
        self.assertEqual(
            sum(event.event_type == "agent_done" for event in events), 1)

    def test_primary_runtime_survives_agents_view_exit(self):
        agent = self._agent("primary", role="primary")
        entered = threading.Event()
        release = threading.Event()

        def blocking_loop(_deps, _text, _session, state, _history, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            return {
                "success": True,
                "state": {**state, "lastReply": "mapped reply"},
                "msg": "mapped reply",
            }

        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, primary_submit_cb=lambda a, t, d:
            laintas_cli._submit_primary_runtime_task(a, t, d, {}))
        controller.selected_id = agent.id
        with create_pipe_input() as pipe_input, \
                mock.patch.object(laintas_cli, "run_agent_loop", blocking_loop):
            def keys():
                pipe_input.send_bytes(b"long task\r")
                entered.wait(timeout=1)
                pipe_input.send_bytes(b"\x1b")

            sender = threading.Thread(target=keys)
            sender.start()
            controller.run(input=pipe_input, output=DummyOutput())
            sender.join(timeout=1)

            self.assertEqual(agent.status, "thinking")
            self.assertTrue(agent.thread.is_alive())
            self.assertTrue(agent.thread.name.startswith("primary-runtime-"))
            release.set()
            agent.thread.join(timeout=1)

        self.assertEqual(agent.status, "idle")
        self.assertEqual(agent.last_reply, "mapped reply")
        self.assertEqual(
            agent_ui_events.hub.agent_events(agent.id)[-1].event_type,
            "agent_done")

    def test_outer_view_enters_thinking_and_attaches_without_second_run(self):
        agent = self._agent("primary", role="primary")
        entered = threading.Event()
        release = threading.Event()
        calls = []
        output = io.StringIO()

        def blocking_loop(*_args, **_kwargs):
            calls.append("run")
            entered.set()
            release.wait(timeout=2)
            return {
                "success": True,
                "state": {"lastReply": "outer mapped reply"},
                "msg": "outer mapped reply",
            }

        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(
                    laintas_cli, "run_agent_loop", blocking_loop):
                ok, _detail = laintas_cli._submit_primary_runtime_task(
                    agent, "work", mock.Mock(), {})
                self.assertTrue(ok)
                self.assertTrue(entered.wait(timeout=1))
                threading.Timer(0.1, release.set).start()
                returned = laintas_cli._attach_primary_runtime_view(agent)
        finally:
            laintas_cli.console = old_console
            release.set()
            if agent.thread:
                agent.thread.join(timeout=1)

        self.assertIs(returned, agent.runtime_session)
        self.assertEqual(calls, ["run"])
        self.assertEqual(agent.status, "idle")
        self.assertIn("outer mapped reply", output.getvalue())

    def test_ctrl_c_clears_the_prompt_but_never_leaves_the_view(self):
        self._agent("worker")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        seen = {}
        with create_pipe_input() as pipe_input:
            def keys():
                time.sleep(0.3)
                pipe_input.send_text("draft")
                time.sleep(0.1)
                pipe_input.send_text("\x03")            # clears the draft
                time.sleep(0.1)
                seen["after_clear"] = controller._input_buffer.text
                pipe_input.send_text("\x03")            # empty: only a hint
                time.sleep(0.1)
                seen["running"] = controller.app is not None
                seen["notice"] = controller.notice
                pipe_input.send_text("\x1b")
            sender = threading.Thread(target=keys)
            sender.start()
            controller.run(input=pipe_input, output=DummyOutput())
            sender.join(timeout=2)
        self.assertEqual(seen["after_clear"], "")
        self.assertTrue(seen["running"])
        self.assertIn("Esc", seen["notice"])

    def test_escape_exits_even_when_input_buffer_has_unsubmitted_text(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {})
        with create_pipe_input() as pipe_input:
            pipe_input.send_bytes(b"unfinished text\x1b")
            controller.run(input=pipe_input, output=DummyOutput())

        self.assertIsNone(controller.app)
        self.assertFalse(any(
            event.event_type == "user_message"
            for event in agent_ui_events.hub.events("term0")))

    def test_exit_command_closes_mode_without_dispatching_to_agent(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {})
        with create_pipe_input() as pipe_input:
            pipe_input.send_bytes(b"/exit\r")
            controller.run(input=pipe_input, output=DummyOutput())

        self.assertFalse(any(
            event.event_type == "user_message"
            for event in agent_ui_events.hub.events("term0")))

    def test_worker_deps_use_silent_renderers_in_full_screen_mode(self):
        agent = self._agent("worker")
        deps = mock.Mock()
        controller = agents_mode.AgentsModeController("term0", deps, {})

        wired = controller._deps_for(agent.id)

        self.assertIsNot(wired.console, deps.console)
        self.assertIs(wired.console, controller._console_for(agent.id))
        # These calls must be harmless and write nothing to the terminal.
        wired.console.print("hidden worker output")
        wired.display_command_output("cmd", 0, "output")

    def test_each_agent_gets_its_own_console(self):
        # rich allows one live display per Console: two agents streaming at
        # once on a shared console kill the second one with LiveError.
        first, second = self._agent("one"), self._agent("two")
        controller = agents_mode.AgentsModeController("term0", mock.Mock(), {})

        self.assertIsNot(controller._deps_for(first.id).console,
                         controller._deps_for(second.id).console)
        self.assertIs(controller._deps_for(first.id).console,
                      controller._deps_for(first.id).console)

    def test_worker_approval_is_resolved_by_ui_and_attributed_to_agent(self):
        agent = self._agent("reviewer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        result = []
        thread = threading.Thread(target=lambda: result.append(
            controller._request_approval(
                agent.id, "write", "auth.py", "token refresh change")))
        thread.start()
        deadline = time.time() + 1
        while controller.pending_approval() is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(controller.pending_approval())

        controller.resolve_approval(True)
        thread.join(timeout=1)

        self.assertEqual(result, [True])
        events = agent_ui_events.hub.agent_events(agent.id)
        self.assertEqual(events[-2].event_type, "approval_requested")
        self.assertEqual(events[-1].event_type, "approval_resolved")
        self.assertEqual(events[-1].status, "approved")

    def test_approval_keeps_original_terminal_after_ui_switch(self):
        self._terminal("child", "term0")
        agent = self._agent("reviewer", terminal="term0")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        result = []
        thread = threading.Thread(target=lambda: result.append(
            controller._request_approval(
                agent.id, "delete", "old.log", "remove generated log")))
        thread.start()
        deadline = time.time() + 1
        while controller.pending_approval() is None and time.time() < deadline:
            time.sleep(0.01)

        controller.terminal_name = "child"
        rendered = "".join(
            fragment[1] for fragment in controller.approval_fragments())
        self.assertIn("on term0", rendered)
        self.assertIn("old.log", rendered)
        controller.resolve_approval(False)
        thread.join(timeout=1)

        events = agent_ui_events.hub.agent_events(agent.id)[-2:]
        self.assertEqual([event.terminal_name for event in events],
                         ["term0", "term0"])
        self.assertEqual(result, [False])

    def test_closed_ui_denies_late_approval_without_blocking(self):
        agent = self._agent("late")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.deny_pending_approvals(
            close=True, reason="agents_mode_closed")

        started = time.monotonic()
        approved = controller._request_approval(
            agent.id, "write", "late.txt", "after close")

        self.assertFalse(approved)
        self.assertLess(time.monotonic() - started, 0.2)
        events = agent_ui_events.hub.agent_events(agent.id)
        self.assertEqual(
            [event.event_type for event in events[-2:]],
            ["approval_requested", "approval_resolved"])
        self.assertEqual(events[-1].data.get("reason"), "agents_mode_closed")

    def test_abort_releases_agent_waiting_for_approval(self):
        agent = self._agent("abortable")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        result = []
        thread = threading.Thread(target=lambda: result.append(
            controller._request_approval(
                agent.id, "command", "danger", "test abort")))
        thread.start()
        deadline = time.time() + 1
        while controller.pending_approval() is None and time.time() < deadline:
            time.sleep(0.01)

        agent.abort_event.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertIsNone(controller.pending_approval())
        self.assertEqual(
            agent_ui_events.hub.agent_events(agent.id)[-1].data.get("reason"),
            "agent_aborted")

    def test_later_approval_does_not_replace_visible_fifo_request(self):
        first = self._agent("first")
        second = self._agent("second")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        results = []
        first_thread = threading.Thread(target=lambda: results.append((
            "first", controller._request_approval(
                first.id, "delete", "first-danger.txt", "delete"))))
        first_thread.start()
        deadline = time.time() + 1
        while controller.pending_approval() is None and time.time() < deadline:
            time.sleep(0.01)
        second_thread = threading.Thread(target=lambda: results.append((
            "second", controller._request_approval(
                second.id, "command", "second-safe", "command"))))
        second_thread.start()
        deadline = time.time() + 1
        while len(controller._approvals) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertIn("first-danger.txt", controller.notice)
        self.assertNotIn("second-safe", controller.notice)
        controller.resolve_approval(True)
        first_thread.join(timeout=1)
        self.assertIn("second-safe", controller.notice)
        controller.resolve_approval(False)
        second_thread.join(timeout=1)
        self.assertCountEqual(results, [("first", True), ("second", False)])

    def test_slash_commands_are_not_duplicated_or_sent_to_agents(self):
        agent = self._agent("queued")
        agent.status = "queued"
        controller = agents_mode.AgentsModeController("term0", object(), {})

        controller.dispatch("/hire reviewer --profile reviewer")

        self.assertTrue(agent.message_queue.empty())
        self.assertTrue(agent.inbox.empty())
        self.assertIsNone(agent_loop.get_agent("reviewer"))
        self.assertIn("main CLI", controller.notice)

    def test_failed_supplementary_delivery_is_not_recorded_as_success(self):
        agent = self._agent("busy")
        agent.status = "running"
        agent.message_queue = queue.Queue(maxsize=1)
        agent.message_queue.put_nowait("occupied")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id

        controller.dispatch("new instruction")

        kinds = [event.event_type
                 for event in agent_ui_events.hub.agent_events(agent.id)]
        self.assertNotIn("user_message", kinds)
        self.assertIn("user_message_failed", kinds)

    def test_finished_subagent_cannot_be_reused_as_employee(self):
        agent = self._agent("temporary", role="subagent")
        agent.status = "done"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        with mock.patch.object(agent_loop, "start_agent_assignment") as start:
            controller.dispatch("do another unrelated task")

        start.assert_not_called()
        self.assertIn("finished temporary Agent", controller.notice)

    def test_markdown_styles_and_completion_has_no_placeholder(self):
        agent = self._agent("writer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        agent_ui_events.hub.emit(
            "ai", agent_id=agent.id, terminal_name="term0",
            detail="# Result\n\n**bold** and `code`\n\n- item")
        agent_ui_events.hub.emit(
            "agent_done", agent_id=agent.id, terminal_name="term0",
            summary="done", status="completed")

        rows = controller._transcript_rows(agent.id, 80)
        fragments = [fragment for row in rows for fragment in row.fragments]

        self.assertIn(("class:md.h1", "Result"), fragments)
        self.assertTrue(any("class:md.bold" in style and text == "bold"
                            for style, text in fragments))
        self.assertTrue(any(style == "class:md.code" and text == "code"
                            for style, text in fragments))
        self.assertNotIn("Task completed",
                         "".join(row.text for row in rows))

    def test_a_working_agent_gets_the_cli_status_row_not_a_placeholder(self):
        """The same row the plain CLI paints during a turn — branded relay
        spinner, the verb, and an elapsed clock."""
        agent = self._agent("writer")
        agent.status = "thinking"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id

        with mock.patch.object(agents_mode.time, "monotonic", return_value=0):
            first = controller._activity_line(agent.id)[1]
        self.assertEqual("L· Thinking… 0.0s", first)

        # The spinner advances on the CLI's own frame interval, and the clock
        # is real: both come from the elapsed time, not a redraw counter.
        with mock.patch.object(agents_mode.time, "monotonic", return_value=1.5):
            later = controller._activity_line(agent.id)[1]
        self.assertEqual("L» Thinking… 1.5s", later)

        agent_ui_events.hub.emit(
            "ai_stream", agent_id=agent.id, terminal_name="term0",
            detail="partial")
        with mock.patch.object(agents_mode.time, "monotonic", return_value=2.0):
            self.assertEqual("L» Writing… 2.0s",
                             controller._activity_line(agent.id)[1])

    def test_the_status_verb_carries_the_moving_highlight(self):
        """The shimmer is the CLI's, glyph for glyph — a second implementation
        of it is a second thing to keep in step."""
        agent = self._agent("writer")
        agent.status = "thinking"
        controller = agents_mode.AgentsModeController("term0", object(), {})

        with mock.patch.object(agents_mode.time, "monotonic", return_value=0.3):
            fragments = controller._status_fragments(agent.id, width=40)
        verb = "".join(text for _style, text in fragments)
        self.assertIn("Thinking…", verb)
        # Split into per-character styled runs: a plain label would be one.
        styles = {style for style, _text in fragments}
        self.assertGreater(len(styles), 2, fragments)

    def test_an_idle_agent_has_no_status_row(self):
        agent = self._agent("writer")
        agent.status = "idle"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        self.assertEqual([], controller._status_fragments(agent.id, width=80))
        self.assertIsNone(controller._activity_line(agent.id))

    def test_the_elapsed_clock_restarts_with_the_next_stretch_of_work(self):
        """A finished-then-restarted Agent counts from the restart, not from
        the age of some earlier task."""
        agent = self._agent("writer")
        controller = agents_mode.AgentsModeController("term0", object(), {})

        agent.status = "thinking"
        with mock.patch.object(agents_mode.time, "monotonic", return_value=10):
            controller._working_elapsed(agent)
        with mock.patch.object(agents_mode.time, "monotonic", return_value=13):
            self.assertEqual(3, round(controller._working_elapsed(agent)))
        agent.status = "idle"
        with mock.patch.object(agents_mode.time, "monotonic", return_value=20):
            self.assertEqual(0, controller._working_elapsed(agent))
        agent.status = "running"
        with mock.patch.object(agents_mode.time, "monotonic", return_value=30):
            self.assertEqual(0, round(controller._working_elapsed(agent)))
        with mock.patch.object(agents_mode.time, "monotonic", return_value=31):
            self.assertEqual(1, round(controller._working_elapsed(agent)))

    def test_primary_raw_view_follows_wrapped_screen_rows(self):
        """Ctrl+R shows the REPL's own output; follow means the true bottom
        after wrapping, and scrolling is by physical rows."""
        agent = self._agent("primary", role="primary")
        lines = [
            f"TURN-{turn} " + (str(turn) * 90) + f" END-{turn}"
            for turn in range(1, 6)
        ]

        class Mirror:
            @staticmethod
            def read_lines(_agent_id):
                return lines

        controller = agents_mode.AgentsModeController(
            "term0", object(), {}, mirror=Mirror())
        controller.selected_id = agent.id
        controller._raw[agent.id] = True

        def screen():
            return "".join(
                text for line in controller.transcript_lines(40, 8)
                for _style, text, *_ in line)

        bottom = screen()
        controller.scroll(6, smooth=False)
        scrolled = screen()
        controller.scroll(-6, smooth=False)
        followed = screen()

        self.assertIn("END-5", bottom)
        self.assertNotIn("TURN-1", bottom)
        self.assertTrue(controller.follow[agent.id])
        self.assertNotIn("END-5", scrolled)
        self.assertIn("END-5", followed)

    def test_scrolled_view_holds_still_while_new_rows_arrive(self):
        agent = self._agent("writer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        controller._profile_pinned = True     # no card: rows are all events
        for index in range(30):
            agent_ui_events.hub.emit(
                "ai", agent_id=agent.id, terminal_name="term0",
                detail=f"reply {index}")

        def screen():
            return "".join(
                text for line in controller.transcript_lines(60, 10)
                for _style, text, *_ in line)

        screen()
        controller.scroll(20, smooth=False)
        before = screen()
        agent_ui_events.hub.emit(
            "ai", agent_id=agent.id, terminal_name="term0", detail="late")
        after = screen()

        self.assertIn("reply 17", before)
        self.assertIn("reply 17", after)
        self.assertNotIn("late", after)
        self.assertIn("new line", after)      # the jump-to-latest pill

    def test_status_row_follows_the_newest_output_like_the_cli(self):
        agent = self._agent("writer")
        agent.status = "thinking"
        agent_ui_events.hub.emit(
            "ai", agent_id=agent.id, terminal_name="term0",
            detail="latest " + ("界" * 80) + " ENDLATEST")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        with mock.patch.object(agents_mode.time, "monotonic", return_value=0):
            lines = ["".join(text for _style, text, *_ in line).strip(" │┃")
                     for line in controller.transcript_lines(40, 12)]
        filled = [line for line in lines if line.strip()]
        # The newest wrapped output is on screen, measured in cells, and the
        # status row sits directly under it, as in the plain CLI.
        self.assertIn("ENDLATEST", filled[-2])
        self.assertTrue(filled[-1].startswith("L· Thinking…"), filled[-1])

        agent.status = "idle"
        idle = "".join(text for line in controller.transcript_lines(40, 12)
                       for _style, text, *_ in line)
        self.assertNotIn("Thinking…", idle)

    def test_every_pane_row_is_exactly_its_width(self):
        """Rows are padded to the pane so each cell maps to a position:
        a click right of the text, or on the scrollbar, lands where aimed."""
        agent = self._agent("writer")
        for index in range(40):
            agent_ui_events.hub.emit(
                "ai", agent_id=agent.id, terminal_name="term0",
                detail=f"**reply** {index} " + "界" * (index % 7))
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        for render, width in ((controller.transcript_lines, 57),
                              (controller.rail_lines, 26),
                              (controller.title_lines, 57),
                              (controller.footer_lines, 57)):
            for line in render(width, 12):
                cells = sum(agents_mode.get_cwidth(ch)
                            for _style, text, *_ in line for ch in text)
                self.assertEqual(cells, width, (render.__name__, line))

    def test_clicking_a_long_tool_output_expands_it_in_place(self):
        agent = self._agent("worker")
        agent_ui_events.hub.ingest(agent.id, [
            {"type": "tool_started", "toolCallId": "c1", "name": "read",
             "command": "big.txt"},
            {"type": "system", "kind": "tool", "content": "read",
             "meta": {"call_id": "c1", "ok": True, "salient": "big.txt"}},
            {"type": "system", "kind": "output",
             "content": "\n".join(f"line {i}" for i in range(50))},
        ], "term0")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        controller._profile_pinned = True
        collapsed = controller._transcript_text(agent.id)
        self.assertNotIn("line 1", "\n".join(collapsed))
        self.assertTrue(any("50L" in row for row in collapsed))

        controller.transcript_lines(80, 20)
        toggle_row = next(index for index, row in enumerate(
            controller._transcript_rows(agent.id, 77)) if "50L" in row.text)
        start = controller._viewport[1]
        from prompt_toolkit.mouse_events import MouseEvent, MouseButton
        from prompt_toolkit.data_structures import Point
        for kind in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
            controller._transcript_mouse(MouseEvent(
                Point(x=8, y=toggle_row - start), kind,
                MouseButton.LEFT, frozenset()))
        expanded = "\n".join(controller._transcript_text(agent.id))
        self.assertIn("line 49", expanded)

    def test_drag_selection_copies_the_text_shown(self):
        agent = self._agent("writer")
        agent_ui_events.hub.emit(
            "ai", agent_id=agent.id, terminal_name="term0",
            detail="alpha beta\ngamma delta")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        controller._profile_pinned = True
        controller.transcript_lines(60, 6)
        start = controller._viewport[1]
        rows = controller._transcript_text(agent.id, 57)
        first = rows.index("● alpha beta")
        from prompt_toolkit.mouse_events import MouseEvent, MouseButton
        from prompt_toolkit.data_structures import Point
        copied = []
        controller._copy_to_clipboard = lambda text: copied.append(text) or True

        def event(kind, x, y):
            controller._transcript_mouse(MouseEvent(
                Point(x=x, y=y), kind, MouseButton.LEFT, frozenset()))
        event(MouseEventType.MOUSE_DOWN, 3, first - start)
        event(MouseEventType.MOUSE_MOVE, 7, first - start + 1)
        event(MouseEventType.MOUSE_UP, 7, first - start + 1)

        self.assertEqual(copied, ["alpha beta\n  gamma"])
        self.assertIn("Copied", controller.notice)

    # ── title-bar buttons ───────────────────────────────────────────────
    def _title_click(self, controller, name, press_on=None):
        from prompt_toolkit.mouse_events import MouseEvent, MouseButton
        from prompt_toolkit.data_structures import Point
        # No app is running, so delivery would go straight to the REPL queue.
        controller._deliver_pending_command = lambda: None
        controller.title_lines(100, 1)
        spans = {hit[2]: hit for hit in controller._title_hits}
        x = spans[name][0] + 1
        press_x = spans[press_on][0] + 1 if press_on else x
        controller._title_mouse(MouseEvent(
            Point(x=press_x, y=0), MouseEventType.MOUSE_DOWN,
            MouseButton.LEFT, frozenset()))
        controller._title_mouse(MouseEvent(
            Point(x=x, y=0), MouseEventType.MOUSE_UP,
            MouseButton.LEFT, frozenset()))

    def test_title_buttons_have_one_shape_and_statuses_have_no_fill(self):
        agent = self._agent("reviewer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        line = controller.title_lines(100, 1)[0]
        filled = [(style, text) for style, text, *_ in line if "chip" in style]
        self.assertEqual([text.strip() for _s, text in filled][:1], ["resume"])
        self.assertEqual({len(text) - len(text.strip()) for _s, text in filled}, {2})
        # ●/○ come from a fallback font; behind a fill they change its height.
        for style, text in filled:
            self.assertTrue(text.isascii(), text)
        self.assertIn("not deployed", "".join(t for _s, t, *_ in line))

    def test_title_hits_line_up_with_the_painted_buttons(self):
        agent = self._agent("reviewer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        line = controller.title_lines(100, 1)[0]
        painted = "".join(t for _s, t, *_ in line)
        for lo, hi, name in controller._title_hits:
            label = painted[lo:hi].strip()
            self.assertEqual(label, "resume" if name == "resume"
                             else controller._agent_profile(agent)["model"]
                             or "default model")

    def test_model_button_targets_the_agent_without_switching(self):
        agent = self._agent("reviewer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        self._title_click(controller, "model")
        self.assertEqual(controller.pending_commands,
                         [f"/model @{agent.id}", f"/agents {agent.id}"])

    def test_resume_button_switches_by_id_then_reopens_the_view(self):
        primary = self._agent("primary", role="primary")
        self.assertTrue(agent_loop.switch_to_agent(primary.id))
        agent = self._agent("reviewer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        self._title_click(controller, "resume")
        self.assertEqual(controller.pending_commands,
                         [f"/agent {agent.id}", "/resume", f"/agents {agent.id}"])

    def test_resume_button_refuses_a_working_agent(self):
        """/agent would refuse the switch and /resume would then restore
        into the REPL's current Agent instead."""
        agent = self._agent("reviewer")
        agent.status = "running"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        self._title_click(controller, "resume")
        self.assertEqual(controller.pending_commands, [])
        self.assertIn("Cannot resume", controller.notice)

    def test_selection_released_on_a_button_does_not_fire_it(self):
        agent = self._agent("reviewer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        self._title_click(controller, "model", press_on="resume")
        self.assertEqual(controller.pending_commands, [])

    def test_subagent_has_no_title_buttons(self):
        agent = self._agent("helper", role="subagent")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        controller.title_lines(100, 1)
        self.assertEqual(controller._title_hits, [])

    def test_rail_click_targets_stay_aligned_when_scrolled(self):
        agents = [self._agent(f"a{index}") for index in range(8)]
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller._terminal_size = lambda: (120, 24)
        controller.agents()
        controller.rail_offset = 2
        lines = controller.rail_lines(30, 24)
        self.assertEqual(len(lines), 24)
        for row, hit in controller._rail_hits.items():
            text = "".join(t for _s, t, *_ in lines[row])
            if hit[0] == "scroll":
                self.assertIn("more", text)
        texts = ["".join(t for _s, t, *_ in line) for line in lines]
        firsts = {}
        for row in sorted(controller._rail_hits):
            hit = controller._rail_hits[row]
            if hit[0] == "agent":
                firsts.setdefault(hit[1], row)
        self.assertEqual(len(firsts), 4)
        for agent_id, row in firsts.items():
            # The first row a card's hit covers is the row with its name.
            self.assertEqual(texts[row].split()[-1],
                             agent_loop.get_agent(agent_id).name)
        scroll_rows = [row for row, hit in controller._rail_hits.items()
                       if hit[0] == "scroll"]
        self.assertEqual(len(scroll_rows), 2)
        self.assertTrue(all("more" in texts[row] for row in scroll_rows))

    def test_model_at_agent_sets_an_undeployed_agents_own_model(self):
        agent = self._agent("reviewer")
        agent.base_model = "glm-5.3"
        term0 = agent_loop.get_terminal("term0")
        before = term0.model_override
        with mock.patch.object(laintas_cli.agent_persistence,
                               "save_agent_state") as save:
            laintas_cli._cmd_model(
                ["/model", f"@{agent.id}", "kimi-k2.7-code"],
                f"@{agent.id} kimi-k2.7-code", {})
        self.assertEqual(agent.base_model, "kimi-k2.7-code")
        self.assertEqual(term0.model_override, before)
        save.assert_not_called()   # not a persisted employee
        laintas_cli._cmd_model(["/model", f"@{agent.id}", "reset"],
                               f"@{agent.id} reset", {})
        self.assertEqual(agent.base_model, "")

    def test_model_at_agent_uses_the_deployed_agents_terminal(self):
        self._terminal("build")
        agent = self._agent("builder", terminal="build")
        self.assertTrue(agent_loop.station_agent(agent.id, "build"))
        with mock.patch.object(laintas_cli, "set_model_selection"):
            laintas_cli._cmd_model(["/model", f"@{agent.id}", "glm-5.3"],
                                   f"@{agent.id} glm-5.3", {})
        self.assertEqual(agent_loop.get_terminal("build").model_override,
                         "glm-5.3")
        self.assertEqual(agent.base_model, "")

    def test_switching_terminal_skips_the_agent_deployed_there(self):
        self._terminal("build")
        deployed = self._agent("builder", terminal="build")
        self.assertTrue(agent_loop.station_agent(deployed.id, "build"))
        other = self._agent("tester", terminal="build")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        while controller.terminal_name != "build":
            controller.cycle_terminal(1)
        self.assertEqual(controller.selected_id, other.id)

    def test_assignment_uses_employee_channels_and_reports_failed_loop(self):
        agent = self._agent("employee")
        captured = {}

        def fake_loop(*_args, **kwargs):
            captured.update(kwargs)
            return {
                "success": False, "exit_reason": "max_loops",
                "state": {"lastReply": "partial"}, "msg": "partial",
            }

        with mock.patch.object(agent_loop, "run_agent_loop", fake_loop), \
                mock.patch.object(agent_loop.agent_persistence,
                                  "save_agent_state", return_value=True):
            ok, _detail, assignment = agent_loop.start_agent_assignment(
                agent.id, "work", object(), {})
            self.assertTrue(ok)
            agent.thread.join(timeout=1)

        self.assertIs(captured["interrupt_event"], agent.abort_event)
        self.assertIs(captured["message_queue"], agent.message_queue)
        self.assertEqual(assignment.status, "error")
        self.assertEqual(agent.status, "error")
        self.assertEqual(
            agent_ui_events.hub.agent_events(agent.id)[-1].event_type,
            "agent_error")

    def test_assignment_admission_is_atomic_under_concurrent_callers(self):
        agent = self._agent("atomic")
        release = threading.Event()
        callers = threading.Barrier(3)
        results = []

        def fake_loop(*_args, **_kwargs):
            release.wait(timeout=2)
            return {"success": True, "state": {}, "msg": "done"}

        def launch(task):
            callers.wait()
            results.append(agent_loop.start_agent_assignment(
                agent.id, task, object(), {}))

        with mock.patch.object(agent_loop, "run_agent_loop", fake_loop), \
                mock.patch.object(agent_loop.agent_persistence,
                                  "save_agent_state", return_value=True):
            first = threading.Thread(target=launch, args=("first",))
            second = threading.Thread(target=launch, args=("second",))
            first.start()
            second.start()
            callers.wait()
            first.join(timeout=1)
            second.join(timeout=1)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(sum(bool(row[0]) for row in results), 1)
            winner = next(row for row in results if row[0])
            self.assertIs(agent.active_assignment, winner[2])
            release.set()
            agent.thread.join(timeout=1)

        self.assertIsNone(agent.active_assignment)

    def test_close_request_race_never_leaves_approval_waiter(self):
        for index in range(20):
            agent = self._agent(f"race-{index}")
            controller = agents_mode.AgentsModeController(
                "term0", object(), {})
            barrier = threading.Barrier(3)
            result = []

            def request():
                barrier.wait()
                result.append(controller._request_approval(
                    agent.id, "write", "race.txt", "race"))

            def close():
                barrier.wait()
                controller.deny_pending_approvals(
                    close=True, reason="agents_mode_closed")

            requester = threading.Thread(target=request)
            closer = threading.Thread(target=close)
            requester.start()
            closer.start()
            barrier.wait()
            requester.join(timeout=1)
            closer.join(timeout=1)

            self.assertFalse(requester.is_alive())
            self.assertFalse(closer.is_alive())
            self.assertEqual(result, [False])

    def test_primary_failed_loop_is_not_reported_done(self):
        agent = self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, primary_submit_cb=lambda a, t, d:
            laintas_cli._submit_primary_runtime_task(a, t, d, {}))
        controller.selected_id = agent.id
        result = {
            "success": False, "exit_reason": "provider_error",
            "state": {"lastReply": "partial"}, "msg": "partial",
        }
        with mock.patch.object(laintas_cli, "run_agent_loop", return_value=result):
            controller.dispatch("work")
            agent.thread.join(timeout=1)

        self.assertEqual(agent.status, "error")
        self.assertEqual(
            agent_ui_events.hub.agent_events(agent.id)[-1].event_type,
            "agent_error")

    def test_external_event_callback_failure_does_not_fail_primary(self):
        agent = self._agent("primary", role="primary")
        callback = mock.Mock(side_effect=RuntimeError("offline"))
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, external_events_cb=callback,
            primary_submit_cb=lambda a, t, d:
            laintas_cli._submit_primary_runtime_task(a, t, d, {}, callback))
        controller.selected_id = agent.id

        def fake_loop(*_args, **kwargs):
            kwargs["events_cb"]([{"type": "ai", "content": "answer"}])
            return {"success": True, "state": {}, "msg": "done"}

        with mock.patch.object(laintas_cli, "run_agent_loop", fake_loop):
            controller.dispatch("work")
            agent.thread.join(timeout=1)

        self.assertEqual(agent.status, "idle")
        self.assertEqual(
            agent_ui_events.hub.agent_events(agent.id)[-1].event_type,
            "agent_done")

    def test_primary_preserves_repl_state_identity_and_existing_session(self):
        agent = self._agent("primary", role="primary")
        state_ref = {"shortTermMemory": "before"}
        history_ref = []
        agent.state = state_ref
        agent.chat_history = history_ref
        existing = object()
        agent.runtime_session = existing
        captured = {}
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, existing_session=existing,
            primary_submit_cb=lambda a, t, d:
            laintas_cli._submit_primary_runtime_task(a, t, d, {}))
        controller.selected_id = agent.id

        def fake_loop(*_args, **kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "state": {"shortTermMemory": "after", "lastReply": "done"},
                "msg": "done", "session": existing,
            }

        with mock.patch.object(laintas_cli, "run_agent_loop", fake_loop):
            controller.dispatch("work")
            agent.thread.join(timeout=1)

        self.assertIs(captured["existing_session"], existing)
        self.assertIs(agent.state, state_ref)
        self.assertEqual(state_ref["shortTermMemory"], "after")
        self.assertIs(agent.chat_history, history_ref)

    def test_primary_has_one_shared_execution_lease_and_message_queue(self):
        agent = self._agent("primary", role="primary")
        state_ref = {"shortTermMemory": "shared"}
        history_ref = []
        agent.state = state_ref
        agent.chat_history = history_ref
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {}, primary_submit_cb=lambda a, t, d:
            laintas_cli._submit_primary_runtime_task(a, t, d, {}))
        controller.selected_id = agent.id
        entered = threading.Event()
        release = threading.Event()
        captured = {}

        def blocking_loop(_deps, _text, _session, state, history, **kwargs):
            captured.update(state=state, history=history,
                            queue=kwargs["message_queue"])
            entered.set()
            release.wait(timeout=2)
            return {"success": True, "state": state, "msg": "same run"}

        with mock.patch.object(laintas_cli, "run_agent_loop", blocking_loop):
            controller.dispatch("first task")
            self.assertTrue(entered.wait(timeout=1))
            admitted, _detail = agent_loop.begin_primary_run(agent.id)
            queued, _detail = agent_loop.queue_primary_message(
                agent.id, "outside update")

            self.assertFalse(admitted)
            self.assertTrue(queued)
            self.assertIs(captured["state"], state_ref)
            self.assertIs(captured["history"], history_ref)
            self.assertIs(captured["queue"], agent.message_queue)
            self.assertEqual(agent.message_queue.get_nowait(), "outside update")
            release.set()
            agent.thread.join(timeout=1)

        self.assertEqual(agent.status, "idle")
        self.assertIs(agent.state, state_ref)
        self.assertIs(agent.chat_history, history_ref)

    def test_agents_mode_is_only_a_primary_runtime_view(self):
        source = Path(agents_mode.__file__).read_text(encoding="utf-8")
        self.assertNotIn("run_agent_loop", source)
        self.assertNotIn("threading.Thread(", source)


class AgentUIEventHubTests(unittest.TestCase):
    def test_event_payload_is_detached_and_bounded(self):
        hub = agent_ui_events.AgentUIEventHub()
        source = {"nested": {"text": "before"}, "huge": "x" * 20_000}
        event = hub.emit(
            "tool_output", detail="y" * 60_000, data=source)
        source["nested"]["text"] = "after"

        self.assertEqual(event.data["nested"]["text"], "before")
        self.assertLessEqual(len(event.data["huge"]), 10_000)
        self.assertLessEqual(len(event.detail), 50_000)
        sanitized = hub.emit(
            "ai", summary="\x1b[31mred", detail="a\x00b\x1b[2J")
        self.assertEqual(sanitized.summary, "red")
        self.assertEqual(sanitized.detail, "ab")

    def test_snapshot_revision_and_rows_are_consistent(self):
        hub = agent_ui_events.AgentUIEventHub()
        first = hub.emit("ai", agent_id="a", detail="one")
        revision, rows = hub.agent_events_snapshot("a")
        self.assertEqual(revision, first.seq)
        self.assertEqual([row.detail for row in rows], ["one"])


class AgentsModeRenderingTests(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        agent_ui_events.hub.reset()
        terminal = mock.Mock()
        terminal.is_alive.return_value = True
        agent_loop.register_terminal(terminal, "/bin/sh", 0, name="term0")

    def tearDown(self):
        agent_loop.close_all_agents()
        agent_loop.close_all_terminals()
        agent_ui_events.hub.reset()

    def test_transcript_rows_are_cached_until_the_agent_changes(self):
        agent = agent_loop.register_agent(name="cached", role="pool")
        agent.home_terminal = "term0"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        agent_ui_events.hub.emit(
            "ai", agent_id=agent.id, terminal_name="term0", detail="answer")
        with mock.patch.object(
                agents_mode.agents_transcript, "build_blocks",
                wraps=agents_mode.agents_transcript.build_blocks) as build:
            first = controller._transcript_rows(agent.id, 60)
            second = controller._transcript_rows(agent.id, 60)
            self.assertIs(first, second)
            self.assertEqual(build.call_count, 1)
            agent_ui_events.hub.emit(
                "ai", agent_id=agent.id, terminal_name="term0", detail="more")
            controller._transcript_rows(agent.id, 60)
            self.assertEqual(build.call_count, 2)

    def test_profile_panel_shows_positioning_model_and_deployment(self):
        agent = agent_loop.register_agent(name="worker", role="pool")
        agent.home_terminal = "term0"
        agent.base_model = "glm-5.3"
        agent.profile.title = "Release engineer"
        agent.profile.description = "Builds and ships packages"
        agent.state["objective"] = "verify release"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        controller.set_available_models([{"id": "gpt-x"}])
        agent_ui_events.hub.emit(
            "tool_finished", agent_id=agent.id, terminal_name="term0",
            summary="pytest", status="done")
        text = "".join(value for _style, value in controller.inspector_fragments())
        for expected in ("Release engineer", "Builds and ships packages",
                         "glm-5.3", "not offered by backend", "not deployed",
                         "verify release", "1 call", "/agent worker"):
            self.assertIn(expected, text)

        agent.deployment_terminal = "term0"
        controller.set_available_models([{"id": "glm-5.3"}])
        text = "".join(value for _style, value in controller.inspector_fragments())
        self.assertIn("deployed in term0", text)
        self.assertIn("available", text)
        self.assertNotIn("not offered", text)

    def test_card_replaces_panel_when_the_panel_does_not_fit(self):
        agent = agent_loop.register_agent(name="worker", role="pool")
        agent.home_terminal = "term0"
        agent.profile.title = "Release engineer"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        with mock.patch.object(controller, "_terminal_size",
                               return_value=(100, 30)):
            rows = "\n".join(controller._transcript_text(agent.id))
        self.assertIn("Release engineer", rows)
        self.assertIn("No conversation yet", rows)
        with mock.patch.object(controller, "_terminal_size",
                               return_value=(170, 30)):
            controller._rows_cache.clear()
            rows = "\n".join(controller._transcript_text(agent.id))
        self.assertNotIn("Release engineer", rows)

class HwoUIRuntimeEventTests(unittest.TestCase):
    def test_step_binding_and_updates_are_exact(self):
        first = hwo_ui.HwoTask("first")
        nested = hwo_ui.HwoTask("nested")
        child = hwo_ui.HwoAgent("child", tasks=[nested])
        root = hwo_ui.HwoAgent("root", tasks=[first], children=[child])
        session = hwo_ui.HwoSession("primary", nodes=[root])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "flow.hwo")
            path.write_text(hwo_ui._session_to_hwo(session), encoding="utf-8")
            mapping = hwo_ui._bind_runtime_step_ids(session, str(path))

        self.assertEqual(set(mapping), {"0.0", "0.1.0"})
        hwo_ui._apply_runtime_events(mapping, [
            {"type": "step_started", "stepId": "0.1.0", "agentId": "child-id"},
        ])
        self.assertEqual(first.status, "pending")
        self.assertEqual(nested.status, "running")
        self.assertEqual(nested.agent_id, "child-id")
        hwo_ui._apply_runtime_events(mapping, [
            {"type": "step_completed", "stepId": "0.1.0"},
        ])
        self.assertEqual(nested.status, "done")
        self.assertIsNotNone(nested.completed_at)

    def test_metadata_round_trip_preserves_model_prompt_and_io(self):
        source = """@line [in(topic: string)]

(review.md)#reviewer@model-x# [in(topic: string), out(report: file)] {
  -> inspect $self.topic
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "flow.hwo")
            path.write_text(source, encoding="utf-8")
            session, error = hwo_ui.load_hwo_file(str(path))
        self.assertIsNone(error)
        agent = session.nodes[0]
        self.assertEqual(agent.prompt_file, "review.md")
        self.assertEqual(agent.model, "model-x")
        reparsed = hwo_runner.parse_hwo(hwo_ui._session_to_hwo(session))
        self.assertEqual(reparsed[0].prompt_file, "review.md")
        self.assertEqual(reparsed[0].model, "model-x")
        self.assertEqual(reparsed[0].io["out"][0]["name"], "report")
        ast = hwo_ui._session_to_hwo(session)
        self.assertIn("@line [in(topic: string)]", ast)

    def test_top_level_tasks_keep_parent_runtime_semantics(self):
        source = "-> inspect\n-> verify\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "parent.hwo")
            path.write_text(source, encoding="utf-8")
            session, error = hwo_ui.load_hwo_file(str(path))
        self.assertIsNone(error)
        self.assertTrue(all(isinstance(node, hwo_ui.HwoTask)
                            for node in session.nodes))
        self.assertEqual(hwo_ui._session_to_hwo(session), source)

    def test_real_studio_keyboard_adds_task_without_dsl(self):
        root = hwo_ui.HwoAgent("root")
        session = hwo_ui.HwoSession("primary", nodes=[root])
        session._last_agent = root
        with create_pipe_input() as pipe:
            # Studio starts in Outline navigation; the form exists only while
            # the add action is active.
            pipe.send_text("aWrite verification\r\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())
        self.assertEqual([task.text for task in root.tasks], ["Write verification"])

    def test_command_palette_accepts_hwo_dsl_then_returns_to_navigation(self):
        session = hwo_ui.HwoSession("primary")
        with create_pipe_input() as pipe:
            pipe.send_text(":#builder#\r:#builder#->ship release\r\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())
        agent = session.find_agent("builder")
        self.assertIsNotNone(agent)
        self.assertEqual([task.text for task in agent.tasks], ["ship release"])

    def test_hash_and_arrow_prefixes_quick_open_command_palette(self):
        session = hwo_ui.HwoSession("primary")
        with create_pipe_input() as pipe:
            pipe.send_text("#builder#\r->compile assets\r\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())
        agent = session.find_agent("builder")
        self.assertIsNotNone(agent)
        self.assertEqual([task.text for task in agent.tasks], ["compile assets"])

    def test_slash_help_moves_focus_off_hidden_command_field(self):
        session = hwo_ui.HwoSession(
            "primary", nodes=[hwo_ui.HwoAgent("root")])
        with create_pipe_input() as pipe:
            # Slash opens the palette, /h replaces it with Help, then the two
            # Esc presses close Help and Studio without focusing a hidden Window.
            pipe.send_text("/h\r\x1b\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())

    def test_delete_confirmation_defaults_to_cancel(self):
        root = hwo_ui.HwoAgent("root")
        session = hwo_ui.HwoSession("primary", nodes=[root])
        with create_pipe_input() as pipe:
            pipe.send_text("d\r\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())
        self.assertEqual(session.nodes, [root])

    def test_delete_confirmation_and_single_undo_restore_content(self):
        root = hwo_ui.HwoAgent("root", tasks=[hwo_ui.HwoTask("verify")])
        session = hwo_ui.HwoSession("primary", nodes=[root])
        with create_pipe_input() as pipe:
            pipe.send_text("dyu\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())
        self.assertEqual(len(session.nodes), 1)
        restored = session.nodes[0]
        self.assertEqual(restored.name, "root")
        self.assertEqual([task.text for task in restored.tasks], ["verify"])

    def test_slash_save_without_argument_opens_labeled_file_form(self):
        root = hwo_ui.HwoAgent("root", tasks=[hwo_ui.HwoTask("verify")])
        session = hwo_ui.HwoSession("primary", nodes=[root])
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "saved-workflow"))
            with create_pipe_input() as pipe:
                pipe.send_text(f"/w\r{path}\r\x1b")
                hwo_ui.run_hwo_ui(
                    "primary", initial_session=session,
                    input=pipe, output=DummyOutput())
            saved = Path(path + ".hwo")
            self.assertTrue(saved.exists())
            self.assertIn("#root#", saved.read_text(encoding="utf-8"))

    def test_studio_renders_distinct_navigation_form_command_and_confirm_surfaces(self):
        cases = [
            ("", ("NAVIGATION",)),
            ("a", ("ADD TASK", "Task          ┃")),
            (":", ("COMMAND PALETTE",)),
            ("d", ("DELETE", "[ Cancel ]")),
            ("?", ("HELP",)),
            ("i", ("INSPECTOR",)),
        ]
        all_frames = []
        for trigger, labels in cases:
            session = hwo_ui.HwoSession(
                "primary", nodes=[hwo_ui.HwoAgent("root")])
            screen = io.StringIO()
            output = Vt100_Output(
                screen, lambda: Size(rows=24, columns=100),
                term="xterm", enable_cpr=False)
            with create_pipe_input() as pipe:
                def drive(keys=trigger):
                    time.sleep(0.08)
                    if keys:
                        pipe.send_text(keys)
                    time.sleep(0.12)
                    pipe.send_text("\x03")

                driver = threading.Thread(target=drive)
                driver.start()
                hwo_ui.run_hwo_ui(
                    "primary", initial_session=session,
                    input=pipe, output=output)
                driver.join(timeout=1)
            plain = re.sub(
                r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen.getvalue())
            all_frames.append(plain)
            for label in labels:
                self.assertIn(label, plain)
        self.assertNotIn("command ›", "".join(all_frames))

    def test_slash_run_focus_and_cancel_confirmation_have_safe_transitions(self):
        root = hwo_ui.HwoAgent("root", tasks=[hwo_ui.HwoTask("verify")])
        session = hwo_ui.HwoSession("primary", nodes=[root])
        screen = io.StringIO()
        output = Vt100_Output(
            screen, lambda: Size(rows=24, columns=100),
            term="xterm", enable_cpr=False)
        started = threading.Event()
        release = threading.Event()

        def fake_run(**_kwargs):
            started.set()
            release.wait(2)
            return {"ok": True, "msg": "done"}

        with create_pipe_input() as pipe, mock.patch.object(
                hwo_runner, "run_hwo_file", side_effect=fake_run):
            def drive():
                time.sleep(0.08)
                pipe.send_text("/r\r")
                if not started.wait(1):
                    pipe.send_text("\x03")
                    return
                time.sleep(0.1)
                pipe.send_text("\x1b")       # running -> cancel confirmation
                time.sleep(0.3)
                pipe.send_text("\x1b")       # dismiss confirmation
                time.sleep(0.3)
                release.set()
                time.sleep(0.2)
                pipe.send_text("\x1b\x1b")  # result -> navigation -> exit

            driver = threading.Thread(target=drive)
            driver.start()
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=output)
            driver.join(timeout=3)
        self.assertTrue(started.is_set())
        self.assertFalse(driver.is_alive())
        plain = re.sub(
            r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen.getvalue())
        self.assertIn("RUNNING", plain)
        self.assertIn("CANCEL RUN", plain)
        self.assertIn("RESULT", plain)

    def test_narrow_studio_inspector_has_predictable_escape_path(self):
        root = hwo_ui.HwoAgent("root", tasks=[hwo_ui.HwoTask("verify")])
        session = hwo_ui.HwoSession("primary", nodes=[root])
        with create_pipe_input() as pipe, mock.patch.object(
                hwo_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 20))):
            # Outline -> inspector -> Outline -> exit.
            pipe.send_text("i\x1b\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())

    def test_wide_studio_inspector_has_predictable_escape_path(self):
        session = hwo_ui.HwoSession(
            "primary", nodes=[hwo_ui.HwoAgent("root")])
        with create_pipe_input() as pipe, mock.patch.object(
                hwo_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((140, 35))):
            pipe.send_text("\t\t\x1b")
            hwo_ui.run_hwo_ui(
                "primary", initial_session=session,
                input=pipe, output=DummyOutput())

    def test_nested_parallel_loader_refuses_lossy_edit(self):
        source = """#root# {
  //
    #a# { -> one }
    #b# { -> two }
  //
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "nested.hwo")
            path.write_text(source, encoding="utf-8")
            session, error = hwo_ui.load_hwo_file(str(path))
        self.assertIsNone(session)
        self.assertIn("not editable", error)


if __name__ == "__main__":
    unittest.main()
