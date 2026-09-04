"""Repair signals — what the user's NEXT turn says about the last one.

The gap this fills
------------------
``critic.py`` judges the agent against the goal *within* a task. ``intent.py``
judges whether the goal was understood before work started. Neither looks at
the one judgement that is not the agent's own: **the user's reaction**. A
correction is the highest-quality signal in the system and it is currently
discarded the moment the next turn begins.

Why this module only LOGS
-------------------------
Same discipline as ``mem_signals`` / ``precheck`` / ``redactor``: capture
weak-labeled signal from real usage now, decide what to do about it later.
That is not caution for its own sake — it is what a measurement of this
machine's own history showed was necessary. Reconstructed from 406 real
sessions (2141 session files, 1736 user turns that follow an assistant reply):

  * A keyword rule marks 2.0% of turns as a possible correction, and roughly a
    third of those are false: the words fire on constraints the user wrote
    INTO the request ("don't do anything else") rather than on rejections of
    the answer.
  * Hand-reading a sample of the turns the rule did NOT mark found real
    corrections at a rate that puts the rule's recall near 10%. The ones it
    misses have no keyword at all -- a bare "what are you doing?" is a
    correction only because of what the agent had just said, and a rule that
    reads one side of the exchange cannot see that. That is why no keyword
    list survives in this module: it was measured and it did not earn its
    place.
  * So a classifier that reads BOTH sides is required, and it cannot be a
    keyword list. It also must not be an uncalibrated score: what this module
    stores are anchored FACTS a later pass can label, never a verdict.

Deliberately NOT recorded here: a satisfaction number. Two observers agree on
"the user quoted their earlier requirement back"; they do not agree on "41 out
of 100", and nothing downstream can tell 49 from 51.

Everything is bounded, redacted through ``redactor``, off by default
(``LAINTAS_REPAIR_SIGNALS=1``), and no function raises into the agent loop.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import paths

#: Off by default, opt-in — parity with mem_signals/precheck/redact capture.
_ENABLED = os.environ.get(
    "LAINTAS_REPAIR_SIGNALS", "0").strip().lower() in ("1", "true", "yes")

try:
    import redactor
except Exception:  # pragma: no cover - sibling module
    redactor = None

SIGNALS_FILENAME = "repair_signals.jsonl"
_SCHEMA_VERSION = 1

#: Text budgets. A correction lives at the start of the user's turn and at the
#: END of the agent's — the claim being rejected is usually its conclusion.
_MAX_USER = 800
_MAX_ASSISTANT = 800

#: How many earlier user turns to compare against for a restatement. A user who
#: repeats themselves does it within the working memory of the conversation,
#: and an unbounded scan makes the cost of this hook grow with session length.
_LOOKBACK_TURNS = 12

#: Turn outcomes that need no judgement at all: the user stopped the agent, or
#: refused what it proposed. These are facts the loop already knows.
DETERMINISTIC_REASONS = frozenset({"interrupted", "aborted", "user_denied"})


def enabled() -> bool:
    return _ENABLED


def _redact(text: str, limit: int) -> str:
    if not text:
        return ""
    value = str(text)
    if redactor is not None:
        try:
            value, _ = redactor.scrub_text(value, enforce=True, capture=False)
        except Exception:
            value = str(text)
    return value[:limit]


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
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Deterministic text similarity ─────────────────────────────────────────

def _bigrams(text: str) -> set:
    """Character bigrams, which behave for Chinese and English alike.

    Word tokenisation would need a segmenter for Chinese and would make the
    measure depend on one; bigrams need nothing and are symmetric. Whitespace
    is collapsed so re-typed formatting does not count as a difference.
    """
    squeezed = re.sub(r"\s+", " ", str(text or "")).strip()
    if not squeezed:
        return set()
    if len(squeezed) == 1:
        return {squeezed}
    return {squeezed[i:i + 2] for i in range(len(squeezed) - 1)}


def similarity(left: str, right: str) -> float:
    """Jaccard overlap of character bigrams, in [0, 1].

    Deterministic and free. It is a CANDIDATE finder, not a decision: the
    highest-scoring pairs in this machine's own history were batch automation
    running one template over many tickers, which is not a correction at all.
    That is precisely why the score is stored rather than acted on.
    """
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    return round(intersection / len(a | b), 4)


def _closest_prior(user_text: str, prior_users: list) -> tuple:
    """(index, score) of the earlier user turn most like this one."""
    best_index, best_score = -1, 0.0
    window = prior_users[-_LOOKBACK_TURNS:]
    offset = len(prior_users) - len(window)
    for position, earlier in enumerate(window):
        score = similarity(user_text, earlier)
        if score > best_score:
            best_index, best_score = offset + position, score
    return best_index, best_score


def user_turns(thread_messages) -> list:
    """The user's own turns, oldest first, from a native message thread."""
    if not isinstance(thread_messages, list):
        return []
    out = []
    for message in thread_messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            out.append(content)
    return out


def last_assistant_text(thread_messages) -> str:
    """What the agent last SAID — not what it did.

    Tool calls are how it worked; the reply is what the user reacted to, and a
    turn that ended in tool calls with no prose gives the next turn nothing to
    be a reaction to.
    """
    if not isinstance(thread_messages, list):
        return ""
    for message in reversed(thread_messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""


# ── Hooks ─────────────────────────────────────────────────────────────────

def note_failure(where: str, error: BaseException) -> None:
    """Record that a hook raised, instead of swallowing it into silence.

    `except Exception: pass` around a capture hook is how a capability goes
    missing for the life of a release with nothing on screen to say so -- this
    repository has the scar (see `extension_runtime.register_tool`). The hook
    still must not be able to end a user's turn, so the exception is caught;
    it just stops being invisible. Written to this module's own log, which is
    off by default and read by whoever is looking at these signals anyway.

    Guarded end to end: it is called FROM an except block, so it raising would
    replace a captured error with an uncaught one.
    """
    try:
        _record("error", where=str(where or "")[:80],
                error=f"{type(error).__name__}: {error}"[:300])
    except Exception:
        pass


def on_user_turn(user_text: str, thread_messages, *,
                 session_id: str = "", run_id: str = "",
                 agent_id: str = "", prior_reason: str = "") -> Optional[dict]:
    """Record one user turn and how it relates to what came before.

    Returns the row it wrote (or would have written) so callers and tests can
    inspect it; returns None when there is nothing to compare against — the
    first turn of a session has no previous answer to be a reaction to.

    Only the primary agent's turns are recorded. A sub-agent's "prompt" is
    written by this program, so treating it as a user reaction would fill the
    log with the agent grading itself: on this machine's own history that is
    2571 of 2600 admitted prompts.
    """
    if agent_id:
        return None
    text = str(user_text or "").strip()
    if not text:
        return None

    prior_users = user_turns(thread_messages)
    # The current turn may already have been appended to the thread by the
    # caller. Drop one trailing copy so a turn is never compared with itself.
    if prior_users and prior_users[-1].strip() == text:
        prior_users = prior_users[:-1]
    assistant = last_assistant_text(thread_messages)
    if not prior_users or not assistant:
        return None

    index, score = _closest_prior(text, prior_users)
    row = {
        "session_id": str(session_id or ""),
        "run_id": str(run_id or ""),
        "turn_index": len(prior_users),
        "user": _redact(text, _MAX_USER),
        "assistant_tail": _redact(assistant[-_MAX_ASSISTANT:], _MAX_ASSISTANT),
        "prior_user": _redact(prior_users[-1], _MAX_USER),
        "closest_prior_index": index,
        "closest_prior_similarity": score,
        "prior_turn_reason": str(prior_reason or ""),
        "prior_turn_deterministic": str(prior_reason or "") in DETERMINISTIC_REASONS,
    }
    _record("followup", **row)
    return row


def on_turn_end(reason: str, *, session_id: str = "", run_id: str = "",
                agent_id: str = "") -> Optional[dict]:
    """Record an outcome that needs no judgement to read.

    `interrupted`, `aborted` and `user_denied` are the user acting on the
    agent rather than talking about it. They are the only dissatisfaction
    signals in this system that no model has to be asked about.
    """
    if agent_id:
        return None
    if str(reason or "") not in DETERMINISTIC_REASONS:
        return None
    row = {
        "session_id": str(session_id or ""),
        "run_id": str(run_id or ""),
        "reason": str(reason),
    }
    _record("outcome", **row)
    return row


# ── Classification: one auxiliary call, judging both sides ────────────────
#
# The measurement above is why this is a model call and not a rule: a keyword
# list finds 2% of turns at ~65% precision and misses roughly nine in ten of
# the real ones, because the ones it misses have no keyword at all. A bare
# "what are you doing?" is a correction only in the light of what the agent
# had just said, and nothing that reads one side of the exchange can see that.
#
# What the call may return is still constrained. It emits a CLASS plus the
# spans it read that class off, and a span that is not literally in the text is
# dropped -- the same rule that lets `intent.validate_spec` accept a small
# model's output into an authoritative place: it can only ever quote, never
# author. There is no score, because nothing downstream could tell 49 from 51.

SYSTEM_PROMPT = (
    "You judge ONE thing: what the user's latest message says about the "
    "assistant's previous answer. You are not doing the task and you never "
    "give advice.\n\n"
    "Classify the user's message as exactly one of:\n"
    "  restated   - they are repeating a requirement they already gave, "
    "because the assistant did not follow it\n"
    "  contradicted - they say the assistant's claim, result or action is "
    "wrong\n"
    "  confused   - they cannot tell what the assistant did, or ask what is "
    "going on\n"
    "  refined    - they accept the direction and add or narrow a "
    "requirement\n"
    "  redirected - they changed their mind or moved to something else; the "
    "assistant did nothing wrong\n"
    "  proceeding - an ordinary next step, a question, or approval\n"
    "  unclear    - you cannot tell from what you were given\n\n"
    "Two distinctions carry most of the weight:\n"
    "  * A constraint written INTO a request (\"don't do anything else\") is "
    "not a rejection of an answer. Judge the relationship to the PREVIOUS "
    "answer, not the presence of negative words.\n"
    "  * refined and redirected are NOT failures. Only restated, contradicted "
    "and confused say something went wrong.\n\n"
    "Answer with one JSON object and nothing else:\n"
    '{"kind": "...", "later_anchor": "...", "earlier_anchor": "...", '
    '"about": "..."}\n\n'
    "later_anchor: a short EXACT quote from the user's latest message that "
    "shows the class.\n"
    "earlier_anchor: for `restated`, the EXACT quote from an earlier user "
    "message being repeated; otherwise \"\".\n"
    "about: for `contradicted`, the EXACT quote from the assistant's answer "
    "being disputed; otherwise \"\".\n"
    "Every quote must appear character-for-character in the text you were "
    "given. Quote nothing rather than paraphrase; an invented quote is worse "
    "than an empty one."
)

#: Classes that say the previous turn went wrong. `refined` and `redirected`
#: are deliberately outside it: a user adding a constraint or changing their
#: mind is not a failure, and counting them as one is how a "dissatisfaction"
#: detector ends up measuring conversation length.
REPAIR_KINDS = frozenset({"restated", "contradicted", "confused"})
_ALL_KINDS = REPAIR_KINDS | {"refined", "redirected", "proceeding", "unclear"}

#: What the judge is shown of each side. The user's message whole (it is the
#: thing being judged); the assistant's tail (a rejection lands on its
#: conclusion); a few earlier turns for the restatement anchor.
_JUDGE_USER = 2000
_JUDGE_ASSISTANT = 2000
_JUDGE_PRIOR = 600
_JUDGE_PRIOR_TURNS = 4


def build_messages(user_text: str, assistant_text: str,
                   prior_users: list) -> list:
    """The single call's input. Tool-less: this call never acts."""
    earlier = [str(t or "")[:_JUDGE_PRIOR]
               for t in (prior_users or [])[-_JUDGE_PRIOR_TURNS:]]
    block = "\n".join(f"[earlier user message {i + 1}] {text}"
                       for i, text in enumerate(earlier))
    return [{
        "role": "user",
        "content": (
            f"{block}\n\n"
            f"[assistant's previous answer]\n"
            f"{str(assistant_text or '')[-_JUDGE_ASSISTANT:]}\n\n"
            f"[user's latest message]\n"
            f"{str(user_text or '')[:_JUDGE_USER]}"
        ),
    }]


def _parse_json_object(raw) -> Optional[dict]:
    """The JSON object in a model reply, or None. Never raises.

    A fenced block, a preamble, or a trailing sentence are all normal from a
    small model and none of them is a failure worth discarding the answer for.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def parse_verdict(raw) -> Optional[dict]:
    return _parse_json_object(raw)


def validate_verdict(verdict, user_text: str,
                     prior_users: list, assistant_text: str) -> Optional[dict]:
    """Keep only what the judge could have QUOTED rather than invented.

    An anchor that is not literally present is dropped, and a class that needs
    an anchor it did not supply falls back to `unclear`. This is the whole
    basis for letting a small auxiliary model write into a signal log: it can
    point at the text, it cannot make text up.
    """
    if not isinstance(verdict, dict):
        return None
    kind = str(verdict.get("kind") or "").strip().lower()
    if kind not in _ALL_KINDS:
        return None

    def _anchor(field: str, haystacks) -> str:
        value = str(verdict.get(field) or "").strip()
        if not value:
            return ""
        for hay in haystacks:
            if value in str(hay or ""):
                return value
        return ""

    later = _anchor("later_anchor", [user_text])
    earlier = _anchor("earlier_anchor", prior_users or [])
    about = _anchor("about", [assistant_text])

    # A repair class with nothing quotable behind it is an opinion, and an
    # opinion is the thing this module exists not to store.
    if kind in REPAIR_KINDS and not later:
        kind = "unclear"
    if kind == "restated" and not earlier:
        kind = "unclear"
    return {"kind": kind, "later_anchor": later,
            "earlier_anchor": earlier, "about": about}


def classify(user_text: str, assistant_text: str, prior_users: list,
             llm_fn) -> Optional[dict]:
    """One auxiliary call. Returns a validated verdict, or None. Never raises."""
    if not callable(llm_fn):
        return None
    try:
        raw = llm_fn(build_messages(user_text, assistant_text, prior_users))
    except Exception:
        return None
    return validate_verdict(parse_verdict(raw), user_text, prior_users,
                            assistant_text)


def record_verdict(verdict, *, session_id: str = "", run_id: str = "",
                   turn_index: int = -1) -> Optional[dict]:
    """Append a validated verdict beside the facts it was read from."""
    if not isinstance(verdict, dict) or not verdict.get("kind"):
        return None
    row = {
        "session_id": str(session_id or ""),
        "run_id": str(run_id or ""),
        "turn_index": int(turn_index),
        "kind_verdict": str(verdict.get("kind")),
        "is_repair": str(verdict.get("kind")) in REPAIR_KINDS,
        "later_anchor": _redact(verdict.get("later_anchor") or "", _MAX_USER),
        "earlier_anchor": _redact(verdict.get("earlier_anchor") or "", _MAX_USER),
        "about": _redact(verdict.get("about") or "", _MAX_ASSISTANT),
    }
    _record("verdict", **row)
    return row


# ── Reflection: what the correction was about ─────────────────────────────
#
# Runs only after a repair verdict, and produces two things: the divergence
# stated as FACTS, and a short analysis of where it happened. It goes into the
# turn that is already running, because that is the turn which has to do
# better -- the same route sub-agent results take when they arrive mid-task.
#
# What it deliberately does NOT produce is a judgement about the user. The
# original design for this had a component whose job was to argue that the
# dissatisfaction might be the user's misunderstanding; injected into the main
# context, that is a licence to dismiss feedback, and with no arbiter between
# the two the model would take the reading that costs it nothing. So the
# output is "the agent acted on X, the user asked for Y" -- both quoted, no
# verdict on either.
#
# It writes no memory and no skill. Whether a lesson is durable is decided by
# the existing memory path, on its own evidence rules, later.

REFLECTION_PROMPT = (
    "The user has just corrected the assistant. State the divergence as "
    "facts, in one JSON object and nothing else:\n"
    '{"understood": "...", "asked": "...", "analysis": "..."}\n\n'
    "understood: an EXACT quote from the assistant's answer showing what it "
    "acted on.\n"
    "asked: an EXACT quote from the user showing what was actually wanted.\n"
    "analysis: one or two sentences on where the two diverged, addressed to "
    "the assistant.\n\n"
    "Rules:\n"
    "  * Both quotes must appear character-for-character in the text you were "
    "given. Quote nothing rather than paraphrase.\n"
    "  * Do not judge the user, and do not explain why the user might be "
    "mistaken. If the request was ambiguous, say what was ambiguous about the "
    "WORDS, not about the person.\n"
    "  * Do not propose a fix and do not restate the task. The assistant is "
    "about to do that itself; your job is to say what it got wrong."
)

_MAX_ANALYSIS = 500

#: Notes waiting to be picked up by the turn they belong to, keyed by run id.
#: A slot rather than a queue: a second correction in the same run supersedes
#: the first, which is what the model needs -- the newest divergence is the one
#: it has to act on.
_pending_notes: dict = {}
_notes_lock = threading.Lock()


def validate_reflection(data, user_text: str, prior_users: list,
                        assistant_text: str) -> Optional[dict]:
    """Keep the quotes that are real; drop the ones that are not.

    Same rule as the verdict: a small model may point at the text, never
    invent it. A reflection with neither quote intact has nothing factual left
    in it and is discarded rather than injected as prose.
    """
    if not isinstance(data, dict):
        return None

    def _anchor(field: str, haystacks) -> str:
        value = str(data.get(field) or "").strip()
        if not value:
            return ""
        for hay in haystacks:
            if value in str(hay or ""):
                return value
        return ""

    understood = _anchor("understood", [assistant_text])
    asked = _anchor("asked", list(prior_users or []) + [user_text])
    analysis = str(data.get("analysis") or "").strip()[:_MAX_ANALYSIS]
    if not understood and not asked:
        return None
    return {"understood": understood, "asked": asked, "analysis": analysis}


def reflect(user_text: str, assistant_text: str, prior_users: list,
            llm_fn) -> Optional[dict]:
    """One more auxiliary call, after a repair verdict. Never raises."""
    if not callable(llm_fn):
        return None
    try:
        raw = llm_fn(build_messages(user_text, assistant_text, prior_users))
    except Exception:
        return None
    return validate_reflection(_parse_json_object(raw), user_text,
                               prior_users, assistant_text)


def format_note(verdict, reflection) -> str:
    """The text the running turn sees. Facts first, analysis last.

    Framed as what the user corrected, not as a score and not as a complaint:
    the model is about to act on this, and a note that reads as an accusation
    invites an apology instead of a change.
    """
    if not isinstance(reflection, dict):
        return ""
    kind = str((verdict or {}).get("kind") or "")
    lines = []
    if kind:
        lines.append(f"The user's message corrects the previous answer "
                     f"({kind}).")
    if reflection.get("understood"):
        lines.append(f"- acted on: \"{reflection['understood']}\"")
    if reflection.get("asked"):
        lines.append(f"- actually asked: \"{reflection['asked']}\"")
    if reflection.get("analysis"):
        lines.append(reflection["analysis"])
    return "\n".join(lines).strip()


def publish_note(run_id: str, note: str) -> None:
    """Hand a finished note to the turn it belongs to."""
    text = str(note or "").strip()
    if not text:
        return
    with _notes_lock:
        _pending_notes[str(run_id or "")] = text


def take_note(run_id: str) -> str:
    """The note for this run, once. Cleared on read so it is not repeated.

    Repeating it every iteration would turn one correction into a standing
    accusation, and the model would keep answering it instead of working.
    """
    with _notes_lock:
        return _pending_notes.pop(str(run_id or ""), "")


def clear_notes() -> None:
    with _notes_lock:
        _pending_notes.clear()


def record_reflection(reflection, *, session_id: str = "", run_id: str = "",
                      turn_index: int = -1) -> Optional[dict]:
    if not isinstance(reflection, dict):
        return None
    row = {
        "session_id": str(session_id or ""),
        "run_id": str(run_id or ""),
        "turn_index": int(turn_index),
        "understood": _redact(reflection.get("understood") or "", _MAX_ASSISTANT),
        "asked": _redact(reflection.get("asked") or "", _MAX_USER),
        "analysis": _redact(reflection.get("analysis") or "", _MAX_ANALYSIS),
    }
    _record("reflection", **row)
    return row
