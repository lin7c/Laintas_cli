"""Images for an agent that cannot see.

The agent model is text-only — deepseek-v4-flash, and most of what users run.
Rather than thread multimodal content through the kernel (messages, compaction
and history are strings end to end), the image is read by a SEPARATE model in
one shot and what comes back into the agent loop is text. The agent never
holds a picture, so nothing about it has to change when the user switches
models, and a cheap T1 session does not have to become a T3 session to look at
one screenshot.

Two entry points, because they answer different questions and their outputs are
not interchangeable:

  describe_image   a vision model ANSWERS A QUESTION about the image. Lossy on
                   purpose: it summarises, judges, and reports what is wrong.
                   For "does this layout look right", "what is this diagram
                   saying", "why does this screenshot look broken".

  image_to_text    OCR REPRODUCES the image as text — page by page, layout
                   preserved, tables and headings intact. For scans, documents,
                   receipts, anything where a summary would be a data loss.

They are deliberately not one function with a mode flag. The agent has to pick
between "tell me about it" and "write it out", and a single tool whose output
shape changes with an argument is a tool whose result the agent cannot predict.

Resolution is handled oppositely for the same reason: describe downscales hard
(base64 inflates the gateway's token estimate, and a vision model does not read
a 4K screenshot any better than a 1024px one), while OCR keeps as much detail
as the transport allows, because small text is exactly what it is being asked
to read.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import threading
from typing import Optional

#: Long edge for the vision path. Helpwo settled on this value for its own
#: describe_image and it has held: enough for UI review, small enough that the
#: base64 does not dominate the request.
DESCRIBE_MAX_EDGE = 1024
DESCRIBE_JPEG_QUALITY = 85

#: Long edge for the OCR path. Not 1024: OCR is being asked to read small text,
#: and downscaling to the vision path's size turns body copy into grey mush.
#: This is a transport ceiling, not a quality choice — most images pass through
#: untouched.
OCR_MAX_EDGE = 3000

#: Refuse before decoding. A camera original or a mis-typed path pointing at a
#: video would otherwise be loaded into memory in full first.
MAX_FILE_BYTES = 20 * 1024 * 1024

#: The gateway's own inline cap is 8M characters of data URL; staying well
#: under it means the refusal, when it comes, is ours and explains itself.
OCR_MAX_DATA_URL_CHARS = 6_000_000

IMAGE_EXTENSIONS = frozenset(
    (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"))

#: Model ordering deliberately does not live in the CLI.  The gateway's
#: /api/chat/vision endpoint owns the independently configurable vision order,
#: while those same models remain eligible for ordinary text chat.  Keeping a
#: second list here would let an old CLI silently override the administrator's
#: current choice.

_DESCRIBE_SYSTEM = (
    "You are looking at an image on behalf of an agent that cannot see it. "
    "Answer the question concretely and in the user's language. Report what is "
    "actually in the image, including anything that looks broken, blank, "
    "misaligned or cut off — a rendering fault is usually the most useful "
    "thing you can report. Do not speculate about what is outside the frame."
)

#: (sha256, kind, question) -> text. An agent loop re-asks the same thing more
#: often than a person does — a retry after a failed edit, a second pass over
#: the same screenshot — and each repeat is a paid call on a tier the session
#: is not otherwise paying. Bounded because a long session can accumulate a lot
#: of screenshots.
_CACHE: "dict[tuple, str]" = {}
_CACHE_MAX = 64
_CACHE_LOCK = threading.Lock()


class VisionError(RuntimeError):
    """A failure with a message meant for the agent's screen."""


def _cache_get(key):
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _cache_put(key, value):
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = value


def _read_image(path: str) -> tuple[bytes, str]:
    """Return (raw bytes, sha256). Raises VisionError with a usable message."""
    path = os.path.expanduser(str(path or "").strip())
    if not path:
        raise VisionError("a file path is required")
    if not os.path.isfile(path):
        raise VisionError(f"no such file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        raise VisionError(
            f"{ext or 'this file'} is not an image format this reads "
            f"({', '.join(sorted(IMAGE_EXTENSIONS))}). For a PDF, use "
            f"image.to_text, which handles documents directly.")
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise VisionError(
            f"image is {size / 1e6:.1f} MB, over the {MAX_FILE_BYTES / 1e6:.0f} MB "
            f"limit — resize it first")
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw, hashlib.sha256(raw).hexdigest()


def _fit(raw: bytes, max_edge: int, *, jpeg_quality: int = 0) -> tuple[bytes, str]:
    """Downscale to fit `max_edge`. Returns (bytes, mime).

    An image already inside the box is returned untouched rather than
    re-encoded: re-encoding a PNG screenshot as JPEG to "normalise" it would
    add compression artefacts to the crisp text edges that are the whole point
    of reading it.
    """
    try:
        from PIL import Image
    except ImportError:
        raise VisionError(
            "Pillow is required to prepare images (pip install Pillow)")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise VisionError(f"could not decode the image ({type(e).__name__}: {e})")

    if max(img.size) <= max_edge:
        mime = Image.MIME.get(img.format or "", "") or "image/png"
        return raw, mime

    scale = max_edge / float(max(img.size))
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.convert("RGB").resize(new_size, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=jpeg_quality or DESCRIBE_JPEG_QUALITY)
    return out.getvalue(), "image/jpeg"


def _data_url(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


# ── describe: a vision model answers a question ──────────────────────

def describe_image(path: str, question: str = "", *, session=None,
                   call_backend=None, models=None, list_models=None) -> dict:
    """Ask a vision model about an image; return {ok, text, model}.

    `call_backend` is injected rather than imported so this module does not
    depend on laintas_cli (which imports it) and so the tests can drive it
    without a backend.
    """
    raw, digest = _read_image(path)
    question = (question or "").strip() or "Describe this image."

    key = (digest, "describe", question)
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "text": cached, "model": "(cached)", "cached": True}

    payload, mime = _fit(raw, DESCRIBE_MAX_EDGE)
    data_url = _data_url(payload, mime)
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": question},
        ],
    }]

    if call_backend is None:
        raise VisionError("no backend callable was provided")

    # `models` and `list_models` remain accepted for source compatibility with
    # extensions built against older CLI releases, but selection now belongs
    # exclusively to the gateway's vision endpoint.
    try:
        result = call_backend(
            session=session, message="", system_prompt=_DESCRIBE_SYSTEM,
            current_path=os.getcwd(), messages=messages,
            tools_enabled=False, model_override=None,
            task_kind="vision") or {}
    except Exception as e:
        raise VisionError(
            "the vision endpoint could not read this image (the file itself "
            f"was read and decoded fine): {type(e).__name__}: {e}") from e
    if result.get("error"):
        detail = str(result.get("reply") or "vision service unavailable")[:300]
        raise VisionError(
            "no enabled vision model could read this image (the file itself "
            "was read and decoded fine). Configure the image-understanding "
            f"model order in System -> API Keys. Gateway response: {detail}")
    text = (result.get("reply") or "").strip()
    if not text:
        raise VisionError("the vision endpoint returned an empty response")
    model = str(result.get("model") or "gateway vision priority")
    _cache_put(key, text)
    return {"ok": True, "text": text, "model": model, "cached": False}


# ── to_text: OCR reproduces the image as text ────────────────────────

def image_to_text(path: str, *, session=None, post_json=None,
                  pages: Optional[list] = None) -> dict:
    """Reproduce a document or image as text via the gateway's /api/ocr.

    `post_json(route, body) -> (status, json)` is injected for the same reason
    `call_backend` is above.
    """
    ext = os.path.splitext(os.path.expanduser(str(path or "")))[1].lower()
    if ext == ".pdf":
        # PDFs go up whole: OCR is per page, and splitting one here would
        # multiply the page count the caller is billed for.
        path_expanded = os.path.expanduser(path)
        if not os.path.isfile(path_expanded):
            raise VisionError(f"no such file: {path}")
        size = os.path.getsize(path_expanded)
        if size > MAX_FILE_BYTES:
            raise VisionError(
                f"PDF is {size / 1e6:.1f} MB, over the "
                f"{MAX_FILE_BYTES / 1e6:.0f} MB limit")
        with open(path_expanded, "rb") as fh:
            raw = fh.read()
        digest = hashlib.sha256(raw).hexdigest()
        data_url = _data_url(raw, "application/pdf")
    else:
        raw, digest = _read_image(path)
        payload, mime = _fit(raw, OCR_MAX_EDGE, jpeg_quality=92)
        data_url = _data_url(payload, mime)

    if len(data_url) > OCR_MAX_DATA_URL_CHARS:
        raise VisionError(
            f"the encoded document is {len(data_url) / 1e6:.1f} M characters, "
            f"over the {OCR_MAX_DATA_URL_CHARS / 1e6:.0f} M inline limit — "
            f"split it or reduce its resolution first")

    key = (digest, "ocr", str(pages or ""))
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "text": cached, "pages": None, "cached": True}

    if post_json is None:
        raise VisionError("no backend callable was provided")

    body = {"dataUrl": data_url}
    if pages:
        body["pages"] = pages
    status, data = post_json("/api/ocr", body)
    if status == 503:
        raise VisionError(
            "no OCR model is available — add one in the admin page under "
            "System -> API Keys, or ask an administrator to "
            "switch one back on")
    if status != 200:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("title") or "")
        raise VisionError(f"OCR failed (HTTP {status}) {detail}"[:300])

    rendered = "\n\n".join(
        (f"--- page {p.get('index', i)} ---\n{p.get('markdown', '')}"
         for i, p in enumerate(data.get("pages") or []))).strip()
    if not rendered:
        raise VisionError(
            "OCR returned no text. If this is a photograph rather than a "
            "document, image.describe is the tool that reads it.")
    _cache_put(key, rendered)
    return {"ok": True, "text": rendered,
            "pages": data.get("pagesProcessed"), "cached": False,
            "model": data.get("model", "")}
