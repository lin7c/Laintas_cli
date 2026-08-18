"""Match storage and Bradley-Terry rating for the blindpick extension.

Everything is local and append-only under ``.laintas/blindpick/``:

    matches.jsonl   one row per sampled turn, both candidates, unrevealed
    votes.jsonl     one row per human judgement

Two files rather than one mutable store because a vote must never be able to
rewrite the match it judged — that is the only thing keeping the comparison
honest once ratings start informing decisions.
"""

from __future__ import annotations

import json
import math
import os
import random
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

# Bradley-Terry on a logistic scale, expressed in familiar Elo units so the
# numbers read the way people expect. K is deliberately small: with a few
# hundred matches a large K makes the table swing on single votes and invites
# reading noise as progress.
ELO_BASE = 1000.0
ELO_K = 16.0
ELO_SCALE = 400.0


#: Machine-level override, read from ``~/.laintas/blindpick.json``:
#:
#:     {"data_dir": "/somewhere/else"}
#:
#: Matches how policy.json / hooks.json / mcp.json already work, so an operator
#: can point collection somewhere durable without editing the extension or
#: exporting an environment variable into every shell.
GLOBAL_CONFIG = Path.home() / ".laintas" / "blindpick.json"


def _configured_dir() -> Path | None:
    try:
        value = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8")).get("data_dir")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(str(value)).expanduser() if value else None


def store_dir(project_dir: Path) -> Path:
    # Default is per-project so two checkouts do not pool their judgements into
    # one rating table; the override exists for machines that deliberately want
    # a single collection point.
    path = _configured_dir() or (Path(project_dir) / "blindpick")
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _append(path: Path, row: dict) -> None:
    """Append one JSON line durably.

    Opened per call in append mode: the CLI can be killed at any point (Esc,
    double Ctrl+C, SSH drop) and a held-open handle would lose buffered votes.
    """
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _read(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue  # a torn final line must not sink the whole history


def record_match(project_dir: Path, *, task: str, context_digest: str,
                 left: dict, right: dict) -> str:
    """Store one unjudged pair. `left`/`right` are {model, reasoning, action}."""
    match_id = uuid.uuid4().hex[:16]
    _append(store_dir(project_dir) / "matches.jsonl", {
        "match_id": match_id,
        "created_at": time.time(),
        "task": str(task)[:4000],
        "context_digest": context_digest,
        # Stored under neutral keys. Which one is the incumbent is knowable from
        # the model field, so display code must never show it before the vote.
        "left": left,
        "right": right,
    })
    return match_id


def record_vote(project_dir: Path, *, match_id: str, winner: str,
                shown_first: str, note: str = "") -> None:
    if winner not in ("left", "right", "tie", "both_bad"):
        raise ValueError("winner must be left/right/tie/both_bad")
    _append(store_dir(project_dir) / "votes.jsonl", {
        "match_id": match_id,
        "voted_at": time.time(),
        "winner": winner,
        # Recorded so position bias is measurable after the fact: if the side
        # shown first wins far more than half the time, the numbers are
        # measuring layout rather than quality.
        "shown_first": shown_first,
        "note": str(note)[:500],
    })


def load_matches(project_dir: Path) -> dict[str, dict]:
    return {row["match_id"]: row
            for row in _read(store_dir(project_dir) / "matches.jsonl")
            if row.get("match_id")}


def load_votes(project_dir: Path) -> dict[str, dict]:
    # Last vote wins, so a re-vote corrects an earlier mistake.
    votes: dict[str, dict] = {}
    for row in _read(store_dir(project_dir) / "votes.jsonl"):
        if row.get("match_id"):
            votes[row["match_id"]] = row
    return votes


def pending(project_dir: Path) -> list[dict]:
    votes = load_votes(project_dir)
    return [match for match_id, match in load_matches(project_dir).items()
            if match_id not in votes]


def ratings(project_dir: Path) -> dict[str, dict]:
    """Elo-scaled Bradley-Terry ratings over every judged match."""
    matches = load_matches(project_dir)
    votes = load_votes(project_dir)
    table: dict[str, dict] = {}

    def entry(model: str) -> dict:
        return table.setdefault(model, {
            "model": model, "rating": ELO_BASE,
            "wins": 0, "losses": 0, "ties": 0, "matches": 0})

    # Chronological: Elo is path-dependent, so a stable order keeps the table
    # reproducible from the same files.
    for match_id, vote in sorted(votes.items(), key=lambda kv: kv[1]["voted_at"]):
        match = matches.get(match_id)
        if not match:
            continue
        a = entry(str(match["left"].get("model") or "?"))
        b = entry(str(match["right"].get("model") or "?"))
        if a["model"] == b["model"]:
            continue  # self-match carries no information
        expected_a = 1.0 / (1.0 + math.pow(
            10.0, (b["rating"] - a["rating"]) / ELO_SCALE))
        outcome = {"left": 1.0, "right": 0.0}.get(vote["winner"], 0.5)
        a["rating"] += ELO_K * (outcome - expected_a)
        b["rating"] += ELO_K * ((1.0 - outcome) - (1.0 - expected_a))
        a["matches"] += 1
        b["matches"] += 1
        if vote["winner"] == "left":
            a["wins"] += 1; b["losses"] += 1
        elif vote["winner"] == "right":
            b["wins"] += 1; a["losses"] += 1
        else:
            a["ties"] += 1; b["ties"] += 1
    return table


def position_bias(project_dir: Path) -> dict[str, Any]:
    """How often the side displayed first won — a health check on the votes."""
    votes = load_votes(project_dir)
    decisive = [v for v in votes.values() if v["winner"] in ("left", "right")]
    if not decisive:
        return {"decisive": 0, "first_won": 0, "rate": None}
    first = sum(1 for v in decisive if v["winner"] == v.get("shown_first"))
    return {"decisive": len(decisive), "first_won": first,
            "rate": first / len(decisive)}


def export_preferences(project_dir: Path, out: Path) -> int:
    """Write judged matches as DPO pairs: chosen = the side that won.

    This is the second payoff of a vote. Ties and both-bad are skipped — DPO
    needs a strict preference, and 'both bad' says the pair should be fixed
    upstream rather than learned from.
    """
    matches = load_matches(project_dir)
    votes = load_votes(project_dir)
    written = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkstemp(dir=str(out.parent), suffix=".tmp")[1])
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for match_id, vote in votes.items():
                if vote["winner"] not in ("left", "right"):
                    continue
                match = matches.get(match_id)
                if not match:
                    continue
                win = match[vote["winner"]]
                lose = match["right" if vote["winner"] == "left" else "left"]
                handle.write(json.dumps({
                    "prompt": match["task"],
                    "context_digest": match["context_digest"],
                    "chosen": {"reasoning": win.get("reasoning", ""),
                               "action": win.get("action", "")},
                    "rejected": {"reasoning": lose.get("reasoning", ""),
                                 "action": lose.get("action", "")},
                    "chosen_model": win.get("model"),
                    "rejected_model": lose.get("model"),
                    # Join keys back into the gateway training ledger, whose
                    # rows carry trajectory_id == run_id. They turn a
                    # whole-outcome verdict into per-turn labels: every turn of
                    # the winning run is a positive example, every turn of the
                    # losing one a negative — which is the only way the vote
                    # reaches step-level training at all.
                    "chosen_run_id": win.get("run_id", ""),
                    "rejected_run_id": lose.get("run_id", ""),
                }, ensure_ascii=False) + "\n")
                written += 1
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return written


def choose_display_order(rng: random.Random | None = None) -> tuple[str, str]:
    """Randomise which stored side is rendered first, per view."""
    rng = rng or random
    return ("left", "right") if rng.random() < 0.5 else ("right", "left")
