"""A retried backend call must say why the turn is waiting.

Timeouts, refused connections, 429 and 5xx were retried with no output at all:
the only thing on screen was "Thinking…" for as long as four 420s attempts.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import laintas_cli


class BackendRetryNoticeTests(unittest.TestCase):

    def test_foreground_turn_is_told_why_it_waits(self):
        with mock.patch.object(laintas_cli, "_is_foreground_turn", return_value=True), \
                mock.patch.object(laintas_cli.console, "print") as out:
            laintas_cli._announce_backend_retry("main_loop", "returned 503", 4.0, 0, 3)
        out.assert_called_once()
        text = out.call_args[0][0]
        self.assertIn("returned 503", text)
        self.assertIn("4s", text)
        self.assertIn("2/4", text)

    def test_auxiliary_calls_stay_quiet(self):
        with mock.patch.object(laintas_cli.console, "print") as out:
            for kind in ("critic", "compaction", "mem_extract", ""):
                laintas_cli._announce_backend_retry(kind, "timed out", 2.0, 0, 3)
        out.assert_not_called()

    def test_a_broken_console_never_breaks_the_retry(self):
        with mock.patch.object(laintas_cli, "_is_foreground_turn", return_value=True), \
                mock.patch.object(laintas_cli.console, "print", side_effect=OSError("EIO")):
            laintas_cli._announce_backend_retry("main_loop", "unreachable", 8.0, 2, 3)


if __name__ == "__main__":
    unittest.main()
