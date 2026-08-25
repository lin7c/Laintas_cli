"""/img and /canvas: the argument shape, and reading a board from the terminal.

Both commands share one rule for their second argument, and it is the rule
that decides whether a mistyped path gets a useful answer. The tempting
version — "does this token look like one of our files?" — turns every path
without a recognised extension into "unknown action", which sends the user to
the help text when what they needed was "no such file". So the verbs are a
small closed set and everything else is a path; these tests pin that direction.

The board rendering rules mirror Helpwo's canvasScene.ts. That duplication is
the reason both sides assert the same properties: a bound label is folded onto
its container's line, an arrow says what it connects, tombstones are gone.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import canvas
import laintas_cli
import vision


IS_IMAGE = (lambda tok: os.path.splitext(tok)[1].lower()
            in {".png", ".jpg"} or tok.lower().endswith(".pdf"))


class ArgumentShape(unittest.TestCase):
    def test_a_known_verb_is_a_verb(self):
        self.assertEqual(
            laintas_cli._split_verb("text a.png", ("text", "list"), IS_IMAGE),
            ("text", "a.png"))

    def test_anything_that_is_not_a_verb_is_a_path(self):
        """The direction that matters. `nosuch` is a file the user got wrong,
        not an action they invented."""
        verb, rest = laintas_cli._split_verb(
            "nosuch what is this", ("text", "list"), IS_IMAGE)
        self.assertEqual(verb, "")
        self.assertEqual(rest, "nosuch what is this")

    def test_a_file_named_like_a_verb_is_still_the_file(self):
        verb, rest = laintas_cli._split_verb(
            "text.png what is this", ("text", "list"), IS_IMAGE)
        self.assertEqual(verb, "")
        self.assertTrue(rest.startswith("text.png"))

    def test_the_default_action_keeps_the_whole_argument(self):
        # The caller re-splits `rest` itself, so the path must still be in it.
        verb, rest = laintas_cli._split_verb(
            "a.png how many boxes?", ("text",), IS_IMAGE)
        self.assertEqual(verb, "")
        self.assertEqual(rest, "a.png how many boxes?")

    def test_no_arguments_is_neither(self):
        self.assertEqual(laintas_cli._split_verb("  ", ("text",), IS_IMAGE), ("", ""))

    def test_img_still_accepts_the_older_flag(self):
        """People will keep typing --text; it means what `text` means."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            open(path, "wb").write(b"\x89PNG\r\n\x1a\n")
            # _cmd_img imports vision inside the function, so the module
            # itself is what has to be patched.
            with mock.patch.object(vision, "image_to_text",
                                   return_value={"text": "t", "pages": 1}) as to_text, \
                    mock.patch.object(vision, "describe_image") as describe:
                laintas_cli._cmd_img(f"{path} --text")
            to_text.assert_called_once()
            describe.assert_not_called()


class QuickStart(unittest.TestCase):
    """Bare `/canvas` has to produce a canvas, not a question."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)

    def _boards(self):
        return sorted(f for f in os.listdir(".") if f.endswith(".excalidraw"))

    def test_it_names_a_board_when_there_is_none(self):
        path, needs_creating = canvas.scratch_board(".")
        self.assertTrue(needs_creating)
        self.assertTrue(os.path.basename(path).startswith("canvas-"))
        self.assertTrue(path.endswith(".excalidraw"))

    def test_running_it_twice_reuses_the_empty_board(self):
        """Otherwise opening the canvas five times leaves five empty files."""
        path, _ = canvas.scratch_board(".")
        canvas.write_scene(path, canvas.empty_scene())
        again, needs_creating = canvas.scratch_board(".")
        self.assertEqual(os.path.abspath(again), os.path.abspath(path))
        self.assertFalse(needs_creating)

    def test_a_board_with_work_on_it_is_never_reused(self):
        path, _ = canvas.scratch_board(".")
        scene = canvas.empty_scene()
        scene["elements"] = [{"id": "r", "type": "rectangle", "x": 0, "y": 0,
                              "width": 10, "height": 10}]
        canvas.write_scene(path, scene)
        fresh, needs_creating = canvas.scratch_board(".")
        self.assertNotEqual(os.path.abspath(fresh), os.path.abspath(path))
        self.assertTrue(needs_creating)

    def test_somebody_elses_empty_board_is_left_alone(self):
        """`plan.excalidraw` not drawn on yet is still their file, not a
        scratch pad to hand back when someone asks for a new canvas."""
        canvas.write_scene("plan.excalidraw", canvas.empty_scene())
        path, needs_creating = canvas.scratch_board(".")
        self.assertTrue(needs_creating)
        self.assertNotIn("plan", os.path.basename(path))

    def test_the_command_creates_and_reports_without_a_terminal(self):
        with mock.patch.object(laintas_cli, "_canvas_can_view",
                               return_value=False):
            laintas_cli._cmd_canvas("")
        self.assertEqual(len(self._boards()), 1)
        # and a second run adds nothing
        with mock.patch.object(laintas_cli, "_canvas_can_view",
                               return_value=False):
            laintas_cli._cmd_canvas("")
        self.assertEqual(len(self._boards()), 1)

    def test_the_command_opens_the_viewer_when_there_is_a_terminal(self):
        with mock.patch.object(laintas_cli, "_canvas_can_view",
                               return_value=True), \
                mock.patch.object(laintas_cli, "_canvas_view",
                                  return_value=True) as view:
            laintas_cli._cmd_canvas("")
        view.assert_called_once()
        self.assertTrue(view.call_args[0][0].endswith(".excalidraw"))

    def test_list_still_lists(self):
        canvas.write_scene("plan.excalidraw", canvas.empty_scene())
        with mock.patch.object(laintas_cli.console, "print") as printed:
            laintas_cli._cmd_canvas("list")
        text = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("plan.excalidraw", text)


class BoardFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "b.excalidraw")

    def _write(self, elements):
        scene = canvas.empty_scene()
        scene["elements"] = elements
        canvas.write_scene(self.path, scene)

    def test_a_corrupt_board_is_reported_not_replaced(self):
        """Substituting an empty board is how a session's work disappears
        behind a message that sounds like success."""
        open(self.path, "w").write("{not json")
        with self.assertRaises(canvas.CanvasError) as caught:
            canvas.read_scene(self.path)
        self.assertIn("not a readable", str(caught.exception))

    def test_a_write_is_refused_when_the_file_moved_under_us(self):
        """Helpwo holds an open board in the editor and writes the file behind
        it; overwriting that silently loses whatever the person just drew."""
        self._write([])
        stale = os.path.getmtime(self.path) - 5
        with self.assertRaises(canvas.CanvasError) as caught:
            canvas.write_scene(self.path, canvas.empty_scene(), expect_mtime=stale)
        self.assertIn("open in Helpwo", str(caught.exception))
        # And nothing was written.
        self.assertEqual(json.load(open(self.path))["elements"], [])

    def test_a_write_with_the_matching_mtime_goes_through(self):
        self._write([])
        canvas.write_scene(self.path, canvas.empty_scene(),
                           expect_mtime=os.path.getmtime(self.path))

    def test_the_description_folds_labels_and_names_arrow_ends(self):
        self._write([
            {"id": "gw", "type": "rectangle", "x": 80, "y": 80, "width": 200, "height": 90},
            {"id": "gwl", "type": "text", "containerId": "gw", "text": "网关",
             "x": 0, "y": 0, "width": 1, "height": 1},
            {"id": "a1", "type": "arrow", "x": 290, "y": 125, "width": 120, "height": 0,
             "startBinding": {"elementId": "gw"}, "endBinding": {"elementId": "cli"}},
            {"id": "dead", "type": "rectangle", "x": 0, "y": 0, "width": 1,
             "height": 1, "isDeleted": True},
        ])
        text = canvas.describe_scene(canvas.read_scene(self.path))
        self.assertIn('gw  rectangle  "网关"', text)
        self.assertNotIn("gwl", text)
        self.assertIn("gw → cli", text)
        self.assertNotIn("dead", text)
        self.assertTrue(text.startswith("3 element(s)"))

    def test_ai_turns_count_what_can_be_seen(self):
        self._write([
            {"id": "r", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1,
             "customData": {"author": "ai", "turn": "t1"}},
            {"id": "l", "type": "text", "containerId": "r", "text": "x",
             "x": 0, "y": 0, "width": 1, "height": 1,
             "customData": {"author": "ai", "turn": "t1"}},
            {"id": "mine", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1},
        ])
        turns = canvas.count_ai_turns(canvas.read_scene(self.path))
        self.assertEqual(turns, [{"turn": "t1", "count": 1}])

    def test_only_excalidraw_files_are_boards(self):
        self.assertTrue(canvas.is_canvas_path("a/b/plan.excalidraw"))
        self.assertTrue(canvas.is_canvas_path("PLAN.EXCALIDRAW"))
        self.assertFalse(canvas.is_canvas_path("plan.excalidraw.bak"))
        with self.assertRaises(canvas.CanvasError):
            canvas.read_scene("notes.md")

    def test_find_boards_skips_the_directories_nobody_draws_in(self):
        for folder in ("node_modules", ".git", "src"):
            os.makedirs(os.path.join(self.tmp.name, folder), exist_ok=True)
            canvas.write_scene(
                os.path.join(self.tmp.name, folder, "x.excalidraw"), canvas.empty_scene())
        found = [os.path.relpath(p, self.tmp.name) for p in canvas.find_boards(self.tmp.name)]
        self.assertIn(os.path.join("src", "x.excalidraw"), found)
        self.assertNotIn(os.path.join("node_modules", "x.excalidraw"), found)
        self.assertFalse([f for f in found if f.startswith(".git")])


if __name__ == "__main__":
    unittest.main()
