"""Regression tests for select_dialog — the shared arrow-key picker.

These drive the real prompt_toolkit Application over a pipe input so a
rendering crash (like the _visible() 2-tuple/3-tuple unpack bug that broke
every picker and auto-denied approval gates) fails the suite instead of
being swallowed by callers' except-Exception blocks.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import laintas_cli
import plan_mode

# select_dialog ignores confirm/cancel keys for 250ms after open (replayed
# typeahead defense); feed keys after this to act as a real user.
AFTER_GRACE = 0.35


class SelectDialogTests(unittest.TestCase):
    def _run(self, items, keys, *, pre_keys="", **kwargs):
        result = {}
        with create_pipe_input() as pipe:
            if pre_keys:
                pipe.send_text(pre_keys)

            def feed():
                time.sleep(AFTER_GRACE)
                pipe.send_text(keys)

            feeder = threading.Thread(target=feed)
            feeder.start()
            try:
                with create_app_session(input=pipe, output=DummyOutput()):
                    result["value"] = laintas_cli.select_dialog(
                        items, refresh_interval=0.5, **kwargs)
            finally:
                feeder.join()
        return result["value"]

    def test_enter_selects_first_item(self):
        self.assertEqual(
            self._run(["Yes", "No"], "\r", full_screen=False), "Yes")

    def test_arrow_down_then_enter_selects_second_item(self):
        self.assertEqual(
            self._run(["Yes", "No"], "\x1b[B\r", full_screen=False), "No")

    def test_renders_with_descriptions_and_search_filter(self):
        items = [("model-a", "provider-a"), ("model-b", "provider-b")]
        self.assertEqual(
            self._run(items, "\x1b[B\r", full_screen=False, search=True),
            ("model-b", "provider-b"))

    def test_q_cancels(self):
        self.assertIsNone(self._run(["Yes", "No"], "q", full_screen=False))

    def test_grace_period_ignores_replayed_enter(self):
        # A stray Enter queued before the dialog opens must not confirm;
        # the post-grace "q" cancels, proving the early Enter was dropped.
        self.assertIsNone(
            self._run(["Yes", "No"], "q", pre_keys="\r", full_screen=False))

    def test_letter_shortcut_confirms(self):
        self.assertEqual(
            self._run(["Yes", "No"], "n", full_screen=False,
                      letter_shortcuts=True),
            "No")


class SelectionEntryPointTests(unittest.TestCase):
    def test_choose_record_maps_rendered_row_back_to_record(self):
        records = [
            {"id": "a", "description": "first"},
            {"id": "b", "description": "second"},
        ]

        def choose_second(rows, **_kwargs):
            return rows[1]

        with mock.patch.object(
                laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(
                    laintas_cli, "select_dialog", side_effect=choose_second):
            chosen = laintas_cli.choose_record(
                records,
                title="Choose",
                label=lambda item: item["id"],
                description=lambda item: item["description"],
            )
        self.assertIs(chosen, records[1])

    def test_choose_record_does_not_prompt_without_tty(self):
        with mock.patch.object(
                laintas_cli.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(laintas_cli, "select_dialog") as dialog:
            self.assertIsNone(laintas_cli.choose_record(
                [{"id": "a"}], title="Choose",
                label=lambda item: item["id"]))
        dialog.assert_not_called()

    def test_login_method_is_browser_oauth_only(self):
        with mock.patch.object(laintas_cli, "select_dialog") as dialog:
            self.assertEqual(laintas_cli.choose_login_method(), "remote")
        dialog.assert_not_called()

if __name__ == "__main__":
    unittest.main()
