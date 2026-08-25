"""The properties the infinite canvas has to keep, whatever it draws.

These are the ones that break silently: a wheel that drifts off target, a
child that keeps being drawn after its parent stopped being readable, a hit
map that answers with the container instead of the thing you clicked.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infinite_canvas as ic


def _nested_scene():
    """A module holding two classes, each holding one function."""
    shapes = [
        ic.Shape(id="m", x=0, y=0, w=200, h=100, label="module", depth=0),
        ic.Shape(id="c1", x=10, y=10, w=80, h=40, label="Alpha",
                 parent="m", depth=1),
        ic.Shape(id="c2", x=110, y=10, w=80, h=40, label="Beta",
                 parent="m", depth=1),
        ic.Shape(id="f1", x=15, y=20, w=30, h=15, label="run",
                 parent="c1", depth=2),
        ic.Shape(id="f2", x=115, y=20, w=30, h=15, label="stop",
                 parent="c2", depth=2),
    ]
    conns = [ic.Connector(src="f1", dst="f2", kind="calls")]
    return ic.Scene(title="t", shapes=shapes, connectors=conns)


class TestViewport(unittest.TestCase):
    def test_screen_world_roundtrip(self):
        vp = ic.Viewport(cx=50, cy=20, scale=0.7, width=80, height=24)
        for col, row in ((0, 0), (40, 12), (79, 23)):
            wx, wy = vp.to_world(col, row)
            self.assertEqual((col, row), vp.to_screen(wx, wy))

    def test_zoom_pins_the_point_under_the_cursor(self):
        vp = ic.Viewport(cx=0, cy=0, scale=1.0, width=80, height=24)
        col, row = 12, 5
        before = vp.to_world(col, row)
        for _ in range(6):
            vp.zoom_at(ic.ZOOM_STEP, col, row)
        after = vp.to_world(col, row)
        self.assertAlmostEqual(before[0], after[0], places=6)
        self.assertAlmostEqual(before[1], after[1], places=6)

    def test_zoom_is_clamped_both_ways(self):
        vp = ic.Viewport(width=80, height=24)
        for _ in range(200):
            vp.zoom_at(ic.ZOOM_STEP, 0, 0)
        self.assertLessEqual(vp.scale, ic.MAX_SCALE)
        for _ in range(400):
            vp.zoom_at(1 / ic.ZOOM_STEP, 0, 0)
        self.assertGreaterEqual(vp.scale, ic.MIN_SCALE)

    def test_fit_frames_the_scene(self):
        scene = _nested_scene()
        vp = ic.Viewport(width=80, height=24)
        vp.fit(scene.bounds())
        x0, y0, x1, y1 = scene.bounds()
        c0, r0 = vp.to_screen(x0, y0)
        c1, r1 = vp.to_screen(x1, y1)
        self.assertGreaterEqual(c0, 0)
        self.assertGreaterEqual(r0, 0)
        self.assertLessEqual(c1, vp.width)
        self.assertLessEqual(r1, vp.height)


class TestLevelOfDetail(unittest.TestCase):
    def test_thresholds_are_ordered(self):
        self.assertEqual(ic.lod_for(0, 0), ic.LOD_HIDDEN)
        self.assertEqual(ic.lod_for(1, 1), ic.LOD_GLYPH)
        self.assertEqual(ic.lod_for(4, 2), ic.LOD_BLOCK)
        self.assertEqual(ic.lod_for(12, 4), ic.LOD_FRAME)
        self.assertEqual(ic.lod_for(40, 10), ic.LOD_DETAIL)

    def test_zooming_out_hides_children_before_parents(self):
        scene = _nested_scene()
        vp = ic.Viewport(width=100, height=30)
        vp.fit(scene.bounds())
        close = ic.render(scene, vp)
        self.assertIn("f1", close.visible)

        for _ in range(10):
            vp.zoom_center(1 / ic.ZOOM_STEP)
        far = ic.render(scene, vp)
        self.assertNotIn("f1", far.visible)
        self.assertLess(len(far.visible), len(close.visible))

    def test_a_child_needs_a_readable_parent(self):
        """The rule that keeps a zoomed-out map from becoming confetti."""
        scene = _nested_scene()
        vp = ic.Viewport(width=100, height=30)
        vp.fit(scene.bounds())
        for _ in range(6):
            vp.zoom_center(1 / ic.ZOOM_STEP)
        out = ic.render(scene, vp)
        for shape in scene.shapes:
            if shape.parent and out.lods.get(shape.id, 0) > ic.LOD_HIDDEN:
                self.assertGreaterEqual(out.lods.get(shape.parent, 0),
                                        ic.LOD_FRAME,
                                        f"{shape.id} drawn under a collapsed parent")

    def test_a_block_still_carries_its_name(self):
        """An overview of unnamed rectangles is not an overview."""
        scene = ic.Scene(shapes=[ic.Shape(id="m", x=0, y=0, w=40, h=20,
                                          label="parser")])
        vp = ic.Viewport(width=60, height=20, cx=20, cy=10, scale=0.2)
        out = ic.render(scene, vp)
        self.assertEqual(out.lods["m"], ic.LOD_BLOCK)
        self.assertIn("pars", "\n".join(out.canvas.lines()))

    def test_labels_appear_only_once_there_is_room(self):
        scene = _nested_scene()
        vp = ic.Viewport(width=100, height=30)
        vp.fit(scene.bounds())
        text = "\n".join(ic.render(scene, vp).canvas.lines())
        self.assertIn("Alpha", text)

        for _ in range(8):
            vp.zoom_center(1 / ic.ZOOM_STEP)
        small = "\n".join(ic.render(scene, vp).canvas.lines())
        self.assertNotIn("Alpha", small)


class TestPaths(unittest.TestCase):
    """A drawing is not always a box. A path has to be drawn as a path: the
    bounding box of a sine wave is the bounding box of a diagonal line."""

    def _painted(self, shape, vp):
        scene = ic.Scene(shapes=[shape])
        return "\n".join(ic.render(scene, vp).canvas.lines())

    def test_a_path_is_drawn_through_its_points(self):
        vp = ic.Viewport(width=40, height=20, cx=20, cy=10, scale=1.0)
        zigzag = ic.Shape(id="p", kind="line", x=0, y=0, w=40, h=20,
                          points=[[0, 0], [0, 20], [40, 20], [40, 0]])
        diagonal = ic.Shape(id="p", kind="line", x=0, y=0, w=40, h=20)
        self.assertNotEqual(self._painted(zigzag, vp),
                            self._painted(diagonal, vp))

    def test_the_path_moves_with_the_viewport(self):
        shape = ic.Shape(id="p", kind="line", x=0, y=0, w=20, h=10,
                         points=[[0, 0], [20, 10]])
        near = ic.Viewport(width=40, height=20, cx=10, cy=5, scale=1.0)
        far = ic.Viewport(width=40, height=20, cx=10, cy=5, scale=0.2)
        self.assertNotEqual(self._painted(shape, near),
                            self._painted(shape, far))

    def test_a_path_with_one_point_does_not_raise(self):
        vp = ic.Viewport(width=20, height=10, scale=1.0)
        shape = ic.Shape(id="p", kind="line", x=0, y=0, w=1, h=1,
                         points=[[0, 0]])
        self._painted(shape, vp)

    def test_a_path_far_off_screen_stays_cheap(self):
        vp = ic.Viewport(width=20, height=10, scale=1.0)
        shape = ic.Shape(id="p", kind="line", x=-1e6, y=-1e6, w=2e6, h=2e6,
                         points=[[-1e6, -1e6], [1e6, 1e6], [-1e6, 1e6]])
        self._painted(shape, vp)


class TestHitMap(unittest.TestCase):
    def test_the_deepest_shape_wins(self):
        scene = _nested_scene()
        vp = ic.Viewport(width=120, height=40)
        vp.fit(scene.bounds())
        out = ic.render(scene, vp)
        child = scene.index()["f1"]
        col, row = vp.to_screen(child.cx, child.cy)
        self.assertEqual(out.at(row, col), "f1")

    def test_empty_cell_hits_nothing(self):
        scene = ic.Scene(shapes=[ic.Shape(id="a", x=0, y=0, w=10, h=10)])
        vp = ic.Viewport(width=80, height=24, cx=5, cy=5, scale=1.0)
        out = ic.render(scene, vp)
        self.assertIsNone(out.at(0, 0))


class TestConnectors(unittest.TestCase):
    def test_edges_lift_to_the_level_being_looked_at(self):
        """A call between two methods is drawn between their classes."""
        scene = _nested_scene()
        index = scene.index()
        boxes = {"c1": (0, 0, 10, 4), "c2": (20, 0, 30, 4)}   # classes visible
        self.assertEqual(ic._lift("f1", "f2", index, boxes), ("c1", "c2"))

    def test_a_containment_pair_is_not_an_edge(self):
        scene = _nested_scene()
        index = scene.index()
        boxes = {"m": (0, 0, 40, 10), "c1": (1, 1, 9, 4)}
        self.assertIsNone(ic._lift("f1", "m", index, boxes))


class TestRobustness(unittest.TestCase):
    def test_a_line_far_off_screen_is_cheap_and_safe(self):
        scene = ic.Scene(shapes=[
            ic.Shape(id="l", kind="line", x=-1e7, y=-1e7, w=2e7, h=2e7)])
        vp = ic.Viewport(width=40, height=10, scale=1.0)
        ic.render(scene, vp)              # must not hang or raise

    def test_a_box_wider_than_the_screen_paints(self):
        scene = ic.Scene(shapes=[ic.Shape(id="b", x=-500, y=-50, w=1000,
                                          h=100, label="wide")])
        vp = ic.Viewport(width=40, height=10, scale=1.0)
        out = ic.render(scene, vp)
        self.assertIn("b", out.visible)


class TestSceneContract(unittest.TestCase):
    def test_round_trips_the_shared_json(self):
        data = {
            "version": 1, "title": "x",
            "shapes": [{"id": "a", "x": 1, "y": 2, "w": 3, "h": 4,
                        "label": "A", "parent": None, "depth": 0,
                        "detail": ["d"], "meta": {"file": "a.py"}}],
            "connectors": [{"src": "a", "dst": "a", "weight": 2}],
        }
        scene = ic.scene_from_json(data)
        self.assertEqual(scene.shapes[0].label, "A")
        self.assertEqual(scene.shapes[0].meta["file"], "a.py")
        self.assertEqual(scene.connectors[0].weight, 2)

    def test_search_prefers_the_shallow_match(self):
        scene = _nested_scene()
        hits = ic.search(scene, "a")
        self.assertTrue(hits)
        self.assertLessEqual(hits[0].depth, hits[-1].depth)


if __name__ == "__main__":
    unittest.main()
