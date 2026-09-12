"""The HWO `#name:gear#` thinking pin, end to end through the CLI.

The pin is a second axis beside the `@model` pin: how hard this agent thinks,
inherited by everything under it until something re-pins. These tests cover the
three places a pin can be silently lost — the parser, the inheritance rule, and
the request that finally carries it — plus the Workflow Studio round trip,
which rewrites the file it read.
"""

import unittest
from unittest import mock

import agent_loop
import backend_profiles
import hwo_runner
import hwo_ui
import laintas_cli


class EffortPinParsingTests(unittest.TestCase):
    def test_a_gear_can_be_pinned_with_or_without_a_model(self):
        steps = hwo_runner.parse_hwo(
            "#planner@glm-5.3:max# { -> think }\n->\n#linter:none# { -> lint }\n")
        self.assertEqual((steps[0].name, steps[0].model, steps[0].effort),
                         ("planner", "glm-5.3", "max"))
        self.assertEqual((steps[1].name, steps[1].model, steps[1].effort),
                         ("linter", None, "none"))

    def test_an_unpinned_agent_carries_no_gear(self):
        steps = hwo_runner.parse_hwo("#worker# { -> work }\n")
        self.assertIsNone(steps[0].effort)
        self.assertIsNone(steps[0].model)

    def test_an_unknown_gear_is_a_parse_error_not_a_silent_default(self):
        with self.assertRaises(hwo_runner.HwoParseError) as caught:
            hwo_runner.parse_hwo("#a:turbo# { -> x }\n")
        self.assertIn("turbo", str(caught.exception))

    def test_the_summary_shows_the_pin_it_will_run_with(self):
        steps = hwo_runner.parse_hwo("#planner@glm-5.3:max# { -> think }\n")
        self.assertIn("#planner@glm-5.3:max#",
                      "\n".join(hwo_runner.summarize_steps(steps)))


class EffortPinInheritanceTests(unittest.TestCase):
    """`_resolve_pins` — own pin wins, else the parent's, per axis."""

    def _ctx(self, model=None, effort=None):
        return hwo_runner.HwoCtx(deps=object(), session={},
                                 model_override=model, effort_override=effort)

    def test_an_agents_own_pins_win_over_the_inherited_ones(self):
        step = hwo_runner.HwoAgent(name="a", model="glm-5.2", effort="none")
        self.assertEqual(
            hwo_runner._resolve_pins(step, self._ctx("glm-5.3", "max")),
            ("glm-5.2", "none"))

    def test_an_unpinned_agent_inherits_the_whole_subtree_pin(self):
        step = hwo_runner.HwoAgent(name="a")
        self.assertEqual(
            hwo_runner._resolve_pins(step, self._ctx("glm-5.3", "high")),
            ("glm-5.3", "high"))

    def test_the_two_axes_are_independent(self):
        """Pinning only the gear must not disturb an inherited model pin, and
        pinning only a model must not disturb an inherited gear."""
        gear_only = hwo_runner.HwoAgent(name="a", effort="none")
        self.assertEqual(
            hwo_runner._resolve_pins(gear_only, self._ctx("glm-5.3", "max")),
            ("glm-5.3", "none"))
        model_only = hwo_runner.HwoAgent(name="a", model="glm-5.2")
        self.assertEqual(
            hwo_runner._resolve_pins(model_only, self._ctx("glm-5.3", "max")),
            ("glm-5.2", "max"))

    def test_nothing_pinned_anywhere_leaves_both_axes_alone(self):
        self.assertEqual(
            hwo_runner._resolve_pins(hwo_runner.HwoAgent(name="a"), self._ctx()),
            (None, None))


class WorkflowStudioRoundTripTests(unittest.TestCase):
    def test_a_pin_studio_cannot_edit_is_still_written_back(self):
        """Studio has no editor for the thinking pin, so the only way it can
        get it wrong is by dropping it on the way out."""
        import tempfile
        from pathlib import Path
        source = "#planner@glm-5.3:max# {\n  -> think\n}\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "flow.hwo")
            path.write_text(source, encoding="utf-8")
            session = hwo_ui.load_hwo_file(str(path))[0]
        self.assertIn("#planner@glm-5.3:max#", hwo_ui._session_to_hwo(session))


class EffortPinOnTheWireTests(unittest.TestCase):
    """What the pin becomes in the request the gateway receives."""

    @staticmethod
    def _config(gear):
        """Only reasoning_effort is forced; every other key keeps its real
        default, so the request builder still gets ints where it needs them."""
        def _get(key, *args, **kw):
            if key == "reasoning_effort":
                return gear
            return agent_loop._DEFAULT_CONFIG.get(key)
        return _get

    def _sent(self, **kwargs):
        profile = backend_profiles.BackendProfile(
            "custom", "custom", "https://ai.example.com")
        lines = ['data: ' + '{"choices": [{"delta": {"content": "ok"}, '
                 '"finish_reason": "stop"}]}']
        with mock.patch.object(laintas_cli, "get_backend_profile", return_value=profile), \
                mock.patch.object(laintas_cli.requests, "post") as post, \
                mock.patch.object(laintas_cli, "get_selected_model", return_value=""), \
                mock.patch.object(laintas_cli, "get_selected_provider", return_value=""):
            post.return_value = _FakeResponse(lines)
            kwargs.setdefault("tools_enabled", False)
            laintas_cli.call_backend_stream({}, "hello", "system", "/tmp", **kwargs)
        return post.call_args.kwargs["json"]

    def test_the_pin_overrides_the_configured_gear(self):
        with mock.patch.object(laintas_cli, "get_runtime_config", side_effect=self._config("low")):
            self.assertEqual(self._sent(effort_override="max")["reasoningEffort"],
                             "max")

    def test_auto_is_a_valid_pin_and_reaches_the_gateway_verbatim(self):
        with mock.patch.object(laintas_cli, "get_runtime_config", side_effect=self._config("low")):
            self.assertEqual(self._sent(effort_override="auto")["reasoningEffort"],
                             "auto")

    def test_no_pin_leaves_the_configured_gear_alone(self):
        with mock.patch.object(laintas_cli, "get_runtime_config", side_effect=self._config("high")):
            self.assertEqual(self._sent()["reasoningEffort"], "high")

    def test_a_malformed_pin_is_ignored_rather_than_forwarded(self):
        """The gateway substitutes its default for an unknown gear, so a typo
        that reached the wire would look like a pin that worked."""
        with mock.patch.object(laintas_cli, "get_runtime_config", side_effect=self._config("low")):
            self.assertEqual(self._sent(effort_override="turbo")["reasoningEffort"],
                             "low")

    def test_the_gateway_is_asked_for_the_pinnable_model_list(self):
        """Nothing else in this CLI tells the model which ids `@model` accepts,
        and only the gateway knows which accounts are switched on."""
        with mock.patch.object(laintas_cli, "get_runtime_config",
                               side_effect=self._config("low")):
            self.assertIs(self._sent(tools_enabled=True)["injectModelPins"], True)
            # A tool-less call (compaction, summaries) can not write a workflow.
            self.assertIs(self._sent()["injectModelPins"], False)

    def test_the_accepted_pin_values_match_what_config_offers(self):
        self.assertEqual(
            set(laintas_cli._EFFORT_PIN_VALUES),
            set(agent_loop._RUNTIME_ENUM_CHOICES["reasoning_effort"]))


class _FakeResponse:
    def __init__(self, lines):
        self.status_code = 200
        self.headers = {}
        self._lines = lines

    def iter_lines(self, *a, **kw):
        yield from self._lines

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
