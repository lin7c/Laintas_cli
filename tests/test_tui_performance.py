"""Keep expensive terminal work bounded by changed state and visible rows."""
import asyncio
import unittest
from unittest import mock

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import laintas_cli as cli
import resource_ui as ui
from terminal_layout import iter_wrapped_fragments, slice_line


class DetailViewportTests(unittest.TestCase):
    def browser(self):
        browser = ui.ResourceBrowser(title="Records", load_items=lambda: [],
                                     output=DummyOutput())
        browser.detail = ui.UIDetail.text(
            "Log", "\n".join(f"line {index} " + "value " * 30 for index in range(10000)))
        return browser

    def test_first_frame_only_formats_visible_source_lines(self):
        browser = self.browser()
        with mock.patch.object(ui, "clean_ansi", wraps=ui.clean_ansi) as clean:
            browser._detail_fragments()
        self.assertLessEqual(clean.call_count, browser._body_height())
        self.assertEqual(len(browser._detail_row_sources), browser._body_height())
        self.assertLess(browser._detail_row_sources[-1], 100)

    def test_cached_frame_does_not_rescan_the_source_lines(self):
        class CountingLines(ui._DetailLines):
            visits = 0
            def __iter__(self):
                for line in super().__iter__():
                    self.visits += 1
                    yield line
        browser = self.browser()
        browser.detail.lines = CountingLines(browser.detail.lines)
        browser._detail_fragments()
        visits = browser.detail.lines.visits
        for _ in range(10):
            browser._detail_fragments()
        self.assertEqual(browser.detail.lines.visits, visits)

    def test_scrolling_extends_the_index_and_end_retains_the_tail(self):
        browser = self.browser()
        browser._detail_fragments()
        before = len(browser._detail_row_sources)
        browser._move_detail(20)
        browser._detail_fragments()
        self.assertGreater(len(browser._detail_row_sources), before)
        self.assertLess(len(browser._detail_row_sources), 200)
        browser._move_detail(len(browser._detail_rows()))
        self.assertIn("line 9999", "".join(value for _, value in browser._detail_fragments()))

    def test_cached_rows_observe_mutations_and_replacement(self):
        browser = self.browser()
        browser._detail_fragments()
        browser.detail.lines[0] = ui.UILine("UPDATED FIRST ROW")
        self.assertIn("UPDATED FIRST ROW", "".join(value for _, value in browser._detail_fragments()))
        browser.detail.lines = [ui.UILine("REPLACED CONTENT")]
        self.assertIn("REPLACED CONTENT", "".join(value for _, value in browser._detail_fragments()))

    def test_all_list_mutations_invalidate_the_row_cache(self):
        operations = [
            lambda lines: lines.append(ui.UILine("c")),
            lambda lines: lines.extend([ui.UILine("c")]),
            lambda lines: lines.insert(0, ui.UILine("c")),
            lambda lines: lines.pop(),
            lambda lines: lines.remove(ui.UILine("a")),
            lambda lines: lines.clear(),
            lambda lines: lines.reverse(),
            lambda lines: lines.sort(key=lambda line: line.text),
            lambda lines: lines.__setitem__(slice(None), [ui.UILine("c")]),
            lambda lines: lines.__delitem__(0),
            lambda lines: lines.__iadd__([ui.UILine("c")]),
            lambda lines: lines.__imul__(2),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                lines = ui.UIDetail.text("Log", "a\nb").lines
                version = lines.version
                operation(lines)
                self.assertIsNot(lines.version, version)

    def test_a_huge_line_is_consumed_only_as_far_as_the_first_row(self):
        class CountedText(str):
            visits = 0
            def __iter__(self):
                for char in super().__iter__():
                    self.visits += 1
                    yield char
        text = CountedText("word " * 200000)
        iterator = iter_wrapped_fragments([("class:detail", text)], 72)
        self.assertTrue(next(iterator))
        self.assertLessEqual(text.visits, 73)
        iterator.close()

    def test_diagram_viewport_does_not_scan_the_hidden_right_tail(self):
        class CountedText(str):
            visits = 0
            def __iter__(self):
                for char in super().__iter__():
                    self.visits += 1
                    yield char
        text = CountedText("x" * 100000)
        self.assertEqual(slice_line([("diagram", text)], 0, 40), [("diagram", "x" * 40)])
        self.assertLessEqual(text.visits, 41)

    def test_search_near_start_does_not_wrap_the_rest_of_a_huge_line(self):
        browser = self.browser()
        browser.detail = ui.UIDetail.text("Log", "NEEDLE " + "value " * 100000)
        browser._detail_query = "NEEDLE"
        browser._rebuild_detail_matches(reset=True)
        self.assertLessEqual(len(browser._detail_row_sources), browser._body_height())
        rendered = "".join(value for _, value in browser._detail_fragments())
        self.assertIn("NEEDLE", rendered)

    def test_search_indexes_matches_by_source_line(self):
        browser = self.browser()
        browser._detail_query = "line"
        browser._rebuild_detail_matches(reset=True)
        self.assertEqual(len(browser._detail_matches), 10000)
        self.assertEqual(len(browser._detail_matches_by_line[3]), 1)
        browser._detail_match_index = 3
        fragments = browser._detail_text_fragments(3, "line 3", "class:detail")
        self.assertEqual(fragments[0], ("class:search.match.current", "line"))


class PickerWorkTests(unittest.TestCase):
    def test_terminal_size_is_read_once_per_render_and_updates_next_frame(self):
        app = mock.Mock(is_running=True, render_counter=1)
        app._laintas_term_size = None
        app.output.get_size.side_effect = [Size(rows=30, columns=120), Size(rows=16, columns=50)]
        with mock.patch("prompt_toolkit.application.current.get_app_or_none", return_value=app):
            for _ in range(80):
                self.assertEqual(cli._get_term_size(), (120, 30))
            self.assertEqual(app.output.get_size.call_count, 1)
            app.render_counter += 1
            self.assertEqual(cli._get_term_size(), (50, 16))
        self.assertEqual(app.output.get_size.call_count, 2)

    def test_navigation_reuses_the_current_filtered_list(self):
        original = cli.Application
        with create_pipe_input() as pipe:
            frame = [0]
            class App(original):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.after_render += self.feed
                def feed(self, app):
                    frame[0] += 1
                    keys = {1: "\x1b[200~needle\x1b[201~", 2: "\x1b[B", 3: "\x1b[B", 4: "\x03"}
                    if frame[0] in keys:
                        pipe.send_text(keys[frame[0]])
            with create_app_session(input=pipe, output=DummyOutput()), \
                    mock.patch.object(cli, "Application", App), \
                    mock.patch.object(cli, "_get_term_size", return_value=(120, 30)), \
                    mock.patch.object(cli, "_fuzzy_match", wraps=cli._fuzzy_match) as match:
                items = [(f"Choice {i}", "needle supply") for i in range(300)]
                cli.select_dialog(items, title="Choices", search=True)
            self.assertEqual(match.call_count, len(items))


class EndNavigationTests(unittest.IsolatedAsyncioTestCase):
    def browser(self):
        browser = ui.ResourceBrowser(title="Log", load_items=lambda: [], output=DummyOutput())
        browser.detail = ui.UIDetail.text("Log", "\n".join(f"row {i}" for i in range(10000)))
        browser._running = True
        return browser

    async def test_end_yields_to_the_input_loop_and_reaches_the_tail(self):
        browser = self.browser()
        browser._start_detail_end()
        task = browser._detail_end_task
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0)
        await task
        self.assertGreater(ticks, 5)
        self.assertIn("row 9999", "".join(value for _, value in browser._detail_fragments()))
        self.assertEqual(browser.status, "")

    async def test_new_input_cancels_end_without_a_late_scroll_jump(self):
        browser = self.browser()
        browser._start_detail_end()
        task = browser._detail_end_task
        await asyncio.sleep(0)
        browser.app.key_processor.before_key_press.fire()
        browser.detail_scroll = 3
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(browser.detail_scroll, 3)
        self.assertLess(len(browser._detail_row_sources), 1000)
        self.assertEqual(browser.status, "")

    async def test_replaced_detail_cancels_end_and_keeps_new_feedback(self):
        browser = self.browser()
        browser._start_detail_end()
        task = browser._detail_end_task
        await asyncio.sleep(0)
        browser.detail = ui.UIDetail.text("New", "new content")
        browser.status = "Operation completed"
        await task
        self.assertEqual(browser.detail_scroll, 0)
        self.assertEqual(browser.status, "Operation completed")
        self.assertIn("new content", "".join(value for _, value in browser._detail_fragments()))

    async def test_collapsible_end_does_not_rewrap_when_selection_changes(self):
        browser = self.browser()
        browser.presentation = "timeline"
        browser.collapsible_detail = True
        browser.detail = ui.UIDetail("Events", lines=[
            ui.UILine(f"Section {i}", "class:detail.heading") for i in range(1000)],
            collapsible=True)
        browser._start_detail_end()
        await browser._detail_end_task
        cache = browser._detail_rows_cache
        text = "".join(value for _, value in browser._detail_fragments())
        self.assertIs(browser._detail_rows_cache, cache)
        self.assertIn("Section 999", text)
        self.assertTrue(any("reverse" in style and "Section 999" in value
                            for style, value in browser._detail_fragments()))


if __name__ == "__main__":
    unittest.main()
