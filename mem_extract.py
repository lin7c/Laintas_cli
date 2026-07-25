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


_VALID_TYPES = {"user", "feedback", "project", "reference"}
_NAME_RE = re.compile(r"[^a-z0-9_-]+")
# Above this cosine similarity to an existing memory we treat a proposal as a
# duplicate and skip it (only when embeddings are available).
_DUP_COSINE = 0.92
_MAX_WRITE = 6  # never write more than this many memories from one task


SYSTEM_PROMPT = (
    "You extract durable, cross-session memories from a coding-assistant "
    "conversation. Record ONLY facts that will matter in FUTURE sessions:\n"
    "- user: stable facts about the user (role, OS/environment, expertise, tools)\n"
    "- feedback: corrections or preferences on HOW to work (the user's habits/rules)\n"
    "- project: goals, constraints, or decisions NOT recoverable from code or git\n"
    "- reference: pointers to external resources (dashboards, tickets, the NAMES "
    "of keys/services — never secret values)\n\n"
    "DO NOT record: transient task state, anything obvious from the code or git "
    "history, one-off trivia, or secrets. Prefer fewer, higher-value memories. "
    "If nothing durable is worth remembering, return an empty array.\n\n"
    "Output ONLY a JSON array, no prose. Each item: "
    '{"type": "user|feedback|project|reference", "name": "short-kebab-slug", '
    '"description": "one line", "body": "the fact", "importance": 0.0-1.0}.'
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


def _semantic_duplicate(proposal: dict, session) -> bool:
    """True if the proposal is near-identical to an existing memory (only checked
    when the embedding endpoint is available; otherwise returns False)."""
    if embeddings is None:
        return False
    try:
        existing = memory_system.list_memories(proposal["type"])
        if not existing:
            return False
        cand_texts = []
        cand_names = []
        for e in existing:
            data = memory_system.read_memory(e["name"])
            if not data:
                continue
            cand_texts.append(f"{e.get('description','')}\n{data['body']}")
            cand_names.append(e["name"])
        if not cand_texts:
            return False
        p_text = f"{proposal['description']}\n{proposal['body']}"
        vecs = embeddings.embed([p_text] + cand_texts, session=session)
        if not vecs or len(vecs) != len(cand_texts) + 1:
            return False
        p_vec = vecs[0]
        for cv in vecs[1:]:
            if embeddings.cosine(p_vec, cv) >= _DUP_COSINE:
                return True
    except Exception:
        return False
    return False


def dedup(proposals: list, session=None) -> list:
    """Drop proposals whose name already exists, whose name collides within this
    batch, or that are semantically near-identical to an existing memory."""
    existing = _existing_names()
    seen_names = set()
    kept = []
    for p in proposals:
        name = p["name"]
        if name in existing or name in seen_names:
            continue
        if _semantic_duplicate(p, session):
            continue
        seen_names.add(name)
        kept.append(p)
    return kept


def extract_and_store(conversation_text: str,
                      llm_fn: Callable[[list], str],
                      *, session=None) -> list:
    """Run the full pipeline: call ``llm_fn(messages) -> reply_text``, parse,
    dedup, and write via ``memory_system.write_memory`` (overwrite=False, so an
    existing curated memory is never clobbered). Returns the names written.
    Never raises."""
    try:
        reply = llm_fn(build_messages(conversation_text))
    except Exception:
        return []
    proposals = parse_proposals(reply)
    if not proposals:
        return []
    proposals = dedup(proposals, session=session)[:_MAX_WRITE]

    written = []
    for p in proposals:
        try:
            ok, _ = memory_system.write_memory(
                p["name"], p["type"], p["description"], p["body"],
                overwrite=False, importance=p["importance"])
            if ok:
                written.append(p["name"])
        except Exception:
            continue
    return written
