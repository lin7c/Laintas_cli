import queue
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import agent_loop
import agent_ui_events
import agents_mode
import hwo_ui
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

    def test_stream_chunks_render_as_one_assistant_message(self):
        agent = self._agent("writer")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        agent_ui_events.hub.ingest(agent.id, [
            {"type": "ai_stream", "content": "hello "},
            {"type": "ai_stream", "content": "world\nnext"},
            {"type": "ai_end"},
        ], "term0")

        lines = controller._event_lines(agent.id)

        self.assertEqual(sum(text == "writer" for _style, text in lines), 1)
        self.assertIn(("", "hello world"), lines)
        self.assertIn(("", "next"), lines)

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

        focus_text = [text for _style, text
                      in controller._event_lines(agent.id)]
        feed_text = "".join(fragment[1]
                            for fragment in controller.feed_fragments())

        self.assertEqual(focus_text.count("hello"), 1)
        self.assertEqual(feed_text.count("hello"), 1)
        self.assertNotIn("ai_end", feed_text)

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
        running = controller._event_lines(agent.id)
        self.assertTrue(any(text.startswith("◐ shell.exec") for _s, text in running))
        agent_ui_events.hub.ingest(agent.id, [{
            "type": "system", "kind": "tool", "content": "shell.exec",
            "meta": {"call_id": "call-1", "salient": "pytest -q", "ok": True},
        }], "term0")
        finished = controller._event_lines(agent.id)
        self.assertFalse(any(text.startswith("◐ shell.exec") for _s, text in finished))
        self.assertEqual(sum(text.startswith("● shell.exec")
                             for _s, text in finished), 1)

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

        self.assertIs(wired.console, controller._silent_console)
        self.assertIsNot(wired.console, deps.console)
        # These calls must be harmless and write nothing to the terminal.
        wired.console.print("hidden worker output")
        wired.display_command_output("cmd", 0, "output")

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
        self.assertIn("Terminal: term0", rendered)
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

        lines = controller._event_lines(agent.id)
        focus = list(controller.focus_fragments())

        self.assertIn(("class:md.h1", "Result"), lines)
        self.assertTrue(any(style == "class:md.bold" and text == "bold"
                            for style, text, *_ in focus))
        self.assertTrue(any(style == "class:md.code" and text == "code"
                            for style, text, *_ in focus))
        self.assertNotIn("Task completed", "".join(text for _style, text in lines))

    def test_running_agent_shows_thinking_then_writing(self):
        agent = self._agent("writer")
        agent.status = "thinking"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.selected_id = agent.id
        with mock.patch.object(agents_mode.time, "monotonic", return_value=0):
            first = controller._activity_line(agent.id)[1]
        with mock.patch.object(agents_mode.time, "monotonic", return_value=0.21):
            second = controller._activity_line(agent.id)[1]
        self.assertEqual(first, "●  ·  ·")
        self.assertEqual(second, "·  ●  ·")

        agent_ui_events.hub.emit(
            "ai_stream", agent_id=agent.id, terminal_name="term0",
            detail="partial")

        self.assertEqual(controller._activity_line(agent.id)[1], "◐ Writing…")

    def test_primary_follow_uses_wrapped_screen_rows_after_five_turns(self):
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
        with mock.patch.object(
                controller, "_terminal_size", return_value=(40, 18)):
            bottom = "".join(
                text for _style, text in controller.focus_fragments())
            controller.scroll(6)
            scrolled = "".join(
                text for _style, text in controller.focus_fragments())
            controller.scroll(-6)
            followed = "".join(
                text for _style, text in controller.focus_fragments())

        self.assertIn("END-5", bottom)
        self.assertNotIn("TURN-1", bottom)
        self.assertTrue(controller.follow[agent.id])
        self.assertNotIn("END-5", scrolled)
        self.assertIn("END-5", followed)

    def test_primary_activity_remains_below_latest_wrapped_row(self):
        agent = self._agent("primary", role="primary")
        agent.status = "thinking"

        class Mirror:
            @staticmethod
            def read_lines(_agent_id):
                return ["latest " + ("界" * 80) + " END-LATEST"]

        controller = agents_mode.AgentsModeController(
            "term0", object(), {}, mirror=Mirror())
        controller.selected_id = agent.id
        with mock.patch.object(
                controller, "_terminal_size", return_value=(40, 18)), \
                mock.patch.object(agents_mode.time, "monotonic", return_value=0):
            rendered = "".join(
                text for _style, text in controller.focus_fragments())

        self.assertIn("END-LATEST", rendered)
        self.assertIn("●  ·  ·", rendered)

    def test_focus_height_matches_full_layout_at_standard_terminal_size(self):
        agent = self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", object(), {})
        controller.selected_id = agent.id

        with mock.patch.object(
                controller, "_terminal_size", return_value=(80, 25)):
            width, body_height = controller._focus_body_height()

        self.assertEqual(width, 80)
        self.assertEqual(body_height, 12)

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


if __name__ == "__main__":
    unittest.main()
