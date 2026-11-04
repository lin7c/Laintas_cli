"""Responsive output and interaction-state regressions for the terminal UI."""
import io
import os
from pathlib import Path
import tempfile
import threading
from prompt_toolkit.application import create_app_session
import unittest
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

import laintas_cli as cli
import resource_ui as ui
import retask_view
from terminal_layout import AdaptiveTable, fit_hints, wrap_fragments


class ResponsiveTableTests(unittest.TestCase):
    def render(self, table, width):
        stream = io.StringIO()
        Console(file=stream, width=width).print(table)
        output = stream.getvalue()
        self.assertTrue(all(cell_len(line) <= width for line in output.splitlines()))
        return output

    def test_narrow_records_preserve_fields_and_literal_values(self):
        table = AdaptiveTable(title="Saved objects")
        for heading in ["", "Path", "Size", "Description"]:
            table.add_column(heading)
        table.add_row("*", Text("folder/[red]/notes.md"), "42 bytes",
                      "A long description that must remain available after reflow.")
        for width in [24, 38, 50, 80, 120]:
            with self.subTest(width=width):
                out = self.render(table, width)
                self.assertIn("42 bytes", out)
                self.assertIn("[red]", out)
                self.assertIn("reflow.", out)

    def test_task_subject_leads_when_agent_column_is_blank(self):
        table = AdaptiveTable()
        for heading in ["AGENT", "ID", "TASK", "STATUS"]:
            table.add_column(heading)
        table.add_row("", "t2", "Verify the release", "pending")
        out = self.render(table, 38)
        self.assertTrue(out.startswith("Verify the release"))
        self.assertIn("t2", out)
        self.assertIn("pending", out)

    def test_headerless_layout_grid_retains_its_horizontal_relationship(self):
        table = AdaptiveTable(show_header=False, box=None)
        table.add_row("Allowance", "120", "remaining")
        self.assertIn("Allowance", self.render(table, 38).splitlines()[0])
        self.assertIn("120", self.render(table, 38).splitlines()[0])

    def test_hints_keep_complete_actions_and_expose_overflow(self):
        parts = [("Esc", "Back"), ("Enter", "Open"), ("d", "Delete"), ("/", "Find")]
        out = "".join(text for _, text in fit_hints(parts, 38))
        self.assertIn("Esc Back", out)
        self.assertIn("? More", out)
        self.assertNotIn("d Del", out)
        self.assertLess(cell_len(out), 38)

    def test_wrapping_preserves_text_styles_and_wide_cells(self):
        original = "\u4e2d\u6587" * 12 + " finished"
        rows = wrap_fragments([("class:detail", original)], 13)
        self.assertEqual("".join(text for row in rows for _, text in row), original)
        self.assertTrue(all(cell_len("".join(text for _, text in row)) <= 13 for row in rows))

    def test_wrapped_suffix_is_rechecked_before_appending_a_wide_character(self):
        for text in [" aaaaaaaaa\u4e2d", "start aaaaaaaaa\u4e2d"]:
            rows = wrap_fragments([("class:detail", text)], 10)
            self.assertEqual("".join(value for row in rows for _, value in row), text)
            self.assertTrue(all(cell_len("".join(value for _, value in row)) <= 10 for row in rows))
        rows = wrap_fragments([("", "\u4e2d")], 1)
        self.assertTrue(all(cell_len("".join(value for _, value in row)) <= 1 for row in rows))


class ResourceStateTests(unittest.TestCase):
    def browser(self, **kwargs):
        return ui.ResourceBrowser(
            title="Records", load_items=lambda: [ui.UIItem("a", "Alpha"), ui.UIItem("b", "Beta")],
            load_detail=lambda item: ui.UIDetail.text(item.title, "original"),
            **kwargs)

    def test_help_suppresses_underlying_mutation_keys(self):
        delete = mock.Mock()
        with create_pipe_input() as pipe:
            browser = self.browser(actions=[ui.UIAction("d", "delete", "Delete", delete)],
                                   input=pipe, output=DummyOutput())
            pipe.send_text("?d\x1bq")
            browser.run()
        delete.assert_not_called()

    def test_cancelled_assistant_cannot_replace_detail_after_completion(self):
        started, release = threading.Event(), threading.Event()

        def assistant(*_args):
            started.set()
            release.wait(2)
            return ui.UIDetail.text("Late response", "must not appear")

        browser = self.browser(assistant_handler=assistant)
        browser.mode = "detail"
        browser.reload()
        browser.assistant.text = "inspect"
        browser._submit_assistant()
        try:
            self.assertTrue(started.wait(1))
            browser._assistant_interrupt.set()
        finally:
            release.set()
            browser._assistant_thread.join(2)
        self.assertFalse(browser._assistant_thread.is_alive())
        self.assertEqual(browser.detail.title, "Alpha")
        self.assertIn("cancelled", browser.status)

    def test_response_for_old_selection_does_not_replace_current_context(self):
        started, release = threading.Event(), threading.Event()

        def assistant(*_args):
            started.set()
            release.wait(2)
            return ui.UIDetail.text("Alpha response", "answer")

        browser = self.browser(assistant_handler=assistant)
        browser.mode = "detail"
        browser.reload()
        browser.assistant.text = "inspect"
        browser._submit_assistant()
        try:
            self.assertTrue(started.wait(1))
            browser._move_selection(1)
        finally:
            release.set()
            browser._assistant_thread.join(2)
        self.assertEqual(browser.detail.title, "Beta")
        self.assertEqual(browser.detail_cache["a"].title, "Alpha response")

    def test_operation_failure_and_refresh_survive_a_selection_change(self):
        started, release = threading.Event(), threading.Event()
        def command(*_args):
            started.set()
            release.wait(2)
            return ui.UIActionResult(message="Assignment failed: capacity exceeded",
                                     message_style="class:error", refresh=True)
        browser = self.browser(assistant_handler=command, assistant_label="Command")
        browser.mode = "detail"
        browser.reload()
        browser.assistant.text = "task inspect"
        with mock.patch.object(browser, "reload", wraps=browser.reload) as reload:
            browser._submit_assistant()
            try:
                self.assertTrue(started.wait(1))
                browser._move_selection(1)
            finally:
                release.set()
                browser._assistant_thread.join(2)
            reload.assert_called_once()
        self.assertEqual(browser.status, "Assignment failed: capacity exceeded")
        self.assertEqual(browser.status_style, "class:error")
        self.assertEqual(browser._selected_item().key, "b")

    def test_requested_source_line_is_mapped_after_wrapping(self):
        with mock.patch.object(ui.shutil, "get_terminal_size", return_value=os.terminal_size((38, 10))):
            browser = self.browser()
            detail = ui.UIDetail.text("Trace", "long " * 55 + "\nsecond\nTARGET")
            detail.start_line = 2
            browser.load_detail = lambda _: detail
            browser.mode = "detail"
            browser.reload()
            self.assertGreater(browser.detail_scroll, 2)
            self.assertEqual(browser.detail_scroll, browser._detail_position(2))
            action = ui.UIAction("v", "view", "View", lambda _: ui.UIActionResult(detail=detail))
            browser._execute_action(action)
            self.assertEqual(browser.detail_scroll, browser._detail_position(2))

    def test_wrapped_document_can_scroll_to_the_end_of_a_single_long_line(self):
        with mock.patch.object(ui.shutil, "get_terminal_size", return_value=os.terminal_size((38, 10))):
            browser = self.browser()
            browser.mode = "detail"
            browser.reload()
            browser.detail = ui.UIDetail.text("Long line", "begin " + "value " * 120 + "THE END")
            browser._move_detail(1000)
            text = "".join(value for _, value in browser._detail_fragments())
            self.assertIn("THE END", text)
            self.assertTrue(all(cell_len(line) < 38 for line in text.splitlines()))

    def test_command_input_is_labelled_as_a_command(self):
        with mock.patch.object(ui.shutil, "get_terminal_size", return_value=os.terminal_size((160, 30))):
            browser = self.browser(assistant_handler=lambda *_: "", assistant_label="Command")
            browser.reload()
            text = "".join(value for _, value in browser._footer_fragments())
        self.assertIn("a Command", text)
        self.assertNotIn("Ask AI", text)

    def test_detail_cache_tracks_the_rendering_kind(self):
        browser = self.browser(presentation="timeline")
        lines = [ui.UILine("USER", "class:detail.heading"), ui.UILine("message")]
        browser.detail = ui.UIDetail("Event", lines=lines, kind="timeline")
        self.assertIn("● USER", "".join(value for _, value in browser._detail_fragments()))
        browser.detail = ui.UIDetail("Event", lines=lines, kind="diff")
        rendered = "".join(value for _, value in browser._detail_fragments())
        self.assertIn("USER", rendered)
        self.assertNotIn("● USER", rendered)

    def test_detail_cache_tracks_collapsibility(self):
        browser = self.browser(collapsible_detail=True)
        browser.detail = ui.UIDetail("Event", lines=[
            ui.UILine("Section", "class:detail.heading"), ui.UILine("section body")])
        self.assertIn("section body", "".join(value for _, value in browser._detail_fragments()))
        browser.detail.collapsible = True
        self.assertNotIn("section body", "".join(value for _, value in browser._detail_fragments()))

    def test_resize_keeps_the_focused_inspector_visible_in_single_pane_mode(self):
        with mock.patch.object(ui.shutil, "get_terminal_size", return_value=os.terminal_size((120, 30))):
            browser = self.browser()
            browser.reload()
            browser._focus_pane("detail")
        with mock.patch.object(ui.shutil, "get_terminal_size", return_value=os.terminal_size((50, 16))):
            browser._resize_focus(browser.app)
            self.assertEqual(browser.mode, "detail")
            self.assertEqual(browser.app.layout.current_window, browser.detail_window)


class LiteralOutputTests(unittest.TestCase):
    def test_cwd_preserves_bracketed_directory_names(self):
        stream = io.StringIO()
        with mock.patch.object(cli.os, "getcwd", return_value="/workspace/[red]/project"), \
                mock.patch.object(cli, "console", Console(file=stream)):
            cli._cmd_cwd()
        self.assertIn("/workspace/[red]/project", stream.getvalue())


class ChecklistLayoutTests(unittest.TestCase):
    def test_narrow_checklist_uses_the_primary_width_and_manual_scroll_stays_put(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long.retask"
            path.write_text("retask 1\ntitle: Review\n## [>] t1 Inspect\n\n" + "description line\n" * 50)
            with mock.patch.object(retask_view.shutil, "get_terminal_size", return_value=os.terminal_size((50, 12))):
                viewer = retask_view.RetaskViewer(str(path), output=DummyOutput())
                self.assertGreater(viewer._body_width(), 40)
                viewer._body_fragments()
                viewer._scroll(12)
                viewer._body_fragments()
                self.assertGreater(viewer.scroll_top, 0)


class ShortPickerTests(unittest.TestCase):
    def test_short_terminal_retains_supply_labels_when_preview_is_hidden(self):
        class Capture(DummyOutput):
            def __init__(self):
                self.data = []
            def write(self, text):
                self.data.append(text)
            def write_raw(self, text):
                self.data.append(text)
        output = Capture()
        stop = threading.Event()
        items = [("same-model", "Supply A"), ("same-model", "Supply B")]
        with create_pipe_input() as pipe:
            def feed():
                if not stop.wait(.35):
                    pipe.send_text("\x1b[B\r")
                if not stop.wait(1):
                    pipe.send_text("\x03")
            thread = threading.Thread(target=feed)
            thread.start()
            try:
                with mock.patch.object(cli, "_get_term_size", return_value=(38, 10)), \
                        create_app_session(input=pipe, output=output):
                    result = cli.select_dialog(items, title="Models", search=True)
            finally:
                stop.set()
                thread.join(2)
        self.assertEqual(result, items[1])
        rendered = "".join(output.data)
        self.assertIn("Supply A", rendered)
        self.assertIn("Supply B", rendered)

