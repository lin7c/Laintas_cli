"""RAG / retrieval-rerank signal capture — capability #9 of the model fleet.

A cross-encoder reranker makes local code retrieval precise: given a query, rank
the candidate files/chunks the agent should actually look at. Training it needs
relevance judgments — which we can *observe* from the agent's own behavior:

  * a **search** tool (``fs.grep`` / ``fs.glob`` / ``fs.ls``) yields a candidate
    set of paths for a query;
  * a subsequent **selection** (``fs.read`` / ``fs.edit`` / ``fs.multi_edit``) on
    one of those paths is a weak relevance-positive — the agent judged it worth
    opening; the other candidates from that search are weak negatives.

Same discipline as the other heads: capture weak-labeled signal from real usage
now (``.laintas/rag_signals.jsonl``), assemble query→(positive, negatives) triples
offline, train later. ``on_tool`` is called once per tool result at the dispatch
site and never raises. Paths are structural, not secret, but the query string is
redacted via ``redactor`` for hygiene.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import paths

try:
    import redactor
except Exception:
    redactor = None


SIGNALS_FILENAME = "rag_signals.jsonl"
_SCHEMA_VERSION = 1

SEARCH_TOOLS = frozenset({"fs.grep", "fs.glob", "fs.ls"})
SELECT_TOOLS = frozenset({"fs.read", "fs.edit", "fs.multi_edit"})

_MAX_CANDIDATES = 60
_MAX_QUERY = 300

# A path-ish token: optional ./, at least one segment, common source shapes.
_PATH_RE = re.compile(r"(?:\.?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]{1,8}")


def _redact(text: str) -> str:
    if not text:
        return ""
    if redactor is not None:
        try:
            out, _ = redactor.scrub_text(str(text), enforce=True, capture=False)
            return out[:_MAX_QUERY]
        except Exception:
            pass
    return str(text)[:_MAX_QUERY]


def _query_of(name: str, arguments: dict) -> str:
    if not isinstance(arguments, dict):
        return ""
    if name == "fs.grep":
        return str(arguments.get("pattern") or arguments.get("query") or "")
    if name == "fs.glob":
        return str(arguments.get("pattern") or "")
    if name == "fs.ls":
        return str(arguments.get("path") or "")
    return ""


def _parse_candidates(output: str) -> list[str]:
    """Best-effort extraction of candidate paths from a search tool's output.
    grep lines are ``path:line:...``; glob/ls emit bare paths. We take the first
    path-like token per line, de-duplicated, capped."""
    if not output:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in str(output).splitlines():
        line = line.strip()
        if not line:
            continue
        head = line.split(":", 1)[0].strip()
        m = _PATH_RE.match(head) or _PATH_RE.search(line)
        if not m:
            continue
        p = m.group(0)
        if p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _norm_path(p: str) -> str:
    return (p or "").lstrip("./").strip()


def _signals_path() -> Path:
    return paths.project_dir() / SIGNALS_FILENAME


def _record(**fields) -> None:
    try:
        row = {"v": _SCHEMA_VERSION, "ts": round(time.time(), 3)}
        row.update(fields)
        path = _signals_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def on_tool(name: str, arguments: dict, result: dict, output: str = "",
            *, session_id: str = "", run_id: str = "", loop: int = 0) -> None:
    """Capture a search or selection signal from one finished tool call."""
    try:
        name = str(name or "")
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        if not ok:
            return
        args = arguments if isinstance(arguments, dict) else {}
        if name in SEARCH_TOOLS:
            candidates = [_norm_path(c) for c in _parse_candidates(output)]
            candidates = [c for c in candidates if c]
            if not candidates:
                return
            _record(kind="search", tool=name, run_id=str(run_id or ""),
                    session_id=str(session_id or ""), loop=int(loop or 0),
                    query=_redact(_query_of(name, args)),
                    candidates=candidates)
        elif name in SELECT_TOOLS:
            path = _norm_path(str(args.get("path") or ""))
            if not path:
                return
            _record(kind="select", tool=name, run_id=str(run_id or ""),
                    session_id=str(session_id or ""), loop=int(loop or 0),
                    path=path)
    except Exception:
        pass


# ── Offline exporter: query → (positive, negatives) rerank triples ─────────
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


def build_dataset(roots, out_dir: Path, val_frac: float = 0.1,
                  max_gap_loops: int = 6) -> dict:
    """Join each selection to the most recent prior search in the same run and
    emit a rerank example: the opened path is the positive, the other candidates
    from that search are negatives. Writes ``rerank_train.jsonl`` / ``_val``."""
    import random

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events: list[dict] = []
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
                        events.append(json.loads(line))
                    except Exception:
                        n_bad += 1
        except Exception:
            continue

    events.sort(key=lambda e: e.get("ts", 0))
    # Most recent search per run_id as we sweep forward in time.
    last_search: dict[str, dict] = {}
    examples: list[dict] = []
    n_search = n_select = n_matched = 0

    for ev in events:
        kind = ev.get("kind")
        rid = ev.get("run_id") or ""
        if kind == "search":
            n_search += 1
            last_search[rid] = ev
        elif kind == "select":
            n_select += 1
            src = last_search.get(rid)
            if not src:
                continue
            if ev.get("loop", 0) - src.get("loop", 0) > max_gap_loops:
                continue
            chosen = ev.get("path", "")
            cands = src.get("candidates", []) or []
            if chosen not in cands:
                continue  # selection wasn't from this candidate set
            negatives = [c for c in cands if c != chosen]
            if not negatives:
                continue  # no contrast to learn from
            n_matched += 1
            examples.append({
                "query": src.get("query", ""),
                "positive": chosen,
                "negatives": negatives[:_MAX_CANDIDATES],
                "tool": src.get("tool", ""),
            })

    rng = random.Random(1337)
    rng.shuffle(examples)
    cut = int(len(examples) * val_frac)
    val, train = examples[:cut], examples[cut:]

    def _dump(data, name):
        p = out_dir / name
        with open(p, "w", encoding="utf-8") as fh:
            for r in data:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    train_path = _dump(train, "rerank_train.jsonl")
    val_path = _dump(val, "rerank_val.jsonl")

    return {
        "files_scanned": len(_iter_signal_files(roots)),
        "rows_read": n_read, "malformed": n_bad,
        "searches": n_search, "selects": n_select, "matched_pairs": n_matched,
        "train": len(train), "val": len(val),
        "train_path": str(train_path), "val_path": str(val_path),
    }


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the retrieval-rerank training set (query → positive + "
                    "negatives) from captured .laintas/rag_signals.jsonl files.")
    parser.add_argument("roots", nargs="*",
                        help="Dirs/files to scan. Default: $HOME and cwd.")
    parser.add_argument("-o", "--out", default="./rerank_dataset")
    parser.add_argument("--val-frac", type=float, default=0.1)
    args = parser.parse_args(argv)

    roots = args.roots or [str(Path.home()), "."]
    rep = build_dataset(roots, Path(args.out), val_frac=args.val_frac)

    print("── retrieval-rerank dataset ──")
    print(f"scanned files : {rep['files_scanned']}")
    print(f"rows read     : {rep['rows_read']}  (bad {rep['malformed']})")
    print(f"events        : search {rep['searches']} · select {rep['selects']} · matched {rep['matched_pairs']}")
    print(f"train / val   : {rep['train']} / {rep['val']}")
    print(f"→ {rep['train_path']}")
    print(f"→ {rep['val_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
