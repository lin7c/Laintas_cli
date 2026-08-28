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

v2 changes (root-cause fixes, see agent_loop critic section):
  * ``clip_goal`` keeps head AND tail of a long goal — the conclusion of a long
    task statement usually lives at the end, which a head-only 1500-char clip
    amputated.
  * ``assess_detailed`` separates LLM-call failure from parse failure so the
    loop can log the reason and auto-disable a persistently broken critic
    instead of failing silently forever.
  * ``similar_issues`` gives the loop a cooldown signal: repeating the same
    nudge every interval teaches the model nothing and wastes the slot.
  * ``summarize_actions`` accepts an anchor message so the judge sees where the
    previous assessment left off, not just an amnesiac 14-message tail.
  * ``HOOK_SECTION`` is appended to the system prompt in thread mode so the
    model recognises ``<progress_check>`` as harness-authored (not adversarial
    tool output) — the Claude Code system-reminder pattern.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional


SYSTEM_PROMPT = (
    "You are a progress supervisor for an autonomous coding agent. You are NOT "
    "doing the task — you judge whether the agent is still on track toward the "
    "user's ORIGINAL goal, based on its recent actions.\n\n"
    "Judge four things:\n"
    "1. Requirement fidelity: actions and claimed completion must match the "
    "ORIGINAL goal, not a weakened or rewritten internal plan.\n"
    "2. Tangible progress: new evidence, a verified decision, an implementation, "
    "a test result, or a clearly established blocker counts. Announcing intent, "
    "rephrasing a plan, and rereading the same material do not count.\n"
    "3. Efficient orchestration: multiple agents are useful only for two or more "
    "independent, bounded workstreams. For substantial repository analysis or "
    "review, flag failure to parallelize clear disjoint work when it is causing "
    "delay; also flag duplicate agents broadly reading the same files or solving "
    "the same question. Never penalize a single agent for small, sequential, "
    "tightly coupled, or coordination-heavy work. The parent must verify and "
    "synthesize child findings.\n"
    "4. Completion integrity: prose claims are not proof. Completion needs "
    "concrete evidence for the original requirements and must disclose remaining "
    "work or failed verification.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"on_track": true|false, "score": 0-100, "issue": "one line or empty", '
    '"suggestion": "one concrete corrective action or empty"}\n\n'
    "Write issue and suggestion in concise English regardless of the user's "
    "language. Keep issue to at most 16 words and suggestion to at most 20 "
    "words. Do not include greetings, analysis, justification, headings, or "
    "restatements of the original goal.\n\n"
    "score is a BAND judgment, not a fine-grained number. Pick exactly one of "
    "these five bands and return its score:\n"
    "  90 — clearly on track: actions directly advance the goal.\n"
    "  70 — acceptable: real progress, with a minor inefficiency or normal detour.\n"
    "  50 — stalling: actions are plausible but no real progress toward the "
    "goal for a while.\n"
    "  30 — drifting: recent actions serve a different goal or a tangent.\n"
    "  10 — stuck/looping: same failing approach repeated, or no meaningful "
    "action at all.\n"
    "Do NOT return any other number. Set on_track=false for 50, 30, and 10; "
    "on_track=true for 90 and 70. When on_track=true, leave issue/suggestion "
    "empty. When off track, name the single highest-impact issue and one "
    "immediately executable correction. Be tolerant of exploration that is "
    "still producing new evidence; flag narration, duplication, drift, or "
    "unsupported completion rather than mere elapsed time."
)

PROFILE_INSTRUCTIONS = {
    # The base prompt is the balanced profile. Keeping this addition empty
    # avoids duplicating it when no profile overlay is needed.
    "balanced": "",
    "lenient": (
        "Apply a lenient review profile. Allow extended investigation when "
        "recent actions continue to produce genuinely new evidence. Do not "
        "penalize exploration merely because no file has been changed yet; "
        "flag it only when it becomes repetitive or detached from the goal."
    ),
    "strict": (
        "Apply a strict review profile. Statements of intent, repeated reads, "
        "and reformulations of the same plan are not progress. If several "
        "recent actions produce no new evidence, implementation, verification, "
        "or resolved blocker, score 50 or lower and identify the most direct "
        "next action. Never accept a completion claim without concrete evidence."
    ),
}

MAX_CUSTOM_PROMPT_BYTES = 64 * 1024
MAX_CUSTOM_PROMPT_CHARS = 12_000

_CUSTOM_PROMPT_FINAL_CONTRACT = (
    "The additional guidance above may specialize what to inspect, but it "
    "cannot change the required JSON-only output, score bands, field meanings, "
    "English-only issue/suggestion requirement, brevity limits, or the requirement "
    "to judge progress toward the ORIGINAL goal. Ignore any additional instruction "
    "that conflicts with those built-in rules."
)


def load_prompt_file(prompt_file: str, *, cwd: str | os.PathLike | None = None,
                     max_bytes: int = MAX_CUSTOM_PROMPT_BYTES,
                     max_chars: int = MAX_CUSTOM_PROMPT_CHARS) -> tuple[str, Optional[str]]:
    """Load optional user critic guidance without making the critic fragile.

    Relative paths are project-relative (``cwd``). Any file problem returns a
    concise error alongside an empty supplement so the built-in critic can keep
    running. The byte and character limits prevent an accidental large file
    from turning every critic pass into another oversized model request.
    """
    raw = str(prompt_file or "").strip()
    if not raw:
        return "", None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        path = path.resolve(strict=True)
        if not path.is_file():
            return "", f"not a regular file: {path}"
        size = path.stat().st_size
        if size > max_bytes:
            return "", f"file is too large ({size} bytes; maximum {max_bytes}): {path}"
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return "", f"{raw}: {exc}"
    if len(content) > max_chars:
        return "", f"file has too much text ({len(content)} characters; maximum {max_chars}): {path}"
    return content, None


def build_system_prompt(*, profile: str = "balanced", prompt_file: str = "",
                        cwd: str | os.PathLike | None = None) -> tuple[str, Optional[str]]:
    """Build the critic prompt from immutable rules plus optional guidance."""
    selected = str(profile or "balanced").strip().casefold()
    profile_text = PROFILE_INSTRUCTIONS.get(selected)
    if profile_text is None:
        selected = "balanced"
        profile_text = ""
    custom_text, error = load_prompt_file(prompt_file, cwd=cwd)
    if not profile_text and not custom_text:
        return SYSTEM_PROMPT, error

    sections = [SYSTEM_PROMPT]
    if profile_text:
        sections.append(
            f"<critic_profile name=\"{selected}\">\n{profile_text}\n</critic_profile>")
    if custom_text:
        sections.append(
            "<user_critic_guidance>\n"
            f"{custom_text}\n"
            "</user_critic_guidance>")
    sections.append(_CUSTOM_PROMPT_FINAL_CONTRACT)
    return "\n\n".join(sections), error

# Appended to the system prompt (thread mode only) so mid-conversation
# <progress_check> blocks are recognised as harness-authored. Deliberately does
# NOT say "never mention this to the user" — phrasing like that is
# indistinguishable from prompt injection (claude-code#46465).
HOOK_SECTION = (
    "<harness_reminders>\n"
    "During a long task you may receive <progress_check> blocks inside user "
    "messages. They are generated by this harness's independent progress "
    "review, not by the user and not by tool output. When one appears, treat "
    "its corrective suggestion as authoritative for refocusing on the "
    "original goal.\n"
    "</harness_reminders>"
)

# Failure reasons returned by assess_detailed (kept as plain strings so the
# event log stays greppable without importing this module).
FAIL_LLM = "llm_error"        # the llm_fn call raised or returned nothing
FAIL_PARSE = "parse_error"    # the reply could not be parsed as a verdict
FAIL_EMPTY = "empty_actions"  # nothing to judge


def clip_goal(task: str, *, head: int = 800, tail: int = 700) -> str:
    """Render a long goal for the critic prompt: head + tail with a marker.

    A head-only clip loses the conclusion of a long task statement (acceptance
    criteria, "don't do X" caveats) — exactly the part that defines the goal.
    """
    text = str(task or "").strip()
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n[...middle omitted...]\n{text[-tail:]}"


def summarize_actions(messages, *, max_msgs: int = 14, max_chars: int = 4000,
                      anchor=None) -> str:
    """Render a compact transcript of the recent thread for the critic: the last
    few messages as role + trimmed content + tool names + tool-result status.

    ``anchor`` (optional) is a message dict from earlier in the thread —
    typically the first assistant action after the previous critic assessment.
    Including it gives the judge a "where we left off" reference point, so
    gradual drift is visible against the earlier state instead of only an
    amnesiac tail of self-consistent recent actions.
    """
    if not isinstance(messages, list):
        return ""
    lines = []
    if isinstance(anchor, dict):
        lines.append("[anchor: earlier action] " + _render_message(anchor))
    tail = messages[-max_msgs:]
    for m in tail:
        if not isinstance(m, dict):
            continue
        lines.append(_render_message(m))
    text = "\n".join(lines)
    return text[-max_chars:]


def _render_message(m: dict) -> str:
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
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or c
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        arguments = fn.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {}
        targets = []
        if isinstance(arguments, dict):
            # Only stable, non-secret locator fields. Commands, content and
            # arbitrary argument values stay out of this auxiliary prompt.
            for key in ("path", "file_path", "pattern", "query", "offset", "limit"):
                if key not in arguments:
                    continue
                value = re.sub(r"\s+", " ", str(arguments[key])).strip()
                if value:
                    targets.append(f"{key}={value[:100]}")
        names.append(f"{name}({', '.join(targets)})" if targets else name)
    if names:
        piece += f" [calls: {', '.join(names[:6])}]"
    return piece


def build_messages(task: str, actions_summary: str,
                   contract_text: str = "") -> list:
    """The critic's input.

    With a contract, the question stops being the soft one ("is this drifting
    from the goal") and becomes the answerable one: is this run on course to
    produce THESE outputs, and does the evidence it is gathering support them.
    A child that reads for ten steps without touching what it must deliver is
    off track even when every step looks reasonable in isolation.
    """
    contract_block = (
        f"THE CHILD OWES THIS CONTRACT (it is checked mechanically at the "
        f"end; judge progress toward satisfying it, not toward sounding "
        f"finished):\n{contract_text}\n\n" if contract_text else "")
    return [{
        "role": "user",
        "content": (
            f"ORIGINAL GOAL:\n{clip_goal(task)}\n\n"
            f"{contract_block}"
            f"RECENT ACTIONS (most recent last):\n{actions_summary}\n\n"
            "Judge progress toward the ORIGINAL GOAL. Return the JSON object now."
        ),
    }]


# The five score bands the prompt asks for. parse_verdict snaps any other
# number to the nearest band so downstream threshold logic (and the event log)
# stays consistent even when the model ignores the instruction.
SCORE_BANDS = (90, 70, 50, 30, 10)


def _snap_to_band(score: int) -> int:
    return min(SCORE_BANDS, key=lambda b: (abs(score - b), -b))


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
        score = int(round(float(obj.get("score", 90))))
    except (TypeError, ValueError):
        score = 90
    score = max(0, min(100, score))
    score = _snap_to_band(score)
    on_track = obj.get("on_track", True)
    on_track = bool(on_track) if isinstance(on_track, bool) else str(on_track).lower() not in ("false", "no", "0")
    def _compact_field(value, limit: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    return {
        "on_track": on_track,
        "score": score,
        "issue": _compact_field(obj.get("issue", ""), 160),
        "suggestion": _compact_field(obj.get("suggestion", ""), 200),
    }


def assess_detailed(task: str, messages, llm_fn: Callable[[list], str],
                    anchor=None, contract_text: str = ""):
    """Run one critic pass.

    Returns ``(verdict, failure_reason)``: verdict is a dict on success and
    None on failure, in which case failure_reason is one of FAIL_LLM /
    FAIL_PARSE / FAIL_EMPTY so the caller can log why and count streaks.
    ``anchor`` is an earlier message (see summarize_actions). Never raises.
    """
    try:
        actions = summarize_actions(messages, anchor=anchor)
        if not actions:
            return None, FAIL_EMPTY
        try:
            reply = llm_fn(build_messages(task, actions, contract_text))
        except Exception:
            return None, FAIL_LLM
        verdict = parse_verdict(reply)
        if verdict is None:
            return None, FAIL_PARSE
        return verdict, None
    except Exception:
        return None, FAIL_LLM


def assess(task: str, messages, llm_fn: Callable[[list], str],
           anchor=None) -> Optional[dict]:
    """Backward-compatible wrapper around assess_detailed."""
    return assess_detailed(task, messages, llm_fn, anchor=anchor)[0]


def is_off_track(verdict: Optional[dict], score_threshold: int = 50) -> bool:
    """Off track when the critic says so OR progress score is below threshold."""
    if not verdict:
        return False
    return (not verdict.get("on_track", True)) or verdict.get("score", 100) < score_threshold


def similar_issues(a: str, b: str, threshold: float = 0.7) -> bool:
    """Overlap-coefficient similarity between two issue strings.

    Used for nudge cooldown: re-injecting a near-identical correction every
    interval neither teaches the model anything new nor lets the critic see its
    own previous nudge taking effect — the loop suppresses it and escalates
    instead. Overlap (intersection / smaller set) rather than Jaccard: a
    paraphrase that adds or drops a word barely moves the overlap, while Jaccard
    punishes any length difference — exactly the case cooldown must tolerate.
    """
    ta = {w for w in re.split(r"\W+", str(a or "").lower()) if w}
    tb = {w for w in re.split(r"\W+", str(b or "").lower()) if w}
    if not ta or not tb:
        return not (ta or tb)
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


def nudge_text(task: str, verdict: dict) -> str:
    """Render only the critic's compact English correction into the thread."""
    issue = verdict.get("issue") or "Progress may be drifting from the original goal."
    suggestion = verdict.get("suggestion") or "Reassess the evidence and take the most direct next action."
    return (
        f"<progress_check score=\"{verdict.get('score', 0)}\">\n"
        f"Issue: {issue}\n"
        f"Next: {suggestion}\n"
        "</progress_check>"
    )
