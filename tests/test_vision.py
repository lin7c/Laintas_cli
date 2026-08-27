"""Reading images on behalf of a model that cannot see one.

The properties worth pinning are the ones that go wrong quietly:

  * describe and to_text prepare the SAME image differently, and getting that
    backwards is invisible until someone tries to read a scanned page — a
    document downscaled to the vision path's 1024px is unreadable body copy,
    and a photo sent at full resolution to the vision path is money burnt on
    pixels the model does not use;
  * when every vision model refuses, the error has to point at the models. An
    agent told only "failed" starts checking the path, and the path was fine;
  * a repeat of the same question must not be a second paid call — an agent
    loop re-asks far more often than a person does.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

import vision


def _png(width, height, path):
    Image.new("RGB", (width, height), (255, 0, 0)).save(path)
    return path


class Preparation(unittest.TestCase):
    def test_describe_downscales_but_to_text_keeps_resolution(self):
        """The two paths want opposite things from the same file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _png(2400, 1200, os.path.join(tmp, "big.png"))
            raw = open(path, "rb").read()

            small, mime = vision._fit(raw, vision.DESCRIBE_MAX_EDGE)
            self.assertEqual(mime, "image/jpeg")
            self.assertEqual(max(Image.open(io.BytesIO(small)).size),
                             vision.DESCRIBE_MAX_EDGE)

            wide, _mime = vision._fit(raw, vision.OCR_MAX_EDGE)
            # 2400 already fits under the OCR ceiling, so it is passed through
            # untouched rather than re-encoded — re-encoding a screenshot as
            # JPEG adds artefacts to exactly the text edges OCR must read.
            self.assertEqual(wide, raw)

    def test_an_image_already_small_enough_is_not_re_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _png(400, 300, os.path.join(tmp, "small.png"))
            raw = open(path, "rb").read()
            out, mime = vision._fit(raw, vision.DESCRIBE_MAX_EDGE)
        self.assertEqual(out, raw)
        self.assertEqual(mime, "image/png")

    def test_a_pdf_is_refused_by_describe_and_pointed_at_to_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "doc.pdf")
            open(path, "wb").write(b"%PDF-1.4\n")
            with self.assertRaises(vision.VisionError) as caught:
                vision.describe_image(path, call_backend=lambda **kw: {})
        self.assertIn("image.to_text", str(caught.exception))

    def test_an_oversized_file_is_refused_before_it_is_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "huge.png")
            open(path, "wb").write(b"\x00" * (vision.MAX_FILE_BYTES + 1))
            with mock.patch.object(vision, "_fit") as fit:
                with self.assertRaises(vision.VisionError):
                    vision.describe_image(path, call_backend=lambda **kw: {})
            fit.assert_not_called()


class DescribeFallback(unittest.TestCase):
    def setUp(self):
        vision._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _png(80, 60, os.path.join(self.tmp.name, "a.png"))

    def test_it_walks_the_chain_until_one_answers(self):
        tried = []

        def backend(**kw):
            tried.append(kw["model_override"])
            if kw["model_override"] != "qwen3.6-plus":
                return {"error": True, "reply": "model is switched off"}
            return {"reply": "a red square"}

        out = vision.describe_image(self.path, "what is it?",
                                    call_backend=backend)
        self.assertEqual(out["text"], "a red square")
        self.assertEqual(out["model"], "qwen3.6-plus")
        # Stops at the first one that answers, having tried the earlier
        # entries in preference order and nothing after.
        expected = list(vision.VISION_MODELS)
        expected = expected[:expected.index("qwen3.6-plus") + 1]
        self.assertEqual(tried, expected)

    def test_when_every_model_refuses_the_error_blames_the_models(self):
        out = vision.describe_image
        with self.assertRaises(vision.VisionError) as caught:
            out(self.path, call_backend=lambda **kw: {"error": True,
                                                      "reply": "no capacity"})
        message = str(caught.exception)
        self.assertIn("no vision model", message)
        # Says plainly that the file was fine, so nobody goes path-hunting.
        self.assertIn("decoded fine", message)
        for model in vision.VISION_MODELS:
            self.assertIn(model, message)

    def test_an_empty_reply_is_a_failure_not_an_answer(self):
        with self.assertRaises(vision.VisionError):
            vision.describe_image(self.path,
                                  call_backend=lambda **kw: {"reply": "   "})

    def test_the_same_question_is_not_paid_for_twice(self):
        calls = []

        def backend(**kw):
            calls.append(1)
            return {"reply": "a red square"}

        first = vision.describe_image(self.path, "what?", call_backend=backend)
        second = vision.describe_image(self.path, "what?", call_backend=backend)
        self.assertEqual(len(calls), 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["text"], first["text"])

    def test_a_different_question_is_a_different_call(self):
        calls = []

        def backend(**kw):
            calls.append(1)
            return {"reply": "answer"}

        vision.describe_image(self.path, "what?", call_backend=backend)
        vision.describe_image(self.path, "how many?", call_backend=backend)
        self.assertEqual(len(calls), 2)


class CatalogueIntersection(unittest.TestCase):
    """A chain that names models nobody enabled is the tesseract bug again."""

    def setUp(self):
        vision._CACHE.clear()
        vision._catalogue = (0.0, frozenset())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _png(80, 60, os.path.join(self.tmp.name, "a.png"))

    def test_only_models_the_deployment_serves_are_tried(self):
        tried = []

        def backend(**kw):
            tried.append(kw["model_override"])
            return {"reply": "ok"}

        served = [{"id": "deepseek-v4-flash"}, {"id": "google/gemma-4-26b-a4b-it"}]
        vision.describe_image(self.path, call_backend=backend,
                              list_models=lambda: served)
        self.assertEqual(tried, ["google/gemma-4-26b-a4b-it"])

    def test_an_unreadable_catalogue_does_not_disable_the_feature(self):
        """A catalogue that failed to load is not evidence that no model can
        see, so the full preference order is still tried."""
        tried = []

        def backend(**kw):
            tried.append(kw["model_override"])
            return {"reply": "ok"}

        def boom():
            raise RuntimeError("gateway unreachable")

        vision.describe_image(self.path, call_backend=backend, list_models=boom)
        self.assertEqual(tried, [vision.VISION_MODELS[0]])

    def test_when_nothing_is_enabled_the_error_names_what_to_switch_on(self):
        served = [{"id": "deepseek-v4-flash"}]
        with self.assertRaises(vision.VisionError) as caught:
            vision.describe_image(
                self.path, call_backend=lambda **kw: {"error": True, "reply": "off"},
                list_models=lambda: served)
        message = str(caught.exception)
        self.assertIn("admin page", message)
        self.assertIn("doubao-seed-2.0-pro", message)


class ToText(unittest.TestCase):
    def setUp(self):
        vision._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _png(80, 60, os.path.join(self.tmp.name, "a.png"))

    def test_pages_are_rendered_with_their_numbers(self):
        def post(route, body):
            self.assertEqual(route, "/api/ocr")
            self.assertTrue(body["dataUrl"].startswith("data:image/"))
            return 200, {"pages": [{"index": 0, "markdown": "# One"},
                                   {"index": 1, "markdown": "Two"}],
                         "pagesProcessed": 2, "model": "mistral-ocr-latest"}

        out = vision.image_to_text(self.path, post_json=post)
        self.assertIn("--- page 0 ---", out["text"])
        self.assertIn("# One", out["text"])
        self.assertEqual(out["pages"], 2)

    def test_no_ocr_model_says_where_to_turn_one_on(self):
        with self.assertRaises(vision.VisionError) as caught:
            vision.image_to_text(self.path,
                                 post_json=lambda r, b: (503, {"detail": "x"}))
        self.assertIn("System -> API Keys", str(caught.exception))

    def test_an_empty_transcription_points_at_describe(self):
        """A photograph has nothing to transcribe, and saying only 'no text'
        leaves the agent with no next move."""
        with self.assertRaises(vision.VisionError) as caught:
            vision.image_to_text(self.path,
                                 post_json=lambda r, b: (200, {"pages": []}))
        self.assertIn("image.describe", str(caught.exception))

    def test_a_pdf_goes_up_whole(self):
        path = os.path.join(self.tmp.name, "doc.pdf")
        open(path, "wb").write(b"%PDF-1.4\n" + b"x" * 100)
        seen = {}

        def post(route, body):
            seen.update(body)
            return 200, {"pages": [{"index": 0, "markdown": "text"}],
                         "pagesProcessed": 1}

        vision.image_to_text(path, post_json=post, pages=[0])
        self.assertTrue(seen["dataUrl"].startswith("data:application/pdf;base64,"))
        self.assertEqual(seen["pages"], [0])


class ToolWiring(unittest.TestCase):
    """The tools as the agent actually reaches them.

    Everything above tests vision.py directly, and that is how two real bugs
    got through: the tool layer looked up the backend profile on the wrong
    module and never imported requests. Neither is visible unless the tool is
    invoked the way the agent invokes it.
    """

    def setUp(self):
        import tools
        vision._CACHE.clear()
        tools.register_builtin_tools()
        self.registry = tools.get_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _png(60, 40, os.path.join(self.tmp.name, "a.png"))
        self.ctx = mock.Mock(session={}, cwd=self.tmp.name)

    def _invoke(self, name, params):
        return self.registry.get(name).invoke(params, self.ctx)

    def test_both_tools_are_registered_with_the_right_capabilities(self):
        import tools
        for name in ("image.describe", "image.to_text"):
            tool = self.registry.get(name)
            self.assertIsNotNone(tool, name)
            # Reads a local file AND sends its contents off the machine —
            # either label alone understates it.
            self.assertEqual(sorted(tools.infer_capabilities(name)),
                             ["fs.read", "network"])

    def test_describe_reaches_the_backend_and_names_the_model(self):
        import tools
        with mock.patch.object(tools, "_vision_backend",
                               return_value=lambda **kw: {"reply": "a red square"}), \
                mock.patch.object(tools, "_vision_catalogue", return_value=None):
            out = self._invoke("image.describe", {"path": self.path, "question": "what?"})
        self.assertTrue(out["ok"], out)
        # The model is named because this call is billed on a different tier
        # from the session that made it.
        self.assertIn(vision.VISION_MODELS[0], out["result"])
        self.assertIn("a red square", out["result"])

    def test_to_text_posts_to_the_gateways_ocr_route(self):
        import tools
        seen = {}

        def post_json(route, body):
            seen["route"] = route
            return 200, {"pages": [{"index": 0, "markdown": "hello"}], "pagesProcessed": 1}

        with mock.patch.object(tools, "_gateway_post_json", return_value=post_json):
            out = self._invoke("image.to_text", {"path": self.path})
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen["route"], "/api/ocr")
        self.assertIn("hello", out["result"])

    def test_a_failure_comes_back_as_a_tool_error_not_an_exception(self):
        import tools
        with mock.patch.object(tools, "_gateway_post_json",
                               return_value=lambda r, b: (503, {"detail": "off"})):
            out = self._invoke("image.to_text", {"path": self.path})
        self.assertFalse(out["ok"])
        self.assertIn("image.to_text", out["error"])

    def test_a_missing_path_is_refused_before_any_request(self):
        """Asserted on the request, not on building the caller: constructing
        the closure costs nothing, sending a request costs money."""
        import tools
        sent = []
        with mock.patch.object(tools, "_gateway_post_json",
                               return_value=lambda r, b: sent.append(r) or (200, {})):
            out = self._invoke("image.to_text", {"path": ""})
        self.assertFalse(out["ok"])
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
