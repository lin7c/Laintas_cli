---
name: file-editing
description: Practical editing workflow for modifying existing files safely.
version: 1.0.0
---

# File Editing

Use this workflow when changing existing files:

- Locate first: use `fs.grep`, `fs.glob`, `fs.ls`, or shell `rg` to find the target.
- Read before editing. Patch only from text you have seen in this turn or recent context.
- Prefer `fs.edit` or `fs.multi_edit` with small exact anchors. Avoid whole-file rewrites unless the file is new or intentionally regenerated.
- If an edit match fails, re-read the region and choose a smaller exact anchor. Do not switch to a large `fs.write` fallback.
- After meaningful edits, read the changed region or run `fs.diff`.
- Run the nearest obvious verification: tests, typecheck, lint, build, or a targeted command.
- Do not bundle unrelated cleanup into a bug fix.
