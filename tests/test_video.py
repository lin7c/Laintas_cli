"""Reading videos on behalf of a model that cannot watch one.

The properties worth pinning:

  * keyframes follow CHANGE, not the clock: a still screen costs one frame
    however long it is held, and a brief change between two clock ticks is
    still caught;
  * a frame is compared with the last KEPT frame, so a slow drift that never
    changes much between neighbours is still noticed;
  * the transcript grid is global, so a range asked about later reuses the
    windows an index already paid for, and silence is never sent;
  * when the video lane cannot serve, describe falls back to keyframes plus
    transcript instead of failing.

The tests that need ffmpeg build their inputs with ffmpeg's own generators
and are skipped where it is not installed.
"""
import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
import wave
from unittest import mock

import laintas_cli  # configures account paths before tools is imported
import tools
import video


def _frame(value: int) -> bytes:
    w, h = video.SCAN_SIZE
    return bytes([value]) * (w * h)


def _with_box(base: int, box: int, x0: int, y0: int, size: int) -> bytes:
    w, h = video.SCAN_SIZE
    data = bytearray([base]) * (w * h)
    for y in range(y0, min(h, y0 + size)):
        for x in range(x0, min(w, x0 + size)):
            data[y * w + x] = box
    return bytes(data)


def _ffmpeg():
    try:
        return video.find_ffmpeg()
    except video.VideoError:
        return None


class TimeAndRanges(unittest.TestCase):
    def test_times(self):
        self.assertEqual(video.parse_time("75"), 75)
        self.assertEqual(video.parse_time("1:15"), 75)
        self.assertEqual(video.parse_time("1:02:03"), 3723)
        self.assertAlmostEqual(video.parse_time("0:01.5"), 1.5)
        with self.assertRaises(video.VideoError):
            video.parse_time("1m15")

    def test_ranges(self):
        self.assertEqual(video.parse_range("1:00-2:30"), (60, 150))
        self.assertEqual(video.parse_range("90-"), (90, None))
        self.assertEqual(video.parse_range("-0:45"), (None, 45))
        with self.assertRaises(video.VideoError):
            video.parse_range("-")

    def test_clamp_refuses_an_empty_range(self):
        info = {"duration": 30.0}
        self.assertEqual(video.clamp_range(info, None, 99), (0.0, 30.0))
        with self.assertRaises(video.VideoError):
            video.clamp_range(info, 40, None)


class Keyframes(unittest.TestCase):
    def test_a_still_screen_is_one_frame_however_long(self):
        frames = [_frame(40)] * 300
        self.assertEqual(video.select_keyframes(frames, 24), [0])

    def test_a_brief_toast_is_caught(self):
        # A 10x10 box on 128x72 is ~1.1% of the picture, up for 2 samples.
        toast = _with_box(40, 230, 100, 5, 10)
        frames = [_frame(40)] * 50 + [toast] * 2 + [_frame(40)] * 50
        kept = video.select_keyframes(frames, 24)
        self.assertIn(50, kept)
        # …and the screen going back to normal is a change too.
        self.assertTrue(any(i > 51 for i in kept))

    def test_slow_drift_is_noticed_against_the_last_kept_frame(self):
        # Each step moves the box one pixel: neighbours barely differ, but
        # forty steps on, the picture has clearly changed.
        frames = [_with_box(40, 230, x, 20, 20) for x in range(0, 100, 1)]
        self.assertGreater(len(video.select_keyframes(frames, 24)), 1)

    def test_a_transition_keeps_where_it_settles(self):
        frames = ([_frame(20)] * 10 + [_frame(80), _frame(140)] + [_frame(200)] * 10)
        kept = video.select_keyframes(frames, 24)
        self.assertEqual(kept[0], 0)
        self.assertEqual(frames[kept[1]], _frame(200))

    def test_the_cap_holds_for_a_video_that_never_holds_still(self):
        frames = [_frame((i * 37) % 256) for i in range(400)]
        kept = video.select_keyframes(frames, 12)
        self.assertLessEqual(len(kept), 12)
        self.assertEqual(kept[0], 0)


class Speech(unittest.TestCase):
    def test_the_window_grid_is_global(self):
        info = {"duration": 300.0, "has_audio": True}
        all_windows = video.windows(info, 0, 300)
        part = video.windows(info, 70, 130)
        self.assertEqual([w[0] for w in part], [1, 2])
        self.assertTrue(set(part) <= set(all_windows))

    def test_windows_stretch_for_long_videos(self):
        info = {"duration": 3 * 3600.0, "has_audio": True}
        self.assertLessEqual(len(video.windows(info, 0, info["duration"])),
                             video.TRANSCRIBE_MAX_WINDOWS)

    def test_silence_is_detected(self):
        def wav(peak):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((int(peak).to_bytes(2, "little", signed=True)) * 1600)
            return buf.getvalue()
        self.assertTrue(video._is_silent(wav(10)))
        self.assertFalse(video._is_silent(wav(5000)))


class _Clip(unittest.TestCase):
    """A generated clip: red 0-4s, blue 4-8s, with a tone."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = os.path.join(cls.tmp.name, "home")
        cls.path = os.path.join(cls.tmp.name, "clip.mp4")
        subprocess.run([
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:s=320x180:d=4:r=10",
            "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=4:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-y", cls.path], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        home = tempfile.mkdtemp(dir=self.tmp.name)
        patcher = mock.patch.object(video, "_cache_root",
                                    lambda: os.path.join(home, "cache", "video"))
        patcher.start()
        self.addCleanup(patcher.stop)
        video._PROBE_CACHE.clear()
        video._DESCRIBE_CACHE.clear()


@unittest.skipIf(_ffmpeg() is None, "ffmpeg is not installed")
class WithFfmpeg(_Clip):
    def test_probe(self):
        info = video.probe(self.path)
        self.assertAlmostEqual(info["duration"], 8.0, delta=0.3)
        self.assertEqual((info["width"], info["height"]), (320, 180))
        self.assertTrue(info["has_audio"])

    def test_keyframes_are_one_per_colour(self):
        out = video.frames(self.path)
        times = [f["t"] for f in out["frames"]]
        self.assertEqual(len(times), 2, times)
        self.assertLess(times[0], 1)
        self.assertAlmostEqual(times[1], 4, delta=0.6)
        for f in out["frames"]:
            self.assertTrue(os.path.isfile(f["path"]))

    def test_transcript_is_cached_per_window(self):
        calls = []

        def call_audio(**kw):
            part = kw["messages"][0]["content"][0]
            self.assertEqual(part["type"], "input_audio")
            self.assertTrue(part["input_audio"]["data"].startswith("data:audio/wav;base64,"))
            calls.append(1)
            return {"reply": "a tone", "model": "whisper"}

        first = video.to_text(self.path, call_audio=call_audio)
        self.assertIn("a tone", first["text"])
        video.to_text(self.path, call_audio=call_audio, start=2, end=6)
        self.assertEqual(len(calls), 1)

    def test_a_short_range_is_watched_natively(self):
        sent = {}

        def call_video(**kw):
            part = kw["messages"][0]["content"][0]
            sent["url"] = part["video_url"]["url"]
            return {"reply": "red, then blue", "model": "gemini"}

        out = video.describe(self.path, "what colours?", call_video=call_video,
                             call_vision=None, call_audio=None)
        self.assertEqual(out["mode"], "native")
        self.assertTrue(sent["url"].startswith("data:video/mp4;base64,"))
        clip = base64.b64decode(sent["url"].split(",", 1)[1])
        self.assertLess(len(clip), video.NATIVE_MAX_BYTES)

    def test_falls_back_to_frames_when_the_video_lane_cannot_serve(self):
        seen = {}

        def call_video(**_kw):
            return {"error": True, "reply": "HTTP 503: Video understanding unavailable"}

        def call_vision(**kw):
            content = kw["messages"][0]["content"]
            seen["images"] = sum(1 for p in content if p.get("type") == "image_url")
            seen["speech"] = any("Speech transcript" in p.get("text", "") for p in content)
            return {"reply": "red then blue", "model": "gemma"}

        out = video.describe(self.path, "what colours?", call_video=call_video,
                             call_vision=call_vision,
                             call_audio=lambda **_kw: {"reply": "beep"})
        self.assertEqual(out["mode"], "frames")
        self.assertIn("503", out["fallback"])
        self.assertEqual(seen["images"], 2)
        self.assertTrue(seen["speech"])

    def test_index_is_cached_on_disk(self):
        vision_calls = []

        def call_vision(**kw):
            vision_calls.append(1)
            return {"reply": "[0:00] red\n[0:04] blue", "model": "gemma"}

        audio = lambda **_kw: {"reply": "beep"}
        first = video.index(self.path, call_vision=call_vision, call_audio=audio)
        self.assertIn("blue", first["text"])
        self.assertIn("Speech:", first["text"])
        video._PROBE_CACHE.clear()
        second = video.index(self.path, call_vision=call_vision, call_audio=audio)
        self.assertTrue(second["cached"])
        self.assertEqual(len(vision_calls), 1)

    def test_a_non_video_is_refused_with_a_pointer(self):
        other = os.path.join(self.tmp.name, "shot.png")
        open(other, "wb").write(b"x")
        with self.assertRaises(video.VideoError) as ctx:
            video.probe(other)
        self.assertIn("image.describe", str(ctx.exception))


class Tools(unittest.TestCase):
    def test_tools_are_registered_with_both_capabilities(self):
        names = {"video.index", "video.describe", "video.to_text", "video.frames"}
        registry = tools.get_registry()
        for name in names:
            self.assertIsNotNone(registry.get(name), name)
        for name in names:
            self.assertEqual(tools.infer_capabilities(name) & {"fs.read", "network"},
                             {"fs.read", "network"})

    def test_a_video_error_becomes_a_tool_error(self):
        ctx = tools.ToolCtx(cwd=tempfile.gettempdir())
        out = tools._bi_video_frames({"path": "missing.mp4"}, ctx)
        self.assertFalse(out["ok"])
        self.assertIn("video.frames", out["error"])


@unittest.skipIf(_ffmpeg() is None, "ffmpeg is not installed")
class Command(_Clip):
    """/video reads the same way the agent's tools do."""

    def _run(self, raw, **lanes):
        printed = []
        transports = {"call_video": lanes.get("video"), "call_vision": lanes.get("vision"),
                      "call_audio": lanes.get("audio")}
        with mock.patch.object(laintas_cli.console, "print",
                               side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))), \
             mock.patch.object(laintas_cli, "load_session", return_value={}), \
             mock.patch.object(laintas_cli.tools_mod, "video_transports",
                               return_value=transports):
            laintas_cli._cmd_video(raw)
        return "\n".join(printed)

    def test_range_then_question(self):
        asked = {}

        def call_video(**kw):
            asked["q"] = kw["messages"][0]["content"][1]["text"]
            return {"reply": "it turns blue", "model": "gemini"}

        out = self._run(f"{self.path} 3-6 what happens at the cut?", video=call_video)
        self.assertEqual(asked["q"], "what happens at the cut?")
        self.assertIn("0:03–0:06", out)
        self.assertIn("it turns blue", out)

    def test_frames_needs_no_model(self):
        out = self._run(f"frames {self.path}")
        self.assertIn("Keyframes of", out)
        self.assertEqual(out.count(".jpg"), 2)

    def test_missing_file(self):
        out = self._run("frames nope.mp4")
        self.assertIn("no such file", out)


if __name__ == "__main__":
    unittest.main()
