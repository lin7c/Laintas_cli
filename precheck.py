"""Tool-call pre-check: labeled-data capture, featurization, and inference stub.

This is capability #1 of the self-trained "experience" model fleet — a narrow
classifier that predicts, *before* a tool runs, whether the call is likely to
fail (and how). It is the ML generalization of the deterministic repeat-failure
ledger already in ``agent_loop`` (``_fail_ledger``): the ledger blocks a call
that has failed identically N times; this model learns to flag a *new* call that
looks doomed.

Three responsibilities, all in one flat module (mirrors ``event_log.py``):

  1. **Capture** — ``record_sample`` turns every real tool invocation into a
     labeled training row (input features → outcome class) and appends it to a
     per-cwd ``.laintas/precheck_samples.jsonl``. Zero manual labeling: the
     outcome *is* the label. This runs today so the dataset accumulates from
     real usage before any model exists.

  2. **Featurize** — ``featurize`` produces the exact same representation at
     capture time and at inference time (train/serve parity). The text field is
     what a shared MiniLM-class encoder consumes; ``feats`` are cheap numeric
     side-signals.

  3. **Predict** — ``predict`` loads an ONNX model from ``~/.laintas/models/``
     if present, else returns ``None`` (a true no-op until we ship a model).

Nothing here may ever raise into the agent loop: capture and predict are both
wrapped so a bug degrades to "no sample recorded" / "no prediction", never a
broken tool call. Redaction is conservative on purpose — this log must never
become a secret store (it also front-runs capability #2, the PII redactor).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import paths


# ── Outcome label taxonomy ────────────────────────────────────────────────
# Keep this list stable and append-only: the trained model's output head is
# indexed by position. "ok" MUST stay index 0.
LABELS = (
    "ok",             # result.ok is True
    "not_found",      # tool/file/path/command not found
    "bad_args",       # schema validation, missing/typed param, malformed input
    "permission",     # denied/blocked by policy or user, sandbox-disabled
    "conflict",       # edit conflict: old_string absent/ambiguous, already exists
    "timeout",        # timed out / did not finish
    "runtime_error",  # non-zero exit / raised exception at execution time
    "other",          # failed, but none of the above matched
)
_LABEL_INDEX = {name: i for i, name in enumerate(LABELS)}

SAMPLES_FILENAME = "precheck_samples.jsonl"
_SCHEMA_VERSION = 1

# Cap how much of the salient arg text we serialize (features, not payloads).
_MAX_TEXT = 400


# ── Redaction (conservative; never let a secret land in the sample log) ───
_REDACTORS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    # Known key prefixes (laintas KGAT_, OpenAI sk-, Bearer tokens, AWS AKIA…).
    (re.compile(r"\b(?:KGAT_|sk-|AKIA|ghp_|xox[baprs]-)[A-Za-z0-9_\-]{8,}"), "<KEY>"),
    (re.compile(r"(?i)\b(?:bearer|token|apikey|api_key|password|secret)\s*[=:]\s*\S+"), "<KEY>"),
    # Long opaque hex / base64-ish blobs (≥24 chars) — likely a token/hash.
    (re.compile(r"\b[A-Fa-f0-9]{24,}\b"), "<HEX>"),
    (re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"), "<B64>"),
    # Home directory → structure-preserving placeholder.
    (re.compile(re.escape(str(Path.home()))), "~"),
)


def redact(text: str) -> str:
    """Best-effort scrub of secrets/PII from a short arg string."""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in _REDACTORS:
        out = pattern.sub(repl, out)
    return out


# ── Featurization (train/serve parity — used by both capture and predict) ──
def _salient_of(name: str, arguments: dict) -> str:
    """Compact, human-meaningful arg summary. Kept local so inference never
    needs to import the agent loop. Mirrors the spirit of ``_salient_arg``."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "cmd", "input", "pattern", "path", "query", "url", "name"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        return json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:
        return str(arguments)


def featurize(name: str, arguments: dict) -> dict:
    """Return ``{"text": str, "feats": {...}}`` — the model's input.

    ``text`` is the redacted, canonical string a text encoder sees; ``feats``
    are cheap structural signals that a linear head can use directly and that
    also survive as columns for classical baselines on Kaggle.
    """
    name = str(name or "")
    args = arguments if isinstance(arguments, dict) else {}
    salient = redact(_salient_of(name, args))[:_MAX_TEXT]
    text = f"{name} | {salient}"

    path = ""
    for key in ("path", "file", "filename", "target"):
        v = args.get(key)
        if isinstance(v, str) and v:
            path = v
            break

    feats = {
        "n_args": len(args),
        "salient_len": len(salient),
        "has_path": bool(path),
        "path_depth": path.count("/") if path else 0,
        "is_abs_path": path.startswith("/") or path.startswith("~"),
        "has_glob": any(c in salient for c in "*?[") if salient else False,
        "has_pipe": "|" in salient,
        "has_redirect": (">" in salient) or (">>" in salient),
        "has_sudo": bool(re.search(r"\bsudo\b", salient)),
        "has_rm": bool(re.search(r"\brm\b", salient)),
        # old_string/new_string presence is a strong signal for edit-conflict risk
        "has_old_string": "old_string" in args,
        "has_new_string": "new_string" in args,
    }
    return {"text": text, "feats": feats}


# ── Outcome classification (derives the training label from the result) ────
def classify_outcome(name: str, result: dict, returncode) -> str:
    """Map a finished tool result to one of ``LABELS``. Pure heuristic — this
    is the auto-labeler, so it stays conservative and readable."""
    if not isinstance(result, dict):
        return "ok" if returncode in (0, None) else "runtime_error"
    if result.get("ok"):
        return "ok"

    err = str(result.get("error") or result.get("output") or "").lower()

    if result.get("_user_denied") or "denied" in err or "blocked" in err \
            or "permission denied" in err or "not permitted" in err \
            or "disabled in" in err or "sandbox" in err:
        return "permission"
    if "not found" in err or "no such file" in err or "does not exist" in err \
            or "unknown tool" in err or "cannot find" in err:
        return "not_found"
    if "old_string" in err or "not unique" in err or "already exists" in err \
            or "no changes" in err or "conflict" in err or "ambiguous" in err:
        return "conflict"
    if "expected" in err and "param" in err or "missing required" in err \
            or "invalid" in err and ("arg" in err or "param" in err) \
            or "schema" in err or "unexpected property" in err:
        return "bad_args"
    if "timed out" in err or "timeout" in err:
        return "timeout"
    if returncode not in (0, None) or "traceback" in err or "exception" in err:
        return "runtime_error"
    return "other"


# ── Capture ────────────────────────────────────────────────────────────────
def _samples_path() -> Path:
    return paths.project_dir() / SAMPLES_FILENAME


def record_sample(name: str, arguments: dict, result: dict, returncode,
                  *, elapsed: float = 0.0, session_id: str = "",
                  run_id: str = "", loop: int = 0) -> None:
    """Append one labeled training row. Best-effort — swallows every error so it
    can never disturb the agent loop. Skips repeat-ledger-blocked pseudo-results
    (they are the ledger's own synthetic failures, not real tool outcomes)."""
    try:
        if isinstance(result, dict) and result.get("_repeat_blocked"):
            return
        f = featurize(name, arguments)
        row = {
            "v": _SCHEMA_VERSION,
            "ts": round(time.time(), 3),
            "tool": str(name or ""),
            "text": f["text"],
            "feats": f["feats"],
            "label": classify_outcome(name, result, returncode),
            "rc": returncode if isinstance(returncode, int) else None,
            "elapsed": round(float(elapsed or 0.0), 3),
            "session_id": str(session_id or ""),
            "run_id": str(run_id or ""),
            "loop": int(loop or 0),
        }
        path = _samples_path()
        paths.ensure_project_dir()
        if not paths.ensure_private_file(path):
            return
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        paths.ensure_private_file(path)
    except Exception:
        # Capture is strictly advisory; never propagate.
        pass


# ── Inference (no-op until a model is shipped) ─────────────────────────────
_model_state: dict = {"tried": False, "session": None, "tokenizer": None}
_MODEL_PATH = paths.LAINTAS_HOME / "models" / "precheck.onnx"


def _ensure_model():
    """Lazy-load the ONNX session once. Returns None if unavailable."""
    if _model_state["tried"]:
        return _model_state["session"]
    _model_state["tried"] = True
    try:
        if not _MODEL_PATH.exists():
            return None
        import onnxruntime  # type: ignore
        _model_state["session"] = onnxruntime.InferenceSession(
            str(_MODEL_PATH), providers=["CPUExecutionProvider"])
    except Exception:
        _model_state["session"] = None
    return _model_state["session"]


def predict(name: str, arguments: dict) -> Optional[dict]:
    """Predict the outcome class for a *pending* call.

    Returns ``None`` when no model is installed (the current state) so callers
    treat pre-check as advisory and absent by default. When a model exists,
    returns ``{"label": str, "fail_prob": float, "probs": {label: p}}``.
    """
    try:
        session = _ensure_model()
        if session is None:
            return None
        # Wiring point for the shipped model. Featurization is already parity-safe:
        #   feat = featurize(name, arguments)
        #   probs = session.run(...)  # encode feat["text"] + feat["feats"]
        # Left unimplemented until the ONNX export lands; capture is what matters now.
        return None
    except Exception:
        return None


# ── Offline exporter: build the Kaggle training set from captured samples ──
def _iter_sample_files(roots) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        if root.is_file() and root.name == SAMPLES_FILENAME:
            found.append(root)
            continue
        found.extend(root.rglob(SAMPLES_FILENAME))
    # de-dup file paths while preserving order
    seen, uniq = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def build_dataset(roots, out_dir: Path, val_frac: float = 0.1) -> dict:
    """Read all captured samples under ``roots``, dedup, stratify-split, and
    write ``precheck_train.jsonl`` + ``precheck_val.jsonl``. Returns a report."""
    import hashlib
    import random

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_label: dict[str, list[dict]] = {name: [] for name in LABELS}
    seen_keys: set[str] = set()
    n_read = n_dup = n_bad = 0

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
                    label = row.get("label")
                    text = row.get("text")
                    if label not in _LABEL_INDEX or not text:
                        n_bad += 1
                        continue
                    key = hashlib.sha1(
                        f"{text}\x00{label}".encode("utf-8")).hexdigest()
                    if key in seen_keys:
                        n_dup += 1
                        continue
                    seen_keys.add(key)
                    by_label[label].append({
                        "text": text,
                        "feats": row.get("feats", {}),
                        "label": label,
                        "label_id": _LABEL_INDEX[label],
                        "tool": row.get("tool", ""),
                    })
        except Exception:
            continue

    rng = random.Random(1337)
    train, val = [], []
    for label, rows in by_label.items():
        rng.shuffle(rows)
        cut = int(len(rows) * val_frac)
        val.extend(rows[:cut])
        train.extend(rows[cut:])
    rng.shuffle(train)
    rng.shuffle(val)

    def _dump(rows, name):
        p = out_dir / name
        with open(p, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    train_path = _dump(train, "precheck_train.jsonl")
    val_path = _dump(val, "precheck_val.jsonl")

    return {
        "files_scanned": len(_iter_sample_files(roots)),
        "rows_read": n_read,
        "duplicates": n_dup,
        "malformed": n_bad,
        "unique": len(seen_keys),
        "train": len(train),
        "val": len(val),
        "class_distribution": {k: len(v) for k, v in by_label.items()},
        "train_path": str(train_path),
        "val_path": str(val_path),
    }


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the tool-precheck training set from captured "
                    ".laintas/precheck_samples.jsonl files.")
    parser.add_argument(
        "roots", nargs="*",
        help="Dirs (or sample files) to scan recursively. "
             "Default: $HOME and the current directory.")
    parser.add_argument("-o", "--out", default="./precheck_dataset",
                        help="Output directory (default: ./precheck_dataset)")
    parser.add_argument("--val-frac", type=float, default=0.1,
                        help="Validation split fraction (default: 0.1)")
    args = parser.parse_args(argv)

    roots = args.roots or [str(Path.home()), "."]
    report = build_dataset(roots, Path(args.out), val_frac=args.val_frac)

    print("── tool-precheck dataset ──")
    print(f"scanned files : {report['files_scanned']}")
    print(f"rows read     : {report['rows_read']}  "
          f"(dup {report['duplicates']}, bad {report['malformed']})")
    print(f"unique        : {report['unique']}")
    print(f"train / val   : {report['train']} / {report['val']}")
    print("class distribution:")
    total = max(1, report["unique"])
    for label in LABELS:
        n = report["class_distribution"].get(label, 0)
        bar = "█" * int(40 * n / total)
        print(f"  {label:<14} {n:>6}  {bar}")
    print(f"→ {report['train_path']}")
    print(f"→ {report['val_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
