"""Switching agent with Alt+A while a task is running.

The switch itself cannot happen when the key is pressed: it rebinds the REPL's
state and history objects, and the running loop is holding both. So the key
picks a target and parks it, and the main loop performs the switch when it is
idle again — the same reason the prompt's own slot switcher submits
"/agent <id>" instead of doing the work in a keybinding handler.
"""
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
        return laintas_cli._agent_switch_rows()


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


class OpeningPositionTests(_RegistryCase):
    def test_it_opens_on_the_previous_agent(self):
        """Alt+A then Enter is 'back to the one I was just on' — the switch
        people actually make, and tmux's last-window."""
        rows = self._three()
        agent_loop.set_current_agent_id("AI-3")
        agent_loop.set_current_agent_id("primary")
        self.assertEqual(
            "AI-3", laintas_cli._agent_switch_initial(rows, "primary"))

    def test_with_no_history_it_opens_on_the_next_one(self):
        rows = self._three()
        agent_loop.set_current_agent_id("primary")
        agent_loop._previous_agent_id = None
        self.assertEqual(
            "AI-2", laintas_cli._agent_switch_initial(rows, "primary"))

    def test_it_never_opens_on_the_agent_you_are_already_using(self):
        rows = self._three()
        agent_loop.set_current_agent_id("primary")
        agent_loop._previous_agent_id = "primary"
        self.assertNotEqual(
            "primary", laintas_cli._agent_switch_initial(rows, "primary"))

    def test_an_empty_registry_selects_nothing(self):
        self.assertEqual("", laintas_cli._agent_switch_initial([], "primary"))


class NavigationTests(_RegistryCase):
    def test_stepping_wraps_in_both_directions(self):
        rows = self._three()
        self.assertEqual("AI-2", laintas_cli._agent_switch_step(rows, "primary", 1))
        self.assertEqual("primary", laintas_cli._agent_switch_step(rows, "AI-3", 1))
        self.assertEqual("AI-3", laintas_cli._agent_switch_step(rows, "primary", -1))

    def test_stepping_from_something_gone_lands_on_the_first(self):
        rows = self._three()
        self.assertEqual("primary",
                         laintas_cli._agent_switch_step(rows, "vanished", 1))

    def test_digits_jump_by_registry_index_not_list_position(self):
        """A number that means a different agent depending on what is open is
        a number nobody can learn."""
        self._agent("primary", 0, role="primary")
        self._agent("late", 7, parent="primary")
        rows = laintas_cli._agent_switch_rows()
        self.assertEqual("late", laintas_cli._agent_switch_jump(rows, "7"))
        self.assertEqual("primary", laintas_cli._agent_switch_jump(rows, "0"))

    def test_an_unused_digit_selects_nothing(self):
        rows = self._three()
        self.assertEqual("", laintas_cli._agent_switch_jump(rows, "9"))
        self.assertEqual("", laintas_cli._agent_switch_jump(rows, "x"))


class RenderingTests(_RegistryCase):
    def _line(self, rows, selected, current, width=200):
        return "".join(text for _style, text in laintas_cli._agent_switch_segments(
            rows, selected, current, width))

    def test_every_agent_is_listed_with_its_index_and_depth(self):
        rows = self._three()
        line = self._line(rows, "AI-2", "primary")
        self.assertIn("0 primary", line)
        self.assertIn("1", line)
        self.assertIn("researcher", line)      # the name, not the id
        self.assertIn("└", line)               # the tree, as choose-tree does

    def test_the_agent_in_use_is_marked(self):
        rows = self._three()
        self.assertIn("primary*", self._line(rows, "AI-2", "primary"))

    def test_the_selection_gets_its_own_style(self):
        rows = self._three()
        styles = {agent: style for style, agent in
                  ((s, t.strip()) for s, t in
                   laintas_cli._agent_switch_segments(rows, "AI-3", "primary", 200))}
        self.assertEqual("selected", styles.get("2     └ AI-3"))

    def test_it_stays_on_one_line(self):
        # The run's Live region owns the rows below; a taller widget would
        # fight it for them.
        for _ in range(12):
            index = len(agent_loop._agent_registry)
            self._agent(f"AI-{index}", index, parent="primary"
                        if "primary" in agent_loop._agent_registry else None)
        rows = laintas_cli._agent_switch_rows()
        line = self._line(rows, rows[0][1], rows[0][1], width=60)
        self.assertNotIn("\n", line)
        self.assertLessEqual(agent_loop._cell_len(line), 59)

    def test_the_hint_is_dropped_before_the_entries_are(self):
        rows = self._three()
        line = self._line(rows, "AI-2", "primary", width=44)
        self.assertIn("primary", line)
        self.assertNotIn("Esc cancel", line)

    def test_rendering_wraps_each_segment_and_resets(self):
        segments = [("selected", " x "), ("item", " y ")]
        out = laintas_cli._agent_switch_render(segments)
        self.assertEqual(2, out.count("\x1b[0m"))
        self.assertIn("\x1b[7m x \x1b[0m", out)


class PendingSwitchTests(_RegistryCase):
    def test_the_target_is_parked_not_applied(self):
        """Applying it here would rebind objects the running loop is holding."""
        self.assertEqual("", laintas_cli._pending_agent_switch)

    def test_the_repl_applies_it_through_the_same_command_path(self):
        # /agent is the only place a switch happens correctly; the switcher
        # must not grow a second implementation of it.
        self._three()
        laintas_cli._pending_agent_switch = "AI-2"
        try:
            with mock.patch.object(laintas_cli, "_cmd_agent") as cmd:
                done = laintas_cli._apply_pending_agent_switch({}, None)
            cmd.assert_called_once_with(["/agent", "AI-2"], {}, None)
            self.assertEqual("AI-2", done)
            self.assertEqual("", laintas_cli._pending_agent_switch)
        finally:
            laintas_cli._pending_agent_switch = ""

    def test_applying_nothing_costs_nothing(self):
        with mock.patch.object(laintas_cli, "_cmd_agent") as cmd:
            self.assertEqual("", laintas_cli._apply_pending_agent_switch({}, None))
        cmd.assert_not_called()

    def test_a_failed_switch_is_reported_and_cleared(self):
        # A target that cannot be switched to must not be retried on every
        # later turn.
        laintas_cli._pending_agent_switch = "gone"
        with mock.patch.object(laintas_cli, "_cmd_agent",
                               side_effect=ValueError("no such agent")), \
                mock.patch.object(laintas_cli.console, "print") as printed:
            self.assertEqual("", laintas_cli._apply_pending_agent_switch({}, None))
        self.assertEqual("", laintas_cli._pending_agent_switch)
        self.assertTrue(printed.called)

    def test_the_label_prefers_the_name(self):
        self._three()
        self.assertEqual("researcher", laintas_cli._agent_switch_label("AI-2"))
        self.assertEqual("gone", laintas_cli._agent_switch_label("gone"))


if __name__ == "__main__":
    unittest.main()


class _FakeTerm:
    """A scripted key source standing in for the arbiter's terminal hold."""

    interactive = True

    def __init__(self, keys):
        self._keys = list(keys)

    def read_key(self, timeout=0.0):
        return self._keys.pop(0) if self._keys else None


class _FakeHold:
    def __init__(self, term, stop):
        self._term, self._stop = term, stop

    def __enter__(self):
        return self._term

    def __exit__(self, *exc):
        return False


class ReaderKeyTests(_RegistryCase):
    """The key handling itself, driven through the real reader loop."""

    def _run(self, keys, buf_text=""):
        import queue as _queue
        import threading
        from terminal_arbiter import Key

        self._three()
        agent_loop.set_current_agent_id("primary")
        agent_loop._previous_agent_id = None
        laintas_cli._pending_agent_switch = ""

        stop = threading.Event()
        interrupt = threading.Event()
        target = _queue.Queue()
        # The reader exits when read_key runs dry, via the stop event set by
        # the last scripted key.
        scripted = [Key(*k) if isinstance(k, tuple) else Key(k) for k in keys]

        class _Stopper:
            def __init__(self, term, event):
                self.term, self.event = term, event

            def read_key(self, timeout=0.0):
                key = self.term.read_key(timeout)
                if key is None:
                    self.event.set()
                return key

            interactive = True

        term = _Stopper(_FakeTerm(scripted), stop)
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

    def test_alt_a_opens_the_switcher_and_enter_parks_a_target(self):
        out, interrupt, _ = self._run([("alt", "a"), "enter"])
        self.assertIn("switch agent", out)
        # Opens on the next agent, so Enter alone is a real switch.
        self.assertEqual("AI-2", laintas_cli._pending_agent_switch)
        self.assertIn("when this turn finishes", out)
        self.assertFalse(interrupt.is_set())
        laintas_cli._pending_agent_switch = ""

    def test_escape_cancels_the_switcher_without_interrupting_the_run(self):
        """Esc means 'stop the task' everywhere else; inside the switcher it
        must mean 'never mind' and nothing more."""
        out, interrupt, _ = self._run([("alt", "a"), "escape"])
        self.assertEqual("", laintas_cli._pending_agent_switch)
        self.assertFalse(interrupt.is_set())
        self.assertNotIn("when this turn finishes", out)

    def test_escape_still_interrupts_when_the_switcher_is_closed(self):
        _out, interrupt, _ = self._run(["escape"])
        self.assertTrue(interrupt.is_set())

    def test_arrows_move_the_selection(self):
        self._run([("alt", "a"), "down", "enter"])
        self.assertEqual("AI-3", laintas_cli._pending_agent_switch)
        laintas_cli._pending_agent_switch = ""

    def test_a_digit_jumps_by_index(self):
        self._run([("alt", "a"), ("text", "2"), "enter"])
        self.assertEqual("AI-3", laintas_cli._pending_agent_switch)
        laintas_cli._pending_agent_switch = ""

    def test_typing_while_the_switcher_is_open_does_not_reach_the_draft(self):
        _out, _i, target = self._run(
            [("alt", "a"), ("text", "h"), ("text", "i"), "escape",
             ("text", "o"), ("text", "k"), "enter"])
        self.assertEqual("ok", target.get_nowait())

    def test_alt_a_toggles_it_shut(self):
        _out, _i, _t = self._run([("alt", "a"), ("alt", "a"), "enter"])
        # The second Alt+A closed it, so Enter submitted an empty draft
        # instead of confirming a switch.
        self.assertEqual("", laintas_cli._pending_agent_switch)

    def test_a_lone_agent_says_so_instead_of_doing_nothing(self):
        import queue as _queue
        import threading
        from terminal_arbiter import Key

        agent_loop._agent_registry.clear()
        self._agent("primary", 0, role="primary")
        agent_loop.set_current_agent_id("primary")
        stop, interrupt = threading.Event(), threading.Event()

        class _Term:
            interactive = True

            def __init__(self):
                self._keys = [Key("alt", "a")]

            def read_key(self, timeout=0.0):
                if self._keys:
                    return self._keys.pop(0)
                stop.set()
                return None

        written = []
        with mock.patch.object(laintas_cli.terminal_arbiter, "hold",
                               return_value=_FakeHold(_Term(), stop)), \
                mock.patch.object(laintas_cli.sys.stdout, "write",
                                  side_effect=written.append), \
                mock.patch.object(laintas_cli.sys.stdout, "flush"):
            laintas_cli._bg_reader_cbreak_mode(_queue.Queue(), interrupt, stop)
        self.assertIn("Only one agent is registered", "".join(written))
