"""Memory extraction (write side of #1) — form a cross-session memory *network*
instead of re-compressing within a single session.

At the end of a completed task, one cheap LLM call reads the conversation and
decides whether anything durable is worth remembering; if so it returns
structured, categorized memories which we dedup and write to the long-term
markdown store (``memory_system``). This replaces the failed "let the main model
decide mid-loop what to save" approach with an explicit, out-of-band judgment.

Categories reuse the existing four memory types:
  user      — stable facts about the user (role, environment, expertise)
  feedback  — corrections / preferences on how to work (the user's "habits")
  project   — goals/constraints/decisions NOT recoverable from code or git
  reference — pointers to external resources (dashboards, tickets, keys' names)

This module holds only pure, testable logic (prompt build, parse, dedup, store).
The agent loop injects an ``llm_fn`` and calls ``extract_and_store`` at turn end.
Everything is best-effort: a failure writes nothing and never disturbs the loop.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

import memory_system

try:
    import embeddings
except Exception:
    embeddings = None


_VALID_TYPES = {"user", "feedback", "project", "structure", "reference"}
_NAME_RE = re.compile(r"[^a-z0-9_-]+")
# At or above this cosine similarity to an existing memory of the SAME type, a
# proposal is merged into that memory (LLM summarises old + new) instead of
# creating a duplicate. Deliberately AGGRESSIVE: at this low threshold most
# same-category facts fold into a single rolling, summarised entry per topic,
# keeping the store (and the injected context) small. Only used when embeddings
# are available. Merges never cross scope — candidates are the visible set only.
_MERGE_COSINE = 0.5
_MAX_WRITE = 6  # never write more than this many memories from one task


SYSTEM_PROMPT = (
    "You extract durable, cross-session memories from a coding-assistant "
    "conversation. Record ONLY facts that will matter in FUTURE sessions. Use "
    "exactly these categories:\n"
    "- user: stable facts about the user (role, OS/environment, expertise, tools)\n"
    "- feedback: corrections or preferences on HOW to work (the user's habits/rules)\n"
    "- project: goals, constraints, or decisions NOT recoverable from code or git\n"
    "- structure: durable architecture/layout facts — the module map, where key "
    "responsibilities live, how components fit — that are NOT obvious from a single file\n"
    "- reference: pointers to external resources (dashboards, tickets, the NAMES "
    "of keys/services — never secret values)\n\n"
    "DO NOT record: transient task state, anything obvious from the code or git "
    "history, one-off trivia, or secrets. Prefer fewer, higher-value memories. "
    "If nothing durable is worth remembering, return an empty array.\n\n"
    "Output ONLY a JSON array, no prose. Each item: "
    '{"type": "user|feedback|project|structure|reference", "name": "short-kebab-slug", '
    '"description": "one line", "body": "the fact", "importance": 0.0-1.0}.'
)


# Used only when a proposal is near-identical to an existing memory: fold the new
# fact into the old one instead of duplicating. Kept self-describing in the user
# message too, so it still works if the caller's llm_fn ignores the system prompt.
MERGE_SYSTEM_PROMPT = (
    "You consolidate two related cross-session memories into one. Keep every "
    "still-valid fact from both, drop duplicates, and let newer information "
    "override stale information. Do not invent anything. Keep the merged body "
    "CONCISE (a few tight bullet-worthy facts, not a transcript). The description "
    "must be a comprehensive one-line summary of the merged body. Output ONLY a "
    'JSON object: {"description": "one-line summary", "body": "the merged facts"}.'
)


def build_messages(conversation_text: str) -> list:
    """Return the OpenAI-format messages for the extraction call."""
    convo = str(conversation_text or "")[:12000]
    return [{
        "role": "user",
        "content": (
            "Conversation to mine for durable memories:\n\n"
            f"{convo}\n\n"
            "Return the JSON array now (or [] if nothing is worth remembering)."
        ),
    }]


def _slugify(name: str) -> str:
    s = _NAME_RE.sub("-", str(name or "").strip().lower()).strip("-")
    return s[:80]


def parse_proposals(reply: str) -> list:
    """Robustly parse the LLM reply into a list of validated proposal dicts.
    Tolerates code fences and surrounding prose; returns [] on anything odd."""
    if not reply or not isinstance(reply, str):
        return []
    text = reply.strip()
    # Strip ```json fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # Isolate the first top-level JSON array.
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        text = text[start:end + 1]
    try:
        raw = json.loads(text)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []

    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mtype = str(item.get("type", "")).strip().lower()
        if mtype not in _VALID_TYPES:
            continue
        name = _slugify(item.get("name", ""))
        desc = str(item.get("description", "")).strip()
        body = str(item.get("body", "")).strip()
        if not name or not body:
            continue
        try:
            importance = float(item.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        out.append({
            "type": mtype, "name": name,
            "description": desc or body[:60], "body": body,
            "importance": importance,
        })
    return out


def _existing_names() -> set:
    try:
        return {e.get("name", "") for e in memory_system.list_memories()}
    except Exception:
        return set()


def _call_llm(llm_fn: Callable, messages: list, system_prompt: str) -> str:
    """Call ``llm_fn`` with a system prompt when it accepts one, else fall back to
    the positional-only form. Merge messages are self-describing, so the fallback
    still produces a usable reply. Returns "" on any failure."""
    try:
        return llm_fn(messages, system_prompt=system_prompt) or ""
    except TypeError:
        try:
            return llm_fn(messages) or ""
        except Exception:
            return ""
    except Exception:
        return ""


def _nearest_existing(proposal: dict, session) -> Optional[dict]:
    """Return the most similar EXISTING memory of the same type in the current
    visible scope as ``{name, meta, body, cosine}``, or None. Only meaningful when
    the embedding endpoint is available; returns None otherwise."""
    if embeddings is None:
        return None
    try:
        existing = memory_system.list_memories(proposal["type"])
        if not existing:
            return None
        cands = []
        for e in existing:
            data = memory_system.read_memory(e["name"])
            if not data:
                continue
            cands.append((e["name"], data["meta"], data["body"],
                          f"{e.get('description','')}\n{data['body']}"))
        if not cands:
            return None
        p_text = f"{proposal['description']}\n{proposal['body']}"
        vecs = embeddings.embed([p_text] + [c[3] for c in cands], session=session)
        if not vecs or len(vecs) != len(cands) + 1:
            return None
        p_vec = vecs[0]
        best = None
        for (name, meta, body, _), cv in zip(cands, vecs[1:]):
            score = embeddings.cosine(p_vec, cv)
            if best is None or score > best["cosine"]:
                best = {"name": name, "meta": meta, "body": body, "cosine": score}
        return best
    except Exception:
        return None


def _parse_merged(reply: str) -> Optional[dict]:
    """Parse the merge LLM reply into {description, body}. Tolerant of fences and
    surrounding prose; returns None on anything odd."""
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
    body = str(obj.get("body", "")).strip()
    if not body:
        return None
    return {"description": str(obj.get("description", "")).strip(), "body": body}


def _merge(proposal: dict, match: dict, llm_fn: Callable, session) -> dict:
    """Fold ``proposal`` into the existing memory ``match``. Returns the merged
    {description, body}. On any failure, falls back to appending the new fact to
    the old body so no information is ever lost."""
    old_desc = match["meta"].get("description", "")
    old_body = match["body"]
    user_msg = {
        "role": "user",
        "content": (
            "Consolidate these two related memories into one. Keep all still-valid "
            "facts, drop duplicates, prefer newer info, and keep the merged body "
            'concise. Output ONLY a JSON object {"description": "...", "body": "..."}.\n\n'
            f"EXISTING description: {old_desc}\nEXISTING body:\n{old_body}\n\n"
            f"NEW description: {proposal['description']}\nNEW body:\n{proposal['body']}"
        ),
    }
    reply = _call_llm(llm_fn, [user_msg], MERGE_SYSTEM_PROMPT)
    merged = _parse_merged(reply)
    if merged:
        if not merged["description"]:
            merged["description"] = old_desc or proposal["description"]
        return merged
    # Fallback: never drop the new fact — append it to the old body.
    if proposal["body"] in old_body:
        combined = old_body
    else:
        combined = f"{old_body}\n\n{proposal['body']}".strip()
    return {"description": old_desc or proposal["description"], "body": combined}


def extract_and_store(conversation_text: str,
                      llm_fn: Callable,
                      *, session=None) -> list:
    """Run the full pipeline: call ``llm_fn(messages) -> reply_text``, parse, then
    for each proposal either MERGE it into the nearest same-type memory (embedding
    cosine ≥ ``_MERGE_COSINE``, LLM-summarised, upsert) or create a fresh entry.

    When embeddings are unavailable it degrades to name-based dedup (skip a
    proposal whose slug already exists). Returns the names written/updated. Never
    raises.

    ``llm_fn`` may accept an optional ``system_prompt`` keyword; if it does not,
    the extraction system prompt is assumed to be already baked in and merges fall
    back to their self-describing user message."""
    try:
        reply = _call_llm(llm_fn, build_messages(conversation_text), SYSTEM_PROMPT)
    except Exception:
        return []
    proposals = parse_proposals(reply)
    if not proposals:
        return []
    proposals = proposals[:_MAX_WRITE]

    taken = _existing_names()          # slugs already on disk (any scope-visible)
    written = []
    for p in proposals:
        try:
            match = _nearest_existing(p, session)
            if match and match["cosine"] >= _MERGE_COSINE:
                merged = _merge(p, match, llm_fn, session)
                try:
                    old_imp = float(match["meta"].get("importance", 0.5) or 0.5)
                except (TypeError, ValueError):
                    old_imp = 0.5
                ok, _ = memory_system.write_memory(
                    match["name"], p["type"], merged["description"], merged["body"],
                    overwrite=True,
                    scope=match["meta"].get("scope"),
                    scope_id=match["meta"].get("scope_id"),
                    importance=max(old_imp, p["importance"]))
                if ok:
                    written.append(match["name"])
                continue

            # No close semantic match (or embeddings unavailable) → create a new
            # entry, but fall back to name-based dedup: skip if the slug already
            # exists on disk or was just written in this batch. This preserves the
            # original conservative behaviour and keeps the offline path safe.
            name = p["name"]
            if name in taken:
                continue
            ok, _ = memory_system.write_memory(
                name, p["type"], p["description"], p["body"],
                overwrite=False, importance=p["importance"])
            if ok:
                taken.add(name)
                written.append(name)
        except Exception:
            continue
    return written
