"""Regression: a stale asyncio running-loop flag must not disable approvals.

prompt_toolkit's ``Application.run()`` goes through ``asyncio.run()``. If an
earlier full-screen app (the /agents view, the .hwo UI, a previous selector)
was interrupted while asyncio was tearing its loop down, the thread keeps a
running-loop flag pointing at a closed loop. Every later dialog on that thread
then raises ``RuntimeError: asyncio.run() cannot be called from a running event
loop`` — which surfaced as browser.navigate/click failing for the rest of a
session, because their approval gate could never be drawn.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import laintas_cli
import tools


def _set_stale_flag():
    """Reproduce the interrupted-teardown state: flag set, loop closed."""
    loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(loop)
    loop.close()
    return loop


class StaleRunningLoopTests(unittest.TestCase):

    def tearDown(self):
        asyncio.events._set_running_loop(None)

    def test_stale_flag_is_cleared(self):
        _set_stale_flag()
        self.assertTrue(laintas_cli._clear_stale_running_loop())
        self.assertIsNone(asyncio.events._get_running_loop())

    def test_live_loop_is_never_stolen(self):
        async def probe():
            cleared = laintas_cli._clear_stale_running_loop()
            return cleared, asyncio.events._get_running_loop() is not None

        cleared, still_set = asyncio.run(probe())
        self.assertFalse(cleared, "a genuinely running loop must be left alone")
        self.assertTrue(still_set)

    def test_no_stale_flag_is_a_noop(self):
        asyncio.events._set_running_loop(None)
        self.assertFalse(laintas_cli._clear_stale_running_loop())

    def test_select_dialog_recovers_from_stale_flag(self):
        """The dialog clears the flag before running, so it starts normally."""
        _set_stale_flag()
        seen = {}

        class _FakeApp:
            def __init__(self, *a, **kw):
                pass

            def run(self, pre_run=None):
                seen["ran"] = asyncio.events._get_running_loop()
                return "y approve"

        with mock.patch.object(laintas_cli, "Application", _FakeApp):
            result = laintas_cli.select_dialog(
                ["y approve", "n deny"], full_screen=False,
                letter_shortcuts=True)

        self.assertEqual(result, "y approve")
        self.assertIsNone(seen["ran"],
                          "flag must be cleared BEFORE app.run(), not after")
        self.assertIsNone(asyncio.events._get_running_loop())


class BrowserApprovalPromptTests(unittest.TestCase):
    """The approval line must name what is being approved."""

    def _prompt_for(self, action, params):
        captured = {}

        def _approve(cmd, reason):
            captured["cmd"] = cmd
            return True

        ctx = tools.ToolCtx(
            agent_id="test", cwd="/tmp", depth=0, session={},
            deps=SimpleNamespace(request_command_approval=_approve))
        decision = SimpleNamespace(action="needs_approval", reason="test rule")
        with mock.patch("policy.evaluate_browser_action", return_value=decision):
            blocked = tools._browser_check_action(action, params, ctx)
        self.assertIsNone(blocked)
        return captured["cmd"]

    def test_click_by_ref_names_the_target(self):
        # The model targets elements by ref (from browser.snapshot); reading
        # only 'selector' rendered a bare "browser.click " with no target.
        self.assertEqual(self._prompt_for("click", {"ref": 12}),
                         "browser.click ref=12")

    def test_click_by_selector_still_shown(self):
        self.assertEqual(self._prompt_for("click", {"selector": "#buy"}),
                         "browser.click #buy")

    def test_click_without_target_is_explicit(self):
        self.assertEqual(self._prompt_for("click", {}),
                         "browser.click (no target given)")


if __name__ == "__main__":
    unittest.main()
