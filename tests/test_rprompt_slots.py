"""Right-prompt slot selection (Alt+1..9): visibility, cycling, ordering.

Covers the slot model _render_rprompt was refactored onto, the modal
keybindings _build_keybindings registers, and a byte-level regression
against the pre-refactor rprompt render (tests/fixtures JSON snapshot).
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import laintas_cli
import symbols
from prompt_toolkit.keys import Keys

_FIXTURE = Path(__file__).parent / "fixtures" / "rprompt_legacy_render.json"


def _text(fragments):
    return "".join(fragment[1] for fragment in fragments)


def _styled(fragments):
    """(style, text) pairs — drops the per-slot mouse handler third element."""
    return [(fragment[0], fragment[1]) for fragment in fragments]


def _join(pairs):
    return "".join(text for _style, text in pairs)


def _without_mark(fragments):
    """Drop the leading L> mark and the separator that follows it.

    The mark is a later addition to the row; everything to its right is what
    the legacy snapshot pins, and it must still match byte for byte.
    """
    pairs = _styled(fragments)
    head = 0
    while head < len(pairs) and pairs[head][0].startswith("class:rprompt-logo"):
        head += 1
    if head and head < len(pairs) and pairs[head][0] == "class:rprompt-sep":
        head += 1
    return pairs[head:]


class _SlotTestBase(unittest.TestCase):
    """Shared harness: isolated status cache, config mocking, state restore."""

    def setUp(self):
        self._saved_cache = dict(laintas_cli._status_cache)
        laintas_cli._status_cache.update(
            agent="primary", terminal="term0", multi_agent=False,
            input_available=True, prompt_path="~", model="glm-5.2")
        laintas_cli._rprompt_modal_exit()
        laintas_cli._rprompt_notice_queue.clear()
        self._saved_model = (
            list(laintas_cli._rprompt_model_cache),
            laintas_cli._rprompt_model_fetch_state,
            laintas_cli._rprompt_model_cache_key,
            laintas_cli._rprompt_model_fetch_error,
        )
        for cleanup in (self._restore_all,):
            self.addCleanup(cleanup)

    def _restore_all(self):
        laintas_cli._status_cache.clear()
        laintas_cli._status_cache.update(self._saved_cache)
        laintas_cli._rprompt_modal_exit()
        laintas_cli._rprompt_notice_queue.clear()
        cache, state, cache_key, error = self._saved_model
        laintas_cli._rprompt_model_cache = list(cache)
        laintas_cli._rprompt_model_fetch_state = state
        laintas_cli._rprompt_model_cache_key = cache_key
        laintas_cli._rprompt_model_fetch_error = error

    def _config(self, **cfg):
        """Patch get_runtime_config with defaults for keys cfg omits."""
        cfg.setdefault("detail", False)
        cfg.setdefault("reasoning_effort", "low")
        cfg.setdefault("rprompt_slots_detail_on",
                       "messages,agent,mode,model,effort,terminal")
        cfg.setdefault("rprompt_slots_detail_off",
                       "messages,agent,mode,model,terminal")
        cfg.setdefault("rprompt_slot_order", "")
        return mock.patch.object(laintas_cli, "get_runtime_config",
                                 side_effect=lambda k: cfg.get(k))

    def _render(self, width=100, **cfg):
        with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                  return_value={"name": "act"}), \
                mock.patch.object(laintas_cli.mode_manager,
                                  "is_read_only_mode", return_value=False), \
                mock.patch.object(laintas_cli, "_terminal_width",
                                  return_value=width), \
                self._config(**cfg):
            return laintas_cli._render_rprompt()


class SlotModelTests(_SlotTestBase):
    def test_parse_slot_list_drops_unknown_and_duplicates(self):
        self.assertEqual(
            laintas_cli._rprompt_parse_slot_list("mode, mode ,bogus,agent,mode"),
            ("mode", "agent"))

    def test_configured_slots_defaults_match_legacy_layout(self):
        with self._config():
            self.assertEqual(laintas_cli._rprompt_configured_slots(True),
                             ("messages", "agent", "mode", "model", "effort",
                              "terminal"))
            self.assertEqual(laintas_cli._rprompt_configured_slots(False),
                             ("messages", "agent", "mode", "model", "terminal"))

    def test_configured_slots_empty_hides_the_row(self):
        with self._config(rprompt_slots_detail_off="", detail=False):
            self.assertEqual(laintas_cli._rprompt_configured_slots(False), ())

    def test_effective_order_default_and_custom(self):
        with self._config():
            self.assertEqual(laintas_cli._rprompt_effective_order(),
                             ["messages", "agent", "mode", "model", "effort",
                              "terminal"])
        with self._config(rprompt_slot_order="model,mode"):
            self.assertEqual(laintas_cli._rprompt_effective_order(),
                             ["messages", "model", "mode", "agent", "effort",
                              "terminal"])

    def test_effective_order_drops_unknown_ids(self):
        with self._config(rprompt_slot_order="bogus,mode,bogus"):
            self.assertEqual(laintas_cli._rprompt_effective_order(),
                             ["messages", "mode", "agent", "model", "effort",
                              "terminal"])

    def test_messages_mark_is_pinned_leftmost_whatever_the_saved_order(self):
        # A saved order that tries to bury the mark must not be honoured:
        # the logo is not a column the layout may shuffle.
        with self._config(rprompt_slot_order="mode,messages,agent"):
            self.assertEqual(laintas_cli._rprompt_effective_order()[0],
                             "messages")

    def test_slot_fits_width_breakpoints(self):
        f = laintas_cli._rprompt_slot_fits
        self.assertTrue(f("mode", 20, False))
        self.assertFalse(f("agent", 61, False))
        self.assertTrue(f("agent", 62, False))
        self.assertFalse(f("model", 77, False))
        self.assertTrue(f("model", 78, False))
        self.assertFalse(f("effort", 77, False))
        self.assertTrue(f("effort", 78, False))
        self.assertFalse(f("terminal", 107, False))
        self.assertTrue(f("terminal", 108, False))
        self.assertFalse(f("terminal", 200, True))   # multi-agent hides it

    def test_visible_slots_detail_off_excludes_effort(self):
        with self._config(detail=False):
            vis = laintas_cli._rprompt_visible_slots(120, False, False)
        self.assertEqual(vis, ["messages", "agent", "mode", "model", "terminal"])

    def test_visible_slots_detail_on_includes_effort(self):
        with self._config(detail=True):
            vis = laintas_cli._rprompt_visible_slots(120, True, False)
        self.assertEqual(vis, ["messages", "agent", "mode", "model", "effort",
                               "terminal"])

    def test_visible_slots_respect_width(self):
        with self._config():
            # The mark has no breakpoint: it is two columns and it leads.
            self.assertEqual(laintas_cli._rprompt_visible_slots(50, False, False),
                             ["messages", "mode"])
            self.assertEqual(laintas_cli._rprompt_visible_slots(70, False, False),
                             ["messages", "agent", "mode"])

    def test_visible_slots_respect_custom_visibility(self):
        with self._config(rprompt_slots_detail_off="mode", detail=False):
            self.assertEqual(laintas_cli._rprompt_visible_slots(120, False, False),
                             ["mode"])

    def test_visible_slots_respect_custom_order(self):
        with self._config(rprompt_slot_order="mode,agent"):
            vis = laintas_cli._rprompt_visible_slots(120, False, False)
        self.assertEqual(vis, ["messages", "mode", "agent", "model", "terminal"])


class SlotSelectionTests(_SlotTestBase):
    def test_select_visible_slot_enters_modal(self):
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=100), self._config(detail=False):
            laintas_cli._rprompt_select_slot("mode")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "mode")

    def test_select_hidden_slot_queues_notice_and_stays_out(self):
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=50), self._config(detail=False):
            # model needs width >= 78; terminal is hidden by detail-off list
            laintas_cli._rprompt_select_slot("model")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        self.assertEqual(len(laintas_cli._rprompt_notice_queue), 1)
        self.assertIn("hidden", laintas_cli._rprompt_notice_queue[0])

    def test_select_unknown_slot_is_ignored(self):
        laintas_cli._rprompt_select_slot("bogus")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        self.assertEqual(laintas_cli._rprompt_notice_queue, [])

    def test_long_path_cannot_select_a_slot_trimmed_by_final_fit(self):
        laintas_cli._status_cache["prompt_path"] = "~/" + "d" * 58
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=80), self._config(detail=False):
            visible = laintas_cli._rprompt_final_visible_slot_ids()
            self.assertNotIn("model", visible)
            laintas_cli._rprompt_select_slot("model")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")


class CycleValueTests(_SlotTestBase):
    # Each test selects the slot it exercises; _rprompt_cycle_value
    # dispatches on the selected slot, so a shared default would misroute.
    def _select(self, slot):
        laintas_cli._rprompt_modal_slot = slot

    def test_cycle_mode_order_and_wraparound(self):
        self._select("mode")
        laintas_cli._rprompt_modal_value = "act"
        modes = [{"name": "act"}, {"name": "review"}, {"name": "study"}]
        with mock.patch.object(laintas_cli.mode_manager, "list_modes",
                               return_value=modes):
            laintas_cli._rprompt_cycle_value(1)
            self.assertEqual(laintas_cli._rprompt_modal_value, "act-always")
            laintas_cli._rprompt_cycle_value(1)
            self.assertEqual(laintas_cli._rprompt_modal_value, "review")
            laintas_cli._rprompt_modal_value = "study"
            laintas_cli._rprompt_cycle_value(1)
            self.assertEqual(laintas_cli._rprompt_modal_value, "plan")

    def test_cycle_mode_is_preview_only(self):
        self._select("mode")
        laintas_cli._rprompt_modal_value = "act"
        with mock.patch.object(laintas_cli.mode_manager, "list_modes",
                              return_value=[{"name": "act"}, {"name": "review"}]), \
                mock.patch.object(laintas_cli.mode_manager, "activate",
                                  return_value=(True, "ok")) as activate:
            laintas_cli._rprompt_cycle_value(1)
        activate.assert_not_called()

    def test_cycle_effort_sets_and_persists(self):
        self._select("effort")
        calls = []
        with mock.patch("agent_loop.get_runtime_config",
                        return_value="low"), \
                mock.patch("agent_loop.set_runtime_config",
                           side_effect=lambda k, v: calls.append((k, v))
                           or True), \
                mock.patch.object(laintas_cli.terminal_preferences,
                                  "set_ui_preference") as persist:
            laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "medium")
        self.assertEqual(calls, [])
        persist.assert_not_called()

    def test_cycle_effort_wraps_max_to_none(self):
        self._select("effort")
        calls = []
        with mock.patch("agent_loop.get_runtime_config",
                        return_value="max"), \
                mock.patch("agent_loop.set_runtime_config",
                           side_effect=lambda k, v: calls.append((k, v))
                           or True), \
                mock.patch.object(laintas_cli.terminal_preferences,
                                  "set_ui_preference"):
            laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "none")
        self.assertEqual(calls, [])

    def test_cycle_model_with_cache(self):
        self._select("model")
        laintas_cli._rprompt_model_cache = ["m1", "m2"]
        laintas_cli._rprompt_model_cache_key = laintas_cli._rprompt_backend_cache_key()
        laintas_cli._rprompt_modal_value = "m1"
        with mock.patch.object(laintas_cli, "get_selected_model",
                               return_value="m1"):
            laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "m2")

    def test_cycle_model_wraps_to_auto(self):
        self._select("model")
        laintas_cli._rprompt_model_cache = ["m1"]
        laintas_cli._rprompt_model_cache_key = laintas_cli._rprompt_backend_cache_key()
        laintas_cli._rprompt_modal_value = "m1"
        with mock.patch.object(laintas_cli, "get_selected_model",
                               return_value="m1"):
            laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "auto")

    def test_cycle_model_empty_cache_kicks_fetch(self):
        self._select("model")
        laintas_cli._rprompt_model_cache = []
        with mock.patch.object(laintas_cli, "_rprompt_kick_model_fetch") as kick, \
                mock.patch.object(laintas_cli, "get_selected_model",
                                  return_value=""):
            laintas_cli._rprompt_cycle_value(1)
        kick.assert_called_once()
        self.assertEqual(laintas_cli._rprompt_notice_queue, [])

    def test_cycle_agent_slot_steps_through_the_registry(self):
        laintas_cli._rprompt_modal_slot = "agent"
        laintas_cli._rprompt_modal_value = "primary"
        with mock.patch.object(
                laintas_cli, "get_all_agents",
                return_value=[SimpleNamespace(id="primary", role="primary"),
                              SimpleNamespace(id="scout", role="pool")]):
            laintas_cli._rprompt_cycle_value(1)
            self.assertEqual(laintas_cli._rprompt_modal_value, "scout")
            laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "primary")
        self.assertEqual(laintas_cli._rprompt_notice_queue, [])

    def test_cycle_agent_slot_says_so_when_there_is_nowhere_to_go(self):
        laintas_cli._rprompt_modal_slot = "agent"
        laintas_cli._rprompt_modal_value = "primary"
        with mock.patch.object(
                laintas_cli, "get_all_agents",
                return_value=[SimpleNamespace(id="primary", role="primary")]):
            laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_value, "primary")
        self.assertIn("Only one agent", laintas_cli._rprompt_notice_queue[0])

    def test_cycle_terminal_slot_is_a_redirect_notice(self):
        laintas_cli._rprompt_modal_slot = "terminal"
        laintas_cli._rprompt_cycle_value(1)
        self.assertEqual(laintas_cli._rprompt_modal_slot, "terminal")
        self.assertIn("/station", laintas_cli._rprompt_notice_queue[0])

    def test_refill_model_cache_dedups_and_marks_ready(self):
        laintas_cli._rprompt_model_cache = []
        laintas_cli._rprompt_model_fetch_state = "pending"
        laintas_cli._rprompt_refill_model_cache(
            [{"id": "m1"}, {"id": "m1"}, {"id": "m2"}, {}])
        self.assertEqual(laintas_cli._rprompt_model_cache, ["m1", "m2"])
        self.assertEqual(laintas_cli._rprompt_model_fetch_state, "ready")

    def test_refill_model_cache_ignores_empty_list(self):
        laintas_cli._rprompt_model_cache = ["m1"]
        laintas_cli._rprompt_model_fetch_state = "ready"
        laintas_cli._rprompt_refill_model_cache([])
        self.assertEqual(laintas_cli._rprompt_model_cache, ["m1"])

    def test_kick_pending_state_starts_no_second_thread(self):
        started = []
        real_thread = laintas_cli.threading.Thread

        def fake_thread(*a, **kw):
            started.append(kw.get("name"))
            thread = real_thread(*a, **kw)
            thread.start = lambda: None      # worker must not run
            return thread

        laintas_cli._rprompt_model_fetch_state = "pending"
        with mock.patch.object(laintas_cli.threading, "Thread",
                               side_effect=fake_thread):
            laintas_cli._rprompt_kick_model_fetch()
        self.assertEqual(started, [])

    def test_kick_model_fetch_failure_marks_failed_and_notifies(self):
        import time
        with mock.patch.object(laintas_cli, "load_session",
                              return_value={}), \
                mock.patch.object(laintas_cli, "fetch_available_models",
                                  side_effect=RuntimeError("net down")):
            laintas_cli._rprompt_model_fetch_state = ""
            laintas_cli._rprompt_kick_model_fetch()
            for _ in range(100):
                if laintas_cli._rprompt_model_fetch_state == "failed":
                    break
                time.sleep(0.02)
        self.assertEqual(laintas_cli._rprompt_model_fetch_state, "failed")
        self.assertIn("net down", laintas_cli._rprompt_model_fetch_error)


class MoveSlotTests(_SlotTestBase):
    def test_move_slot_swaps_order_and_persists(self):
        laintas_cli._rprompt_modal_slot = "mode"
        laintas_cli._rprompt_modal_order = list(laintas_cli._RPROMPT_SLOT_IDS)
        laintas_cli._rprompt_modal_original_order = tuple(
            laintas_cli._rprompt_modal_order)
        calls, persists = [], []
        with mock.patch.object(laintas_cli, "set_runtime_config",
                               side_effect=lambda k, v: calls.append((k, v))
                               or True), \
                mock.patch.object(laintas_cli.terminal_preferences,
                                  "set_ui_preference",
                                  side_effect=lambda k, v:
                                      persists.append((k, v))):
            laintas_cli._rprompt_move_slot(-1)   # agent <-> mode
        self.assertEqual(calls, [])
        self.assertEqual(persists, [])
        # The pinned mark keeps index 0; the setting slots swap behind it.
        self.assertEqual(laintas_cli._rprompt_modal_order[:3],
                         ["messages", "mode", "agent"])

    def test_move_slot_at_boundary_is_a_noop(self):
        laintas_cli._rprompt_modal_slot = "agent"   # first slot
        with mock.patch.object(laintas_cli, "set_runtime_config") as setter:
            laintas_cli._rprompt_move_slot(-1)
        setter.assert_not_called()
        laintas_cli._rprompt_modal_slot = "terminal"   # last visible slot
        laintas_cli._rprompt_move_slot(1)
        setter.assert_not_called()

    def test_move_skips_hidden_effort_slot(self):
        laintas_cli._rprompt_modal_slot = "model"
        laintas_cli._rprompt_modal_value = "glm-5.2"
        laintas_cli._rprompt_modal_order = list(laintas_cli._RPROMPT_SLOT_IDS)
        laintas_cli._rprompt_modal_original_order = tuple(
            laintas_cli._rprompt_modal_order)
        with mock.patch.object(laintas_cli, "_terminal_width",
                               return_value=120), self._config(detail=False):
            laintas_cli._rprompt_move_slot(1)
        # terminal is the next visible slot; hidden effort is skipped.
        order = laintas_cli._rprompt_modal_order
        self.assertLess(order.index("terminal"), order.index("model"))


class CommitSideEffectTests(_SlotTestBase):
    def test_model_apply_updates_live_terminal_and_clears_provider(self):
        agent = object()
        with mock.patch.object(laintas_cli, "get_current_agent",
                               return_value=agent), \
                mock.patch.object(laintas_cli, "agent_deployment_terminal",
                                  return_value="term0"), \
                mock.patch.object(laintas_cli, "get_terminal",
                                  return_value=object()), \
                mock.patch.object(laintas_cli, "set_terminal_model_selection",
                                  return_value=True) as live, \
                mock.patch.object(laintas_cli, "set_model_selection") as durable:
            ok, _msg = laintas_cli._rprompt_apply_model_choice("m2")
        self.assertTrue(ok)
        live.assert_called_once_with("term0", "m2", "")
        durable.assert_called_once_with("m2", "")

    def test_ordinary_mode_apply_syncs_approval_posture(self):
        with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                mock.patch.object(laintas_cli.mode_manager, "activate",
                                  return_value=(True, "ok")) as activate, \
                mock.patch.object(laintas_cli,
                                  "_sync_session_approval_from_mode") as sync:
            ok, _msg = laintas_cli._rprompt_apply_mode_choice("review")
        self.assertTrue(ok)
        activate.assert_called_once_with("review")
        sync.assert_called_once_with()

    def test_act_always_is_a_real_logical_choice(self):
        old_writes = laintas_cli._session_approval_state["all_writes"]
        old_commands = laintas_cli._session_approval_state["all_commands"]
        self.addCleanup(
            laintas_cli._session_approval_state.__setitem__,
            "all_writes", old_writes)
        self.addCleanup(
            laintas_cli._session_approval_state.__setitem__,
            "all_commands", old_commands)
        with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                mock.patch.object(laintas_cli.mode_manager, "activate",
                                  return_value=(True, "ok")):
            ok, _msg = laintas_cli._rprompt_apply_mode_choice("act-always")
        self.assertTrue(ok)
        self.assertTrue(laintas_cli._session_approval_state["all_writes"])
        self.assertTrue(laintas_cli._session_approval_state["all_commands"])

    def test_effort_and_order_persist_only_on_commit(self):
        laintas_cli._rprompt_modal_slot = "effort"
        laintas_cli._rprompt_modal_value = "medium"
        laintas_cli._rprompt_modal_order = [
            "mode", "agent", "model", "effort", "terminal"]
        laintas_cli._rprompt_modal_original_order = tuple(
            laintas_cli._RPROMPT_SLOT_IDS)
        calls = []
        with mock.patch.object(laintas_cli, "get_runtime_config",
                               side_effect=lambda key: "low"), \
                mock.patch.object(laintas_cli, "set_runtime_config",
                                  side_effect=lambda k, v: calls.append((k, v)) or True), \
                mock.patch.object(laintas_cli.terminal_preferences,
                                  "set_ui_preference") as persist:
            ok, _msg, _submit = laintas_cli._rprompt_commit()
        self.assertTrue(ok)
        self.assertIn(("reasoning_effort", "medium"), calls)
        self.assertIn(("rprompt_slot_order",
                       "mode,agent,model,effort,terminal"), calls)
        persist.assert_any_call("reasoning_effort", "medium")


class ConfigValidationTests(unittest.TestCase):
    def test_slot_lists_normalize_and_reject_unknown_ids(self):
        import agent_loop
        self.assertEqual(
            agent_loop._coerce_runtime_config_value(
                "rprompt_slots_detail_off", " Mode,AGENT,mode "),
            "mode,agent")
        with self.assertRaises(ValueError):
            agent_loop._coerce_runtime_config_value(
                "rprompt_slots_detail_off", "mode,bogus")

    def test_empty_visibility_is_explicitly_allowed(self):
        import agent_loop
        self.assertEqual(agent_loop._coerce_runtime_config_value(
            "rprompt_slots_detail_off", ""), "")


class SlotKeybindingTests(_SlotTestBase):
    @classmethod
    def setUpClass(cls):
        cls.kb = laintas_cli._build_keybindings()

    def _find(self, *keys):
        wanted = tuple(keys)
        for binding in self.kb.bindings:
            if binding.keys == wanted:
                return binding
        self.fail(f"no binding for {wanted!r}")

    def test_alt_digit_bindings_select_visible_positions(self):
        for digit in "123456789":
            binding = self._find(Keys.Escape, digit)
            with mock.patch.object(laintas_cli, "_rprompt_select_index") as sel:
                binding.handler(SimpleNamespace(app=mock.Mock()))
                self.assertEqual(sel.call_args[0][0], int(digit) - 1)

    def test_cycle_bindings_only_active_while_modal(self):
        for keys in (("up",), ("=",), ("down",), ("-",)):
            binding = self._find(*keys)
            laintas_cli._rprompt_modal_slot = ""
            self.assertFalse(binding.filter())
            laintas_cli._rprompt_modal_slot = "mode"
            self.assertTrue(binding.filter())
            self.addCleanup(laintas_cli._rprompt_modal_exit)

    def test_alt_arrow_move_bindings(self):
        for keys, delta in (((Keys.Escape, Keys.Left), -1),
                            ((Keys.Escape, Keys.Right), 1)):
            binding = self._find(*keys)
            laintas_cli._rprompt_modal_slot = "mode"
            with mock.patch.object(laintas_cli, "_rprompt_move_slot") as move:
                binding.handler(SimpleNamespace(app=mock.Mock()))
                self.assertEqual(move.call_args[0][0], delta)

    def test_typing_exits_modal_and_inserts(self):
        binding = self._find(Keys.Any)
        buf = mock.Mock()
        laintas_cli._rprompt_modal_slot = "mode"
        binding.handler(SimpleNamespace(current_buffer=buf, data="x"))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        buf.insert_text.assert_called_once_with("x")

    def test_enter_commits_and_drops_selection_without_submitting(self):
        binding = self._find(Keys.Enter)
        buf = mock.Mock()
        laintas_cli._rprompt_modal_slot = "mode"
        with mock.patch.object(laintas_cli, "_rprompt_commit",
                               return_value=(True, "ok", "")) as commit:
            binding.handler(SimpleNamespace(current_buffer=buf, app=mock.Mock()))
        commit.assert_called_once_with()
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        buf.validate_and_handle.assert_not_called()

    def test_nonprintable_any_key_is_replayed_after_cancel(self):
        binding = self._find(Keys.Any)
        press = mock.Mock()
        processor = mock.Mock()
        app = SimpleNamespace(key_processor=processor)
        laintas_cli._rprompt_modal_slot = "mode"
        binding.handler(SimpleNamespace(
            current_buffer=mock.Mock(), data="\x03", app=app,
            key_sequence=[press]))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        processor.feed.assert_called_once_with(press, first=True)

    def test_failed_commit_keeps_selection_for_correction(self):
        binding = self._find(Keys.Enter)
        laintas_cli._rprompt_modal_slot = "mode"
        with mock.patch.object(laintas_cli, "_rprompt_commit",
                               return_value=(False, "nope", "")):
            binding.handler(SimpleNamespace(
                current_buffer=mock.Mock(), app=mock.Mock()))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "mode")

    def test_enter_submits_when_no_selection(self):
        binding = self._find(Keys.Enter)
        buf = mock.Mock()
        laintas_cli._rprompt_modal_slot = ""
        binding.handler(SimpleNamespace(current_buffer=buf, app=mock.Mock()))
        buf.validate_and_handle.assert_called_once_with()

    def test_escape_drops_selection_instead_of_clearing(self):
        binding = self._find(Keys.Escape)
        buf = mock.Mock()
        laintas_cli._rprompt_modal_slot = "mode"
        binding.handler(SimpleNamespace(current_buffer=buf, app=mock.Mock()))
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        buf.reset.assert_not_called()

    def test_escape_clears_buffer_without_selection(self):
        binding = self._find(Keys.Escape)
        buf = mock.Mock()
        laintas_cli._rprompt_modal_slot = ""
        binding.handler(SimpleNamespace(current_buffer=buf, app=mock.Mock()))
        buf.reset.assert_called_once_with()


class SlotRenderTests(_SlotTestBase):
    def test_selected_slot_is_highlighted(self):
        laintas_cli._rprompt_modal_slot = "mode"
        frags = self._render(width=100)
        self.assertIn(("class:rprompt-slot-selected", "ACT"), _styled(frags))

    def test_selection_of_hidden_slot_renders_normally(self):
        # effort is detail-off hidden; a stale selection must not style anything
        laintas_cli._rprompt_modal_slot = "effort"
        frags = self._render(width=100, detail=False)
        self.assertNotIn("class:rprompt-slot-selected",
                          [style for style, _t in _styled(frags)])
        self.assertIn("ACT", _text(frags))

    def test_custom_order_renders(self):
        frags = self._render(width=100, rprompt_slot_order="mode,agent")
        # width 100 < 108: the terminal segment stays hidden. The mark leads
        # whatever the order says, so compare the settings behind it.
        self.assertEqual(_join(_without_mark(frags)).strip(),
                         "ACT · primary · glm-5.2")

    def test_custom_visibility_renders(self):
        frags = self._render(width=100, rprompt_slots_detail_off="mode")
        self.assertEqual(_text(frags).strip(), "ACT")


class MessagesMarkRenderTests(_SlotTestBase):
    def test_mark_leads_the_row_in_two_colours(self):
        import startup_mail
        startup_mail.clear()
        startup_mail.post("a", "One")
        startup_mail.post("b", "Two")
        self.addCleanup(startup_mail.clear)
        frags = self._render(width=100)
        self.assertEqual(
            _styled(frags)[:4],
            [("class:rprompt-logo-l", "L"),
             ("class:rprompt-logo-gt", ">"),
             ("class:rprompt-logo-count", " 2"),
             ("class:rprompt-sep", f" {symbols.BULLET} ")])

    def test_mark_leaves_the_row_once_everything_is_read(self):
        import startup_mail
        startup_mail.clear()
        startup_mail.post("a", "One")
        self.addCleanup(startup_mail.clear)
        self.assertIn("L>", _text(self._render(width=100)))
        startup_mail.mark_all_read()
        rendered = self._render(width=100)
        self.assertNotIn("L>", _text(rendered))
        # ...and the settings behind it are untouched by its going.
        self.assertEqual(_join(_without_mark(rendered)).strip(),
                         "primary · ACT · glm-5.2")

    def test_mark_is_absent_when_there_are_no_messages_at_all(self):
        import startup_mail
        startup_mail.clear()
        self.assertNotIn("L>", _text(self._render(width=100)))

    def test_mark_survives_the_narrowest_row(self):
        import startup_mail
        startup_mail.clear()
        startup_mail.post("a", "One")
        self.addCleanup(startup_mail.clear)
        self.assertIn("L>", _text(self._render(width=30)))

    def test_alt_n_still_indexes_the_setting_slots(self):
        # Alt+1 meant "agent" before the mark existed and must still mean it.
        with self._config():
            self.assertEqual(
                laintas_cli._rprompt_final_visible_slot_ids(
                    settings_only=True)[0],
                "agent")

    def test_the_mark_is_selectable_when_there_is_something_to_read(self):
        import startup_mail
        startup_mail.clear()
        startup_mail.post("a", "One")
        self.addCleanup(startup_mail.clear)
        with mock.patch.object(laintas_cli,
                               "_rprompt_slot_currently_visible",
                               return_value=True):
            laintas_cli._rprompt_select_slot("messages")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "messages")
        self.assertEqual(laintas_cli._rprompt_modal_value, "a")

    def test_the_mark_refuses_selection_with_an_empty_list(self):
        import startup_mail
        startup_mail.clear()
        laintas_cli._rprompt_select_slot("messages")
        self.assertEqual(laintas_cli._rprompt_modal_slot, "")
        self.assertIn("No messages", laintas_cli._rprompt_notice_queue[0])

    def test_the_selected_mark_renders_as_a_selected_slot(self):
        import startup_mail
        startup_mail.clear()
        startup_mail.post("a", "One")
        self.addCleanup(startup_mail.clear)
        laintas_cli._rprompt_modal_slot = "messages"
        frags = self._render(width=100)
        self.assertEqual(_styled(frags)[0],
                         ("class:rprompt-slot-selected", "L> 1"))


class DeferredNoticeTests(_SlotTestBase):
    def test_slot_notices_drain_through_console(self):
        laintas_cli._rprompt_queue_notice("plain notice")
        laintas_cli._rprompt_queue_notice("bracketed [mode]")
        with mock.patch.object(laintas_cli.console, "print") as printed:
            laintas_cli._flush_deferred_notices()
        self.assertEqual(printed.call_count, 2)
        second = printed.call_args_list[1][0][0]
        self.assertIn("\\[mode]", second)   # Rich markup escaped ([ only)


class LegacyRenderEquivalenceTests(unittest.TestCase):
    """The slot refactor must not change what the rprompt looks like.

    Byte-level comparison against a snapshot of the pre-refactor renderer
    across width x detail x multi-agent x path x model combinations.
    """

    def setUp(self):
        self._saved_cache = dict(laintas_cli._status_cache)
        laintas_cli._rprompt_modal_exit()
        self.addCleanup(self._restore)

    def _restore(self):
        laintas_cli._status_cache.clear()
        laintas_cli._status_cache.update(self._saved_cache)
        laintas_cli._rprompt_modal_exit()

    def test_refactored_render_is_byte_identical(self):
        snapshot = json.loads(_FIXTURE.read_text())
        cfg = {"detail": False, "reasoning_effort": "low"}
        mismatches = []
        for key, legacy in snapshot.items():
            width, detail, multi, avail, path, model = key.split("|")
            laintas_cli._status_cache.update(
                model=model, agent="primary", terminal="term0",
                multi_agent=multi == "1", input_available=avail == "1",
                prompt_path=path)
            cfg["detail"] = detail == "1"
            with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                    mock.patch.object(laintas_cli.mode_manager,
                                      "get_active_mode",
                                      return_value={"name": "act"}), \
                    mock.patch.object(laintas_cli.mode_manager,
                                      "is_read_only_mode",
                                      return_value=False), \
                    mock.patch.object(laintas_cli, "_terminal_width",
                                      return_value=int(width)), \
                    mock.patch.object(laintas_cli, "get_runtime_config",
                                      side_effect=lambda k: cfg.get(k)):
                fresh = laintas_cli._render_rprompt()
            if _without_mark(fresh) != [tuple(x) for x in legacy]:
                mismatches.append(key)
        self.assertEqual(mismatches, [],
                         f"{len(mismatches)} render regressions: {mismatches[:5]}")


if __name__ == "__main__":
    unittest.main()
