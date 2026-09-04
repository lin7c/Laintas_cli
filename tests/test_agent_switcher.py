"""Alt+A during a run, and the input reader it lives in.

Alt+A opens the /agents view. That is where work is handed to another Agent —
selecting one there and typing calls start_agent_assignment, which runs it on
its own thread. The view is a slash command, and slash commands are dispatched
by the main thread after _get_input() returns, which never happens while a
turn is running; so without this key the multi-agent surface is unreachable in
exactly the situation that needs it.
"""
import threading
import unittest
from unittest import mock

import agent_loop
import laintas_cli


class _RegistryCase(unittest.TestCase):
    def setUp(self):
        self._saved = dict(agent_loop._agent_registry)
        self._saved_current = agent_loop._current_agent_id
        self._saved_previous = agent_loop._previous_agent_id
        agent_loop._agent_registry.clear()

    def tearDown(self):
        agent_loop._agent_registry.clear()
        agent_loop._agent_registry.update(self._saved)
        agent_loop._current_agent_id = self._saved_current
        agent_loop._previous_agent_id = self._saved_previous

    def _agent(self, agent_id, index, parent=None, role="pool", name=None):
        info = agent_loop.AgentInfo(
            id=agent_id, name=name or agent_id, index=index,
            parent_id=parent, role=role)
        agent_loop._agent_registry[agent_id] = info
        return info

    def _three(self):
        self._agent("primary", 0, role="primary")
        self._agent("AI-2", 1, parent="primary", name="researcher")
        self._agent("AI-3", 2, parent="AI-2")


class MostRecentlyUsedTests(_RegistryCase):
    def test_the_previous_agent_is_remembered(self):
        self._three()
        agent_loop.set_current_agent_id("primary")
        agent_loop.set_current_agent_id("AI-2")
        self.assertEqual("primary", agent_loop.previous_agent_id())

    def test_reselecting_the_same_agent_is_not_a_switch(self):
        self._three()
        agent_loop.set_current_agent_id("primary")
        agent_loop.set_current_agent_id("AI-2")
        agent_loop.set_current_agent_id("AI-2")
        self.assertEqual("primary", agent_loop.previous_agent_id())

    def test_a_departed_agent_is_not_offered_as_the_previous_one(self):
        self._three()
        agent_loop.set_current_agent_id("AI-3")
        agent_loop.set_current_agent_id("primary")
        del agent_loop._agent_registry["AI-3"]
        self.assertEqual("", agent_loop.previous_agent_id())


class ReaderRewiringTests(unittest.TestCase):
    """Pausing the reader for a prompt must not rewire it.

    A primary's run starts the reader on that Agent's own message queue and
    abort event, and hands run_agent_loop the same pair. Restarting it on the
    module-level user queue instead means supplementary typing lands where
    nothing drains it and Esc sets an event nothing checks — which is what
    every approval prompt used to do, so the first shell command of a turn
    silently killed typing for the rest of it.
    """

    def setUp(self):
        self._saved = (laintas_cli._bg_reader_thread,
                       laintas_cli._bg_reader_args)
        laintas_cli._bg_reader_thread = None
        laintas_cli._bg_reader_args = ()

    def tearDown(self):
        laintas_cli._bg_reader_thread, laintas_cli._bg_reader_args = self._saved

    def _start_and_capture(self, target_queue, interrupt_event):
        """Start a reader whose mode function only records how it was wired."""
        seen = []
        started = threading.Event()

        def _record(q, ev, _stop):
            seen.append((q, ev))
            started.set()

        with mock.patch.object(laintas_cli, "_bg_reader_cbreak_mode", _record), \
                mock.patch.object(laintas_cli.sys.stdin, "isatty",
                                  return_value=True), \
                mock.patch.object(laintas_cli, "_set_run_input_state"):
            laintas_cli._start_bg_input_reader(target_queue, interrupt_event)
            started.wait(5)
            thread = laintas_cli._bg_reader_thread
            if thread is not None:
                thread.join(5)
            laintas_cli._bg_reader_thread = None
        return seen

    def test_a_restart_reuses_the_queue_and_event_the_run_is_watching(self):
        import queue as _queue

        run_queue, run_event = _queue.Queue(), threading.Event()
        self._start_and_capture(run_queue, run_event)

        # What an approval prompt does: stop, ask, restart.
        restarted = []
        started = threading.Event()

        def _record(q, ev, _stop):
            restarted.append((q, ev))
            started.set()

        with mock.patch.object(laintas_cli, "_bg_reader_cbreak_mode", _record), \
                mock.patch.object(laintas_cli.sys.stdin, "isatty",
                                  return_value=True), \
                mock.patch.object(laintas_cli, "_set_run_input_state"):
            laintas_cli._restart_bg_input_reader()
            started.wait(5)
            thread = laintas_cli._bg_reader_thread
            if thread is not None:
                thread.join(5)
            laintas_cli._bg_reader_thread = None

        self.assertEqual([(run_queue, run_event)], restarted)
        # Specifically NOT the module-level pair, which is what it used to be.
        self.assertIsNot(run_queue, agent_loop.get_user_message_queue())
        self.assertIsNot(run_event, agent_loop.get_user_interrupt_event())

    def test_a_primary_run_and_the_user_globals_are_different_objects(self):
        """The premise the bug depended on, pinned so it cannot drift back."""
        agent = agent_loop.AgentInfo(
            id="primary", name="primary", index=0, role="primary")
        self.assertIsNot(
            agent.message_queue, agent_loop.get_user_message_queue())
        self.assertIsNot(
            agent.abort_event, agent_loop.get_user_interrupt_event())

    def test_a_restart_with_nothing_remembered_is_a_no_op(self):
        laintas_cli._bg_reader_args = ()
        with mock.patch.object(laintas_cli, "_start_bg_input_reader") as start:
            laintas_cli._restart_bg_input_reader()
        start.assert_not_called()


class MidRunHandoverTests(_RegistryCase):
    """The whole point, end to end: a turn is running and you get out.

    A real _run_agent_loop_with_interrupt on the main thread — where a
    primary's turn really does run — with the real input reader pressing
    Alt+A from its own thread. The view's prompt_toolkit app cannot run in a
    test, so the controller is a stand-in that blocks the way the real one
    does; everything between the keystroke and it is the production path.

    The other half — dispatching from the view actually starting an employee
    on its own thread — is test_agents_mode's
    test_one_shot_route_does_not_change_focus and its neighbours.
    """

    def setUp(self):
        super().setUp()
        self._agent("primary", 0, role="primary")
        self._agent("AI-2", 1, parent="primary", name="researcher")
        agent_loop.set_current_agent_id("primary")
        self._saved_run_active = laintas_cli._foreground_run_active

    def tearDown(self):
        laintas_cli._foreground_run_active = self._saved_run_active
        laintas_cli.repl_mirror.hub.set_owner("cli")
        try:
            laintas_cli._exit_agents_view()
        except Exception:
            pass
        super().tearDown()

    def test_alt_a_hands_the_screen_over_while_the_turn_keeps_running(self):
        import queue as _queue
        from terminal_arbiter import Key

        in_loop = threading.Event()
        view_open = threading.Event()
        may_finish = threading.Event()
        turn_finished = threading.Event()
        observed = {}

        def _fake_turn(*_a, **_kw):
            in_loop.set()
            # Wait for the view to take over, then look around from inside
            # the still-running turn.
            view_open.wait(5)
            observed["view_active"] = laintas_cli._agents_view_is_active()
            observed["screen_owner_is_view"] = (
                laintas_cli.repl_mirror.hub.is_agents())
            observed["run_still_marked_active"] = (
                laintas_cli._foreground_run_active)
            may_finish.set()
            turn_finished.set()
            return {"success": True, "msg": "done", "state": {},
                    "session": None, "exit_reason": "complete"}

        class _StubController:
            """Stands in for the view: takes the screen, then blocks."""

            app = None

            def __init__(self, *_a, **_kw):
                pass

            def run(self):
                view_open.set()
                may_finish.wait(5)

        stop = threading.Event()
        term = _StopWhenDry(_FakeTerm([Key("alt", "a")]), stop)

        def _press_alt_a():
            in_loop.wait(5)
            laintas_cli._bg_reader_cbreak_mode(
                _queue.Queue(), threading.Event(), stop)

        presser = threading.Thread(target=_press_alt_a, daemon=True)
        import agents_mode
        with mock.patch.object(laintas_cli, "run_agent_loop", _fake_turn), \
                mock.patch.object(agents_mode, "AgentsModeController",
                                  _StubController), \
                mock.patch.object(laintas_cli, "_start_bg_input_reader"), \
                mock.patch.object(laintas_cli, "_stop_bg_input_reader",
                                  return_value=True), \
                mock.patch.object(laintas_cli, "_set_run_input_state"), \
                mock.patch.object(laintas_cli.terminal_arbiter, "hold",
                                  return_value=_FakeHold(term, stop)), \
                mock.patch.object(laintas_cli.sys.stdout, "write"), \
                mock.patch.object(laintas_cli.sys.stdout, "flush"):
            presser.start()
            response = laintas_cli._run_agent_loop_with_interrupt(
                laintas_cli.get_loop_deps(), "long task", {}, {}, [])
        # Tell the reader to leave, then insist that it did. `join(5)` alone
        # lets a thread that is still running outlive this test: it goes on
        # calling module-level functions, and a LATER test that has one of
        # them patched records the call as its own. That is what made
        # `_restart_bg_input_reader` look like it fired twice in
        # MidRunOpenerTests, at roughly one run in two, with nothing in the
        # failing test to explain it.
        stop.set()
        presser.join(5)
        self.assertFalse(presser.is_alive(),
                         "the reader thread outlived its test")

        # The view came up while the turn was mid-flight...
        self.assertTrue(observed.get("view_active"), "view never opened")
        self.assertTrue(observed.get("screen_owner_is_view"),
                        "the view did not take the screen")
        # ...and the turn was neither interrupted nor lost.
        self.assertTrue(observed.get("run_still_marked_active"))
        self.assertTrue(turn_finished.is_set(), "the turn stopped")
        self.assertEqual("done", response.get("msg"))


if __name__ == "__main__":
    unittest.main()


class _FakeTerm:
    """A scripted key source standing in for the arbiter's terminal hold."""

    interactive = True

    def __init__(self, keys):
        self._keys = list(keys)

    def read_key(self, timeout=0.0):
        return self._keys.pop(0) if self._keys else None


class _StopWhenDry:
    """Ends the reader loop when the scripted keys run out."""

    interactive = True

    def __init__(self, term, stop):
        self._term, self._stop = term, stop

    def read_key(self, timeout=0.0):
        key = self._term.read_key(timeout)
        if key is None:
            self._stop.set()
        return key


class _FakeHold:
    def __init__(self, term, stop):
        self._term, self._stop = term, stop

    def __enter__(self):
        return self._term

    def __exit__(self, *exc):
        return False


class ReaderKeyTests(_RegistryCase):
    """The key handling itself, driven through the real reader loop."""

    def _run(self, keys):
        """Drive the real reader over scripted keys. Returns what it wrote."""
        import queue as _queue
        from terminal_arbiter import Key

        self._three()
        agent_loop.set_current_agent_id("primary")
        agent_loop._previous_agent_id = None

        stop = threading.Event()
        interrupt = threading.Event()
        target = _queue.Queue()
        scripted = [Key(*k) if isinstance(k, tuple) else Key(k) for k in keys]
        term = _StopWhenDry(_FakeTerm(scripted), stop)
        written = []
        with mock.patch.object(laintas_cli.terminal_arbiter, "hold",
                               return_value=_FakeHold(term, stop)), \
                mock.patch.object(laintas_cli.sys.stdout, "write",
                                  side_effect=written.append), \
                mock.patch.object(laintas_cli.sys.stdout, "flush"), \
                mock.patch.object(laintas_cli, "_terminal_width",
                                  return_value=200):
            laintas_cli._bg_reader_cbreak_mode(target, interrupt, stop)
        return "".join(written), interrupt, target

    def test_alt_a_opens_the_agents_view(self):
        with mock.patch.object(laintas_cli,
                               "_open_agents_view_from_run") as opener:
            _out, interrupt, _target = self._run([("alt", "a")])
        opener.assert_called_once_with()
        # Not an interrupt: the turn it was pressed during keeps running.
        self.assertFalse(interrupt.is_set())

    def test_the_view_is_opened_only_after_the_terminal_hold_is_released(self):
        """The view's prompt_toolkit app needs the terminal this reader holds
        in CBREAK. Opening while still inside the hold hands it a terminal
        somebody else owns."""
        import queue as _queue
        from terminal_arbiter import Key

        self._three()
        agent_loop.set_current_agent_id("primary")
        order = []
        stop = threading.Event()
        term = _StopWhenDry(_FakeTerm([Key("alt", "a")]), stop)

        class _RecordingHold:
            def __enter__(self):
                order.append("hold")
                return term

            def __exit__(self, *exc):
                order.append("release")
                return False

        with mock.patch.object(laintas_cli.terminal_arbiter, "hold",
                               return_value=_RecordingHold()), \
                mock.patch.object(laintas_cli.sys.stdout, "write"), \
                mock.patch.object(laintas_cli.sys.stdout, "flush"), \
                mock.patch.object(laintas_cli, "_open_agents_view_from_run",
                                  side_effect=lambda: order.append("open")):
            laintas_cli._bg_reader_cbreak_mode(
                _queue.Queue(), threading.Event(), stop)

        self.assertEqual(["hold", "release", "open"], order)

    def test_a_half_typed_line_is_cleared_before_the_view_takes_over(self):
        with mock.patch.object(laintas_cli, "_open_agents_view_from_run"):
            out, _i, _t = self._run(
                [("text", "h"), ("text", "i"), ("alt", "a")])
        # The draft is erased, not left painted under a full-screen view.
        self.assertTrue(out.endswith("\r  \r"), out[-12:])

    def test_escape_still_interrupts_the_run(self):
        _out, interrupt, _t = self._run(["escape"])
        self.assertTrue(interrupt.is_set())

    def test_typing_still_reaches_the_running_turn(self):
        _out, _i, target = self._run([("text", "o"), ("text", "k"), "enter"])
        self.assertEqual("ok", target.get_nowait())


class MidRunOpenerTests(unittest.TestCase):
    """_open_agents_view_from_run: what happens around the view."""

    def setUp(self):
        self._saved = (laintas_cli._foreground_run_active,
                       laintas_cli._repl_session)
        laintas_cli._foreground_run_active = True
        laintas_cli._repl_session = {"userId": "u1"}

    def tearDown(self):
        (laintas_cli._foreground_run_active,
         laintas_cli._repl_session) = self._saved

    def test_it_opens_the_view_with_the_runs_session(self):
        with mock.patch.object(laintas_cli, "_open_agents_view",
                               return_value=True) as opener, \
                mock.patch.object(laintas_cli, "_set_run_input_state"):
            laintas_cli._open_agents_view_from_run()
        self.assertEqual({"userId": "u1"}, opener.call_args[0][0])

    def test_closing_the_view_puts_the_reader_back_while_the_turn_runs(self):
        with mock.patch.object(laintas_cli, "_open_agents_view",
                               return_value=True) as opener, \
                mock.patch.object(laintas_cli, "_restart_bg_input_reader") as restart, \
                mock.patch.object(laintas_cli, "_set_run_input_state"):
            laintas_cli._open_agents_view_from_run()
            restart.assert_not_called()      # not until the view closes
            opener.call_args.kwargs["on_close"]()
        restart.assert_called_once_with()

    def test_a_finished_turn_leaves_the_terminal_to_the_prompt(self):
        """If the run ended while the view was open, the main loop is about to
        draw its own prompt; a reader in CBREAK would fight it for stdin."""
        with mock.patch.object(laintas_cli, "_open_agents_view",
                               return_value=True) as opener, \
                mock.patch.object(laintas_cli, "_restart_bg_input_reader") as restart, \
                mock.patch.object(laintas_cli, "_set_run_input_state"):
            laintas_cli._open_agents_view_from_run()
            laintas_cli._foreground_run_active = False
            opener.call_args.kwargs["on_close"]()
        restart.assert_not_called()

    def test_a_view_that_refuses_to_open_gives_the_keyboard_back(self):
        with mock.patch.object(laintas_cli, "_open_agents_view",
                               return_value=False), \
                mock.patch.object(laintas_cli, "_restart_bg_input_reader") as restart, \
                mock.patch.object(laintas_cli, "_set_run_input_state"):
            laintas_cli._open_agents_view_from_run()
        restart.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
