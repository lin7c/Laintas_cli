"""Long-task critic (#2) — an external progress supervisor.

Models are poor at self-monitoring across a long context: they drift from the
original goal, loop on a dead end, or declare success prematurely. Rather than
trust the working model to police itself, a *separate* cheap LLM call periodically
looks at the original task plus the recent actions and answers: is this still on
track, and how is progress going? When it says "off track", we inject a focused
nudge back into the conversation.

This complements — does not replace — the deterministic tripwires already in the
loop (staleness, repetition, the repeat-failure ledger): those catch mechanical
spinning cheaply and always; the critic catches *semantic* drift a rule can't see.

Pure, testable logic here (prompt, parse, orchestrate one call). The agent loop
supplies an ``llm_fn`` and decides when to call ``assess``. Never raises.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional


SYSTEM_PROMPT = (
    "You are a progress supervisor for an autonomous coding agent. You are NOT "
    "doing the task — you judge whether the agent is still on track toward the "
    "user's ORIGINAL goal, based on its recent actions.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"on_track": true|false, "score": 0-100, "issue": "one line or empty", '
    '"suggestion": "one concrete corrective action or empty"}\n\n'
    "score = how well recent actions serve the original goal (100 = perfectly, "
    "0 = completely lost). Set on_track=false when the agent is looping on a dead "
    "end, chasing a tangent, fighting the same error repeatedly, or drifting from "
    "the goal. If it is progressing sensibly, on_track=true and leave issue/"
    "suggestion empty. Be tolerant of normal exploration; only flag REAL trouble."
)


def summarize_actions(messages, *, max_msgs: int = 14, max_chars: int = 4000) -> str:
    """Render a compact transcript of the recent thread for the critic: the last
    few messages as role + trimmed content + tool names + tool-result status."""
    if not isinstance(messages, list):
        return ""
    tail = messages[-max_msgs:]
    lines = []
    for m in tail:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):  # OpenAI content parts
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict))
        content = re.sub(r"\s+", " ", str(content or "")).strip()
        piece = f"{role}: {content[:400]}" if content else f"{role}:"
        calls = m.get("tool_calls") or []
        names = []
        for c in calls:
            fn = (c.get("function") or {}) if isinstance(c, dict) else {}
            if fn.get("name"):
                names.append(fn["name"])
        if names:
            piece += f" [calls: {', '.join(names[:6])}]"
        lines.append(piece)
    text = "\n".join(lines)
    return text[-max_chars:]


def build_messages(task: str, actions_summary: str) -> list:
    return [{
        "role": "user",
        "content": (
            f"ORIGINAL GOAL:\n{str(task or '')[:1500]}\n\n"
            f"RECENT ACTIONS (most recent last):\n{actions_summary}\n\n"
            "Judge progress toward the ORIGINAL GOAL. Return the JSON object now."
        ),
    }]


def parse_verdict(reply: str) -> Optional[dict]:
    """Parse the critic reply into {on_track, score, issue, suggestion}, or None
    if it can't be understood (caller then treats it as 'no verdict')."""
    if not reply or not isinstance(reply, str):
        return None
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    try:
        score = int(round(float(obj.get("score", 100))))
    except (TypeError, ValueError):
        score = 100
    score = max(0, min(100, score))
    on_track = obj.get("on_track", True)
    on_track = bool(on_track) if isinstance(on_track, bool) else str(on_track).lower() not in ("false", "no", "0")
    return {
        "on_track": on_track,
        "score": score,
        "issue": str(obj.get("issue", "")).strip()[:300],
        "suggestion": str(obj.get("suggestion", "")).strip()[:300],
    }


def assess(task: str, messages, llm_fn: Callable[[list], str]) -> Optional[dict]:
    """Run one critic pass. Returns a verdict dict or None on any failure."""
    try:
        actions = summarize_actions(messages)
        if not actions:
            return None
        reply = llm_fn(build_messages(task, actions))
        return parse_verdict(reply)
    except Exception:
        return None


def is_off_track(verdict: Optional[dict], score_threshold: int = 50) -> bool:
    """Off track when the critic says so OR progress score is below threshold."""
    if not verdict:
        return False
    return (not verdict.get("on_track", True)) or verdict.get("score", 100) < score_threshold


def nudge_text(task: str, verdict: dict) -> str:
    """A focused corrective message injected back into the conversation."""
    issue = verdict.get("issue") or "you may be drifting from the original goal"
    suggestion = verdict.get("suggestion") or "step back and re-plan the next action toward the goal"
    return (
        "<progress_check>\n"
        f"An independent progress review flags a problem (score {verdict.get('score', 0)}/100): "
        f"{issue}\n"
        f"Suggested correction: {suggestion}\n"
        f"Refocus on the ORIGINAL goal and take the most direct next step. "
        "Do not repeat the failing approach.\n"
        "</progress_check>"
    )
