"""Command discovery and shared picker regressions from the TUI audit."""
import io
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest import mock

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

import laintas_cli as cli
import resource_ui
import terminal_theme
import hwo_ui


class CommandDiscoveryTests(unittest.TestCase):
    def test_help_flag_does_not_intercept_tool_or_quoted_arguments(self):
        for command in ('/bash echo --help', '/tool shell.exec --help',
                        '/name "--help"', '/fork name --help', 'help --help',
                        'model --help'):
            self.assertEqual(cli._slash_help_target(command), "")

    def test_extension_help_and_palette_include_registered_commands(self):
        runtime = mock.Mock()
        runtime.command_names.return_value = ["/canvas"]
        runtime.command_description.return_value = "Draw a whiteboard"
        runtime.command_subcommands.return_value = [("list", "List boards")]
        with mock.patch.object(cli.extension_runtime, "get_runtime", return_value=runtime), \
                mock.patch.object(cli, "select_dialog", side_effect=lambda rows, **kw: rows[-1]):
            self.assertEqual(cli.show_command_palette(), "/canvas")
            self.assertEqual(cli._slash_help_target("/canvas --help"), "/canvas")
            output = io.StringIO()
            with mock.patch.object(cli, "console", Console(file=output)):
                cli.handle_meta_command("/canvas --help", mock.Mock(), {})
                cli.show_help("CANVAS")
            self.assertIn("List boards", output.getvalue())
            self.assertNotIn("Unknown command", output.getvalue())
        runtime.invoke_command.assert_not_called()

    def test_help_flags_for_every_command_and_alias_have_no_side_effects(self):
        for spec in cli.COMMAND_SPECS:
            for name in spec.all_names:
                for flag in ("--help", "-h"):
                    with self.subTest(command=name, flag=flag):
                        output = io.StringIO()
                        with mock.patch.object(cli, "console", Console(
                                file=output, width=160, theme=cli.LAINTAS_THEME)), \
                                mock.patch.object(cli, "_validate_slash_args") as validate:
                            self.assertFalse(cli.handle_meta_command(
                                f"{name} {flag}", mock.Mock(), {}))
                        validate.assert_not_called()
                        self.assertIn(spec.description, output.getvalue())

    def test_palette_returns_a_command_without_display_markup(self):
        for row in (0, 15, len(cli._COMMANDS) - 1):
            with self.subTest(row=row), mock.patch.object(
                    cli, "select_dialog", side_effect=lambda items, **kw: items[row]):
                self.assertEqual(cli.show_command_palette(), cli._COMMANDS[row][0])

    def test_detailed_help_lists_each_declared_subcommand(self):
        for spec in cli.COMMAND_SPECS:
            with self.subTest(command=spec.name):
                output = io.StringIO()
                with mock.patch.object(cli, "console", Console(
                        file=output, width=200, theme=cli.LAINTAS_THEME)):
                    cli.show_help(spec.name)
                for item in spec.contextual_completions:
                    self.assertIn(item.value, output.getvalue())
                    self.assertIn(item.description, output.getvalue())


class PickerInteractionTests(unittest.TestCase):
    def run_picker(self, items, steps, **kwargs):
        stop = threading.Event()
        with create_pipe_input() as pipe:
            def feed():
                for delay, text in steps:
                    if stop.wait(delay):
                        return
                    pipe.send_text(text)
                # A regression must fail a test instead of hanging the suite.
                if not stop.wait(1):
                    pipe.send_text("\x03")

            worker = threading.Thread(target=feed)
            worker.start()
            try:
                with create_app_session(input=pipe, output=DummyOutput()):
                    return cli.select_dialog(
                        items, refresh_interval=0.02, **kwargs)
            finally:
                stop.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())

    def test_filter_cannot_confirm_a_hidden_selection(self):
        result = self.run_picker(
            ["alpha", "hidden", "alpine"], [(0.35, "al\r")],
            selected_index=1, search=True, full_screen=False)
        self.assertEqual(result, "alpha")

    def test_search_includes_descriptions(self):
        rows = [("model-a", "provider-a"), ("model-b", "provider-b")]
        result = self.run_picker(rows, [(0.35, "provider-b\r")],
                                 search=True, full_screen=False)
        self.assertEqual(result, rows[1])

    def test_enter_with_no_matches_keeps_filter_editable(self):
        result = self.run_picker(
            ["alpha", "beta"],
            [(0.35, "zzzz\r"), (0.1, "\x7f" * 4 + "alpha\r")],
            search=True, full_screen=False)
        self.assertEqual(result, "alpha")

    def test_searchable_actions_do_not_consume_typing(self):
        result = self.run_picker(
            ["other", "dev"], [(0.35, "dev\r")],
            search=True, action_keys={"d": "delete"}, enter_action="open",
            full_screen=False)
        self.assertEqual(result, ("open", 1))

    def test_searchable_actions_use_alt_shortcuts(self):
        result = self.run_picker(
            ["other", "dev"], [(0.35, "dev"), (0.1, "\x1bd")],
            search=True, action_keys={"d": "delete"}, full_screen=False)
        self.assertEqual(result, ("delete", 1))

    def test_navigation_stops_automatic_confirmation(self):
        result = self.run_picker(
            ["Yes", "No"], [(0.35, "\x1b[B"), (0.3, "\r")],
            auto_confirm_seconds=0.5, auto_confirm_index=0, full_screen=False)
        self.assertEqual(result, "No")

    def test_page_down_moves_by_a_visible_page(self):
        with mock.patch.object(cli, "_get_term_size", return_value=(80, 12)):
            result = self.run_picker(
                [str(i) for i in range(50)], [(0.35, "\x1b[6~\r")])
        self.assertNotEqual(result, "0")


class PickerTextTests(unittest.TestCase):
    def test_workflow_studio_keeps_escape_visible_at_narrow_widths(self):
        for mode in (hwo_ui.StudioMode.NAVIGATION, hwo_ui.StudioMode.RUNNING,
                     hwo_ui.StudioMode.RESULT, hwo_ui.StudioMode.INSPECTOR,
                     hwo_ui.StudioMode.HELP):
            for width in (24, 38, 50, 60):
                with self.subTest(mode=mode, width=width):
                    text = "".join(value for _, value in hwo_ui._navigation_hints(
                        mode, width=width))
                    self.assertIn("Esc", text)
                    self.assertTrue(all(len(line) < width for line in text.splitlines()))

    def test_no_color_applies_to_interactive_chrome(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(cli._ui_theme_mode(), "mono")
            fragments = cli._ptk_fragments(
                [("class:selected", "[cyan]value[/cyan]")], color_mode="mono")
            self.assertFalse(any("#" in style for style, _ in fragments))

    def test_marquee_fits_wide_text_and_preserves_style(self):
        with mock.patch.object(cli, "_get_term_size", return_value=(18, 24)):
            markup = cli._apply_marquee(
                " › [cyan]\u4e2d\u6587\u6587\u4ef6\u540d\u4e2d\u6587\u6587\u4ef6\u540d[/cyan]", 0, [-1], [0.0], 3)
        text = Text.from_markup(markup)
        self.assertLessEqual(text.cell_len, 17)
        self.assertTrue(text.spans)

    def test_picker_styles_follow_light_and_mono_themes(self):
        for mode in ("dark", "light", "mono"):
            with self.subTest(mode=mode), mock.patch.object(
                    cli, "_ui_theme_mode", return_value=mode):
                style = cli._build_dialog_style()
                focus = style.get_attrs_for_style_str("class:selected")
                self.assertTrue(focus.bold)
                if mode == "mono":
                    self.assertTrue(focus.reverse)
                    self.assertFalse(focus.color)
                    self.assertFalse(focus.bgcolor)
                elif mode == "light":
                    self.assertEqual(focus.color, "24292f")

    def test_resource_themes_and_narrow_footer(self):
        for mode in ("light", "mono"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ), \
                    mock.patch.object(terminal_theme, "_active", mode):
                os.environ.pop("NO_COLOR", None)
                style = resource_ui._browser_style()
                attrs = style.get_attrs_for_style_str("class:list.selected")
                if mode == "mono":
                    self.assertTrue(attrs.reverse)
                    for key, _ in style.style_rules:
                        attrs = style.get_attrs_for_style_str("class:" + key)
                        self.assertFalse(attrs.color)
                        self.assertFalse(attrs.bgcolor)
                else:
                    self.assertEqual(attrs.color, "24292f")
        browser = resource_ui.ResourceBrowser(
            title="Test", load_items=lambda: [],
            load_detail=lambda item: resource_ui.UIDetail.text("Test", ""))
        for width in (18, 24, 40, 80):
            with self.subTest(width=width), mock.patch.object(
                    resource_ui.shutil, "get_terminal_size",
                    return_value=os.terminal_size((width, 10))):
                text = "".join(value for _, value in browser._footer_fragments())
                self.assertIn("Esc", text)
                self.assertLessEqual(resource_ui.display_width(text), width - 1)


class TerminalExitTests(unittest.TestCase):
    def test_subterminal_sigterm_restores_the_shell(self):
        root = Path(cli.__file__).parent
        with tempfile.TemporaryDirectory(prefix="tui-exit-") as directory:
            master, slave = pty.openpty()
            termios.tcsetwinsize(slave, (24, 100))
            before = termios.tcgetattr(slave)
            env = dict(os.environ, LAINTAS_HOME=directory + "/state",
                       LAINTAS_BACKEND="http://127.0.0.1:1", TERM="xterm-256color",
                       LAINTAS_ACCOUNT_MODE="anonymous", LAINTAS_ACCOUNT_ID="",
                       LAINTAS_TERMINAL_ID="exit-test", PYTHONDONTWRITEBYTECODE="1")
            process = subprocess.Popen(
                [sys.executable, str(root / "laintas_cli.py"), "--depth", "1",
                 "--terminal-name", "exit-test", "--theme", "dark"],
                cwd=directory, env=env, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True)
            received = bytearray()
            try:
                deadline = time.monotonic() + 20
                # The agent label is intentionally hidden when a long cwd
                # leaves little room. Bracketed paste starts with the prompt.
                ready = b"\x1b[?2004h"
                while ready not in received and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        data = os.read(master, 65536)
                        received.extend(data)
                        if b"\x1b[6n" in data:
                            os.write(master, b"\x1b[1;1R")
                    if process.poll() is not None:
                        break
                self.assertIn(ready, received)
                process.send_signal(signal.SIGTERM)
                process.wait(8)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(termios.tcgetattr(slave), before)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(5)
                os.close(master)
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
