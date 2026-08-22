"""Tests for hwg_view: interactive viewer with a headless event loop.

Drives the real prompt_toolkit event loop through create_pipe_input +
DummyOutput (same pattern as test_resource_ui), so key handling, the
selection model, and the render path all execute for real.
"""

import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from hwg_adapter import parse as parse_hwg
import workflow_viz
import hwg_view


_SOURCE = (
    "(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n"
    "#a# -> #b#\n#b# -> #c#\n"
    "#b# -> { on: FAIL, maxLoops: 2 } #a#\n"
)


def _viewer(statements=None, status=None, pipe=None):
    statements = statements if statements is not None else parse_hwg(_SOURCE)
    graph = workflow_viz.graph_from_statements(statements, status)
    view = workflow_viz.build_view(graph)
    return hwg_view.HwgViewer("flow.hwg", view, status=status,
                              statements=statements,
                              input=pipe, output=DummyOutput())


class HeadlessSessionTests(unittest.TestCase):
    def test_q_quits_the_session(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            pipe.send_text("q")
            viewer.run()
            self.assertTrue(viewer._quit)

    def test_down_moves_selection_and_render_paths_execute(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            pipe.send_text("j")
            # let one event loop iteration handle the key, then quit
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.selected, "b")

    def test_edge_navigation_follows_outgoing_edge(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            viewer.select("b")
            pipe.send_text("l")   # follow outgoing edge
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.selected, "c")

    def test_edge_navigation_follows_incoming_edge(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            viewer.select("c")
            pipe.send_text("h")   # follow incoming edge
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.selected, "b")

    def test_tab_cycles_status_and_rebuilds_view(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            pipe.send_text("\t")
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.status.get("a"),
                             workflow_viz.STATUS_RUNNING)

    def test_pagedown_scrolls_within_bounds(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            # simulate a short terminal so the canvas actually overflows;
            # patch the method, not the attribute, or rendering resets it
            viewer._visible_height = lambda: 3
            viewer._visible = 3
            pipe.send_text("\x1b[6~")   # PageDown
            pipe.send_text("q")
            viewer.run()
            self.assertGreater(viewer.scroll_top, 0)
            self.assertLess(viewer.scroll_top, viewer.view.canvas.height)


class SelectionModelTests(unittest.TestCase):
    def test_select_rejects_unknown_node(self):
        viewer = _viewer()
        viewer.select("a")
        viewer.select("nope")
        self.assertEqual(viewer.selected, "a")

    def test_move_clamps_at_both_ends(self):
        viewer = _viewer()
        viewer._move(-5)
        self.assertEqual(viewer.selected, "a")
        viewer._move(99)
        self.assertEqual(viewer.selected, "c")

    def test_neighbor_falls_back_to_order_when_no_edges(self):
        # plain chain a->b->c (no back edge): 'a' has no incoming edge, so
        # up() degrades to order movement and clamps at the top.
        chain = ("(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n"
                 "#a# -> #b#\n#b# -> #c#\n")
        viewer = _viewer(statements=parse_hwg(chain))
        viewer.select("a")
        viewer._neighbor("up")
        self.assertEqual(viewer.selected, "a")
        # 'c' has no outgoing edge: down() clamps at the bottom.
        viewer.select("c")
        viewer._neighbor("down")
        self.assertEqual(viewer.selected, "c")

    def test_click_hit_region_selects_node(self):
        viewer = _viewer()
        row = viewer.view.node_rows["b"]
        col = viewer.view.node_rects["b"][1] + 5
        nid = viewer.view.canvas.hit_at(row, col)
        viewer.select(nid)
        self.assertEqual(viewer.selected, "b")


class InspectorTests(unittest.TestCase):
    def test_inspector_renders_contract_and_edges(self):
        viewer = _viewer()
        viewer.select("b")
        text = "".join(t for _, t in viewer._inspector_fragments())
        self.assertIn("#b#", text)
        self.assertIn("b.hwo", text)
        self.assertIn("IN", text)
        self.assertIn("OUT", text)
        # the FAIL loop edge label is surfaced
        self.assertIn("FAIL", text)

    def test_inspector_on_missing_selection(self):
        viewer = _viewer()
        viewer.selected = ""
        text = "".join(t for _, t in viewer._inspector_fragments())
        self.assertIn("Select a node", text)


class OpenViewerTests(unittest.TestCase):
    def test_open_viewer_reports_parse_errors(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = hwg_view.open_viewer("/nonexistent.hwg")
        self.assertFalse(ok)
        self.assertIn("cannot read", buf.getvalue())

    def test_open_viewer_rejects_invalid_graph(self):
        import io
        import contextlib
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.hwg"
            bad.write_text("#ghost# -> #missing#\n", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = hwg_view.open_viewer(str(bad))
        self.assertFalse(ok)
        self.assertIn("validation", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
