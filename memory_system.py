"""
Cross-session persistent memory system for laintas_cli.

Mirrors Claude Code's memory architecture:
  - 4 memory types: user, feedback, project, reference
  - Each memory is a .md file with YAML-style frontmatter
  - MEMORY.md is the index (one-liner per entry, no frontmatter)
  - Stored in ~/.laintas/memory/

Memory files are loaded into the agent's context at each loop iteration,
giving the AI persistent knowledge across sessions.
"""

from __future__ import annotations

import os
import re
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional


import paths

MEMORY_DIR = paths.MEMORY_DIR
MEMORY_INDEX = paths.MEMORY_INDEX
LOCAL_USER_SCOPE = "local-user"

# Valid memory types and their descriptions
MEMORY_TYPES = {
    "user": "User profile, role, preferences, knowledge level",
    "feedback": "User corrections and confirmations about how to approach work",
    "project": "Project goals, deadlines, constraints, ongoing initiatives",
    "reference": "Pointers to external resources (dashboards, repos, channels)",
}


def ensure_memory_dir() -> Path:
    """Create the memory directory and index if they don't exist."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_INDEX.exists():
        MEMORY_INDEX.write_text(
            "# laintas_cli Memory Index\n"
            "# One line per memory entry. Lines starting with # are ignored.\n"
            "# Format: - [Title](file.md) — one-line description\n\n",
            encoding="utf-8",
        )
    return MEMORY_DIR


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML-like frontmatter from a memory file.

    Expects:
    ---
    name: slug-name
    description: one-line summary
    metadata:
      type: user|feedback|project|reference
    ---
    Body content follows.

    Returns (metadata_dict, body_text).
    """
    meta: dict = {}
    body = content

    # Match frontmatter delimited by ---
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, re.DOTALL)
    if not m:
        return meta, content

    front = m.group(1)
    body = m.group(2)

    # Parse simple key: value lines (nested via indentation)
    current_section = meta
    section_stack: list = []
    for line in front.split('\n'):
        # Skip empty lines
        if not line.strip():
            continue
        # Detect indentation level
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if ':' in stripped:
            key, _, value = stripped.partition(':')
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            # Pop sections that are at same or higher level
            while section_stack and section_stack[-1][0] >= indent:
                section_stack.pop()
                current_section = meta
                for _, sec in section_stack:
                    current_section = current_section.setdefault(
                        section_stack[-1][1], {})

            if not value:  # This is a section header
                current_section = current_section.setdefault(key, {})
                section_stack.append((indent, key))
            else:
                current_section[key] = value

    # Normalize: extract 'metadata' sub-dict if present
    if 'metadata' in meta and isinstance(meta['metadata'], dict):
        for k, v in meta['metadata'].items():
            meta.setdefault(k, v)

    return meta, body.strip()


def _format_frontmatter(meta: dict, body: str) -> str:
    """Format a memory file with frontmatter."""
    lines = ["---"]
    lines.append(f"name: {meta.get('name', '')}")
    lines.append(f"description: {meta.get('description', '')}")
    if 'type' in meta:
        lines.append("metadata:")
        lines.append(f"  type: {meta['type']}")
    if meta.get('product'):
        lines.append(f"product: {meta['product']}")
    if meta.get('scope'):
        lines.append(f"scope: {meta['scope']}")
    if meta.get('scope_id'):
        lines.append(f"scope_id: {meta['scope_id']}")
    if meta.get('importance') is not None:
        lines.append(f"importance: {meta['importance']}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return '\n'.join(lines)


def _project_scope_id(cwd: str = None) -> str:
    start = Path(os.path.realpath(cwd or os.getcwd()))
    root = start
    for candidate in (start, *start.parents):
        if (candidate / ".laintas").is_dir() or (candidate / ".git").exists():
            root = candidate
            break
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _resolve_scope(mem_type: str, scope: str = None,
                   scope_id: str = None) -> tuple[str, str]:
    resolved = scope or ("user" if mem_type in ("user", "feedback") else "project")
    if resolved not in ("user", "project"):
        raise ValueError("scope must be 'user' or 'project'")
    if resolved == "user":
        return resolved, scope_id or LOCAL_USER_SCOPE
    return resolved, scope_id or _project_scope_id()


def _visible_in_current_scope(meta: dict) -> bool:
    scope = meta.get("scope")
    if scope == "user":
        return meta.get("scope_id", LOCAL_USER_SCOPE) == LOCAL_USER_SCOPE
    if scope == "project":
        return meta.get("scope_id") == _project_scope_id()
    return False


def list_memories(mem_type: str = None) -> list[dict]:
    """List all memory entries, optionally filtered by type.

    Returns list of {name, description, type, path, mtime}.
    """
    ensure_memory_dir()
    results = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)
            entry_type = meta.get("type") or meta.get("metadata", {}).get("type", "unknown")
            if not meta.get("scope") or not meta.get("scope_id"):
                # Compatibility migration: bind legacy project/reference facts
                # to the current project on first use; user/feedback stays global.
                scope, scope_id = _resolve_scope(entry_type)
                meta.update({
                    "product": "laintas_cli",
                    "scope": scope,
                    "scope_id": scope_id,
                    "importance": meta.get("importance", "0.5"),
                })
                f.write_text(_format_frontmatter(meta, body), encoding="utf-8")
            if mem_type and entry_type != mem_type:
                continue
            if not _visible_in_current_scope(meta):
                continue
            results.append({
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": entry_type,
                "scope": meta.get("scope"),
                "scope_id": meta.get("scope_id"),
                "importance": float(meta.get("importance", 0.5) or 0.5),
                "path": str(f),
                "mtime": f.stat().st_mtime,
            })
        except (OSError, ValueError):
            pass
    return results


def read_memory(name: str) -> Optional[dict]:
    """Read a single memory file. Returns {meta, body, path} or None."""
    ensure_memory_dir()
    f = MEMORY_DIR / f"{name}.md"
    if not f.exists():
        return None
    try:
        content = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)
        return {"meta": meta, "body": body, "path": str(f)}
    except OSError:
        return None


def write_memory(name: str, mem_type: str, description: str,
                 body: str, overwrite: bool = True,
                 scope: str = None, scope_id: str = None,
                 importance: float = 0.5) -> tuple[bool, str]:
    """Create or update a memory file. Updates MEMORY.md index.

    Returns (ok, message).
    """
    ensure_memory_dir()

    if mem_type not in MEMORY_TYPES and mem_type != "unknown":
        return False, f"Invalid memory type: {mem_type}. Use: {list(MEMORY_TYPES)}"

    f = MEMORY_DIR / f"{name}.md"
    if f.exists() and not overwrite:
        return False, f"Memory '{name}' already exists. Use overwrite=True."

    try:
        resolved_scope, resolved_scope_id = _resolve_scope(mem_type, scope, scope_id)
        resolved_importance = max(0.0, min(1.0, float(importance)))
    except (TypeError, ValueError) as exc:
        return False, str(exc)

    meta = {
        "name": name,
        "description": description,
        "type": mem_type,
        "product": "laintas_cli",
        "scope": resolved_scope,
        "scope_id": resolved_scope_id,
        "importance": resolved_importance,
    }
    content = _format_frontmatter(meta, body)
    try:
        f.write_text(content, encoding="utf-8")
    except OSError as e:
        return False, str(e)

    _update_index(name, description, mem_type, resolved_scope)
    return True, str(f)


def delete_memory(name: str) -> tuple[bool, str]:
    """Delete a memory file. Removes from MEMORY.md index.

    Returns (ok, message).
    """
    f = MEMORY_DIR / f"{name}.md"
    if not f.exists():
        return False, f"Memory '{name}' not found"
    try:
        f.unlink()
        _remove_from_index(name)
        return True, f"Deleted {name}"
    except OSError as e:
        return False, str(e)


def _update_index(name: str, description: str, mem_type: str,
                  scope: str = "user") -> None:
    """Add or update an entry in MEMORY.md."""
    ensure_memory_dir()
    lines = MEMORY_INDEX.read_text(encoding="utf-8").split('\n')
    new_entry = f"- [{name}]({name}.md) — {description} [{mem_type}; {scope}]"

    # Replace existing entry if present
    replaced = False
    for i, line in enumerate(lines):
        if f"]({name}.md)" in line:
            lines[i] = new_entry
            replaced = True
            break

    if not replaced:
        lines.append(new_entry)

    MEMORY_INDEX.write_text('\n'.join(lines), encoding="utf-8")


def _remove_from_index(name: str) -> None:
    """Remove an entry from MEMORY.md."""
    if not MEMORY_INDEX.exists():
        return
    lines = MEMORY_INDEX.read_text(encoding="utf-8").split('\n')
    lines = [l for l in lines if f"]({name}.md)" not in l]
    MEMORY_INDEX.write_text('\n'.join(lines), encoding="utf-8")


def load_all_for_prompt(max_per_type: int = 5) -> str:
    """Load all memories formatted for inclusion in the AI system prompt.

    Returns a multi-section string suitable for appending to the prompt.
    Grouped by type. Capped at max_per_type per type.
    """
    ensure_memory_dir()
    memories = list_memories()
    if not memories:
        return ""

    by_type: dict[str, list] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)

    sections = []
    type_labels = {
        "user": "USER PROFILE",
        "feedback": "FEEDBACK & PREFERENCES",
        "project": "PROJECT CONTEXT",
        "reference": "REFERENCES",
    }

    for mem_type, label in type_labels.items():
        entries = sorted(
            by_type.get(mem_type, []),
            key=lambda entry: (entry.get("importance", 0.5), entry.get("mtime", 0)),
            reverse=True,
        )[:max_per_type]
        if not entries:
            continue
        lines = [f"[{label}]"]
        for entry in entries:
            data = read_memory(entry["name"])
            if data:
                body_preview = data["body"][:300]
                lines.append(
                    f"  {entry['name']} [{entry.get('scope')}:{entry.get('scope_id')}]: {body_preview}"
                )
        sections.append('\n'.join(lines))

    return '\n\n'.join(sections) if sections else ""


# ── Integration with agent_loop ─────────────────────────────────────────

def get_memory_context() -> str:
    """Called by agent_loop to get memory context for the prompt.

    Shortcut that loads all memories and returns formatted text.
    """
    ctx = load_all_for_prompt(max_per_type=5)
    return ctx or "(no persistent memories)"
