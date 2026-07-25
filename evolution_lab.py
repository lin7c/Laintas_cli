"""Project-scoped feature creation and code evolution for laintas_cli.

The workflow mirrors Prompt Lab: idea branch -> candidate -> baseline/candidate
test -> explicit activation -> hot reload -> disable/rollback.  It targets
project extensions plus the two legacy executable customization files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import evolution_runner
import paths
import trust_store


_LOCK = threading.RLock()
_SCOPE = threading.local()
_SAFE_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _root() -> Path:
    override = getattr(_SCOPE, "root", None)
    return Path(override) if override else paths.evolution_lab_dir()


def project_root() -> Path:
    return _root().resolve(strict=False)


@contextmanager
def project_scope(root: Optional[str | Path]):
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


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def _ensure() -> None:
    for name in ("branches", "candidates", "runs", "profiles", "backups"):
        (_root() / name).mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _item_path(group: str, item_id: str) -> Optional[Path]:
    if not _SAFE_ID.fullmatch(item_id or ""):
        return None
    return _root() / group / f"{item_id}.json"


def _candidate_digest(candidate: dict) -> str:
    material = {
        "target_type": candidate.get("target_type"),
        "name": candidate.get("name"),
        "files": candidate.get("files"),
        "dependencies": candidate.get("dependencies"),
    }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _state() -> dict:
    value = _read_json(_root() / "state.json", {})
    return value if isinstance(value, dict) else {}


def _save_state(value: dict) -> None:
    _atomic_json(_root() / "state.json", value)


def create_branch(description: str, intent: str = "") -> dict:
    with _LOCK:
        _ensure()
        text = str(description or "").strip() or "Create a useful project extension"
        normalized = str(intent or "").upper()
        if normalized not in {"CREATE", "IMPROVE", "REPAIR"}:
            lowered = text.lower()
            repair_terms = ("repair", "fix", "bug", "error",
                            "\u4fee\u590d", "\u9519\u8bef")
            improve_terms = ("improve", "enhance", "optimize",
                             "\u6539\u8fdb", "\u6539\u5584", "\u4f18\u5316")
            normalized = ("REPAIR" if any(k in lowered for k in repair_terms)
                          else "IMPROVE" if any(k in lowered for k in improve_terms)
                          else "CREATE")
        branch = {
            "id": _new_id("branch"), "intent": normalized,
            "description": text, "status": "CAPTURED",
            "candidate_id": None, "worker_agent_id": None,
            "notes": [], "created_at": _now(), "updated_at": _now(),
        }
        _atomic_json(_root() / "branches" / f"{branch['id']}.json", branch)
        state = _state()
        state["active_branch"] = branch["id"]
        state.setdefault("active_profile", "default")
        _save_state(state)
        ensure_default_profile()
        return branch


def read_branch(branch_id: Optional[str] = None) -> Optional[dict]:
    if not branch_id:
        branch_id = _state().get("active_branch")
    path = _item_path("branches", str(branch_id or ""))
    value = _read_json(path, None) if path else None
    return value if isinstance(value, dict) else None


def set_active_branch(branch_id: str) -> tuple[bool, str]:
    if read_branch(branch_id) is None:
        return False, f"Evolution branch {branch_id} not found."
    state = _state()
    state["active_branch"] = branch_id
    _save_state(state)
    return True, f"Evolution branch {branch_id} selected."


def update_branch(branch_id: str, **updates: Any) -> Optional[dict]:
    with _LOCK:
        branch = read_branch(branch_id)
        if branch is None:
            return None
        branch.update(updates)
        branch["updated_at"] = _now()
        path = _item_path("branches", branch_id)
        assert path is not None
        _atomic_json(path, branch)
        return branch


def add_branch_note(branch_id: str, content: str, kind: str = "user") -> Optional[dict]:
    branch = read_branch(branch_id)
    if branch is None:
        return None
    notes = list(branch.get("notes") or [])
    notes.append({"kind": kind, "content": str(content)[:12000], "created_at": _now()})
    return update_branch(branch_id, notes=notes)


def list_branches() -> list[dict]:
    _ensure()
    values = [_read_json(path, None) for path in (_root() / "branches").glob("*.json")]
    return sorted((v for v in values if isinstance(v, dict)),
                  key=lambda v: v.get("created_at", ""), reverse=True)


def draft_candidate(branch_id: str, title: str, target_type: str, name: str,
                    files: list[dict], description: str = "",
                    dependencies: Optional[list[str]] = None,
                    tests: Optional[list[dict]] = None) -> dict:
    with _LOCK:
        branch = read_branch(branch_id)
        if branch is None:
            raise ValueError(f"Evolution branch not found: {branch_id}")
        target_type = str(target_type or "extension").lower()
        if target_type not in {"extension", "commands", "loop"}:
            raise ValueError("target_type must be extension, commands, or loop")
        if not _SAFE_ID.fullmatch(name or ""):
            raise ValueError("invalid extension/candidate name")
        normalized_files = []
        for item in files or []:
            if not isinstance(item, dict):
                raise ValueError("candidate files must be objects")
            path = str(item.get("path") or "")
            relative = Path(path)
            if not path or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"invalid candidate file path: {path}")
            normalized_files.append({
                "path": path, "operation": str(item.get("operation") or "write"),
                "content": str(item.get("content") or ""),
            })
        candidate = {
            "id": _new_id("evo"), "branch_id": branch_id,
            "intent": branch.get("intent", "CREATE"),
            "title": str(title or "Feature candidate")[:200],
            "description": str(description or "")[:12000],
            "target_type": target_type, "name": name,
            "files": normalized_files,
            "dependencies": [str(item) for item in (dependencies or [])][:100],
            "tests": list(tests or [])[:50], "test_runs": [],
            "status": "DRAFT", "created_at": _now(), "updated_at": _now(),
        }
        ok, errors = evolution_runner.static_check(candidate)
        if not ok:
            raise ValueError("; ".join(errors))
        candidate["candidate_sha256"] = _candidate_digest(candidate)
        _atomic_json(_root() / "candidates" / f"{candidate['id']}.json", candidate)
        update_branch(branch_id, status="PROPOSING", candidate_id=candidate["id"])
        return candidate


def read_candidate(candidate_id: Optional[str] = None) -> Optional[dict]:
    if not candidate_id:
        branch = read_branch()
        candidate_id = branch.get("candidate_id") if branch else None
    path = _item_path("candidates", str(candidate_id or ""))
    value = _read_json(path, None) if path else None
    return value if isinstance(value, dict) else None


def list_candidates() -> list[dict]:
    _ensure()
    values = [_read_json(path, None) for path in (_root() / "candidates").glob("*.json")]
    return sorted((v for v in values if isinstance(v, dict)),
                  key=lambda v: v.get("created_at", ""), reverse=True)


def active_candidate_id() -> Optional[str]:
    branch = read_branch()
    return str(branch.get("candidate_id")) if branch and branch.get("candidate_id") else None


def _baseline_path(candidate: dict) -> Optional[Path]:
    kind = candidate.get("target_type")
    if kind == "extension":
        return paths.extensions_dir() / str(candidate.get("name"))
    return paths.project_dir()


def test_candidate(candidate_id: str) -> tuple[bool, str, Optional[dict]]:
    with _LOCK:
        candidate = read_candidate(candidate_id)
        if candidate is None:
            return False, f"Candidate {candidate_id} not found.", None
        digest = _candidate_digest(candidate)
        report = evolution_runner.run_candidate(candidate, _baseline_path(candidate))
        run = {
            "id": _new_id("run"), "candidate_id": candidate_id,
            "candidate_sha256": digest, "passed": bool(report.get("passed")),
            "report": report, "created_at": _now(),
        }
        _atomic_json(_root() / "runs" / f"{run['id']}.json", run)
        candidate["candidate_sha256"] = digest
        candidate["test_runs"] = list(candidate.get("test_runs") or []) + [run]
        candidate["status"] = "READY" if run["passed"] else "TEST_FAILED"
        candidate["updated_at"] = _now()
        path = _item_path("candidates", candidate_id)
        assert path is not None
        _atomic_json(path, candidate)
        update_branch(candidate["branch_id"], status=candidate["status"])
        summary = "candidate passed" if run["passed"] else "candidate failed"
        return run["passed"], summary, run


def ensure_default_profile() -> dict:
    _ensure()
    path = _root() / "profiles" / "default.json"
    profile = _read_json(path, None)
    if not isinstance(profile, dict):
        profile = {"name": "default", "extensions": {}, "updated_at": _now()}
        _atomic_json(path, profile)
    return profile


def get_active_profile() -> dict:
    name = str(_state().get("active_profile") or "default")
    path = _item_path("profiles", name)
    profile = _read_json(path, None) if path else None
    return profile if isinstance(profile, dict) else ensure_default_profile()


def list_profiles() -> list[dict]:
    ensure_default_profile()
    active = str(_state().get("active_profile") or "default")
    result = []
    for path in (_root() / "profiles").glob("*.json"):
        value = _read_json(path, None)
        if isinstance(value, dict):
            value["active"] = value.get("name") == active
            result.append(value)
    return sorted(result, key=lambda item: str(item.get("name")))


def create_profile(name: str, candidate_ids: list[str]) -> tuple[bool, str]:
    if not _SAFE_ID.fullmatch(name or ""):
        return False, "invalid profile name"
    path = _item_path("profiles", name)
    assert path is not None
    if path.exists():
        return False, f"Profile {name} already exists."
    extensions = {}
    for candidate_id in candidate_ids:
        candidate = read_candidate(candidate_id)
        if not candidate or candidate.get("target_type") != "extension":
            return False, f"Profile candidate must be an extension: {candidate_id}"
        extensions[str(candidate["name"])] = candidate_id
    _atomic_json(path, {"name": name, "extensions": extensions, "updated_at": _now()})
    return True, f"Profile {name} created."


def switch_profile(name: str, runtime) -> tuple[bool, str]:
    path = _item_path("profiles", name)
    profile = _read_json(path, None) if path else None
    if not isinstance(profile, dict):
        return False, f"Profile {name} not found."
    for candidate_id in (profile.get("extensions") or {}).values():
        candidate = read_candidate(str(candidate_id))
        runs = (candidate or {}).get("test_runs") or []
        if (not candidate or not runs or not runs[-1].get("passed")
                or runs[-1].get("candidate_sha256") != _candidate_digest(candidate)):
            return False, f"Profile candidate is missing a current passing test: {candidate_id}"
    previous = str(_state().get("active_profile") or "default")
    for item in runtime.list():
        runtime.unload(item["name"])
    state = _state()
    state["active_profile"] = name
    _save_state(state)
    for candidate_id in (profile.get("extensions") or {}).values():
        ok, message = activate_candidate(
            str(candidate_id), runtime=runtime, record_history=False)
        if not ok:
            state["active_profile"] = previous
            _save_state(state)
            previous_profile = get_active_profile()
            for previous_id in (previous_profile.get("extensions") or {}).values():
                activate_candidate(
                    str(previous_id), runtime=runtime, force=True,
                    record_history=False)
            return False, f"Profile switch failed: {message}"
    _append_history({
        "id": uuid.uuid4().hex, "action": "switch",
        "before_profile": previous, "after_profile": name,
        "created_at": _now(),
    })
    return True, f"Evolution profile {name} selected and hot-reloaded."


def _append_history(value: dict) -> None:
    path = _root() / "activation-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _write_candidate_files(candidate: dict, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in candidate.get("files") or []:
        path = destination / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")


def activate_candidate(candidate_id: str, runtime=None,
                       force: bool = False,
                       record_history: bool = True) -> tuple[bool, str]:
    with _LOCK:
        candidate = read_candidate(candidate_id)
        if candidate is None:
            return False, f"Candidate {candidate_id} not found."
        digest = _candidate_digest(candidate)
        runs = candidate.get("test_runs") or []
        tested = bool(runs and runs[-1].get("passed")
                      and runs[-1].get("candidate_sha256") == digest)
        if not tested and not force:
            return False, "Candidate needs a passing test for its current SHA-256."
        kind = str(candidate["target_type"])
        name = str(candidate["name"])
        backup_id = _new_id("backup")
        backup = _root() / "backups" / backup_id
        backup.parent.mkdir(parents=True, exist_ok=True)
        target: Optional[Path] = None
        try:
            if kind == "extension":
                target = paths.extensions_dir() / name
                if target.exists():
                    shutil.copytree(target, backup)
                staging = paths.extensions_dir() / f".{name}.{uuid.uuid4().hex}.tmp"
                _write_candidate_files(candidate, staging)
                if runtime is not None:
                    runtime.unload(name)
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
                if runtime is not None:
                    loaded, message = runtime.load(name)
                    if not loaded:
                        shutil.rmtree(target, ignore_errors=True)
                        if backup.exists():
                            shutil.copytree(backup, target)
                            runtime.load(name)
                        return False, f"Activation rolled back: {message}"
                profile = get_active_profile()
                before = dict(profile.get("extensions") or {})
                old_candidate_id = before.get(name)
                if old_candidate_id and old_candidate_id != candidate_id:
                    old_candidate = read_candidate(str(old_candidate_id))
                    if old_candidate:
                        old_candidate["status"] = "SUPERSEDED"
                        old_candidate["updated_at"] = _now()
                        old_path = _item_path("candidates", str(old_candidate_id))
                        if old_path:
                            _atomic_json(old_path, old_candidate)
                profile["extensions"] = dict(before, **{name: candidate_id})
                profile["updated_at"] = _now()
                profile_path = _item_path("profiles", str(profile.get("name") or "default"))
                assert profile_path is not None
                _atomic_json(profile_path, profile)
            else:
                target = paths.project_file(
                    paths.CWD_COMMANDS if kind == "commands" else paths.CWD_LOOP)
                backup.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.copy2(target, backup / target.name)
                content = candidate["files"][0]["content"]
                tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, target)
                trust_store.trust_executable_file(target, f"evolution:{candidate_id}")
                if kind == "loop":
                    import agent_loop
                    agent_loop.clear_loop_command_cache()
                else:
                    try:
                        import laintas_cli
                        laintas_cli._extra_cmd_handler_cache = None
                        laintas_cli._extra_cmd_mtime_cache = 0
                    except Exception:
                        pass
                before = {}
            candidate["status"] = "ACTIVE"
            candidate["candidate_sha256"] = digest
            candidate["updated_at"] = _now()
            path = _item_path("candidates", candidate_id)
            assert path is not None
            _atomic_json(path, candidate)
            update_branch(candidate["branch_id"], status="ACTIVE")
            if record_history:
                _append_history({
                    "id": uuid.uuid4().hex, "action": "activate",
                    "candidate_id": candidate_id, "target_type": kind,
                    "name": name, "backup": str(backup),
                    "before": before, "created_at": _now(),
                })
            return True, f"Candidate {candidate_id} activated and hot-reloaded."
        except Exception as exc:
            try:
                if target is not None and kind == "extension":
                    if runtime is not None:
                        runtime.unload(name)
                    shutil.rmtree(target, ignore_errors=True)
                    if backup.is_dir():
                        shutil.copytree(backup, target)
                        if runtime is not None:
                            runtime.load(name)
                elif target is not None and kind in ("commands", "loop"):
                    source = backup / target.name
                    if source.is_file():
                        shutil.copy2(source, target)
                        trust_store.trust_executable_file(
                            target, "evolution:activation-rollback")
            except Exception:
                pass
            return False, f"Activation failed: {type(exc).__name__}: {exc}"


def load_active_extensions(runtime) -> list[tuple[str, bool, str]]:
    profile = get_active_profile()
    results = []
    for name in sorted((profile.get("extensions") or {})):
        ok, message = runtime.load(name)
        results.append((name, ok, message))
    return results


def reconcile_workspace() -> None:
    """Reconcile candidate labels after /reload or out-of-band edits."""
    for candidate in list_candidates():
        if candidate.get("status") != "ACTIVE":
            continue
        kind = candidate.get("target_type")
        matches = True
        if kind in ("commands", "loop"):
            target = paths.project_file(
                paths.CWD_COMMANDS if kind == "commands" else paths.CWD_LOOP)
            expected = str((candidate.get("files") or [{}])[0].get("content") or "")
            try:
                matches = target.read_text(encoding="utf-8") == expected
            except OSError:
                matches = False
        elif kind == "extension":
            target = paths.extensions_dir() / str(candidate.get("name") or "")
            for item in candidate.get("files") or []:
                try:
                    if (target / item["path"]).read_text(encoding="utf-8") != item["content"]:
                        matches = False
                        break
                except OSError:
                    matches = False
                    break
        if not matches:
            candidate["status"] = (
                "RESET_BY_RELOAD" if kind in ("commands", "loop") else "DRIFTED")
            candidate["updated_at"] = _now()
            path = _item_path("candidates", str(candidate.get("id") or ""))
            if path:
                _atomic_json(path, candidate)


def disable_extension(name: str, runtime) -> tuple[bool, str]:
    with _LOCK:
        profile = get_active_profile()
        before = dict(profile.get("extensions") or {})
        if name not in before:
            return True, f"Extension {name} is not active."
        after = dict(before)
        after.pop(name, None)
        profile["extensions"] = after
        profile["updated_at"] = _now()
        path = _item_path("profiles", str(profile.get("name") or "default"))
        assert path is not None
        _atomic_json(path, profile)
        runtime.unload(name)
        candidate_id = before.get(name)
        candidate = read_candidate(str(candidate_id or ""))
        if candidate:
            candidate["status"] = "DISABLED"
            candidate["updated_at"] = _now()
            candidate_path = _item_path("candidates", str(candidate_id))
            if candidate_path:
                _atomic_json(candidate_path, candidate)
        _append_history({
            "id": uuid.uuid4().hex, "action": "disable", "name": name,
            "before": before, "created_at": _now(),
        })
        return True, f"Extension {name} disabled."


def rollback(runtime) -> tuple[bool, str]:
    path = _root() / "activation-history.jsonl"
    try:
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        return False, "No Evolution Lab activation history."
    reverted = {str(item.get("target_id")) for item in entries if item.get("action") == "rollback"}
    changes = [item for item in entries if item.get("action") != "rollback"
               and str(item.get("id")) not in reverted]
    if not changes:
        return False, "No Evolution Lab change to roll back."
    last = changes[-1]
    name = str(last.get("name") or "")
    if last.get("action") == "switch":
        previous = str(last.get("before_profile") or "default")
        profile_path = _item_path("profiles", previous)
        if profile_path is None or not profile_path.exists():
            return False, f"Previous profile {previous} is missing."
        for item in runtime.list():
            runtime.unload(item["name"])
        state = _state()
        state["active_profile"] = previous
        _save_state(state)
        previous_profile = get_active_profile()
        for candidate_id in (previous_profile.get("extensions") or {}).values():
            ok, message = activate_candidate(
                str(candidate_id), runtime=runtime, force=True,
                record_history=False)
            if not ok:
                return False, f"Profile rollback failed: {message}"
    elif last.get("action") == "disable":
        profile = get_active_profile()
        profile["extensions"] = dict(last.get("before") or {})
        profile_path = _item_path("profiles", str(profile.get("name") or "default"))
        assert profile_path is not None
        _atomic_json(profile_path, profile)
        if name:
            runtime.load(name)
            restored_id = (last.get("before") or {}).get(name)
            restored = read_candidate(str(restored_id or ""))
            if restored:
                restored["status"] = "ACTIVE"
                restored["updated_at"] = _now()
                restored_path = _item_path("candidates", str(restored_id))
                if restored_path:
                    _atomic_json(restored_path, restored)
    else:
        kind = last.get("target_type")
        backup = Path(str(last.get("backup") or ""))
        if kind == "extension":
            target = paths.extensions_dir() / name
            runtime.unload(name)
            shutil.rmtree(target, ignore_errors=True)
            if backup.is_dir():
                shutil.copytree(backup, target)
                runtime.load(name)
            profile = get_active_profile()
            profile["extensions"] = dict(last.get("before") or {})
            profile_path = _item_path("profiles", str(profile.get("name") or "default"))
            assert profile_path is not None
            _atomic_json(profile_path, profile)
        else:
            target = paths.project_file(
                paths.CWD_COMMANDS if kind == "commands" else paths.CWD_LOOP)
            source = backup / target.name
            if not source.is_file():
                return False, "Evolution backup is missing."
            shutil.copy2(source, target)
            trust_store.trust_executable_file(target, "evolution:rollback")
            if kind == "loop":
                import agent_loop
                agent_loop.clear_loop_command_cache()
    _append_history({
        "id": uuid.uuid4().hex, "action": "rollback",
        "target_id": last.get("id"), "created_at": _now(),
    })
    return True, "Latest Evolution Lab change rolled back and hot-reloaded."


def build_design_task(branch_id: str, feedback: str = "") -> str:
    branch = read_branch(branch_id)
    if branch is None:
        raise ValueError(f"Branch not found: {branch_id}")
    return (
        "You are an Evolution Lab feature designer. Turn the user's creative idea "
        "into a working laintas project extension or a focused commands.py/loop.py "
        "improvement. Prefer a standalone extension for new functionality. Explore "
        "existing code read-only, then call evolve.lab_draft. Do not modify active "
        "files directly.\n\n"
        f"Branch ID: {branch_id}\nIntent: {branch.get('intent')}\n"
        f"Idea: {branch.get('description')}\nAdditional feedback: {feedback or '(none)'}\n\n"
        "For an extension provide extension.json, main.py, optional tests.py and "
        "README.md. main.py must define setup(ctx) and may call "
        "ctx.register_command(name, handler), ctx.register_tool(tool), or "
        "ctx.register_loop(handler). Integrated inference uses ctx.backend.chat; "
        "the gateway does not expose authentication. Include useful tests and stop "
        "after drafting so the user can review and activate it."
    )
