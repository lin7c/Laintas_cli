"""/model routing — the auto model's preferences.

The gateway applies them (agent_gateway `_routing_prefs`); this end stores
them as one terminal preference, edits them in /model, and sends them as the
request field `routing` only when the model is auto.
"""
import unittest
from unittest import mock

import auto_routing
import laintas_cli
import terminal_preferences

ROUTES = ["deepseek-v4-pro", "glm-5.3", "gpt-6-luna"]


def _catalogue(routes=ROUTES, supported=True):
    return (list(routes), supported)


class _PrefsCase(unittest.TestCase):
    def setUp(self):
        auto_routing.save({})
        self.addCleanup(auto_routing.save, {})


class ModuleTest(_PrefsCase):
    def test_default_stores_nothing_and_sends_nothing(self):
        self.assertIsNone(terminal_preferences.get(auto_routing.PREF_KEY))
        self.assertEqual(auto_routing.request_field(), {})

    def test_round_trip_and_field(self):
        auto_routing.save({"candidates": [" a ", "a", "b", ""], "complex": " b "})
        self.assertEqual(auto_routing.load(),
                         {"candidates": ["a", "b"], "everyday": "", "complex": "b"})
        self.assertEqual(auto_routing.request_field(), {"candidates": ["a", "b"], "complex": "b"})
        auto_routing.save({"candidates": []})
        self.assertIsNone(terminal_preferences.get(auto_routing.PREF_KEY))

    def test_choosing_every_model_stores_no_list(self):
        prefs, cleared = auto_routing.set_candidates({}, list(reversed(ROUTES)), ROUTES)
        self.assertEqual(prefs["candidates"], [])
        self.assertEqual(cleared, [])

    def test_narrowing_keeps_backend_order_and_clears_orphaned_slots(self):
        prefs, cleared = auto_routing.set_candidates(
            {"everyday": "glm-5.3", "complex": "gpt-6-luna"},
            ["gpt-6-luna", "deepseek-v4-pro"], ROUTES)
        self.assertEqual(prefs["candidates"], ["deepseek-v4-pro", "gpt-6-luna"])
        self.assertEqual((prefs["everyday"], prefs["complex"]), ("", "gpt-6-luna"))
        self.assertEqual(cleared, ["everyday"])

    def test_an_empty_choice_is_refused_not_read_as_all(self):
        with self.assertRaises(ValueError):
            auto_routing.set_candidates({}, [], ROUTES)

    def test_catalogue_is_scoped_to_the_backend(self):
        auto_routing.remember_catalogue(
            {"modelCapabilities": {"auto": {"routesTo": ROUTES, "routingPrefs": True}}}, "b1")
        self.assertEqual(auto_routing.catalogue("b1"), (ROUTES, True))
        self.assertIsNone(auto_routing.catalogue("b2"))
        # An older gateway lists auto's models but does not apply preferences.
        auto_routing.remember_catalogue({"modelCapabilities": {"auto": {"routesTo": ["x"]}}}, "b2")
        self.assertEqual(auto_routing.catalogue("b2"), (["x"], False))
        # No auto entry at all is still an answer.
        auto_routing.remember_catalogue({"modelCapabilities": {}}, "b3")
        self.assertEqual(auto_routing.catalogue("b3"), ([], False))

    def test_new_terminals_inherit_it_like_the_model(self):
        self.assertIn(auto_routing.PREF_KEY, terminal_preferences.SEEDED_KEYS)


class PayloadTest(_PrefsCase):
    class _Sent(Exception):
        """Stops the call at the wire: the payload is complete by then."""

    def _sent(self, selected, override=None):
        sink = {}
        with mock.patch.object(laintas_cli, "get_selected_model", return_value=selected), \
                mock.patch.object(laintas_cli, "get_selected_provider", return_value=""), \
                mock.patch.object(laintas_cli, "_post_with_interrupt", side_effect=self._Sent), \
                mock.patch.object(laintas_cli.usage_tracker, "record"):
            try:
                laintas_cli._call_backend_stream_impl(
                    {}, "hi", "SYS", "/tmp", tools_enabled=False, model_override=override,
                    _local_sink=sink)
            except self._Sent:
                pass
        return sink.get("payload") or {}

    def test_only_auto_requests_carry_it(self):
        auto_routing.save({"complex": "glm-5.3"})
        self.assertEqual(self._sent("auto")["routing"], {"complex": "glm-5.3"})
        self.assertEqual(self._sent("")["routing"], {"complex": "glm-5.3"})
        self.assertEqual(self._sent("glm-5.3", override="auto")["routing"], {"complex": "glm-5.3"})
        self.assertNotIn("routing", self._sent("glm-5.3"))

    def test_nothing_set_leaves_the_payload_unchanged(self):
        self.assertNotIn("routing", self._sent("auto"))


class SelectorTest(_PrefsCase):
    MODELS = [{"id": "glm-5.3", "tier": "T2"}, {"id": "gpt-6-luna", "tier": "T3"}]

    def _pick(self, row, **kwargs):
        def fake(labels, **_kw):
            return labels[row]
        with mock.patch.object(laintas_cli, "select_dialog", side_effect=fake):
            return laintas_cli.show_model_selector(self.MODELS, "", **kwargs)

    def test_settings_row_shifts_models_by_one(self):
        self.assertEqual(self._pick(0, routing_settings=True), {"id": "auto", "provider": ""})
        self.assertEqual(self._pick(1, routing_settings=True), {"routing_settings": True})
        self.assertEqual(self._pick(2, routing_settings=True)["id"], "glm-5.3")
        self.assertEqual(self._pick(3, routing_settings=True)["id"], "gpt-6-luna")

    def test_other_pickers_are_unchanged(self):
        self.assertEqual(self._pick(1)["id"], "glm-5.3")
        self.assertEqual(self._pick(2)["id"], "gpt-6-luna")

    def test_current_model_is_preselected_below_the_settings_row(self):
        seen = {}

        def fake(labels, **kw):
            seen["index"] = kw["selected_index"]
            return None
        with mock.patch.object(laintas_cli, "select_dialog", side_effect=fake):
            laintas_cli.show_model_selector(self.MODELS, "gpt-6-luna", routing_settings=True)
        self.assertEqual(seen["index"], 3)


class MenuTest(_PrefsCase):
    def _run(self, script, catalogue=None):
        """Drive the editor: `script` answers each dialog in turn.

        An answer is a label prefix to pick (single), a list of label
        substrings to tick (multi), or None for Esc.
        """
        answers = list(script)
        titles = []

        def fake(labels, **kw):
            titles.append(kw.get("title", ""))
            answer = answers.pop(0)
            if answer is None:
                return None
            if kw.get("multi"):
                return [lab for lab in labels if any(a in lab for a in answer)]
            plain = [lab[0] if isinstance(lab, tuple) else lab for lab in labels]
            return labels[next(i for i, p in enumerate(plain) if answer in p)]
        with mock.patch.object(laintas_cli, "select_dialog", side_effect=fake), \
                mock.patch.object(laintas_cli, "_auto_routing_catalogue",
                                  return_value=catalogue or _catalogue()):
            laintas_cli._cmd_model_routing_menu({})
        self.assertEqual(answers, [], "every scripted dialog was shown")
        return titles

    def test_pick_a_complex_model_then_done(self):
        self._run(["Complex tasks", "glm-5.3", "Done"])
        self.assertEqual(auto_routing.load()["complex"], "glm-5.3")

    def test_esc_in_a_sub_picker_changes_nothing(self):
        self._run(["Everyday tasks", None, None])
        self.assertTrue(auto_routing.is_default(auto_routing.load()))

    def test_untick_one_candidate(self):
        self._run(["Models auto may pick", ["deepseek-v4-pro", "gpt-6-luna"], "Done"])
        self.assertEqual(auto_routing.load()["candidates"], ["deepseek-v4-pro", "gpt-6-luna"])

    def test_unticking_everything_is_refused_with_a_notice(self):
        titles = self._run(["Models auto may pick", [], "Done"])
        self.assertTrue(auto_routing.is_default(auto_routing.load()))
        self.assertIn("keep at least one model", titles[-1])

    def test_unticking_the_slot_model_clears_the_slot_and_says_so(self):
        auto_routing.save({"everyday": "glm-5.3"})
        titles = self._run(["Models auto may pick", ["deepseek-v4-pro"], "Done"])
        self.assertEqual(auto_routing.load()["everyday"], "")
        self.assertIn("everyday tasks back to auto picks", titles[-1])

    def test_slot_picker_offers_only_allowed_models(self):
        auto_routing.save({"candidates": ["glm-5.3"]})
        offered = []

        def fake(labels, **kw):
            offered.append(labels)
            return None
        with mock.patch.object(laintas_cli, "select_dialog", side_effect=fake):
            laintas_cli._auto_routing_pick_slot("complex", auto_routing.load(), ROUTES)
        text = " ".join(lab[0] for lab in offered[0])
        self.assertIn("glm-5.3", text)
        self.assertNotIn("gpt-6-luna", text)

    def test_reset_row_appears_only_when_customised(self):
        rows = []

        def fake(labels, **kw):
            rows.append([lab[0] for lab in labels])
            return None
        with mock.patch.object(laintas_cli, "select_dialog", side_effect=fake), \
                mock.patch.object(laintas_cli, "_auto_routing_catalogue", return_value=_catalogue()):
            laintas_cli._cmd_model_routing_menu({})
            auto_routing.save({"complex": "glm-5.3"})
            laintas_cli._cmd_model_routing_menu({})
        self.assertFalse(any(r.startswith("Reset") for r in rows[0]))
        self.assertTrue(any(r.startswith("Reset") for r in rows[1]))

    def test_backend_without_support_is_named_in_the_title(self):
        titles = self._run([None], catalogue=_catalogue(supported=False))
        self.assertIn("ignores these settings", titles[0])


class TypedCommandTest(_PrefsCase):
    def _cmd(self, *args, catalogue="default"):
        cat = _catalogue() if catalogue == "default" else catalogue
        with mock.patch.object(laintas_cli, "_auto_routing_catalogue", return_value=cat), \
                laintas_cli.console.capture() as cap:
            laintas_cli._cmd_model_routing(list(args), {})
        return cap.get()

    def test_set_and_clear_a_slot(self):
        self._cmd("everyday", "glm-5.3")
        self.assertEqual(auto_routing.load()["everyday"], "glm-5.3")
        self._cmd("everyday", "off")
        self.assertEqual(auto_routing.load()["everyday"], "")

    def test_unknown_model_is_refused(self):
        out = self._cmd("complex", "no-such-model")
        self.assertIn("not among the models", out)
        self.assertEqual(auto_routing.load()["complex"], "")

    def test_slot_must_be_an_allowed_candidate(self):
        auto_routing.save({"candidates": ["glm-5.3"]})
        out = self._cmd("complex", "gpt-6-luna")
        self.assertIn("auto may pick", out)

    def test_without_a_catalogue_the_id_is_kept(self):
        self._cmd("complex", "anything", catalogue=None)
        self.assertEqual(auto_routing.load()["complex"], "anything")

    def test_candidates_and_all(self):
        self._cmd("candidates", "glm-5.3,deepseek-v4-pro")
        self.assertEqual(auto_routing.load()["candidates"], ["deepseek-v4-pro", "glm-5.3"])
        self._cmd("candidates", "all")
        self.assertEqual(auto_routing.load()["candidates"], [])

    def test_reset_and_usage(self):
        self._cmd("everyday", "glm-5.3")
        self._cmd("reset")
        self.assertTrue(auto_routing.is_default(auto_routing.load()))
        self.assertIn("Usage", self._cmd("everyday"))
        self.assertIn("Usage", self._cmd("bogus", "x"))

    def test_status_warns_when_the_terminal_is_not_on_auto(self):
        with mock.patch.object(laintas_cli, "_current_terminal_model", return_value="glm-5.3"), \
                mock.patch.object(laintas_cli.sys, "stdin", mock.Mock(isatty=lambda: False)), \
                mock.patch.object(laintas_cli, "_auto_routing_catalogue", return_value=_catalogue()), \
                laintas_cli.console.capture() as cap:
            laintas_cli._cmd_model_routing([], {})
        self.assertIn("apply when /model is auto", cap.get())


class SlashContractTest(unittest.TestCase):
    def test_arg_rule_and_dispatch(self):
        rule = laintas_cli._SLASH_ARG_RULES[("/model", "routing")]
        self.assertEqual(rule.max_args, 3)
        with mock.patch.object(laintas_cli, "_cmd_model_routing") as routed:
            laintas_cli._cmd_model(["/model", "routing", "reset"], "routing reset", {})
        routed.assert_called_once_with(["reset"], {})

    def test_completions(self):
        laintas_cli._ARG_COMPLETION_CACHE.clear()
        first = [v for v, _ in laintas_cli._dynamic_arg_candidates("/model", ["routing"], "")]
        self.assertEqual(first, ["everyday", "complex", "candidates", "reset"])
        with mock.patch.object(laintas_cli, "_auto_routing_cached_routes", return_value=ROUTES):
            slot = [v for v, _ in laintas_cli._dynamic_arg_candidates(
                "/model", ["routing", "complex"], "")]
            listed = [v for v, _ in laintas_cli._dynamic_arg_candidates(
                "/model", ["routing", "candidates"], "glm-5.3,g")]
        self.assertEqual(slot, ["off", *ROUTES])
        self.assertIn("glm-5.3,gpt-6-luna", listed)
        self.assertNotIn("glm-5.3,glm-5.3", listed)


if __name__ == "__main__":
    unittest.main()
