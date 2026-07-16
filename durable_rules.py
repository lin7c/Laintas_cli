"""Project-scoped durable user rules and recurring completion obligations.

Rules are structured state, not conversation prose.  This keeps explicit
long-lived instructions intact across compaction and /resume while allowing a
later user instruction to disable or supersede them deterministically.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

import json_store
import paths

# Project scope is the only scope with a complete lifecycle today: the project
# file naturally survives /resume and is removed with the project. Do not
# advertise session/global scopes until their ownership and cancellation
# semantics are implemented end-to-end.
_SCOPES = {"project"}
_KINDS = {"constraint", "preference", "completion_hook", "safety_requirement", "output_requirement"}
_TRIGGERS = {"always", "before_task_completion"}


def _path(cwd: Optional[str] = None) -> Path:
    root = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    return root / ".laintas" / paths.CWD_RULES


def _load(cwd: Optional[str] = None) -> dict:
    value = json_store.load_json(_path(cwd), default={})
    if not isinstance(value, dict):
        value = {}
    rules = value.get("rules")
    if not isinstance(rules, list):
        rules = []
    return {"version": 1, "rules": rules}


def _save(value: dict, cwd: Optional[str] = None) -> None:
    path = _path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    json_store.save_json_atomic(path, value)


def list_rules(cwd: Optional[str] = None, *, active_only: bool = False) -> list[dict]:
    rules = list(_load(cwd)["rules"])
    if active_only:
        rules = [r for r in rules if r.get("status") == "active"]
    return rules


def save_rule(text: str, *, scope: str = "project", kind: str = "constraint",
              trigger: str = "always", source: str = "explicit_user_instruction",
              cwd: Optional[str] = None) -> dict:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        raise ValueError("rule text is required")
    if scope not in _SCOPES:
        raise ValueError(f"invalid rule scope: {scope}")
    if kind not in _KINDS:
        raise ValueError(f"invalid rule kind: {kind}")
    if trigger not in _TRIGGERS:
        raise ValueError(f"invalid rule trigger: {trigger}")
    data = _load(cwd)
    # Idempotent: repeating the same explicit rule must not stack hooks.
    for rule in data["rules"]:
        if (rule.get("status") == "active" and rule.get("text") == text
                and rule.get("scope") == scope and rule.get("kind") == kind
                and rule.get("trigger") == trigger):
            return dict(rule)
    now = time.time()
    rule = {
        "id": "rule_" + uuid.uuid4().hex[:12],
        "text": text,
        "scope": scope,
        "kind": kind,
        "trigger": trigger,
        "status": "active",
        "source": source,
        "created_at": now,
        "updated_at": now,
        "cancelled_by": None,
    }
    data["rules"].append(rule)
    _save(data, cwd)
    return dict(rule)


def cancel_rule(rule_id: str, *, reason: str = "explicit_user_instruction",
                cwd: Optional[str] = None) -> Optional[dict]:
    data = _load(cwd)
    for rule in data["rules"]:
        if rule.get("id") == rule_id:
            rule["status"] = "cancelled"
            rule["cancelled_by"] = reason
            rule["updated_at"] = time.time()
            _save(data, cwd)
            return dict(rule)
    return None


def completion_hooks(cwd: Optional[str] = None) -> list[dict]:
    return [r for r in list_rules(cwd, active_only=True)
            if r.get("kind") == "completion_hook"
            and r.get("trigger") == "before_task_completion"]


def format_for_prompt(cwd: Optional[str] = None) -> str:
    rules = list_rules(cwd, active_only=True)
    if not rules:
        return "(none)"
    lines = []
    for rule in rules:
        lines.append(
            f"- [{rule.get('id')}] ({rule.get('scope')}/{rule.get('kind')}/"
            f"{rule.get('trigger')}) {rule.get('text')}"
        )
    return "\n".join(lines)


def unsatisfied_completion_hooks(satisfied_ids: list[str],
                                 cwd: Optional[str] = None) -> list[dict]:
    satisfied = {str(item) for item in (satisfied_ids or [])}
    return [r for r in completion_hooks(cwd) if str(r.get("id")) not in satisfied]
