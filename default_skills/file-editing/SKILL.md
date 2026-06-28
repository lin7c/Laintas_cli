---
name: file-editing
description: Practical editing workflow for modifying existing files safely.
version: 1.0.0
---

# File Editing

The core read → edit (exact anchor) → verify workflow is always in effect — see
the `<core_tool_usage>` block in your system prompt. This skill only adds the
laintas-specific habits on top:

- Locate with `grep`/`glob`/`ls`, or shell `rg` when a richer search helps.
- Run the nearest obvious verification after a change: tests, typecheck, lint,
  build, or a targeted command — not just a re-read.
- Do not bundle unrelated cleanup into a bug fix; keep the diff to the task.
- For large generated files, follow the chunked-write rule from the core guide.
