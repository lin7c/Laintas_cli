"""Regression tests for select_dialog — the shared arrow-key picker.

These drive the real prompt_toolkit Application over a pipe input so a
rendering crash (like the _visible() 2-tuple/3-tuple unpack bug that broke
every picker and auto-denied approval gates) fails the suite instead of
being swallowed by callers' except-Exception blocks.
"""

import os
import pty
import sys
import termios
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
import terminal_arbiter

# select_dialog ignores confirm/cancel keys for 250ms after open (replayed
# typeahead defense); feed keys after this to act as a real user.
AFTER_GRACE = 0.35


class SelectDialogTests(unittest.TestCase):
    def _run(self, items, keys, *, pre_keys="", delay=AFTER_GRACE, **kwargs):
        result = {}
        with create_pipe_input() as pipe:
            if pre_keys:
                pipe.send_text(pre_keys)

            def feed():
                time.sleep(delay)
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

    def test_escape_cancels_during_startup_grace_period(self):
        self.assertIsNone(
            self._run(
                ["Yes", "No"], "\x1b", delay=0.05, full_screen=False))

    def test_control_c_cancels_during_startup_grace_period(self):
        self.assertIsNone(
            self._run(
                ["Yes", "No"], "\x03", delay=0.05, full_screen=False))

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

    def test_auto_confirm_selects_requested_item(self):
        with create_pipe_input() as pipe, \
                create_app_session(input=pipe, output=DummyOutput()):
            result = laintas_cli.select_dialog(
                ["Yes", "No"],
                selected_index=1,
                full_screen=False,
                auto_confirm_seconds=0.05,
                auto_confirm_index=0,
                refresh_interval=0.01,
            )
        self.assertEqual(result, "Yes")

    def test_user_can_override_auto_confirm_before_timeout(self):
        self.assertEqual(
            self._run(
                ["Yes", "No"], "n",
                full_screen=False,
                letter_shortcuts=True,
                auto_confirm_seconds=1.0,
                auto_confirm_index=0,
            ),
            "No",
        )


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


class KeyPromptTests(unittest.TestCase):
    """The single-key prompts, driven over a real pty.

    These used to be written against mocks of ``tty.setraw`` / ``os.read``,
    which meant they asserted the *implementation* — and kept passing while
    that implementation raced other readers for stdin. Now they write real
    bytes into a real terminal and check what the prompt decides, so the
    whole path (arbiter, mode, parser) is under test.
    """

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(self._close_pty)
        self.arbiter = terminal_arbiter.TerminalArbiter(fd=self.slave)
        self.addCleanup(self.arbiter.shutdown)
        patcher = mock.patch.object(terminal_arbiter, "_arbiter", self.arbiter)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pristine = termios.tcgetattr(self.slave)

    def _close_pty(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def _send_later(self, data: bytes, delay: float = 0.15):
        t = threading.Timer(delay, lambda: os.write(self.master, data))
        t.daemon = True
        t.start()
        self.addCleanup(t.cancel)

    def test_blocking_operation_can_be_cancelled_by_escape(self):
        self._send_later(b"\x1b")
        with self.assertRaises(laintas_cli.BlockingOperationCancelled):
            laintas_cli.run_cancellable_blocking(
                lambda _cancel: time.sleep(5.0))

    def test_blocking_operation_restores_the_terminal(self):
        self._send_later(b"\x1b")
        with self.assertRaises(laintas_cli.BlockingOperationCancelled):
            laintas_cli.run_cancellable_blocking(
                lambda _cancel: time.sleep(5.0))
        self.assertEqual(self.pristine[3],
                         termios.tcgetattr(self.slave)[3])

    def test_arrow_key_does_not_cancel_a_blocking_operation(self):
        # An impatient arrow press starts with 0x1b too. The old code guessed
        # from inter-byte timing and could read it as a bare Esc.
        self._send_later(b"\x1b[B")
        self.assertEqual(
            "done",
            laintas_cli.run_cancellable_blocking(lambda _cancel: "done"))

    def test_single_key_approval_takes_yes(self):
        self._send_later(b"y")
        self.assertEqual("yes", laintas_cli._read_single_key_choice(
            allow_always=True, auto_confirm_seconds=None))

    def test_single_key_approval_takes_always(self):
        self._send_later(b"a")
        self.assertEqual("always", laintas_cli._read_single_key_choice(
            allow_always=True, auto_confirm_seconds=None))

    def test_single_key_approval_denies_on_bare_enter(self):
        self._send_later(b"\r")
        self.assertEqual("no", laintas_cli._read_single_key_choice(
            allow_always=True, auto_confirm_seconds=None))

    def test_single_key_approval_cancels_on_escape(self):
        self._send_later(b"\x1b")
        self.assertIsNone(laintas_cli._read_single_key_choice(
            allow_always=True, auto_confirm_seconds=None))

    def test_single_key_approval_leaves_ctrl_c_to_the_kernel(self):
        # Behaviour change, deliberate: this prompt used to run in raw mode so
        # it could read 0x03 as a byte, which disabled ISIG — a prompt the
        # user could not signal out of if it ever stopped consuming input.
        # In cbreak the terminal raises SIGINT instead, so the escape hatch
        # does not depend on this loop being healthy.
        seen = {}

        def watch(*_args):
            seen["isig"] = bool(termios.tcgetattr(self.slave)[3] & termios.ISIG)
            os.write(self.master, b"n")

        t = threading.Timer(0.15, watch)
        t.daemon = True
        t.start()
        self.addCleanup(t.cancel)

        self.assertEqual("no", laintas_cli._read_single_key_choice(
            allow_always=True, auto_confirm_seconds=None))
        self.assertTrue(seen.get("isig"),
                        "approval prompt disabled ISIG; Ctrl+C would be dead")

    def test_single_key_approval_auto_confirms_after_the_timeout(self):
        self.assertEqual("yes", laintas_cli._read_single_key_choice(
            allow_always=False, auto_confirm_seconds=0.2))


if __name__ == "__main__":
    unittest.main()
