"""Skill loader for laintas_cli — explicit progressive loading.

A "skill" is a directory under SKILLS_DIR that exposes optional tools via
skill.py and optionally provides a SKILL.md with metadata and reference
documents.

Progressive Loading:
  1. **Startup**: Only scan SKILL.md frontmatter (name, description)
  2. **Runtime**: AI calls skill.load(name) when the catalog shows a relevant skill
  3. **On-demand**: Reference docs loaded when skill tools request them

Directory layout:
    ~/.laintas/skills/
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
    description: Use this when working with weather, forecasts, or temperature.
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
import json
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools import Tool, get_registry, infer_capabilities


import paths
import trust_store

SKILLS_DIR = paths.SKILLS_DIR
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "default_skills"
SKILL_MANIFEST = "extension.json"
ALLOWED_CAPABILITIES = {
    "fs.read", "fs.write", "process.exec", "network", "browser.mutate",
    "agent.control", "core.other",
}


def load_skill_manifest(skill_dir: Path, expected_name: str) -> tuple[Optional[dict], str]:
    path = skill_dir / SKILL_MANIFEST
    if not path.is_file() or path.is_symlink():
        return None, f"missing trusted extension manifest: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"invalid extension manifest: {exc}"
    caps = data.get("capabilities")
    if (not isinstance(data, dict) or data.get("schemaVersion") != 1
            or data.get("name") != expected_name
            or data.get("entrypoint") != "skill.py"
            or not isinstance(caps, list)
            or not all(isinstance(cap, str) and cap in ALLOWED_CAPABILITIES
                       for cap in caps)):
        return None, "extension manifest fields or capabilities are invalid"
    return data, ""


def ensure_skills_dir() -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR


def ensure_bundled_skills_installed() -> list[str]:
    """Copy bundled documentation skills into the user skills dir if missing.

    Existing user skills win; this only seeds defaults for fresh installs.
    """
    ensure_skills_dir()
    installed: list[str] = []
    if not BUNDLED_SKILLS_DIR.is_dir():
        return installed
    for src in sorted(BUNDLED_SKILLS_DIR.iterdir()):
        if not src.is_dir():
            continue
        dst = SKILLS_DIR / src.name
        if dst.exists():
            continue
        try:
            shutil.copytree(src, dst)
            installed.append(src.name)
        except OSError:
            continue
    return installed


# ── Skill Metadata & State ──────────────────────────────────────────────

@dataclass
class SkillMetadata:
    """Lightweight metadata parsed from SKILL.md frontmatter at startup.
    Always loaded — used for catalog display and explicit skill.load."""
    name: str
    description: str = ""
    trigger_patterns: list[str] = field(default_factory=list)
    version: str = ""
    dir_path: str = ""


@dataclass
class SkillState:
    """Full state of a loaded skill. Created on demand by skill.load."""
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
    ensure_bundled_skills_installed()
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
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", meta.name):
            continue
        _skill_metadata[meta.name] = meta

    # Bundled defaults are also usable in-place. User-installed skills with the
    # same metadata name take precedence because they were scanned first.
    if BUNDLED_SKILLS_DIR.is_dir():
        for child in sorted(BUNDLED_SKILLS_DIR.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "skill.py").is_file() and not (child / "SKILL.md").is_file():
                continue
            meta, _ = _parse_skill_md(child)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", meta.name):
                continue
            _skill_metadata.setdefault(meta.name, meta)

    _scan_done = True
    return _skill_metadata


def get_all_metadata() -> dict[str, SkillMetadata]:
    """Return all scanned metadata. Scans on first call."""
    if not _scan_done:
        scan_metadata()
    return _skill_metadata


def load_skill(name: str) -> tuple[bool, str]:
    """Public wrapper for explicitly loading a skill by name."""
    if not _scan_done:
        scan_metadata()
    return _load_skill_full(name)


def unload_skill(name: str) -> tuple[bool, str]:
    """Unload a previously-loaded skill (the inverse of ``load_skill``).

    Drops the tools the skill registered (source ``skill:<name>``), clears its
    loaded flag so its body stops being injected via ``{{skillContext}}``, and
    forgets the imported module so a later ``load_skill`` re-imports a fresh
    copy. Metadata stays scanned, so the skill remains listable and reloadable.
    Returns ``(ok, message)``.
    """
    if not _scan_done:
        scan_metadata()
    state = _skill_states.get(name)
    if state is None or not state.loaded:
        return False, f"{name}: not loaded"

    removed = 0
    try:
        removed = get_registry().unregister_source(f"skill:{name}")
    except Exception:
        pass
    state.tools.clear()
    # Drop the imported skill.py module so re-loading picks up edits/fresh state.
    sys.modules.pop(f"laintas_skill_{name}", None)
    state.module = None
    state.loaded = False
    return True, f"{name}: unloaded ({removed} tool(s) removed)"


def loaded_skill_names() -> list[str]:
    """Return the names of all currently-loaded skills (tools/body active)."""
    return [name for name, st in _skill_states.items() if st and st.loaded]


def unload_all_skills() -> list[tuple[str, bool, str]]:
    """Unload every currently-loaded skill, freeing their tools and context.

    The inverse of loading each skill; a no-op returns []. Use this to reclaim
    context in one shot when a batch of specialized work is finished. Returns
    ``[(name, ok, message), ...]`` for each skill that was loaded.
    """
    if not _scan_done:
        scan_metadata()
    results: list[tuple[str, bool, str]] = []
    for name in loaded_skill_names():
        ok, msg = unload_skill(name)
        results.append((name, ok, msg))
    return results


def list_skills() -> list[dict]:
    """Return lightweight skill catalog for tools/UI."""
    if not _scan_done:
        scan_metadata()
    out = []
    for name, meta in sorted(_skill_metadata.items()):
        state = _skill_states.get(name)
        out.append({
            "name": name,
            "description": meta.description,
            "version": meta.version,
            "loaded": bool(state and state.loaded),
            "path": meta.dir_path,
        })
    return out


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

    trust = trust_store.extension_status(
        "skill", name, skill_py, (skill_dir / SKILL_MANIFEST,))
    if not trust.get("trusted"):
        return False, (
            f"{name}: executable skill is not trusted ({trust.get('reason')}). "
            f"Review {skill_py} and run '/skill trust {name}'."
        )
    manifest, manifest_error = load_skill_manifest(skill_dir, name)
    if manifest is None:
        return False, f"{name}: {manifest_error}"
    declared_capabilities = frozenset(manifest["capabilities"])

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
            raise ValueError("no get_tools()")

        provided_tools = getter() or []
        if not isinstance(provided_tools, (list, tuple)):
            raise ValueError("get_tools() must return a list or tuple")
        registry = get_registry()
        prepared_tools: list[Tool] = []
        prepared_names: set[str] = set()
        for t in provided_tools:
            if not isinstance(t, Tool):
                raise ValueError("get_tools() returned a non-Tool value")
            original_name = t.name
            prefix = f"skill.{name}."
            if not t.name.startswith(prefix):
                t.name = prefix + t.name.lstrip(".")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", t.name or ""):
                raise ValueError(f"invalid tool name: {t.name!r}")
            if t.name in prepared_names or registry.get(t.name) is not None:
                raise ValueError(f"tool name is already registered: {t.name}")
            t.source = f"skill:{name}"
            t.trust_level = "trusted-extension"
            # Infer from the author-supplied name before applying our namespace;
            # otherwise "shell.exec" becomes "skill.foo.shell.exec" and looks
            # like an unprivileged, unknown tool.
            effective_caps = t.capabilities or infer_capabilities(original_name)
            if not effective_caps.issubset(declared_capabilities):
                raise ValueError(
                    f"{name}: tool {t.name} requests undeclared capabilities: "
                    f"{sorted(effective_caps - declared_capabilities)}"
                )
            t.capabilities = effective_caps
            prepared_names.add(t.name)
            prepared_tools.append(t)

        # Registration is transactional: a failed load leaves no partial tools.
        for t in prepared_tools:
            if not registry.register(t, overwrite=False):
                registry.unregister_source(f"skill:{name}")
                raise ValueError(f"failed to register tool: {t.name}")
        state.tools.extend(prepared_tools)
        registered = len(prepared_tools)

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
        get_registry().unregister_source(f"skill:{name}")
        state.tools.clear()
        state.module = None
        sys.modules.pop(mod_name, None)
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
    """Return every immediate subdirectory of SKILLS_DIR that has a skill file."""
    if not SKILLS_DIR.exists():
        return []
    out = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if child.is_dir() and ((child / "skill.py").is_file() or (child / "SKILL.md").is_file()):
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

    In explicit progressive mode, this only scans metadata. Skill bodies and
    skill tools are loaded only when the AI calls skill.load.
    """
    ensure_skills_dir()
    ensure_bundled_skills_installed()
    scan_metadata()
    results: list[tuple[str, bool, str]] = []
    for name, meta in _skill_metadata.items():
        results.append((name, True, f"{name}: metadata loaded, use skill.load to activate"))
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
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""):
        return False, "skill name must contain only letters, numbers, underscore, or hyphen"
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"already exists: {target}"
    try:
        target.mkdir(parents=True)
        (target / "skill.py").write_text(_SKELETON.format(name=name), encoding="utf-8")
        (target / "SKILL.md").write_text(_SKILL_MD_TEMPLATE.format(name=name), encoding="utf-8")
        (target / SKILL_MANIFEST).write_text(
            json.dumps({
                "schemaVersion": 1,
                "name": name,
                "version": "0.1.0",
                "entrypoint": "skill.py",
                "capabilities": ["core.other"],
            }, indent=2),
            encoding="utf-8",
        )
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

    Used for {{skillContext}} template variable injection. The full SKILL.md body
    is injected — there is no per-skill length cap, so a skill is never silently
    truncated once loaded. Context is instead bounded by unloading skills when
    their work is done (see ``unload_skill`` / ``unload_all_skills`` and the
    ``skill.unload`` tool).
    """
    parts = []
    for name, state in _skill_states.items():
        if state.loaded and state.body:
            parts.append(f"### Skill: {name}\n{state.body}")
    return "\n\n".join(parts) if parts else ""


def describe_skills_for_prompt() -> str:
    """Render a compact skill catalog for the system prompt.

    Shows all scanned skills with their descriptions and loaded status.
    """
    if not _scan_done:
        scan_metadata()
    if not _skill_metadata:
        return ""

    lines = ["Available skills. Load a relevant skill with `skill.load` before relying on its instructions:"]
    for name, meta in sorted(_skill_metadata.items()):
        state = _skill_states.get(name)
        status = "loaded" if (state and state.loaded) else "available"
        desc = meta.description[:100] if meta.description else "(no description)"
        lines.append(f"  - {name} [{status}]: {desc}")
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
            name="hello",
            description="Say hello to someone.",
            schema={{
                "type": "object",
                "properties": {{"name": {{"type": "string", "default": "world"}}}},
            }},
            invoke=_hello,
            capabilities=frozenset({{"core.other"}}),
        ),
    ]
'''

_SKILL_MD_TEMPLATE = """---
name: {name}
description: Describe what this skill does and when it should be used.
version: 0.1.0
---

# {name} Skill

Describe how to use this skill. This text is loaded into context when
the AI calls `skill.load`.

## References

Add reference documents to the `references/` directory. They are loaded
on-demand when skill tools request them via `load_reference()`.
"""
