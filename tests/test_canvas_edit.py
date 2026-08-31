"""Drawing a board from the terminal: the parts that corrupt work if wrong.

Three classes of failure are pinned here, because all three are silent:

  * an element that is missing schema fields — it renders once and then
    misbehaves when somebody edits or undoes it in Excalidraw;
  * a deletion that removes the entry instead of tombstoning it — the element
    comes back the next time an older copy of the scene is merged in;
  * a write that lands on a file somebody else changed — Helpwo holds an open
    board in its editor, and overwriting it loses what they just drew.
"""

import json
import os
import tempfile
import unittest

import canvas
import canvas_edit


# What Excalidraw's own element type requires of every element. A skeleton
# that omits these is completed by `convertToExcalidrawElements` in the
# browser; nothing completes it here, so the factories have to.
REQUIRED = {"id", "type", "x", "y", "width", "height", "angle", "strokeColor",
            "backgroundColor", "fillStyle", "strokeWidth", "strokeStyle",
            "roughness", "opacity", "groupIds", "seed", "version",
            "versionNonce", "isDeleted", "boundElements", "updated", "link",
            "locked"}


class ElementShape(unittest.TestCase):
    def test_every_factory_produces_a_complete_element(self):
        made = [canvas_edit.shape("rectangle", 0, 0, 100, 50),
                canvas_edit.shape("ellipse", 0, 0, 100, 50),
                canvas_edit.shape("diamond", 0, 0, 100, 50),
                canvas_edit.text("hello", 10, 10),
                canvas_edit.arrow(0, 0, 50, 20)]
        for element in made:
            missing = REQUIRED - set(element)
            self.assertFalse(missing, f"{element['type']}: {sorted(missing)}")
            self.assertTrue(json.dumps(element))       # JSON-serialisable

    def test_a_bound_arrow_still_carries_its_own_geometry(self):
        """A binding attaches an arrow; it does not place it. Without x/y and
        a width, bound arrows are all drawn in the board's top-left corner."""
        a = canvas_edit.arrow(100, 100, 60, 0, start_id="s", end_id="e")
        self.assertEqual((a["x"], a["y"]), (100, 100))
        self.assertEqual(a["width"], 60)
        self.assertEqual(a["points"], [[0, 0], [60, 0]])
        self.assertEqual(a["startBinding"]["elementId"], "s")
        self.assertEqual(a["endBinding"]["elementId"], "e")

    def test_a_backwards_drag_is_still_a_rectangle(self):
        r = canvas_edit.shape("rectangle", 100, 80, -60, -40)
        self.assertEqual((r["x"], r["y"]), (40, 40))
        self.assertEqual((r["width"], r["height"]), (60, 40))

    def test_a_click_without_a_drag_does_not_make_a_sliver(self):
        r = canvas_edit.shape("rectangle", 10, 10, 0, 0)
        self.assertGreaterEqual(r["width"], canvas_edit.MIN_SIZE)
        self.assertGreaterEqual(r["height"], canvas_edit.MIN_SIZE)

    def test_ids_do_not_collide(self):
        """Ids are minted in tight loops (a shape and its label in the same
        millisecond), so the randomness has to survive that, not just survive
        a person clicking. 16 bits does not: this test caught it."""
        made = {canvas_edit.new_id() for _ in range(50000)}
        self.assertEqual(len(made), 50000)


class ElementOps(unittest.TestCase):
    def setUp(self):
        self.box = canvas_edit.shape("rectangle", 0, 0, 100, 50)
        self.elements = [self.box]

    def test_a_label_is_bound_from_both_sides(self):
        """Excalidraw reads the relationship from the container too; writing
        only the child's containerId makes the label jump out of its box the
        first time the box moves."""
        out = canvas_edit.label(self.elements, self.box["id"], "gateway")
        child = next(e for e in out if e["type"] == "text")
        container = next(e for e in out if e["id"] == self.box["id"])
        self.assertEqual(child["containerId"], self.box["id"])
        self.assertIn({"id": child["id"], "type": "text"},
                      container["boundElements"])

    def test_labelling_twice_edits_the_label(self):
        out = canvas_edit.label(self.elements, self.box["id"], "one")
        out = canvas_edit.label(out, self.box["id"], "two")
        texts = [e for e in out if e["type"] == "text" and not e["isDeleted"]]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]["text"], "two")
        self.assertEqual(texts[0]["originalText"], "two")

    def test_deleting_leaves_a_tombstone_and_takes_the_label_with_it(self):
        out = canvas_edit.label(self.elements, self.box["id"], "gateway")
        out = canvas_edit.delete(out, self.box["id"])
        self.assertEqual(len(out), 2)                  # nothing removed
        self.assertTrue(all(e["isDeleted"] for e in out))

    def test_an_edit_wins_over_an_older_copy(self):
        out = canvas_edit.move(self.elements, self.box["id"], 10, 0)
        moved = out[0]
        self.assertEqual(moved["version"], self.box["version"] + 1)
        self.assertNotEqual(moved["versionNonce"], self.box["versionNonce"])

    def test_binding_the_same_child_twice_records_it_once(self):
        out = canvas_edit.bind(self.elements, self.box["id"], "a1", "arrow")
        out = canvas_edit.bind(out, self.box["id"], "a1", "arrow")
        self.assertEqual(len(out[0]["boundElements"]), 1)


class Placement(unittest.TestCase):
    """Where things land. Both rules here were found by rendering the file in
    Excalidraw and looking at it: the editor lays labels and bound arrows out
    while you drag them, and nothing re-lays them out when a file is opened,
    so whatever this module writes is what gets drawn."""

    def test_a_label_is_centred_inside_its_container(self):
        box = canvas_edit.shape("rectangle", 100, 100, 200, 80)
        out = canvas_edit.label([box], box["id"], "gateway")
        child = next(e for e in out if e["type"] == "text")
        self.assertGreaterEqual(child["x"], box["x"])
        self.assertLessEqual(child["x"] + child["width"],
                             box["x"] + box["width"] + 0.01)
        self.assertAlmostEqual(child["y"] + child["height"] / 2,
                               box["y"] + box["height"] / 2, delta=1.0)
        self.assertEqual(child["textAlign"], "center")

    def test_an_arrow_starts_outside_the_shape_it_comes_from(self):
        src = canvas_edit.shape("rectangle", 0, 0, 200, 100)
        dst = canvas_edit.shape("ellipse", 400, 0, 200, 100)
        x, y = canvas_edit.edge_point(src, dst)
        self.assertGreater(x, src["x"] + src["width"])      # past the edge
        self.assertAlmostEqual(y, src["y"] + src["height"] / 2, delta=0.01)

    def test_edge_points_work_in_every_direction(self):
        src = canvas_edit.shape("rectangle", 100, 100, 100, 100)
        for dx, dy in ((300, 0), (-300, 0), (0, 300), (0, -300), (300, 300)):
            dst = canvas_edit.shape("rectangle", 100 + dx, 100 + dy, 100, 100)
            x, y = canvas_edit.edge_point(src, dst)
            inside = (src["x"] < x < src["x"] + src["width"]
                      and src["y"] < y < src["y"] + src["height"])
            self.assertFalse(inside, f"toward {(dx, dy)} started inside")

    def test_two_shapes_on_top_of_each_other_do_not_divide_by_zero(self):
        a = canvas_edit.shape("rectangle", 0, 0, 100, 100)
        b = dict(a, id="other")
        self.assertEqual(canvas_edit.edge_point(a, b), (50, 50))


class ArrowLabels(unittest.TestCase):
    def test_a_label_on_an_arrow_keeps_its_own_width(self):
        """Fitting it to the arrow's box squeezes a word like "autosaves"
        into the 80 pixels between two shapes, on top of the arrowhead."""
        a = canvas_edit.arrow(100, 100, 80, 0, start_id="s", end_id="e")
        out = canvas_edit.label([a], a["id"], "autosaves")
        child = next(e for e in out if e["type"] == "text")
        self.assertGreater(child["width"], a["width"])
        self.assertAlmostEqual(child["x"] + child["width"] / 2,
                               a["x"] + a["width"] / 2, delta=0.01)
        self.assertAlmostEqual(child["y"] + child["height"] / 2,
                               a["y"] + a["height"] / 2, delta=0.01)


class BatchDrawing(unittest.TestCase):
    """One write per diagram: five separate writes are five chances for the
    board to change underneath and leave half a diagram behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "b.excalidraw")
        canvas.write_scene(self.path, canvas.empty_scene())
        self.editor = canvas_edit.BoardEditor(self.path, canvas,
                                              author="ai", turn="run-1")

    def test_a_diagram_lands_in_one_write(self):
        writes = []
        real_write = canvas.write_scene

        def counting(path, scene, **kw):
            writes.append(path)
            return real_write(path, scene, **kw)

        self.editor.canvas = type("C", (), {
            "write_scene": staticmethod(counting),
            "read_scene": staticmethod(canvas.read_scene),
            "CanvasError": canvas.CanvasError})
        ok, msg, names = self.editor.draw_batch(
            [{"id": "a", "kind": "rectangle", "label": "one"},
             {"id": "b", "kind": "ellipse", "label": "two"}],
            [{"from": "a", "to": "b", "label": "then"}])
        self.assertTrue(ok, msg)
        self.assertEqual(len(writes), 1)
        self.assertEqual(sorted(names), ["a", "b"])

    def test_local_names_become_real_ids_and_connect_by_them(self):
        ok, _, names = self.editor.draw_batch(
            [{"id": "gw", "kind": "rectangle"}, {"id": "wk", "kind": "ellipse"}],
            [{"from": "gw", "to": "wk"}])
        self.assertTrue(ok)
        live = canvas.live_elements(canvas.read_scene(self.path))
        arrow = next(e for e in live if e["type"] == "arrow")
        self.assertEqual(arrow["startBinding"]["elementId"], names["gw"])
        self.assertEqual(arrow["endBinding"]["elementId"], names["wk"])

    def test_an_agents_work_is_tagged_for_review(self):
        """Helpwo's Show / Keep / Undo banner groups by author and turn; work
        tagged anything else lands on the board with no way to review it."""
        self.editor.draw_batch([{"kind": "rectangle", "label": "x"}])
        scene = canvas.read_scene(self.path)
        self.assertEqual(canvas.count_ai_turns(scene),
                         [{"turn": "run-1", "count": 1}])

    def test_a_person_drawing_is_not_tagged_as_the_ai(self):
        mine = canvas_edit.BoardEditor(self.path, canvas)      # default author
        mine.draw_shape("rectangle", 0, 0, 50, 50)
        self.assertEqual(canvas.count_ai_turns(canvas.read_scene(self.path)), [])

    def test_a_second_diagram_goes_below_the_first(self):
        self.editor.draw_batch([{"kind": "rectangle", "label": "first"}])
        first_bottom = canvas_edit.content_bottom(self.editor.elements)
        self.editor.draw_batch([{"kind": "rectangle", "label": "second"}])
        boxes = [e for e in canvas.live_elements(canvas.read_scene(self.path))
                 if e["type"] == "rectangle"]
        self.assertEqual(len(boxes), 2)
        self.assertGreaterEqual(max(b["y"] for b in boxes), first_bottom)

    def test_connecting_something_that_is_not_there_is_skipped(self):
        ok, _, _ = self.editor.draw_batch(
            [{"id": "a", "kind": "rectangle"}],
            [{"from": "a", "to": "ghost"}])
        self.assertTrue(ok)
        live = canvas.live_elements(canvas.read_scene(self.path))
        self.assertNotIn("arrow", [e["type"] for e in live])


class BoardEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "b.excalidraw")
        canvas.write_scene(self.path, canvas.empty_scene())
        self.editor = canvas_edit.BoardEditor(self.path, canvas)

    def _live(self):
        return canvas.live_elements(canvas.read_scene(self.path))

    def test_drawing_writes_the_file_immediately(self):
        ok, msg, new_id = self.editor.draw_shape("rectangle", 0, 0, 80, 40)
        self.assertTrue(ok, msg)
        live = self._live()
        self.assertEqual([e["id"] for e in live], [new_id])

    def test_an_arrow_binds_both_ends(self):
        _, _, a = self.editor.draw_shape("rectangle", 0, 0, 80, 40)
        _, _, b = self.editor.draw_shape("ellipse", 200, 0, 80, 40)
        ok, msg, arrow_id = self.editor.draw_arrow(
            self.editor.find(a), self.editor.find(b))
        self.assertTrue(ok, msg)
        for end in (a, b):
            bound = self.editor.find(end)["boundElements"]
            self.assertIn({"id": arrow_id, "type": "arrow"}, bound)

    def test_undo_puts_the_board_back(self):
        self.editor.draw_shape("rectangle", 0, 0, 80, 40)
        ok, _ = self.editor.undo()
        self.assertTrue(ok)
        self.assertEqual(self._live(), [])

    def test_undo_with_nothing_to_undo_is_not_an_error_that_writes(self):
        ok, msg = self.editor.undo()
        self.assertFalse(ok)
        self.assertIn("nothing", msg)

    def test_two_writes_in_one_timestamp_tick_are_still_caught(self):
        """A modification time is not a fingerprint: writes close enough
        together compare equal, and the guard then waves through the exact
        overwrite it exists to stop. This failed one run in three before the
        content digest was added."""
        theirs = canvas.empty_scene()
        theirs["elements"] = [canvas_edit.shape("ellipse", 5, 5, 20, 20)]
        canvas.write_scene(self.path, theirs)
        os.utime(self.path, (self.editor.mtime, self.editor.mtime))  # same tick

        ok, msg = self.editor.apply(
            canvas_edit.add(self.editor.elements,
                            canvas_edit.shape("rectangle", 0, 0, 80, 40)))
        self.assertFalse(ok, "an identical mtime let a concurrent write through")
        self.assertEqual([e["type"] for e in self._live()], ["ellipse"])

    def test_a_board_changed_underneath_is_refused_not_overwritten(self):
        """Helpwo has it open and just saved. Their work stays; ours does
        not land silently on top of it."""
        theirs = canvas.empty_scene()
        theirs["elements"] = [canvas_edit.shape("ellipse", 5, 5, 20, 20)]
        os.utime(self.path, (0, 0))                    # ensure a new mtime
        canvas.write_scene(self.path, theirs)

        ok, msg = self.editor.apply(
            canvas_edit.add(self.editor.elements,
                            canvas_edit.shape("rectangle", 0, 0, 80, 40)))
        self.assertFalse(ok)
        self.assertIn("reloaded", msg)
        live = self._live()
        self.assertEqual([e["type"] for e in live], ["ellipse"])
        # and the editor is now looking at what is really on disk
        self.assertEqual([e["type"] for e in self.editor.elements], ["ellipse"])

    def test_the_file_stays_a_readable_board_after_every_op(self):
        _, _, a = self.editor.draw_shape("rectangle", 0, 0, 80, 40)
        self.editor.set_label(a, "gateway")
        _, _, b = self.editor.draw_shape("diamond", 200, 0, 80, 40)
        self.editor.draw_arrow(self.editor.find(a), self.editor.find(b))
        self.editor.draw_text("note", 0, 200)
        self.editor.erase(b)
        scene = canvas.read_scene(self.path)            # parses, and…
        text = canvas.describe_scene(scene)              # …renders
        self.assertIn("gateway", text)
        self.assertEqual(scene["type"], "excalidraw")


if __name__ == "__main__":
    unittest.main()
