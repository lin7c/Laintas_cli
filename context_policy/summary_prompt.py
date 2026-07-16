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
- Do not mention the summary process or that context was compacted."""

_TEMPLATE_CN = """严格按照 <template> 内的 Markdown 结构输出并保持章节顺序，不要输出 <template> 标签。
<template>
## 当前目标
- [一句话概括当前任务]

## 约束与偏好
- [仅限当前任务的约束、偏好和规格，或“（无）”]

## 长期用户规则
- [只记录明确建立的跨轮/重复规则；保留规则 ID、原始义务、作用域和状态，不得根据关键词推断，或“（无）”]

## 进度
### 已完成
- [已完成工作，或“（无）”]

### 进行中
- [当前工作，或“（无）”]

### 阻塞
- [阻塞原因，或“（无）”]

## 关键决策
- [决策及原因，或“（无）”]

## 下一步
- [按顺序列出下一步，或“（无）”]

## 关键上下文
- [重要技术事实、错误、待确认问题，或“（无）”]

## 相关文件
- [文件或目录路径：作用，或“（无）”]
</template>

规则：
- 即使没有内容也必须保留所有章节。
- 使用简洁条目，不写长段落。
- 已知路径、命令、错误文本、标识符和长期规则 ID 必须保持原文。
- 摘要不能取消或覆盖长期规则；“每次、仅当、除非、不得、本次、此后、直到取消”等限定范围必须保持准确。
- 不要提及摘要过程或上下文压缩。"""

_PREAMBLE_NEW_EN = "Create a new anchored summary from the conversation history."
_PREAMBLE_UPDATE_EN = (
    "Update the anchored summary below using the conversation history above.\n"
    "Preserve still-true details, remove stale details, and merge in the new facts.\n"
    "<previous-summary>\n{prev}\n</previous-summary>"
)
_PREAMBLE_NEW_CN = "根据会话历史创建一份有明确锚点的新摘要。"
_PREAMBLE_UPDATE_CN = (
    "使用上方会话历史更新下面的既有摘要。\n"
    "保留仍然有效的内容，移除已明确失效的任务局部信息，并合并新事实。\n"
    "不得自行删除、改写或推断长期用户规则。\n"
    "<previous-summary>\n{prev}\n</previous-summary>"
)


def summary_prompt(lang: str = "CN", previous_summary: Optional[str] = None) -> str:
    """Return the compaction summary instruction for ``lang`` ('CN'/'EN').

    When ``previous_summary`` is given, the model is told to UPDATE it (incremental
    running summary); otherwise to CREATE a fresh one. The serialized conversation
    "head" is appended by the caller after this instruction.
    """
    chinese = (lang or "").upper() in {"CN", "ZH"}
    template = _TEMPLATE_CN if chinese else _TEMPLATE_EN
    if previous_summary and previous_summary.strip():
        preamble = _PREAMBLE_UPDATE_CN if chinese else _PREAMBLE_UPDATE_EN
        pre = preamble.format(prev=previous_summary.strip())
    else:
        pre = _PREAMBLE_NEW_CN if chinese else _PREAMBLE_NEW_EN
    return f"{pre}\n\n{template}"
