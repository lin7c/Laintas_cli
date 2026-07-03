"""Project-scoped Prompt Lab for assisted prompt diagnosis and testing.

Prompt Lab deliberately keeps the generated ``cli.prop`` base immutable.
Approved patches are stored as structured JSON overlays and compiled into the
``{{promptOpt}}`` slot on every agent-loop iteration.  This makes activation,
profile switching, and rollback immediate without a process restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import paths


_LOCK = threading.RLock()
_SCOPE = threading.local()
_MAX_CONTEXT_CHARS = 48_000
_MAX_PATCH_CHARS = 20_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*|"
    r"token\s*[:=]\s*|password\s*[:=]\s*)([^\s,;\"']+)"
)


def _root() -> Path:
    override = getattr(_SCOPE, "root", None)
    return Path(override) if override else paths.project_dir() / "prompt-lab"


def project_root() -> Path:
    """Return the bound Prompt Lab root for passing into background workers."""
    return _root().resolve(strict=False)


@contextmanager
def project_scope(root: Optional[str | Path]):
    """Bind storage to the originating project for one background operation."""
    previous = getattr(_SCOPE, "root", None)
    if root:
        _SCOPE.root = str(Path(root).resolve(strict=False))
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_SCOPE, "root")
            except AttributeError:
                pass
        else:
            _SCOPE.root = previous


def _branches_dir() -> Path:
    return _root() / "branches"


def _patches_dir() -> Path:
    return _root() / "patches"


def _profiles_dir() -> Path:
    return _root() / "profiles"


def _state_path() -> Path:
    return _root() / "state.json"


def _history_path() -> Path:
    return _root() / "activation-history.jsonl"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _ensure() -> None:
    for directory in (_branches_dir(), _patches_dir(), _profiles_dir()):
        directory.mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _safe_path(directory: Path, item_id: str) -> Optional[Path]:
    if not _SAFE_ID.fullmatch(item_id or ""):
        return None
    return directory / f"{item_id}.json"


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub(lambda m: m.group(1) + "[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in {"token", "password", "authorization", "cookie", "cookies"}:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact(item)
        return result
    return value


def _bounded(value: Any, limit: int = _MAX_CONTEXT_CHARS) -> Any:
    """Bound persisted context while preserving valid JSON structure."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return value
    if isinstance(value, list):
        kept = []
        used = 0
        for item in reversed(value):
            text = json.dumps(item, ensure_ascii=False, default=str)
            if kept and used + len(text) > limit:
                break
            kept.append(item)
            used += len(text)
        kept.reverse()
        return kept
    return raw[-limit:]


def _load_state() -> dict:
    state = _read_json(_state_path(), {})
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    _atomic_json(_state_path(), state)


def _profile_path(name: str) -> Optional[Path]:
    return _safe_path(_profiles_dir(), name)


def _load_profile(name: str) -> Optional[dict]:
    path = _profile_path(name)
    if path is None or not path.exists():
        return None
    value = _read_json(path, None)
    return value if isinstance(value, dict) else None


def _default_profile() -> dict:
    return {
        "name": "default",
        "patches": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def ensure_default_profile() -> dict:
    with _LOCK:
        _ensure()
        profile = _load_profile("default")
        if profile is None:
            profile = _default_profile()
            _atomic_json(_profiles_dir() / "default.json", profile)
        state = _load_state()
        if not state.get("active_profile"):
            state["active_profile"] = "default"
            state.setdefault("active_branch", None)
            _save_state(state)
        return profile


def read_branch(branch_id: Optional[str] = None) -> Optional[dict]:
    with _LOCK:
        if not branch_id:
            branch_id = _load_state().get("active_branch")
        path = _safe_path(_branches_dir(), branch_id or "")
        if path is None:
            return None
        value = _read_json(path, None)
        return value if isinstance(value, dict) else None


def set_active_branch(branch_id: str) -> tuple[bool, str]:
    with _LOCK:
        if read_branch(branch_id) is None:
            return False, f"Prompt Lab branch {branch_id} not found."
        state = _load_state()
        state["active_branch"] = branch_id
        state.setdefault("active_profile", "default")
        _save_state(state)
        return True, f"Prompt Lab branch {branch_id} selected."


def active_patch_id() -> Optional[str]:
    branch = read_branch()
    return str(branch.get("candidate_patch_id")) if branch and branch.get("candidate_patch_id") else None


def read_patch(patch_id: str) -> Optional[dict]:
    with _LOCK:
        path = _safe_path(_patches_dir(), patch_id)
        if path is None:
            return None
        value = _read_json(path, None)
        return value if isinstance(value, dict) else None


def list_branches() -> list[dict]:
    _ensure()
    result = []
    for path in sorted(_branches_dir().glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        item = _read_json(path, None)
        if isinstance(item, dict):
            result.append(item)
    return result


def list_patches() -> list[dict]:
    _ensure()
    result = []
    for path in sorted(_patches_dir().glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        item = _read_json(path, None)
        if isinstance(item, dict):
            result.append(item)
    return result


def list_profiles() -> list[dict]:
    ensure_default_profile()
    result = []
    active = _load_state().get("active_profile", "default")
    for path in sorted(_profiles_dir().glob("*.json")):
        item = _read_json(path, None)
        if isinstance(item, dict):
            item = dict(item)
            item["active"] = item.get("name") == active
            result.append(item)
    return result


def _event_tail(limit: int = 80) -> list[dict]:
    path = paths.project_dir() / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def capture_incident(description: str, chat_history: Optional[list] = None,
                     agent_state: Optional[dict] = None,
                     effective_prompt: str = "") -> dict:
    """Create an immutable problem snapshot and a recoverable lab branch."""
    with _LOCK:
        _ensure()
        branch_id = _new_id("branch")
        clean_prompt = _redact(effective_prompt or "")
        snapshot = {
            "description": description.strip() or "Review the latest AI behavior",
            "cwd": str(Path.cwd()),
            "conversation": _bounded(_redact(list(chat_history or []))),
            "events": _bounded(_redact(_event_tail())),
            "agent_state": _bounded(_redact(dict(agent_state or {})), 12_000),
            "effective_prompt": clean_prompt[:_MAX_CONTEXT_CHARS],
            "effective_prompt_sha256": hashlib.sha256(
                (effective_prompt or "").encode("utf-8")).hexdigest(),
            "captured_at": _now(),
        }
        branch = {
            "id": branch_id,
            "status": "CAPTURED",
            "description": snapshot["description"],
            "snapshot": snapshot,
            "candidate_patch_id": None,
            "worker_agent_id": None,
            "test_agent_id": None,
            "notes": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        _atomic_json(_branches_dir() / f"{branch_id}.json", branch)
        state = _load_state()
        state["active_branch"] = branch_id
        state.setdefault("active_profile", "default")
        _save_state(state)
        return branch


def update_branch(branch_id: str, **updates: Any) -> Optional[dict]:
    with _LOCK:
        branch = read_branch(branch_id)
        if branch is None:
            return None
        branch.update(updates)
        branch["updated_at"] = _now()
        path = _safe_path(_branches_dir(), branch_id)
        assert path is not None
        _atomic_json(path, branch)
        return branch


def add_branch_note(branch_id: str, note: str, kind: str = "diagnosis") -> Optional[dict]:
    with _LOCK:
        branch = read_branch(branch_id)
        if branch is None:
            return None
        notes = list(branch.get("notes") or [])
        notes.append({"kind": kind, "content": note[:12_000], "created_at": _now()})
        return update_branch(branch_id, notes=notes)


def validate_patch_content(content: str) -> list[str]:
    errors = []
    if not content.strip():
        errors.append("patch content is empty")
    if len(content) > _MAX_PATCH_CHARS:
        errors.append(f"patch exceeds {_MAX_PATCH_CHARS} characters")
    if "<prompt_opt_patch" in content or "</prompt_opt_patch" in content:
        errors.append("legacy prompt_opt_patch wrappers are not allowed")
    if "<prompt_lab_patch" in content or "</prompt_lab_patch" in content:
        errors.append("prompt_lab_patch wrappers are added by the compiler")
    if "{{" in content or "}}" in content:
        errors.append("patches may not introduce template placeholders")
    return errors


def draft_patch(branch_id: str, title: str, content: str, rationale: str,
                diagnosis: str = "", tests: Optional[list] = None) -> dict:
    """Persist an AI-authored structured overlay; never activates it."""
    with _LOCK:
        branch = read_branch(branch_id)
        if branch is None:
            raise ValueError(f"Prompt Lab branch not found: {branch_id}")
        errors = validate_patch_content(content)
        if errors:
            raise ValueError("; ".join(errors))
        patch_id = _new_id("patch")
        normalized_tests = []
        for index, test in enumerate(tests or []):
            if not isinstance(test, dict):
                continue
            normalized_tests.append({
                "name": str(test.get("name") or f"case-{index + 1}")[:120],
                "input": str(test.get("input") or "")[:8000],
                "expected": str(test.get("expected") or "")[:4000],
                "forbidden": str(test.get("forbidden") or "")[:4000],
            })
        patch = {
            "id": patch_id,
            "branch_id": branch_id,
            "title": title.strip()[:200] or "Prompt behavior patch",
            "content": content.strip(),
            "rationale": rationale.strip(),
            "diagnosis": diagnosis.strip(),
            "tests": normalized_tests,
            "test_runs": [],
            "status": "DRAFT",
            "created_at": _now(),
            "updated_at": _now(),
        }
        _atomic_json(_patches_dir() / f"{patch_id}.json", patch)
        update_branch(branch_id, status="PROPOSING", candidate_patch_id=patch_id)
        return patch


def compile_patch(patch: dict) -> str:
    errors = validate_patch_content(str(patch.get("content") or ""))
    if errors:
        raise ValueError("; ".join(errors))
    patch_id = str(patch.get("id") or "")
    if not _SAFE_ID.fullmatch(patch_id):
        raise ValueError("invalid patch id")
    return (
        f'<prompt_lab_patch id="{patch_id}">\n'
        f'{patch["content"].strip()}\n'
        f'</prompt_lab_patch>'
    )


def get_active_profile() -> dict:
    ensure_default_profile()
    state = _load_state()
    name = state.get("active_profile", "default")
    profile = _load_profile(name)
    if profile is None:
        profile = ensure_default_profile()
    return profile


def get_prompt_lab_section(exclude_patch_ids: Optional[set[str]] = None) -> str:
    """Compile the active project profile uncached for immediate hot reload."""
    try:
        profile = get_active_profile()
        blocks = []
        for patch_id in profile.get("patches") or []:
            if exclude_patch_ids and str(patch_id) in exclude_patch_ids:
                continue
            patch = read_patch(str(patch_id))
            if patch is None:
                continue
            blocks.append(compile_patch(patch))
        if not blocks:
            return ""
        return (
            "\n[PROMPT LAB: ACTIVE PROFILE " + str(profile.get("name", "default")) + "]\n"
            "The following user-approved project prompt overlays are active. "
            "Follow them unless they conflict with higher-priority safety rules.\n"
            + "\n\n".join(blocks) + "\n"
        )
    except Exception:
        # Prompt compilation must never make the main agent unusable.
        return ""


def preview_activation(patch_id: str) -> tuple[bool, str]:
    patch = read_patch(patch_id)
    if patch is None:
        return False, f"Patch {patch_id} not found."
    try:
        block = compile_patch(patch)
    except ValueError as exc:
        return False, f"Patch validation failed: {exc}"
    tests = patch.get("test_runs") or []
    last = tests[-1] if tests else None
    test_text = "not run"
    if last:
        test_text = "passed" if last.get("passed") else "FAILED"
    return True, (
        f"Patch: {patch_id}\nTitle: {patch.get('title', '')}\n"
        f"Tests: {test_text}\n\n{block}"
    )


def _append_history(entry: dict) -> dict:
    entry = dict(entry)
    entry.setdefault("event_id", uuid.uuid4().hex)
    _history_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_history_path(), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def activate_patch(patch_id: str) -> tuple[bool, str]:
    """Activate after the CLI has obtained explicit user confirmation."""
    with _LOCK:
        patch = read_patch(patch_id)
        if patch is None:
            return False, f"Patch {patch_id} not found."
        try:
            compile_patch(patch)
        except ValueError as exc:
            return False, f"Patch validation failed: {exc}"
        profile = get_active_profile()
        before = list(profile.get("patches") or [])
        if patch_id in before:
            return True, f"Patch {patch_id} is already active."
        after = before + [patch_id]
        profile["patches"] = after
        profile["updated_at"] = _now()
        path = _profile_path(str(profile.get("name") or "default"))
        if path is None:
            return False, "Active profile has an invalid name."
        _atomic_json(path, profile)
        patch["status"] = "ACTIVE"
        patch["updated_at"] = _now()
        patch_path = _safe_path(_patches_dir(), patch_id)
        assert patch_path is not None
        _atomic_json(patch_path, patch)
        branch_id = patch.get("branch_id")
        if branch_id:
            update_branch(str(branch_id), status="ACTIVE")
        _append_history({
            "action": "activate", "profile": profile.get("name"),
            "patch_id": patch_id, "before": before, "after": after,
            "created_at": _now(),
        })
        return True, f"Patch {patch_id} activated and hot-reloaded."


def disable_patch(patch_id: str) -> tuple[bool, str]:
    """Disable after the CLI has obtained explicit user confirmation."""
    with _LOCK:
        profile = get_active_profile()
        before = list(profile.get("patches") or [])
        if patch_id not in before:
            return True, f"Patch {patch_id} is not active."
        after = [item for item in before if item != patch_id]
        profile["patches"] = after
        profile["updated_at"] = _now()
        path = _profile_path(str(profile.get("name") or "default"))
        assert path is not None
        _atomic_json(path, profile)
        patch = read_patch(patch_id)
        if patch:
            patch["status"] = "DISABLED"
            patch["updated_at"] = _now()
            patch_path = _safe_path(_patches_dir(), patch_id)
            assert patch_path is not None
            _atomic_json(patch_path, patch)
        _append_history({
            "action": "disable", "profile": profile.get("name"),
            "patch_id": patch_id, "before": before, "after": after,
            "created_at": _now(),
        })
        return True, f"Patch {patch_id} disabled and hot-reloaded."


def create_profile(name: str, patch_ids: Optional[list[str]] = None) -> tuple[bool, str]:
    with _LOCK:
        path = _profile_path(name)
        if path is None:
            return False, "Profile name may contain only letters, numbers, '.', '_' and '-'."
        if path.exists():
            return False, f"Profile {name} already exists."
        missing = [item for item in (patch_ids or []) if read_patch(item) is None]
        if missing:
            return False, "Unknown patches: " + ", ".join(missing)
        untested = []
        for item in patch_ids or []:
            patch = read_patch(item) or {}
            runs = patch.get("test_runs") or []
            if not runs or not runs[-1].get("passed"):
                untested.append(item)
        if untested:
            return False, "Patches need a passing latest test: " + ", ".join(untested)
        profile = {
            "name": name, "patches": list(dict.fromkeys(patch_ids or [])),
            "created_at": _now(), "updated_at": _now(),
        }
        _atomic_json(path, profile)
        return True, f"Profile {name} created."


def switch_profile(name: str) -> tuple[bool, str]:
    """Switch after the CLI has obtained explicit user confirmation."""
    with _LOCK:
        profile = _load_profile(name)
        if profile is None:
            return False, f"Profile {name} not found."
        # Compile every referenced patch before committing the switch.
        for patch_id in profile.get("patches") or []:
            patch = read_patch(str(patch_id))
            if patch is None:
                return False, f"Profile references missing patch {patch_id}."
            runs = patch.get("test_runs") or []
            if not runs or not runs[-1].get("passed"):
                return False, f"Patch {patch_id} does not have a passing latest test."
            try:
                compile_patch(patch)
            except ValueError as exc:
                return False, f"Patch {patch_id} is invalid: {exc}"
        state = _load_state()
        before = state.get("active_profile", "default")
        state["active_profile"] = name
        _save_state(state)
        _append_history({
            "action": "switch", "before_profile": before,
            "after_profile": name, "created_at": _now(),
        })
        return True, f"Profile {name} selected and hot-reloaded."


def rollback() -> tuple[bool, str]:
    """Undo the latest activation/profile switch after external confirmation."""
    try:
        lines = _history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return False, "No Prompt Lab activation history."
    entries = []
    reverted_ids = set()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("action") == "rollback":
            target = value.get("target_event_id")
            if target:
                reverted_ids.add(str(target))
        else:
            entries.append(value)
    entries = [entry for entry in entries
               if str(entry.get("event_id") or "") not in reverted_ids]
    if not entries:
        return False, "No Prompt Lab change to roll back."
    last = entries[-1]
    with _LOCK:
        if last.get("action") == "switch":
            target = str(last.get("before_profile") or "default")
            if _load_profile(target) is None:
                return False, f"Previous profile {target} no longer exists."
            state = _load_state()
            state["active_profile"] = target
            _save_state(state)
            message = f"Rolled back to profile {target}."
        else:
            profile_name = str(last.get("profile") or "default")
            profile = _load_profile(profile_name)
            if profile is None:
                return False, f"Profile {profile_name} no longer exists."
            profile["patches"] = list(last.get("before") or [])
            profile["updated_at"] = _now()
            path = _profile_path(profile_name)
            assert path is not None
            _atomic_json(path, profile)
            message = f"Rolled back the latest change in profile {profile_name}."
        _append_history({
            "action": "rollback", "target_event_id": last.get("event_id"),
            "reverted": last,
            "created_at": _now(),
        })
        return True, message + " Prompt hot-reloaded."


def record_test_result(patch_id: str, passed: bool, report: str,
                       cases: Optional[list] = None) -> dict:
    with _LOCK:
        patch = read_patch(patch_id)
        if patch is None:
            raise ValueError(f"Patch not found: {patch_id}")
        run = {
            "passed": bool(passed), "report": report[:16_000],
            "cases": list(cases or [])[:50], "created_at": _now(),
        }
        runs = list(patch.get("test_runs") or [])
        runs.append(run)
        patch["test_runs"] = runs
        patch["status"] = "TESTED" if passed else "TEST_FAILED"
        patch["updated_at"] = _now()
        path = _safe_path(_patches_dir(), patch_id)
        assert path is not None
        _atomic_json(path, patch)
        branch_id = patch.get("branch_id")
        if branch_id:
            update_branch(str(branch_id), status="READY" if passed else "PROPOSING")
        return run


def build_diagnosis_task(branch_id: str, feedback: str = "") -> str:
    branch = read_branch(branch_id)
    if branch is None:
        raise ValueError(f"Branch not found: {branch_id}")
    snapshot = branch.get("snapshot") or {}
    compact = {
        "description": snapshot.get("description"),
        "conversation": snapshot.get("conversation"),
        "events": snapshot.get("events"),
        "effective_prompt": str(snapshot.get("effective_prompt") or "")[:20_000],
        "branch_notes": branch.get("notes") or [],
    }
    existing = read_patch(str(branch.get("candidate_patch_id") or ""))
    return (
        "You are working in an isolated Prompt Lab branch. Diagnose the reported "
        "AI behavior without changing the user's workspace or active prompt. Distinguish "
        "prompt deficiency, model inconsistency, skill/tool instructions, and missing hard "
        "policy enforcement. A prompt is not a security boundary.\n\n"
        f"Branch ID: {branch_id}\n"
        f"Additional user feedback: {feedback or '(none)'}\n"
        f"Existing candidate: {json.dumps(existing, ensure_ascii=False, default=str) if existing else '(none)'}\n"
        f"Incident snapshot:\n{json.dumps(compact, ensure_ascii=False, default=str)}\n\n"
        "Produce a minimal additive behavioral overlay and 2-5 regression cases. "
        "Use prompt.lab_draft with branch_id, title, content, rationale, diagnosis, "
        "and tests. Tests need name/input/expected/forbidden. If the issue cannot be "
        "reliably fixed by a prompt, explain that in diagnosis and draft only a bounded "
        "mitigation; never claim it is a hard guarantee. Stop after drafting."
    )
