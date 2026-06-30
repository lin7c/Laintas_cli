"""
Prompt Self-Optimization for laintas_cli — feedback-driven prompt refinement.

Mirrors plan_mode.py's pattern: a state module with a get_prompt_opt_section()
function that returns "" (no-op) or instructions, injected via the {{promptOpt}}
template slot in agent_loop.py.

Workflow (non-blocking — the main agent's task is NOT interrupted):
  1. /prompt feedback "<description>"  → capture feedback, spawn background optimizer
  2. Optimizer sub-agent reads current cli.prop + feedback, drafts a candidate patch
     → writes ~/.laintas/prompts/candidates/<id>.md (status=draft)
  3. Result lands in main agent's inbox → surfaced via {{parallelResults}}
  4. /prompt review    → show diff of candidate vs current cli.prop
  5. /prompt apply      → append <prompt_opt_patch> block to cli.prop (idempotent)
                          — takes effect next loop iteration, NO /reload needed
  6. /prompt discard    → strip the <prompt_opt_patch> block from cli.prop
  7. /prompt export <id>  → portable .md pack (shareable via git/gist)
  8. /prompt install <x>  → import a shared pack
  9. /prompt publish <id> → POST /api/prompts/publish (graceful fallback)

Design decisions:
  - Additive patch layer: NEVER rewrite the base cli.prop template. /prompt apply
    appends a <prompt_opt_patch>...</prompt_opt_patch> block to the END of cli.prop.
    /prompt discard or /reload fully reverts. Safe, reviewable, diff-friendly.
  - cli.prop is read UNCACHED every loop iteration (agent_loop.py:3468), so an
    applied patch takes effect immediately without restart.
  - Canonical storage lives under ~/.laintas/prompts/ (survives /reload, which
    only wipes the 4 per-cwd files in _ALL_CWD_FILES).
  - spawn_subagent() is non-blocking (agent_loop.py:1225) — the main agent keeps
    working; the optimizer's result arrives via inbox → {{parallelResults}}.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paths

CANDIDATES_DIR = paths.PROMPT_CANDIDATES_DIR
FEEDBACK_LOG = paths.PROMPT_FEEDBACK_LOG
STATE_PATH = paths.PROMPT_OPT_STATE

_lock = threading.RLock()

# Active optimization session: {"feedback_id", "candidate_id", "status", "child_agent_id"}
_current_opt: Optional[dict] = None

# The marker block delimiters injected into cli.prop on /prompt apply.
PATCH_OPEN = "<prompt_opt_patch>"
PATCH_CLOSE = "</prompt_opt_patch>"

# Skill patch markers (additive blocks appended to SKILL.md, or string
# replacement in skill.py). Same idempotent-strip pattern as PATCH_*.
SKILL_PATCH_OPEN = "<skill_opt_patch>"
SKILL_PATCH_CLOSE = "</skill_opt_patch>"
SKILL_REPLACE_OLD_OPEN = "<skill_replace_old>"
SKILL_REPLACE_OLD_CLOSE = "</skill_replace_old>"
SKILL_REPLACE_NEW_OPEN = "<skill_replace_new>"
SKILL_REPLACE_NEW_CLOSE = "</skill_replace_new>"

# Structured failure-report categories (v3 template).
FAILURE_CATEGORIES = [
    "Objective unclear",
    "Missing tool-use rule",
    "Missing completion criteria",
    "Weak safety boundary",
    "Bad output format",
    "Too much ambiguity",
    "Model capability limitation",
    "Tool/environment limitation",
]

# Display-only template (NOT injected into the system prompt).
FAILURE_TEMPLATE = """\
Prompt version: v3

Failure case:
用户任务：____
期望行为：____
实际行为：____

Failure category:
[ ] Objective unclear
[ ] Missing tool-use rule
[ ] Missing completion criteria
[ ] Weak safety boundary
[ ] Bad output format
[ ] Too much ambiguity
[ ] Model capability limitation
[ ] Tool/environment limitation

Minimal fix:
____

Regression tests to rerun:
____
"""

# Category → likely fix target. Used by _build_triage_guidance().
_CLI_PROP_CATEGORIES = {
    "Objective unclear", "Missing completion criteria",
    "Too much ambiguity", "Bad output format", "Weak safety boundary",
}
_SKILL_CATEGORIES = {
    "Missing tool-use rule", "Tool/environment limitation",
}


# ── Directory / state helpers ────────────────────────────────────────────

def ensure_prompts_dir() -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    return paths.PROMPTS_DIR


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    ensure_prompts_dir()
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ── Feedback capture ─────────────────────────────────────────────────────

def capture_feedback(description: str, context: Optional[dict] = None) -> dict:
    """Record a user feedback entry and return it.

    Appends to ~/.laintas/prompts/feedback.jsonl (one JSON object per line).
    Also writes a 'feedback'-type memory entry so the agent can see it in
    {{persistentMemory}} (memory_system.py:35 has a 'feedback' type).

    Returns the entry dict (including its 'id').
    """
    ensure_prompts_dir()
    fid = _now_id()
    entry = {
        "id": fid,
        "description": description,
        "context": context or {},
        "created": _now_iso(),
    }
    try:
        with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    # Mirror into memory_system so the main agent sees it in its prompt.
    try:
        import memory_system
        memory_system.write_memory(
            name=f"prompt_opt_feedback_{fid}",
            mem_type="feedback",
            description=f"Prompt optimization feedback: {description[:80]}",
            body=description,
            importance=0.7,
        )
    except Exception:
        pass

    return entry


def get_failure_template() -> str:
    """Return the blank failure-report template for display (/prompt fail)."""
    return FAILURE_TEMPLATE


def capture_structured_failure(fields: dict) -> dict:
    """Capture a structured failure report (v3 template fields).

    fields should contain: task, expected, actual, category, minimal_fix,
    regression_tests.  The entry is stored in feedback.jsonl alongside
    plain-text entries; a 'category' key distinguishes structured entries.

    Returns the entry dict (including its 'id').
    """
    ensure_prompts_dir()
    fid = _now_id()
    task = fields.get("task", "")
    actual = fields.get("actual", "")
    entry = {
        "id": fid,
        "task": task,
        "expected": fields.get("expected", ""),
        "actual": actual,
        "category": fields.get("category", ""),
        "minimal_fix": fields.get("minimal_fix", ""),
        "regression_tests": fields.get("regression_tests", ""),
        # Keep 'description' for backward-compat with list_feedback consumers.
        "description": f"{task} | {actual}" if task or actual else "",
        "context": {"structured": True},
        "created": _now_iso(),
    }
    try:
        with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    try:
        import memory_system
        memory_system.write_memory(
            name=f"prompt_opt_failure_{fid}",
            mem_type="feedback",
            description=f"Failure: {entry['category']} — {task[:60]}",
            body=json.dumps(entry, ensure_ascii=False, indent=2),
            importance=0.8,
        )
    except Exception:
        pass

    return entry


def _format_structured_feedback(entry: dict) -> str:
    """Format a structured feedback entry for the optimizer task string."""
    return (
        f"- Task: {entry.get('task', '')}\n"
        f"- Expected: {entry.get('expected', '')}\n"
        f"- Actual: {entry.get('actual', '')}\n"
        f"- Category: {entry.get('category', '')}\n"
        f"- Minimal fix: {entry.get('minimal_fix', '')}\n"
        f"- Regression tests: {entry.get('regression_tests', '')}\n"
    )


def _build_triage_guidance(category: str) -> str:
    """Build triage guidance text based on the failure category."""
    if not category:
        return (
            "No failure category specified. Consider both cli.prop and skill "
            "issues. Check if any loaded skill's instructions might be causing "
            "the problem before assuming it's a cli.prop issue."
        )
    if category in _CLI_PROP_CATEGORIES:
        return (
            f"Category '{category}' → likely a cli.prop problem.\n"
            "Diagnose which XML section of cli.prop is deficient and draft a "
            "<prompt_opt_patch> block using 'prompt.draft'."
        )
    if category in _SKILL_CATEGORIES:
        return (
            f"Category '{category}' → likely a SKILL problem.\n"
            "1. Use skill.list to enumerate all skills.\n"
            "2. Load and read each relevant skill's SKILL.md (skill.load + fs.read).\n"
            "3. Identify which skill's instructions or tool descriptions caused "
            "the failure.\n"
            "4. Draft a skill patch using 'prompt.skill_patch'.\n"
            "Also check cli.prop in case the skill-routing rules there are deficient."
        )
    if category == "Model capability limitation":
        return (
            f"Category '{category}' → NOT fixable via prompt/skill changes.\n"
            "Write a candidate with rationale explaining the limitation. "
            "Do NOT draft a patch."
        )
    return f"Category '{category}' → unknown. Investigate both cli.prop and skills."


def list_feedback(limit: int = 20) -> list[dict]:
    """Recent feedback entries, newest first."""
    if not FEEDBACK_LOG.exists():
        return []
    entries = []
    try:
        with open(FEEDBACK_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    entries.reverse()
    return entries[:limit]


# ── Background optimizer spawn ───────────────────────────────────────────

def spawn_optimizer(feedback_id: str, parent_agent_id: str, deps,
                    session: Optional[dict] = None) -> Optional[str]:
    """Spawn a non-blocking background sub-agent to draft a candidate patch.

    Uses spawn_subagent() (agent_loop.py:1225) which returns immediately —
    the main agent's loop is NOT blocked. The optimizer's result arrives in
    the parent's inbox and is surfaced via {{parallelResults}} on the next
    iteration.

    If the feedback entry is structured (has a 'category' field), the task
    includes triage logic that routes the diagnosis to cli.prop, a skill,
    or marks it as a model-capability limitation.

    Returns the child agent_id, or None if spawn failed.
    """
    # Lazy import to avoid circular dependency at module load time.
    from agent_loop import spawn_subagent

    # Read feedback entry (may be structured or plain text).
    feedback_entry = None
    for entry in list_feedback(limit=50):
        if entry.get("id") == feedback_id:
            feedback_entry = entry
            break

    if feedback_entry:
        if feedback_entry.get("category"):
            feedback_text = _format_structured_feedback(feedback_entry)
            category = feedback_entry.get("category", "")
        else:
            feedback_text = feedback_entry.get("description", "")
            category = ""
    else:
        feedback_text = f"(feedback id {feedback_id} not found)"
        category = ""

    # Read current cli.prop for reference.
    try:
        with open(paths.project_file(paths.CWD_CLI_PROP), "r",
                  encoding="utf-8") as f:
            current_prop = f.read()
    except OSError:
        current_prop = "(cli.prop not found — will use default template)"

    # Build skill catalog so the optimizer can inspect skills.
    skill_catalog = "(no skills installed)"
    try:
        import skills as skills_mod
        catalog = skills_mod.list_skills()
        if catalog:
            lines = []
            for s in catalog:
                status = "loaded" if s.get("loaded") else "available"
                desc = (s.get("description") or "")[:80]
                lines.append(f"  - {s['name']} [{status}]: {desc}")
            skill_catalog = "\n".join(lines)
    except Exception:
        pass

    triage = _build_triage_guidance(category)

    task = (
        "You are a prompt-optimization sub-agent. Your job is to diagnose the "
        "root cause of a failure and produce a SMALL, ADDITIVE patch.\n\n"
        f"## Failure Report\n{feedback_text}\n\n"
        f"## Triage\n{triage}\n\n"
        f"## Skill Catalog\n{skill_catalog}\n\n"
        f"## Current cli.prop (for reference)\n{current_prop[:4000]}\n\n"
        "## Instructions\n"
        "1. Load the 'prompt-engineering' skill (skill.load) for patch guidelines.\n"
        "2. Apply the triage logic above.\n"
        "3. If cli.prop problem: diagnose the deficient XML section and draft a "
        "patch using 'prompt.draft' (patch, rationale, feedback_id). The patch "
        "must:\n"
        "   - Use XML-style tags consistent with the existing template.\n"
        "   - NOT redefine existing {{var}} slots or duplicate existing sections.\n"
        "   - Be as small as possible while addressing the feedback.\n"
        "   - Not introduce new unrecognized {{...}} placeholders.\n"
        "4. If skill problem: use skill.list and skill.load to inspect skills, "
        "identify the deficient one, then draft a skill patch using "
        "'prompt.skill_patch' (skill_name, skill_file, mode, patch, rationale, "
        "feedback_id).\n"
        "5. If model limitation: use 'prompt.draft' with an empty patch and "
        "rationale explaining the limitation.\n"
        "6. Stop after drafting — do NOT apply. The user will review and apply.\n"
    )

    child_id = spawn_subagent(
        parent_id=parent_agent_id,
        task=task,
        deps=deps,
        name=f"prompt-opt-{feedback_id}",
        session=session,
    )

    if child_id:
        with _lock:
            global _current_opt
            _current_opt = {
                "feedback_id": feedback_id,
                "candidate_id": None,
                "status": "optimizing",
                "child_agent_id": child_id,
                "started": _now_iso(),
            }
            _save_state(_current_opt)
    return child_id


# ── Candidate file management ────────────────────────────────────────────

def _candidate_path(cid: str) -> Path:
    return CANDIDATES_DIR / f"{cid}.md"


def _parse_candidate(raw: str) -> dict:
    """Parse a candidate .md file (frontmatter + body). Returns dict or {}."""
    meta = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            body = parts[2].strip()
    meta["body"] = body
    # Extract the patch block from the body.
    m = re.search(r"<prompt_opt_patch>(.*)</prompt_opt_patch>",
                  body, re.DOTALL)
    meta["patch"] = m.group(1).strip() if m else ""
    return meta


def draft_candidate(feedback_id: str, patch: str, rationale: str) -> dict:
    """Write a candidate file. Called by the 'prompt.draft' tool (optimizer sub-agent).

    Returns the candidate metadata dict.
    """
    ensure_prompts_dir()
    cid = _now_id()
    # Hash the current cli.prop so we can detect drift at apply time.
    try:
        base_content = paths.project_file(paths.CWD_CLI_PROP).read_text(
            encoding="utf-8")
    except OSError:
        base_content = ""
    base_sha = hashlib.sha256(base_content.encode("utf-8")).hexdigest()[:16]

    candidate_raw = (
        f"---\n"
        f"id: {cid}\n"
        f"created: {_now_iso()}\n"
        f"status: draft\n"
        f"feedback: \"{feedback_id}\"\n"
        f"base_prop_sha: {base_sha}\n"
        f"---\n\n"
        f"# Prompt Optimization Candidate\n\n"
        f"## Feedback\n{feedback_id}\n\n"
        f"## Rationale\n{rationale}\n\n"
        f"## Patch\n"
        f"<prompt_opt_patch>\n{patch}\n</prompt_opt_patch>\n"
    )
    _candidate_path(cid).write_text(candidate_raw, encoding="utf-8")

    with _lock:
        global _current_opt
        if _current_opt:
            _current_opt["candidate_id"] = cid
            _current_opt["status"] = "drafted"
            _current_opt["patch"] = patch
            _save_state(_current_opt)
        else:
            _current_opt = {
                "feedback_id": feedback_id,
                "candidate_id": cid,
                "status": "drafted",
                "child_agent_id": None,
                "started": _now_iso(),
                "patch": patch,
            }
            _save_state(_current_opt)

    return {"id": cid, "status": "draft", "feedback": feedback_id,
            "rationale": rationale, "patch": patch}


def list_candidates() -> list[dict]:
    """All candidates, newest first (by mtime)."""
    ensure_prompts_dir()
    out = []
    for f in sorted(CANDIDATES_DIR.glob("*.md"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            raw = f.read_text(encoding="utf-8")
            meta = _parse_candidate(raw)
            entry = {
                "id": meta.get("id", f.stem),
                "status": meta.get("status", "draft"),
                "type": meta.get("type", "cli_prop"),
                "feedback": meta.get("feedback", ""),
                "created": meta.get("created", ""),
                "file": str(f),
            }
            if entry["type"] == "skill_patch":
                entry["skill_name"] = meta.get("skill_name", "")
                entry["skill_file"] = meta.get("skill_file", "")
                entry["mode"] = meta.get("mode", "append")
            out.append(entry)
        except OSError:
            pass
    return out


def read_candidate(cid: Optional[str] = None) -> Optional[dict]:
    """Read a candidate by id. If cid is None, reads the active candidate."""
    if cid is None:
        with _lock:
            cid = (_current_opt or {}).get("candidate_id")
    if not cid:
        return None
    p = _candidate_path(cid)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    meta = _parse_candidate(raw)
    meta["id"] = cid
    return meta


def get_active_candidate_id() -> Optional[str]:
    with _lock:
        return (_current_opt or {}).get("candidate_id")


# ── Apply / discard (cli.prop mutation) ──────────────────────────────────

def _read_live_prop() -> str:
    """Read the current cli.prop, or generate the default if missing."""
    p = paths.project_file(paths.CWD_CLI_PROP)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _strip_existing_patch(content: str) -> str:
    """Remove any existing <prompt_opt_patch>...</prompt_opt_patch> block."""
    return re.sub(
        r"\n*<prompt_opt_patch>.*?</prompt_opt_patch>\s*",
        "\n\n",
        content,
        flags=re.DOTALL,
    ).rstrip() + "\n"


def apply_candidate(cid: Optional[str] = None,
                    force: bool = False) -> tuple[bool, str]:
    """Append the candidate's patch block to cli.prop.

    Idempotent: strips any existing <prompt_opt_patch> block first, then
    appends the new one. Takes effect on the next loop iteration (cli.prop
    is read uncached — agent_loop.py:3468). NO /reload required.

    Returns (ok, message).
    """
    with _lock:
        cand = read_candidate(cid)
        if not cand:
            return False, "No candidate found to apply."
        patch = cand.get("patch", "").strip()
        if not patch:
            return False, "Candidate has no patch block."

        # Drift detection
        base_sha = cand.get("base_prop_sha")
        if base_sha and not force:
            live = _read_live_prop()
            live_sha = hashlib.sha256(live.encode("utf-8")).hexdigest()[:16]
            if live_sha != base_sha:
                return False, (
                    f"cli.prop has changed since this candidate was drafted "
                    f"(base sha {base_sha} → live {live_sha}). "
                    f"Re-run /prompt review or use force=true to override."
                )

        live = _read_live_prop()
        cleaned = _strip_existing_patch(live)
        new_content = cleaned.rstrip("\n") + "\n\n" + \
            f"{PATCH_OPEN}\n{patch}\n{PATCH_CLOSE}\n"

        try:
            paths.project_file(paths.CWD_CLI_PROP).write_text(
                new_content, encoding="utf-8")
        except OSError as e:
            return False, f"Failed to write cli.prop: {e}"

        # Update candidate status
        cid = cand.get("id", "")
        _update_candidate_status(cid, "applied")
        global _current_opt
        if _current_opt:
            _current_opt["status"] = "applied"
            _current_opt["candidate_id"] = cid
            _save_state(_current_opt)
        else:
            _current_opt = {"candidate_id": cid, "status": "applied",
                            "started": _now_iso()}
            _save_state(_current_opt)

        return True, f"Candidate {cid} applied. Patch appended to cli.prop. " \
                     f"Next loop iteration will use the new prompt (no /reload needed)."


def discard_candidate(cid: Optional[str] = None) -> tuple[bool, str]:
    """Strip the <prompt_opt_patch> block from cli.prop. Fully reverts the patch."""
    with _lock:
        live = _read_live_prop()
        if PATCH_OPEN not in live:
            return True, "No patch block found in cli.prop (nothing to discard)."
        cleaned = _strip_existing_patch(live)
        try:
            paths.project_file(paths.CWD_CLI_PROP).write_text(
                cleaned, encoding="utf-8")
        except OSError as e:
            return False, f"Failed to write cli.prop: {e}"

        if cid:
            _update_candidate_status(cid, "discarded")
        global _current_opt
        if _current_opt:
            _current_opt["status"] = "discarded"
            _save_state(_current_opt)

        return True, "Patch discarded. cli.prop reverted to its base template."


def _update_candidate_status(cid: str, status: str) -> None:
    p = _candidate_path(cid)
    if not p.exists():
        return
    try:
        raw = p.read_text(encoding="utf-8")
        raw = re.sub(r"status: \w+", f"status: {status}", raw, count=1)
        p.write_text(raw, encoding="utf-8")
    except OSError:
        pass


# ── Skill patch management ───────────────────────────────────────────────
#
# Skill patches modify a skill's SKILL.md or skill.py file. They use the
# same candidates/ directory but carry type=skill_patch in frontmatter.
# Two modes:
#   append  — append a <skill_opt_patch> block to the file (idempotent, like
#             cli.prop patches). Best for SKILL.md instruction tweaks.
#   replace — find old_string, replace with new_string (exact, unique match).
#             Best for skill.py code fixes or targeted SKILL.md edits.
#
# On apply: original file is backed up to candidates/<cid>.backup.
# On discard: backup is restored and deleted. Hot-reload via skills.reload_all().


def _resolve_skill_file(skill_name: str, skill_file: str) -> Optional[Path]:
    """Resolve the full path to a skill file."""
    try:
        import skills as skills_mod
        meta = skills_mod.get_all_metadata().get(skill_name)
        if meta and meta.dir_path:
            return Path(meta.dir_path) / skill_file
    except Exception:
        pass
    return paths.SKILLS_DIR / skill_name / skill_file


def draft_skill_patch(skill_name: str, skill_file: str, mode: str,
                      patch: str, rationale: str, feedback_id: str,
                      old_string: str = "",
                      new_string: str = "") -> dict:
    """Write a skill patch candidate. Called by the optimizer sub-agent.

    Returns the candidate metadata dict.
    """
    ensure_prompts_dir()
    cid = _now_id()

    skill_path = _resolve_skill_file(skill_name, skill_file)
    if skill_path and skill_path.exists():
        try:
            base_content = skill_path.read_text(encoding="utf-8")
        except OSError:
            base_content = ""
    else:
        base_content = ""
    base_sha = hashlib.sha256(base_content.encode("utf-8")).hexdigest()[:16]

    body_parts = [
        f"# Skill Patch: {skill_name}/{skill_file}\n",
        f"## Rationale\n{rationale}\n",
    ]
    if mode == "append":
        body_parts.append(
            f"## Patch (append mode)\n"
            f"{SKILL_PATCH_OPEN}\n{patch}\n{SKILL_PATCH_CLOSE}\n"
        )
    elif mode == "replace":
        body_parts.append(
            f"## Patch (replace mode)\n"
            f"{SKILL_REPLACE_OLD_OPEN}\n{old_string}\n{SKILL_REPLACE_OLD_CLOSE}\n"
            f"{SKILL_REPLACE_NEW_OPEN}\n{new_string}\n{SKILL_REPLACE_NEW_CLOSE}\n"
        )

    candidate_raw = (
        f"---\n"
        f"id: {cid}\n"
        f"created: {_now_iso()}\n"
        f"status: draft\n"
        f"type: skill_patch\n"
        f"feedback: {feedback_id}\n"
        f"skill_name: {skill_name}\n"
        f"skill_file: {skill_file}\n"
        f"mode: {mode}\n"
        f"base_sha: {base_sha}\n"
        f"---\n\n"
        + "\n".join(body_parts)
    )
    _candidate_path(cid).write_text(candidate_raw, encoding="utf-8")

    return {
        "id": cid,
        "status": "draft",
        "type": "skill_patch",
        "skill_name": skill_name,
        "skill_file": skill_file,
        "mode": mode,
        "feedback": feedback_id,
        "rationale": rationale,
    }


def read_skill_patch(cid: Optional[str] = None) -> Optional[dict]:
    """Read a skill patch candidate by id."""
    if not cid:
        return None
    p = _candidate_path(cid)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    meta = _parse_candidate(raw)
    meta["id"] = cid

    mode = meta.get("mode", "append")
    body = meta.get("body", "")
    if mode == "append":
        m = re.search(
            re.escape(SKILL_PATCH_OPEN) + r"(.*?)" + re.escape(SKILL_PATCH_CLOSE),
            body, re.DOTALL,
        )
        meta["patch"] = m.group(1).strip() if m else ""
    elif mode == "replace":
        m_old = re.search(
            re.escape(SKILL_REPLACE_OLD_OPEN) + r"(.*?)" + re.escape(SKILL_REPLACE_OLD_CLOSE),
            body, re.DOTALL,
        )
        m_new = re.search(
            re.escape(SKILL_REPLACE_NEW_OPEN) + r"(.*?)" + re.escape(SKILL_REPLACE_NEW_CLOSE),
            body, re.DOTALL,
        )
        meta["old_string"] = m_old.group(1).strip() if m_old else ""
        meta["new_string"] = m_new.group(1).strip() if m_new else ""

    return meta


def apply_skill_patch(cid: Optional[str] = None,
                      force: bool = False) -> tuple[bool, str]:
    """Apply a skill patch. Backs up original, modifies file, hot-reloads.

    Returns (ok, message).
    """
    with _lock:
        cand = read_skill_patch(cid)
        if not cand:
            return False, "No skill patch found."
        if cand.get("type") != "skill_patch":
            return False, f"Candidate {cid} is not a skill patch."

        skill_name = cand.get("skill_name", "")
        skill_file = cand.get("skill_file", "")
        mode = cand.get("mode", "append")

        if not skill_name or not skill_file:
            return False, "Candidate missing skill_name or skill_file."

        skill_path = _resolve_skill_file(skill_name, skill_file)
        if not skill_path or not skill_path.exists():
            return False, f"Skill file not found: {skill_name}/{skill_file}"

        try:
            original = skill_path.read_text(encoding="utf-8")
        except OSError as e:
            return False, f"Failed to read skill file: {e}"

        # Drift detection
        base_sha = cand.get("base_sha")
        if base_sha and not force:
            live_sha = hashlib.sha256(
                original.encode("utf-8")).hexdigest()[:16]
            if live_sha != base_sha:
                return False, (
                    f"Skill file has changed since this patch was drafted "
                    f"(base sha {base_sha} -> live {live_sha}). "
                    f"Re-review or use force=true."
                )

        # Compute new content
        if mode == "append":
            patch_content = cand.get("patch", "").strip()
            if not patch_content:
                return False, "Patch has no content."
            cleaned = re.sub(
                r"\n*<skill_opt_patch>.*?</skill_opt_patch>\s*",
                "\n\n",
                original,
                flags=re.DOTALL,
            ).rstrip() + "\n"
            new_content = cleaned.rstrip("\n") + "\n\n" + \
                f"{SKILL_PATCH_OPEN}\n{patch_content}\n{SKILL_PATCH_CLOSE}\n"
        elif mode == "replace":
            old_str = cand.get("old_string", "")
            new_str = cand.get("new_string", "")
            if not old_str:
                return False, "Replace-mode patch has empty old_string."
            if old_str not in original:
                return False, (
                    "old_string not found in skill file. The file may have "
                    "changed, or the old_string doesn't match exactly."
                )
            count = original.count(old_str)
            if count > 1:
                return False, (
                    f"old_string appears {count} times in skill file. "
                    "Make it more specific (include surrounding context)."
                )
            new_content = original.replace(old_str, new_str, 1)
        else:
            return False, f"Unknown patch mode: {mode}"

        # Write backup
        backup_path = _candidate_path(cid).with_suffix(".backup")
        try:
            backup_path.write_text(original, encoding="utf-8")
        except OSError as e:
            return False, f"Failed to write backup: {e}"

        # Write patched file
        try:
            skill_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return False, f"Failed to write skill file: {e}"

        _update_candidate_status(cid, "applied")

        # Hot-reload the skill
        try:
            import skills as skills_mod
            skills_mod.reload_all()
        except Exception:
            pass

        return True, (
            f"Skill patch {cid} applied to {skill_name}/{skill_file}. "
            f"Skill reloaded. Changes take effect immediately."
        )


def discard_skill_patch(cid: Optional[str] = None) -> tuple[bool, str]:
    """Revert a skill patch by restoring from backup.

    Returns (ok, message).
    """
    with _lock:
        if not cid:
            return False, "Skill patch id required."

        cand = read_skill_patch(cid)
        if not cand:
            return False, f"Skill patch {cid} not found."
        if cand.get("type") != "skill_patch":
            return False, f"Candidate {cid} is not a skill patch."

        skill_name = cand.get("skill_name", "")
        skill_file = cand.get("skill_file", "")
        mode = cand.get("mode", "append")

        skill_path = _resolve_skill_file(skill_name, skill_file)
        if not skill_path or not skill_path.exists():
            return False, f"Skill file not found: {skill_name}/{skill_file}"

        backup_path = _candidate_path(cid).with_suffix(".backup")

        if mode == "append" and not backup_path.exists():
            # No backup — try stripping the <skill_opt_patch> block
            try:
                current = skill_path.read_text(encoding="utf-8")
            except OSError as e:
                return False, f"Failed to read skill file: {e}"
            if SKILL_PATCH_OPEN not in current:
                return True, "No patch block found (nothing to discard)."
            cleaned = re.sub(
                r"\n*<skill_opt_patch>.*?</skill_opt_patch>\s*",
                "\n\n",
                current,
                flags=re.DOTALL,
            ).rstrip() + "\n"
            try:
                skill_path.write_text(cleaned, encoding="utf-8")
            except OSError as e:
                return False, f"Failed to write skill file: {e}"
        elif backup_path.exists():
            try:
                original = backup_path.read_text(encoding="utf-8")
                skill_path.write_text(original, encoding="utf-8")
                backup_path.unlink()
            except OSError as e:
                return False, f"Failed to restore from backup: {e}"
        else:
            return False, (
                f"No backup found for patch {cid}. Cannot revert automatically. "
                "The skill file may need manual restoration."
            )

        _update_candidate_status(cid, "discarded")

        try:
            import skills as skills_mod
            skills_mod.reload_all()
        except Exception:
            pass

        return True, (
            f"Skill patch {cid} discarded. {skill_name}/{skill_file} restored. "
            f"Skill reloaded."
        )


def list_skill_patches() -> list[dict]:
    """All skill patch candidates, newest first."""
    ensure_prompts_dir()
    return [c for c in list_candidates()
            if c.get("type") == "skill_patch"]


# ── System prompt injection ({{promptOpt}} slot) ─────────────────────────

def get_prompt_opt_section() -> str:
    """Return the string injected into the {{promptOpt}} system-prompt slot.

    Returns "" when no optimization is active, so the slot is a no-op
    normally (mirrors plan_mode.get_plan_prompt()).
    """
    with _lock:
        opt = dict(_current_opt) if _current_opt else None
    if not opt:
        return ""

    status = opt.get("status", "")
    if status == "optimizing":
        return (
            "\n[PROMPT OPT: OPTIMIZING]\n"
            "A background sub-agent is drafting a prompt-optimization patch based "
            "on user feedback. Your current prompt is UNCHANGED — keep working on "
            "the user's task normally. The candidate will arrive via your inbox "
            "when ready; the user will review and apply it.\n"
        )
    if status == "drafted":
        cid = opt.get("candidate_id", "?")
        return (
            f"\n[PROMPT OPT: CANDIDATE READY]\n"
            f"A prompt-optimization candidate ({cid}) has been drafted and is "
            f"awaiting user review. Your current prompt is still the original. "
            f"Tell the user they can run /prompt review and /prompt apply.\n"
        )
    if status == "applied":
        cid = opt.get("candidate_id", "?")
        return (
            f"\n[PROMPT OPT: PATCH ACTIVE]\n"
            f"A prompt-optimization patch ({cid}) has been applied to your "
            f"cli.prop. Follow the <prompt_opt_patch> block in your system prompt. "
            f"If the patch is counterproductive, tell the user to run /prompt discard.\n"
        )
    return ""


# ── Export / install (portable packs) ────────────────────────────────────

_PACK_KIND = "laintas-prompt-pack"
_PACK_VERSION = "1"


def export_pack(cid: str, out_path: Optional[str] = None) -> tuple[bool, str]:
    """Write a portable .md pack for sharing. Returns (ok, path|err)."""
    cand = read_candidate(cid)
    if not cand:
        return False, f"Candidate {cid} not found."

    # Author from session.json if available.
    author = "unknown"
    try:
        if paths.SESSION_FILE.exists():
            sess = json.loads(paths.SESSION_FILE.read_text(encoding="utf-8"))
            author = sess.get("userName") or sess.get("userEmail") or "unknown"
    except (OSError, json.JSONDecodeError):
        pass

    name = f"prompt-pack-{cid}"
    if not out_path:
        out_path = str(paths.PROMPTS_DIR / f"{name}.md")

    pack = (
        f"---\n"
        f"kind: {_PACK_KIND}\n"
        f"version: {_PACK_VERSION}\n"
        f"name: {name}\n"
        f"description: Prompt optimization patch — see body.\n"
        f"author: {author}\n"
        f"created: {_now_iso()}\n"
        f"base_template: additive\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"## Rationale\n{cand.get('body', '')}\n\n"
        f"## Patch\n"
        f"<prompt_opt_patch>\n{cand.get('patch', '')}\n</prompt_opt_patch>\n"
    )
    try:
        Path(out_path).write_text(pack, encoding="utf-8")
    except OSError as e:
        return False, str(e)
    return True, out_path


def install_pack(path_or_url: str) -> tuple[bool, str, Optional[str]]:
    """Import a shared pack. Returns (ok, message, candidate_id).

    Fetches from URL if the arg looks like a URL, else reads a local file.
    Installs as a 'draft' candidate — user must /prompt apply to activate.
    """
    raw = ""
    if path_or_url.startswith(("http://", "https://")):
        try:
            import urllib.request
            req = urllib.request.Request(path_or_url, headers={"User-Agent": "laintas-cli"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return False, f"Failed to fetch URL: {e}", None
    else:
        try:
            raw = Path(path_or_url).read_text(encoding="utf-8")
        except OSError as e:
            return False, f"Failed to read file: {e}", None

    # Validate frontmatter
    if not raw.startswith("---"):
        return False, "Invalid pack: missing frontmatter.", None
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return False, "Invalid pack: malformed frontmatter.", None
    meta_text = parts[1].strip()
    body = parts[2].strip()
    meta = {}
    for line in meta_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    if meta.get("kind") != _PACK_KIND:
        return False, f"Invalid pack: kind must be '{_PACK_KIND}', got '{meta.get('kind')}'.", None

    m = re.search(r"<prompt_opt_patch>(.*)</prompt_opt_patch>", body, re.DOTALL)
    if not m:
        return False, "Invalid pack: no <prompt_opt_patch> block found.", None
    patch = m.group(1).strip()

    cid = _now_id()
    candidate_raw = (
        f"---\n"
        f"id: {cid}\n"
        f"created: {_now_iso()}\n"
        f"status: draft\n"
        f"feedback: \"installed-pack:{meta.get('name', 'unknown')}\"\n"
        f"base_prop_sha: (imported)\n"
        f"---\n\n"
        f"# Imported Pack: {meta.get('name', cid)}\n\n"
        f"## Source\n{path_or_url}\n\n"
        f"## Author\n{meta.get('author', 'unknown')}\n\n"
        f"## Patch\n"
        f"<prompt_opt_patch>\n{patch}\n</prompt_opt_patch>\n"
    )
    _candidate_path(cid).write_text(candidate_raw, encoding="utf-8")
    return True, f"Pack installed as candidate {cid}. Run /prompt apply {cid} to activate.", cid


# ── Publish (backend-dependent, graceful fallback) ───────────────────────

def publish_pack(cid: str, session: dict) -> tuple[bool, str]:
    """Publish a candidate to the community via POST /api/prompts/publish.

    Requires backend support. If the endpoint is absent (404/network error),
    falls back to saving the pack locally and printing share instructions.
    Returns (ok, message).
    """
    cand = read_candidate(cid)
    if not cand:
        return False, f"Candidate {cid} not found."

    ok, path_or_err = export_pack(cid)
    if not ok:
        return False, f"Failed to export pack: {path_or_err}"
    pack_path = path_or_err

    # Attempt backend publish
    try:
        import urllib.request
        backend_url = os.environ.get("LAINTAS_BACKEND", "")
        if not backend_url:
            return False, (
                f"No backend URL configured (LAINTAS_BACKEND). "
                f"Pack saved locally at {pack_path}. Share it via git/gist."
            )

        # Build auth headers from session
        headers = {"Content-Type": "application/json", "User-Agent": "laintas-cli"}
        token = session.get("token") if session else None
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = json.dumps({
            "candidate_id": cid,
            "name": f"prompt-pack-{cid}",
            "patch": cand.get("patch", ""),
            "rationale": cand.get("body", "")[:500],
            "author": session.get("userName", "unknown") if session else "unknown",
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{backend_url.rstrip('/')}/api/prompts/publish",
            data=payload, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
                share_url = body.get("url") or body.get("shareUrl") or ""
                return True, f"Published. Share URL: {share_url}" if share_url \
                    else f"Published (id: {body.get('id', cid)})."
            return False, f"Backend returned status {resp.status}."
    except Exception as e:
        return False, (
            f"Backend publish failed ({e}). Pack saved locally at {pack_path}. "
            f"Share it via git/gist, or ask the backend admin to enable "
            f"/api/prompts/publish."
        )


# ── Init: restore state from disk ────────────────────────────────────────

def _restore_state() -> None:
    global _current_opt
    state = _load_state()
    if state:
        _current_opt = state


_restore_state()
