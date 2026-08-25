"""Tests for canvas_view: the interactive canvas, driven headlessly.

Same pattern as test_hwg_view — a real prompt_toolkit loop over a pipe input
and a DummyOutput, so key handling, the viewport maths and the whole render
path execute for real rather than being asserted about.
"""

import os
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.mouse_events import (
    MouseButton, MouseEvent, MouseEventType, MouseModifier)
from prompt_toolkit.data_structures import Point

import canvas_view
import infinite_canvas as ic


def _scene():
    return ic.Scene(
        title="board",
        shapes=[
            ic.Shape(id="m", x=0, y=0, w=200, h=100, label="module"),
            ic.Shape(id="c", x=10, y=10, w=80, h=40, label="Alpha",
                     parent="m", depth=1),
            ic.Shape(id="f", x=15, y=15, w=30, h=15, label="run",
                     parent="c", depth=2, detail=["does the thing"]),
        ],
        connectors=[ic.Connector(src="c", dst="m")],
    )


def _viewer(pipe=None):
    return canvas_view.CanvasViewer(_scene(), title="board.excalidraw",
                                    input=pipe, output=DummyOutput())


class HeadlessSessionTests(unittest.TestCase):
    def test_q_closes_the_session(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe)
            pipe.send_text("q")
            viewer.run()                     # returns => the loop exited

    def test_zoom_keys_change_scale_and_repaint(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe)
            start = viewer.vp.scale
            pipe.send_text("++-q")
            viewer.run()
            self.assertGreater(viewer.vp.scale, start)
            self.assertTrue(viewer._last_render.visible)

    def test_fit_returns_to_the_whole_scene(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe)
            pipe.send_text("+++0q")
            viewer.run()
            x0, y0, x1, y1 = viewer.scene.bounds()
            self.assertAlmostEqual(viewer.vp.cx, (x0 + x1) / 2, places=6)

    def test_pan_moves_the_centre(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe)
            before = viewer.vp.cx
            pipe.send_text("llq")
            viewer.run()
            self.assertGreater(viewer.vp.cx, before)

    def test_search_selects_and_dives(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe)
            pipe.send_text("/run\rq")
            viewer.run()
            self.assertEqual(viewer.matches, ["f"])
            self.assertEqual(viewer.selected, "f")

    def test_search_reports_a_miss_instead_of_moving(self):
        with create_pipe_input() as pipe:
            viewer = _viewer(pipe)
            pipe.send_text("/zzz\rq")
            viewer.run()
            self.assertEqual(viewer.matches, [])
            self.assertIn("no match", viewer.status_note)


class EmptyBoardTests(unittest.TestCase):
    """A board with nothing on it is the normal first state of a new board,
    so it has to open, not be refused."""

    def _empty(self, **kw):
        return canvas_view.CanvasViewer(
            ic.Scene(title="fresh"), title="fresh.excalidraw",
            empty_hint=["draw on it in Helpwo", ""], output=DummyOutput(), **kw)

    def test_an_empty_scene_opens_and_says_so(self):
        viewer = self._empty()
        text = "".join(t for _s, t in viewer._canvas_fragments())
        self.assertIn("empty board", text)
        self.assertIn("draw on it in Helpwo", text)

    def test_open_scene_refuses_an_empty_scene_unless_asked(self):
        self.assertFalse(canvas_view.open_scene(ic.Scene()))

    def test_an_empty_scene_survives_the_usual_keys(self):
        with create_pipe_input() as pipe:
            viewer = self._empty(input=pipe)
            pipe.send_text("+-0/x\rnNi\tq")
            viewer.run()                      # no shapes, no crash


class BoardPickerTests(unittest.TestCase):
    def _viewer(self, boards=("a.excalidraw", "b.excalidraw"), **kw):
        self.loaded = []

        def load(path):
            self.loaded.append(path)
            return (ic.Scene(shapes=[ic.Shape(id="x", x=0, y=0, w=10, h=10,
                                              label=path)]), path)

        return canvas_view.CanvasViewer(
            _scene(), title="here.excalidraw", boards=list(boards),
            load_board=load, output=DummyOutput(), **kw)

    def test_b_opens_the_list_and_enter_swaps_the_board(self):
        viewer = self._viewer()
        viewer.open_picker()
        self.assertTrue(viewer.picker_open)
        viewer.move_picker(1)
        viewer.choose_board()
        self.assertFalse(viewer.picker_open)
        self.assertEqual(self.loaded, ["b.excalidraw"])
        self.assertEqual(viewer.title, "b.excalidraw")
        self.assertEqual([s.id for s in viewer.scene.shapes], ["x"])

    def test_switching_drops_everything_derived_from_the_old_board(self):
        """A selection or a match list from the old board points at ids the
        new one has never heard of."""
        viewer = self._viewer()
        viewer.select("c")
        viewer.query = "run"
        viewer.run_search()
        self.assertTrue(viewer.matches)
        viewer.open_picker()
        viewer.choose_board()
        self.assertIsNone(viewer.selected)
        self.assertEqual(viewer.matches, [])
        self.assertEqual(viewer.query, "")

    def test_an_unreadable_board_reports_and_keeps_the_current_one(self):
        def boom(path):
            raise ValueError("not a readable Excalidraw scene")
        viewer = canvas_view.CanvasViewer(
            _scene(), boards=["bad.excalidraw"], load_board=boom,
            output=DummyOutput())
        viewer.open_picker()
        viewer.choose_board()
        self.assertIn("not a readable", viewer.status_note)
        self.assertEqual(len(viewer.scene.shapes), 3)

    def test_with_no_other_boards_the_list_does_not_open(self):
        viewer = self._viewer(boards=())
        viewer.open_picker()
        self.assertFalse(viewer.picker_open)
        self.assertIn("no other boards", viewer.status_note)

    def test_the_mouse_cannot_reach_the_canvas_under_the_list(self):
        viewer = self._viewer()
        viewer._canvas_fragments()
        viewer.open_picker()
        viewer._mouse(MouseEvent(position=Point(x=5, y=5),
                                 event_type=MouseEventType.MOUSE_UP,
                                 button=MouseButton.LEFT,
                                 modifiers=frozenset()))
        self.assertIsNone(viewer.selected)

    def test_the_list_is_painted_over_the_canvas(self):
        viewer = self._viewer()
        viewer.open_picker()
        text = "".join(t for _s, t in viewer._canvas_fragments())
        self.assertIn("boards", text)
        self.assertIn("a.excalidraw", text)

    def test_keys_drive_the_list_end_to_end(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(input=pipe)
            pipe.send_text("bj\rq")
            viewer.run()
            self.assertEqual(self.loaded, ["b.excalidraw"])


class DrawingTests(unittest.TestCase):
    """The viewer half of drawing: gestures in, board operations out."""

    def setUp(self):
        import tempfile, canvas, canvas_edit
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "b.excalidraw")
        canvas.write_scene(self.path, canvas.empty_scene())
        self.canvas = canvas
        self.editor = canvas_edit.BoardEditor(self.path, canvas)
        self.viewer = self._viewer()

    def _scene_now(self):
        import infinite_canvas
        return infinite_canvas.scene_from_json(
            self.canvas.to_canvas_scene(self.editor.scene, title="b"))

    def _viewer(self, **kw):
        return canvas_view.CanvasViewer(
            self._scene_now(), title="b.excalidraw", editor=self.editor,
            reload_scene=self._scene_now, output=DummyOutput(), **kw)

    def _mouse_at(self, kind, col, row):
        self.viewer._mouse(MouseEvent(position=Point(x=col, y=row),
                                      event_type=kind,
                                      button=MouseButton.LEFT,
                                      modifiers=frozenset()))

    def _path_drag(self, path):
        self._mouse_at(MouseEventType.MOUSE_DOWN, *path[0])
        for col, row in path[1:]:
            self._mouse_at(MouseEventType.MOUSE_MOVE, col, row)
        self._mouse_at(MouseEventType.MOUSE_UP, *path[-1])

    def _drag(self, c0, r0, c1, r1):
        def ev(kind, col, row):
            self.viewer._mouse(MouseEvent(position=Point(x=col, y=row),
                                          event_type=kind,
                                          button=MouseButton.LEFT,
                                          modifiers=frozenset()))
        ev(MouseEventType.MOUSE_DOWN, c0, r0)
        ev(MouseEventType.MOUSE_MOVE, c1, r1)
        ev(MouseEventType.MOUSE_UP, c1, r1)

    def _live(self):
        return self.canvas.live_elements(self.canvas.read_scene(self.path))

    def test_a_drag_outside_draw_mode_pans_and_draws_nothing(self):
        before = self.viewer.vp.cx
        self._drag(10, 5, 30, 9)
        self.assertNotEqual(self.viewer.vp.cx, before)
        self.assertEqual(self._live(), [])

    def test_a_drag_in_draw_mode_makes_one_shape_where_it_was_drawn(self):
        self.viewer.set_tool("rectangle")
        self.viewer._canvas_fragments()
        expect_x, expect_y = self.viewer.vp.to_world(10, 5)
        self._drag(10, 5, 30, 9)
        live = self._live()
        self.assertEqual([e["type"] for e in live], ["rectangle"])
        self.assertAlmostEqual(live[0]["x"], expect_x, delta=1.0)
        self.assertAlmostEqual(live[0]["y"], expect_y, delta=1.0)
        self.assertGreater(live[0]["width"], 0)

    def test_the_band_is_painted_while_the_button_is_down(self):
        self.viewer.set_tool("ellipse")
        self.viewer._mouse(MouseEvent(position=Point(x=8, y=4),
                                      event_type=MouseEventType.MOUSE_DOWN,
                                      button=MouseButton.LEFT,
                                      modifiers=frozenset()))
        self.viewer._mouse(MouseEvent(position=Point(x=20, y=8),
                                      event_type=MouseEventType.MOUSE_MOVE,
                                      button=MouseButton.LEFT,
                                      modifiers=frozenset()))
        painted = "".join(t for _s, t in self.viewer._canvas_fragments())
        self.assertIn("│", painted)
        self.assertEqual(self._live(), [])       # nothing committed yet

    def test_a_tool_key_turns_drawing_on(self):
        self.assertFalse(self.viewer.draw_mode)
        self.viewer.set_tool("diamond")
        self.assertTrue(self.viewer.draw_mode)
        self.assertEqual(self.viewer.tool, "diamond")

    def test_p_places_a_shape_without_a_mouse(self):
        self.viewer.set_tool("rectangle")
        self.viewer.place_shape_at_centre()
        self.assertEqual([e["type"] for e in self._live()], ["rectangle"])

    def test_typing_a_label_puts_it_on_the_selected_shape(self):
        self.viewer.set_tool("rectangle")
        self.viewer.place_shape_at_centre()
        self.viewer.begin_label()
        self.viewer.label_text = "网关"
        self.viewer.commit_label()
        text = self.canvas.describe_scene(self.canvas.read_scene(self.path))
        self.assertIn("网关", text)

    def test_a_label_with_nothing_selected_becomes_standalone_text(self):
        self.viewer.select(None)
        self.viewer.begin_label()
        self.viewer.label_text = "note"
        self.viewer.commit_label()
        self.assertEqual([e["type"] for e in self._live()], ["text"])

    def test_an_arrow_takes_two_picks(self):
        self.viewer.set_tool("rectangle")
        self.viewer.place_shape_at_centre()
        first = self.viewer.selected
        self.viewer.vp.pan_cells(40, 0)
        self.viewer.place_shape_at_centre()
        second = self.viewer.selected
        self.assertNotEqual(first, second)
        self.viewer.select(first)
        self.viewer.start_arrow()
        self.assertEqual(self.viewer.arrow_from, first)
        self.viewer.select(second)
        self.viewer.start_arrow()
        self.assertIsNone(self.viewer.arrow_from)
        self.assertIn("arrow", [e["type"] for e in self._live()])

    def test_an_arrow_to_itself_is_refused_and_keeps_the_first_pick(self):
        self.viewer.set_tool("rectangle")
        self.viewer.place_shape_at_centre()
        self.viewer.start_arrow()
        self.viewer.start_arrow()
        self.assertNotIn("arrow", [e["type"] for e in self._live()])

    def test_delete_then_undo_puts_it_back(self):
        self.viewer.set_tool("rectangle")
        self.viewer.place_shape_at_centre()
        self.viewer.erase_selected()
        self.assertEqual(self._live(), [])
        self.viewer.undo_edit()
        self.assertEqual(len(self._live()), 1)

    def test_a_read_only_view_says_so_instead_of_drawing(self):
        viewer = canvas_view.CanvasViewer(_scene(), output=DummyOutput())
        viewer.set_tool("rectangle")
        self.assertFalse(viewer.draw_mode)
        self.assertIn("read-only", viewer.status_note)
        viewer.erase_selected()
        viewer.undo_edit()
        viewer.begin_label()
        self.assertFalse(viewer.typing_label)

    def test_an_edit_refused_by_a_concurrent_write_is_reported(self):
        """Helpwo saved the board while it was open here."""
        import canvas_edit
        theirs = self.canvas.empty_scene()
        theirs["elements"] = [canvas_edit.shape("ellipse", 0, 0, 20, 20)]
        os.utime(self.path, (0, 0))
        self.canvas.write_scene(self.path, theirs)

        self.viewer.set_tool("rectangle")
        self.viewer.place_shape_at_centre()
        self.assertIn("changed while", self.viewer.status_note)
        self.assertEqual([e["type"] for e in self._live()], ["ellipse"])
        # the view now shows their board, not a stale one
        self.assertEqual(len(self.viewer.scene.shapes), 1)

    def test_the_status_bar_shows_the_tool_and_the_label_prompt(self):
        self.viewer.set_tool("ellipse")
        bar = "".join(t for _s, t in self.viewer._status_fragments())
        self.assertIn("DRAW [ellipse]", bar)
        self.viewer.begin_label()
        self.viewer.label_text = "ab"
        bar = "".join(t for _s, t in self.viewer._status_fragments())
        self.assertIn("ab", bar)

    def test_keys_draw_end_to_end(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(input=pipe)
            # rect tool, space places it, t types a label, Enter commits
            pipe.send_text("r tdone\rq")
            viewer.run()
        live = self._live()
        self.assertEqual(sorted(e["type"] for e in live), ["rectangle", "text"])

    def test_a_pencil_stroke_follows_the_pointer(self):
        self.viewer.set_tool("freedraw")
        self.viewer._canvas_fragments()
        path = [(6, 3), (7, 3), (8, 4), (9, 5), (10, 5)]
        self._path_drag(path)
        live = self._live()
        self.assertEqual([e["type"] for e in live], ["freedraw"])
        # one point per cell visited, and the path is stored relative to the
        # element's own origin
        self.assertEqual(len(live[0]["points"]), len(path))
        self.assertEqual(live[0]["points"][0], [0, 0])

    def test_a_line_keeps_only_its_two_ends(self):
        self.viewer.set_tool("line")
        self.viewer._canvas_fragments()
        self._path_drag([(4, 2), (8, 4), (12, 6), (16, 8)])
        live = self._live()
        self.assertEqual([e["type"] for e in live], ["line"])
        self.assertEqual(len(live[0]["points"]), 2)

    def test_a_stroke_that_never_moved_is_not_drawn(self):
        self.viewer.set_tool("freedraw")
        self.viewer._canvas_fragments()
        self._path_drag([(5, 5)])
        self.assertEqual(self._live(), [])
        self.assertIn("too short", self.viewer.status_note)

    def test_the_pen_style_reaches_the_element(self):
        import canvas_edit
        self.viewer.set_tool("rectangle")
        self.viewer.cycle_style("color")       # off the default black
        self.viewer.cycle_style("fill")        # on
        self.viewer.style_at["width"] = 2      # thick
        self.viewer.place_shape_at_centre()
        box = self._live()[0]
        self.assertEqual(box["strokeColor"], canvas_edit.STROKE_COLORS[1])
        self.assertNotEqual(box["backgroundColor"], "transparent")
        self.assertEqual(box["strokeWidth"], canvas_edit.STROKE_WIDTHS[2])

    def test_the_stroke_in_progress_is_painted_before_it_is_committed(self):
        self.viewer.set_tool("freedraw")
        self._mouse_at(MouseEventType.MOUSE_DOWN, 6, 3)
        self._mouse_at(MouseEventType.MOUSE_MOVE, 7, 4)
        painted = "".join(t for _s, t in self.viewer._canvas_fragments())
        self.assertIn("·", painted)
        self.assertEqual(self._live(), [])

    def test_hjkl_pans_while_reading_and_picks_tools_while_drawing(self):
        """`l` cannot be both pan-right and the line tool at once; the two
        bindings live under filters that are never both true."""
        with create_pipe_input() as pipe:
            viewer = self._viewer(input=pipe)
            before = viewer.vp.cx
            pipe.send_text("l")               # reading: pans
            pipe.send_text("wl")              # drawing: picks the line tool
            pipe.send_text("q")
            viewer.run()
            self.assertGreater(viewer.vp.cx, before)
            self.assertEqual(viewer.tool, "line")


class ViewportBehaviourTests(unittest.TestCase):
    """The parts a user feels, exercised without the event loop."""

    def setUp(self):
        self.viewer = canvas_view.CanvasViewer(_scene(), output=DummyOutput())

    def _wheel(self, kind, col, row):
        self.viewer._mouse(MouseEvent(position=Point(x=col, y=row),
                                      event_type=kind,
                                      button=MouseButton.LEFT,
                                      modifiers=frozenset()))

    def test_wheel_zooms_about_the_pointer(self):
        viewer = self.viewer
        col, row = 10, 4
        before = viewer.vp.to_world(col, row)
        self._wheel(MouseEventType.SCROLL_UP, col, row)
        self._wheel(MouseEventType.SCROLL_UP, col, row)
        after = viewer.vp.to_world(col, row)
        self.assertAlmostEqual(before[0], after[0], places=6)
        self.assertAlmostEqual(before[1], after[1], places=6)

    def test_drag_pans_and_does_not_select(self):
        viewer = self.viewer
        viewer._canvas_fragments()                    # paint once for hits
        start = viewer.vp.cx
        self._wheel(MouseEventType.MOUSE_DOWN, 20, 6)
        self._wheel(MouseEventType.MOUSE_MOVE, 10, 6)
        self._wheel(MouseEventType.MOUSE_UP, 10, 6)
        self.assertGreater(viewer.vp.cx, start)
        self.assertIsNone(viewer.selected)

    def test_click_selects_what_is_under_it(self):
        viewer = self.viewer
        viewer._canvas_fragments()
        shape = viewer.index["c"]
        col, row = viewer.vp.to_screen(shape.cx, shape.cy)
        self._wheel(MouseEventType.MOUSE_DOWN, col, row)
        self._wheel(MouseEventType.MOUSE_UP, col, row)
        self.assertIn(viewer.selected, ("c", "f"))

    def test_dive_then_back_out_returns_to_the_parent(self):
        viewer = self.viewer
        viewer.dive("f")
        self.assertEqual(viewer.selected, "f")
        deep = viewer.vp.scale
        viewer.ascend()
        self.assertEqual(viewer.selected, "c")
        self.assertLess(viewer.vp.scale, deep)

    def test_inspector_describes_the_selection(self):
        viewer = self.viewer
        viewer.select("f")
        text = "".join(t for _s, t in viewer._inspector_fragments())
        self.assertIn("run", text)
        self.assertIn("does the thing", text)

    def test_i_hides_the_side_pane_and_the_canvas_takes_the_width(self):
        """The key that hides the pane has to be reachable from outside it."""
        viewer = self.viewer
        viewer._canvas_fragments()
        wide_before = viewer.vp.width
        self.assertIn("i pane", "".join(t for _s, t in viewer._status_fragments()))
        viewer.show_inspector = False
        viewer._canvas_fragments()
        self.assertGreater(viewer.vp.width, wide_before)
        self.assertIn("i panel back",
                      "".join(t for _s, t in viewer._status_fragments()))

    def test_inspector_falls_back_to_the_key_map(self):
        text = "".join(t for _s, t in self.viewer._inspector_fragments())
        self.assertIn("zoom", text)


if __name__ == "__main__":
    unittest.main()
