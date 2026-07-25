"""Outbound secret/PII redaction — capability #2 of the model fleet.

This guards the network boundary: before any context is uploaded to the cloud
model, secrets and PII in the user's messages *and in tool outputs* (a `cat .env`
result flows upstream as a role:tool message) can be scrubbed. It is the ML
generalization target of a regex detector — the same "capture first, upgrade to
a trained model later" discipline as ``precheck.py``:

  * **capture** — ``scrub_text`` weak-labels every outbound text block with the
    regex detector and appends a *privacy-safe* NER row (redacted text + typed
    spans, never the raw secret) to ``.laintas/redact_samples.jsonl``. This
    corpus trains the token-level detector that will catch what regex misses.
  * **enforce** — when enabled, the same pass actually replaces spans with typed
    placeholders in the outbound payload. Default OFF so we measure before we
    mutate what the model sees.

Runs on THIS machine, upstream of the upload — placing it server-side would
defeat the purpose (the data would already have left). Two runtime flags gate
it: ``redact_capture`` (default True) and ``redact_enforce`` (default False).

Nothing here may raise into the agent loop; callers wrap defensively and the
module also guards its own IO.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

import paths


# ── Detector taxonomy ─────────────────────────────────────────────────────
# (type, compiled pattern). Order matters: earlier, more-specific patterns win
# a character position over later broad ones (spans are resolved non-overlapping,
# first match keeps the region). Append-only — the trained head is indexed by
# TYPES position; keep it stable.
TYPES = (
    "EMAIL", "KEY", "JWT", "PRIVATE_KEY", "CARD", "IPV4", "PHONE",
    "HEX", "B64", "HOME",
)

_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("KEY", re.compile(r"\b(?:KGAT_|sk-|AKIA|ghp_|gho_|github_pat_|xox[baprs]-|AIza)[A-Za-z0-9_\-]{8,}")),
    ("KEY", re.compile(r"(?i)(?<![A-Za-z])(?:bearer|api[_-]?key|access[_-]?token|secret|password|passwd|pwd)\s*[=:]\s*[^\s\"']{4,}")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("IPV4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:1[3-9]\d{9}|\d{3}[ -]\d{3,4}[ -]\d{4})(?!\d)")),
    ("HEX", re.compile(r"\b[A-Fa-f0-9]{32,}\b")),
    ("B64", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")),
    ("HOME", re.compile(re.escape(str(Path.home())))),
)

SAMPLES_FILENAME = "redact_samples.jsonl"
_SCHEMA_VERSION = 1

# Per-process de-dup so the re-sent conversation thread isn't re-logged every
# loop. Bounded to avoid unbounded growth in very long sessions.
_seen: set[str] = set()
_SEEN_CAP = 20000


# ── Detection ──────────────────────────────────────────────────────────────
def scan_text(text: str) -> list[dict]:
    """Return non-overlapping spans ``[{start,end,type,length}]`` in ``text``.
    First (most specific) pattern wins a character region."""
    if not text:
        return []
    taken = [False] * len(text)
    spans: list[dict] = []
    for typ, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            s, e = m.start(), m.end()
            if s >= e:
                continue
            if any(taken[s:e]):
                continue  # overlaps an earlier, more-specific match
            for i in range(s, e):
                taken[i] = True
            spans.append({"start": s, "end": e, "type": typ, "length": e - s})
    spans.sort(key=lambda x: x["start"])
    return spans


def apply_spans(text: str, spans: list[dict]) -> str:
    """Replace spans with typed placeholders, right-to-left to keep offsets."""
    out = text
    for sp in sorted(spans, key=lambda x: x["start"], reverse=True):
        out = out[: sp["start"]] + f"<{sp['type']}>" + out[sp["end"]:]
    return out


def _reindex_spans(redacted: str, spans: list[dict]) -> list[dict]:
    """Given the ORIGINAL spans (left-to-right) and the redacted text, compute
    the placeholder spans in *redacted* coordinates so the stored corpus never
    references original offsets/lengths that could leak structure. Preserves the
    original length as ``orig_len`` for synthetic-secret substitution at train."""
    out: list[dict] = []
    shift = 0
    for sp in spans:
        placeholder = f"<{sp['type']}>"
        new_start = sp["start"] + shift
        new_end = new_start + len(placeholder)
        out.append({
            "start": new_start, "end": new_end,
            "type": sp["type"], "orig_len": sp["length"],
        })
        shift += len(placeholder) - (sp["end"] - sp["start"])
    return out


# ── Capture (privacy-safe: stores redacted text + typed spans only) ────────
def _samples_path() -> Path:
    return paths.project_dir() / SAMPLES_FILENAME


def _record(redacted: str, redacted_spans: list[dict], *, source: str) -> None:
    try:
        row = {
            "v": _SCHEMA_VERSION,
            "ts": round(time.time(), 3),
            "source": source,          # "message" | "tool_args" | "user_input"
            "text": redacted,          # placeholders only — NO raw secret
            "spans": redacted_spans,   # positions of placeholders + orig_len + type
            "n_spans": len(redacted_spans),
        }
        path = _samples_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Public scrub API ───────────────────────────────────────────────────────
def scrub_text(text: str, *, enforce: bool = False, capture: bool = True,
               source: str = "message") -> tuple[str, list[dict]]:
    """Detect (and optionally redact) secrets in one text block.

    Returns ``(text_out, spans)``. ``text_out`` is redacted only when
    ``enforce`` is True; otherwise the original text passes through untouched
    (capture still logs a weak-labeled row). Safe on any input."""
    try:
        if not isinstance(text, str) or not text:
            return text, []
        # Skip fully when there's nothing to gain: not enforcing and already seen.
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        already = digest in _seen
        if not enforce and already:
            return text, []

        spans = scan_text(text)
        if spans and not already:
            if len(_seen) < _SEEN_CAP:
                _seen.add(digest)
            if capture:
                redacted = apply_spans(text, spans)
                _record(redacted, _reindex_spans(redacted, spans), source=source)

        if enforce and spans:
            return apply_spans(text, spans), spans
        return text, spans
    except Exception:
        return text, []


_TEXT_KEYS = ("content", "text", "output", "command", "input", "value")


def _scrub_content(value, *, enforce, capture, source):
    """Recursively scrub strings inside a message's content (str, or list of
    OpenAI content parts, or nested dicts). Returns (new_value, span_count)."""
    if isinstance(value, str):
        out, spans = scrub_text(value, enforce=enforce, capture=capture, source=source)
        return out, len(spans)
    if isinstance(value, list):
        total = 0
        new_list = []
        for item in value:
            nv, n = _scrub_content(item, enforce=enforce, capture=capture, source=source)
            new_list.append(nv)
            total += n
        return new_list, total
    if isinstance(value, dict):
        total = 0
        new_d = dict(value)
        for k, v in list(new_d.items()):
            sub = "tool_args" if k == "arguments" else source
            if isinstance(v, (dict, list)):
                # Always descend into nested structures so wrappers like
                # tool_calls → function → arguments are reached.
                nv, n = _scrub_content(v, enforce=enforce, capture=capture, source=sub)
            elif isinstance(v, str) and (k in _TEXT_KEYS or k == "arguments"):
                # `arguments` may be a serialized JSON string (OpenAI format);
                # scrubbing it as text still catches embedded secrets.
                nv, n = _scrub_content(v, enforce=enforce, capture=capture, source=sub)
            else:
                nv, n = v, 0
            new_d[k] = nv
            total += n
        return new_d, total
    return value, 0


def scrub_messages(messages, *, enforce: bool = False, capture: bool = True):
    """Scrub an outbound message list. Returns ``(messages_out, n_spans)``.
    When ``enforce`` is False the list is returned structurally unchanged (the
    pass is capture-only). Never raises."""
    try:
        if not isinstance(messages, list):
            return messages, 0
        total = 0
        out = []
        for msg in messages:
            if not isinstance(msg, dict):
                out.append(msg)
                continue
            src = "tool_args" if msg.get("role") == "tool" else "message"
            new_msg = dict(msg)
            for key in ("content", "tool_calls"):
                if key in new_msg:
                    nv, n = _scrub_content(new_msg[key], enforce=enforce,
                                           capture=capture, source=src)
                    new_msg[key] = nv
                    total += n
            out.append(new_msg)
        return (out if enforce else messages), total
    except Exception:
        return messages, 0


# ── Offline exporter: weak-labeled NER dataset for Kaggle ──────────────────
def _iter_sample_files(roots) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        if root.is_file() and root.name == SAMPLES_FILENAME:
            found.append(root)
        else:
            found.extend(root.rglob(SAMPLES_FILENAME))
    seen, uniq = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def build_dataset(roots, out_dir: Path, val_frac: float = 0.1) -> dict:
    """Aggregate captured NER rows → ``redact_train.jsonl`` / ``redact_val.jsonl``."""
    import random

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    seen_keys: set[str] = set()
    n_read = n_dup = n_bad = 0
    type_counts = {t: 0 for t in TYPES}

    for path in _iter_sample_files(roots):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    n_read += 1
                    try:
                        row = json.loads(line)
                    except Exception:
                        n_bad += 1
                        continue
                    text = row.get("text")
                    spans = row.get("spans")
                    if not text or not isinstance(spans, list):
                        n_bad += 1
                        continue
                    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
                    if key in seen_keys:
                        n_dup += 1
                        continue
                    seen_keys.add(key)
                    for sp in spans:
                        if sp.get("type") in type_counts:
                            type_counts[sp["type"]] += 1
                    rows.append({"text": text, "spans": spans})
        except Exception:
            continue

    rng = random.Random(1337)
    rng.shuffle(rows)
    cut = int(len(rows) * val_frac)
    val, train = rows[:cut], rows[cut:]

    def _dump(data, name):
        p = out_dir / name
        with open(p, "w", encoding="utf-8") as fh:
            for r in data:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    train_path = _dump(train, "redact_train.jsonl")
    val_path = _dump(val, "redact_val.jsonl")

    return {
        "files_scanned": len(_iter_sample_files(roots)),
        "rows_read": n_read, "duplicates": n_dup, "malformed": n_bad,
        "unique": len(seen_keys), "train": len(train), "val": len(val),
        "type_counts": type_counts,
        "train_path": str(train_path), "val_path": str(val_path),
    }


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the secret/PII redaction (NER) training set from "
                    "captured .laintas/redact_samples.jsonl files.")
    parser.add_argument("roots", nargs="*",
                        help="Dirs/files to scan. Default: $HOME and cwd.")
    parser.add_argument("-o", "--out", default="./redact_dataset")
    parser.add_argument("--val-frac", type=float, default=0.1)
    args = parser.parse_args(argv)

    roots = args.roots or [str(Path.home()), "."]
    rep = build_dataset(roots, Path(args.out), val_frac=args.val_frac)

    print("── secret/PII redaction dataset ──")
    print(f"scanned files : {rep['files_scanned']}")
    print(f"rows read     : {rep['rows_read']}  (dup {rep['duplicates']}, bad {rep['malformed']})")
    print(f"unique        : {rep['unique']}")
    print(f"train / val   : {rep['train']} / {rep['val']}")
    print("span type counts:")
    total = max(1, sum(rep["type_counts"].values()))
    for t in TYPES:
        n = rep["type_counts"].get(t, 0)
        bar = "█" * int(40 * n / total)
        print(f"  {t:<12} {n:>6}  {bar}")
    print(f"→ {rep['train_path']}")
    print(f"→ {rep['val_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
