"""Reverse tasks: the .retask format, the done gate, the tools and the viewer."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import context_router
import retask
import retask_view
import tools

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "retask_vectors.json").read_text(encoding="utf-8"))

SAMPLE = (
    "retask 1\n"
    "title: Integrate pay\n"
    "goal: one sandbox payment\n"
    "\n"
    "## [>] t1 Register merchant\n"
    'check: contains .env "MCH_ID="\n'
    "\n"
    "Sign up, then put the id in .env.\n"
    "\n"
    "## [ ] t2 Save a screenshot\n"
    "after: t1\n"
    "check: file_exists evidence/saved.png\n"
    'check: review "dashboard shows the callback saved"\n'
    "\n"
    "Save it to evidence/saved.png.\n"
)


def _as_dict(doc):
    return {
        "title": doc.title, "goal": doc.goal,
        "tasks": [{"id": t.id, "title": t.title, "status": t.status, "after": t.after,
                   "checks": t.checks, "notes": t.notes, "description": t.description}
                  for t in doc.tasks],
    }


class VectorTests(unittest.TestCase):
    def test_tokenize_vectors(self):
        for case in VECTORS["tokenize"]:
            self.assertEqual(retask.tokenize(case["line"]), case["tokens"], case["line"])

    def test_quote_round_trips_every_token(self):
        for case in VECTORS["tokenize"]:
            line = " ".join(retask.quote(t) for t in case["tokens"])
            self.assertEqual(retask.tokenize(line), case["tokens"])

    def test_parse_vectors(self):
        for case in VECTORS["parse"]:
            doc = retask.parse(case["text"])
            expect = dict(case["expect"])
            current, progress = expect.pop("current"), expect.pop("progress")
            self.assertEqual(_as_dict(doc), expect, case["name"])
            self.assertEqual(retask.current_task(doc).id, current, case["name"])
            self.assertEqual(list(retask.progress(doc)), progress, case["name"])

    def test_serialize_is_a_fixed_point(self):
        for case in VECTORS["parse"]:
            once = retask.serialize(retask.parse(case["text"]))
            self.assertEqual(retask.serialize(retask.parse(once)), once, case["name"])
            self.assertEqual(_as_dict(retask.parse(once)), _as_dict(retask.parse(case["text"])))

    def test_invalid_vectors(self):
        for case in VECTORS["invalid"]:
            if case.get("valid"):
                retask.parse(case["text"])
                continue
            with self.assertRaises(retask.RetaskError, msg=case["name"]):
                retask.parse(case["text"])


class DoneGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.path = os.path.join(self.root, "pay.retask")
        Path(self.path).write_text(SAMPLE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _done(self, doc, task_id, note=""):
        return retask.set_status(doc, task_id, retask.DONE, note=note,
                                 base_dir=self.root, root=self.root)

    def test_failing_check_rejects_with_the_gap(self):
        doc = retask.load(self.path)
        outcome = self._done(doc, "t1")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["status"], retask.REJECTED)
        self.assertIn(".env does not exist", outcome["gaps"][0])
        self.assertEqual(doc.task("t1").status, retask.REJECTED)
        self.assertIn("not done:", doc.task("t1").notes[-1])

    def test_passing_check_completes_and_advances(self):
        Path(self.root, ".env").write_text("MCH_ID=123\n", encoding="utf-8")
        doc = retask.load(self.path)
        outcome = self._done(doc, "t1", "saw MCH_ID in .env")
        self.assertTrue(outcome["ok"])
        self.assertEqual(doc.task("t1").status, retask.DONE)
        self.assertIn("passed: saw MCH_ID", doc.task("t1").notes[-1])
        self.assertEqual(doc.task("t2").status, retask.DOING)
        self.assertEqual(retask.current_task(doc).id, "t2")

    def test_dependency_blocks_completion(self):
        doc = retask.load(self.path)
        outcome = retask.set_status(doc, "t2", retask.SUBMITTED)
        self.assertFalse(outcome["ok"])
        self.assertIn("waits on t1", outcome["error"])

    def test_review_check_requires_an_evidence_note(self):
        Path(self.root, ".env").write_text("MCH_ID=1", encoding="utf-8")
        Path(self.root, "evidence").mkdir()
        Path(self.root, "evidence", "saved.png").write_bytes(b"png")
        doc = retask.load(self.path)
        self._done(doc, "t1", "ok")
        outcome = self._done(doc, "t2")
        self.assertFalse(outcome["ok"])
        self.assertIn("note", outcome["error"])
        self.assertEqual(doc.task("t2").status, retask.DOING)
        self.assertTrue(self._done(doc, "t2", "screenshot shows saved callback")["ok"])
        self.assertTrue(retask.is_finished(doc))

    def test_only_one_task_is_in_hand(self):
        doc = retask.parse(
            "retask 1\ntitle: x\n## [>] a one\n## [ ] b two\n")
        retask.set_status(doc, "b", retask.DOING)
        self.assertEqual([t.status for t in doc.tasks], [retask.TODO, retask.DOING])

    def test_claims_go_in_order(self):
        doc = retask.parse(
            "retask 1\ntitle: x\n## [>] t1 one\n## [ ] t2 two\n## [ ] t3 three\nafter: t2\n")
        self.assertEqual(retask.claim_block(doc, doc.task("t1")), "")
        self.assertEqual(retask.claim_block(doc, doc.task("t2")), "finish t1 first")
        self.assertEqual(retask.claim_block(doc, doc.task("t3")), "waits on t2")
        doc.task("t1").status = retask.SUBMITTED     # claimed but not checked yet
        self.assertEqual(retask.claim_block(doc, doc.task("t2")), "finish t1 first")
        doc.task("t1").status = retask.DONE
        self.assertEqual(retask.claim_block(doc, doc.task("t2")), "")
        doc.task("t2").status = retask.SKIPPED
        self.assertEqual(retask.claim_block(doc, doc.task("t3")), "")

    def test_check_paths_cannot_leave_the_workspace(self):
        gap = retask.run_check("file_exists ../../etc/passwd", self.root, self.root)
        self.assertIn("outside the workspace", gap)

    def test_check_kinds(self):
        Path(self.root, "a.md").write_text("answer 42\n" + "x" * 50, encoding="utf-8")
        self.assertIsNone(retask.run_check('matches a.md "\\b42\\b"', self.root, self.root))
        self.assertIsNotNone(retask.run_check('matches a.md "^43"', self.root, self.root))
        self.assertIsNone(retask.run_check("min_length a.md 40", self.root, self.root))
        self.assertIn("needs at least", retask.run_check("min_length a.md 999", self.root, self.root))
        self.assertIsNone(retask.run_check('review "anything"', self.root, self.root))


class MediaTests(unittest.TestCase):
    """Helpwo may put reference media in a description; the terminal does not show it."""

    TEXT = ("Open the settings page.\n"
            "![callback settings](refs/callback.png)\n"
            "![how to](https://cdn.example.com/howto.mp4?x=1)\n"
            "![](refs/blank.jpg)")

    def test_strip_media_leaves_placeholders(self):
        out = retask.strip_media(self.TEXT)
        self.assertIn("[image: callback settings]", out)
        self.assertIn("[video: how to]", out)
        self.assertIn("[image: untitled]", out)
        self.assertNotIn("refs/callback.png", out)

    def test_file_keeps_media_but_anchor_and_viewer_do_not(self):
        doc = retask.parse(
            "retask 1\ntitle: x\n## [>] t1 look\n\n" + self.TEXT + "\n")
        self.assertIn("![callback settings](refs/callback.png)", retask.serialize(doc))
        block = retask.anchor_text("x.retask", doc)
        self.assertIn("[image: callback settings]", block)
        self.assertNotIn("refs/callback.png", block)
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "x.retask")
            retask.save(path, doc)
            with create_pipe_input() as pipe:
                viewer = retask_view.RetaskViewer(path, root=root, input=pipe, output=DummyOutput())
                body = "".join(t for _, t in viewer._body_fragments())
                self.assertIn("[video: how to]", body)
                self.assertNotIn("howto.mp4", body)
                self.assertNotIn("refs/callback.png", viewer.task_text())

    def test_cli_skill_and_tool_do_not_teach_media(self):
        skill = (Path(__file__).parent.parent / "default_skills" / "retask" / "SKILL.md").read_text(encoding="utf-8")
        for word in ("image.describe", "screenshot", "photo", "video", "shot list"):
            self.assertNotIn(word, skill.lower().replace("no images or videos", ""), word)
        tool = tools.get_registry().get("retask.create")
        self.assertNotIn("footage", tool.description)


class FindAndAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        retask._find_cache.clear()

    def tearDown(self):
        retask._find_cache.clear()
        self.tmp.cleanup()

    def test_active_is_newest_unfinished_and_skips_build_dirs(self):
        finished = os.path.join(self.root, "old.retask")
        Path(finished).write_text("retask 1\ntitle: old\n## [x] t1 a\n", encoding="utf-8")
        time.sleep(0.01)
        open_path = os.path.join(self.root, "sub", "new.retask")
        os.makedirs(os.path.dirname(open_path))
        Path(open_path).write_text(SAMPLE, encoding="utf-8")
        os.makedirs(os.path.join(self.root, "node_modules"))
        Path(self.root, "node_modules", "x.retask").write_text(SAMPLE, encoding="utf-8")
        retask._find_cache.clear()
        self.assertEqual(retask.find_active(self.root), open_path)
        self.assertNotIn(os.path.join(self.root, "node_modules", "x.retask"),
                         retask.find_files(self.root))

    def test_anchor_carries_only_the_current_task_in_full(self):
        path = os.path.join(self.root, "pay.retask")
        Path(path).write_text(SAMPLE, encoding="utf-8")
        retask._find_cache.clear()
        block = retask.context_block(self.root)
        self.assertIn('<retask file="pay.retask" progress="0/2">', block)
        self.assertIn("t1 [doing] Register merchant  <- current", block)
        self.assertIn("Sign up, then put the id in .env.", block)
        self.assertNotIn("Save it to evidence/saved.png.", block)
        self.assertIn("t2 [todo] Save a screenshot", block)

    def test_no_list_means_no_block(self):
        self.assertEqual(retask.context_block(self.root), "")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ctx = tools.ToolCtx(cwd=self.root)
        retask._find_cache.clear()

    def tearDown(self):
        retask._find_cache.clear()
        self.tmp.cleanup()

    def _create(self):
        return tools._bi_retask_create({
            "title": "Integrate pay",
            "goal": "one payment",
            "tasks": [
                {"title": "Register", "description": "Go to the dashboard.",
                 "checks": ['contains .env "MCH_ID="']},
                {"title": "Pay once", "after": ["t1"], "checks": ['review "receipt shows success"']},
            ],
        }, self.ctx)

    def test_tools_are_registered(self):
        registry = tools.get_registry()
        for name in ("retask.create", "retask.update", "retask.read"):
            self.assertIsNotNone(registry.get(name), name)

    def test_create_writes_the_file_and_refuses_to_overwrite(self):
        result = self._create()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["retask"]["path"], "Integrate-pay.retask")
        self.assertEqual(result["retask"]["current"], "t1")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "Integrate-pay.retask")))
        self.assertFalse(self._create()["ok"])

    def test_update_done_runs_checks_and_saves_the_rejection(self):
        self._create()
        result = tools._bi_retask_update({"id": "t1", "status": "done"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertTrue(result["gaps"])
        self.assertEqual(result["retask"]["changes"][0]["status"], "rejected")
        doc = retask.load(os.path.join(self.root, "Integrate-pay.retask"))
        self.assertEqual(doc.task("t1").status, retask.REJECTED)

        Path(self.root, ".env").write_text("MCH_ID=9", encoding="utf-8")
        result = tools._bi_retask_update(
            {"id": "t1", "status": "done", "note": "MCH_ID present"}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["retask"]["current"], "t2")
        self.assertEqual([c["id"] for c in result["retask"]["changes"]], ["t1", "t2"])

    def test_update_edits_adds_and_removes(self):
        self._create()
        result = tools._bi_retask_update({
            "id": "t2", "description": "Pay with the sandbox card.",
            "add_tasks": [{"title": "Check the order", "after": ["t2"]}],
        }, self.ctx)
        self.assertTrue(result["ok"], result)
        doc = retask.load(os.path.join(self.root, "Integrate-pay.retask"))
        self.assertEqual(doc.task("t2").description, "Pay with the sandbox card.")
        self.assertEqual(doc.task("t3").after, ["t2"])
        result = tools._bi_retask_update({"id": "t2", "remove": True}, self.ctx)
        self.assertTrue(result["ok"], result)
        doc = retask.load(os.path.join(self.root, "Integrate-pay.retask"))
        self.assertIsNone(doc.task("t2"))
        self.assertEqual(doc.task("t3").after, [])

    def test_read_and_path_guard(self):
        self._create()
        result = tools._bi_retask_read({}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertIn("## [>] t1 Register", result["result"])
        self.assertFalse(tools._bi_retask_read({"path": "../x.retask"}, self.ctx)["ok"])
        self.assertFalse(tools._bi_retask_read({"path": "notes.md"}, self.ctx)["ok"])


class RoutingTests(unittest.TestCase):
    def test_chinese_and_english_wording_route_the_tools(self):
        registry = tools.get_registry().list()
        for query in ("带我一步步接入微信支付", "give me a quiz on react hooks"):
            names = context_router.select_tool_names(query, registry)
            self.assertIn("retask.create", names, query)
        plain = context_router.select_tool_names("fix the failing unit test", registry)
        self.assertNotIn("retask.create", plain)


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "pay.retask")
        Path(self.path).write_text(SAMPLE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _viewer(self, pipe):
        return retask_view.RetaskViewer(self.path, root=self.tmp.name,
                                        input=pipe, output=DummyOutput())

    def test_current_task_is_selected_and_unfolded(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(pipe)
            self.assertEqual(viewer.selected, "t1")
            self.assertEqual(viewer.expanded, {"t1"})
            text = "".join(t for _, t in viewer._body_fragments())
            self.assertIn("Sign up, then put the id in .env.", text)
            self.assertNotIn("Save it to evidence", text)

    def test_keys_move_fold_and_quit(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(pipe)
            pipe.send_text("j")
            pipe.send_text("\r")
            pipe.send_text("q")
            viewer.run()
            self.assertTrue(viewer._quit)
            self.assertEqual(viewer.selected, "t2")
            self.assertIn("t2", viewer.expanded)

    def test_s_submits_the_selected_task_to_the_file(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(pipe)
            pipe.send_text("s")
            pipe.send_text("q")
            viewer.run()
        self.assertEqual(retask.load(self.path).task("t1").status, retask.SUBMITTED)
        self.assertIn("submitted", viewer.flash)

    def test_mouse_is_off_so_the_terminal_can_select_text(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(pipe)
            self.assertFalse(viewer.mouse)
            self.assertFalse(viewer._app.mouse_support())
            viewer.toggle_mouse()
            self.assertTrue(viewer._app.mouse_support())

    def test_y_copies_the_selected_task_through_osc52(self):
        written = []
        output = mock.Mock()
        output.write_raw.side_effect = written.append
        with create_pipe_input() as pipe, \
                mock.patch.object(retask_view, "_system_clipboard", return_value=False), \
                mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("TMUX", None)
            viewer = self._viewer(pipe)
            via = retask_view.copy_to_clipboard(viewer.task_text(), output)
            text = viewer.task_text()
        self.assertEqual(via, "terminal")
        self.assertIn("t1 Register merchant", text)
        self.assertIn("Sign up, then put the id in .env.", text)
        self.assertIn("- .env contains 'MCH_ID='", text)
        import base64
        self.assertEqual(written[0], "\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07")

    def test_y_key_sets_a_flash(self):
        with create_pipe_input() as pipe, \
                mock.patch.object(retask_view, "copy_to_clipboard", return_value="terminal") as copy:
            viewer = self._viewer(pipe)
            pipe.send_text("y")
            pipe.send_text("q")
            viewer.run()
        self.assertTrue(copy.called)
        self.assertIn("copied t1", viewer.flash)

    def test_s_refuses_a_task_that_is_waiting(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(pipe)
            viewer.selected = "t2"
            viewer.submit()
        self.assertIn("waits on t1", viewer.flash)
        self.assertEqual(retask.load(self.path).task("t2").status, retask.TODO)

    def test_external_change_is_picked_up(self):
        with create_pipe_input() as pipe:
            viewer = self._viewer(pipe)
            time.sleep(0.01)
            doc = retask.load(self.path)
            doc.task("t1").status = retask.REJECTED
            retask.save(self.path, doc)
            os.utime(self.path, (time.time() + 5, time.time() + 5))
            viewer._header_fragments()
            self.assertEqual(viewer.doc.task("t1").status, retask.REJECTED)
            inspector = "".join(t for _, t in viewer._inspector_fragments())
            self.assertIn("rejected", inspector)
            self.assertIn('.env contains', inspector)


class ClaimOrderViewerTests(unittest.TestCase):
    def test_s_refuses_a_later_task_while_an_earlier_one_is_open(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "x.retask")
            Path(path).write_text(
                "retask 1\ntitle: x\n## [>] t1 one\n## [ ] t2 two\n", encoding="utf-8")
            with create_pipe_input() as pipe:
                viewer = retask_view.RetaskViewer(path, root=root, input=pipe, output=DummyOutput())
                viewer.selected = "t2"
                viewer.submit()
            self.assertIn("finish t1 first", viewer.flash)
            self.assertEqual(retask.load(path).task("t2").status, retask.TODO)


class CommandTests(unittest.TestCase):
    def test_retask_done_marks_submitted(self):
        import laintas_cli
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "pay.retask")
            Path(path).write_text(SAMPLE, encoding="utf-8")
            retask._find_cache.clear()
            cwd = os.getcwd()
            os.chdir(root)
            try:
                with mock.patch.object(laintas_cli, "console") as console:
                    laintas_cli._cmd_retask("done t1 put the id in")
            finally:
                os.chdir(cwd)
                retask._find_cache.clear()
            doc = retask.load(path)
            self.assertEqual(doc.task("t1").status, retask.SUBMITTED)
            self.assertIn("submitted: put the id in", doc.task("t1").notes[-1])
            self.assertTrue(console.print.called)


if __name__ == "__main__":
    unittest.main()
