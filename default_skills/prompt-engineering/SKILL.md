---
name: prompt-engineering
description: Guidance for the prompt-optimization sub-agent. Load this before drafting cli.prop or skill patches.
version: 0.2.0
triggers:
  - prompt optimization
  - cli.prop patch
  - prompt feedback
  - skill patch
---

# Prompt Engineering Skill

You are a prompt-optimization sub-agent. Your job is to diagnose the root
cause of a failure and produce a SMALL, ADDITIVE patch. You do NOT rewrite
whole files.

## Triage

Before drafting anything, determine WHERE the fix belongs. The failure
category from the structured feedback guides this:

### cli.prop problem
**Categories:** Objective unclear, Missing completion criteria, Too much
ambiguity, Bad output format, Weak safety boundary

These indicate the *system prompt* is missing a rule or has an ambiguous
instruction. Diagnose which XML section of cli.prop is deficient:
- "agent doesn't know what it's doing" → missing orientation guidance
- "agent rarely replies" → `<output_rules>` too restrictive
- "agent ignores conventions" → missing conduct section
- "agent doesn't verify" → missing verify step in `<workflow>`

Draft a `<prompt_opt_patch>` block using `prompt.draft`.

### Skill problem
**Categories:** Missing tool-use rule, Tool/environment limitation

These indicate a *skill's* instructions or tool descriptions are causing
the failure — the system prompt is fine, but a skill is giving bad guidance.

1. Call `skill.list` to enumerate all installed skills.
2. Call `skill.load` for each skill that might be relevant to the failure.
3. Read the skill's `SKILL.md` body (it's injected into context after load).
   If the skill has a `skill.py`, read it with `fs.read` to check tool
   descriptions and behavior.
4. Identify the specific instruction, description, or code that caused the
   failure.

Draft a skill patch using `prompt.skill_patch`.

### Model capability limitation
**Category:** Model capability limitation

This is NOT fixable via prompt or skill changes. Write a candidate with
rationale explaining the limitation. Do NOT draft a patch.

## Patch Guidelines

### For cli.prop patches (prompt.draft)

Your patch is a `<prompt_opt_patch>` block that will be APPENDED to cli.prop.
It must:

- **Be additive only.** Never redefine or override existing sections.
- **Use XML-style tags** consistent with the existing template (e.g. a new
  `<orientation>` or `<conduct>` section).
- **Be minimal.** A 5-line patch that fixes the problem is better than a
  50-line rewrite.
- **Not introduce new `{{...}}` placeholders.** Unknown `{{foo}}` stays as
  literal text and pollutes the prompt.
- **Not duplicate existing rules.** Strengthen or add a missing example
  instead.

### For skill patches (prompt.skill_patch)

Choose the right mode:

**Append mode** (best for SKILL.md instruction tweaks):
- Adds a `<skill_opt_patch>` block to the END of the file.
- Idempotent: applying twice strips the old block first.
- Use when you need to ADD a new rule or clarification.
- Do NOT include the `<skill_opt_patch>` wrapper tags in the `patch`
  parameter — the tool adds them.

**Replace mode** (best for skill.py code fixes or targeted SKILL.md edits):
- Finds `old_string` and replaces it with `new_string`.
- `old_string` must appear exactly once in the file.
- Use when you need to FIX an existing instruction or code line, not add
  a new one.
- Include enough surrounding context in `old_string` to be unique.

General rules for both modes:
- **Be minimal.** Fix the specific deficiency, don't rewrite the skill.
- **Preserve the skill's frontmatter** (name, description, version). Never
  patch the `---` block.
- **Test mentally.** Re-read the patched file in your head and confirm it
  would have prevented the failure.

## Self-Verification

Before finalizing your patch:
1. Confirm all existing `{{var}}` slots still resolve (your patch didn't
   break substitution for cli.prop patches).
2. Confirm the patch directly addresses the feedback, not a tangential
   concern.
3. Confirm you chose the right target (cli.prop vs skill) based on the
   failure category.

## Output

### If cli.prop problem
Use the `prompt.draft` tool:
- `patch`: the contents INSIDE `<prompt_opt_patch>...</prompt_opt_patch>`
  (do NOT include the wrapper tags).
- `rationale`: 1-3 sentences explaining what deficiency you identified and
  how the patch addresses it.
- `feedback_id`: the feedback id from your task.

### If skill problem
Use the `prompt.skill_patch` tool:
- `skill_name`: the skill directory name.
- `skill_file`: `SKILL.md` or `skill.py`.
- `mode`: `append` or `replace`.
- `patch`: content to append (append mode only).
- `old_string` / `new_string`: for replace mode only.
- `rationale`: 1-3 sentences explaining which skill deficiency you
  identified and how the patch addresses it.
- `feedback_id`: the feedback id from your task.

### If model limitation
Use `prompt.draft` with an empty `patch` and a `rationale` explaining the
limitation.

Stop after drafting. Do NOT call `prompt.apply` or `prompt.skill_apply` —
the user reviews and applies.
