"""Videos for an agent that cannot watch one.

Same stance as vision.py: the agent model never holds the media. A video is
turned into text by separate one-shot calls, and only text comes back into the
loop. What differs is that a video is too big to hand over whole, so it is read
COARSE TO FINE instead of all at once:

  index     a timeline: duration, keyframes picked where the picture changed
            (not every N seconds), one caption per keyframe, and the speech
            transcribed per time window. Cheap, and enough to decide where to
            look next.
  describe  a question about the whole video or one range of it. A short range
            goes to a model that watches video natively (motion and sound
            included); a long one, or one the video lane cannot serve, is read
            as keyframes plus transcript by the vision lane.
  to_text   the speech, word for word, with timestamps. Reproduces rather than
            summarises — the video counterpart of image_to_text.
  frames    the keyframes as image files, no model call. From there the
            image.* tools read any one of them in detail.

Why keyframes by change and not by clock: most videos an agent is given are
screen recordings, which hold still most of the time. Sampling every second
pays to look at the same screen over and over, and still misses a toast that
is up for 400 ms between two samples. Comparing each frame with the last KEPT
one (not the previous one) also catches a slow scroll that never changes much
between neighbouring frames.

Model choice does not live here: /api/chat/video, /api/chat/audio and
/api/chat/vision each answer with the first live model of the family the
administrator ordered, exactly as image.describe relies on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from array import array
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

VIDEO_EXTENSIONS = frozenset((
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg",
    ".3gp", ".flv", ".wmv", ".ogv"))
# Not `.ts`: in a code workspace that is TypeScript far more often than an
# MPEG transport stream, and a /video listing full of source files is noise.

#: Longest video read at all. Transcription windows stretch to cover longer
#: inputs (see _window_seconds), but past this the index stops being a useful
#: summary and the scan pass takes minutes.
MAX_DURATION_S = 3 * 3600

#: A range at most this long is sent as a clip to the native video lane.
NATIVE_MAX_S = 120
#: …and the encoded clip has to stay under this. The gateway holds money on
#: the request's byte length before the call, so a fat clip is refused for
#: balance long before any model sees it.
NATIVE_MAX_BYTES = 4 * 1024 * 1024
#: 2 fps: what video models sample anyway (Gemini reads 1 fps), with headroom
#: for a lane model that samples differently.
NATIVE_FPS = 2
NATIVE_MAX_EDGE = 640

#: Keyframes.
SCAN_SIZE = (128, 72)               # grey thumbnails used only for comparing
SCAN_SAMPLE_BUDGET = 900            # frames compared over the whole range
SCAN_MAX_FPS = 4.0
CHANGE_PIXEL_DELTA = 24             # grey levels; below this is codec noise
CHANGE_THRESHOLD = 0.01             # share of pixels that must change
INDEX_MAX_FRAMES = 24
DESCRIBE_MAX_FRAMES = 12
FRAME_MAX_EDGE = 1280               # files written by frames()/index()
CAPTION_MAX_EDGE = 768              # what is sent to the vision lane
CAPTION_BATCH = 6                   # frames per vision call when captioning

#: Speech. 16 kHz mono WAV is what every recognizer and every OpenAI-compatible
#: fallback accepts; webm/m4a are refused by some (see the gateway's audio lane).
TRANSCRIBE_WINDOW_S = 60
TRANSCRIBE_MAX_WINDOWS = 20
SILENCE_PEAK = 300                  # int16 peak below which a window is skipped

CACHE_MAX_BYTES = 400 * 1024 * 1024
CACHE_MAX_VIDEOS = 16


class VideoError(RuntimeError):
    """A failure with a message meant for the agent's screen."""


# ── ffmpeg ───────────────────────────────────────────────────────────

def find_ffmpeg() -> str:
    """The ffmpeg binary: $LAINTAS_FFMPEG, PATH, then imageio-ffmpeg's."""
    explicit = os.environ.get("LAINTAS_FFMPEG", "").strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise VideoError(f"LAINTAS_FFMPEG points at {explicit}, which does not exist")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # bundles a static build
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if sys.platform == "darwin":
        hint = "brew install ffmpeg"
    elif sys.platform.startswith("win"):
        hint = "winget install ffmpeg"
    else:
        hint = "sudo apt install ffmpeg  (or: pip install imageio-ffmpeg)"
    raise VideoError(f"reading video needs ffmpeg, which is not installed. Install it: {hint}")


def _run(args: list, *, timeout: float, binary: bool = False) -> subprocess.CompletedProcess:
    cmd = [find_ffmpeg(), "-hide_banner", "-nostdin", *args]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=not binary, errors=None if binary else "replace")
    except subprocess.TimeoutExpired:
        raise VideoError(f"ffmpeg took longer than {timeout:.0f}s and was stopped")


# ── probing ──────────────────────────────────────────────────────────

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #\S+.*?: Video: .*?(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?) fps")
_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _hms(h, m, s) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def resolve_path(path: str) -> str:
    path = os.path.expanduser(str(path or "").strip())
    if not path:
        raise VideoError("a file path is required")
    if not os.path.isfile(path):
        raise VideoError(f"no such file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        raise VideoError(
            f"{ext or 'this file'} is not a video format this reads "
            f"({', '.join(sorted(VIDEO_EXTENSIONS))}). For a still image use image.describe.")
    return path


def file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PROBE_CACHE: dict = {}
_PROBE_LOCK = threading.Lock()


def probe(path: str) -> dict:
    """{path, digest, duration, width, height, fps, has_audio, size}."""
    path = resolve_path(path)
    stat = os.stat(path)
    key = (os.path.abspath(path), stat.st_size, stat.st_mtime_ns)
    with _PROBE_LOCK:
        if key in _PROBE_CACHE:
            return dict(_PROBE_CACHE[key])
    out = _run(["-i", path], timeout=60).stderr or ""
    if "Video:" not in out:
        raise VideoError("ffmpeg found no video stream in this file")
    video = _VIDEO_STREAM.search(out)
    fps = None
    for line in out.splitlines():
        if "Video:" in line:
            match = _FPS.search(line)
            fps = float(match.group(1)) if match else None
            break
    duration = None
    match = _DURATION.search(out)
    if match:
        duration = _hms(*match.groups())
    if not duration:
        # A browser's MediaRecorder writes WebM without a duration header.
        # Counting through the stream without decoding it is the reliable way.
        counted = _run(["-i", path, "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                       timeout=300).stderr or ""
        times = _TIME.findall(counted)
        duration = _hms(*times[-1]) if times else None
    if not duration:
        raise VideoError("could not determine how long this video is")
    info = {
        "path": path, "digest": file_digest(path), "duration": round(duration, 3),
        "width": int(video.group(1)) if video else None,
        "height": int(video.group(2)) if video else None,
        "fps": fps, "has_audio": "Audio:" in out, "size": stat.st_size,
    }
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = info
    return dict(info)


# ── time and ranges ──────────────────────────────────────────────────

def parse_time(text) -> float:
    """'75', '75.5', '1:15', '1:15.5', '1:02:03' -> seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    text = str(text or "").strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", text):
        raise VideoError(f"not a time: {text!r} (use seconds or m:ss)")
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


RANGE_RE = re.compile(r"^(\d[\d:.]*)?-(\d[\d:.]*)?$")


def parse_range(text: str) -> tuple:
    """'1:00-2:30', '90-', '-0:30' -> (start|None, end|None)."""
    match = RANGE_RE.match(str(text or "").strip())
    if not match or not (match.group(1) or match.group(2)):
        raise VideoError(f"not a range: {text!r} (use start-end, e.g. 1:00-2:30)")
    start = parse_time(match.group(1)) if match.group(1) else None
    end = parse_time(match.group(2)) if match.group(2) else None
    return start, end


def fmt_time(seconds: float, precise: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    frac = f".{int((seconds - whole) * 10)}" if precise else ""
    return f"{h}:{m:02d}:{s:02d}{frac}" if h else f"{m}:{s:02d}{frac}"


def clamp_range(info: dict, start=None, end=None) -> tuple:
    duration = info["duration"]
    s = 0.0 if start is None else parse_time(start)
    e = duration if end is None else parse_time(end)
    e = min(e, duration)
    if s >= e:
        raise VideoError(
            f"empty range {fmt_time(s)}–{fmt_time(e)}; the video is {fmt_time(duration)} long")
    return s, e


# ── cache ────────────────────────────────────────────────────────────

def _cache_root() -> str:
    try:
        import paths
        base = str(paths.LAINTAS_HOME)
    except Exception:
        base = os.path.expanduser("~/.laintas")
    return os.path.join(base, "cache", "video")


def cache_dir(digest: str) -> str:
    """Per-video directory for keyframes and paid results, kept bounded."""
    root = _cache_root()
    path = os.path.join(root, digest[:20])
    fresh = not os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    if fresh:
        _gc(root, keep=path)
    else:
        os.utime(path)
    return path


def _dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def _gc(root: str, keep: str) -> None:
    try:
        entries = [os.path.join(root, d) for d in os.listdir(root)]
    except OSError:
        return
    entries = [p for p in entries if os.path.isdir(p) and p != keep]
    entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    total = 0
    for i, path in enumerate(entries):
        total += _dir_size(path)
        if i + 1 >= CACHE_MAX_VIDEOS or total > CACHE_MAX_BYTES:
            shutil.rmtree(path, ignore_errors=True)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, value) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)
    os.replace(tmp, path)


# ── keyframes ────────────────────────────────────────────────────────

def _scan(info: dict, start: float, end: float) -> tuple:
    """Grey thumbnails across [start, end). Returns (fps, [bytes per frame])."""
    length = end - start
    fps = min(SCAN_MAX_FPS, max(0.05, SCAN_SAMPLE_BUDGET / length))
    w, h = SCAN_SIZE
    args = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
    if length > 20 * 60:
        # Decoding every frame of an hour of 1080p to look at 900 of them
        # takes minutes; at that scale the key frames are dense enough.
        args = ["-skip_frame", "nokey", *args]
    args += ["-i", info["path"], "-map", "0:v:0", "-an",
             "-vf", f"fps={fps:.4f},scale={w}:{h},format=gray",
             "-f", "rawvideo", "-"]
    proc = _run(args, timeout=max(120.0, length / 2), binary=True)
    data = proc.stdout or b""
    size = w * h
    frames = [data[i:i + size] for i in range(0, len(data) - size + 1, size)]
    if not frames:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise VideoError("ffmpeg could not decode any frames"
                         + (f": {err[-1]}" if err else ""))
    return fps, frames


def changed_fraction(a: bytes, b: bytes) -> float:
    from PIL import Image, ImageChops
    size = SCAN_SIZE
    diff = ImageChops.difference(Image.frombytes("L", size, a), Image.frombytes("L", size, b))
    mask = diff.point(lambda v: 255 if v > CHANGE_PIXEL_DELTA else 0)
    return mask.histogram()[255] / float(size[0] * size[1])


def select_keyframes(frames: list, max_frames: int, threshold: float = CHANGE_THRESHOLD) -> list:
    """Indices of frames worth looking at.

    A frame is kept when it differs from the last KEPT frame by `threshold`.
    During a transition (a page loading, a scroll) the kept frame slides to
    where the picture settles, so the model reads the result rather than the
    blur halfway through. The last frame is kept when it shows anything new:
    how a recording ends is usually what it was recorded for.
    """
    if not frames:
        return []
    n = len(frames)
    diffs: dict = {}

    def diff(i, j):
        key = (i, j)
        if key not in diffs:
            diffs[key] = changed_fraction(frames[i], frames[j])
        return diffs[key]

    def run(limit):
        kept = [0]
        i = 1
        while i < n:
            if diff(kept[-1], i) >= limit:
                settled = i
                while settled + 1 < n and settled - i < 8 and diff(settled, settled + 1) >= limit / 2:
                    settled += 1
                kept.append(settled)
                i = settled + 1
            else:
                i += 1
        if kept[-1] != n - 1 and diff(kept[-1], n - 1) >= limit / 2:
            kept.append(n - 1)
        return kept

    limit = threshold
    kept = run(limit)
    for _ in range(8):
        if len(kept) <= max_frames:
            return kept
        limit *= 1.6
        kept = run(limit)
    # Still too many (a video that never holds still): spread them evenly.
    step = (len(kept) - 1) / float(max_frames - 1)
    return sorted({kept[round(k * step)] for k in range(max_frames)})


def _extract_frame(info: dict, t: float, out: str, max_edge: int = FRAME_MAX_EDGE) -> str:
    if os.path.isfile(out):
        return out
    scale = (f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':"
             f"'if(gt(iw,ih),-2,min({max_edge},ih))'")
    proc = _run(["-ss", f"{t:.3f}", "-i", info["path"], "-frames:v", "1",
                 "-vf", scale, "-q:v", "3", "-y", out], timeout=60)
    if not os.path.isfile(out):
        err = (proc.stderr or "").strip().splitlines()
        raise VideoError(f"could not extract the frame at {fmt_time(t)}"
                         + (f": {err[-1]}" if err else ""))
    return out


def keyframes(info: dict, start: float, end: float, max_frames: int,
              progress: Optional[Callable[[str], None]] = None) -> list:
    """[{t, path}] — keyframes in [start, end) written to the cache."""
    folder = os.path.join(cache_dir(info["digest"]), "frames")
    os.makedirs(folder, exist_ok=True)
    memo = os.path.join(folder, f"pick-{start:.2f}-{end:.2f}-{max_frames}.json")
    picked = _read_json(memo)
    if not isinstance(picked, list):
        if progress:
            progress("Scanning for changes…")
        fps, frames = _scan(info, start, end)
        picked = [round(start + i / fps, 2) for i in select_keyframes(frames, max_frames)]
        _write_json(memo, picked)
    out = []
    for n, t in enumerate(picked):
        if progress:
            progress(f"Extracting frame {n + 1}/{len(picked)}…")
        path = os.path.join(folder, f"t{t:010.2f}.jpg")
        out.append({"t": t, "path": _extract_frame(info, t, path)})
    return out


# ── speech ───────────────────────────────────────────────────────────

_TRANSCRIBE_SYSTEM = (
    "Transcribe the speech in this audio clip word for word, in the language "
    "spoken. Output only the words. If nothing is said, output nothing.")


def _window_seconds(duration: float) -> float:
    return max(TRANSCRIBE_WINDOW_S, math.ceil(duration / TRANSCRIBE_MAX_WINDOWS))


def windows(info: dict, start: float, end: float) -> list:
    """[(index, ws, we)] of the global grid overlapping [start, end).

    A fixed grid over the whole video, not one cut per request, so a range
    asked about later reuses the windows an earlier index already paid for.
    """
    size = _window_seconds(info["duration"])
    first, last = int(start // size), int(math.ceil(end / size))
    return [(k, k * size, min((k + 1) * size, info["duration"])) for k in range(first, last)]


def _extract_wav(info: dict, ws: float, we: float) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "a.wav")
        _run(["-ss", f"{ws:.3f}", "-t", f"{we - ws:.3f}", "-i", info["path"],
              "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", out],
             timeout=120)
        if not os.path.isfile(out):
            raise VideoError(f"could not extract the audio at {fmt_time(ws)}")
        with open(out, "rb") as fh:
            return fh.read()


def _is_silent(wav: bytes) -> bool:
    pcm = array("h")
    body = wav[44:]
    pcm.frombytes(body[: len(body) - len(body) % 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    return not pcm or max(max(pcm), -min(pcm)) < SILENCE_PEAK


def _reply_text(result: dict) -> str:
    return str((result or {}).get("reply") or "").strip()


def transcript(info: dict, start: float, end: float, *, call_audio,
               progress: Optional[Callable[[str], None]] = None) -> list:
    """[{start, end, text}] for the grid windows overlapping the range.

    Each window is cached on disk once transcribed; silent windows are never
    sent (a recognizer fed silence still writes something).
    """
    if not info["has_audio"]:
        return []
    folder = os.path.join(cache_dir(info["digest"]), "speech")
    os.makedirs(folder, exist_ok=True)
    size = _window_seconds(info["duration"])
    todo, out = [], {}
    for k, ws, we in windows(info, start, end):
        memo = os.path.join(folder, f"w{k}-{size:g}.json")
        cached = _read_json(memo)
        if isinstance(cached, dict) and "text" in cached:
            out[k] = cached
        else:
            todo.append((k, ws, we, memo))

    done = [0]
    lock = threading.Lock()

    def one(job):
        k, ws, we, memo = job
        wav = _extract_wav(info, ws, we)
        text = ""
        if not _is_silent(wav):
            data_url = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
            result = call_audio(
                system_prompt=_TRANSCRIBE_SYSTEM,
                messages=[{"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": data_url, "format": "wav"}},
                    {"type": "text", "text": "Transcribe this audio."}]}],
                task_kind="audio") or {}
            if result.get("error"):
                raise VideoError(
                    "no enabled audio model could transcribe the speech "
                    f"(gateway: {str(result.get('reply') or '')[:200]})")
            text = _reply_text(result)
        entry = {"start": ws, "end": we, "text": text}
        _write_json(memo, entry)
        with lock:
            done[0] += 1
            if progress:
                progress(f"Transcribing {done[0]}/{len(todo)}…")
        return k, entry

    if todo:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for k, entry in pool.map(one, todo):
                out[k] = entry
    return [out[k] for k in sorted(out)]


def render_transcript(lines: list) -> str:
    rows = [f"[{fmt_time(x['start'])}–{fmt_time(x['end'])}] {x['text']}"
            for x in lines if x.get("text")]
    return "\n".join(rows)


# ── vision over frames ───────────────────────────────────────────────

def _frame_part(path: str) -> dict:
    import vision
    with open(path, "rb") as fh:
        raw = fh.read()
    payload, mime = vision._fit(raw, CAPTION_MAX_EDGE)
    return {"type": "image_url", "image_url": {"url": vision._data_url(payload, mime)}}


_CAPTION_SYSTEM = (
    "You are looking at frames taken from one video, in order, on behalf of an "
    "agent that cannot see them. Each frame is labelled with its timestamp. "
    "For every frame write exactly one line: `[timestamp] what is on screen`, "
    "naming any text, error, dialog or state that matters and what changed "
    "since the previous frame. Answer in the user's language. No preamble.")

_FRAMES_SYSTEM = (
    "You are looking at a video on behalf of an agent that cannot watch it. "
    "You are given its keyframes in order (each labelled with its timestamp) "
    "and, when it has speech, a timestamped transcript. Answer the question "
    "concretely, in the user's language, citing timestamps. Report what is "
    "actually shown, including anything that looks broken or wrong. Do not "
    "invent what happens between keyframes.")

_NATIVE_SYSTEM = (
    "You are watching a video on behalf of an agent that cannot watch it. "
    "Answer the question concretely, in the user's language, citing "
    "timestamps (relative to the start of this clip). Report what is "
    "actually shown and said, including anything that looks broken or wrong.")


def _vision_call(call_vision, system: str, content: list) -> dict:
    try:
        result = call_vision(system_prompt=system,
                             messages=[{"role": "user", "content": content}],
                             task_kind="vision") or {}
    except Exception as e:
        raise VideoError(f"the vision endpoint failed: {type(e).__name__}: {e}") from e
    if result.get("error"):
        raise VideoError(
            "no enabled vision model could read the frames. Configure the "
            "image-understanding model order in System -> API Keys. Gateway: "
            + str(result.get("reply") or "")[:200])
    text = _reply_text(result)
    if not text:
        raise VideoError("the vision endpoint returned an empty response")
    return {"text": text, "model": str(result.get("model") or "gateway vision priority")}


def caption(frames: list, *, call_vision, progress=None) -> tuple:
    """One line per keyframe, CAPTION_BATCH frames per vision call."""
    lines, model = [], ""
    batches = [frames[i:i + CAPTION_BATCH] for i in range(0, len(frames), CAPTION_BATCH)]
    for n, batch in enumerate(batches):
        if progress:
            progress(f"Reading frames {n + 1}/{len(batches)}…")
        content = []
        for f in batch:
            content.append({"type": "text", "text": f"[{fmt_time(f['t'], precise=True)}]"})
            content.append(_frame_part(f["path"]))
        content.append({"type": "text", "text": "One line per frame."})
        got = _vision_call(call_vision, _CAPTION_SYSTEM, content)
        lines.append(got["text"])
        model = got["model"]
    return "\n".join(lines), model


# ── public entry points ──────────────────────────────────────────────

def plan(info: dict, kind: str, start: float, end: float) -> dict:
    """Upper bound on the paid calls an operation makes, before making them."""
    speech = len(windows(info, start, end)) if info["has_audio"] else 0
    if kind == "index":
        return {"vision": math.ceil(INDEX_MAX_FRAMES / CAPTION_BATCH), "audio": speech}
    if kind == "text":
        return {"vision": 0, "audio": speech}
    if kind == "describe":
        if end - start <= NATIVE_MAX_S:
            return {"video": 1, "vision": 0, "audio": 0}
        return {"vision": 1, "audio": speech}
    return {"vision": 0, "audio": 0}


def index(path: str, *, call_vision, call_audio, captions: bool = True,
          progress=None) -> dict:
    info = probe(path)
    _check_length(info)
    folder = cache_dir(info["digest"])
    memo = os.path.join(folder, f"index-{int(captions)}.json")
    cached = _read_json(memo)
    if isinstance(cached, dict) and cached.get("text"):
        return {**cached, "cached": True}
    frames = keyframes(info, 0.0, info["duration"], INDEX_MAX_FRAMES, progress)
    caption_text, model = "", ""
    if captions:
        caption_text, model = caption(frames, call_vision=call_vision, progress=progress)
    speech = transcript(info, 0.0, info["duration"], call_audio=call_audio, progress=progress)

    head = (f"duration {fmt_time(info['duration'])} · "
            f"{info['width']}x{info['height']} · "
            f"{'has audio' if info['has_audio'] else 'no audio'} · "
            f"{len(frames)} keyframes")
    parts = [head, "", "Keyframes (picked where the picture changed):"]
    if caption_text:
        parts.append(caption_text)
    parts += [f"  {fmt_time(f['t'], precise=True)}  {f['path']}" for f in frames]
    spoken = render_transcript(speech)
    if info["has_audio"]:
        parts += ["", "Speech:", spoken or "(no speech recognized)"]
    result = {"ok": True, "text": "\n".join(parts), "model": model,
              "frames": frames, "duration": info["duration"]}
    _write_json(memo, result)
    return {**result, "cached": False}


def to_text(path: str, *, call_audio, start=None, end=None, progress=None) -> dict:
    info = probe(path)
    _check_length(info)
    if not info["has_audio"]:
        raise VideoError("this video has no audio track. For what is on screen, "
                         "use video.index or video.frames.")
    s, e = clamp_range(info, start, end)
    lines = transcript(info, s, e, call_audio=call_audio, progress=progress)
    text = render_transcript(lines)
    if not text:
        raise VideoError("no speech was recognized in this range")
    return {"ok": True, "text": text, "range": (s, e)}


def frames(path: str, *, start=None, end=None, max_frames: int = DESCRIBE_MAX_FRAMES,
           progress=None) -> dict:
    info = probe(path)
    _check_length(info)
    s, e = clamp_range(info, start, end)
    max_frames = max(1, min(int(max_frames or DESCRIBE_MAX_FRAMES), INDEX_MAX_FRAMES))
    got = keyframes(info, s, e, max_frames, progress)
    return {"ok": True, "frames": got, "range": (s, e)}


def _encode_clip(info: dict, start: float, end: float) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "clip.mp4")
        edge = NATIVE_MAX_EDGE
        scale = (f"scale='if(gt(iw,ih),min({edge},iw),-2)':'if(gt(iw,ih),-2,min({edge},ih))'")
        args = ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", info["path"],
                "-map", "0:v:0", "-vf", f"fps={NATIVE_FPS},{scale}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                "-pix_fmt", "yuv420p"]
        if info["has_audio"]:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "32k", "-ac", "1"]
        args += ["-movflags", "+faststart", "-y", out]
        proc = _run(args, timeout=max(60.0, (end - start) * 2))
        if not os.path.isfile(out):
            err = (proc.stderr or "").strip().splitlines()
            raise VideoError("could not encode the clip" + (f": {err[-1]}" if err else ""))
        with open(out, "rb") as fh:
            return fh.read()


_DESCRIBE_CACHE: dict = {}
_DESCRIBE_LOCK = threading.Lock()


def describe(path: str, question: str = "", *, call_video, call_vision, call_audio,
             start=None, end=None, progress=None) -> dict:
    """Answer a question about the video or a range of it.

    Returns {ok, text, model, mode: native|frames, range, cached[, fallback]}.
    """
    info = probe(path)
    _check_length(info)
    s, e = clamp_range(info, start, end)
    question = (question or "").strip() or "Describe what happens in this video."
    key = (info["digest"], round(s, 2), round(e, 2), question)
    with _DESCRIBE_LOCK:
        if key in _DESCRIBE_CACHE:
            return {**_DESCRIBE_CACHE[key], "cached": True}

    fallback = ""
    if e - s <= NATIVE_MAX_S:
        try:
            if progress:
                progress("Encoding clip…")
            clip = _encode_clip(info, s, e)
            if len(clip) > NATIVE_MAX_BYTES:
                raise VideoError(f"the clip encodes to {len(clip) / 1e6:.1f} MB")
            if progress:
                progress("Watching…")
            url = "data:video/mp4;base64," + base64.b64encode(clip).decode("ascii")
            result = call_video(
                system_prompt=_NATIVE_SYSTEM, task_kind="video",
                messages=[{"role": "user", "content": [
                    {"type": "video_url", "video_url": {"url": url}},
                    {"type": "text", "text": question}]}]) or {}
            text = _reply_text(result)
            if result.get("error") or not text:
                raise VideoError(str(result.get("reply") or "empty response")[:200])
            out = {"ok": True, "text": text, "mode": "native", "range": (s, e),
                   "model": str(result.get("model") or "gateway video priority")}
            with _DESCRIBE_LOCK:
                _DESCRIBE_CACHE[key] = out
            return {**out, "cached": False}
        except VideoError as err:
            # The frames path below answers the same question with less
            # motion; an error here is a reason to change path, not to fail.
            fallback = str(err)

    picked = keyframes(info, s, e, DESCRIBE_MAX_FRAMES, progress)
    speech = render_transcript(transcript(info, s, e, call_audio=call_audio, progress=progress)) \
        if info["has_audio"] else ""
    content = [{"type": "text", "text":
                f"Video range {fmt_time(s)}–{fmt_time(e)} of {fmt_time(info['duration'])}."}]
    for f in picked:
        content.append({"type": "text", "text": f"[{fmt_time(f['t'], precise=True)}]"})
        content.append(_frame_part(f["path"]))
    if speech:
        content.append({"type": "text", "text": "Speech transcript:\n" + speech})
    content.append({"type": "text", "text": "Question: " + question})
    if progress:
        progress("Looking…")
    got = _vision_call(call_vision, _FRAMES_SYSTEM, content)
    out = {"ok": True, "text": got["text"], "model": got["model"], "mode": "frames",
           "range": (s, e)}
    if fallback:
        out["fallback"] = fallback
    with _DESCRIBE_LOCK:
        if len(_DESCRIBE_CACHE) > 64:
            _DESCRIBE_CACHE.pop(next(iter(_DESCRIBE_CACHE)), None)
        _DESCRIBE_CACHE[key] = out
    return {**out, "cached": False}


def _check_length(info: dict) -> None:
    if info["duration"] > MAX_DURATION_S:
        raise VideoError(
            f"the video is {fmt_time(info['duration'])} long, over the "
            f"{fmt_time(MAX_DURATION_S)} this reads. Cut the part you need first.")
