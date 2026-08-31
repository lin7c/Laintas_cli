"""Intent alignment (#3) — understand the request before working on it.

The progress critic in ``critic.py`` answers "is this still going somewhere?"
It cannot answer "was this ever the right somewhere?", because its only
statement of the goal is the user's original sentence — the same sentence the
working model already read, and possibly misread, on turn one. A request like
"I want a website like ChatGPT" is understood, silently and differently, by
every model that reads it; by the time drift is measurable the wrong thing has
been half-built.

So this module runs *before* the work: a few rounds of self-questioning expand
the request into a **spec**, the spec is compared against what the model
actually started doing, and a disagreement is resolved by argument rather than
by decree.

Four rules shape everything here, and each one is load-bearing:

  1. **The critic never gathers evidence; it asks.** These calls are tool-less
     (see the cost note in ``agent_loop``'s critic launcher) and run on the
     cheap auxiliary model. A question it cannot answer from the user's own
     words becomes either an item for the *working* model — which has the whole
     toolset and is the stronger reasoner — or a recorded assumption. It never
     becomes an invented fact.

  2. **Every requirement is anchored to the user's literal words.**
     ``validate_spec`` mechanically drops any requirement whose ``anchor`` is
     not a substring of the original request. This is the entire basis for
     letting a 26B judge write into an authoritative prompt section: it can
     only ever quote, never author.

  3. **The working model may disagree.** A challenge is injected into the
     thread it was going to answer anyway, so a rebuttal costs no extra call
     from the expensive model, and the argument runs interleaved with real
     work. Debate is what makes a weaker judge usable at all; unstructured
     agreement is not evidence of correctness.

  4. **Unresolved means ask the user, not pick a winner.** After the round
     budget, neither side overwrites the other.

Pure, testable logic only: prompts, parsing, validation, rendering, and the
phase machine. The loop supplies ``llm_fn`` and decides when to call. Nothing
here raises.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional


# ── Phases ────────────────────────────────────────────────────────────────
# Plain strings so the event log and /debug stay greppable without importing.
IDLE = "idle"
ANALYZING = "analyzing"          # self-ask rounds in flight
SPEC_READY = "spec_ready"        # a validated spec exists, not yet compared
COMPARING = "comparing"          # comparison call in flight
ALIGNED = "aligned"              # spec agreed; it becomes the critic's contract
CORRECTING = "correcting"        # a correction was injected, awaiting effect
DEBATING = "debating"            # challenge injected, awaiting a rebuttal
ESCALATED = "escalated"          # round budget spent; the user decides
DISABLED = "disabled"            # persistent failure; degrade to today's loop

TERMINAL_PHASES = (ALIGNED, ESCALATED, DISABLED)

# Failure reasons (mirrors critic.FAIL_*).
FAIL_LLM = "llm_error"
FAIL_PARSE = "parse_error"
FAIL_EMPTY = "empty_input"

# Divergence severities, in the order the loop escalates them.
DETAIL_GAP = "detail_gap"          # right direction, missing specifics
SCOPE_ERROR = "scope_error"        # working on a different problem
CRITIC_UNSURE = "critic_unsure"    # the judge itself has no confident reading
SEVERITIES = (DETAIL_GAP, SCOPE_ERROR, CRITIC_UNSURE)

# Where an open question can be answered from.
NEEDS_USER = "user"            # only the person who wrote the request knows
NEEDS_EVIDENCE = "evidence"    # the repo or the web knows; the model can look

MAX_REQUIREMENTS = 12
MAX_QUESTIONS = 8
MAX_TASKS = 10
MIN_ANCHOR_CHARS = 4


# ── Prompts ───────────────────────────────────────────────────────────────

SELF_ASK_SYSTEM = (
    "You are clarifying a work request BEFORE any work happens. You are not "
    "doing the task and you must not propose a technical solution.\n\n"
    "Method: ask yourself the questions a careful colleague would ask about "
    "this request, then answer only the ones the request itself answers.\n\n"
    "Hard rules:\n"
    "1. Every requirement MUST carry an `anchor`: a VERBATIM substring copied "
    "from the request, at least a few words long. Never paraphrase, translate, "
    "or tidy an anchor. A requirement you cannot anchor is not a requirement — "
    "it is a question or an assumption.\n"
    "2. Never state a fact about the world, a product, a library, or this "
    "codebase. If answering needs a lookup, emit it as an open question with "
    "needs=\"evidence\". If only the requester can settle it, use "
    "needs=\"user\".\n"
    "3. Prefer few, sharp requirements over many vague ones.\n"
    "4. Keep the requester's own language for `goal`, `text` and `anchor`. "
    "Anchors are compared literally against the request.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"goal": "one sentence", '
    '"requirements": [{"id": "R1", "text": "...", "anchor": "verbatim words"}], '
    '"out_of_scope": ["..."], "deliverables": ["..."], '
    '"open_questions": [{"id": "Q1", "q": "...", "needs": "user|evidence", '
    '"why": "what it changes"}], '
    '"assumptions": [{"id": "A1", "text": "...", "risk": "high|low"}], '
    '"task_breakdown": ["ordered steps a competent engineer would take"]}'
)

SELF_ASK_FOLLOWUP = (
    "This is a later round. The spec so far is below. Deepen it: split a "
    "requirement that hides two, drop one the request does not support, turn a "
    "shaky assumption into a question, and add the tasks the earlier round "
    "missed. Return the COMPLETE updated JSON object, not a diff."
)

COMPARE_SYSTEM = (
    "You judge whether an autonomous coding agent's opening moves match the "
    "agreed reading of the request. You are NOT judging technical choices, "
    "code quality, or speed — only whether it is working on the right "
    "problem.\n\n"
    "Recent actions are numbered `[step N]`. When you report a divergence you "
    "MUST cite the step numbers it appears in, and the requirement id it "
    "violates or ignores.\n\n"
    "Choose exactly one severity:\n"
    "  detail_gap — the direction is right; specifics from the spec are "
    "missing or not yet addressed. Normal early work is a detail_gap, not an "
    "error.\n"
    "  scope_error — the actions serve a different problem than the spec "
    "describes, or contradict something the spec puts out of scope.\n"
    "  critic_unsure — you cannot tell from the actions shown. Say so; do not "
    "guess.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"severity": "detail_gap|scope_error|critic_unsure", '
    '"aligned": true|false, '
    '"divergences": [{"req_id": "R1", "steps": [3,4], "what": "one line", '
    '"why": "one line"}], '
    '"void_steps": [3,4], '
    '"missing": ["requirement ids not yet addressed"], '
    '"next": "one concrete corrective action"}\n\n'
    "void_steps lists ONLY steps whose output must be undone or redone; leave "
    "it empty for a detail_gap. Keep every string under 25 words."
)

JUDGE_SYSTEM = (
    "The working agent has disputed your reading of the request. Judge the "
    "dispute against the REQUEST ITSELF — not against your previous wording, "
    "and not against which side sounds more confident.\n\n"
    "Decide:\n"
    "  main_right — the agent's reading is supported by the request and yours "
    "was not. Say what your spec got wrong.\n"
    "  critic_right — the request supports your reading and the rebuttal does "
    "not engage with it.\n"
    "  unresolved — the request is genuinely ambiguous on this point, or the "
    "rebuttal raises a fact you cannot check. This is the honest answer "
    "whenever the disagreement is about something the request never says.\n\n"
    "Prefer unresolved over a coin flip. An ambiguity in the request is not "
    "something either of you can resolve by arguing.\n\n"
    "Return ONLY a JSON object, no prose:\n"
    '{"verdict": "main_right|critic_right|unresolved", '
    '"reason": "one line, quoting the request where possible", '
    '"spec_fix": {"drop": ["R1"], "add": [{"id": "R9", "text": "...", '
    '"anchor": "verbatim words"}]}, '
    '"user_question": "one question, only when verdict is unresolved"}\n\n'
    "spec_fix applies only to main_right; anchors there follow the same "
    "verbatim rule."
)

# Appended to the agent's system prompt so the three injected blocks read as
# harness-authored rather than as user prose or adversarial tool output. Same
# deliberate omission as critic.HOOK_SECTION: nothing here tells the model to
# hide anything from the user, because that phrasing is indistinguishable from
# prompt injection.
HOOK_SECTION = (
    "<intent_alignment>\n"
    "This harness reviews whether your understanding of the request matches "
    "the request itself, and may insert these blocks into user messages:\n"
    "  <intent_questions> — points the review could not settle from the "
    "request. Answer them from the repository or the web as part of your next "
    "turn; you have the tools for it and the review does not.\n"
    "  <intent_correction> — a mismatch it is confident about, with the steps "
    "it considers void and the files touched since the checkpoint. Reconcile "
    "before continuing; nothing is rolled back for you.\n"
    "  <intent_challenge> — a disputed point. If the reading is wrong, say so "
    "in your next reply and quote the part of the request that supports you; "
    "a rebuttal is expected to change the review's own understanding. If it is "
    "right, say that instead and correct course.\n"
    "The agreed understanding, once settled, appears in <task_understanding> "
    "in this system prompt and outranks any earlier reading of the request, "
    "including your own.\n"
    "</intent_alignment>"
)


# ── Parsing and validation ────────────────────────────────────────────────

def _loads(reply: str) -> Optional[dict]:
    """Parse a JSON object out of a model reply, or None."""
    if not reply or not isinstance(reply, str):
        return None
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _norm(text) -> str:
    """Whitespace-normalised, case-folded text for literal anchor matching."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _line(value, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _str_list(value, limit: int, item_limit: int = 200) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _line(item, item_limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _int_list(value, limit: int = 20) -> list:
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
        if len(out) >= limit:
            break
    return sorted(set(out))


def is_anchored(anchor: str, task: str) -> bool:
    """True when ``anchor`` is a literal quotation from ``task``.

    Whitespace is normalised and case ignored — a model that reflows a quote
    across lines is still quoting. Anything shorter than a few characters is
    rejected: a one-word "anchor" matches almost any request and would let an
    invented requirement in through the one gate that exists to stop it.
    """
    a = _norm(anchor)
    if len(a) < MIN_ANCHOR_CHARS:
        return False
    return a in _norm(task)


def validate_spec(spec, task: str) -> dict:
    """Normalise a raw spec and drop everything not supported by the request.

    This is the only function permitted to produce a spec that later gets
    written into the system prompt, and it is deliberately mechanical: a
    requirement survives if and only if it quotes the request. Dropped ones are
    counted in ``dropped_anchors`` — a rising count is the signal that the
    auxiliary model has started inventing and the feature should be turned down
    or off, so it is logged rather than discarded.
    """
    out = {
        "goal": "", "requirements": [], "out_of_scope": [], "deliverables": [],
        "open_questions": [], "assumptions": [], "task_breakdown": [],
        "dropped_anchors": 0, "round": 0, "spec_version": 1,
    }
    if not isinstance(spec, dict):
        return out

    out["goal"] = _line(spec.get("goal"), 300)

    seen_ids = set()
    for index, item in enumerate(spec.get("requirements") or []):
        if not isinstance(item, dict):
            continue
        text = _line(item.get("text"), 300)
        anchor = _line(item.get("anchor"), 300)
        if not text:
            continue
        if not is_anchored(anchor, task):
            out["dropped_anchors"] += 1
            continue
        req_id = _line(item.get("id"), 8) or f"R{index + 1}"
        if req_id in seen_ids:
            req_id = f"{req_id}_{index + 1}"
        seen_ids.add(req_id)
        out["requirements"].append(
            {"id": req_id, "text": text, "anchor": anchor})
        if len(out["requirements"]) >= MAX_REQUIREMENTS:
            break

    out["out_of_scope"] = _str_list(spec.get("out_of_scope"), 8)
    out["deliverables"] = _str_list(spec.get("deliverables"), 8)
    out["task_breakdown"] = _str_list(spec.get("task_breakdown"), MAX_TASKS)

    for index, item in enumerate(spec.get("open_questions") or []):
        if not isinstance(item, dict):
            continue
        question = _line(item.get("q"), 250)
        if not question:
            continue
        needs = _line(item.get("needs"), 16).lower()
        needs = needs if needs in (NEEDS_USER, NEEDS_EVIDENCE) else NEEDS_USER
        out["open_questions"].append({
            "id": _line(item.get("id"), 8) or f"Q{index + 1}",
            "q": question, "needs": needs,
            "why": _line(item.get("why"), 200),
        })
        if len(out["open_questions"]) >= MAX_QUESTIONS:
            break

    for index, item in enumerate(spec.get("assumptions") or []):
        if not isinstance(item, dict):
            continue
        text = _line(item.get("text"), 250)
        if not text:
            continue
        risk = _line(item.get("risk"), 8).lower()
        out["assumptions"].append({
            "id": _line(item.get("id"), 8) or f"A{index + 1}",
            "text": text, "risk": "high" if risk == "high" else "low",
        })
        if len(out["assumptions"]) >= MAX_QUESTIONS:
            break

    return out


def is_usable(spec) -> bool:
    """A spec worth showing the model: it survived anchoring with content.

    An empty spec is not a neutral one. Injecting "here is the authoritative
    understanding of your task: (nothing)" is worse than injecting nothing, so
    the loop needs a single honest predicate for "did this produce anything".
    """
    if not isinstance(spec, dict):
        return False
    return bool(spec.get("requirements") or spec.get("goal"))


# ── The three model calls ─────────────────────────────────────────────────

def build_self_ask_messages(task: str, prior: Optional[dict] = None,
                            round_index: int = 1) -> list:
    parts = [f"REQUEST (verbatim — anchors must be quoted from this):\n{task}"]
    if prior and is_usable(prior):
        parts.append(SELF_ASK_FOLLOWUP)
        parts.append("SPEC SO FAR:\n" + json.dumps(
            {k: prior.get(k) for k in (
                "goal", "requirements", "out_of_scope", "deliverables",
                "open_questions", "assumptions", "task_breakdown")},
            ensure_ascii=False, indent=1))
    parts.append(f"Round {round_index}. Return the JSON object now.")
    return [{"role": "user", "content": "\n\n".join(parts)}]


def self_ask_round(task: str, prior, llm_fn: Callable[[list], str],
                   round_index: int = 1):
    """One round of self-questioning. Returns ``(spec, failure_reason)``."""
    try:
        if not str(task or "").strip():
            return None, FAIL_EMPTY
        try:
            reply = llm_fn(build_self_ask_messages(task, prior, round_index))
        except Exception:
            return None, FAIL_LLM
        raw = _loads(reply)
        if raw is None:
            return None, FAIL_PARSE
        spec = validate_spec(raw, task)
        spec["round"] = round_index
        return spec, None
    except Exception:
        return None, FAIL_LLM


def build_spec(task: str, llm_fn: Callable[[list], str], rounds: int = 2):
    """Run the self-ask rounds and return ``(spec, failure_reason)``.

    A later round that fails does not throw away an earlier usable spec: two
    good rounds and a third that returns garbage should still leave the loop
    with the two good ones, because the alternative is no alignment at all.
    """
    spec, last_fail = None, None
    for index in range(1, max(1, int(rounds or 1)) + 1):
        candidate, fail = self_ask_round(task, spec, llm_fn, index)
        if candidate is None:
            last_fail = fail
            break
        spec = candidate
    if spec is None:
        return None, last_fail or FAIL_LLM
    return spec, None


def build_compare_messages(spec: dict, actions_summary: str) -> list:
    return [{
        "role": "user",
        "content": (
            f"AGREED UNDERSTANDING OF THE REQUEST:\n{to_contract_text(spec)}\n\n"
            f"WHAT THE AGENT HAS DONE SO FAR (numbered):\n{actions_summary}\n\n"
            "Judge whether it is working on the right problem. Return the JSON "
            "object now."
        ),
    }]


def compare(spec: dict, actions_summary: str, llm_fn: Callable[[list], str]):
    """Compare the spec against the agent's opening moves.

    Returns ``(result, failure_reason)`` where result is
    ``{severity, aligned, divergences, void_steps, missing, next}``.
    """
    try:
        if not is_usable(spec) or not str(actions_summary or "").strip():
            return None, FAIL_EMPTY
        try:
            reply = llm_fn(build_compare_messages(spec, actions_summary))
        except Exception:
            return None, FAIL_LLM
        raw = _loads(reply)
        if raw is None:
            return None, FAIL_PARSE

        severity = _line(raw.get("severity"), 16).lower()
        if severity not in SEVERITIES:
            severity = DETAIL_GAP
        divergences = []
        for item in raw.get("divergences") or []:
            if not isinstance(item, dict):
                continue
            what = _line(item.get("what"), 200)
            if not what:
                continue
            divergences.append({
                "req_id": _line(item.get("req_id"), 8),
                "steps": _int_list(item.get("steps")),
                "what": what,
                "why": _line(item.get("why"), 200),
            })
            if len(divergences) >= 6:
                break
        aligned = raw.get("aligned", severity == DETAIL_GAP and not divergences)
        aligned = bool(aligned) if isinstance(aligned, bool) else \
            str(aligned).lower() not in ("false", "no", "0")
        # A scope error is never "aligned", whatever the model ticked.
        if severity == SCOPE_ERROR:
            aligned = False
        return {
            "severity": severity,
            "aligned": aligned,
            "divergences": divergences,
            # void_steps only mean something for a scope error; a detail gap
            # that voids work is a contradiction, and acting on it would undo
            # correct work over a missing detail.
            "void_steps": (_int_list(raw.get("void_steps"))
                           if severity == SCOPE_ERROR else []),
            "missing": _str_list(raw.get("missing"), 8, 40),
            "next": _line(raw.get("next"), 250),
        }, None
    except Exception:
        return None, FAIL_LLM


def build_judge_messages(task: str, spec: dict, comparison: dict,
                         rebuttal: str) -> list:
    return [{
        "role": "user",
        "content": (
            f"REQUEST (verbatim):\n{task}\n\n"
            f"YOUR READING:\n{to_contract_text(spec)}\n\n"
            f"WHAT YOU FLAGGED:\n{render_flagged(comparison)}\n\n"
            f"THE AGENT'S REBUTTAL:\n{_line(rebuttal, 2000)}\n\n"
            "Judge the dispute. Return the JSON object now."
        ),
    }]


def judge_rebuttal(task: str, spec: dict, comparison: dict, rebuttal: str,
                   llm_fn: Callable[[list], str]):
    """Judge the working model's rebuttal. Returns ``(verdict, failure)``."""
    try:
        if not str(rebuttal or "").strip():
            return None, FAIL_EMPTY
        try:
            reply = llm_fn(build_judge_messages(task, spec, comparison,
                                                rebuttal))
        except Exception:
            return None, FAIL_LLM
        raw = _loads(reply)
        if raw is None:
            return None, FAIL_PARSE
        verdict = _line(raw.get("verdict"), 16).lower()
        if verdict not in ("main_right", "critic_right", "unresolved"):
            verdict = "unresolved"
        fix = raw.get("spec_fix") if isinstance(raw.get("spec_fix"), dict) else {}
        add = []
        for item in (fix.get("add") or []):
            if isinstance(item, dict) and _line(item.get("text")):
                add.append({"id": _line(item.get("id"), 8),
                            "text": _line(item.get("text"), 300),
                            "anchor": _line(item.get("anchor"), 300)})
        question = _line(raw.get("user_question"), 300)
        return {
            "verdict": verdict,
            "reason": _line(raw.get("reason"), 300),
            "drop": _str_list(fix.get("drop"), 8, 8),
            "add": add[:6],
            # Only an unresolved dispute produces a question for the user;
            # a decided one has an answer already and asking would be noise.
            "user_question": question if verdict == "unresolved" else "",
        }, None
    except Exception:
        return None, FAIL_LLM


def apply_resolution(spec: dict, verdict: dict, task: str) -> dict:
    """Fold a ``main_right`` verdict back into the spec.

    The point of losing an argument is to stop making the same objection. A
    verdict that only silenced this round would leave the next assessment
    judging against the reading the working model just disproved.
    """
    if not isinstance(spec, dict):
        return spec
    if not isinstance(verdict, dict) or verdict.get("verdict") != "main_right":
        return spec
    updated = json.loads(json.dumps(spec))       # cheap deep copy of plain data
    drop = set(verdict.get("drop") or [])
    if drop:
        updated["requirements"] = [
            r for r in updated.get("requirements", [])
            if r.get("id") not in drop]
    for item in verdict.get("add") or []:
        anchor = item.get("anchor", "")
        if not is_anchored(anchor, task):
            updated["dropped_anchors"] = int(
                updated.get("dropped_anchors", 0)) + 1
            continue
        req_id = item.get("id") or f"R{len(updated.get('requirements', [])) + 1}"
        updated.setdefault("requirements", []).append(
            {"id": req_id, "text": item.get("text", ""), "anchor": anchor})
    updated["requirements"] = updated.get("requirements", [])[:MAX_REQUIREMENTS]
    updated["spec_version"] = int(updated.get("spec_version", 1)) + 1
    return updated


# ── Rendering ─────────────────────────────────────────────────────────────

def to_contract_text(spec) -> str:
    """The spec as the critic's contract text (``critic.build_messages``).

    Once the two sides agree on a reading, the progress critic should judge
    against THAT, not against the original sentence it was already unable to
    interpret. Reusing the existing contract channel means the agreement
    reaches every later assessment without a second mechanism.
    """
    if not is_usable(spec):
        return ""
    lines = []
    if spec.get("goal"):
        lines.append(f"Goal: {spec['goal']}")
    for req in spec.get("requirements") or []:
        lines.append(f"  {req['id']}. {req['text']}   (\"{req['anchor']}\")")
    for label, key in (("Deliverables", "deliverables"),
                       ("Out of scope", "out_of_scope")):
        values = spec.get(key) or []
        if values:
            lines.append(f"{label}: " + "; ".join(values))
    assumptions = [a["text"] for a in (spec.get("assumptions") or [])
                   if a.get("risk") == "high"]
    if assumptions:
        lines.append("Unconfirmed assumptions: " + "; ".join(assumptions))
    return "\n".join(lines)


def render_understanding(spec) -> str:
    """The authoritative system-prompt section, or "" when there is nothing.

    Marked authoritative because it is quotation, not interpretation: every
    line traces to words the user actually wrote (see ``validate_spec``).
    """
    body = to_contract_text(spec)
    if not body:
        return ""
    version = int((spec or {}).get("spec_version", 1))
    return (f"<task_understanding authoritative=\"true\" version=\"{version}\">\n"
            f"{body}\n"
            "This is the agreed reading of the request. Where it and an earlier "
            "reading disagree, this one governs.\n"
            "</task_understanding>")


def render_questions(spec) -> str:
    """Evidence questions handed to the working model, which has the tools."""
    questions = [q for q in (spec or {}).get("open_questions") or []
                 if q.get("needs") == NEEDS_EVIDENCE]
    if not questions:
        return ""
    lines = [f"- {q['id']}: {q['q']}" + (f" ({q['why']})" if q.get("why") else "")
             for q in questions]
    return ("<intent_questions>\n"
            "The intent review could not settle these from the request alone, "
            "and it has no tools. Answer them from the repository or the web as "
            "part of your next turn.\n"
            + "\n".join(lines) + "\n"
            "</intent_questions>")


def render_flagged(comparison) -> str:
    """The comparison's findings as plain lines (also used in judge prompts)."""
    if not isinstance(comparison, dict):
        return ""
    lines = []
    for d in comparison.get("divergences") or []:
        steps = (" at step" + ("s " if len(d.get("steps") or []) > 1 else " ")
                 + ", ".join(str(s) for s in d["steps"])) if d.get("steps") else ""
        req = f"[{d['req_id']}] " if d.get("req_id") else ""
        why = f" — {d['why']}" if d.get("why") else ""
        lines.append(f"- {req}{d['what']}{steps}{why}")
    if comparison.get("missing"):
        lines.append("- not yet addressed: "
                     + ", ".join(comparison["missing"]))
    return "\n".join(lines)


def _files_line(changed) -> str:
    if not isinstance(changed, dict):
        return ""
    parts = []
    for label, key in (("added", "added"), ("modified", "modified"),
                       ("deleted", "deleted")):
        values = changed.get(key) or []
        if values:
            parts.append(f"{label}: " + ", ".join(values[:15])
                         + (" …" if len(values) > 15 else ""))
    if not parts:
        return ""
    # Said plainly because it is the part that surprises: an undo restores
    # what was changed, never removes what was created.
    return ("Files touched since the checkpoint (nothing has been reverted; "
            "`/undo` would not remove newly created files) — "
            + "; ".join(parts))


def render_correction(comparison, spec, changed=None) -> str:
    """A confident mismatch: what is void, what was touched, what governs now."""
    if not isinstance(comparison, dict):
        return ""
    flagged = render_flagged(comparison) or "- the actions do not match the agreed reading"
    void = comparison.get("void_steps") or []
    void_line = ("Steps considered void: "
                 + ", ".join(str(s) for s in void) + "\n") if void else ""
    files_line = _files_line(changed)
    next_line = comparison.get("next") or "Reconcile with the agreed reading before continuing."
    body = (f"<intent_correction severity=\"{comparison.get('severity', SCOPE_ERROR)}\">\n"
            f"{flagged}\n"
            f"{void_line}")
    if files_line:
        body += files_line + "\n"
    body += (f"Next: {next_line}\n"
             "If you believe this reading of the request is wrong, say so in "
             "your reply and quote the part of the request that supports you.\n"
             "</intent_correction>")
    return body


def render_challenge(comparison, round_index: int = 1) -> str:
    """A disputed point put to the working model, answered in its normal turn."""
    flagged = render_flagged(comparison)
    if not flagged:
        return ""
    return (f"<intent_challenge round=\"{round_index}\">\n"
            f"{flagged}\n"
            "Either correct course, or rebut: quote the part of the request "
            "that supports your reading. A rebuttal is judged against the "
            "request, not against who argues better.\n"
            "</intent_challenge>")


def render_revision(verdict, spec) -> str:
    """Told plainly when the agent wins the argument.

    A prompt section that changes mid-thread produces no signal a model can
    notice — the prefix is simply different next turn. Saying so in the thread
    is what makes losing the argument visible to the side that won it.
    """
    reason = (verdict or {}).get("reason") or ""
    version = int((spec or {}).get("spec_version", 1))
    return ("<intent_revision>\n"
            "Your reading was better supported by the request. The agreed "
            f"reading has been revised (now version {version}) and the updated "
            "<task_understanding> governs from this turn on.\n"
            + (f"What it got wrong: {reason}\n" if reason else "")
            + "</intent_revision>")


def render_unresolved(question: str, *, escalate: bool, assumed: str = "") -> str:
    """The end of an argument neither side can win from the request alone.

    ``escalate`` asks the user. Unattended, there is nobody to ask, so the run
    continues under a named assumption rather than stalling — but the
    assumption is stated, because an unexamined one is how this whole class of
    failure starts.
    """
    text = _line(question, 300)
    if escalate:
        return ("<intent_unresolved>\n"
                "The request does not settle this, and neither reading follows "
                "from it. Stop and ask the user before doing more work on this "
                "point:\n"
                f"  {text or 'Which reading of the request is intended?'}\n"
                "</intent_unresolved>")
    return ("<intent_unresolved>\n"
            "The request does not settle this and there is nobody to ask, so "
            "work continues on an unconfirmed assumption. Say so in your final "
            "report.\n"
            f"  Open question: {text or 'the disputed reading of the request'}\n"
            + (f"  Assuming: {_line(assumed, 300)}\n" if assumed else "")
            + "</intent_unresolved>")


def render_escalation(spec, verdict) -> str:
    """The one question to put to the user when the argument does not settle."""
    question = ""
    if isinstance(verdict, dict):
        question = verdict.get("user_question") or ""
    if not question:
        pending = [q["q"] for q in (spec or {}).get("open_questions") or []
                   if q.get("needs") == NEEDS_USER]
        question = pending[0] if pending else ""
    return question


# ── Phase machine ─────────────────────────────────────────────────────────

def new_state() -> dict:
    """The per-task intent state kept in the loop's ``state`` dict."""
    return {
        "phase": IDLE, "spec": None, "round": 0, "debate_round": 0,
        "fail_streak": 0, "last_comparison": None, "escalated_question": "",
        "questions_sent": False,
    }


def should_start(task: str, *, thread_mode: bool, enabled: bool,
                 has_contract: bool, plan_mode: bool, min_chars: int) -> bool:
    """Whether this task earns an intent pass at all.

    Deliberately conservative. A short instruction has no room to be misread,
    a child agent already has a contract that says exactly this, and plan mode
    is a human doing this job better — spending calls on any of the three buys
    nothing and makes the feature feel like a tax.
    """
    if not (enabled and thread_mode) or has_contract or plan_mode:
        return False
    return len(str(task or "").strip()) >= max(0, int(min_chars or 0))


def next_phase(state: dict, event: str, *, max_debate_rounds: int = 2) -> str:
    """The phase machine. ``event`` is one of:

    ``spec_ready``, ``spec_failed``, ``aligned``, ``diverged``, ``disputed``,
    ``conceded``, ``main_right``, ``critic_right``, ``unresolved``, ``failed``.

    Returns the new phase; the caller stores it. Kept separate from the calls
    so every edge is testable without a model.
    """
    phase = (state or {}).get("phase", IDLE)
    if event == "spec_ready":
        return SPEC_READY
    if event == "spec_failed":
        return DISABLED
    if event == "aligned":
        return ALIGNED
    if event == "diverged":
        return CORRECTING
    if event == "disputed":
        # A dispute past the round budget is not another round of argument.
        if int((state or {}).get("debate_round", 0)) >= max(0, max_debate_rounds):
            return ESCALATED
        return DEBATING
    if event in ("conceded", "critic_right", "main_right"):
        return ALIGNED
    if event == "unresolved":
        if int((state or {}).get("debate_round", 0)) >= max(0, max_debate_rounds):
            return ESCALATED
        return DEBATING
    if event == "failed":
        return DISABLED
    return phase


def is_settled(state) -> bool:
    """True once the intent layer has nothing further to do this task."""
    return (state or {}).get("phase") in TERMINAL_PHASES
