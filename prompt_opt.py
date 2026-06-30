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

    Returns the child agent_id, or None if spawn failed.
    """
    # Lazy import to avoid circular dependency at module load time.
    from agent_loop import spawn_subagent

    feedback_desc = ""
    for entry in list_feedback(limit=50):
        if entry.get("id") == feedback_id:
            feedback_desc = entry.get("description", "")
            break

    try:
        with open(paths.project_file(paths.CWD_CLI_PROP), "r",
                  encoding="utf-8") as f:
            current_prop = f.read()
    except OSError:
        current_prop = "(cli.prop not found — will use default template)"

    task = (
        "You are a prompt-optimization sub-agent. Your job is to produce a "
        "SMALL, ADDITIVE patch to the laintas-cli system prompt (cli.prop) "
        "that addresses the user's feedback. Do NOT rewrite the whole template.\n\n"
        f"## User Feedback\n{feedback_desc}\n\n"
        f"## Current cli.prop (for reference)\n{current_prop[:4000]}\n\n"
        "## Instructions\n"
        "1. Load the 'prompt-engineering' skill (skill_load) for patch guidelines.\n"
        "2. Diagnose which section of cli.prop is deficient.\n"
        "3. Draft a <prompt_opt_patch> block — a self-contained XML section that "
        "will be APPENDED to cli.prop. It must:\n"
        "   - Use XML-style tags consistent with the existing template.\n"
        "   - NOT redefine existing {{var}} slots or duplicate existing sections.\n"
        "   - Be as small as possible while addressing the feedback.\n"
        "   - Not introduce new unrecognized {{...}} placeholders.\n"
        "4. Write the candidate using the 'prompt.draft' tool with: patch (the "
        "block contents, WITHOUT the <prompt_opt_patch> wrapper), rationale, "
        "and feedback_id.\n"
        "5. Stop after drafting — do NOT apply it. The user will review and apply.\n"
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
            out.append({
                "id": meta.get("id", f.stem),
                "status": meta.get("status", "draft"),
                "feedback": meta.get("feedback", ""),
                "created": meta.get("created", ""),
                "file": str(f),
            })
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
