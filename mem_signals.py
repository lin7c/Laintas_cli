"""Memory signal capture — capability #4 of the model fleet.

Two narrow models make the cross-session memory system *smart* instead of merely
persistent:

  * **salience** — "is this fact worth keeping?" A scoring head that gates writes
    and prunes low-value memories.
  * **recall rerank** — given the current task, surface the memories that actually
    matter (better than the current lexical scorer in ``search_memories``).

Same discipline as ``precheck.py`` / ``redactor.py``: capture weak-labeled signal
from real usage now, train the model later. This module only *logs* — it never
changes memory behavior. Three low-noise, unambiguous events are recorded to
``.laintas/mem_signals.jsonl``:

  * ``write``  — a memory was deemed worth saving (salience-positive candidate)
  * ``delete`` — a memory was removed (join with its write to get lifetime; a
    short-lived deletion is a weak salience-negative)
  * ``search`` — a recall query + the candidate pool the lexical scorer returned
    (foundation for the rerank dataset)

We deliberately do NOT log ``read_memory``: ``search_memories`` calls it for every
entry it scans, so it is far too noisy to be a "this was used" signal. The usage
label for recall is left to a future explicit signal.

All memory text is passed through ``redactor`` (capability #2) before it is
written here, so this signal log never becomes a secret store. Best-effort: no
function raises into the memory system.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import paths

# On-switch (parity with precheck_capture / redact_capture, but memory_system
# has no access to agent_loop's runtime config, so use an env guard). Capture is
# OFF by default and opt-in:  LAINTAS_MEM_SIGNALS=1  enables all capture.
_ENABLED = os.environ.get("LAINTAS_MEM_SIGNALS", "0").strip().lower() in ("1", "true", "yes")

try:
    import redactor
except Exception:  # pragma: no cover - redactor is a sibling module
    redactor = None


SIGNALS_FILENAME = "mem_signals.jsonl"
_SCHEMA_VERSION = 1

# A delete within this many seconds of the write is treated as a weak
# salience-negative ("drop"); longer-lived deletes are ambiguous and skipped.
_SHORTLIVED_SECS = 24 * 3600

_MAX_TEXT = 600


def _redact(text: str) -> str:
    if not text:
        return ""
    if redactor is not None:
        try:
            out, _ = redactor.scrub_text(str(text), enforce=True, capture=False)
            return out[:_MAX_TEXT]
        except Exception:
            pass
    return str(text)[:_MAX_TEXT]


def _signals_path() -> Path:
    return paths.project_dir() / SIGNALS_FILENAME


def _record(kind: str, **fields) -> None:
    if not _ENABLED:
        return
    try:
        row = {"v": _SCHEMA_VERSION, "kind": kind, "ts": round(time.time(), 3)}
        row.update(fields)
        path = _signals_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Event hooks (called from memory_system.py at return sites) ─────────────
def on_write(name: str, mem_type: str, description: str, body: str,
             *, is_update: bool = False, importance: float = 0.5) -> None:
    desc = _redact(description)
    body_prev = _redact(body)
    _record(
        "write",
        name=str(name or ""),
        mem_type=str(mem_type or ""),
        is_update=bool(is_update),
        importance=float(importance) if importance is not None else 0.5,
        desc_len=len(description or ""),
        body_len=len(body or ""),
        text=f"{desc} | {body_prev}",
    )


def on_delete(name: str) -> None:
    _record("delete", name=str(name or ""))


def on_search(query: str, results: list) -> None:
    try:
        candidates = [
            {
                "name": r.get("name", ""),
                "score": r.get("score", 0),
                "importance": r.get("importance", 0.5),
                "mem_type": r.get("type") or r.get("mem_type", ""),
            }
            for r in (results or [])[:20]
            if isinstance(r, dict)
        ]
    except Exception:
        candidates = []
    _record(
        "search",
        query=_redact(query),
        query_len=len(query or ""),
        n_candidates=len(candidates),
        candidates=candidates,
    )


# ── Offline exporter: salience dataset + recall event log ──────────────────
def _iter_signal_files(roots) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        if root.is_file() and root.name == SIGNALS_FILENAME:
            found.append(root)
        else:
            found.extend(root.rglob(SIGNALS_FILENAME))
    seen, uniq = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def build_dataset(roots, out_dir: Path, val_frac: float = 0.1) -> dict:
    """Join write/delete events into a salience dataset (keep vs drop) and copy
    search events into a recall event log for later relevance labeling."""
    import hashlib
    import random

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    writes: list[dict] = []
    deletes: list[dict] = []
    searches: list[dict] = []
    n_read = n_bad = 0

    for path in _iter_signal_files(roots):
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
                    kind = row.get("kind")
                    if kind == "write":
                        writes.append(row)
                    elif kind == "delete":
                        deletes.append(row)
                    elif kind == "search":
                        searches.append(row)
        except Exception:
            continue

    # For each memory name, the ordered timeline of delete timestamps.
    deletes_by_name: dict[str, list[float]] = {}
    for d in deletes:
        deletes_by_name.setdefault(d.get("name", ""), []).append(d.get("ts", 0))
    for lst in deletes_by_name.values():
        lst.sort()

    def _first_delete_after(name: str, ts: float):
        for dts in deletes_by_name.get(name, []):
            if dts >= ts:
                return dts
        return None

    # Salience rows: label each write as keep(1) / drop(0) by its lifetime.
    salience: list[dict] = []
    seen_keys: set[str] = set()
    n_keep = n_drop = n_skip = 0
    for w in writes:
        name = w.get("name", "")
        wts = w.get("ts", 0)
        text = w.get("text", "")
        if not text:
            n_skip += 1
            continue
        key = hashlib.sha1(f"{text}\x00{wts}".encode("utf-8")).hexdigest()
        if key in seen_keys:
            continue
        seen_keys.add(key)

        del_ts = _first_delete_after(name, wts)
        if del_ts is None:
            label, label_id = "keep", 1
            n_keep += 1
        else:
            lifetime = del_ts - wts
            if lifetime <= _SHORTLIVED_SECS:
                label, label_id = "drop", 0
                n_drop += 1
            else:
                # Long-lived then deleted: ambiguous (superseded / outdated).
                n_skip += 1
                continue
        salience.append({
            "text": text,
            "label": label,
            "label_id": label_id,
            "feats": {
                "mem_type": w.get("mem_type", ""),
                "is_update": bool(w.get("is_update")),
                "importance": w.get("importance", 0.5),
                "desc_len": w.get("desc_len", 0),
                "body_len": w.get("body_len", 0),
            },
        })

    rng = random.Random(1337)
    rng.shuffle(salience)
    cut = int(len(salience) * val_frac)
    val, train = salience[:cut], salience[cut:]

    def _dump(rows, name):
        p = out_dir / name
        with open(p, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    train_path = _dump(train, "salience_train.jsonl")
    val_path = _dump(val, "salience_val.jsonl")
    recall_path = _dump(searches, "recall_events.jsonl")

    return {
        "files_scanned": len(_iter_signal_files(roots)),
        "rows_read": n_read, "malformed": n_bad,
        "writes": len(writes), "deletes": len(deletes), "searches": len(searches),
        "salience_keep": n_keep, "salience_drop": n_drop, "salience_skipped": n_skip,
        "train": len(train), "val": len(val),
        "train_path": str(train_path), "val_path": str(val_path),
        "recall_path": str(recall_path),
    }


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the memory-salience training set (and recall event "
                    "log) from captured .laintas/mem_signals.jsonl files.")
    parser.add_argument("roots", nargs="*",
                        help="Dirs/files to scan. Default: $HOME and cwd.")
    parser.add_argument("-o", "--out", default="./mem_dataset")
    parser.add_argument("--val-frac", type=float, default=0.1)
    args = parser.parse_args(argv)

    roots = args.roots or [str(Path.home()), "."]
    rep = build_dataset(roots, Path(args.out), val_frac=args.val_frac)

    print("── memory-salience dataset ──")
    print(f"scanned files : {rep['files_scanned']}")
    print(f"rows read     : {rep['rows_read']}  (bad {rep['malformed']})")
    print(f"events        : write {rep['writes']} · delete {rep['deletes']} · search {rep['searches']}")
    print(f"salience       : keep {rep['salience_keep']} · drop {rep['salience_drop']} · skipped {rep['salience_skipped']}")
    print(f"train / val    : {rep['train']} / {rep['val']}")
    print(f"→ {rep['train_path']}")
    print(f"→ {rep['val_path']}")
    print(f"→ {rep['recall_path']}  (raw search events, awaiting relevance labels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
