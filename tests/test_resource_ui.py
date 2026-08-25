import os
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import resource_ui


class RecordingOutput(DummyOutput):
    def __init__(self):
        self.data = []

    def write(self, data):
        self.data.append(data)

    def write_raw(self, data):
        self.data.append(data)


class ResourceBrowserModelTests(unittest.TestCase):
    def _browser(self, items=None, detail=None, **kwargs):
        rows = items or [
            resource_ui.UIItem("a", "Alpha", "first", payload=1),
            resource_ui.UIItem("b", "Beta", "second", payload=2),
        ]
        return resource_ui.ResourceBrowser(
            title="Resources", load_items=lambda: rows,
            load_detail=detail or (
                lambda item: resource_ui.UIDetail.text(item.title, item.subtitle)),
            **kwargs)

    def test_fuzzy_match_is_case_insensitive_subsequence(self):
        self.assertTrue(resource_ui.fuzzy_match("Mode Manager", "mdmgr"))
        self.assertFalse(resource_ui.fuzzy_match("Mode Manager", "xyz"))

    def test_palette_matches_main_cli_green_red_and_violet_tokens(self):
        color = lambda name: resource_ui._STYLE.get_attrs_for_style_str(
            "class:" + name).color
        self.assertEqual(color("header.brand"), "4ade80")
        self.assertEqual(color("list.marker"), "3fb950")
        self.assertEqual(color("error"), "f85149")
        self.assertEqual(color("timeline.user"), "d2a8ff")
        self.assertEqual(color("timeline.tool"), "a78bfa")
        self.assertEqual(color("assistant.prompt"), "a78bfa")

    def test_optional_assistant_returns_detail_without_blocking_caller(self):
        finished = threading.Event()

        def assistant(item, detail, prompt, interrupt_event):
            self.assertEqual(item.key, "a")
            self.assertEqual(prompt, "translate this")
            self.assertFalse(interrupt_event.is_set())
            finished.set()
            return resource_ui.UIDetail.text("Translation", "translated")

        browser = self._browser(assistant_handler=assistant)
        browser.reload(preserve=False)
        browser.assistant.text = "translate this"
        browser._submit_assistant()
        self.assertTrue(finished.wait(1))
        for _ in range(100):
            if not browser.assistant_busy:
                break
            threading.Event().wait(0.005)
        self.assertEqual(browser.detail.title, "Translation")
        self.assertEqual(browser.detail.lines[0].text, "translated")

    def test_reload_preserves_selected_stable_key(self):
        browser = self._browser()
        browser.reload(preserve=False)
        browser.selected = 1
        browser._sync_detail()
        browser.reload(preserve=True)
        self.assertEqual(browser._selected_item().key, "b")

    def test_detail_is_lazy_and_cached(self):
        loader = mock.Mock(
            side_effect=lambda item: resource_ui.UIDetail.text(item.title, "body"))
        browser = self._browser(detail=loader)
        browser.mode = "detail"
        browser.reload(preserve=False)
        browser._sync_detail()
        browser._sync_detail()
        self.assertEqual(loader.call_count, 1)

    def test_search_filters_title_subtitle_and_hidden_search_text(self):
        browser = self._browser(items=[
            resource_ui.UIItem("a", "Alpha", "first", search_text="needle"),
            resource_ui.UIItem("b", "Beta", "second"),
        ])
        browser.reload(preserve=False)
        browser.search.text = "ndl"
        self.assertEqual([item.key for item in browser.filtered], ["a"])

    def test_list_search_highlights_visible_fuzzy_match(self):
        browser = self._browser(items=[
            resource_ui.UIItem("a", "Alpha target", "first result"),
        ])
        browser.reload(preserve=False)
        browser.search_scope = "list"
        browser.search.text = "tgt"
        fragments = browser._list_fragments()
        highlighted = "".join(
            text for style, text in fragments
            if style == "class:search.match")
        self.assertEqual(highlighted.casefold(), "tgt")

    def test_detail_search_indexes_highlights_and_locates_matches(self):
        lines = [resource_ui.UILine(f"line {index}") for index in range(30)]
        lines[4] = resource_ui.UILine("first Needle here")
        lines[24] = resource_ui.UILine("second needle here")
        browser = self._browser(detail=lambda item: resource_ui.UIDetail(
            item.title, lines=lines, kind="document"),
            presentation="document")
        with mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 12))):
            browser.mode = "detail"
            browser.reload(preserve=False)
            browser.search_scope = "detail"
            browser.search.text = "needle"
            self.assertEqual(len(browser._detail_matches), 2)
            self.assertEqual(browser._detail_match_index, 0)
            first_scroll = browser.detail_scroll
            browser._jump_detail_match(1)
            self.assertGreater(browser.detail_scroll, first_scroll)
            fragments = browser._detail_fragments()
        current = "".join(
            text for style, text in fragments
            if style == "class:search.match.current")
        self.assertEqual(current.casefold(), "needle")

    def test_action_runs_in_place_and_refreshes(self):
        rows = [resource_ui.UIItem("a", "Alpha")]

        def remove(item):
            rows.clear()
            return resource_ui.UIActionResult(
                message="Removed", refresh=True)

        browser = self._browser(
            items=rows,
            actions=[resource_ui.UIAction("x", "delete", "Delete", remove)])
        browser.reload(preserve=False)
        browser._execute_action(browser.actions[0])
        self.assertEqual(browser.items, [])
        self.assertEqual(browser.status, "Removed")

    def test_refresh_invalidates_current_detail_not_just_cache(self):
        state = {"value": "before"}
        rows = [resource_ui.UIItem("a", "Alpha")]

        def change(_item):
            state["value"] = "after"
            return resource_ui.UIActionResult(message="Changed", refresh=True)

        browser = self._browser(
            items=rows,
            detail=lambda item: resource_ui.UIDetail.text(
                item.title, state["value"]),
            actions=[resource_ui.UIAction("t", "toggle", "Toggle", change)])
        browser.mode = "detail"
        browser.reload(preserve=False)
        self.assertEqual(browser.detail.lines[0].text, "before")
        browser._execute_action(browser.actions[0])
        self.assertEqual(browser.detail.lines[0].text, "after")

    def test_footer_never_exceeds_terminal_width(self):
        browser = self._browser(actions=[
            resource_ui.UIAction("t", "toggle", "Toggle"),
            resource_ui.UIAction("l", "load", "Load"),
            resource_ui.UIAction("u", "unload", "Unload"),
            resource_ui.UIAction("r", "reload", "Reload"),
        ])
        with mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((80, 24))):
            rendered = "".join(value for _style, value in browser._footer_fragments())
            self.assertLessEqual(len(rendered), 79)
            browser.status = "A very long operation status " * 10
            rendered = "".join(value for _style, value in browser._footer_fragments())
            self.assertLessEqual(len(rendered), 79)
            browser.status = "操作已经完成，正在刷新详情" * 10
            rendered = "".join(value for _style, value in browser._footer_fragments())
            self.assertLessEqual(resource_ui.display_width(rendered), 79)
            browser.status = ""
            browser.mode = "detail"
            rendered = "".join(value for _style, value in browser._footer_fragments())
            self.assertLessEqual(resource_ui.display_width(rendered), 79)

    def test_primary_action_can_return_selected_payload(self):
        browser = self._browser(primary_action="resume", primary_label="Resume")
        browser.reload(preserve=False)
        browser.app.exit = mock.Mock()
        browser._primary()
        outcome = browser.app.exit.call_args.kwargs["result"]
        self.assertEqual(outcome.action, "resume")
        self.assertEqual(outcome.value, 1)

    def test_global_action_works_when_list_is_empty(self):
        browser = self._browser(
            items=[], actions=[resource_ui.UIAction(
                "n", "new", "New", allow_empty=True)])
        browser.load_items = lambda: []
        browser.reload(preserve=False)
        browser.app.exit = mock.Mock()
        browser._execute_action(browser.actions[0])
        outcome = browser.app.exit.call_args.kwargs["result"]
        self.assertEqual(outcome.action, "new")
        self.assertIsNone(outcome.item)

    def test_mouse_click_selects_row_and_opens_narrow_detail(self):
        browser = self._browser()
        browser.reload(preserve=False)
        mouse = SimpleNamespace(
            event_type=resource_ui.MouseEventType.MOUSE_UP,
            position=SimpleNamespace(y=1))
        with mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 24))):
            browser._list_mouse(mouse)
        self.assertEqual(browser._selected_item().key, "b")
        self.assertEqual(browser.mode, "detail")

    def test_presentations_have_distinct_list_density(self):
        operations = self._browser(presentation="operations")
        document = self._browser(presentation="document")
        timeline = self._browser(presentation="timeline")
        for browser in (operations, document, timeline):
            browser.reload(preserve=False)
        self.assertEqual(operations._list_row_height(), 1)
        self.assertEqual(document._list_row_height(), 2)
        self.assertEqual(timeline._list_row_height(), 2)
        operations_text = "".join(text for _, text in operations._list_fragments())
        timeline_text = "".join(text for _, text in timeline._list_fragments())
        self.assertIn("Alpha  —  first", operations_text)
        self.assertIn("│ first", timeline_text)

    def test_timeline_renders_transcript_but_leaves_diff_exact(self):
        browser = self._browser(presentation="timeline")
        browser.detail = resource_ui.UIDetail(
            "Conversation", lines=[
                resource_ui.UILine("AI REPLY 1", "class:detail.heading"),
                resource_ui.UILine("complete answer"),
                resource_ui.UILine("TOOL · Edit", "class:detail.heading"),
            ], kind="timeline")
        rendered = "".join(text for _, text in browser._detail_fragments())
        self.assertIn("● AI REPLY 1", rendered)
        self.assertIn("│  complete answer", rendered)
        self.assertIn("├ TOOL · Edit", rendered)

        browser.detail = resource_ui.UIDetail(
            "sample.py", lines=[
                resource_ui.UILine(" unchanged"),
                resource_ui.UILine("-old", "class:detail.delete"),
                resource_ui.UILine("+new", "class:detail.add"),
            ], kind="diff")
        rendered = "".join(text for _, text in browser._detail_fragments())
        self.assertEqual(rendered, "  unchanged\n -old\n +new\n")

    def test_section_navigation_wraps_across_headings_and_changes(self):
        browser = self._browser(presentation="document")
        browser.detail = resource_ui.UIDetail(
            "Manual", lines=[
                resource_ui.UILine("First", "class:detail.heading"),
                resource_ui.UILine("body"),
                resource_ui.UILine("Second", "class:detail.heading"),
            ], kind="document")
        browser._jump_detail_anchor(True)
        self.assertEqual(browser.detail_scroll, 2)
        browser._jump_detail_anchor(True)
        self.assertEqual(browser.detail_scroll, 0)
        browser.detail = resource_ui.UIDetail(
            "Diff", lines=[
                resource_ui.UILine("same"),
                resource_ui.UILine("-old", "class:detail.delete"),
                resource_ui.UILine("+new", "class:detail.add"),
            ], kind="diff")
        browser.detail_scroll = 0
        browser._jump_detail_anchor(True)
        self.assertEqual(browser.detail_scroll, 1)

    def test_custom_pane_headers_report_focus_and_detail_title(self):
        browser = self._browser(
            presentation="document", pane_labels=("INDEX", "SOURCE"))
        with mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((140, 40))):
            browser.reload(preserve=False)
            left = browser._pane_header_fragments("list")
            right = browser._pane_header_fragments("detail")
        self.assertEqual(left[0][0], "class:pane.header.focus")
        self.assertIn("INDEX  2", left[0][1])
        self.assertIn("SOURCE  Alpha", right[0][1])

    def test_real_event_loop_enters_detail_then_quits(self):
        loaded = []
        with create_pipe_input() as pipe:
            browser = resource_ui.ResourceBrowser(
                title="Interactive",
                load_items=lambda: [resource_ui.UIItem("a", "Alpha")],
                load_detail=lambda item: (
                    loaded.append(item.key)
                    or resource_ui.UIDetail.text(item.title, "full detail")),
                input=pipe, output=DummyOutput())
            # Enter opens the narrow detail page; q then closes the app.
            pipe.send_text("\rq")
            outcome = browser.run()
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(loaded, ["a"])
        self.assertEqual(browser.mode, "detail")

    def test_real_event_loop_filters_and_runs_explicit_action(self):
        rows = [
            resource_ui.UIItem("a", "Alpha", payload=1),
            resource_ui.UIItem("b", "Beta target", payload=2),
        ]
        with create_pipe_input() as pipe:
            browser = resource_ui.ResourceBrowser(
                title="Pick", load_items=lambda: rows,
                load_detail=lambda item: resource_ui.UIDetail.text(
                    item.title, "selected detail"),
                actions=[resource_ui.UIAction("r", "resume", "Resume")],
                input=pipe, output=DummyOutput())
            # Finish search, open the detail page, then act from the detail.
            pipe.send_text("/target\r\rr")
            outcome = browser.run()
        self.assertEqual(outcome.action, "resume")
        self.assertEqual(outcome.value, 2)

    def test_narrow_detail_search_escape_restores_visible_detail_focus(self):
        output = RecordingOutput()
        with create_pipe_input() as pipe, mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 24))):
            browser = resource_ui.ResourceBrowser(
                title="Trace", presentation="timeline",
                load_items=lambda: [resource_ui.UIItem("a", "Alpha trace")],
                load_detail=lambda item: resource_ui.UIDetail.text(
                    item.title, "complete evidence"),
                input=pipe, output=output)
            # Open detail, search while its narrow pane is active, then cancel
            # search. The detail pane—not the hidden list—must regain focus.
            pipe.send_text("\r/Alpha\x1bq")
            outcome = browser.run()
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(browser.mode, "detail")
        self.assertEqual(browser.focus, "detail")
        self.assertNotIn("Unhandled exception", "".join(output.data))

    def test_narrow_detail_search_enter_restores_visible_detail_focus(self):
        output = RecordingOutput()
        with create_pipe_input() as pipe, mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 24))):
            browser = resource_ui.ResourceBrowser(
                title="Trace", presentation="timeline",
                load_items=lambda: [resource_ui.UIItem("a", "Alpha trace")],
                load_detail=lambda item: resource_ui.UIDetail.text(
                    item.title, "complete evidence"),
                input=pipe, output=output)
            pipe.send_text("\r/Alpha\rq")
            outcome = browser.run()
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(browser.mode, "detail")
        self.assertEqual(browser.focus, "detail")
        self.assertNotIn("Unhandled exception", "".join(output.data))

    def test_real_detail_search_locates_next_match_without_focus_error(self):
        output = RecordingOutput()
        lines = [resource_ui.UILine(f"line {index}") for index in range(40)]
        lines[2] = resource_ui.UILine("needle first")
        lines[32] = resource_ui.UILine("needle second")
        with create_pipe_input() as pipe, mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 16))):
            browser = resource_ui.ResourceBrowser(
                title="Trace", presentation="timeline",
                load_items=lambda: [resource_ui.UIItem("a", "Alpha trace")],
                load_detail=lambda item: resource_ui.UIDetail(
                    item.title, lines=lines, kind="timeline"),
                input=pipe, output=output)
            # Open detail, find the first hit, jump to the second, clear the
            # retained search, return to the list, then quit.
            pipe.send_text("\r/needle\rn\x1b\x1bq")
            outcome = browser.run()
        self.assertEqual(outcome.action, "cancel")
        self.assertGreater(browser.detail_scroll, 20)
        self.assertEqual(browser.mode, "list")
        self.assertNotIn("Unhandled exception", "".join(output.data))

    def test_j_navigates_and_help_never_opens_an_unselected_resource(self):
        loaded = []
        with create_pipe_input() as pipe, mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 24))):
            browser = resource_ui.ResourceBrowser(
                title="Transcript", presentation="timeline",
                load_items=lambda: [
                    resource_ui.UIItem("a", "First"),
                    resource_ui.UIItem("b", "Second"),
                ],
                load_detail=lambda item: (
                    loaded.append(item.key)
                    or resource_ui.UIDetail(
                        item.title,
                        lines=[resource_ui.UILine(
                            "AI REPLY", "class:detail.heading")],
                        kind="timeline")),
                input=pipe, output=DummyOutput())
            # j only moves; Enter is the explicit read. Help opens and closes
            # without switching the selected record or invoking the loader.
            pipe.send_text("j\r]?\x1b\x1bq")
            outcome = browser.run()
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(browser._selected_item().key, "b")
        self.assertEqual(loaded, ["b"])
        self.assertFalse(browser.help_open)

    def test_wide_and_narrow_modes_share_same_state(self):
        browser = self._browser()
        browser.reload(preserve=False)
        with mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((140, 40))):
            self.assertTrue(browser.is_wide)
            self.assertIn("Alpha", "".join(text for _, text in browser._list_fragments()))
        with mock.patch.object(
                resource_ui.shutil, "get_terminal_size",
                return_value=os.terminal_size((70, 24))):
            self.assertFalse(browser.is_wide)
            browser.mode = "detail"
            self.assertEqual(browser.mode, "detail")


if __name__ == "__main__":
    unittest.main()
