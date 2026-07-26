"""Canonical compaction summary prompt — shared across agent products.

Faithful port of opencode's running-summary prompt
(``packages/core/src/session/compaction.ts``: ``SUMMARY_TEMPLATE`` + ``buildPrompt``).
When the conversation overflows the model window, each product summarizes the
older "head" of the conversation into this fixed Markdown structure and carries
it forward (incrementally merging the previous summary). Centralizing the prompt
keeps the summary shape identical across laintas_cli and Helpwo.

Stdlib-only, vendorable. EN prompt for all language modes.
"""
from __future__ import annotations

from typing import Optional

# The fixed output structure both products must produce (opencode SUMMARY_TEMPLATE).
_TEMPLATE_EN = """Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Durable User Rules
- [only explicit recurring/cross-session rules, preserving rule ids and exact scope; never infer one from a keyword, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers when known.
- Preserve active durable-rule ids and their exact obligation. A conversation summary cannot cancel or supersede a durable rule.
- Do not mention the summary process or that context was compacted.
- If present, incrementally merge the previous summary; never throw away still-relevant facts.
- Mark the beginning of each compaction boundary with a compact <compaction> tag: \"## Compaction Epoch N\" where N is the compaction round, so the LLM can reason about older vs. newer context."""

_PREAMBLE_NEW_EN = "Create a new anchored summary from the conversation history."
_PREAMBLE_UPDATE_EN = (
    "Update the anchored summary below using the conversation history above.\n"
    "Preserve still-true details, remove stale details, and merge in the new facts.\n"
    "<previous-summary>\n{prev}\n</previous-summary>"
)


def summary_prompt(lang: str = "EN", previous_summary: Optional[str] = None) -> str:
    """Return the English compaction summary instruction.

    When ``previous_summary`` is given, the model is told to UPDATE it (incremental
    running summary); otherwise to CREATE a fresh one. The serialized conversation
    "head" is appended by the caller after this instruction. ``lang`` remains in
    the signature for compatibility with existing callers but does not alter the
    prompt language.
    """
    if previous_summary and previous_summary.strip():
        pre = _PREAMBLE_UPDATE_EN.format(prev=previous_summary.strip())
    else:
        pre = _PREAMBLE_NEW_EN
    return f"{pre}\n\n{_TEMPLATE_EN}"
