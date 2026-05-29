"""Skill loader for laintas_cli — Phase 3b + progressive loading.

A "skill" is a directory under SKILLS_DIR that exposes tools via skill.py
and optionally provides a SKILL.md with metadata, trigger patterns, and
reference documents.

Progressive Loading (inspired by Claude Code's skill system):
  1. **Startup**: Only scan SKILL.md frontmatter (name, description, triggers)
  2. **Runtime**: When user input matches trigger patterns, load skill body + tools
  3. **On-demand**: Reference docs loaded when skill tools request them

Directory layout:
    ~/.laintas_cli_skills/
        weather/
            skill.py         # must define get_tools()
            SKILL.md         # optional: frontmatter + instructions
            requirements.txt # optional, informational only
            references/      # optional: loaded on-demand
                api_docs.md
            scripts/         # optional: utility scripts
                validate.sh
        my_thing/
            skill.py
            ...

SKILL.md example:

    ---
    name: weather
    description: This skill should be used when the user asks about weather,
        forecasts, or temperature for any city.
    triggers:
      - weather
      - forecast
      - temperature
      - "what's it like in"
    version: 1.0.0
    ---

    # Weather Skill

    Use the weather.get tool to fetch current weather data.

    ## References
    - **references/api_docs.md** - API endpoint documentation

skill.py example:

    from tools import Tool
    import requests

    def _weather(params, ctx):
        city = params.get("city")
        r = requests.get(f"https://wttr.in/{city}?format=3", timeout=5)
        return {"ok": True, "result": r.text}

    def get_tools():
        return [Tool(
            name="weather.get",
            description="Look up a city's current weather (wttr.in).",
            schema={"type":"object", "properties":{"city":{"type":"string"}},
                    "required":["city"]},
            invoke=_weather,
            source="skill:weather",
        )]

Loaded skills are tagged with source="skill:<dirname>" so
ToolRegistry.unregister_source can pull them out on reload.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools import Tool, get_registry


SKILLS_DIR = Path(os.path.expanduser("~/.laintas_cli_skills"))


def ensure_skills_dir() -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR


# ── Skill Metadata & State ──────────────────────────────────────────────

@dataclass
class SkillMetadata:
    """Lightweight metadata parsed from SKILL.md frontmatter at startup.
    Always loaded — used for trigger matching and catalog display."""
    name: str
    description: str = ""
    trigger_patterns: list[str] = field(default_factory=list)
    version: str = ""
    dir_path: str = ""


@dataclass
class SkillState:
    """Full state of a loaded skill. Created on demand when triggered."""
    metadata: SkillMetadata
    body: str = ""                    # SKILL.md body (after frontmatter)
    loaded: bool = False              # whether tools have been registered
    tools: list = field(default_factory=list)  # registered Tool objects
    references: dict = field(default_factory=dict)  # filename -> content
    module: object = None             # imported skill.py module


# ── Global State ────────────────────────────────────────────────────────

_skill_metadata: dict[str, SkillMetadata] = {}   # name -> metadata (always loaded)
_skill_states: dict[str, SkillState] = {}         # name -> state (loaded on demand)
_scan_done: bool = False


# ── Frontmatter Parsing ─────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-like frontmatter from a markdown file.

    Returns (metadata_dict, body_text).
    Supports:
      ---
      name: foo
      description: bar
      triggers:
        - pattern1
        - pattern2
      version: 1.0
      ---
      Body text here...
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("---", 3)
    if end < 0:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 3:].strip()

    meta = {}
    current_key = None
    current_list = None

    for line in fm_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item under a key
        if stripped.startswith("- ") and current_key and current_list is not None:
            current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # Key: value
        m = re.match(r'^(\w+)\s*:\s*(.*)', stripped)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip().strip('"').strip("'")
            if val:
                meta[key] = val
                current_key = key
                current_list = None
            else:
                # Start of a list
                current_key = key
                current_list = []
                meta[key] = current_list

    return meta, body


def _parse_skill_md(skill_dir: Path) -> tuple[SkillMetadata, str]:
    """Parse SKILL.md from a skill directory.

    Returns (metadata, body_text). If no SKILL.md exists, returns
    a metadata object with just the directory name.
    """
    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return SkillMetadata(name=name, dir_path=str(skill_dir)), ""

    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return SkillMetadata(name=name, dir_path=str(skill_dir)), ""

    meta, body = _parse_frontmatter(text)

    triggers = meta.get("triggers", [])
    if isinstance(triggers, str):
        triggers = [triggers]

    return SkillMetadata(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        trigger_patterns=triggers,
        version=meta.get("version", ""),
        dir_path=str(skill_dir),
    ), body


# ── Metadata Scanning (Startup) ─────────────────────────────────────────

def scan_metadata() -> dict[str, SkillMetadata]:
    """Scan all skill directories and parse SKILL.md frontmatter.

    Called once at startup. Lightweight — only reads frontmatter,
    does NOT import skill.py or register tools.
    """
    global _skill_metadata, _scan_done
    ensure_skills_dir()
    _skill_metadata.clear()

    if not SKILLS_DIR.exists():
        _scan_done = True
        return _skill_metadata

    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir():
            continue
        # Must have either skill.py or SKILL.md
        if not (child / "skill.py").is_file() and not (child / "SKILL.md").is_file():
            continue
        meta, _ = _parse_skill_md(child)
        _skill_metadata[meta.name] = meta

    _scan_done = True
    return _skill_metadata


def get_all_metadata() -> dict[str, SkillMetadata]:
    """Return all scanned metadata. Scans on first call."""
    if not _scan_done:
        scan_metadata()
    return _skill_metadata


# ── Trigger Matching ────────────────────────────────────────────────────

def match_triggers(user_input: str) -> list[str]:
    """Check user input against all skill trigger patterns.

    Returns list of skill names whose triggers match.
    """
    if not _scan_done:
        scan_metadata()

    matched = []
    input_lower = user_input.lower()

    for name, meta in _skill_metadata.items():
        if not meta.trigger_patterns:
            continue
        for pattern in meta.trigger_patterns:
            try:
                if re.search(pattern, input_lower, re.IGNORECASE):
                    matched.append(name)
                    break
            except re.error:
                # Fall back to substring match if regex is invalid
                if pattern.lower() in input_lower:
                    matched.append(name)
                    break

    return matched


def check_and_activate(user_input: str) -> list[str]:
    """Check triggers and activate matching skills.

    Returns list of newly activated skill names.
    """
    matched = match_triggers(user_input)
    activated = []
    for name in matched:
        if name in _skill_states and _skill_states[name].loaded:
            continue  # already loaded
        ok, msg = _load_skill_full(name)
        if ok:
            activated.append(name)
    return activated


# ── Full Loading (On-Demand) ────────────────────────────────────────────

def _load_skill_full(name: str) -> tuple[bool, str]:
    """Fully load a skill: import skill.py, register tools, load references.

    The skill must already be in _skill_metadata (from scan_metadata).
    """
    meta = _skill_metadata.get(name)
    if meta is None:
        return False, f"skill '{name}' not found in metadata"

    skill_dir = Path(meta.dir_path)
    if not skill_dir.is_dir():
        return False, f"skill dir '{skill_dir}' not found"

    # Load SKILL.md body if not yet parsed
    if name not in _skill_states:
        _, body = _parse_skill_md(skill_dir)
        _skill_states[name] = SkillState(metadata=meta, body=body)

    state = _skill_states[name]
    if state.loaded:
        return True, f"{name}: already loaded"

    # Import skill.py and register tools
    skill_py = skill_dir / "skill.py"
    if not skill_py.is_file():
        # No skill.py — just load the body (documentation-only skill)
        state.loaded = True
        return True, f"{name}: loaded (documentation only, no tools)"

    # Add skill dir to sys.path temporarily
    added_path = str(skill_dir)
    path_inserted = False
    if added_path not in sys.path:
        sys.path.insert(0, added_path)
        path_inserted = True

    mod_name = f"laintas_skill_{name}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, skill_py)
        if spec is None or spec.loader is None:
            return False, f"{name}: spec_from_file_location returned None"
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        state.module = module

        getter = getattr(module, "get_tools", None)
        if getter is None:
            return False, f"{name}: no get_tools()"

        tools = getter() or []
        registry = get_registry()
        registered = 0
        for t in tools:
            if not isinstance(t, Tool):
                continue
            t.source = f"skill:{name}"
            registry.register(t)
            state.tools.append(t)
            registered += 1

        # Load references/ directory
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            for ref_file in refs_dir.iterdir():
                if ref_file.is_file() and ref_file.suffix in ('.md', '.txt', '.json', '.yaml'):
                    try:
                        state.references[ref_file.name] = ref_file.read_text(encoding="utf-8")
                    except Exception:
                        pass

        state.loaded = True
        return True, f"{name}: registered {registered} tool(s), {len(state.references)} reference(s)"
    except Exception as e:
        return False, f"{name}: {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    finally:
        if path_inserted:
            try:
                sys.path.remove(added_path)
            except ValueError:
                pass


def load_reference(skill_name: str, ref_name: str) -> Optional[str]:
    """Load a reference document from a skill's references/ directory.

    Can be called by skill tools via ctx to get on-demand documentation.
    """
    state = _skill_states.get(skill_name)
    if state is None:
        return None
    # Check cache first
    if ref_name in state.references:
        return state.references[ref_name]
    # Try loading from disk
    meta = _skill_metadata.get(skill_name)
    if meta is None:
        return None
    ref_path = Path(meta.dir_path) / "references" / ref_name
    if ref_path.is_file():
        try:
            content = ref_path.read_text(encoding="utf-8")
            state.references[ref_name] = content
            return content
        except Exception:
            return None
    return None


# ── Backward-Compatible API ─────────────────────────────────────────────

def list_skill_dirs() -> list[Path]:
    """Return every immediate subdirectory of SKILLS_DIR that has skill.py."""
    if not SKILLS_DIR.exists():
        return []
    out = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if child.is_dir() and (child / "skill.py").is_file():
            out.append(child)
    return out


def _load_one(skill_dir: Path) -> tuple[bool, str]:
    """Import skill_dir/skill.py, call get_tools(), register each tool.

    Returns (ok, message). Legacy compatibility wrapper.
    """
    name = skill_dir.name
    # Ensure metadata is scanned
    if name not in _skill_metadata:
        meta, body = _parse_skill_md(skill_dir)
        _skill_metadata[meta.name] = meta
        name = meta.name
    return _load_skill_full(name)


def load_all() -> list[tuple[str, bool, str]]:
    """Load every skill under SKILLS_DIR. Returns [(name, ok, msg), ...].

    In progressive mode, this only scans metadata. Tools are loaded on demand.
    For backward compatibility, skills WITHOUT trigger patterns are loaded fully.
    """
    ensure_skills_dir()
    scan_metadata()
    results: list[tuple[str, bool, str]] = []
    for name, meta in _skill_metadata.items():
        if not meta.trigger_patterns:
            # No triggers = always active, load fully
            ok, msg = _load_skill_full(name)
            results.append((name, ok, msg))
        else:
            # Has triggers = load on demand
            results.append((name, True, f"{name}: metadata loaded, tools deferred until triggered"))
    return results


def reload_all() -> list[tuple[str, bool, str]]:
    """Drop every previously-registered skill tool and re-load from disk."""
    global _skill_metadata, _skill_states, _scan_done

    registry = get_registry()
    sources_to_clear = {
        t.source for t in registry.list()
        if t.source.startswith("skill:")
    }
    for src in sources_to_clear:
        registry.unregister_source(src)

    for mod_name in list(sys.modules):
        if mod_name.startswith("laintas_skill_"):
            sys.modules.pop(mod_name, None)

    _skill_metadata.clear()
    _skill_states.clear()
    _scan_done = False

    return load_all()


def install_template(name: str) -> tuple[bool, str]:
    """Create a skeleton skill at SKILLS_DIR/<name>/. Returns (ok, path|err).

    Creates the full progressive-disclosure directory structure.
    """
    ensure_skills_dir()
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"already exists: {target}"
    try:
        target.mkdir(parents=True)
        (target / "skill.py").write_text(_SKELETON.format(name=name), encoding="utf-8")
        (target / "SKILL.md").write_text(_SKILL_MD_TEMPLATE.format(name=name), encoding="utf-8")
        # Create optional directories
        (target / "references").mkdir(exist_ok=True)
        (target / "references" / "README.md").write_text(
            f"# Reference Documents for {name}\n\n"
            "Add .md, .txt, .json, or .yaml files here.\n"
            "They will be loaded on-demand when skill tools request them.\n",
            encoding="utf-8",
        )
        return True, str(target)
    except OSError as e:
        return False, str(e)


def get_activated_skills_context() -> str:
    """Return a concatenated string of all activated skill bodies.

    Used for {{skillContext}} template variable injection.
    """
    parts = []
    for name, state in _skill_states.items():
        if state.loaded and state.body:
            parts.append(f"### Skill: {name}\n{state.body[:2000]}")
    return "\n\n".join(parts) if parts else ""


def describe_skills_for_prompt() -> str:
    """Render a compact skill catalog for the system prompt.

    Shows all scanned skills with their descriptions and trigger status.
    """
    if not _scan_done:
        scan_metadata()
    if not _skill_metadata:
        return ""

    lines = ["Available skills:"]
    for name, meta in sorted(_skill_metadata.items()):
        state = _skill_states.get(name)
        status = "✅ active" if (state and state.loaded) else "⏳ standby"
        triggers = f" triggers=[{', '.join(meta.trigger_patterns[:5])}]" if meta.trigger_patterns else ""
        desc = meta.description[:100] if meta.description else "(no description)"
        lines.append(f"  - {name} [{status}]: {desc}{triggers}")
    return "\n".join(lines)


# ── Templates ───────────────────────────────────────────────────────────

_SKELETON = '''"""Skill template — edit and reload with `/skill reload`."""

from tools import Tool, ToolCtx


def _hello(params: dict, ctx: ToolCtx) -> dict:
    who = params.get("name", "world")
    return {{"ok": True, "result": f"hello, {{who}}!"}}


def get_tools():
    return [
        Tool(
            name="{name}.hello",
            description="Say hello to someone.",
            schema={{
                "type": "object",
                "properties": {{"name": {{"type": "string", "default": "world"}}}},
            }},
            invoke=_hello,
        ),
    ]
'''

_SKILL_MD_TEMPLATE = """---
name: {name}
description: Describe what this skill does and when it should be used.
triggers:
  - trigger_keyword_1
  - trigger_keyword_2
version: 0.1.0
---

# {name} Skill

Describe how to use this skill. This text is loaded into context when
the skill is triggered.

## References

Add reference documents to the `references/` directory. They are loaded
on-demand when skill tools request them via `load_reference()`.
"""
