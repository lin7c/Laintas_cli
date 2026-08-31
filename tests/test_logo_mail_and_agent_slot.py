"""The L> message list (Alt+0), the clickable status slots, and agent switching.

Three features that share one surface: the startup advisories moved behind the
logo mark instead of being printed above the first prompt, the right-prompt
slots gaining mouse/touch control equivalent to Alt+N, and the agent slot
becoming a real control now that every session registers a second agent.
"""

import json
import pathlib
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import laintas_cli
import startup_mail
from prompt_toolkit.keys import Keys
from prompt_toolkit.input import DummyInput
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Point



def _config(**cfg):
    """get_runtime_config patched with the shipped slot-visibility defaults."""
    cfg.setdefault("detail", False)
    cfg.setdefault("reasoning_effort", "low")
    cfg.setdefault("rprompt_slots_detail_on",
                   "agent,mode,model,effort,terminal")
    cfg.setdefault("rprompt_slots_detail_off", "agent,mode,model,terminal")
    cfg.setdefault("rprompt_slot_order", "")
    return mock.patch.object(laintas_cli, "get_runtime_config",
                             side_effect=lambda k: cfg.get(k))


class MailStoreTests(unittest.TestCase):
    def setUp(self):
        self._saved = startup_mail.items()
        startup_mail.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        startup_mail.clear()
        for item in self._saved:
            startup_mail.post(item.key, item.title, item.body,
                              item.action, item.level)

    def test_post_appends_and_counts_unread(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        self.assertEqual([i.key for i in startup_mail.items()], ["a", "b"])
        self.assertEqual(startup_mail.unread_count(), 2)

    def test_reposting_a_key_replaces_in_place_and_reopens_it(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        startup_mail.mark_all_read()
        startup_mail.post("a", "First, updated")
        self.assertEqual([i.key for i in startup_mail.items()], ["a", "b"])
        self.assertEqual(startup_mail.items()[0].title, "First, updated")
        self.assertEqual(startup_mail.unread_count(), 1)

    def test_unknown_level_falls_back_to_info(self):
        item = startup_mail.post("a", "First", level="chartreuse")
        self.assertEqual(item.level, "info")
        self.assertIn(item.level, startup_mail.LEVEL_STYLES)

    def test_mark_text_carries_the_count_only_while_something_is_new(self):
        self.assertEqual(startup_mail.mark_text(), "L>")
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        self.assertEqual(startup_mail.mark_text(), "L> 2")
        startup_mail.mark_all_read()
        self.assertEqual(startup_mail.mark_text(), "L>")

    def test_mark_text_accepts_an_explicit_count(self):
        self.assertEqual(startup_mail.mark_text(0), "L>")
        self.assertEqual(startup_mail.mark_text(7), "L> 7")

    def test_printing_the_list_never_marks_anything_read(self):
        startup_mail.post("a", "First", "body", "/do")
        with mock.patch.object(laintas_cli.console, "print"):
            laintas_cli._print_messages()
            laintas_cli._print_messages()
        self.assertEqual(startup_mail.unread_count(), 1)

    def test_printing_an_empty_list_says_so(self):
        with mock.patch.object(laintas_cli.console, "print") as printed:
            laintas_cli._print_messages()
        self.assertTrue(any("No messages" in str(call)
                            for call in printed.call_args_list))

    def test_store_supports_reading_and_dismissing_one_message(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        self.assertEqual(startup_mail.index_of("b"), 1)
        self.assertTrue(startup_mail.mark_read("a"))
        self.assertFalse(startup_mail.mark_read("a"))
        self.assertEqual(startup_mail.unread_count(), 1)
        self.assertTrue(startup_mail.delete("a"))
        self.assertFalse(startup_mail.delete("a"))
        self.assertEqual([i.key for i in startup_mail.items()], ["b"])
        self.assertIsNone(startup_mail.get("a"))
        self.assertEqual(startup_mail.index_of("a"), -1)


class MailKeybindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = laintas_cli._build_keybindings()

    def setUp(self):
        laintas_cli._rprompt_modal_exit()
        laintas_cli._pending_prompt_default = ""
        laintas_cli._mailbox_initial_key = ""
        laintas_cli._rprompt_notice_queue.clear()
        startup_mail.clear()
        self.addCleanup(startup_mail.clear)
        self.addCleanup(laintas_cli._rprompt_modal_exit)
        self.addCleanup(setattr, laintas_cli, "_pending_prompt_default", "")

    def _find(self, *keys):
        for binding in self.kb.bindings:
            if binding.keys == tuple(keys):
                return binding
        self.fail(f"no binding for {keys!r}")

    def test_alt_zero_selects_the_mark_rather_than_opening_it(self):
        startup_mail.post("a", "First")
        self.addCleanup(startup_mail.clear)
        binding = self._find(Keys.Escape, "0")
        app = mock.Mock()
        with mock.patch.object(laintas_cli,
                               "_rprompt_slot_currently_visible",
                               return_value=True):
            binding.handler(SimpleNamespace(
                current_buffer=SimpleNamespace(text=""), app=app))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "messages")
        app.exit.assert_not_called()

    def test_alt_zero_starts_on_the_first_unread(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        startup_mail.mark_read("a")
        self.addCleanup(startup_mail.clear)
        binding = self._find(Keys.Escape, "0")
        with mock.patch.object(laintas_cli,
                               "_rprompt_slot_currently_visible",
                               return_value=True):
            binding.handler(SimpleNamespace(
                current_buffer=SimpleNamespace(text=""), app=mock.Mock()))
        self.assertEqual(laintas_cli._rprompt_modal_value, "b")

    def test_alt_zero_again_drops_the_selection(self):
        startup_mail.post("a", "First")
        self.addCleanup(startup_mail.clear)
        binding = self._find(Keys.Escape, "0")
        laintas_cli._rprompt_modal_slot = "messages"
        binding.handler(SimpleNamespace(
            current_buffer=SimpleNamespace(text=""), app=mock.Mock()))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")

    def test_alt_zero_opens_the_reader_when_the_mark_has_gone(self):
        # Everything read means the mark is off the prompt, so there is no
        # slot to select and nothing to skim — Alt+0 must still get you in.
        startup_mail.post("a", "First")
        startup_mail.mark_all_read()
        self.addCleanup(startup_mail.clear)
        binding = self._find(Keys.Escape, "0")
        app = mock.Mock()
        binding.handler(SimpleNamespace(
            current_buffer=SimpleNamespace(text="typed"), app=app))
        app.exit.assert_called_once_with(
            result=laintas_cli._MAILBOX_SENTINEL)
        self.assertEqual(laintas_cli._pending_prompt_default, "typed")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")

    def test_alt_zero_says_so_when_there_is_nothing_to_read(self):
        startup_mail.clear()
        laintas_cli._rprompt_notice_queue.clear()
        binding = self._find(Keys.Escape, "0")
        binding.handler(SimpleNamespace(
            current_buffer=SimpleNamespace(text=""), app=mock.Mock()))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        self.assertIn("No messages", laintas_cli._rprompt_notice_queue[0])

    def test_enter_on_the_mark_opens_the_reader_and_parks_the_line(self):
        binding = self._find(Keys.Enter)
        laintas_cli._rprompt_modal_slot = "messages"
        laintas_cli._rprompt_modal_value = "b"
        app = mock.Mock()
        binding.handler(SimpleNamespace(
            current_buffer=SimpleNamespace(text="half a thought"), app=app))
        app.exit.assert_called_once_with(
            result=laintas_cli._MAILBOX_SENTINEL)
        self.assertEqual(laintas_cli._mailbox_initial_key, "b")
        self.assertEqual(laintas_cli._pending_prompt_default,
                         "half a thought")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")

    def test_skimming_walks_the_message_list_both_ways(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        startup_mail.post("c", "Third")
        self.addCleanup(startup_mail.clear)
        laintas_cli._rprompt_modal_slot = "messages"
        laintas_cli._rprompt_modal_value = "a"
        laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "b")
        laintas_cli._rprompt_cycle_value(-1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "a")
        laintas_cli._rprompt_cycle_value(-1)   # wraps to the end
        self.assertEqual(laintas_cli._rprompt_modal_value, "c")

    def test_status_bar_previews_the_skimmed_message(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second headline")
        self.addCleanup(startup_mail.clear)
        laintas_cli._rprompt_modal_slot = "messages"
        laintas_cli._rprompt_modal_value = "b"
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=120):
            text = "".join(f[1]
                           for f in laintas_cli._render_bottom_toolbar())
        self.assertIn("messages 2/2", text)
        self.assertIn("Second headline", text)
        self.assertIn("Enter read", text)

    def test_pt_prompt_consumes_the_sentinel_and_reprompts(self):
        results = [laintas_cli._MAILBOX_SENTINEL, "the real input"]
        with mock.patch.object(laintas_cli, "_pt_prompt_once",
                               side_effect=results) as once, \
                mock.patch.object(laintas_cli, "_show_mailbox") as shown:
            self.assertEqual(laintas_cli.pt_prompt("/tmp"), "the real input")
        self.assertEqual(once.call_count, 2)
        shown.assert_called_once_with()


class SlotMouseTests(unittest.TestCase):
    def setUp(self):
        self._saved_cache = dict(laintas_cli._status_cache)
        laintas_cli._status_cache.update(
            agent="primary", terminal="term0", multi_agent=False,
            input_available=True, prompt_path="~", model="glm-5.2")
        laintas_cli._rprompt_modal_exit()
        laintas_cli._rprompt_notice_queue.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        laintas_cli._status_cache.clear()
        laintas_cli._status_cache.update(self._saved_cache)
        laintas_cli._rprompt_modal_exit()
        laintas_cli._rprompt_notice_queue.clear()

    @staticmethod
    def _event(event_type):
        return MouseEvent(position=Point(x=0, y=0), event_type=event_type,
                          button=None, modifiers=frozenset())

    def _handler(self, slot):
        return laintas_cli._rprompt_slot_mouse_handler(slot)

    def test_render_attaches_one_handler_per_slot_and_none_to_separators(self):
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=100), _config():
            fragments = laintas_cli._render_rprompt()
        for fragment in fragments:
            if fragment[0] == "class:rprompt-sep" or fragment[0] == "":
                self.assertEqual(len(fragment), 2, fragment)
            else:
                self.assertEqual(len(fragment), 3, fragment)
                self.assertTrue(callable(fragment[2]))

    def test_first_click_on_the_mark_selects_it(self):
        startup_mail.post("a", "First")
        self.addCleanup(startup_mail.clear)
        app = mock.Mock()
        with mock.patch.object(laintas_cli, "get_app", return_value=app), \
                mock.patch.object(laintas_cli,
                                  "_rprompt_slot_currently_visible",
                                  return_value=True):
            self._handler("messages")(self._event(MouseEventType.MOUSE_DOWN))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "messages")
        app.exit.assert_not_called()

    def test_clicking_the_selected_mark_opens_the_reader(self):
        startup_mail.post("a", "First")
        self.addCleanup(startup_mail.clear)
        laintas_cli._rprompt_modal_slot = "messages"
        laintas_cli._rprompt_modal_value = "a"
        laintas_cli._pending_prompt_default = ""
        self.addCleanup(setattr, laintas_cli, "_pending_prompt_default", "")
        app = mock.Mock()
        app.current_buffer.text = "half a thought"
        with mock.patch.object(laintas_cli, "get_app", return_value=app):
            self._handler("messages")(self._event(MouseEventType.MOUSE_DOWN))
        app.exit.assert_called_once_with(
            result=laintas_cli._MAILBOX_SENTINEL)
        self.assertEqual(laintas_cli._mailbox_initial_key, "a")
        self.assertEqual(laintas_cli._pending_prompt_default,
                         "half a thought")

    def test_wheel_over_the_mark_skims_the_messages(self):
        startup_mail.post("a", "First")
        startup_mail.post("b", "Second")
        self.addCleanup(startup_mail.clear)
        app = mock.Mock()
        with mock.patch.object(laintas_cli, "get_app", return_value=app), \
                mock.patch.object(laintas_cli,
                                  "_rprompt_slot_currently_visible",
                                  return_value=True):
            self._handler("messages")(self._event(MouseEventType.SCROLL_UP))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "messages")
        app.exit.assert_not_called()

    def test_press_selects_the_slot_like_alt_n(self):
        with mock.patch.object(laintas_cli, "get_app"), \
                mock.patch.object(laintas_cli,
                                  "_rprompt_slot_currently_visible",
                                  return_value=True):
            self._handler("mode")(self._event(MouseEventType.MOUSE_DOWN))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "mode")

    def test_pressing_the_selected_slot_again_drops_the_selection(self):
        laintas_cli._rprompt_modal_slot = "mode"
        with mock.patch.object(laintas_cli, "get_app"):
            self._handler("mode")(self._event(MouseEventType.MOUSE_DOWN))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")

    def test_wheel_selects_then_cycles_in_both_directions(self):
        with mock.patch.object(laintas_cli, "get_app"), \
                mock.patch.object(laintas_cli,
                                  "_rprompt_slot_currently_visible",
                                  return_value=True), \
                mock.patch.object(laintas_cli, "_rprompt_cycle_value") as cycle:
            self._handler("mode")(self._event(MouseEventType.SCROLL_UP))
            self._handler("mode")(self._event(MouseEventType.SCROLL_DOWN))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "mode")
        self.assertEqual([call[0][0] for call in cycle.call_args_list], [1, -1])

    def test_wheel_on_a_hidden_slot_cycles_nothing(self):
        with mock.patch.object(laintas_cli, "get_app"), \
                mock.patch.object(laintas_cli,
                                  "_rprompt_slot_currently_visible",
                                  return_value=False), \
                mock.patch.object(laintas_cli, "_rprompt_cycle_value") as cycle:
            self._handler("effort")(self._event(MouseEventType.SCROLL_UP))
        cycle.assert_not_called()
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")

    def test_release_is_swallowed_rather_than_moving_the_cursor(self):
        laintas_cli._rprompt_modal_slot = "mode"
        with mock.patch.object(laintas_cli, "get_app"):
            result = self._handler("mode")(
                self._event(MouseEventType.MOUSE_UP))
        self.assertIsNone(result)
        self.assertEqual(laintas_cli._rprompt_modal_slot, "mode")


class MailboxBrowserTests(unittest.TestCase):
    """The reader behind the mark: read one, dismiss one, clear the lot."""

    def setUp(self):
        startup_mail.clear()
        self.addCleanup(startup_mail.clear)
        startup_mail.post("a", "First message", "body one", "/do-a", "warn")
        startup_mail.post("b", "Second message", "body two")
        # Dummy io on purpose: a prompt_toolkit Application binds its input
        # when it is constructed, so a headless build would grab fd 0 and
        # unsettle every other test in this process that reads a terminal.
        self.browser = laintas_cli._build_mailbox_browser(
            input=DummyInput(), output=DummyOutput())

    def _action(self, name):
        for action in self.browser.actions:
            if action.name == name:
                return action
        self.fail(f"no {name} action")

    def _item(self, key):
        for item in self.browser.load_items():
            if item.key == key:
                return item
        self.fail(f"no item {key}")

    def test_list_carries_position_unread_state_and_level(self):
        items = self.browser.load_items()
        self.assertEqual([i.key for i in items], ["a", "b"])
        self.assertEqual([i.ordinal for i in items], ["1", "2"])
        self.assertEqual([i.status for i in items], ["new", "new"])
        self.assertEqual(items[0].badge, "warn")
        self.assertEqual(items[1].badge, "")

    def test_opening_a_message_is_what_reads_it(self):
        self.assertEqual(startup_mail.unread_count(), 2)
        detail = self.browser.load_detail(self._item("a"))
        self.assertEqual(detail.title, "First message")
        self.assertIn("body one", [line.text for line in detail.lines])
        self.assertIn("/do-a", [line.text for line in detail.lines])
        self.assertEqual(startup_mail.unread_count(), 1)
        self.assertEqual(self._item("a").status, "read")

    def test_detail_of_a_message_deleted_underneath_does_not_explode(self):
        item = self._item("a")
        startup_mail.delete("a")
        detail = self.browser.load_detail(item)
        self.assertIn("This message is no longer there.",
                      [line.text for line in detail.lines])

    def test_dismiss_asks_twice_like_the_memory_browser_does(self):
        first = self._action("dismiss").handler(self._item("a"))
        self.assertEqual(first.message_style, "class:warning")
        self.assertEqual(len(startup_mail.items()), 2)
        second = self._action("dismiss").handler(self._item("a"))
        self.assertTrue(second.refresh)
        self.assertEqual([i.key for i in self.browser.load_items()], ["b"])

    def test_arming_dismiss_on_one_row_does_not_arm_another(self):
        self._action("dismiss").handler(self._item("a"))
        other = self._action("dismiss").handler(self._item("b"))
        self.assertEqual(other.message_style, "class:warning")
        self.assertEqual(len(startup_mail.items()), 2)

    def test_enter_opens_the_detail_pane_instead_of_closing_the_reader(self):
        # The bug this pins: primary_action="read" with no action of that
        # name makes ResourceBrowser._primary fall through to app.exit(), so
        # Enter shut the whole reader the moment it opened. The default
        # "view" — what /memory and every other browser here uses — opens the
        # detail pane. app.exit() on an app that is not running raises, so
        # this would fail loudly rather than silently.
        self.assertEqual(self.browser.primary_action, "view")
        self.browser.reload(preserve=False)
        self.browser._primary()
        self.assertEqual(self.browser.mode, "detail")
        self.assertFalse(self.browser.app.is_done)

    def test_dismiss_reports_instead_of_failing_on_a_stale_row(self):
        item = self._item("a")
        startup_mail.delete("a")
        result = self._action("dismiss").handler(item)
        self.assertFalse(result.refresh)
        self.assertEqual(result.message_style, "class:warning")

    def test_dismiss_with_no_selection_is_a_warning_not_a_crash(self):
        result = self._action("dismiss").handler(None)
        self.assertEqual(result.message_style, "class:warning")
        self.assertEqual(len(startup_mail.items()), 2)

    def test_mark_all_read_clears_the_count_but_keeps_the_messages(self):
        result = self._action("mark-all").handler(None)
        self.assertTrue(result.refresh)
        self.assertEqual(startup_mail.unread_count(), 0)
        self.assertEqual(len(startup_mail.items()), 2)
        again = self._action("mark-all").handler(None)
        self.assertIn("Nothing was unread", again.message)

    def test_clear_all_asks_twice_before_throwing_everything_away(self):
        first = self._action("clear").handler(None)
        self.assertEqual(first.message_style, "class:warning")
        self.assertEqual(len(startup_mail.items()), 2)
        second = self._action("clear").handler(None)
        self.assertTrue(second.refresh)
        self.assertEqual(startup_mail.items(), [])

    def test_clear_disarms_after_the_confirmation_window(self):
        self._action("clear").handler(None)
        with mock.patch.object(laintas_cli.time, "monotonic",
                               return_value=__import__("time").monotonic() + 60):
            again = self._action("clear").handler(None)
        self.assertEqual(again.message_style, "class:warning")
        self.assertEqual(len(startup_mail.items()), 2)

    def test_show_mailbox_falls_back_to_the_printout_when_the_browser_dies(self):
        with mock.patch.object(laintas_cli, "_build_mailbox_browser",
                               side_effect=RuntimeError("no tty")), \
                mock.patch.object(laintas_cli, "_print_messages") as plain, \
                mock.patch.object(laintas_cli.console, "print"):
            laintas_cli._show_mailbox()
        plain.assert_called_once_with()

    def test_a_widget_can_never_end_the_session(self):
        # SystemExit is not an Exception; letting it out of a full-screen app
        # would exit the CLI, which from the outside looks like a crash.
        with mock.patch.object(laintas_cli, "_build_mailbox_browser",
                               side_effect=SystemExit(1)), \
                mock.patch.object(laintas_cli, "_print_messages") as plain, \
                mock.patch.object(laintas_cli.console, "print"):
            laintas_cli._show_mailbox()
        plain.assert_called_once_with()

    def test_ctrl_c_out_of_the_reader_is_not_an_error(self):
        with mock.patch.object(laintas_cli, "_build_mailbox_browser",
                               side_effect=KeyboardInterrupt), \
                mock.patch.object(laintas_cli, "_print_messages") as plain, \
                mock.patch.object(laintas_cli.console, "print") as printed:
            laintas_cli._show_mailbox()
        plain.assert_not_called()
        printed.assert_not_called()


class MessagesCommandTests(unittest.TestCase):
    """/messages — the reader's operations from the keyboard.

    These exist for the places a full-screen reader cannot run: a
    sub-terminal, a piped session, --execute.
    """

    def setUp(self):
        startup_mail.clear()
        self.addCleanup(startup_mail.clear)
        startup_mail.post("tips", "How input is routed", "body one", "/help")
        startup_mail.post("upd", "Update available", "body two", "/v update",
                          "good")

    def _run(self, cmd):
        with mock.patch.object(laintas_cli.console, "print") as printed:
            laintas_cli.handle_meta_command(cmd, None, {}, None)
        return "\n".join(str(call.args[0]) if call.args else ""
                          for call in printed.call_args_list)

    def test_list_shows_everything_and_reads_nothing(self):
        out = self._run("/messages list")
        self.assertIn("How input is routed", out)
        self.assertIn("Update available", out)
        self.assertIn("2 new", out)
        self.assertEqual(startup_mail.unread_count(), 2)

    def test_list_on_an_empty_inbox_says_so(self):
        startup_mail.clear()
        self.assertIn("No messages", self._run("/messages list"))

    def test_read_by_position_marks_that_one_read(self):
        out = self._run("/messages read 2")
        self.assertIn("Update available", out)
        self.assertIn("body two", out)
        self.assertTrue(startup_mail.get("upd").read)
        self.assertFalse(startup_mail.get("tips").read)

    def test_read_by_key_works_too(self):
        self._run("/messages read tips")
        self.assertTrue(startup_mail.get("tips").read)

    def test_read_rejects_a_position_that_is_not_there(self):
        self.assertIn("No message 99", self._run("/messages read 99"))
        self.assertEqual(startup_mail.unread_count(), 2)

    def test_read_without_an_argument_prints_usage(self):
        self.assertIn("Usage: /messages read", self._run("/messages read"))

    def test_seen_marks_them_all(self):
        self.assertIn("Marked 2", self._run("/messages seen"))
        self.assertEqual(startup_mail.unread_count(), 0)
        self.assertIn("Nothing was unread", self._run("/messages seen"))

    def test_dismiss_removes_one(self):
        self._run("/messages dismiss 1")
        self.assertEqual([i.key for i in startup_mail.items()], ["upd"])

    def test_dismiss_reports_a_reference_that_matches_nothing(self):
        self.assertIn("No message nope", self._run("/messages dismiss nope"))
        self.assertEqual(len(startup_mail.items()), 2)

    def test_clear_removes_everything_and_says_how_many(self):
        self.assertIn("Cleared 2", self._run("/messages clear"))
        self.assertEqual(startup_mail.items(), [])
        self.assertIn("No messages", self._run("/messages clear"))

    def test_the_short_alias_reaches_the_same_command(self):
        self.assertIn("How input is routed", self._run("/msg list"))

    def test_there_is_exactly_one_long_name_for_this_command(self):
        # /message and /messages differing by one letter is two names for one
        # thing; only the plural exists, matching /agents, /tools, /skills.
        spec = laintas_cli._find_command_spec("/messages")
        self.assertEqual(spec.all_names, ("/messages", "/msg"))
        self.assertIsNone(laintas_cli._find_command_spec("/message"))

    def test_an_unknown_subcommand_prints_the_whole_usage(self):
        out = self._run("/messages bogus")
        # Rich eats a bare bracket as a style tag; the usage must survive it.
        self.assertIn("[list|read <n>|seen|dismiss <n>|clear]", out)

    def test_too_many_arguments_are_rejected_per_subcommand(self):
        self.assertIn("Usage: /messages list",
                      self._run("/messages list extra"))

    def test_no_argument_opens_the_reader_on_a_real_terminal(self):
        with mock.patch.object(laintas_cli, "_mailbox_can_browse",
                               return_value=True), \
                mock.patch.object(laintas_cli, "_show_mailbox") as shown:
            laintas_cli.handle_meta_command("/messages", None, {}, None)
        shown.assert_called_once_with()

    def test_no_argument_falls_back_to_the_list_without_a_terminal(self):
        with mock.patch.object(laintas_cli, "_mailbox_can_browse",
                               return_value=False), \
                mock.patch.object(laintas_cli, "_show_mailbox") as shown:
            out = self._run("/messages")
        shown.assert_not_called()
        self.assertIn("How input is routed", out)
        # A fallback must not consume what it could not let the user open.
        self.assertEqual(startup_mail.unread_count(), 2)

    def test_the_command_is_in_the_palette_and_help(self):
        spec = laintas_cli._find_command_spec("/msg")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "/messages")
        self.assertIn("/messages", [name for name, _desc
                                    in laintas_cli._COMMANDS])


class SlashUsageEscapingTests(unittest.TestCase):
    """A usage string is text, not Rich markup."""

    def test_optional_argument_groups_survive_printing(self):
        with mock.patch.object(laintas_cli.console, "print") as printed:
            laintas_cli.handle_meta_command(
                "/training bogus extra", None, {}, None)
        out = "".join(str(call.args[0]) if call.args else ""
                      for call in printed.call_args_list)
        self.assertIn("[status|on|off]", out)


class AgentSlotSwitchTests(unittest.TestCase):
    """Committing the agent slot goes through /agent, not a direct pointer write.

    Switching has to rebind the REPL's state/history objects; only the
    command path does that, so the slot submits it rather than reimplementing
    it inside a keybinding handler.
    """

    @classmethod
    def setUpClass(cls):
        cls.kb = laintas_cli._build_keybindings()

    def setUp(self):
        laintas_cli._rprompt_modal_exit()
        laintas_cli._pending_prompt_default = ""
        self.addCleanup(laintas_cli._rprompt_modal_exit)
        self.addCleanup(setattr, laintas_cli, "_pending_prompt_default", "")

    def _find(self, *keys):
        for binding in self.kb.bindings:
            if binding.keys == tuple(keys):
                return binding
        self.fail(f"no binding for {keys!r}")

    def test_commit_emits_the_agent_command(self):
        laintas_cli._rprompt_modal_slot = "agent"
        laintas_cli._rprompt_modal_value = "scout"
        laintas_cli._rprompt_modal_order = list(laintas_cli._RPROMPT_SLOT_IDS)
        laintas_cli._rprompt_modal_original_order = tuple(
            laintas_cli._RPROMPT_SLOT_IDS)
        with mock.patch.object(laintas_cli, "get_current_agent",
                               return_value=SimpleNamespace(id="primary")):
            ok, _notice, submit = laintas_cli._rprompt_commit()
        self.assertTrue(ok)
        self.assertEqual(submit, "/agent scout")

    def test_commit_is_a_no_op_when_the_draft_is_the_current_agent(self):
        laintas_cli._rprompt_modal_slot = "agent"
        laintas_cli._rprompt_modal_value = "primary"
        laintas_cli._rprompt_modal_order = list(laintas_cli._RPROMPT_SLOT_IDS)
        laintas_cli._rprompt_modal_original_order = tuple(
            laintas_cli._RPROMPT_SLOT_IDS)
        with mock.patch.object(laintas_cli, "get_current_agent",
                               return_value=SimpleNamespace(id="primary")):
            ok, _notice, submit = laintas_cli._rprompt_commit()
        self.assertTrue(ok)
        self.assertEqual(submit, "")

    def test_enter_submits_the_command_and_parks_the_typed_line(self):
        binding = self._find(Keys.Enter)
        buf = mock.Mock()
        buf.text = "an unfinished message"
        laintas_cli._rprompt_modal_slot = "agent"
        with mock.patch.object(laintas_cli, "_rprompt_commit",
                               return_value=(True, "", "/agent scout")):
            binding.handler(SimpleNamespace(
                current_buffer=buf, app=mock.Mock()))
        self.assertEqual(buf.text, "/agent scout")
        buf.validate_and_handle.assert_called_once_with()
        self.assertEqual(laintas_cli._pending_prompt_default,
                         "an unfinished message")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")

    def test_slot_preview_shows_the_draft_agent(self):
        laintas_cli._status_cache.update(
            agent="primary", terminal="term0", multi_agent=False,
            input_available=True, prompt_path="~", model="glm-5.2")
        laintas_cli._rprompt_modal_slot = "agent"
        laintas_cli._rprompt_modal_value = "scout"
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=100), \
                mock.patch.object(laintas_cli, "get_agent",
                                  return_value=SimpleNamespace(
                                      id="scout", name="scout")), \
                _config():
            items = laintas_cli._rprompt_slot_items()
        agent_slot = next(row for row in items if row[0] == "agent")
        self.assertEqual(agent_slot[2], "scout")
        self.assertEqual(agent_slot[1], "class:rprompt-slot-selected")


if __name__ == "__main__":
    unittest.main()


class ReadReceiptTests(unittest.TestCase):
    """Read state has to outlive the process.

    Every start re-posts the same standing advisories, so a purely in-memory
    "read" flag meant the mark came back with the same count on the next
    launch and reading it never accomplished anything.
    """

    def setUp(self):
        self._saved = startup_mail.items()
        self.store = pathlib.Path(
            tempfile.mkdtemp(prefix="laintas-mail-")) / "messages_read.json"
        self.addCleanup(shutil.rmtree, str(self.store.parent),
                        ignore_errors=True)
        self.addCleanup(self._restore)

    def _restore(self):
        startup_mail.disable_persistence()
        startup_mail.clear()
        for item in self._saved:
            startup_mail.post(item.key, item.title, item.body,
                              item.action, item.level)

    def _restart(self, tips_body="PATH commands run directly",
                 resume_digest="cwd:1000:3", resume_age="10 hour(s) ago"):
        """Simulate a fresh process posting the same standing advisories."""
        startup_mail.disable_persistence()
        startup_mail.clear()
        startup_mail.enable_persistence(self.store)
        startup_mail.post("tips", "How input is routed", tips_body)
        startup_mail.post("training", "Training-data sharing is off", "x")
        startup_mail.post("resume", "A previous session can be resumed",
                          f"3 turn(s), {resume_age}.", digest=resume_digest)

    def test_reading_survives_a_restart(self):
        self._restart()
        self.assertEqual(startup_mail.unread_count(), 3)
        startup_mail.mark_all_read()
        self.assertEqual(startup_mail.mark_text(), "L>")

        self._restart()
        self.assertEqual(startup_mail.unread_count(), 0)
        self.assertEqual(startup_mail.mark_text(), "L>")
        self.assertEqual(startup_mail.count(), 3,
                         "the notices are still readable with /messages")

    def test_an_aging_body_is_not_new_information(self):
        self._restart()
        startup_mail.mark_all_read()
        self._restart(resume_age="11 hour(s) ago")
        self.assertEqual(startup_mail.unread_count(), 0)

    def test_a_different_checkpoint_comes_back_unread(self):
        self._restart()
        startup_mail.mark_all_read()
        self._restart(resume_digest="cwd:2000:7")
        self.assertEqual([i.key for i in startup_mail.unread()], ["resume"])

    def test_changed_content_comes_back_unread(self):
        self._restart()
        startup_mail.mark_all_read()
        self._restart(tips_body="the routing rules changed")
        self.assertEqual([i.key for i in startup_mail.unread()], ["tips"])

    def test_dismissing_and_clearing_also_file_a_receipt(self):
        self._restart()
        startup_mail.delete("training")
        startup_mail.mark_read("tips")
        self._restart()
        self.assertEqual([i.key for i in startup_mail.unread()], ["resume"])

        startup_mail.clear(remember=True)
        self._restart()
        self.assertEqual(startup_mail.unread_count(), 0)

    def test_persistence_is_off_until_asked_for(self):
        startup_mail.disable_persistence()
        startup_mail.clear()
        startup_mail.post("a", "First")
        startup_mail.mark_all_read()
        startup_mail.delete("a")
        self.assertFalse(self.store.exists())

    def test_a_corrupt_receipt_file_just_means_nothing_is_read(self):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text("{not json", encoding="utf-8")
        self._restart()
        self.assertEqual(startup_mail.unread_count(), 3)

    def test_expired_receipts_are_dropped(self):
        self._restart()
        startup_mail.mark_all_read()
        stale = json.loads(self.store.read_text(encoding="utf-8"))
        for entry in stale["read"].values():
            entry["ts"] = time.time() - (startup_mail._RECEIPT_MAX_AGE + 60)
        self.store.write_text(json.dumps(stale), encoding="utf-8")
        self._restart()
        self.assertEqual(startup_mail.unread_count(), 3)

    def test_enabling_late_still_quiets_the_mark(self):
        self._restart()
        startup_mail.mark_all_read()

        startup_mail.disable_persistence()
        startup_mail.clear()
        startup_mail.post("tips", "How input is routed",
                          "PATH commands run directly")
        self.assertTrue(startup_mail.has_unread())
        startup_mail.enable_persistence(self.store)
        self.assertFalse(startup_mail.has_unread())

    def test_the_receipt_file_is_private(self):
        self._restart()
        startup_mail.mark_all_read()
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
