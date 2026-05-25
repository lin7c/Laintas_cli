"""Skill loader for laintas_cli — Phase 3b.

A "skill" is a directory under SKILLS_DIR that exposes a top-level
`get_tools() -> list[Tool]`. Skills can `pip install` any third-party
library — they share the laintas-cli venv at import time.

Directory layout:
    ~/.laintas_cli_skills/
        weather/
            skill.py         # must define get_tools()
            requirements.txt # optional, informational only
        my_thing/
            skill.py
            ...

skill.py example:

    from tools import Tool   # or: from laintas_cli.tools import Tool
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
import sys
import traceback
from pathlib import Path

from tools import Tool, get_registry


SKILLS_DIR = Path(os.path.expanduser("~/.laintas_cli_skills"))


def ensure_skills_dir() -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR


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

    Returns (ok, message). Errors are caught; one bad skill cannot prevent
    others from loading.
    """
    name = skill_dir.name
    skill_py = skill_dir / "skill.py"
    if not skill_py.is_file():
        return False, f"{name}: missing skill.py"

    # Add skill dir to sys.path *temporarily* so a skill can import its own
    # sibling modules without colliding across skills.
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

        getter = getattr(module, "get_tools", None)
        if getter is None:
            return False, f"{name}: no get_tools()"

        tools = getter() or []
        registry = get_registry()
        registered = 0
        for t in tools:
            if not isinstance(t, Tool):
                continue
            # Force the source tag so reload can find them later, even if
            # the skill author set something different.
            t.source = f"skill:{name}"
            registry.register(t)
            registered += 1
        return True, f"{name}: registered {registered} tool(s)"
    except Exception as e:
        return False, f"{name}: {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    finally:
        if path_inserted:
            try:
                sys.path.remove(added_path)
            except ValueError:
                pass


def load_all() -> list[tuple[str, bool, str]]:
    """Load every skill under SKILLS_DIR. Returns [(name, ok, msg), ...]."""
    ensure_skills_dir()
    results: list[tuple[str, bool, str]] = []
    for sd in list_skill_dirs():
        ok, msg = _load_one(sd)
        results.append((sd.name, ok, msg))
    return results


def reload_all() -> list[tuple[str, bool, str]]:
    """Drop every previously-registered skill tool and re-load from disk.

    Tools tagged source="skill:*" are removed first. Skills that fail to
    re-import leave a gap; the registry stays consistent.
    """
    registry = get_registry()
    # Find all skill sources currently registered and drop them.
    sources_to_clear = {
        t.source for t in registry.list()
        if t.source.startswith("skill:")
    }
    for src in sources_to_clear:
        registry.unregister_source(src)

    # Also forget the cached modules so the next import re-executes them.
    for mod_name in list(sys.modules):
        if mod_name.startswith("laintas_skill_"):
            sys.modules.pop(mod_name, None)

    return load_all()


def install_template(name: str) -> tuple[bool, str]:
    """Create a skeleton skill at SKILLS_DIR/<name>/. Returns (ok, path|err)."""
    ensure_skills_dir()
    target = SKILLS_DIR / name
    if target.exists():
        return False, f"already exists: {target}"
    try:
        target.mkdir(parents=True)
        (target / "skill.py").write_text(_SKELETON.format(name=name), encoding="utf-8")
        return True, str(target / "skill.py")
    except OSError as e:
        return False, str(e)


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
