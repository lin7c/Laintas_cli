"""The canvas tools an agent calls.

The thing being pinned here is not "does it write a file" — it is that what
the model draws stays *reviewable*. Helpwo's editor groups its Show / Keep /
Undo banner by `customData.author == "ai"` and the turn id; anything drawn
without those tags lands on a person's board as an anonymous change they
cannot single out. That, and the same concurrency rule everything else on a
board obeys: a write that would land on somebody else's edit is refused.
"""

import os
import tempfile
import unittest

import canvas
import tools


class CanvasToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools.register_builtin_tools()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = tools.get_registry()
        self.ctx = tools.ToolCtx(cwd=self.tmp.name, run_id="run-7")

    def call(self, name, **params):
        return self.registry.invoke(name, params, self.ctx)

    def board(self, name="flow.excalidraw"):
        return canvas.read_scene(os.path.join(self.tmp.name, name))

    def live(self, name="flow.excalidraw"):
        return canvas.live_elements(self.board(name))

    # ---- drawing ----

    def test_a_diagram_in_one_call_creates_the_board(self):
        out = self.call("canvas.draw", path="flow.excalidraw", shapes=[
            {"id": "a", "kind": "rectangle", "label": "gateway"},
            {"id": "b", "kind": "ellipse", "label": "worker"},
        ], connect=[{"from": "a", "to": "b", "label": "dispatch"}])
        self.assertTrue(out["ok"], out.get("error"))
        types = sorted(e["type"] for e in self.live())
        self.assertEqual(types, ["arrow", "ellipse", "rectangle", "text",
                                 "text", "text"])

    def test_a_path_without_the_extension_still_finds_the_board(self):
        self.call("canvas.draw", path="notes",
                  shapes=[{"kind": "rectangle"}])
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp.name, "notes.excalidraw")))

    def test_local_names_come_back_so_a_second_call_can_use_them(self):
        out = self.call("canvas.draw", path="flow.excalidraw",
                        shapes=[{"id": "gw", "kind": "rectangle"}])
        real = out["ids"]["gw"]
        self.assertTrue(any(e["id"] == real for e in self.live()))

    def test_what_the_agent_draws_is_tagged_for_review(self):
        self.call("canvas.draw", path="flow.excalidraw",
                  shapes=[{"kind": "rectangle", "label": "x"},
                          {"kind": "ellipse", "label": "y"}])
        self.assertEqual(canvas.count_ai_turns(self.board()),
                         [{"turn": "run-7", "count": 2}])

    def test_shapes_are_required(self):
        out = self.call("canvas.draw", path="flow.excalidraw", shapes=[])
        self.assertFalse(out["ok"])
        self.assertIn("non-empty", out["error"])

    def test_an_unknown_shape_kind_is_refused_with_the_allowed_ones(self):
        """The registry's own schema check gets there first, and its message
        names what is allowed — which is what the model needs to retry."""
        out = self.call("canvas.draw", path="flow.excalidraw",
                        shapes=[{"kind": "hexagon"}])
        self.assertFalse(out["ok"])
        self.assertIn("rectangle", out["error"])
        self.assertEqual(self.live() if os.path.exists(
            os.path.join(self.tmp.name, "flow.excalidraw")) else [], [])

    def test_a_board_is_not_only_boxes_and_arrows(self):
        """Free drawing: a path, a colour and a fill, none of which is a node
        graph. The point of the board is that it is not one."""
        out = self.call("canvas.draw", path="sketch.excalidraw", shapes=[
            {"kind": "line", "points": [[0, 0], [100, 40], [200, 0]],
             "color": "#e03131", "strokeStyle": "dashed"},
            {"kind": "freedraw",
             "points": [[0, 100], [10, 120], [25, 90], [40, 130]],
             "strokeWidth": 4},
            {"kind": "rectangle", "x": 0, "y": 200, "width": 80, "height": 40,
             "background": "#ffec99", "fill": "hachure"},
        ])
        self.assertTrue(out["ok"], out.get("error"))
        live = self.live("sketch.excalidraw")
        by_type = {e["type"]: e for e in live}
        self.assertEqual(sorted(by_type), ["freedraw", "line", "rectangle"])
        self.assertEqual(len(by_type["line"]["points"]), 3)
        self.assertEqual(by_type["line"]["strokeStyle"], "dashed")
        self.assertEqual(len(by_type["freedraw"]["points"]), 4)
        self.assertEqual(by_type["freedraw"]["strokeWidth"], 4)
        self.assertEqual(by_type["rectangle"]["fillStyle"], "hachure")

    def test_a_path_keeps_its_own_coordinates(self):
        """Auto-layout is for boxes. Moving a drawing into a grid row would
        move the drawing."""
        self.call("canvas.draw", path="sketch.excalidraw",
                  shapes=[{"kind": "rectangle", "label": "first"}])
        self.call("canvas.draw", path="sketch.excalidraw", shapes=[
            {"kind": "line", "points": [[500, 500], [600, 500]]}])
        line = next(e for e in self.live("sketch.excalidraw")
                    if e["type"] == "line")
        self.assertEqual((line["x"], line["y"]), (500, 500))

    def test_a_path_needs_two_points(self):
        out = self.call("canvas.draw", path="sketch.excalidraw",
                        shapes=[{"kind": "line", "points": [[0, 0]]}])
        self.assertFalse(out["ok"])
        self.assertIn("two points", out["error"])

    # ---- reading ----

    def test_read_lists_ids_labels_and_what_arrows_connect(self):
        drawn = self.call("canvas.draw", path="flow.excalidraw", shapes=[
            {"id": "a", "kind": "rectangle", "label": "gateway"},
            {"id": "b", "kind": "ellipse", "label": "worker"}],
            connect=[{"from": "a", "to": "b"}])
        out = self.call("canvas.read", path="flow.excalidraw")
        self.assertTrue(out["ok"])
        self.assertIn("gateway", out["result"])
        self.assertIn(drawn["ids"]["a"], out["result"])
        self.assertIn("→", out["result"])

    def test_reading_a_board_that_is_not_there_says_so(self):
        out = self.call("canvas.read", path="missing.excalidraw")
        self.assertFalse(out["ok"])
        self.assertIn("no board at", out["error"])

    def test_list_reports_the_boards(self):
        self.call("canvas.draw", path="flow.excalidraw",
                  shapes=[{"kind": "rectangle"}])
        out = self.call("canvas.list")
        self.assertIn("flow.excalidraw", out["result"])

    # ---- updating ----

    def test_relabel_move_and_erase(self):
        drawn = self.call("canvas.draw", path="flow.excalidraw", shapes=[
            {"id": "a", "kind": "rectangle", "label": "old"},
            {"id": "b", "kind": "ellipse"}])
        a, b = drawn["ids"]["a"], drawn["ids"]["b"]
        before = next(e for e in self.live() if e["id"] == b)["x"]

        out = self.call("canvas.update", path="flow.excalidraw",
                        label=[{"id": a, "text": "new"}],
                        move=[{"id": b, "dx": 40, "dy": 0}])
        self.assertTrue(out["ok"], out.get("error"))
        text = canvas.describe_scene(self.board())
        self.assertIn("new", text)
        self.assertNotIn("old", text)
        after = next(e for e in self.live() if e["id"] == b)["x"]
        self.assertEqual(after, before + 40)

        out = self.call("canvas.update", path="flow.excalidraw", erase=[b])
        self.assertTrue(out["ok"], out.get("error"))
        self.assertNotIn(b, [e["id"] for e in self.live()])
        # tombstoned, not shredded: the editor can still bring it back
        self.assertIn(b, [e["id"] for e in self.board()["elements"]])

    def test_updating_an_element_that_is_not_there_reports_it(self):
        self.call("canvas.draw", path="flow.excalidraw",
                  shapes=[{"kind": "rectangle"}])
        out = self.call("canvas.update", path="flow.excalidraw",
                        erase=["nope"])
        self.assertFalse(out["ok"])
        self.assertIn("nope", out["error"])

    def test_an_update_with_nothing_in_it_is_refused(self):
        self.call("canvas.draw", path="flow.excalidraw",
                  shapes=[{"kind": "rectangle"}])
        out = self.call("canvas.update", path="flow.excalidraw")
        self.assertFalse(out["ok"])

    # ---- the rule everything on a board obeys ----

    def test_a_write_over_somebody_elses_edit_is_refused(self):
        """Between the read and the write, Helpwo saved the board."""
        import canvas_edit
        self.call("canvas.draw", path="flow.excalidraw",
                  shapes=[{"kind": "rectangle"}])
        path = os.path.join(self.tmp.name, "flow.excalidraw")

        original_editor = canvas_edit.BoardEditor

        class Slow(original_editor):
            """Reads the board, then somebody else writes it."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                theirs = canvas.read_scene(path)
                theirs["elements"] = list(theirs["elements"]) + [
                    canvas_edit.shape("diamond", 500, 500, 40, 40)]
                canvas.write_scene(path, theirs)

        canvas_edit.BoardEditor = Slow
        self.addCleanup(setattr, canvas_edit, "BoardEditor", original_editor)
        out = self.call("canvas.draw", path="flow.excalidraw",
                        shapes=[{"kind": "ellipse"}])
        self.assertFalse(out["ok"])
        self.assertIn("changed while", out["error"])
        self.assertNotIn("ellipse", [e["type"] for e in self.live()])
        self.assertIn("diamond", [e["type"] for e in self.live()])

    def test_the_tools_declare_that_they_touch_the_file_system(self):
        for name, expected in (("canvas.list", {"fs.read"}),
                               ("canvas.read", {"fs.read"}),
                               ("canvas.draw", {"fs.read", "fs.write"}),
                               ("canvas.update", {"fs.read", "fs.write"})):
            tool = self.registry.get(name)
            self.assertIsNotNone(tool, name)
            self.assertEqual(set(tool.capabilities), expected, name)


if __name__ == "__main__":
    unittest.main()
