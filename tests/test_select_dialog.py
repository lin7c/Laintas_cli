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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import laintas_cli

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


if __name__ == "__main__":
    unittest.main()
