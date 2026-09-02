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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools import Tool, get_registry, infer_capabilities


import paths
import backend_profiles
import json_store
import trust_store

SKILLS_DIR = paths.SKILLS_DIR
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "default_skills"

#: Project-scoped skills: `<project>/.laintas/skills/`. A function, not a
#: constant, because os.chdir() moves the project during a session.
#:
#: This scope is what makes a learned, repo-specific skill safe to write at
#: all. Skill selection degrades monotonically with library size — measured at
#: -8% pass rate at 52 skills, -14% at 102, -21% at 202 (arXiv 2605.24050),
#: through "shadowing": a distractor whose description overlaps the right skill
#: hides it. A global store would pay that price on every project for lessons
#: that apply to one. Scoped, the catalogue a task actually sees stays small.
PROJECT_SKILLS_SUBDIR = "skills"

#: Where a skill came from, in precedence order. Project beats user beats
#: bundled: the most specific description of how to work here wins.
SCOPE_PROJECT = "project"
SCOPE_USER = "user"
SCOPE_BUNDLED = "bundled"


def project_skills_dir() -> Path:
    return paths.project_dir() / PROJECT_SKILLS_SUBDIR


def ensure_project_skills_dir() -> Path:
    target = project_skills_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target
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


# ── Managed (externally synchronised) skills ────────────────────────────
#
# A skill directory carrying this marker was placed here by something other than
# the user — today, an Enterprise organisation pushing shared skills to its
# members. The marker is what makes re-syncing safe: it is the only evidence
# that overwriting the directory destroys nothing the user wrote.

MANAGED_MARKER = ".laintas-asset.json"
GATEWAY_SKILL_OWNER = "gateway"
GATEWAY_SKILL_MAX_RESPONSE_BYTES = 512 * 1024
GATEWAY_SKILL_MAX_MANUAL_BYTES = 200 * 1024


def managed_owner(directory: Path) -> str:
    """Who manages this skill directory, or "" if the user owns it."""
    try:
        data = json.loads((directory / MANAGED_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    owner = data.get("managed_by") if isinstance(data, dict) else None
    return str(owner or "")


def claim_managed_dir(name: str, owner: str) -> tuple[Optional[Path], str]:
    """Resolve where a managed skill may be written, refusing to take over.

    Returns ``(path, "")`` when the slot is free or already belongs to *owner*,
    and ``(None, reason)`` when it does not. A user who happens to have written
    a skill of the same name keeps it — silently replacing it would be the
    behaviour that made pushed defaults so destructive the last time round.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""):
        return None, f"invalid skill name: {name!r}"
    target = SKILLS_DIR / name
    if not target.exists():
        return target, ""
    existing = managed_owner(target)
    if existing == owner:
        return target, ""
    if existing:
        return None, f"'{name}' is already managed by {existing}"
    return None, f"'{name}' already exists as your own skill and was left alone"


def sync_gateway_skills(session: Optional[dict], *, requests_module=None,
                        timeout: float = 4.0) -> list[tuple[str, bool, str]]:
    """Best-effort sync of documentation-only skills from the active gateway.

    A local user-owned directory always wins. Gateway entries create only a
    SKILL.md plus the managed marker: no Python or executable manifest received
    over the network is accepted. Official credentials remain audience-bound
    through backend_profiles.request_auth().
    """
    if not session:
        return []
    try:
        requests_module = requests_module or __import__("requests")
        profile = backend_profiles.resolve("https://laintas.com")
        headers, cookies = backend_profiles.request_auth(profile, session)
        response = requests_module.get(
            f"{profile.base_url}/api/skills",
            headers=headers, cookies=cookies, timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        return [("gateway", False, f"skill catalog unavailable: {exc}")]
    if response.status_code != 200:
        return [("gateway", False, f"skill catalog HTTP {response.status_code}")]
    raw = response.content
    if len(raw) > GATEWAY_SKILL_MAX_RESPONSE_BYTES:
        return [("gateway", False, "skill catalog exceeds the size limit")]
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return [("gateway", False, "skill catalog is not valid JSON")]
    entries = payload.get("skills") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or not isinstance(entries, list) or len(entries) > 32):
        return [("gateway", False, "skill catalog schema is invalid")]

    ensure_skills_dir()
    results: list[tuple[str, bool, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            results.append(("gateway", False, "ignored malformed skill entry"))
            continue
        name = str(item.get("name") or "")
        description = str(item.get("description") or "").strip()
        revision = str(item.get("revision") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        manual = str(item.get("manual") or "").strip()
        client = (item.get("clients") or {}).get("laintas_cli") or {}
        triggers = client.get("trigger_patterns") or []
        valid = (
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name)
            and 1 <= len(description) <= 2000
            and re.fullmatch(r"gateway-[a-f0-9]{12}", revision)
            and re.fullmatch(r"[a-f0-9]{64}", digest)
            and 0 < len(manual.encode("utf-8")) <= GATEWAY_SKILL_MAX_MANUAL_BYTES
            and "[TODO" not in manual
            and isinstance(triggers, list) and len(triggers) <= 32
            and all(isinstance(value, str) and 0 < len(value) <= 120 for value in triggers)
        )
        if not valid:
            results.append((name or "gateway", False, "ignored invalid skill entry"))
            continue
        target, reason = claim_managed_dir(name, GATEWAY_SKILL_OWNER)
        if target is None:
            results.append((name, True, reason))
            continue
        try:
            if target.is_symlink():
                raise OSError("managed skill target is a symlink")
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker_path = target / MANAGED_MARKER
            skill_path = target / "SKILL.md"
            if marker_path.is_symlink() or skill_path.is_symlink():
                raise OSError("managed skill file is a symlink")
            marker = {
                "managed_by": GATEWAY_SKILL_OWNER,
                "revision": revision,
                "sha256": digest,
                "source": f"{profile.base_url}/api/skills",
            }
            json_store.save_json_atomic(marker_path, marker, mode=0o600)
            trigger_block = "".join(
                f"  - {json.dumps(value, ensure_ascii=False)}\n" for value in triggers
            )
            rendered = (
                "---\n"
                f"name: {name}\n"
                f"description: {json.dumps(description, ensure_ascii=False)}\n"
                f"version: {revision}\n"
                "triggers:\n"
                f"{trigger_block}"
                "---\n\n"
                f"{manual}\n"
            )
            temporary = target / ".SKILL.md.tmp"
            if temporary.is_symlink():
                raise OSError("managed skill temporary file is a symlink")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.chmod(0o600)
            os.replace(temporary, skill_path)
            results.append((name, True, f"synced {revision}"))
        except OSError as exc:
            results.append((name, False, f"could not install managed skill: {exc}"))
    return results


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
    managed_by: str = ""              # e.g. "org" — see MANAGED_MARKER
    scope: str = SCOPE_USER           # project / user / bundled
    #: Assertion lifecycle, mirroring memory_system: a learned skill cites the
    #: source it was learned from and goes `stale` when that source moves.
    status: str = "active"
    evidence: str = ""
    stale_reason: str = ""
    superseded_by: str = ""
    #: Utility counters. A skill library only stays useful if it is repairable,
    #: and nothing can be repaired that is not measured.
    helpful: int = 0
    harmful: int = 0


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
#: Which project the current scan was taken in. The catalogue is cwd-dependent
#: now, so a scan cached from another directory is not merely stale, it is
#: wrong — it would offer another repo's skills for this one.
_scan_project: str = ""


def _scan_stale() -> bool:
    """True when the catalogue must be rebuilt before it is read."""
    return not _scan_done or _scan_project != str(project_skills_dir())


def invalidate_scan() -> None:
    """Force the next catalogue read to rescan (a skill was written/changed)."""
    global _scan_done
    _scan_done = False


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

    def _count(key: str) -> int:
        try:
            return max(0, int(str(meta.get(key, "0")).strip() or 0))
        except (TypeError, ValueError):
            return 0

    status = str(meta.get("status", "active")).strip() or "active"
    if status not in ("active", "stale", "superseded"):
        status = "active"

    return SkillMetadata(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        trigger_patterns=triggers,
        version=meta.get("version", ""),
        dir_path=str(skill_dir),
        status=status,
        evidence=str(meta.get("evidence", "") or ""),
        stale_reason=str(meta.get("stale_reason", "") or ""),
        superseded_by=str(meta.get("superseded_by", "") or ""),
        helpful=_count("helpful"),
        harmful=_count("harmful"),
    ), body


# ── Metadata Scanning (Startup) ─────────────────────────────────────────

def scan_metadata() -> dict[str, SkillMetadata]:
    """Scan all skill directories and parse SKILL.md frontmatter.

    Called once at startup. Lightweight — only reads frontmatter,
    does NOT import skill.py or register tools.
    """
    global _skill_metadata, _scan_done, _scan_project
    ensure_skills_dir()
    ensure_bundled_skills_installed()
    _skill_metadata.clear()

    def _absorb(root: Path, scope: str, managed: bool = False) -> None:
        """Add every skill directory under ``root``, first writer wins.

        `setdefault`, not assignment: the scopes are visited in precedence
        order, so a project skill named `git` shadows the user's and the
        bundled one rather than being overwritten by them.
        """
        if not root.is_dir():
            return
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            # Must have either skill.py or SKILL.md
            if (not (child / "skill.py").is_file()
                    and not (child / "SKILL.md").is_file()):
                continue
            meta, _ = _parse_skill_md(child)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", meta.name):
                continue
            # A superseded skill stays on disk for the record but must not be
            # catalogued: keeping it visible is exactly the shadowing that
            # makes a growing library cost accuracy.
            if meta.status == "superseded":
                continue
            meta.scope = scope
            if managed:
                meta.managed_by = managed_owner(child)
            _skill_metadata.setdefault(meta.name, meta)

    _scan_project = str(project_skills_dir())
    _absorb(project_skills_dir(), SCOPE_PROJECT)
    _absorb(SKILLS_DIR, SCOPE_USER, managed=True)
    _absorb(BUNDLED_SKILLS_DIR, SCOPE_BUNDLED)

    _scan_done = True
    return _skill_metadata


def get_all_metadata() -> dict[str, SkillMetadata]:
    """Return all scanned metadata. Scans on first call, or after a `cd`."""
    if _scan_stale():
        scan_metadata()
    return _skill_metadata


def load_skill(name: str) -> tuple[bool, str]:
    """Public wrapper for explicitly loading a skill by name."""
    if _scan_stale():
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
    if _scan_stale():
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
    state.body = ""
    state.references.clear()
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
    if _scan_stale():
        scan_metadata()
    results: list[tuple[str, bool, str]] = []
    for name in loaded_skill_names():
        ok, msg = unload_skill(name)
        results.append((name, ok, msg))
    return results


def list_skills() -> list[dict]:
    """Return lightweight skill catalog for tools/UI."""
    if _scan_stale():
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
            "managed_by": meta.managed_by,
            "scope": meta.scope,
            "learned": is_learned(Path(meta.dir_path)),
            "status": meta.status,
            "evidence": meta.evidence,
            "stale_reason": meta.stale_reason,
            "helpful": meta.helpful,
            "harmful": meta.harmful,
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
    if _scan_stale():
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


# ── Learned skills ──────────────────────────────────────────────────────
#
# A skill the AGENT wrote, rather than one a human installed. The container is
# deliberately the same — same directory layout, same catalogue, same
# `skill.load` — because a second parallel store would need its own routing,
# its own UI and its own lifecycle, and would then have to compete with this
# one for the same slot in the prompt.
#
# Two properties are not negotiable:
#
#   * A learned skill is DOCUMENTATION ONLY. `write_learned_skill` writes
#     SKILL.md and nothing else, and refuses a directory that holds a skill.py
#     or a managed marker. An agent that could author skill.py would be an
#     agent that persists arbitrary code into every later session, behind a
#     trust prompt the user already answered for somebody else's code.
#   * It carries the evidence it was learned from and the same
#     active/stale/superseded lifecycle as a memory. A repo-specific lesson
#     whose repo has moved is exactly the "stale or harmful guidance" that
#     makes a growing skill library score below having no skills at all.

LEARNED_SKILL_MAX_BODY = 20_000
_LEARNED_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _scope_root(scope: str) -> Optional[Path]:
    if scope == SCOPE_PROJECT:
        return ensure_project_skills_dir()
    if scope == SCOPE_USER:
        return ensure_skills_dir()
    return None


#: Frontmatter stamp marking a skill as written by the agent rather than
#: installed by a human. Everything in this module edits files in place —
#: counters, staleness, retirement — and none of that may touch a SKILL.md a
#: person wrote and keeps in git. Absence of the stamp is the guard.
AUTHORED_BY_AGENT = "agent"


def is_learned(skill_dir: Path) -> bool:
    md = skill_dir / "SKILL.md"
    if not md.is_file() or (skill_dir / "skill.py").is_file():
        return False
    try:
        front, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
    except OSError:
        return False
    return str(front.get("authored_by") or "") == AUTHORED_BY_AGENT


def _format_skill_md(meta: dict, body: str) -> str:
    lines = ["---", f"name: {meta['name']}",
             f"description: {meta.get('description', '')}"]
    if meta.get("version"):
        lines.append(f"version: {meta['version']}")
    lines.append(f"authored_by: {AUTHORED_BY_AGENT}")
    if meta.get("evidence"):
        lines.append(f"evidence: {meta['evidence']}")
    status = str(meta.get("status") or "active")
    if status != "active":
        lines.append(f"status: {status}")
    if meta.get("stale_reason"):
        lines.append(f"stale_reason: {str(meta['stale_reason'])[:300]}")
    if meta.get("superseded_by"):
        lines.append(f"superseded_by: {meta['superseded_by']}")
    if meta.get("helpful"):
        lines.append(f"helpful: {int(meta['helpful'])}")
    if meta.get("harmful"):
        lines.append(f"harmful: {int(meta['harmful'])}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def write_learned_skill(name: str, description: str, body: str, *,
                        scope: str = SCOPE_PROJECT,
                        evidence: str = "") -> tuple[bool, str]:
    """Create or update a documentation-only skill written by the agent.

    Updating preserves the utility counters and, when ``evidence`` is not
    re-supplied, the existing evidence and status — the same rule memories
    follow, and for the same reason: rewriting the prose of a lesson whose
    source has moved is not re-checking that source.
    """
    name = str(name or "").strip()
    if not _LEARNED_NAME_RE.fullmatch(name):
        return False, ("skill name must be a lowercase slug, 2-64 chars "
                       "(letters, digits, hyphen)")
    description = " ".join(str(description or "").split())
    if not description:
        return False, "description is required — it is what routing ranks"
    body = str(body or "").strip()
    if not body:
        return False, "body is required"
    if len(body) > LEARNED_SKILL_MAX_BODY:
        return False, f"body exceeds {LEARNED_SKILL_MAX_BODY} chars"
    root = _scope_root(scope)
    if root is None:
        return False, f"scope must be '{SCOPE_PROJECT}' or '{SCOPE_USER}'"

    skill_dir = root / name
    if skill_dir.exists() and not skill_dir.is_dir():
        return False, f"{skill_dir} exists and is not a directory"
    if (skill_dir / "skill.py").is_file():
        return False, (f"'{name}' is an executable skill; this tool only "
                       f"writes documentation and will not touch it")
    if (skill_dir / MANAGED_MARKER).is_file():
        return False, (f"'{name}' is managed externally and would be "
                       f"overwritten on the next sync")
    if (skill_dir / "SKILL.md").is_file() and not is_learned(skill_dir):
        return False, (f"'{name}' was written by a person; pick another name "
                       f"rather than overwriting it")

    prior: dict = {}
    existing_md = skill_dir / "SKILL.md"
    if existing_md.is_file():
        try:
            prior, _ = _parse_frontmatter(existing_md.read_text(encoding="utf-8"))
        except OSError:
            prior = {}

    if evidence:
        status, stale_reason, evidence_value = "active", "", evidence
    else:
        evidence_value = str(prior.get("evidence") or "")
        status = str(prior.get("status") or "active")
        stale_reason = str(prior.get("stale_reason") or "")
    if status not in ("active", "stale", "superseded"):
        status = "active"

    meta = {
        "name": name,
        "description": description,
        "version": str(prior.get("version") or "1.0.0"),
        "evidence": evidence_value,
        "status": status,
        "stale_reason": stale_reason if status == "stale" else "",
        "superseded_by": str(prior.get("superseded_by") or ""),
        "helpful": prior.get("helpful") or 0,
        "harmful": prior.get("harmful") or 0,
    }
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        tmp = skill_dir / f".SKILL.md.{uuid.uuid4().hex}.tmp"
        tmp.write_text(_format_skill_md(meta, body), encoding="utf-8")
        os.replace(str(tmp), str(skill_dir / "SKILL.md"))
    except OSError as exc:
        return False, str(exc)
    invalidate_scan()
    _skill_states.pop(name, None)
    return True, str(skill_dir / "SKILL.md")


def _rewrite_learned_meta(name: str, updates: dict) -> tuple[bool, str]:
    """Change a learned skill's frontmatter, never its body."""
    meta_obj = get_all_metadata().get(name)
    search = ([Path(meta_obj.dir_path)] if meta_obj else []) + [
        project_skills_dir() / name, SKILLS_DIR / name]
    for skill_dir in search:
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        if (skill_dir / "skill.py").is_file():
            return False, f"'{name}' is an executable skill; not editable here"
        if not is_learned(skill_dir):
            return False, (f"'{name}' was installed, not learned — this tool "
                           f"does not edit human-authored skills")
        try:
            front, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        except OSError as exc:
            return False, str(exc)
        front.setdefault("name", name)
        front.update(updates)
        try:
            tmp = skill_dir / f".SKILL.md.{uuid.uuid4().hex}.tmp"
            tmp.write_text(_format_skill_md(front, body), encoding="utf-8")
            os.replace(str(tmp), str(md))
        except OSError as exc:
            return False, str(exc)
        invalidate_scan()
        _skill_states.pop(name, None)
        return True, str(md)
    return False, f"learned skill '{name}' not found"


def mark_skill_stale(name: str, reason: str) -> tuple[bool, str]:
    """Flag a learned skill as unverified because its cited source changed."""
    return _rewrite_learned_meta(
        name, {"status": "stale", "stale_reason": str(reason or "")[:300]})


def clear_skill_stale(name: str, evidence: str = "") -> tuple[bool, str]:
    updates = {"status": "active", "stale_reason": ""}
    if evidence:
        updates["evidence"] = evidence
    return _rewrite_learned_meta(name, updates)


def retire_skill(name: str, reason: str,
                 successor: str = "") -> tuple[bool, str]:
    """Retire a learned skill. The file stays; the catalogue entry does not.

    Retirement is the load-bearing half of a skill library. Selection accuracy
    falls monotonically with catalogue size, so a lesson that stopped being
    true costs every later task whether or not anything ever loads it.
    """
    if successor and successor == name:
        return False, "a skill cannot supersede itself"
    return _rewrite_learned_meta(
        name, {"status": "superseded", "superseded_by": successor,
               "stale_reason": str(reason or "")[:300]})


def record_outcome(names, helpful: bool) -> list[str]:
    """Increment the utility counters for skills that were loaded in a turn.

    The signal is deliberately crude — did the turn that had this skill loaded
    end well — because anything finer would need an attribution model, and a
    crude counter that actually accumulates beats a precise one that never
    does. What it is FOR is retirement: a skill whose harmful count outruns its
    helpful count is the one to look at first.
    """
    touched = []
    field_name = "helpful" if helpful else "harmful"
    for name in dict.fromkeys(str(n or "").strip() for n in (names or [])):
        if not name:
            continue
        meta = get_all_metadata().get(name)
        # Only agent-written skills are annotated. A shipped or human-authored
        # SKILL.md is somebody else's file — usually one kept in git — and
        # counting on it would rewrite it under them. _rewrite_learned_meta
        # refuses those anyway; skipping here keeps the no-op quiet.
        if meta is None or not is_learned(Path(meta.dir_path)):
            continue
        current = getattr(meta, field_name, 0)
        ok, _ = _rewrite_learned_meta(name, {field_name: int(current) + 1})
        if ok:
            touched.append(name)
    return touched


def learned_skills(include_retired: bool = False) -> list[dict]:
    """Every agent-written skill in scope, newest-looking first."""
    out = []
    for scope, root in ((SCOPE_PROJECT, project_skills_dir()),
                        (SCOPE_USER, SKILLS_DIR)):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            md = child / "SKILL.md"
            if not child.is_dir() or not md.is_file():
                continue
            if not is_learned(child):
                continue
            meta, _ = _parse_skill_md(child)
            if meta.status == "superseded" and not include_retired:
                continue
            meta.scope = scope
            out.append({
                "name": meta.name, "description": meta.description,
                "scope": scope, "status": meta.status,
                "evidence": meta.evidence, "stale_reason": meta.stale_reason,
                "superseded_by": meta.superseded_by,
                "helpful": meta.helpful, "harmful": meta.harmful,
                "path": str(md),
            })
    return out
