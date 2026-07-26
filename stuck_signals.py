"""Stuck / spinning detection — capability #5 of the model fleet.

The worst long-task experience is an agent that silently spins: repeating calls,
erroring in a loop, editing nothing, until it hits the step cap. A small sequence
classifier over the last N steps ("normal / spinning / stuck") lets the loop
intervene early — switch strategy, ask the user, or escalate.

Unlike the other heads, this one needs **no new capture hook**: the durable event
log (``.laintas/events.jsonl``) already records every ``tool_result``
(``name``/``ok``/``run_id``/``loop``) and every ``turn_ended`` (``reason``). This
module is therefore an *offline exporter only*: it groups a run's tool steps into
a sequence and labels the whole run from how the turn ended.

Label source (turn_ended ``reason`` → class):
  * ``stuck``  ← staleness / max_loops / repetition / warning_force_exit /
                 repair_gave_up / parse_gave_up  (the loop gave up unproductively)
  * ``ok``     ← completed / end_turn            (the turn produced a result)
  * skipped    ← aborted / interrupted / user_denied / backend_error /
                 provider_error / silent_failure (external, not the agent's doing)

Best-effort throughout; a malformed log line is skipped, never fatal.
"""

from __future__ import annotations

import json
from pathlib import Path

import paths


EVENTS_FILENAME = "events.jsonl"

_STUCK_REASONS = frozenset({
    "staleness", "max_loops", "repetition", "warning_force_exit",
    "repair_gave_up", "parse_gave_up",
})
_OK_REASONS = frozenset({"completed", "end_turn"})
# Everything else (aborted/interrupted/user_denied/backend_error/…) is external
# and excluded from the training set.

LABELS = ("ok", "stuck")
_LABEL_ID = {"ok": 0, "stuck": 1}


def _iter_event_files(roots) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        if root.is_file() and root.name == EVENTS_FILENAME:
            found.append(root)
        else:
            found.extend(root.rglob(EVENTS_FILENAME))
    seen, uniq = set(), []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def _max_repeat_run(names: list[str]) -> int:
    """Longest run of the identical consecutive tool name (a spin signature)."""
    best = cur = 0
    prev = None
    for n in names:
        cur = cur + 1 if n == prev else 1
        prev = n
        best = max(best, cur)
    return best


def build_dataset(roots, out_dir: Path, val_frac: float = 0.1) -> dict:
    """Reconstruct per-run step sequences from events.jsonl and label each run
    by its terminal reason. Writes ``stuck_train.jsonl`` / ``stuck_val.jsonl``."""
    import random

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # run_id → ordered list of (loop, name, ok); run_id → terminal reason
    steps: dict[str, list] = {}
    reasons: dict[str, str] = {}
    n_read = n_bad = 0

    for path in _iter_event_files(roots):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    n_read += 1
                    try:
                        ev = json.loads(line)
                    except Exception:
                        n_bad += 1
                        continue
                    etype = ev.get("type")
                    rid = ev.get("run_id") or ""
                    if not rid:
                        continue
                    if etype == "tool_result":
                        steps.setdefault(rid, []).append((
                            ev.get("loop", 0),
                            str(ev.get("name", "")),
                            bool(ev.get("ok", False)),
                        ))
                    elif etype == "turn_ended":
                        reasons[rid] = str(ev.get("reason", ""))
        except Exception:
            continue

    rows: list[dict] = []
    n_ok = n_stuck = n_skip = 0
    for rid, reason in reasons.items():
        if reason in _STUCK_REASONS:
            label = "stuck"
        elif reason in _OK_REASONS:
            label = "ok"
        else:
            n_skip += 1
            continue
        seq = sorted(steps.get(rid, []), key=lambda x: x[0])
        names = [n for _, n, _ in seq]
        oks = [o for _, _, o in seq]
        n_steps = len(seq)
        if n_steps == 0:
            # An ok turn with no tool calls is a valid "not stuck" example;
            # a stuck turn with no steps carries no sequence signal — skip it.
            if label == "stuck":
                n_skip += 1
                continue
        fails = sum(1 for o in oks if not o)
        rows.append({
            "seq": [{"tool": n, "ok": o} for n, o in zip(names, oks)],
            "label": label,
            "label_id": _LABEL_ID[label],
            "feats": {
                "n_steps": n_steps,
                "n_unique_tools": len(set(names)),
                "fail_rate": round(fails / n_steps, 3) if n_steps else 0.0,
                "max_repeat_run": _max_repeat_run(names),
                "loops": max((lp for lp, _, _ in seq), default=0),
            },
            "reason": reason,
        })
        if label == "stuck":
            n_stuck += 1
        else:
            n_ok += 1

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

    train_path = _dump(train, "stuck_train.jsonl")
    val_path = _dump(val, "stuck_val.jsonl")

    return {
        "files_scanned": len(_iter_event_files(roots)),
        "rows_read": n_read, "malformed": n_bad,
        "runs_with_outcome": len(reasons),
        "ok": n_ok, "stuck": n_stuck, "skipped": n_skip,
        "train": len(train), "val": len(val),
        "train_path": str(train_path), "val_path": str(val_path),
    }


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the stuck-detection training set from the durable "
                    "event log (.laintas/events.jsonl). No capture hook needed.")
    parser.add_argument("roots", nargs="*",
                        help="Dirs/files to scan. Default: $HOME and cwd.")
    parser.add_argument("-o", "--out", default="./stuck_dataset")
    parser.add_argument("--val-frac", type=float, default=0.1)
    args = parser.parse_args(argv)

    roots = args.roots or [str(Path.home()), "."]
    rep = build_dataset(roots, Path(args.out), val_frac=args.val_frac)

    print("── stuck-detection dataset ──")
    print(f"scanned files    : {rep['files_scanned']}")
    print(f"events read      : {rep['rows_read']}  (bad {rep['malformed']})")
    print(f"runs w/ outcome  : {rep['runs_with_outcome']}")
    print(f"labeled          : ok {rep['ok']} · stuck {rep['stuck']} · skipped {rep['skipped']}")
    print(f"train / val      : {rep['train']} / {rep['val']}")
    print(f"→ {rep['train_path']}")
    print(f"→ {rep['val_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
