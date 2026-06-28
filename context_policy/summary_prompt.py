"""Canonical compaction summary prompt — shared across agent products.

Faithful port of opencode's running-summary prompt
(``packages/core/src/session/compaction.ts``: ``SUMMARY_TEMPLATE`` + ``buildPrompt``).
When the conversation overflows the model window, each product summarizes the
older "head" of the conversation into this fixed Markdown structure and carries
it forward (incrementally merging the previous summary). Centralizing the prompt
keeps the summary shape identical across laintas_cli and Helpwo.

Stdlib-only, vendorable. CN/EN variants; default CN (gateway default).
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
- Do not mention the summary process or that context was compacted."""

_TEMPLATE_CN = """严格按 <template> 内的 Markdown 结构输出，章节顺序不变。回复中不要包含 <template> 标签。
<template>
## Goal
- [一句话任务概述]

## Constraints & Preferences
- [用户约束、偏好、规格，或 "(none)"]

## Progress
### Done
- [已完成的工作，或 "(none)"]

### In Progress
- [进行中的工作，或 "(none)"]

### Blocked
- [阻塞项，或 "(none)"]

## Key Decisions
- [决策及原因，或 "(none)"]

## Next Steps
- [有序的后续动作，或 "(none)"]

## Critical Context
- [重要技术事实、报错、未决问题，或 "(none)"]

## Relevant Files
- [文件或目录路径：为何重要，或 "(none)"]
</template>

规则：
- 每个章节都保留，即使为空。
- 用简洁要点，不要大段叙述。
- 已知时逐字保留文件路径、命令、报错串、标识符。
- 不要提及摘要过程或"上下文被压缩"。"""

_PREAMBLE_NEW_EN = "Create a new anchored summary from the conversation history."
_PREAMBLE_UPDATE_EN = (
    "Update the anchored summary below using the conversation history above.\n"
    "Preserve still-true details, remove stale details, and merge in the new facts.\n"
    "<previous-summary>\n{prev}\n</previous-summary>"
)
_PREAMBLE_NEW_CN = "根据上面的对话历史，新建一份锚定摘要。"
_PREAMBLE_UPDATE_CN = (
    "用上面的对话历史更新下面这份锚定摘要。\n"
    "保留仍然成立的细节，删除已过时的细节，并合并新的事实。\n"
    "<previous-summary>\n{prev}\n</previous-summary>"
)


def summary_prompt(lang: str = "CN", previous_summary: Optional[str] = None) -> str:
    """Return the compaction summary instruction for ``lang`` ('CN'/'EN').

    When ``previous_summary`` is given, the model is told to UPDATE it (incremental
    running summary); otherwise to CREATE a fresh one. The serialized conversation
    "head" is appended by the caller after this instruction.
    """
    en = (lang or "").upper() == "EN"
    template = _TEMPLATE_EN if en else _TEMPLATE_CN
    if previous_summary and previous_summary.strip():
        pre = (_PREAMBLE_UPDATE_EN if en else _PREAMBLE_UPDATE_CN).format(prev=previous_summary.strip())
    else:
        pre = _PREAMBLE_NEW_EN if en else _PREAMBLE_NEW_CN
    return f"{pre}\n\n{template}"
