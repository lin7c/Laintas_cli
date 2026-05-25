"""
Cross-session persistent memory system for laintas_cli.

Mirrors Claude Code's memory architecture:
  - 4 memory types: user, feedback, project, reference
  - Each memory is a .md file with YAML-style frontmatter
  - MEMORY.md is the index (one-liner per entry, no frontmatter)
  - Stored in ~/.laintas_cli_memory/

Memory files are loaded into the agent's context at each loop iteration,
giving the AI persistent knowledge across sessions.
"""

from __future__ import annotations

import os
import re
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


MEMORY_DIR = Path.home() / ".laintas_cli_memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

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
    lines.append("---")
    lines.append("")
    lines.append(body)
    return '\n'.join(lines)


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
            meta, _ = _parse_frontmatter(content)
            entry_type = meta.get("type") or meta.get("metadata", {}).get("type", "unknown")
            if mem_type and entry_type != mem_type:
                continue
            results.append({
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": entry_type,
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
                 body: str, overwrite: bool = True) -> tuple[bool, str]:
    """Create or update a memory file. Updates MEMORY.md index.

    Returns (ok, message).
    """
    ensure_memory_dir()

    if mem_type not in MEMORY_TYPES and mem_type != "unknown":
        return False, f"Invalid memory type: {mem_type}. Use: {list(MEMORY_TYPES)}"

    f = MEMORY_DIR / f"{name}.md"
    if f.exists() and not overwrite:
        return False, f"Memory '{name}' already exists. Use overwrite=True."

    meta = {
        "name": name,
        "description": description,
        "type": mem_type,
    }
    content = _format_frontmatter(meta, body)
    try:
        f.write_text(content, encoding="utf-8")
    except OSError as e:
        return False, str(e)

    _update_index(name, description, mem_type)
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


def _update_index(name: str, description: str, mem_type: str) -> None:
    """Add or update an entry in MEMORY.md."""
    ensure_memory_dir()
    lines = MEMORY_INDEX.read_text(encoding="utf-8").split('\n')
    new_entry = f"- [{name}]({name}.md) — {description} [{mem_type}]"

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
        entries = by_type.get(mem_type, [])[:max_per_type]
        if not entries:
            continue
        lines = [f"[{label}]"]
        for entry in entries:
            data = read_memory(entry["name"])
            if data:
                body_preview = data["body"][:300]
                lines.append(f"  {entry['name']}: {body_preview}")
        sections.append('\n'.join(lines))

    return '\n\n'.join(sections) if sections else ""


# ── Integration with agent_loop ─────────────────────────────────────────

def get_memory_context() -> str:
    """Called by agent_loop to get memory context for the prompt.

    Shortcut that loads all memories and returns formatted text.
    """
    ctx = load_all_for_prompt(max_per_type=5)
    return ctx or "(no persistent memories)"
