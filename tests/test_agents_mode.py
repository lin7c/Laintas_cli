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


class _WideOutput(DummyOutput):
    """Wide enough for the rail-and-conversation layout (DummyOutput is 80)."""

    def get_size(self):
        return Size(rows=40, columns=120)


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

    def test_roster_lists_persistent_agents_not_subagents(self):
        primary = self._agent("primary", role="primary")
        worker = self._agent("worker")
        child = self._agent("child", role="subagent")
        child.parent_id = worker.id
        controller = agents_mode.AgentsModeController("term0", object(), {})
        ids = [a.id for a in controller.agents()]
        self.assertEqual([primary.id, worker.id], ids)
        self.assertNotIn(child.id, ids)

    def test_enter_opens_detail_and_switches_on_exit(self):
        self._agent("primary", role="primary")
        worker = self._agent("worker")
        worker.parent_id = "primary"
        child = self._agent("child", role="subagent")
        child.parent_id = worker.id
        agent_loop._current_agent_id = "primary"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.select(worker.id)
        controller.open_detail()
        self.assertTrue(controller.detail)
        text = "\n".join("".join(t for _s, t, *_ in row)
                         for row in controller._detail_rows(80))
        for heading in ("WHO", "RESPONSIBILITY", "NOW", "DELEGATED (1)",
                        "RECENT TASKS", "SWITCH"):
            self.assertIn(heading, text)
        self.assertIn("child", text)
        self.assertEqual(["/agent worker"], controller.exit_commands())

    def test_busy_agent_is_not_made_a_switch_target(self):
        self._agent("primary", role="primary")
        worker = self._agent("worker")
        worker.status = "running"
        agent_loop._current_agent_id = "primary"
        controller = agents_mode.AgentsModeController("term0", object(), {})
        controller.select(worker.id)
        controller.open_detail()
        self.assertEqual([], controller.exit_commands())
        self.assertIn("working", controller.notice)

    def test_view_has_no_input_and_ctrl_c_never_leaves(self):
        self._agent("worker")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        seen = {}
        with create_pipe_input() as pipe_input:
            def keys():
                time.sleep(0.3)
                pipe_input.send_text("hello\x03")
                time.sleep(0.2)
                seen["running"] = controller.app is not None
                seen["notice"] = controller.notice
                pipe_input.send_text("\x1b")
            sender = threading.Thread(target=keys)
            sender.start()
            controller.run(input=pipe_input, output=DummyOutput())
            sender.join(timeout=2)
        self.assertTrue(seen["running"])
        self.assertIn("Esc", seen["notice"])
        self.assertFalse(hasattr(controller, "dispatch"))

    def test_real_application_event_loop_starts_and_quits_cleanly(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        with create_pipe_input() as pipe_input:
            pipe_input.send_bytes(b"\x11")  # Ctrl-Q
            controller.run(input=pipe_input, output=DummyOutput())
        self.assertIsNone(controller.app)

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

    def test_escape_closes_mode_without_dispatching_to_agent(self):
        self._agent("primary", role="primary")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {})
        with create_pipe_input() as pipe_input:
            pipe_input.send_bytes(b"\x1b")
            controller.run(input=pipe_input, output=_WideOutput())

        self.assertFalse(any(
            event.event_type == "user_message"
            for event in agent_ui_events.hub.events("term0")))

    def test_escape_works_from_a_narrow_conversation(self):
        self._agent("primary", role="primary")
        self._agent("worker", role="pool")
        controller = agents_mode.AgentsModeController(
            "term0", mock.Mock(), {})
        with create_pipe_input() as pipe_input:
            # Narrow terminals open on the agent list; Enter opens the
            # selected Agent, Esc steps back out level by level.
            pipe_input.send_bytes(b"\r\x1b\x1b\x1b")
            controller.run(input=pipe_input, output=DummyOutput())

        self.assertFalse(any(
            event.event_type == "user_message"
            for event in agent_ui_events.hub.events("term0")))

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
            detail="latest " + ('\u754c' * 80) + " ENDLATEST")
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
                detail=f"**reply** {index} " + '\u754c' * (index % 7))
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

    def test_switching_terminal_lists_the_agent_deployed_there(self):
        # An Agent deployed in a terminal is still an Agent of that terminal:
        # the roster lists every persistent Agent, deployed or not.
        self._terminal("build")
        deployed = self._agent("builder", terminal="build")
        self.assertTrue(agent_loop.station_agent(deployed.id, "build"))
        other = self._agent("tester", terminal="build")
        controller = agents_mode.AgentsModeController("term0", object(), {})
        while controller.terminal_name != "build":
            controller.cycle_terminal(1)
        ids = {a.id for a in controller.agents()}
        self.assertEqual({deployed.id, other.id}, ids)
        self.assertIn(controller.selected_id, ids)

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
