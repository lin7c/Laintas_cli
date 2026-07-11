"""Durable HWO/HWG run state.

This module intentionally uses a small project-local JSON store instead of a
new WorkGraph schema migration. The state shape is stable JSON so both CLI
commands and future Helpwo sync code can inspect or migrate it cheaply.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional


RUNS_FILE = "workflow_runs.json"
CACHE_FILE = "hwg_cache.json"


def _project_dir(cwd: Optional[str] = None) -> Path:
    root = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    path = root / ".laintas"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _runs_path(cwd: Optional[str] = None) -> Path:
    return _project_dir(cwd) / RUNS_FILE


def _cache_path(cwd: Optional[str] = None) -> Path:
    return _project_dir(cwd) / CACHE_FILE


def new_run(kind: str, source: str, inputs: Optional[dict] = None, cwd: Optional[str] = None) -> dict:
    now = time.time()
    run = {
        "runId": f"{kind}-{uuid.uuid4().hex[:12]}",
        "kind": kind,
        "source": source,
        "status": "running",
        "inputs": inputs or {},
        "currentNode": None,
        "nodeOutputs": {},
        "agentOutputs": {},
        "loopCounts": {},
        "history": [],
        "pendingInterrupt": None,
        "checkpoints": [],
        "createdAt": now,
        "updatedAt": now,
    }
    save_run(run, cwd=cwd)
    return run


def list_runs(cwd: Optional[str] = None) -> list[dict]:
    store = _read_json(_runs_path(cwd), {"runs": {}})
    runs = list((store.get("runs") or {}).values())
    return sorted(runs, key=lambda r: r.get("updatedAt", 0), reverse=True)


def load_run(run_id: str, cwd: Optional[str] = None) -> Optional[dict]:
    store = _read_json(_runs_path(cwd), {"runs": {}})
    run = (store.get("runs") or {}).get(run_id)
    return dict(run) if isinstance(run, dict) else None


def save_run(run: dict, cwd: Optional[str] = None) -> dict:
    run = dict(run)
    run["updatedAt"] = time.time()
    path = _runs_path(cwd)
    store = _read_json(path, {"runs": {}})
    runs = dict(store.get("runs") or {})
    runs[run["runId"]] = run
    store["runs"] = runs
    _write_json(path, store)
    return run


def checkpoint(run: dict, label: str, payload: Optional[dict] = None, cwd: Optional[str] = None) -> dict:
    run = dict(run)
    checkpoints = list(run.get("checkpoints") or [])
    item = {
        "id": f"cp-{len(checkpoints) + 1:04d}",
        "label": label,
        "createdAt": time.time(),
        "status": run.get("status"),
        "currentNode": run.get("currentNode"),
        "payload": payload or {},
    }
    checkpoints.append(item)
    run["checkpoints"] = checkpoints
    return save_run(run, cwd=cwd)


def emit(run: dict, event_type: str, payload: Optional[dict] = None,
         cwd: Optional[str] = None) -> dict:
    """Append a structured workflow event and persist the run atomically."""
    event = {
        "type": str(event_type),
        "payload": dict(payload or {}),
        "createdAt": time.time(),
    }
    updated = dict(run)
    events = list(updated.get("events") or [])
    event["seq"] = len(events) + 1
    events.append(event)
    # Keep the durable run bounded; checkpoints remain the compact recovery log.
    updated["events"] = events[-500:]
    return save_run(updated, cwd=cwd)


def cache_get(key: str, cwd: Optional[str] = None) -> Optional[dict]:
    store = _read_json(_cache_path(cwd), {"entries": {}})
    entry = (store.get("entries") or {}).get(key)
    if not isinstance(entry, dict):
        return None
    expires_at = entry.get("expiresAt")
    if expires_at and expires_at < time.time():
        cache_delete(key, cwd=cwd)
        return None
    return entry.get("value")


def cache_set(key: str, value: dict, ttl_seconds: Optional[float] = None, cwd: Optional[str] = None) -> None:
    path = _cache_path(cwd)
    store = _read_json(path, {"entries": {}})
    entries = dict(store.get("entries") or {})
    entries[key] = {
        "createdAt": time.time(),
        "expiresAt": (time.time() + ttl_seconds) if ttl_seconds else None,
        "value": value,
    }
    store["entries"] = entries
    _write_json(path, store)


def cache_delete(key: str, cwd: Optional[str] = None) -> None:
    path = _cache_path(cwd)
    store = _read_json(path, {"entries": {}})
    entries = dict(store.get("entries") or {})
    entries.pop(key, None)
    store["entries"] = entries
    _write_json(path, store)
