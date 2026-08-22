"""Tests for the HWO lane (swimlane) layout and its interactive viewer.

Layout tests drive workflow_viz.build_lane_view directly on ASTs built
by hand (or via hwo_adapter.parse); viewer tests run the real
prompt_toolkit event loop headlessly, mirroring test_hwg_view.
"""

import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from hwo_adapter import parse as parse_hwo
import workflow_viz
import hwo_view


_SOURCE = (
    "(plan.md)#feeder# [in(topic: string), out(feed: file)] {\n"
    "  -> search the web for news\n"
    "  -> #digest# {\n"
    "    -> summarize feed\n"
    "    -> write summary file\n"
    "  }\n"
    "  //\n"
    "    #a# { -> do A }\n"
    "    #b# { -> do B }\n"
    "  //\n"
    "}\n"
)


def _tree(status=None):
    return workflow_viz.tree_from_steps(parse_hwo(_SOURCE), status)


def _viewer(status=None, pipe=None):
    tree = _tree(status)
    view = workflow_viz.build_lane_view(tree)
    return hwo_view.HwoLaneViewer("flow.hwo", view, status=status,
                                  statements=tree.steps,
                                  input=pipe, output=DummyOutput())


class LaneLayoutTests(unittest.TestCase):
    def test_step_paths_match_runner_paths(self):
        """Element ids are the stepId paths hwo_runner emits."""
        view = workflow_viz.build_lane_view(_tree())
        self.assertEqual(view.node_order[0], "0")           # feeder
        self.assertIn("0.0", view.node_order)               # task
        self.assertIn("0.1", view.node_order)               # digest
        self.assertIn("0.1.0", view.node_order)             # digest tasks
        self.assertIn("0.2", view.node_order)               # parallel
        self.assertIn("0.2.0", view.node_order)             # a
        self.assertIn("0.2.0.0", view.node_order)           # a's task

    def test_parallel_members_are_side_by_side(self):
        view = workflow_viz.build_lane_view(_tree())
        ra = view.node_rects["0.2.0"]
        rb = view.node_rects["0.2.1"]
        self.assertEqual(ra[0], rb[0])                       # same row
        self.assertGreater(rb[1], ra[1] + ra[2] - 1)         # b right of a

    def test_nested_lane_sits_inside_parent(self):
        view = workflow_viz.build_lane_view(_tree())
        outer = view.node_rects["0"]
        inner = view.node_rects["0.1"]
        self.assertGreaterEqual(inner[0], outer[0])
        self.assertLess(inner[0] + inner[3], outer[0] + outer[3])
        self.assertGreaterEqual(inner[1], outer[1] + 1)

    def test_status_icons_paint_into_canvas(self):
        tree = _tree({"0.1.0": workflow_viz.STATUS_DONE,
                      "0.1.1": workflow_viz.STATUS_RUNNING})
        text = workflow_viz.render_lane_plain(tree)
        self.assertIn(workflow_viz.status_icon(workflow_viz.STATUS_DONE),
                      text)
        self.assertIn(workflow_viz.status_icon(workflow_viz.STATUS_RUNNING),
                      text)

    def test_hit_regions_map_rows_to_step_ids(self):
        view = workflow_viz.build_lane_view(_tree())
        row, col = view.node_rows["0.1.0"], view.node_rects["0.1.0"][1] + 4
        self.assertEqual(view.canvas.hit_at(row, col), "0.1.0")
        # an agent's header row hits the agent itself, not its child
        hrow = view.node_rows["0.1"]
        self.assertEqual(view.canvas.hit_at(hrow, view.node_rects["0.1"][1] + 1),
                         "0.1")

    def test_navigation_edges_chain_and_dive(self):
        view = workflow_viz.build_lane_view(_tree())
        # consecutive siblings chain: 0.0 -> 0.1
        outs = [e["to"] for e in view.out_edges.get("0.0", [])]
        self.assertIn("0.1", outs)
        # agent dives into its first child: 0.1 -> 0.1.0
        outs = [e["to"] for e in view.out_edges.get("0.1", [])]
        self.assertIn("0.1.0", outs)
        # parallel fans out to every member head
        outs = [e["to"] for e in view.out_edges.get("0.2", [])]
        self.assertEqual(sorted(outs), ["0.2.0", "0.2.1"])

    def test_empty_workflow_renders_placeholder(self):
        tree = workflow_viz.tree_from_steps([])
        view = workflow_viz.build_lane_view(tree)
        self.assertEqual(view.node_order, [])
        self.assertIn("empty workflow", "\n".join(view.canvas.lines()))

    def test_plain_render_includes_header_and_counts(self):
        text = workflow_viz.render_lane_plain(_tree(), "MY TITLE")
        self.assertIn("MY TITLE", text)
        self.assertIn("agents", text)
        self.assertIn("#feeder#", text)
        self.assertIn("plan.md", text)
        self.assertIn("[in(topic), out(feed)]", text)


class LaneViewerTests(unittest.TestCase):
    def test_q_quits_the_session(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            pipe.send_text("q")
            viewer.run()
            self.assertTrue(viewer._quit)

    def test_j_moves_into_preorder_sequence(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            pipe.send_text("j")   # 0 -> 0.0
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.selected, "0.0")

    def test_l_follows_dive_edge(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            viewer.select("0.1")
            pipe.send_text("l")   # dive into first child
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.selected, "0.1.0")

    def test_tab_cycles_status_and_rebuilds_lanes(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe=pipe)
            pipe.send_text("\t")
            pipe.send_text("q")
            viewer.run()
            self.assertEqual(viewer.status.get("0"),
                             workflow_viz.STATUS_RUNNING)

    def test_inspector_shows_step_contract(self):
        viewer = _viewer()
        viewer.select("0")
        text = "".join(t for _, t in viewer._inspector_fragments())
        self.assertIn("#feeder#", text)
        self.assertIn("plan.md", text)
        self.assertIn("IN", text)
        self.assertIn("topic", text)
        self.assertIn("feed", text)

    def test_inspector_shows_task_text_and_parent(self):
        viewer = _viewer()
        viewer.select("0.1.0")
        text = "".join(t for _, t in viewer._inspector_fragments())
        self.assertIn("summarize feed", text)
        self.assertIn("PARENT", text)
        self.assertIn("#digest#", text)

    def test_inspector_on_missing_selection(self):
        viewer = _viewer()
        viewer.selected = ""
        text = "".join(t for _, t in viewer._inspector_fragments())
        self.assertIn("Select a node", text)


class OpenLaneViewerTests(unittest.TestCase):
    def test_parse_error_is_reported(self):
        import io
        import contextlib
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".hwo")
        os.write(fd, b"#unclosed { -> x")
        os.close(fd)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ok = hwo_view.open_lane_viewer(path)
        finally:
            os.unlink(path)
        self.assertFalse(ok)
        self.assertIn("parse error", buf.getvalue())

    def test_missing_file_is_reported(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = hwo_view.open_lane_viewer("/nonexistent/thing.hwo")
        self.assertFalse(ok)
        self.assertIn("cannot read", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
