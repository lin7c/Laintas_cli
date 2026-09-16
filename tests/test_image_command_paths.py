"""/img, /canvas and the image tools: the paths a person or an agent names.

Each case is a bug that shipped because the typed path and the agent path
were two implementations: /img asked the user's text model to look at the
picture, a filename with a space was read as its first word, a small TIFF or
iPhone photo went upstream under a MIME type nothing accepts, and a relative
path in an agent's own directory was opened against the process directory.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

import laintas_cli
import tools
import vision


def _write_png(path, size=(40, 30)):
    Image.new("RGB", size, (0, 128, 255)).save(path)
    return path


class _Quiet:
    def __enter__(self):
        self._patch = mock.patch.object(laintas_cli.console, "print")
        self.print = self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    @property
    def text(self):
        return "\n".join(str(c.args[0]) for c in self.print.call_args_list if c.args)


class ImageFormatTests(unittest.TestCase):

    def _fit(self, fmt, **save):
        buf = io.BytesIO()
        Image.new("RGB", (50, 40)).save(buf, format=fmt, **save)
        return vision._fit(buf.getvalue(), vision.DESCRIBE_MAX_EDGE)

    def test_formats_the_endpoints_refuse_are_re_encoded(self):
        for fmt, extra in (("TIFF", {}), ("BMP", {}),
                           ("MPO", {"save_all": True,
                                    "append_images": [Image.new("RGB", (50, 40))]})):
            with self.subTest(fmt=fmt):
                payload, mime = self._fit(fmt, **extra)
                self.assertEqual(mime, "image/png")
                self.assertEqual(Image.open(io.BytesIO(payload)).format, "PNG")

    def test_accepted_formats_still_pass_through(self):
        for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"),
                          ("WEBP", "image/webp"), ("GIF", "image/gif")):
            with self.subTest(fmt=fmt):
                self.assertEqual(self._fit(fmt)[1], mime)

    def test_a_photo_marked_rotated_is_sent_upright(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° clockwise to display
        buf = io.BytesIO()
        Image.new("RGB", (60, 20)).save(buf, format="JPEG", exif=exif)
        payload, _mime = vision._fit(buf.getvalue(), vision.DESCRIBE_MAX_EDGE)
        self.assertEqual(Image.open(io.BytesIO(payload)).size, (20, 60))


class CacheKeepsWhatTheHeaderShows(unittest.TestCase):

    def setUp(self):
        vision._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _write_png(os.path.join(self.tmp.name, "a.png"))

    def test_a_cached_answer_still_names_its_model(self):
        backend = lambda **kw: {"reply": "blue", "model": "vision-x"}
        vision.describe_image(self.path, "colour?", call_backend=backend)
        again = vision.describe_image(self.path, "colour?", call_backend=backend)
        self.assertTrue(again["cached"])
        self.assertEqual(again["model"], "vision-x")

    def test_a_cached_transcription_keeps_its_page_count(self):
        post = lambda route, body: (200, {"pages": [{"index": 0, "markdown": "hi"}],
                                          "pagesProcessed": 3})
        vision.image_to_text(self.path, post_json=post)
        again = vision.image_to_text(self.path, post_json=post)
        self.assertTrue(again["cached"])
        self.assertEqual(again["pages"], 3)


class LeadingPathArgTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spaced = _write_png(os.path.join(self.tmp.name, "my shot.png"))

    def test_quoted(self):
        self.assertEqual(laintas_cli._leading_path_arg(f'"{self.spaced}" why blank?'),
                         (self.spaced, "why blank?"))

    def test_escaped_spaces(self):
        escaped = self.spaced.replace(" ", "\\ ")
        self.assertEqual(laintas_cli._leading_path_arg(f"{escaped} why"),
                         (self.spaced, "why"))

    def test_unquoted_name_that_exists(self):
        self.assertEqual(laintas_cli._leading_path_arg(f"{self.spaced} what is it"),
                         (self.spaced, "what is it"))

    def test_a_missing_file_is_reported_as_the_word_typed(self):
        self.assertEqual(laintas_cli._leading_path_arg("nosuch.png what is it"),
                         ("nosuch.png", "what is it"))

    def test_empty(self):
        self.assertEqual(laintas_cli._leading_path_arg("   "), ("", ""))


class ImgCommandTests(unittest.TestCase):

    def setUp(self):
        vision._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spaced = _write_png(os.path.join(self.tmp.name, "my shot.png"))

    def test_img_asks_the_gateway_vision_route_not_the_chat_model(self):
        import backend_profiles
        import requests
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {
            "model": "vision-x", "choices": [{"message": {"content": "a blue box"}}]}
        with _Quiet() as out, \
                mock.patch.object(laintas_cli, "load_session", return_value={}), \
                mock.patch.object(laintas_cli, "get_backend_profile",
                                  return_value=mock.Mock(base_url="https://gw.example")), \
                mock.patch.object(backend_profiles, "request_auth", return_value=({}, {})), \
                mock.patch.object(laintas_cli, "call_backend_stream") as chat, \
                mock.patch.object(requests, "post", return_value=response) as post:
            laintas_cli._cmd_img(f'"{self.spaced}" what is it?')
        chat.assert_not_called()
        self.assertEqual(post.call_args.args[0], "https://gw.example/api/chat/vision")
        self.assertNotIn("model", post.call_args.kwargs["json"])
        self.assertIn("a blue box", out.text)

    def test_a_filename_with_a_space_is_one_path(self):
        with _Quiet(), \
                mock.patch.object(laintas_cli, "load_session", return_value={}), \
                mock.patch.object(vision, "describe_image",
                                  return_value={"text": "x", "model": "m"}) as describe:
            laintas_cli._cmd_img(f"{self.spaced} what is it")
        self.assertEqual(describe.call_args.args[:2], (self.spaced, "what is it"))

    def test_an_apostrophe_in_the_question_is_not_a_quoting_error(self):
        with _Quiet(), mock.patch.object(laintas_cli, "_cmd_img") as img:
            laintas_cli.handle_meta_command(
                "/img shot.png what's wrong here?", mock.Mock(), {})
        img.assert_called_once_with("shot.png what's wrong here?")

    def test_other_commands_still_reject_unbalanced_quotes(self):
        with self.assertRaises(laintas_cli.SlashCommandUsageError):
            laintas_cli._parse_slash_command("/config theme 'dark")


class CanvasCommandTests(unittest.TestCase):

    def test_text_without_a_path_prints_usage(self):
        with _Quiet() as out:
            laintas_cli._cmd_canvas("text")
        self.assertIn("a board path is required", out.text)


class ImageToolPathTests(unittest.TestCase):

    def setUp(self):
        vision._CACHE.clear()
        tools.register_builtin_tools()
        self.registry = tools.get_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _write_png(os.path.join(self.tmp.name, "a.png"))

    def test_a_relative_path_resolves_in_the_agents_directory(self):
        ctx = mock.Mock(session={}, cwd=self.tmp.name)
        seen = {}

        def backend(**kw):
            seen["ok"] = True
            return {"reply": "blue", "model": "m"}

        elsewhere = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, elsewhere)
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(tools, "_vision_backend", return_value=backend):
            out = self.registry.get("image.describe").invoke({"path": "a.png"}, ctx)
        self.assertTrue(out["ok"], out)
        self.assertTrue(seen.get("ok"))


if __name__ == "__main__":
    unittest.main()
