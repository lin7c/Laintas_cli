"""Workspace trust for executable project customization.

Declarative files (prompt and memory) remain usable everywhere. Python project
extensions execute only when they are generated, byte-identical defaults or
when the user explicitly trusts the current executable file hashes.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

import json_store
import paths

_DEFAULT_TRUST = {"version": 1, "projects": {}, "generated": {}, "files": {}, "extensions": {}}


EXECUTABLE_PROJECT_FILES = (paths.CWD_COMMANDS, paths.CWD_LOOP)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_key(project_dir: Path) -> str:
    return hashlib.sha256(str(project_dir.resolve()).encode("utf-8")).hexdigest()


def _load() -> dict:
    if paths.TRUST_FILE.exists() and not paths.ensure_private_file(paths.TRUST_FILE):
        return dict(_DEFAULT_TRUST)
    data = json_store.load_json(paths.TRUST_FILE, default=lambda: dict(_DEFAULT_TRUST))
    if isinstance(data, dict):
        for key, val in _DEFAULT_TRUST.items():
            data.setdefault(key, dict(val) if isinstance(val, dict) else val)
        return data
    return dict(_DEFAULT_TRUST)


def _save(data: dict) -> None:
    json_store.save_json_atomic(paths.TRUST_FILE, data, mode=0o600)


def record_generated_file(path: Path, expected_content: str) -> None:
    """Trust only a byte-identical generated default, never a modified file."""
    try:
        if path.is_symlink() or not path.is_file():
            return
        expected = hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
        if _sha256(path) != expected:
            return
        data = _load()
        resolved = str(path.resolve())
        if data["generated"].get(resolved) == expected:
            return
        data["generated"][resolved] = expected
        _save(data)
    except OSError:
        return


def _current_hashes(project_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    config_dir = project_dir / ".laintas"
    for name in EXECUTABLE_PROJECT_FILES:
        path = config_dir / name
        if path.is_symlink():
            raise ValueError(f"refusing executable symlink: {path}")
        if path.is_file():
            hashes[name] = _sha256(path)
    return hashes


def trust_project(project_dir: Optional[Path] = None) -> dict:
    root = (project_dir or Path.cwd()).resolve()
    hashes = _current_hashes(root)
    data = _load()
    data["projects"][_project_key(root)] = {
        "realpath": str(root),
        "hashes": hashes,
        "trustedAt": time.time(),
    }
    _save(data)
    return {"realpath": str(root), "hashes": hashes, "trusted": True}


def revoke_project(project_dir: Optional[Path] = None) -> bool:
    root = (project_dir or Path.cwd()).resolve()
    data = _load()
    removed = data["projects"].pop(_project_key(root), None) is not None
    if removed:
        _save(data)
    return removed


def project_status(project_dir: Optional[Path] = None) -> dict:
    root = (project_dir or Path.cwd()).resolve()
    data = _load()
    entry = data["projects"].get(_project_key(root))
    try:
        current = _current_hashes(root)
    except (OSError, ValueError) as exc:
        return {"trusted": False, "reason": str(exc), "realpath": str(root)}
    if not entry:
        return {
            "trusted": False, "reason": "project has not been trusted",
            "realpath": str(root), "hashes": current,
        }
    if entry.get("realpath") != str(root) or entry.get("hashes") != current:
        return {
            "trusted": False,
            "reason": "executable project customization changed since approval",
            "realpath": str(root), "hashes": current,
        }
    return {
        "trusted": True, "reason": "approved hashes match",
        "realpath": str(root), "hashes": current,
    }


def is_execution_allowed(path: Path) -> tuple[bool, str]:
    try:
        if path.is_symlink():
            return False, "executable customization is a symbolic link"
        resolved = str(path.resolve())
        digest = _sha256(path)
        data = _load()
        if data.get("generated", {}).get(resolved) == digest:
            return True, "generated default"
        if data.get("files", {}).get(resolved, {}).get("sha256") == digest:
            return True, "approved file hash"
        status = project_status(path.parent.parent)
        if status.get("trusted"):
            expected = status.get("hashes", {}).get(path.name)
            if expected == digest:
                return True, "trusted project hash"
        return False, status.get("reason", "project is not trusted")
    except OSError as exc:
        return False, str(exc)


def trust_executable_file(path: Path, source: str = "user") -> dict:
    """Approve one executable customization without trusting sibling files."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"refusing non-file or symbolic link: {path}")
    resolved = str(path.resolve())
    digest = _sha256(path)
    data = _load()
    data["files"][resolved] = {
        "sha256": digest, "source": source, "trustedAt": time.time(),
    }
    _save(data)
    return {"realpath": resolved, "sha256": digest, "source": source}


def revoke_executable_file(path: Path) -> bool:
    data = _load()
    removed = data.get("files", {}).pop(str(path.resolve()), None) is not None
    if removed:
        _save(data)
    return removed


def extension_status(kind: str, name: str, entrypoint: Path,
                     related_paths: tuple[Path, ...] = ()) -> dict:
    key = f"{kind}:{name}"
    try:
        if entrypoint.is_symlink():
            return {"trusted": False, "reason": "entrypoint is a symbolic link"}
        digest = _sha256(entrypoint)
        related = {
            str(path.resolve()): _sha256(path)
            for path in related_paths
            if path.is_file() and not path.is_symlink()
        }
    except OSError as exc:
        return {"trusted": False, "reason": str(exc)}
    entry = _load().get("extensions", {}).get(key)
    trusted = bool(
        entry and entry.get("realpath") == str(entrypoint.resolve())
        and entry.get("sha256") == digest
        and entry.get("related", {}) == related
    )
    return {
        "trusted": trusted,
        "reason": "approved hash matches" if trusted else "extension hash is not approved",
        "sha256": digest,
        "realpath": str(entrypoint.resolve()),
        "related": related,
    }


def trust_extension(kind: str, name: str, entrypoint: Path,
                    related_paths: tuple[Path, ...] = ()) -> dict:
    status = extension_status(kind, name, entrypoint, related_paths)
    if "sha256" not in status:
        raise ValueError(status.get("reason", "cannot hash extension"))
    data = _load()
    data["extensions"][f"{kind}:{name}"] = {
        "realpath": status["realpath"],
        "sha256": status["sha256"],
        "trustedAt": time.time(),
        "related": status.get("related", {}),
    }
    _save(data)
    status["trusted"] = True
    status["reason"] = "approved hash matches"
    return status


def revoke_extension(kind: str, name: str) -> bool:
    data = _load()
    removed = data.get("extensions", {}).pop(f"{kind}:{name}", None) is not None
    if removed:
        _save(data)
    return removed
