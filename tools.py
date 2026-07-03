"""Tool registry for laintas_cli — Phase 3a.

A Tool is a structured, AI-callable function with:
  - a name like "mem.read", "weather.get", "github.create_issue"
  - a JSONSchema-style param schema (for prompt rendering + validation)
  - an invoke(params, ctx) → {ok, result, error} callable
  - a source tag: "builtin" | "skill:<name>" | "mcp:<server>"

Tools live in a module-level singleton `_registry` so any module
(agent_loop, REPL meta-command handlers, skills, MCP client) can hit
the same registry. Built-in tools are registered when this module is
imported. Skills/MCP register lazily via skills.load_all() / mcp_client.connect().

Tools must NEVER block on network for more than a couple of seconds at
the abstraction layer — long operations should run in their own thread
and return a job id, or stream via events_cb.
"""

from __future__ import annotations

import json
import os
import re
import hashlib
import shutil
import stat
import subprocess
import sys
import time
import traceback
import difflib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import paths
from hwo_adapter import HWO_TOOL_DESCRIPTION


# ── Public dataclasses ─────────────────────────────────────────────────

@dataclass
class ToolCtx:
    """Runtime context passed to every tool invocation.

    Tools should treat ctx as read-only inputs; mutations to deps/session
    are not propagated back to the caller's state."""
    deps: Any = None                  # LoopDeps instance (read_file, etc.)
    agent_id: Optional[str] = None    # the agent calling this tool
    session: dict = field(default_factory=dict)
    events_cb: Optional[Callable] = None
    cwd: str = ""
    # ── Loop-local context (populated by agent_loop at dispatch time) ──
    interactive_session: Any = None
    stationed_terminal: Any = None    # SubTerminalSession of the agent's stationed terminal
    get_terminal: Optional[Callable] = None
    get_all_terminals: Optional[Callable] = None
    register_terminal: Optional[Callable] = None
    unregister_terminal: Optional[Callable] = None
    get_agent: Optional[Callable] = None
    get_all_agents: Optional[Callable] = None
    get_current_agent: Optional[Callable] = None
    station_agent: Optional[Callable] = None
    unstation_agent: Optional[Callable] = None
    send_to_agent: Optional[Callable] = None
    wait_for_agent: Optional[Callable] = None
    abort_agent: Optional[Callable] = None
    spawn_subagent: Optional[Callable] = None
    rename_agent: Optional[Callable] = None
    switch_to_agent: Optional[Callable] = None
    register_agent_fn: Optional[Callable] = None
    set_terminal_trigger: Optional[Callable] = None
    depth: int = 0


@dataclass
class Tool:
    name: str
    description: str
    schema: dict                                          # JSONSchema-ish
    invoke: Callable[[dict, ToolCtx], dict]               # → {ok,result,error}
    source: str = "builtin"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    trust_level: str = "untrusted"


def infer_capabilities(name: str) -> frozenset[str]:
    """Conservative capability labels used by policy and audit layers."""
    caps: set[str] = set()
    if name.startswith(("fs.read", "fs.ls", "fs.grep", "fs.glob")):
        caps.add("fs.read")
    if name.startswith(("fs.write", "fs.edit", "fs.multi_edit", "fs.delete")):
        caps.add("fs.write")
    if name.startswith(("shell.", "terminal.", "session.keys")):
        caps.add("process.exec")
    if name.startswith(("web.", "browser.")):
        caps.add("network")
    if name.startswith("browser.") and name not in {
            "browser.snapshot", "browser.query", "browser.get_url",
            "browser.get_title", "browser.screenshot"}:
        caps.add("browser.mutate")
    if name.startswith(("agent.", "spawn", "await_spawns")):
        caps.add("agent.control")
    return frozenset(caps or {"core.other"})


# ── Input validation ───────────────────────────────────────────────────

def _validate_params(params: dict, schema: dict) -> Optional[str]:
    """Validate ``params`` against a JSONSchema. Returns an error string or None.

    Uses ``jsonschema`` when available for full draft-07 validation; falls back
    to a lightweight required + type check so the gate works even without the
    dependency. Never raises — validation errors are returned as strings.
    """
    if not isinstance(params, dict):
        return f"expected object, got {type(params).__name__}"
    try:
        import jsonschema
        jsonschema.validate(params, schema)
        return None
    except ImportError:
        pass
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) or "(root)"
        return f"param '{path}': {e.message}"
    except Exception:
        pass
    # Lightweight fallback: check required + basic types
    required = schema.get("required") or []
    for r in required:
        if r not in params:
            return f"missing required param '{r}'"
    props = schema.get("properties") or {}
    _py_types = {"string": str, "integer": int, "number": (int, float),
                 "boolean": bool, "array": list, "object": dict}
    for k, v in params.items():
        if k not in props:
            continue
        ptype = (props[k] or {}).get("type")
        if ptype and ptype in _py_types:
            if not isinstance(v, _py_types[ptype]):
                return f"param '{k}': expected {ptype}, got {type(v).__name__}"
    return None


# ── Registry ───────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._builtin_names: set[str] = set()

    def register(self, tool: Tool, overwrite: bool = True) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", tool.name or ""):
            return False
        existing = self._tools.get(tool.name)
        if existing is not None and existing.source == "builtin" and tool.source != "builtin":
            return False
        if (existing is not None and tool.source != "builtin"
                and existing.source != tool.source):
            return False
        if not overwrite and tool.name in self._tools:
            return False
        if not tool.capabilities:
            tool.capabilities = infer_capabilities(tool.name)
        if tool.source == "builtin":
            tool.trust_level = "builtin"
            self._builtin_names.add(tool.name)
        self._tools[tool.name] = tool
        return True

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_source(self, source: str) -> int:
        """Drop every tool whose source equals `source`. Returns count removed."""
        victims = [n for n, t in self._tools.items() if t.source == source]
        for n in victims:
            del self._tools[n]
        return len(victims)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def list_by_source(self) -> dict[str, list[Tool]]:
        groups: dict[str, list[Tool]] = {}
        for t in self.list():
            groups.setdefault(t.source, []).append(t)
        return groups

    def invoke(self, name: str, params: dict, ctx: ToolCtx) -> dict:
        """Look up and invoke a tool. Catches exceptions; never raises.

        Validates params against the tool's JSONSchema before calling the
        tool's invoke callable. Returns shape:
        {ok: bool, result?: any, error?: str, tool: name}.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"tool '{name}' not found", "tool": name}
        if tool.source != "builtin" and tool.trust_level != "trusted-extension":
            return {
                "ok": False, "tool": name,
                "error": f"tool '{name}' is from an untrusted extension",
            }
        # ── Input validation (opencode Schema.Struct gate) ──
        schema = tool.schema
        if schema and isinstance(schema, dict) and schema.get("properties") is not None:
            _vErr = _validate_params(params or {}, schema)
            if _vErr:
                return {"ok": False, "tool": name, "error": _vErr, "_validation_error": True}
        try:
            invoke_ctx = ctx
            if tool.source != "builtin":
                # Never hand authentication state or control-plane callbacks to
                # extension code. Extensions receive only task identity and cwd;
                # stronger OS isolation remains an optional deployment boundary.
                invoke_ctx = ToolCtx(
                    agent_id=ctx.agent_id,
                    cwd=ctx.cwd,
                    depth=ctx.depth,
                    session={},
                )
            out = tool.invoke(params or {}, invoke_ctx)
            if not isinstance(out, dict):
                out = {"ok": True, "result": out}
            out.setdefault("ok", True)
            out["tool"] = name
            return out
        except Exception as e:
            return {
                "ok": False, "tool": name,
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=3),
            }

    def describe_for_prompt(self, indent: int = 2) -> str:
        """Render the toolset for inclusion in the AI system prompt.

        Format: grouped by source (builtin / skill:* / mcp:*), each tool on
        2–4 lines with name, description, required params, optional params
        and a usage example. This is far more token-efficient and
        teachable than a raw JSON dump.
        """
        groups = self.list_by_source()
        # Stable source ordering: builtin first, then skills, then MCP, then anything else.
        def _src_key(s: str) -> tuple:
            if s == "builtin":
                return (0, s)
            if s.startswith("skill:"):
                return (1, s)
            if s.startswith("mcp:"):
                return (2, s)
            return (3, s)

        lines: list[str] = []
        for src in sorted(groups, key=_src_key):
            tools = sorted(groups[src], key=lambda t: t.name)
            if src == "builtin":
                header = "## Built-in tools"
            elif src.startswith("skill:"):
                header = f"## Skill: {src.split(':', 1)[1]}"
            elif src.startswith("mcp:"):
                header = f"## MCP server: {src.split(':', 1)[1]}"
            else:
                header = f"## {src}"
            lines.append(header)

            for t in tools:
                props = (t.schema or {}).get("properties", {}) or {}
                required = set((t.schema or {}).get("required", []) or [])

                req_parts = []
                opt_parts = []
                for pname, pinfo in props.items():
                    pinfo = pinfo if isinstance(pinfo, dict) else {}
                    ptype = pinfo.get("type", "any")
                    if pname in required:
                        req_parts.append(f"{pname}:{ptype}")
                    else:
                        default = pinfo.get("default")
                        if default is not None:
                            opt_parts.append(f"{pname}:{ptype}={default!r}")
                        else:
                            opt_parts.append(f"{pname}?:{ptype}")

                desc = (t.description or "").strip().replace("\n", " ")
                if len(desc) > 240:
                    desc = desc[:237] + "..."

                lines.append(f"- {t.name} — {desc}")
                if req_parts:
                    lines.append(f"    required: {', '.join(req_parts)}")
                if opt_parts:
                    lines.append(f"    optional: {', '.join(opt_parts)}")
            lines.append("")  # blank line between groups

        return "\n".join(lines).rstrip()

    def describe_short_reminder(self) -> str:
        """One-line tool reminder for follow-up turns (saves prompt tokens).

        After turn 1, the model has already seen the full catalog and the
        examples; we only need to remind it of available names. If it tries
        an unknown name, the dispatch loop re-injects the full catalog.
        """
        names = sorted(self._tools.keys())
        n = len(names)
        # Show the most-used names verbatim; truncate the rest into a count.
        head = names[:18]
        tail_count = max(0, n - len(head))
        head_str = ", ".join(head)
        tail_str = f", … (+{tail_count} more — emit any name; unknown will re-show catalog)" if tail_count else ""
        return (
            f"## Tools available ({n})\n"
            f"Names: {head_str}{tail_str}\n"
            f"Call them via the native function-calling interface."
        )

    def to_openai_tools(self, unified: bool = False) -> tuple[list[dict], dict[str, str]]:
        """Render the toolset as OpenAI-style function-calling schemas.

        Returns ``(tools, name_map)`` where ``tools`` is a list of
        ``{"type":"function","function":{name, description, parameters}}`` and
        ``name_map`` maps each emitted wire name back to the original tool name.

        Tool names use ``.`` separators (e.g. ``fs.write``), but some providers
        (notably DeepSeek) reject function names containing ``.`` with a
        Provider Error. We mangle ``.`` → ``_`` on the way out and keep an
        explicit reverse map so native ``tool_calls`` can be un-mangled on the
        way back — we never rely on a global ``_`` → ``.`` rule, since names
        like ``agent_send`` and ``fs.multi_edit`` would make that ambiguous.

        When ``unified`` is True, tools that exist in the shared
        ``agent_tools`` catalog are emitted under their canonical flat name
        (``fs.read`` → ``read``) so every agent product speaks one taxonomy;
        the reverse map still points back to this registry's internal name, so
        dispatch is unchanged. Tools with no catalog alias (CLI-specific
        extensions) fall back to the mangled name.
        """
        _canon = None
        if unified:
            try:
                from agent_tools import load as _load_catalog
                _canon = _load_catalog()
            except Exception:
                _canon = None  # vendored catalog missing → graceful fallback
        tools: list[dict] = []
        name_map: dict[str, str] = {}
        used: set[str] = set()
        for t in self.list():
            wire = None
            if _canon is not None:
                wire = _canon.canonical(t.name, "laintas_cli")
            if not wire:
                wire = t.name.replace(".", "_")
            # Guarantee uniqueness within this request even if a future name
            # set collides after mangling.
            if wire in used:
                i = 2
                while f"{wire}_{i}" in used:
                    i += 1
                wire = f"{wire}_{i}"
            used.add(wire)
            name_map[wire] = t.name
            params = t.schema if isinstance(t.schema, dict) and t.schema else {
                "type": "object", "properties": {},
            }
            desc = (t.description or "").strip()
            if len(desc) > 1024:
                desc = desc[:1021] + "..."
            tools.append({
                "type": "function",
                "function": {
                    "name": wire,
                    "description": desc,
                    "parameters": params,
                },
            })
        return tools, name_map


# Module-level singleton — every consumer hits this.
_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _registry


def _make_unified_diff(old_text: str, new_text: str, label_a: str, label_b: str,
                       context: int = 3) -> str:
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=label_a,
        tofile=label_b,
        n=context,
    )
    return "".join(diff)


def _run_diagnostics(abs_path: str) -> Optional[str]:
    """Run the best-available native checker for ``abs_path`` and return its
    findings (or None when clean / no checker / unavailable). The agent-appropriate
    slice of LSP: after an edit, tell the model if it just introduced a
    syntax/lint error. Checker selection = shared vendored registry
    (diagnostics_adapter); execution stays here. Never raises."""
    try:
        from diagnostics_adapter import pick_checker, timeout_seconds, max_output_chars
    except Exception:
        return None
    try:
        cmd = pick_checker(abs_path)
        if not cmd:
            return None
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_seconds(), cwd=os.path.dirname(abs_path) or None,
        )
        if proc.returncode == 0:
            return None
        out = ((proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or "")).strip()
        if not out:
            return None
        cap = max_output_chars()
        if len(out) > cap:
            out = out[:cap] + "\n…(truncated)"
        return out
    except Exception:
        return None


def _attach_diagnostics(result: dict, abs_path: str) -> dict:
    """Append post-edit checker findings to a successful edit/write result so the
    model immediately sees errors it introduced. No-op when clean/unavailable."""
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    diag = _run_diagnostics(abs_path)
    if diag:
        result["diagnostics"] = diag
        base = result.get("result") or ""
        result["result"] = f"{base}\n\n[DIAGNOSTICS — errors detected in your edit; fix them]\n{diag}"
    return result


def _run_formatter(abs_path: str) -> bool:
    """Run the best-available in-place code formatter for ``abs_path`` (shared
    vendored registry: format_adapter). No-op (returns False) when disabled, no
    formatter is installed, or on any error. Applied to full-file WRITES only —
    surgical edits stay byte-precise. Never raises."""
    try:
        from agent_loop import get_runtime_config as _grc
        if not _grc("auto_format"):
            return False
    except Exception:
        pass
    try:
        from format_adapter import pick_formatter, timeout_seconds
    except Exception:
        return False
    try:
        cmd = pick_formatter(abs_path)
        if not cmd:
            return False
        subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout_seconds(), cwd=os.path.dirname(abs_path) or None)
        return True
    except Exception:
        return False


# ── Built-in tools ─────────────────────────────────────────────────────
# Kept intentionally minimal — duplicating existing meta-commands (/spawn,
# /term, /keys) here would be confusing. Built-ins fill gaps the meta-command
# layer doesn't cover: filesystem reads, memory introspection.

def _bi_mem_read(params: dict, ctx: ToolCtx) -> dict:
    deps = ctx.deps
    if deps is None or not hasattr(deps, "read_file"):
        return {"ok": False, "error": "no deps.read_file available"}
    content = deps.read_file(str(paths.project_file(paths.CWD_MEMORY))) or ""
    return {"ok": True, "result": content}


def _bi_mem_save(params: dict, ctx: ToolCtx) -> dict:
    if _mem_sys is None:
        return {"ok": False, "error": "memory_system module not available"}
    name = params.get("name", "").strip()
    mem_type = params.get("type", "project")
    description = params.get("description", "").strip()
    body = params.get("body", "").strip()
    scope = params.get("scope") or None
    importance = params.get("importance", 0.5)
    if not name or not body:
        return {"ok": False, "error": "missing 'name' or 'body'"}
    ok, msg = _mem_sys.write_memory(
        name, mem_type, description, body,
        scope=scope, importance=importance,
    )
    return {"ok": ok, "result": msg if ok else "", "error": "" if ok else msg}


def _bi_mem_delete(params: dict, ctx: ToolCtx) -> dict:
    if _mem_sys is None:
        return {"ok": False, "error": "memory_system module not available"}
    name = params.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    ok, msg = _mem_sys.delete_memory(name)
    return {"ok": ok, "result": msg if ok else "", "error": "" if ok else msg}


def _bi_mem_list(params: dict, ctx: ToolCtx) -> dict:
    if _mem_sys is None:
        return {"ok": False, "error": "memory_system module not available"}
    mem_type = params.get("type") or None
    entries = _mem_sys.list_memories(mem_type)
    return {"ok": True, "result": entries, "count": len(entries)}


def _bi_skill_list(params: dict, ctx: ToolCtx) -> dict:
    """List skills available for explicit progressive loading."""
    import skills as _skills
    items = _skills.list_skills()
    return {"ok": True, "result": items, "count": len(items)}


def _bi_skill_load(params: dict, ctx: ToolCtx) -> dict:
    """Load a skill body into subsequent prompt context."""
    import skills as _skills
    name = (params.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    ok, msg = _skills.load_skill(name)
    if not ok:
        return {"ok": False, "error": msg}
    loaded = next((s for s in _skills.list_skills() if s["name"] == name), None)
    return {
        "ok": True,
        "result": msg,
        "skill": loaded or {"name": name, "loaded": True},
        "instruction": "The skill is now loaded. Continue the task using its instructions from the next model turn.",
    }


def _bi_skill_unload(params: dict, ctx: ToolCtx) -> dict:
    """Unload a previously loaded skill, freeing its tools and context."""
    import skills as _skills
    name = (params.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    ok, msg = _skills.unload_skill(name)
    if not ok:
        return {"ok": False, "error": msg}
    return {
        "ok": True,
        "result": msg,
        "instruction": "The skill is now unloaded; its instructions and tools are no longer available from the next turn.",
    }


def _bi_fs_read(params: dict, ctx: ToolCtx) -> dict:
    """Read a file as UTF-8 with optional line range and cat-style numbering.

    params:
      path:       file path (required)
      offset:     1-based starting line (default 1)
      limit:      max lines to return (default 2000)
      max_bytes:  hard byte cap on returned payload (default 200_000)
      line_numbers: prepend each line with "N→ " (default True)

    Prefer offset/limit over
    max_bytes for large files; line numbers make follow-up fs.edit calls
    trivial because the AI can refer to exact lines.
    """
    path = params.get("path")
    if not path:
        return {"ok": False, "error": "missing 'path'"}
    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path)) \
        if not os.path.isabs(path) else path

    offset = max(1, int(params.get("offset", 1) or 1))
    limit = max(1, int(params.get("limit", 2000) or 2000))
    max_bytes = int(params.get("max_bytes", 200_000) or 200_000)
    line_numbers = bool(params.get("line_numbers", True))

    try:
        with open(abs_path, "rb") as f:
            raw = f.read(max_bytes + 1)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    byte_truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    all_lines = text.split("\n")
    total_lines = len(all_lines)

    start_idx = offset - 1
    end_idx = min(start_idx + limit, total_lines)
    selected = all_lines[start_idx:end_idx]

    line_truncated = end_idx < total_lines
    if line_numbers:
        width = len(str(end_idx))
        body = "\n".join(
            f"{(start_idx + i + 1):>{width}}→{ln}" for i, ln in enumerate(selected)
        )
    else:
        body = "\n".join(selected)

    return {
        "ok": True,
        "result": body,
        "path": abs_path,
        "offset": offset,
        "lines_returned": len(selected),
        "total_lines": total_lines,
        "truncated": byte_truncated or line_truncated,
        "byte_truncated": byte_truncated,
    }


def _check_file_write_policy(abs_path: str, ctx: ToolCtx, diff_preview: str) -> Optional[dict]:
    """Run a write target through policy.evaluate_file_write() before any bytes hit disk.

    Returns an {"ok": False, ...} dict to block the write, or None to proceed.
    "needs_approval" blocks only if a request_file_write_approval callback is
    wired on ctx.deps (interactive REPL, or remote delegate via _request_approval);
    without one (headless/automated contexts with no human to ask), it proceeds —
    the decision is still audited, it just can't be confirmed live.
    """
    if _policy_mod is None:
        return None
    try:
        decision = _policy_mod.evaluate_file_write(abs_path, ctx.cwd, agent_id=ctx.agent_id)
    except Exception:
        return None
    if decision.action == "deny":
        return {"ok": False, "error": f"Blocked by policy: {decision.reason}", "path": abs_path}
    if decision.action == "needs_approval":
        approve_fn = getattr(ctx.deps, "request_file_write_approval", None) if ctx.deps is not None else None
        if callable(approve_fn):
            try:
                approved = approve_fn(abs_path, diff_preview, decision.reason)
            except Exception:
                approved = False
            if not approved:
                return {"ok": False, "error": f"User denied write: {decision.reason}", "path": abs_path, "_user_denied": True}
    return None


def _check_file_delete_policy(abs_path: str, ctx: ToolCtx,
                              preview: str) -> Optional[dict]:
    """Authorize deletion before any filesystem mutation occurs."""
    if _policy_mod is None:
        return None
    try:
        decision = _policy_mod.evaluate_file_delete(
            abs_path, ctx.cwd, agent_id=ctx.agent_id)
    except Exception as exc:
        return {"ok": False, "error": f"Delete policy failed: {exc}",
                "path": abs_path}
    if decision.action == "deny":
        return {"ok": False,
                "error": f"Blocked by policy: {decision.reason}",
                "path": abs_path}
    if decision.action == "needs_approval":
        approve_fn = (getattr(ctx.deps, "request_file_delete_approval", None)
                      if ctx.deps is not None else None)
        if not callable(approve_fn):
            return {"ok": False,
                    "error": "Deletion requires approval but no approval channel is available",
                    "path": abs_path}
        try:
            approved = approve_fn(abs_path, preview, decision.reason)
        except Exception:
            approved = False
        if not approved:
            return {"ok": False,
                    "error": f"User denied deletion: {decision.reason}",
                    "path": abs_path,
                    "_user_denied": True}
    return None


_DELETE_ENTRY_LIMIT = 10_000
_DELETE_PREVIEW_LIMIT = 80


def _describe_delete_target(abs_path: str) -> tuple[dict, Optional[str]]:
    """Return a bounded preview and fingerprint without following symlinks."""
    try:
        root_stat = os.lstat(abs_path)
    except OSError as exc:
        return {}, str(exc)

    digest = hashlib.sha256()
    entries: list[str] = []
    count = 1

    def _add(rel: str, st, suffix: str = "") -> None:
        nonlocal count
        encoded = (f"{rel}\0{st.st_dev}\0{st.st_ino}\0{st.st_mode}\0"
                   f"{st.st_size}\0{st.st_mtime_ns}").encode(
                       "utf-8", errors="surrogateescape")
        digest.update(encoded)
        if len(entries) < _DELETE_PREVIEW_LIMIT:
            entries.append(rel + suffix)

    is_link = stat.S_ISLNK(root_stat.st_mode)
    is_dir = stat.S_ISDIR(root_stat.st_mode) and not is_link
    kind = "symlink" if is_link else "directory" if is_dir else "file"
    _add(".", root_stat, "/" if is_dir else "")

    if is_dir:
        def _raise_walk_error(exc):
            raise exc
        try:
            for base, dirs, files in os.walk(
                    abs_path, followlinks=False, onerror=_raise_walk_error):
                dirs.sort()
                files.sort()
                for name in dirs + files:
                    full = os.path.join(base, name)
                    rel = os.path.relpath(full, abs_path)
                    try:
                        item_stat = os.lstat(full)
                    except OSError as exc:
                        return {}, f"Cannot inspect '{rel}': {exc}"
                    count += 1
                    if count > _DELETE_ENTRY_LIMIT:
                        return {}, (
                            f"Refusing to delete more than {_DELETE_ENTRY_LIMIT} entries "
                            "in one operation")
                    _add(rel, item_stat,
                         "/" if stat.S_ISDIR(item_stat.st_mode)
                         and not stat.S_ISLNK(item_stat.st_mode) else "")
        except OSError as exc:
            return {}, str(exc)

    preview_lines = [
        f"DELETE {kind}: {abs_path}",
        f"Entries: {count}",
        "Contents:",
        *[f"  - {entry}" for entry in entries],
    ]
    if count > len(entries):
        preview_lines.append(
            f"  ... {count - len(entries)} additional entries")
    return {
        "kind": kind,
        "count": count,
        "fingerprint": digest.hexdigest(),
        "preview": "\n".join(preview_lines),
    }, None


def _bi_fs_delete(params: dict, ctx: ToolCtx) -> dict:
    """Delete one file, symlink, or directory after explicit policy approval."""
    path = params.get("path")
    recursive = bool(params.get("recursive", False))
    if not path:
        return {"ok": False, "error": "missing 'path'"}
    abs_path = (os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path))
                if not os.path.isabs(path) else os.path.abspath(path))
    if not os.path.lexists(abs_path):
        return {"ok": False, "error": f"Path does not exist: {abs_path}",
                "path": abs_path}

    before, error = _describe_delete_target(abs_path)
    if error:
        return {"ok": False, "error": error, "path": abs_path}
    if before["kind"] == "directory" and before["count"] > 1 and not recursive:
        return {"ok": False,
                "error": "Directory is not empty; set recursive=true to delete it",
                "path": abs_path, "entries": before["count"]}

    blocked = _check_file_delete_policy(abs_path, ctx, before["preview"])
    if blocked is not None:
        return blocked

    # Detect replacements or content changes while the user was reviewing the
    # confirmation.  Never apply an approval to a different target snapshot.
    after, error = _describe_delete_target(abs_path)
    if error:
        return {"ok": False,
                "error": f"Delete target changed before execution: {error}",
                "path": abs_path}
    if after["fingerprint"] != before["fingerprint"]:
        return {"ok": False,
                "error": "Delete target changed while awaiting approval; review again",
                "path": abs_path}

    try:
        if before["kind"] == "directory":
            if recursive:
                shutil.rmtree(abs_path)
            else:
                os.rmdir(abs_path)
        else:
            os.unlink(abs_path)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": abs_path}

    return {"ok": True,
            "result": f"Deleted {before['kind']} {abs_path}",
            "path": abs_path, "kind": before["kind"],
            "entries_deleted": before["count"]}


def _bi_fs_write(params: dict, ctx: ToolCtx) -> dict:
    path = params.get("path")
    content = params.get("content", "")
    if not path:
        return {"ok": False, "error": "missing 'path'"}
    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path)) \
        if not os.path.isabs(path) else path
    old_content = ""
    existed = False
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            old_content = f.read()
        existed = True
    except FileNotFoundError:
        old_content = ""
    except OSError as e:
        return {"ok": False, "error": str(e)}

    diff = _make_unified_diff(
        old_content,
        content,
        abs_path if existed else f"{abs_path} (new)",
        abs_path,
    )

    blocked = _check_file_write_policy(abs_path, ctx, diff or "(no differences)")
    if blocked is not None:
        return blocked

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    action = "updated" if existed else "created"
    if _run_formatter(abs_path):
        try:
            with open(abs_path, "r", encoding="utf-8") as _ff:
                _formatted = _ff.read()
            if _formatted != content:
                diff = _make_unified_diff(old_content, _formatted, abs_path, abs_path)
                content = _formatted
        except OSError:
            pass
    return _attach_diagnostics({
        "ok": True,
        "result": f"{action} {abs_path} ({len(content)} bytes)",
        "path": abs_path,
        "changed": old_content != content,
        "diff": diff or "(no differences)",
    }, abs_path)


def _bi_fs_ls(params: dict, ctx: ToolCtx) -> dict:
    path = params.get("path", ".")
    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path)) \
        if not os.path.isabs(path) else path
    try:
        entries = []
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            entries.append({
                "name": name,
                "type": "dir" if os.path.isdir(full) else "file",
                "size": os.path.getsize(full) if os.path.isfile(full) else None,
            })
        return {"ok": True, "result": entries, "path": abs_path}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def _bi_time_now(params: dict, ctx: ToolCtx) -> dict:
    return {"ok": True, "result": {"epoch": time.time(),
                                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}


def _bi_task_create(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    subject = params.get("subject", "").strip()
    description = params.get("description", "").strip()
    if not subject:
        return {"ok": False, "error": "missing 'subject'"}
    try:
        task = _task_mgr.create_task(
            subject, description,
            metadata=params.get("metadata"),
            session_only=params.get("session_only", False),
            parent_task_id=params.get("parent_task_id"),
            cwd=ctx.cwd or None,
        )
    except _task_mgr.TaskStorageError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": task}


def _bi_task_update(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    task_id = params.get("id", "")
    if not task_id:
        return {"ok": False, "error": "missing 'id'"}
    kwargs = {}
    for k in ("status", "subject", "description", "metadata",
              "addBlocks", "addBlockedBy", "removeBlocks", "removeBlockedBy",
              "progress", "notes", "addSubtask"):
        if k in params:
            kwargs[k] = params[k]
    try:
        ok, msg, task = _task_mgr.update_task(
            str(task_id), cwd=ctx.cwd or None, **kwargs)
    except _task_mgr.TaskStorageError as exc:
        return {"ok": False, "result": None, "error": str(exc)}
    return {"ok": ok, "result": task if ok else None, "error": "" if ok else msg}


def _bi_task_list(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    status = params.get("status") or None
    available = params.get("available", False)
    if available:
        tasks = _task_mgr.get_available_tasks(cwd=ctx.cwd or None)
    else:
        tasks = _task_mgr.list_tasks(status=status, cwd=ctx.cwd or None)
    return {"ok": True, "result": tasks, "count": len(tasks)}


def _bi_task_get(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    task_id = params.get("id", "")
    if not task_id:
        return {"ok": False, "error": "missing 'id'"}
    task = _task_mgr.get_task(str(task_id), cwd=ctx.cwd or None)
    if task is None:
        return {"ok": False, "error": f"Task '{task_id}' not found"}
    return {"ok": True, "result": task}


def _bi_plan_read(params: dict, ctx: ToolCtx) -> dict:
    if _plan_mod is None:
        return {"ok": False, "error": "plan_mode module not available"}
    name = params.get("name") or None
    content = _plan_mod.read_plan(name)
    if content is None:
        return {"ok": False, "error": "No plan available"}
    return {"ok": True, "result": content, "plan_mode": _plan_mod.is_plan_mode()}


def _bi_plan_update(params: dict, ctx: ToolCtx) -> dict:
    if _plan_mod is None:
        return {"ok": False, "error": "plan_mode module not available"}
    content = params.get("content", "")
    if not content:
        return {"ok": False, "error": "missing 'content'"}
    ok = _plan_mod.update_plan(content)
    return {"ok": ok, "result": "Plan updated" if ok else "",
            "error": "" if ok else "No active plan to update"}


def _bi_plan_list(params: dict, ctx: ToolCtx) -> dict:
    if _plan_mod is None:
        return {"ok": False, "error": "plan_mode module not available"}
    plans = _plan_mod.list_plans()
    return {"ok": True, "result": plans, "count": len(plans)}


def _bi_plan_submit(params: dict, ctx: ToolCtx) -> dict:
    """Declare the current immutable plan revision ready for user review."""
    if _plan_mod is None:
        return {"ok": False, "error": "plan_mode module not available"}
    snapshot = _plan_mod.submit_current_plan()
    if not snapshot:
        return {"ok": False, "error": (
            "Plan could not be submitted. Ensure it is substantial and has an active revision.")}
    revision = snapshot["revision"]
    return {
        "ok": True,
        "result": (
            f"Plan revision {revision['revision']} submitted for user review. "
            "Stop planning; the CLI will display the approval dialog."
        ),
        "_plan_submitted": True,
        "work_id": snapshot["work"]["id"],
        "revision": revision["revision"],
        "content_sha": revision["content_sha"],
    }


def _bi_prompt_feedback(params: dict, ctx: ToolCtx) -> dict:
    """Capture feedback and spawn a background optimizer sub-agent."""
    if _prompt_opt_mod is None:
        return {"ok": False, "error": "prompt_opt module not available"}
    description = params.get("description", "")
    if not description:
        return {"ok": False, "error": "missing 'description'"}
    entry = _prompt_opt_mod.capture_feedback(description)
    # Try to spawn optimizer if we can reach agent_loop + current agent.
    try:
        from agent_loop import spawn_subagent
        from laintas_cli import get_current_agent, get_loop_deps
        parent = get_current_agent()
        if parent:
            child_id = _prompt_opt_mod.spawn_optimizer(
                entry["id"], parent.id, get_loop_deps(), None)
            return {"ok": True, "result": f"Feedback captured ({entry['id']}), "
                    f"optimizer spawned: {child_id}", "feedback_id": entry["id"],
                    "child_agent_id": child_id}
    except Exception:
        pass
    return {"ok": True, "result": f"Feedback captured ({entry['id']}). "
            "No optimizer spawned (no active agent).", "feedback_id": entry["id"]}


def _bi_prompt_draft(params: dict, ctx: ToolCtx) -> dict:
    """Write a candidate patch file. Used by the optimizer sub-agent."""
    if _prompt_opt_mod is None:
        return {"ok": False, "error": "prompt_opt module not available"}
    feedback_id = params.get("feedback_id", "")
    patch = params.get("patch", "")
    rationale = params.get("rationale", "")
    if not patch and not rationale:
        return {"ok": False, "error": "missing 'patch' and 'rationale'"}
    if not feedback_id:
        return {"ok": False, "error": "missing 'feedback_id'"}
    cand = _prompt_opt_mod.draft_candidate(feedback_id, patch, rationale)
    return {"ok": True, "result": f"Candidate {cand['id']} drafted.",
            "candidate_id": cand["id"]}


def _bi_prompt_review(params: dict, ctx: ToolCtx) -> dict:
    """Read a candidate (or the active one) for review."""
    if _prompt_opt_mod is None:
        return {"ok": False, "error": "prompt_opt module not available"}
    cid = params.get("id") or None
    cand = _prompt_opt_mod.read_candidate(cid)
    if not cand:
        return {"ok": False, "error": "No candidate found"}
    return {"ok": True, "result": cand}


def _bi_prompt_apply(params: dict, ctx: ToolCtx) -> dict:
    """Apply a candidate patch to cli.prop."""
    return {"ok": False, "error": (
        "Applying prompt candidates requires explicit user approval. "
        "Ask the user to run /prompt apply [id].")}


def _bi_prompt_discard(params: dict, ctx: ToolCtx) -> dict:
    """Strip the applied patch from cli.prop."""
    return {"ok": False, "error": (
        "Discarding prompt candidates requires explicit user approval. "
        "Ask the user to run /prompt discard [id].")}


def _bi_prompt_structured_feedback(params: dict, ctx: ToolCtx) -> dict:
    """Capture a structured failure report (v3 template) and spawn optimizer."""
    if _prompt_opt_mod is None:
        return {"ok": False, "error": "prompt_opt module not available"}
    fields = {
        "task": params.get("task", ""),
        "expected": params.get("expected", ""),
        "actual": params.get("actual", ""),
        "category": params.get("category", ""),
        "minimal_fix": params.get("minimal_fix", ""),
        "regression_tests": params.get("regression_tests", ""),
    }
    if not fields["task"] and not fields["actual"]:
        return {"ok": False, "error": "missing 'task' and 'actual' (at least one required)"}
    if fields["category"] and fields["category"] not in _prompt_opt_mod.FAILURE_CATEGORIES:
        return {"ok": False, "error": f"invalid category '{fields['category']}'. Valid: {_prompt_opt_mod.FAILURE_CATEGORIES}"}
    entry = _prompt_opt_mod.capture_structured_failure(fields)
    try:
        from agent_loop import spawn_subagent
        from laintas_cli import get_current_agent, get_loop_deps
        parent = get_current_agent()
        if parent:
            child_id = _prompt_opt_mod.spawn_optimizer(
                entry["id"], parent.id, get_loop_deps(), None)
            return {"ok": True, "result": f"Structured feedback captured ({entry['id']}), "
                    f"optimizer spawned: {child_id}", "feedback_id": entry["id"],
                    "child_agent_id": child_id}
    except Exception:
        pass
    return {"ok": True, "result": f"Structured feedback captured ({entry['id']}). "
            "No optimizer spawned (no active agent).", "feedback_id": entry["id"]}


def _bi_prompt_skill_patch(params: dict, ctx: ToolCtx) -> dict:
    """Draft a skill patch candidate. Used by the optimizer sub-agent."""
    if _prompt_opt_mod is None:
        return {"ok": False, "error": "prompt_opt module not available"}
    skill_name = params.get("skill_name", "")
    skill_file = params.get("skill_file", "SKILL.md")
    mode = params.get("mode", "append")
    patch = params.get("patch", "")
    rationale = params.get("rationale", "")
    feedback_id = params.get("feedback_id", "")
    old_string = params.get("old_string", "")
    new_string = params.get("new_string", "")
    if not skill_name:
        return {"ok": False, "error": "missing 'skill_name'"}
    if not feedback_id:
        return {"ok": False, "error": "missing 'feedback_id'"}
    if mode == "append" and not patch:
        return {"ok": False, "error": "missing 'patch' (required for append mode)"}
    if mode == "replace" and (not old_string or not new_string):
        return {"ok": False, "error": "missing 'old_string' or 'new_string' (required for replace mode)"}
    if mode not in ("append", "replace"):
        return {"ok": False, "error": f"invalid mode '{mode}'. Use 'append' or 'replace'."}
    try:
        cand = _prompt_opt_mod.draft_skill_patch(
            skill_name=skill_name, skill_file=skill_file, mode=mode,
            patch=patch, rationale=rationale, feedback_id=feedback_id,
            old_string=old_string, new_string=new_string)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": f"Skill patch {cand['id']} drafted for "
            f"{skill_name}/{skill_file}.", "candidate_id": cand["id"]}


def _bi_prompt_skill_apply(params: dict, ctx: ToolCtx) -> dict:
    """Apply a skill patch candidate."""
    return {"ok": False, "error": (
        "Applying skill patches requires explicit user approval. "
        "Ask the user to run /prompt skill apply <id>.")}


def _bi_prompt_skill_discard(params: dict, ctx: ToolCtx) -> dict:
    """Discard a skill patch candidate (restore from backup)."""
    return {"ok": False, "error": (
        "Discarding skill patches requires explicit user approval. "
        "Ask the user to run /prompt skill discard <id>.")}


def _bi_prompt_lab_draft(params: dict, ctx: ToolCtx) -> dict:
    """Draft a project-scoped Prompt Lab overlay; activation stays user-only."""
    if _prompt_lab_mod is None:
        return {"ok": False, "error": "prompt_lab module not available"}
    agent = ctx.get_agent(ctx.agent_id) if ctx.get_agent and ctx.agent_id else None
    lab_root = ((agent.state or {}).get("_prompt_lab_root") if agent else None)
    try:
        with _prompt_lab_mod.project_scope(lab_root):
            patch = _prompt_lab_mod.draft_patch(
                branch_id=str(params.get("branch_id") or ""),
                title=str(params.get("title") or ""),
                content=str(params.get("content") or ""),
                rationale=str(params.get("rationale") or ""),
                diagnosis=str(params.get("diagnosis") or ""),
                tests=params.get("tests") if isinstance(params.get("tests"), list) else [],
            )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "result": (
            f"Prompt Lab patch {patch['id']} drafted. It is NOT active. "
            f"Ask the user to run /prompt test {patch['id']} and review it."
        ),
        "patch_id": patch["id"],
    }


def _bi_evolve_lab_draft(params: dict, ctx: ToolCtx) -> dict:
    """Draft an Evolution Lab feature candidate; activation stays user-only."""
    if _evolution_lab_mod is None:
        return {"ok": False, "error": "evolution_lab module not available"}
    agent = ctx.get_agent(ctx.agent_id) if ctx.get_agent and ctx.agent_id else None
    lab_root = ((agent.state or {}).get("_evolution_lab_root") if agent else None)
    try:
        with _evolution_lab_mod.project_scope(lab_root):
            candidate = _evolution_lab_mod.draft_candidate(
                branch_id=str(params.get("branch_id") or ""),
                title=str(params.get("title") or ""),
                target_type=str(params.get("target_type") or "extension"),
                name=str(params.get("name") or ""),
                files=params.get("files") if isinstance(params.get("files"), list) else [],
                description=str(params.get("description") or ""),
                dependencies=(params.get("dependencies")
                              if isinstance(params.get("dependencies"), list) else []),
                tests=params.get("tests") if isinstance(params.get("tests"), list) else [],
            )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "result": (
            f"Evolution candidate {candidate['id']} drafted. It is not active. "
            f"Ask the user to run /evolve test {candidate['id']} and review it."
        ),
        "candidate_id": candidate["id"],
    }


def _bi_fs_edit(params: dict, ctx: ToolCtx) -> dict:
    """Exact string replacement in a file.

    Finds old_string in the file and replaces it with new_string.
    If old_string is not unique, requires replace_all: true.
    """
    path = params.get("path")
    old = params.get("old_string", "")
    new = params.get("new_string", "")
    replace_all = params.get("replace_all", False)

    if not path:
        return {"ok": False, "error": "missing 'path'"}
    if old == new:
        return {"ok": False, "error": "old_string and new_string are identical"}

    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path)) \
        if not os.path.isabs(path) else path

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return {"ok": False, "error": str(e)}

    count = content.count(old)
    _fuzzy_strategy = None
    if count == 0:
        # Exact match failed — try opencode's fuzzy replacers (shared, vendored)
        # so an edit still lands when old_string differs only in whitespace or
        # indentation (the #1 cause of edit failures). Only the matching is
        # shared; the file I/O stays here.
        try:
            from patch_adapter import apply_edit as _apply_edit
            _fuzzy_new, _fuzzy_strategy = _apply_edit(content, old, new, replace_all)
        except Exception:
            _fuzzy_new, _fuzzy_strategy = None, None
        if _fuzzy_new is None:
            return {"ok": False, "error": "old_string not found in file",
                    "hint": "Check exact whitespace and indentation"}
        new_content = _fuzzy_new
        diff = _make_unified_diff(content, new_content, abs_path, abs_path)
        blocked = _check_file_write_policy(abs_path, ctx, diff or "(no differences)")
        if blocked is not None:
            return blocked
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return _attach_diagnostics({"ok": True, "result": f"Edited {path} (fuzzy match: {_fuzzy_strategy})",
                "diff": diff, "tool": "fs.edit", "fuzzy": _fuzzy_strategy}, abs_path)

    if count > 1 and not replace_all:
        # Find line numbers for the first 3 occurrences
        lines_with_match = []
        for i, line in enumerate(content.split('\n'), 1):
            if old in line:
                lines_with_match.append(i)
                if len(lines_with_match) >= 3:
                    break
        return {
            "ok": False,
            "error": f"old_string appears {count} times (not unique). "
                     f"Use replace_all: true or add more context to make it unique.",
            "occurrences": count,
            "first_at_lines": lines_with_match,
        }

    new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)

    diff = _make_unified_diff(content, new_content, abs_path, abs_path)

    blocked = _check_file_write_policy(abs_path, ctx, diff or "(no differences)")
    if blocked is not None:
        return blocked

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    return _attach_diagnostics({
        "ok": True,
        "result": f"Replaced {count} occurrence(s) in {abs_path}",
        "path": abs_path,
        "replacements": count,
        "changed": content != new_content,
        "diff": diff or "(no differences)",
    }, abs_path)


def _bi_fs_multi_edit(params: dict, ctx: ToolCtx) -> dict:
    """Apply multiple sequential exact-string edits to one file atomically.

    Each edit is applied in order to the result of the previous one; if any
    edit fails (string not found, ambiguous without replace_all), the whole
    operation rolls back and the file is left untouched.

    params:
      path:  file path (required)
      edits: list of {old_string, new_string, replace_all?} (required, >=1)
    """
    path = params.get("path")
    edits = params.get("edits") or []
    if not path:
        return {"ok": False, "error": "missing 'path'"}
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "error": "'edits' must be a non-empty array"}

    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path)) \
        if not os.path.isabs(path) else path

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return {"ok": False, "error": str(e)}

    working = content
    applied = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return {"ok": False, "error": f"edit #{i+1} is not an object"}
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        replace_all = bool(edit.get("replace_all", False))
        if old == new:
            return {"ok": False, "error": f"edit #{i+1}: old_string equals new_string"}
        count = working.count(old)
        if count == 0:
            return {"ok": False, "error": f"edit #{i+1}: old_string not found"}
        if count > 1 and not replace_all:
            return {"ok": False,
                    "error": f"edit #{i+1}: old_string appears {count} times "
                             f"(set replace_all:true or add more context)"}
        working = working.replace(old, new) if replace_all else working.replace(old, new, 1)
        applied.append({"index": i + 1, "replacements": count if replace_all else 1})

    diff = _make_unified_diff(content, working, abs_path, abs_path)

    blocked = _check_file_write_policy(abs_path, ctx, diff or "(no differences)")
    if blocked is not None:
        return blocked

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(working)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    return _attach_diagnostics({"ok": True, "result": f"Applied {len(applied)} edits to {abs_path}",
            "path": abs_path, "edits_applied": applied,
            "changed": content != working, "diff": diff or "(no differences)"}, abs_path)


def _bi_fs_diff(params: dict, ctx: ToolCtx) -> dict:
    """Compute a unified diff between two files or between a file and a string.

    params:
      a:        path to file A (required)
      path:     git working-tree diff for this path (compatibility alias)
      b:        path to file B (optional)
      b_text:   raw text to compare against A (alternative to b)
      context:  context lines (default 3)
      label_a:  display label for A (default = path)
      label_b:  display label for B (default = path or "<inline>")
    """
    a = params.get("a")
    compat_path = params.get("path")
    b = params.get("b")
    b_text = params.get("b_text")
    if not a and compat_path:
        if b or b_text is not None:
            a = compat_path
        else:
            base = ctx.cwd or os.getcwd()
            try:
                proc = subprocess.run(
                    ["git", "diff", "--", compat_path],
                    cwd=base,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as e:
                return {"ok": False, "error": f"git diff failed: {e}"}
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                return {"ok": False, "error": err or f"git diff exited {proc.returncode}"}
            body = proc.stdout
            return {
                "ok": True,
                "result": body or "(no differences)",
                "changed": bool(body),
                "path": os.path.abspath(os.path.join(base, compat_path)) if not os.path.isabs(compat_path) else compat_path,
                "mode": "git",
            }
    if not a:
        return {"ok": False, "error": "missing 'a'"}
    if not b and b_text is None:
        return {"ok": False, "error": "provide either 'b' or 'b_text'"}

    base = ctx.cwd or os.getcwd()
    a_abs = a if os.path.isabs(a) else os.path.abspath(os.path.join(base, a))
    try:
        with open(a_abs, "r", encoding="utf-8", errors="replace") as f:
            a_text = f.read()
    except OSError as e:
        return {"ok": False, "error": f"read a: {e}"}

    if b:
        b_abs = b if os.path.isabs(b) else os.path.abspath(os.path.join(base, b))
        try:
            with open(b_abs, "r", encoding="utf-8", errors="replace") as f:
                b_text_resolved = f.read()
        except OSError as e:
            return {"ok": False, "error": f"read b: {e}"}
    else:
        b_text_resolved = str(b_text)

    n = int(params.get("context", 3) or 3)
    label_a = params.get("label_a") or a
    label_b = params.get("label_b") or (b if b else "<inline>")

    body = _make_unified_diff(a_text, b_text_resolved, label_a, label_b, context=n)
    return {"ok": True, "result": body or "(no differences)",
            "changed": bool(body), "a": a_abs}


def _bi_fs_grep(params: dict, ctx: ToolCtx) -> dict:
    """Search for a regex pattern in files under a directory.

    Returns matching lines with file path, line number, and line content.
    Respects .gitignore-style exclusions via include/exclude globs.
    """
    import glob as glob_mod
    import fnmatch

    pattern = params.get("pattern", "")
    search_path = params.get("path", ".")
    include = params.get("include", "**/*")
    exclude = params.get("exclude", "")
    max_results = int(params.get("max_results", 100))
    case_sensitive = params.get("case_sensitive", True)
    max_file_size = int(params.get("max_file_size", 1048576))  # 1MB default

    if not pattern:
        return {"ok": False, "error": "missing 'pattern'"}

    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"ok": False, "error": f"Invalid regex: {e}"}

    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), search_path)) \
        if not os.path.isabs(search_path) else search_path

    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"Path not found: {abs_path}"}

    # Exclude common directories
    default_excludes = ["**/.git/**", "**/node_modules/**", "**/__pycache__/**",
                        "**/.venv/**", "**/venv/**", "**/*.pyc", "**/.DS_Store",
                        "**/*.min.js", "**/*.min.css", "**/dist/**", "**/build/**"]
    exclude_patterns = [e for e in exclude.split(",") if e.strip()] + default_excludes

    results = []
    files_scanned = 0
    truncated = False

    if os.path.isfile(abs_path):
        files = [abs_path]
    else:
        # Use recursive glob
        if isinstance(include, str):
            include = [g.strip() for g in include.split(",") if g.strip()]
        if not include:
            include = ["**/*"]
        files = []
        for inc_pattern in include:
            full_pattern = os.path.join(abs_path, inc_pattern)
            for f in glob_mod.glob(full_pattern, recursive=True):
                if os.path.isfile(f):
                    files.append(f)
        files = sorted(set(files))

    for filepath in files:
        if len(results) >= max_results:
            truncated = True
            break

        # Check exclusions
        try:
            rel = os.path.relpath(filepath, abs_path) if os.path.isdir(abs_path) else os.path.basename(filepath)
        except ValueError:
            rel = filepath
        excluded = False
        for exc in exclude_patterns:
            if fnmatch.fnmatch(filepath, exc) or fnmatch.fnmatch(rel, exc):
                excluded = True
                break
        if excluded:
            continue

        # Check file size
        try:
            fsize = os.path.getsize(filepath)
            if fsize > max_file_size:
                continue
        except OSError:
            continue

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        results.append({
                            "file": os.path.relpath(filepath, ctx.cwd or os.getcwd()),
                            "line": lineno,
                            "content": line.rstrip('\n')[:500],
                        })
                        if len(results) >= max_results:
                            truncated = True
                            break
        except OSError:
            continue
        files_scanned += 1

    return {
        "ok": True,
        "result": results,
        "matches": len(results),
        "files_scanned": files_scanned,
        "truncated": truncated,
    }


def _bi_fs_glob(params: dict, ctx: ToolCtx) -> dict:
    """Find files matching a glob pattern.

    Returns matching file/directory paths with type and size.
    """
    import glob as glob_mod

    patterns = params.get("pattern", "**/*")
    base_path = params.get("path", ".")
    max_results = int(params.get("max_results", 200))

    if isinstance(patterns, str):
        patterns = [p.strip() for p in patterns.split(",") if p.strip()]
    if not patterns:
        patterns = ["**/*"]

    abs_base = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), base_path)) \
        if not os.path.isabs(base_path) else base_path

    results = []
    truncated = False

    for pat in patterns:
        if len(results) >= max_results:
            truncated = True
            break
        full_pat = os.path.join(abs_base, pat)
        for f in glob_mod.glob(full_pat, recursive=True):
            if len(results) >= max_results:
                truncated = True
                break
            try:
                rel = os.path.relpath(f, ctx.cwd or os.getcwd())
            except ValueError:
                rel = f
            is_dir = os.path.isdir(f)
            try:
                size = os.path.getsize(f) if not is_dir else None
            except OSError:
                size = None
            results.append({
                "path": rel,
                "type": "dir" if is_dir else "file",
                "size": size,
            })

    # Sort: dirs first, then by path
    results.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["path"]))

    return {
        "ok": True,
        "result": results,
        "matches": len(results),
        "truncated": truncated,
    }


_WEB_FETCH_TIMEOUT = 15

# ── Import memory system for mem.save ──────────────────────────────────
try:
    import memory_system as _mem_sys
except ImportError:
    _mem_sys = None

try:
    import task_manager as _task_mgr
except ImportError:
    _task_mgr = None

try:
    import plan_mode as _plan_mod
except ImportError:
    _plan_mod = None

try:
    import prompt_opt as _prompt_opt_mod
except ImportError:
    _prompt_opt_mod = None

try:
    import prompt_lab as _prompt_lab_mod
except ImportError:
    _prompt_lab_mod = None

try:
    import evolution_lab as _evolution_lab_mod
except ImportError:
    _evolution_lab_mod = None

try:
    import policy as _policy_mod
except ImportError:
    _policy_mod = None

# Try to import html2text for better content extraction (optional)
try:
    import html2text as _html2text_mod
    _HTML2TEXT = _html2text_mod.HTML2Text()
    _HTML2TEXT.ignore_links = False
    _HTML2TEXT.ignore_images = True
    _HTML2TEXT.body_width = 0
except ImportError:
    _HTML2TEXT = None


def _bi_web_search(params: dict, ctx: ToolCtx) -> dict:
    """Search the web using HTML search pages (no API key needed).

    Returns list of {title, url, snippet} results.
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    import html as _html
    import re as _re_html

    query = params.get("query", "").strip()
    if not query:
        return {"ok": False, "error": "missing 'query'"}

    max_results = min(max(int(params.get("max_results", 10)), 1), 20)

    def _fetch_html(url: str, referer: str = "") -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        if referer:
            req.add_header("Referer", referer)
        with urllib.request.urlopen(req, timeout=_WEB_FETCH_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(2_000_000)
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return raw.decode(charset, errors="replace")

    def _clean(text: str) -> str:
        text = _re_html.sub(r'<script[^>]*>.*?</script>', '', text,
                            flags=_re_html.DOTALL | _re_html.IGNORECASE)
        text = _re_html.sub(r'<style[^>]*>.*?</style>', '', text,
                            flags=_re_html.DOTALL | _re_html.IGNORECASE)
        text = _re_html.sub(r'<[^>]+>', '', text)
        text = _html.unescape(text)
        return _re_html.sub(r'\s+', ' ', text).strip()

    def _dedupe(results: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for item in results:
            url = item.get("url", "").strip()
            title = item.get("title", "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            out.append(item)
            if len(out) >= max_results:
                break
        return out

    def _parse_duckduckgo(html: str) -> list[dict]:
        results = []
        blocks = _re_html.split(r'<div[^>]*class="[^"]*result[^"]*"[^>]*>', html)
        for block in blocks:
            title_m = _re_html.search(
                r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                block, _re_html.DOTALL)
            if not title_m:
                continue
            href = _html.unescape(title_m.group(1))
            parsed = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                href = qs["uddg"][0]
            snippet_m = _re_html.search(
                r'<[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
                block, _re_html.DOTALL)
            results.append({
                "title": _clean(title_m.group(2)),
                "url": href,
                "snippet": _clean(snippet_m.group(1))[:500] if snippet_m else "",
            })
        return _dedupe(results)

    def _parse_bing(html: str) -> list[dict]:
        results = []
        blocks = _re_html.split(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>', html)
        for block in blocks:
            title_m = _re_html.search(
                r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>',
                block, _re_html.DOTALL)
            if not title_m:
                continue
            snippet_m = _re_html.search(
                r'<p[^>]*>(.*?)</p>',
                block, _re_html.DOTALL)
            results.append({
                "title": _clean(title_m.group(2)),
                "url": _html.unescape(title_m.group(1)),
                "snippet": _clean(snippet_m.group(1))[:500] if snippet_m else "",
            })
        return _dedupe(results)

    engine = str(params.get("engine") or os.environ.get("LAINTAS_SEARCH_ENGINE") or "auto").lower()
    engines = {
        "duckduckgo": [("duckduckgo", _parse_duckduckgo,
                       "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}), "")],
        "ddg": [("duckduckgo", _parse_duckduckgo,
                 "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}), "")],
        "bing": [("bing", _parse_bing,
                  "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "mkt": "zh-CN"}), "https://cn.bing.com/")],
        "bing-cn": [("bing", _parse_bing,
                     "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "mkt": "zh-CN"}), "https://cn.bing.com/")],
    }
    search_plan = engines.get(engine)
    if search_plan is None:
        search_plan = engines["duckduckgo"] + engines["bing"]

    errors = []
    for engine_name, parser, url, referer in search_plan:
        try:
            html = _fetch_html(url, referer=referer)
            results = parser(html)
            if results:
                return {
                    "ok": True,
                    "result": results,
                    "query": query,
                    "count": len(results),
                    "engine": engine_name,
                }
            errors.append(f"{engine_name}: no results parsed")
        except urllib.error.URLError as e:
            errors.append(f"{engine_name}: {e}")
        except Exception as e:
            errors.append(f"{engine_name}: {type(e).__name__}: {e}")

    return {"ok": False, "error": "Search request failed; " + " | ".join(errors), "query": query}


def _bi_web_fetch(params: dict, ctx: ToolCtx) -> dict:
    """Fetch a URL and extract its text content.

    Strips HTML tags and returns clean text. Uses html2text if available.
    """
    import urllib.request
    import urllib.error

    url = params.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}

    max_bytes = int(params.get("max_bytes", 65536))
    timeout = int(params.get("timeout", 15))

    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}", "url": url}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Request failed: {e.reason}", "url": url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}

    # Decode body
    charset = "utf-8"
    if "charset=" in content_type:
        try:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        except (IndexError, ValueError):
            pass

    try:
        body = raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        body = raw.decode("utf-8", errors="replace")

    # Extract text
    is_html = "text/html" in content_type or "<html" in body[:1000].lower() or "<!doctype html" in body[:1000].lower()

    if is_html and _HTML2TEXT is not None:
        text = _HTML2TEXT.handle(body)
    elif is_html:
        # Fallback: strip HTML tags
        import re as _re_html
        # Remove scripts and styles
        body = _re_html.sub(r'<script[^>]*>.*?</script>', '', body,
                            flags=_re_html.DOTALL | _re_html.IGNORECASE)
        body = _re_html.sub(r'<style[^>]*>.*?</style>', '', body,
                            flags=_re_html.DOTALL | _re_html.IGNORECASE)
        # Convert block elements to newlines
        body = _re_html.sub(r'<(?:br|p|div|li|tr|h[1-6])[^>]*>', '\n', body,
                            flags=_re_html.IGNORECASE)
        # Strip remaining tags
        text = _re_html.sub(r'<[^>]+>', '', body)
        # Collapse whitespace
        text = _re_html.sub(r'\n{3,}', '\n\n', text)
        # Decode common entities
        for ent, ch in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                         ('&quot;', '"'), ('&#x27;', "'"), ('&nbsp;', ' ')]:
            text = text.replace(ent, ch)
    else:
        text = body

    # Trim
    text = text.strip()
    if len(text) > max_bytes:
        text = text[:max_bytes]
        truncated = True

    return {
        "ok": True,
        "result": text,
        "url": url,
        "content_type": content_type,
        "size": len(text),
        "truncated": truncated,
    }


# ── Agent / Terminal / Session tools (replace meta-commands) ──────────

def _bi_agent_spawn(params: dict, ctx: ToolCtx) -> dict:
    """Spawn an in-process child agent to handle a sub-task.

    Supports:
    - role: specialized agent role (explorer, architect, reviewer, etc.)
    - tasks: list of tasks for parallel spawning
    - wait: block until all children complete (parallel mode)
    """
    # ── Parallel mode: tasks list ──
    tasks_list = params.get("tasks")
    if tasks_list and isinstance(tasks_list, list):
        if ctx.spawn_subagent is None or ctx.deps is None:
            return {"ok": False, "error": "spawn not available in this context"}
        parent_id = ctx.agent_id
        if parent_id is None:
            return {"ok": False, "error": "no agent_id in context"}

        # Import the parallel spawner from agent_loop
        import agent_loop as _al
        child_ids = _al.spawn_subagents_parallel(
            parent_id=parent_id,
            tasks=tasks_list,
            deps=ctx.deps,
            session=ctx.session,
            events_cb=ctx.events_cb,
        )
        if not child_ids:
            return {"ok": False, "error": f"parallel spawn failed (parent '{parent_id}' not found)"}

        # If wait=true, block until all children complete
        if params.get("wait", False):
            results = []
            deadline = time.time() + float(params.get("timeout", 120.0))
            _al.enter_waiting(parent_id)
            try:
                for cid in child_ids:
                    remaining = max(0.0, deadline - time.time())
                    info = _al.wait_for_agent(cid, timeout=remaining)
                    if info:
                        results.append(f"[{cid}] {info.status}: {info.last_reply[:200] if info.last_reply else '(no reply)'}")
                    else:
                        _al.abort_agent(cid)
                        results.append(f"[{cid}] timed out; cancellation requested")
            finally:
                _al.exit_waiting(parent_id)
            return {"ok": True,
                    "result": f"Spawned {len(child_ids)} agents in parallel. Results:\n" + "\n".join(results),
                    "child_ids": child_ids}

        task_desc = ", ".join(t.get("role", t.get("task", "?")[:30]) for t in tasks_list)
        return {"ok": True,
                "result": f"Spawned {len(child_ids)} agents in parallel: [{task_desc}]. "
                          f"IDs: {', '.join(child_ids)}. Check inbox for results.",
                "child_ids": child_ids}

    # ── Single agent mode ──
    task = params.get("task", "").strip()
    if not task:
        return {"ok": False, "error": "missing 'task' (or 'tasks' for parallel mode)"}
    name = params.get("name") or None
    role = params.get("role") or None
    if ctx.spawn_subagent is None or ctx.deps is None:
        return {"ok": False, "error": "spawn not available in this context"}
    parent_id = ctx.agent_id
    if parent_id is None:
        return {"ok": False, "error": "no agent_id in context"}
    child_id = ctx.spawn_subagent(
        parent_id=parent_id, task=task, deps=ctx.deps,
        name=name, session=ctx.session, events_cb=ctx.events_cb,
        role=role,
    )
    if child_id is None:
        return {"ok": False, "error": f"spawn failed (parent '{parent_id}' not found)"}
    role_note = f" (role: {role})" if role else ""
    return {"ok": True, "result": f"Spawned child agent '{child_id}'{role_note} for task: {task[:120]}", "child_id": child_id}


def _bi_spawn(params: dict, ctx: ToolCtx) -> dict:
    """Blocking single spawn — mirrors Helpwo spawn tool."""
    import agent_loop as _al
    import threading

    goal = (params.get("goal") or "").strip()
    if not goal:
        return {"ok": False, "error": "missing 'goal'"}
    context = (params.get("context") or "").strip()

    parent_id = ctx.agent_id
    if parent_id and not _al.can_spawn(parent_id):
        return {"ok": False, "error": "Cannot spawn: maximum agent depth reached."}

    parent = _al.get_agent(parent_id) if parent_id else None
    depth = (parent.depth + 1) if parent else 0

    child = _al.register_agent(depth=depth, parent_id=parent_id, role="subagent")
    spawn_ctx = context or ""

    done_evt = threading.Event()
    result_holder = {}

    def _runner(ok: bool):
        if not ok:
            result_holder.update({"ok": False, "result": f"[{child.id}] cancelled while queued."})
            done_evt.set()
            return
        try:
            r = _al.run_agent_loop(
                ctx.deps, goal, ctx.session, child.state, child.chat_history,
                depth=child.depth, agent_id=child.id,
            )
            reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
            _al.mark_agent_finished(child.id, result=reply)
            result_holder.update({"ok": True, "result": f"[{child.id}] {reply or '(done)'}"})
        except Exception as e:
            _al.mark_agent_finished(child.id, error=repr(e))
            result_holder.update({"ok": False, "result": repr(e)})
        finally:
            done_evt.set()

    if parent_id:
        _al.enter_waiting(parent_id)
    try:
        t = threading.Thread(
            target=lambda: _al.schedule_agent(child.id, _runner),
            daemon=True, name=f"spawn-{child.id}",
        )
        t.start()
        done_evt.wait(timeout=300)
    finally:
        if parent_id:
            _al.exit_waiting(parent_id)

    if not result_holder:
        # Timeout is cancellation, not completion. Signal the live loop and
        # leave its scheduler lease intact until the runner actually exits.
        _al.abort_agent(child.id)
        return {"ok": False, "error": f"spawn timed out: {goal[:80]}"}
    return result_holder


def _bi_spawn_parallel(params: dict, ctx: ToolCtx) -> dict:
    """Fan-out + join — mirrors Helpwo spawn_parallel."""
    import agent_loop as _al
    import threading

    tasks = params.get("tasks") or []
    if not tasks:
        return {"ok": False, "error": "spawn_parallel requires at least one task"}
    if len(tasks) > 6:
        return {"ok": False, "error": "spawn_parallel: maximum 6 tasks"}

    parent_id = ctx.agent_id
    if parent_id and not _al.can_spawn(parent_id):
        return {"ok": False, "error": "Cannot spawn: maximum agent depth reached."}

    parent = _al.get_agent(parent_id) if parent_id else None
    depth = (parent.depth + 1) if parent else 0
    group_id = f"group-{int(time.time() * 1000)}"

    children = []
    for t in tasks:
        child = _al.register_agent(depth=depth, parent_id=parent_id, role="subagent")
        child.group_id = group_id
        children.append((child, t))

    results = [None] * len(children)
    done_events = [threading.Event() for _ in children]

    def _make_runner(idx, child, task):
        goal = (task.get("goal") or "").strip()
        hint = (task.get("hint") or "").strip()
        spawn_ctx = hint or ""

        def _runner(ok: bool):
            if not ok:
                results[idx] = {"ok": False, "result": f"[{child.id}] cancelled."}
                done_events[idx].set()
                return
            try:
                r = _al.run_agent_loop(
                    ctx.deps, goal, ctx.session, child.state, child.chat_history,
                    depth=child.depth, agent_id=child.id,
                )
                reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
                _al.mark_agent_finished(child.id, result=reply)
                results[idx] = {"ok": True, "result": reply or "(done)"}
            except Exception as e:
                _al.mark_agent_finished(child.id, error=repr(e))
                results[idx] = {"ok": False, "result": repr(e)}
            finally:
                done_events[idx].set()
        return _runner

    if parent_id:
        _al.enter_waiting(parent_id)
    try:
        threads = []
        for i, (child, task) in enumerate(children):
            runner = _make_runner(i, child, task)
            t = threading.Thread(
                target=lambda r=runner: _al.schedule_agent(child.id, r),
                daemon=True, name=f"par-{child.id}",
            )
            threads.append(t)
            t.start()
        for evt in done_events:
            evt.wait(timeout=600)
    finally:
        if parent_id:
            _al.exit_waiting(parent_id)

    for (child, _), result in zip(children, results):
        if result is None:
            _al.abort_agent(child.id)

    ok = all(r and r.get("ok") for r in results)
    succeeded = sum(1 for r in results if r and r.get("ok"))
    lines = [f"═══ Parallel Results ({len(children)} agents) ═══"]
    for i, ((child, task), r) in enumerate(zip(children, results)):
        icon = "✓" if (r and r.get("ok")) else "✗"
        goal = (task.get("goal") or "")[:80]
        msg = (r.get("result") or "(timeout)") if r else "(timeout)"
        lines.append(f"\n─── [{icon}] {child.id} ───\nGoal: {goal}\nResult: {msg[:400]}")
    lines.append(f"\n═══ Summary: {succeeded}/{len(children)} succeeded ═══")

    return {"ok": ok, "result": "\n".join(lines)}


def _bi_spawn_chain(params: dict, ctx: ToolCtx) -> dict:
    """Sequential pipeline with handoff — mirrors Helpwo spawn_chain."""
    import agent_loop as _al
    import threading

    steps = params.get("steps") or []
    if len(steps) < 2:
        return {"ok": False, "error": "spawn_chain requires at least 2 steps"}
    if len(steps) > 6:
        return {"ok": False, "error": "spawn_chain: maximum 6 steps"}

    parent_id = ctx.agent_id
    if parent_id and not _al.can_spawn(parent_id):
        return {"ok": False, "error": "Cannot spawn: maximum agent depth reached."}

    parent = _al.get_agent(parent_id) if parent_id else None
    depth = (parent.depth + 1) if parent else 0
    chain_id = f"chain-{int(time.time() * 1000)}"

    # Register all steps upfront so they're visible immediately
    children = []
    for i, step in enumerate(steps):
        child = _al.register_agent(depth=depth, parent_id=parent_id, role="subagent")
        child.chain_id = chain_id
        child.chain_step_index = i
        if i > 0:
            child.status = "queued"   # visually show as queued
        children.append((child, step))

    summaries = []

    if parent_id:
        _al.enter_waiting(parent_id)
    try:
        handoff = ""
        for i, (child, step) in enumerate(children):
            goal = (step.get("goal") or "").strip()
            hint = (step.get("hint") or "").strip()
            is_last = i == len(steps) - 1

            pipeline_note = (
                f"[PIPELINE STEP {i+1}/{len(steps)}] You are one step of a sequential pipeline. "
            )
            if not is_last:
                pipeline_note += (
                    "The next step depends on your output. End with a handoff document:\n"
                    "## Done\n## Key findings\n## Files touched\n## Notes for next step\n## Open issues"
                )
            else:
                pipeline_note += "You are the LAST step — your final message is the deliverable."

            sections = [pipeline_note]
            if handoff:
                sections.append(f"[HANDOFF FROM PREVIOUS STEP]\n{handoff}")
            if hint:
                sections.append(f"[STEP HINT] {hint}")

            spawn_ctx = "\n\n".join(sections)

            done_evt = threading.Event()
            result_holder = {}

            def _runner(ok, child=child, goal=goal, spawn_ctx=spawn_ctx):
                if not ok:
                    result_holder.update({"ok": False, "result": "Cancelled while queued."})
                    done_evt.set()
                    return
                try:
                    full_goal = f"{spawn_ctx}\n\n{goal}"
                    r = _al.run_agent_loop(
                        ctx.deps, full_goal, ctx.session, child.state, child.chat_history,
                        depth=child.depth, agent_id=child.id,
                    )
                    reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
                    _al.mark_agent_finished(child.id, result=reply)
                    result_holder.update({"ok": True, "result": reply or "(done)"})
                except Exception as e:
                    _al.mark_agent_finished(child.id, error=repr(e))
                    result_holder.update({"ok": False, "result": repr(e)})
                finally:
                    done_evt.set()

            t = threading.Thread(
                target=lambda r=_runner: _al.schedule_agent(child.id, r),
                daemon=True, name=f"chain-{child.id}",
            )
            t.start()
            done_evt.wait(timeout=300)

            if not result_holder:
                _al.abort_agent(child.id)
                # abort remaining
                for j, (c2, _) in enumerate(children):
                    if j > i:
                        _al.abort_agent(c2.id)
                return {"ok": False, "result": f"Chain {chain_id} timed out at step {i+1}"}

            if not result_holder.get("ok"):
                for j, (c2, _) in enumerate(children):
                    if j > i:
                        _al.abort_agent(c2.id)
                done_steps = "\n".join(summaries)
                return {
                    "ok": False,
                    "result": (
                        f"═══ Chain {chain_id} FAILED at step {i+1}/{len(steps)} ═══\n"
                        f"Goal: {goal[:120]}\nError: {result_holder['result'][:400]}"
                        + (f"\n\nCompleted:\n{done_steps}" if done_steps else "")
                    ),
                }

            handoff = result_holder["result"]
            summaries.append(f"  ✓ Step {i+1}: {goal[:80]}")

    finally:
        if parent_id:
            _al.exit_waiting(parent_id)

    return {
        "ok": True,
        "result": (
            f"═══ Chain {chain_id} completed ({len(steps)} steps) ═══\n"
            + "\n".join(summaries)
            + f"\n\n─── Final step output ───\n{handoff}"
        ),
    }


def _bi_await_spawns(params: dict, ctx: ToolCtx) -> dict:
    """Wait for sub-agents to finish and collect results."""
    import agent_loop as _al

    parent_id = ctx.agent_id
    agent_ids = params.get("agent_ids")

    if agent_ids:
        target_ids = list(agent_ids)
    elif parent_id:
        parent = _al.get_agent(parent_id)
        target_ids = list(parent.child_ids) if parent else []
    else:
        return {"ok": False, "error": "No agent_ids specified and no current agent"}

    if not target_ids:
        return {"ok": True, "result": "No agents to wait for."}

    STALL_S = 120
    HARD_CAP_S = 20 * 60
    start = time.time()
    hard_cap_reached = False

    if parent_id:
        _al.enter_waiting(parent_id)
    try:
        while True:
            agents = [_al.get_agent(aid) for aid in target_ids]
            agents = [a for a in agents if a is not None]
            if all(a.status in ("done", "error", "aborted") for a in agents):
                break
            elapsed = time.time() - start
            if elapsed > HARD_CAP_S:
                hard_cap_reached = True
                break
            time.sleep(0.5)
    finally:
        if parent_id:
            _al.exit_waiting(parent_id)

    if hard_cap_reached:
        for aid in target_ids:
            info = _al.get_agent(aid)
            if info and info.status not in ("done", "error", "aborted"):
                _al.abort_agent(aid)

    agents = [_al.get_agent(aid) for aid in target_ids if _al.get_agent(aid)]
    lines = [f"═══ Sub-agent Results ({len(agents)} agents) ═══"]
    for a in agents:
        icon = "✓" if a.status == "done" else "✗"
        lines.append(f"\n─── [{icon}] {a.id} ───")
        lines.append(f"Goal: {a.name} [status={a.status}]")
        if a.result:
            lines.append(f"Result: {a.result[:400]}")
        if a.error:
            lines.append(f"Error: {a.error[:200]}")
    ok = all(a.status == "done" for a in agents)
    succeeded = sum(1 for a in agents if a.status == "done")
    lines.append(f"\n═══ Summary: {succeeded}/{len(agents)} succeeded ═══")
    return {"ok": ok, "result": "\n".join(lines)}


def _bi_hwo(params: dict, ctx: ToolCtx) -> dict:
    """Compile or run a .hwo workflow file."""
    import hwo_runner
    action = (params.get("action") or "run").lower()
    path = (params.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "missing 'path'"}

    if action == "compile":
        r = hwo_runner.compile_hwo_file(path)
    else:
        r = hwo_runner.run_hwo_file(
            path=path,
            deps=ctx.deps,
            session=ctx.session,
            parent_id=ctx.agent_id,
        )
    return {"ok": r.get("ok", False), "result": r.get("msg", "")}


def _hwo_is_sibling_or_ancestor(caller_id: str, target_id: str) -> bool:
    """True if target is a sibling of caller (shares a parent) or an ancestor."""
    import agent_loop as _al
    if not caller_id or not target_id or caller_id == target_id:
        return False
    caller = _al.get_agent(caller_id)
    if caller is None:
        return False
    if caller.parent_id == target_id:
        return True  # target is caller's direct parent
    if caller.parent_id is not None:
        parent = _al.get_agent(caller.parent_id)
        if parent and target_id in parent.child_ids:
            return True  # same parent -> sibling
    cur = caller
    while cur and cur.parent_id:
        if cur.parent_id == target_id:
            return True
        cur = _al.get_agent(cur.parent_id)
    return False


def _bi_hwo_agent_send(params: dict, ctx: ToolCtx) -> dict:
    """Send a message to a sibling or parent agent within the current HWO workflow."""
    import agent_loop as _al
    import hwo_runner

    to = (params.get("to") or "").strip()
    message = params.get("message", "")
    if not to:
        return {"ok": False, "error": "missing 'to'"}
    if message == "":
        return {"ok": False, "error": "missing 'message'"}
    caller_id = ctx.agent_id
    if not caller_id:
        return {"ok": False, "error": "no current agent"}
    if _al.get_agent(to) is None:
        return {"ok": False, "error": f"no such agent '{to}' — check the [TEAM MANIFEST] for valid names"}
    if not _hwo_is_sibling_or_ancestor(caller_id, to):
        return {
            "ok": False,
            "error": f"'{to}' is not a sibling or ancestor of '{caller_id}' — "
                     "agent_send only reaches siblings or your parent, never descendants.",
        }
    ok = hwo_runner.hwo_send(to=to, from_=caller_id, text=str(message))
    if not ok:
        return {"ok": False, "error": f"mailbox for '{to}' is full"}
    return {"ok": True, "result": f"Sent to #{to}#."}


def _bi_hwo_agent_receive(params: dict, ctx: ToolCtx) -> dict:
    """Block until a message arrives from a specific HWO teammate (or anyone)."""
    import hwo_runner

    from_ = (params.get("from") or "").strip() or None
    try:
        timeout = float(params.get("timeout", 60) or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    timeout = max(0.0, min(timeout, 300.0))
    caller_id = ctx.agent_id
    if not caller_id:
        return {"ok": False, "error": "no current agent"}

    msg = hwo_runner.hwo_receive(caller_id, from_, timeout)
    if msg is None:
        who = f"#{from_}#" if from_ else "any sender"
        return {"ok": False, "error": f"agent_receive timed out after {timeout:.0f}s waiting for {who}."}
    return {"ok": True, "result": f"[{msg.get('from')}] {msg.get('text', '')}"}


def _bi_hwo_agent_return(params: dict, ctx: ToolCtx) -> dict:
    """Record an explicit HWO return value that overrides this step's natural reply."""
    value = params.get("value", "")
    if value == "":
        return {"ok": False, "error": "missing 'value'"}
    if ctx.get_agent is None or ctx.agent_id is None:
        return {"ok": False, "error": "agent_return is only meaningful inside an HWO workflow"}
    info = ctx.get_agent(ctx.agent_id)
    if info is None:
        return {"ok": False, "error": "current agent not found in registry"}
    info.state['_hwo_return'] = str(value)
    return {
        "ok": True,
        "result": "Return value recorded. Give a brief final reply now and make no more tool calls this turn.",
    }


def _bi_agent_tell(params: dict, ctx: ToolCtx) -> dict:
    """Send a message to another agent's inbox."""
    target_id = (params.get("agent_id") or "").strip()
    if not target_id:
        return {"ok": False, "error": "missing 'agent_id'"}
    msg = params.get("message", "")
    if not msg:
        return {"ok": False, "error": "missing 'message'"}
    if ctx.send_to_agent is None:
        return {"ok": False, "error": "send_to_agent not available"}
    if isinstance(msg, dict):
        body = dict(msg)
    else:
        body = {"kind": "msg", "text": str(msg)}
    body.setdefault("from", ctx.agent_id or "unknown")
    ok = ctx.send_to_agent(target_id, body)
    if not ok:
        return {"ok": False, "error": f"agent '{target_id}' not found or inbox full"}
    return {"ok": True, "result": f"Sent to {target_id}"}


def _bi_agent_station(params: dict, ctx: ToolCtx) -> dict:
    """Station the current agent at a named terminal (bash sub-shell)."""
    name = (params.get("name") or "main").strip() or "main"
    target_agent = (
        ctx.get_agent(ctx.agent_id)
        if ctx.get_agent is not None and ctx.agent_id
        else None
    )
    if target_agent is None:
        return {"ok": False, "error": "no current agent to station"}

    if ctx.get_terminal is not None and ctx.register_terminal is not None:
        existing = ctx.get_terminal(name)
        if existing and existing.session and existing.session.is_alive():
            # Re-use existing live terminal — just attach the agent
            if ctx.station_agent is not None:
                ctx.station_agent(target_agent.id, name)
            return {"ok": True, "result": f"Stationed {target_agent.id} in existing terminal {name}"}
        if existing and ctx.unregister_terminal:
            ctx.unregister_terminal(name)

        # Spawn a fresh bash sub-terminal — agents operate via marker-poll
        shell_cmd = os.environ.get("SHELL", "/bin/bash")
        sub = ctx.deps.SubTerminalSession(shell_cmd)
        sub.start()
        time.sleep(0.1)
        if not sub.is_alive():
            return {"ok": False, "error": f"failed to start terminal '{name}'"}
        sub.read_output(timeout=0.1)
        ctx.register_terminal(sub, shell_cmd, ctx.depth, name=name)
    if ctx.station_agent is not None:
        ctx.station_agent(target_agent.id, name)
    return {"ok": True, "result": f"Stationed {target_agent.id} in terminal {name}"}


def _bi_agent_abort(params: dict, ctx: ToolCtx) -> dict:
    """Abort another agent's execution."""
    target_id = (params.get("agent_id") or "").strip()
    if not target_id:
        return {"ok": False, "error": "missing 'agent_id'"}
    if ctx.abort_agent is None:
        return {"ok": False, "error": "abort not available"}
    ok = ctx.abort_agent(target_id)
    if not ok:
        return {"ok": False, "error": f"agent '{target_id}' not found"}
    return {"ok": True, "result": f"Aborted {target_id}"}


def _bi_agent_wait(params: dict, ctx: ToolCtx) -> dict:
    """Wait for another agent to finish."""
    target_id = (params.get("agent_id") or "").strip()
    if not target_id:
        return {"ok": False, "error": "missing 'agent_id'"}
    timeout = float(params.get("timeout", 300))
    if ctx.wait_for_agent is None:
        return {"ok": False, "error": "wait not available"}
    import agent_loop as _al
    if ctx.agent_id:
        _al.enter_waiting(ctx.agent_id)
    try:
        info = ctx.wait_for_agent(target_id, timeout)
    finally:
        if ctx.agent_id:
            _al.exit_waiting(ctx.agent_id)
    if info is None:
        return {"ok": False, "error": f"agent '{target_id}' not found or timed out"}
    return {"ok": True, "result": f"Agent {target_id}: {info.status}", "status": info.status}


def _bi_agent_hire(params: dict, ctx: ToolCtx) -> dict:
    """Register a new agent slot."""
    if ctx.register_agent_fn is None:
        return {"ok": False, "error": "hire not available"}
    info = ctx.register_agent_fn(depth=ctx.depth)
    return {"ok": True, "result": f"Hired {info.id}", "agent_id": info.id}


def _bi_agent_list(params: dict, ctx: ToolCtx) -> dict:
    """List all agents."""
    if ctx.get_all_agents is None:
        return {"ok": False, "error": "agent listing not available"}
    agents = ctx.get_all_agents()
    current = (
        ctx.get_agent(ctx.agent_id)
        if ctx.get_agent is not None and ctx.agent_id
        else None
    )
    lines = []
    for a in agents:
        marker = " <-- self" if (current and a.id == current.id) else ""
        st = f" [stationed: {a.stationed_terminal}]" if a.stationed_terminal else ""
        st += f" [{a.status}]" if a.status != "idle" else ""
        lines.append(f"  {a.id}: {a.name}{st}{marker}")
    return {"ok": True, "result": "\n".join(lines) if lines else "(no agents)"}


def _bi_agent_rename(params: dict, ctx: ToolCtx) -> dict:
    """Rename the current agent."""
    new_name = (params.get("name") or "").strip()
    if not new_name:
        return {"ok": False, "error": "missing 'name'"}
    if ctx.rename_agent is None or ctx.get_agent is None or not ctx.agent_id:
        return {"ok": False, "error": "rename not available"}
    current = ctx.get_agent(ctx.agent_id)
    if current and ctx.rename_agent(current.id, new_name):
        return {"ok": True, "result": f"Renamed to {new_name}"}
    return {"ok": False, "error": "no current agent to rename"}


def _bi_agent_switch(params: dict, ctx: ToolCtx) -> dict:
    """Switch to a different agent identity."""
    if ctx.depth > 0:
        return {"ok": False, "error": "sub-agents cannot change the REPL's active agent"}
    target_id = (params.get("agent_id") or "").strip()
    if not target_id:
        return {"ok": False, "error": "missing 'agent_id'"}
    if ctx.switch_to_agent is None:
        return {"ok": False, "error": "switch not available"}
    if ctx.switch_to_agent(target_id):
        agent = ctx.get_agent(target_id) if ctx.get_agent else None
        label = agent.name if agent else target_id
        return {"ok": True, "result": f"Switched to {label}"}
    return {"ok": False, "error": f"agent '{target_id}' not found"}


def _bi_terminal_send(params: dict, ctx: ToolCtx) -> dict:
    """Send a command/keystrokes to a named terminal."""
    target = (params.get("name") or "").strip()
    cmd = (params.get("command") or "").strip()
    if not target:
        return {"ok": False, "error": "missing 'name'"}
    if not cmd:
        return {"ok": False, "error": "missing 'command'"}
    if ctx.get_terminal is None:
        return {"ok": False, "error": "terminal access not available"}
    term = ctx.get_terminal(target)
    if term is None:
        return {"ok": False, "error": f"terminal '{target}' not found"}
    if not (term.session and term.session.is_alive()):
        return {"ok": False, "error": f"terminal '{target}' is dead"}
    # Use CR (\r) as the Enter keystroke — that's what a real keyboard sends,
    # and what raw-mode apps (prompt_toolkit, codex, claude, vim, …) expect.
    # Cooked-mode shells (bash) also accept CR via the ICRNL line discipline.
    term.session.send_keys(cmd + "\r")
    time.sleep(0.3)
    term.session.read_output(timeout=0.5)
    try:
        output = ctx.deps.strip_ansi(term.session.full_output) if ctx.deps else term.session.full_output
    except Exception:
        output = term.session.full_output
    return {"ok": True, "result": output.strip() or "(no output)", "terminal": target}


def _bi_terminal_terminate(params: dict, ctx: ToolCtx) -> dict:
    """Terminate a named terminal."""
    target = (params.get("name") or "").strip()
    if not target:
        return {"ok": False, "error": "missing 'name'"}
    if ctx.unregister_terminal is None:
        return {"ok": False, "error": "terminate not available"}
    if ctx.get_terminal is not None:
        term = ctx.get_terminal(target)
        if term and ctx.unstation_agent is not None:
            for aid in list(term.stationed_agent_ids):
                ctx.unstation_agent(aid)
    if ctx.unregister_terminal(target):
        return {"ok": True, "result": f"Terminated {target}"}
    return {"ok": False, "error": f"terminal '{target}' not found"}


def _bi_terminal_create(params: dict, ctx: ToolCtx) -> dict:
    """Create a new named sub-terminal (no agent stationed)."""
    name = (params.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    if ctx.register_terminal is None or ctx.deps is None:
        return {"ok": False, "error": "terminal creation not available"}
    if ctx.get_terminal is not None:
        existing = ctx.get_terminal(name)
        if existing and existing.session and not existing.session.is_alive() and ctx.unregister_terminal:
            ctx.unregister_terminal(name)
            existing = None
        if existing is not None:
            return {"ok": False, "error": f"terminal '{name}' already exists"}

    lain_cmd = f"{sys.executable} {os.path.abspath(__file__)} --depth {ctx.depth + 1}"
    sub = ctx.deps.SubTerminalSession(lain_cmd)
    sub.start()
    time.sleep(0.15)
    if not sub.is_alive():
        return {"ok": False, "error": f"failed to start terminal '{name}'"}
    sub.read_output(timeout=0.1)
    ctx.register_terminal(sub, "laintas-cli", ctx.depth, name=name)
    return {"ok": True, "result": f"Created sub-terminal {name}", "terminal": name}


def _bi_terminal_list(params: dict, ctx: ToolCtx) -> dict:
    """List all named terminals."""
    if ctx.get_all_terminals is None:
        return {"ok": False, "error": "terminal listing not available"}
    terminals = ctx.get_all_terminals()
    if not terminals:
        return {"ok": True, "result": "(no terminals)"}
    lines = []
    for t in terminals:
        alive = t.session and t.session.is_alive()
        status = "alive" if alive else "dead"
        stationed = f" [stationed: {', '.join(t.stationed_agent_ids)}]" if t.stationed_agent_ids else ""
        trigger = f" [trigger: {t.trigger_pattern!r}]" if t.trigger_pattern else ""
        lines.append(f"  {t.name} ({t.command}) [{status}]{stationed}{trigger}")
    return {"ok": True, "result": "\n".join(lines)}


def _bi_terminal_exec(params: dict, ctx: ToolCtx) -> dict:
    """Run an arbitrary command in a background sub-terminal, optionally with a trigger."""
    import sys as _sys
    import os as _os
    name = (params.get("name") or "").strip()
    command = (params.get("command") or "").strip()
    trigger = (params.get("trigger") or "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    if not command:
        return {"ok": False, "error": "missing 'command'"}
    if ctx.register_terminal is None or ctx.deps is None:
        return {"ok": False, "error": "terminal creation not available"}
    if ctx.get_terminal is not None:
        existing = ctx.get_terminal(name)
        if existing and existing.session and existing.session.is_alive():
            return {"ok": False, "error": f"terminal '{name}' already exists and is alive; terminate it first"}
        if existing and ctx.unregister_terminal:
            ctx.unregister_terminal(name)
    sub = ctx.deps.SubTerminalSession(command)
    sub.start()
    time.sleep(0.2)
    if sub.is_alive():
        sub.read_output(timeout=0.1)
    ctx.register_terminal(
        sub, command, ctx.depth, name=name,
        trigger=trigger or None,
        trigger_agent_id=ctx.agent_id if trigger else None,
    )
    msg = f"Started sub-terminal '{name}': {command}"
    if trigger:
        msg += f"\nTrigger active — pattern {trigger!r} will push events to your inbox."
    return {"ok": True, "result": msg, "terminal": name}


def _bi_terminal_watch(params: dict, ctx: ToolCtx) -> dict:
    """Set or clear a trigger on an existing terminal.

    When pattern is non-empty, any new output line matching the regex pushes
    a watch.trigger event into the agent's inbox. Pass an empty pattern to
    remove the trigger.
    """
    name = (params.get("name") or "").strip()
    pattern = (params.get("pattern") or "")
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    if ctx.set_terminal_trigger is None:
        return {"ok": False, "error": "trigger control not available"}
    if ctx.get_terminal is not None:
        term = ctx.get_terminal(name)
        if term is None:
            return {"ok": False, "error": f"terminal '{name}' not found"}
    ok = ctx.set_terminal_trigger(name, pattern.strip(), ctx.agent_id)
    if not ok:
        return {"ok": False, "error": f"terminal '{name}' not found"}
    if pattern.strip():
        return {"ok": True, "result": f"Trigger set on '{name}': {pattern.strip()!r}"}
    return {"ok": True, "result": f"Trigger cleared on '{name}'"}


def _bi_session_close(params: dict, ctx: ToolCtx) -> dict:
    """Close the current interactive PTY session."""
    if ctx.interactive_session is None:
        return {"ok": True, "result": "No active session to close"}
    session = ctx.interactive_session
    session.close()
    try:
        output = ctx.deps.strip_ansi(session.full_output) if ctx.deps else session.full_output
    except Exception:
        output = session.full_output
    ctx.interactive_session = None
    return {"ok": True, "result": output.strip() or "(no output)", "command": session.command[:120]}


def _bi_session_keys(params: dict, ctx: ToolCtx) -> dict:
    """Send keystrokes to the current interactive PTY session."""
    keys = (params.get("keys") or "").strip()
    if not keys:
        return {"ok": False, "error": "missing 'keys'"}
    if ctx.interactive_session is None:
        return {"ok": False, "error": "no active session — run a long-lived command first"}
    session = ctx.interactive_session
    session.send_keys(keys)
    time.sleep(0.3)
    new_output = session.read_output(timeout=0.5)
    try:
        full = ctx.deps.strip_ansi(session.full_output) if ctx.deps else session.full_output
    except Exception:
        full = session.full_output
    return {"ok": True, "result": full.strip() or "(no output)",
            "new_output": (new_output or "").strip()[:500],
            "alive": session.is_alive()}


def _bi_sleep(params: dict, ctx: ToolCtx) -> dict:
    """Sleep for N seconds (e.g. after starting a server)."""
    secs = float(params.get("seconds", 1))
    secs = max(0.1, min(secs, 30))
    time.sleep(secs)
    return {"ok": True, "result": f"Slept {secs:.1f}s"}


def _bi_task_continue(params: dict, ctx: ToolCtx) -> dict:
    """No-op continuation signal.

    Its only purpose is to BE a tool call: the agent loop keeps looping whenever
    a turn contains any tool call, so calling this lets the model keep working on
    a turn where it has nothing concrete to run yet (still reasoning, planning a
    next step) WITHOUT ending on an empty tool-call turn — which the loop
    otherwise reads as "done / handing back to the user".
    """
    return {"ok": True, "result": "(continuing)"}


def _bi_session_continue(params: dict, ctx: ToolCtx) -> dict:
    """Signal that the user wants to resume prior session work.

    Unlike task.continue (a generic keep-looping no-op), this is called when
    the AI determines the user's input (e.g. "继续", "continue", "接着做") is a
    request to resume the current session's pending task — not a new task.

    The agent loop detects the _session_continue marker and:
      - clears any max-loops exhaustion state so the loop can run fresh,
      - preserves the active run objective instead of creating a new task.

    The AI should call this BEFORE resuming work, then proceed with the actual
    task steps (shell.exec, fs.write, etc.) in subsequent turns.
    """
    reason = (params.get("reason") or "").strip()
    result = "Continuing current session. Resume the in_progress task in <active_tasks>; if none, work on <objective>."
    if reason:
        result += f" (reason: {reason})"
    return {"ok": True, "result": result, "_session_continue": True}


def _bi_task_complete(params: dict, ctx: ToolCtx) -> dict:
    """Affirmatively signal the user's task is finished.

    The agent loop watches for the `_task_complete` marker in the result and
    ends the loop normally. This is the canonical completion signal — in
    autonomous/execute mode it is the only way to finish, because the loop no
    longer infers "done" from a turn that simply lacks a tool call.
    """
    summary = (params.get("summary") or "").strip()
    return {
        "ok": True,
        "result": summary or "Task marked complete.",
        "_task_complete": True,
        "summary": summary,
    }


# ── Shell execution tool ──────────────────────────────────────────────

def _bi_shell_exec(params: dict, ctx: ToolCtx) -> dict:
    """Execute a shell command.

    Priority order:
      1. cd/clear → run in parent process (side effects stick).
      2. ctx.stationed_terminal → marker-poll inside the agent's stationed PTY.
      3. ctx.interactive_session → marker-poll inside an ad-hoc one-shot PTY.
      4. subprocess.run fallback.

    Policy is enforced by the dispatch loop, not here (single source of truth).
    """
    command = (params.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "missing 'command'"}

    # Models sometimes echo the tool name into the command payload
    # (for example `shell.exec ls -la`). Strip that wrapper so the tool
    # executes the intended shell command instead of trying to invoke a
    # nonexistent `shell.exec` binary.
    if command.startswith("shell.exec "):
        command = command[len("shell.exec "):].strip()
    elif command == "shell.exec":
        return {"ok": False, "error": "missing shell command after 'shell.exec'"}

    cwd = params.get("cwd") or ctx.cwd or os.getcwd()
    timeout = int(params.get("timeout", 60))

    # cd / clear short-circuit: only when there's no live PTY session to
    # route through. When stationed to a bash terminal, cd MUST go through
    # marker-poll so bash's own cwd changes (mutating parent cwd would
    # diverge from the stationed shell's state). Also, this short-circuit
    # only matches bare `cd <path>` / `clear` — never compound commands
    # like `cd /tmp && pwd` (those need real shell parsing).
    has_live_session = (
        (ctx.stationed_terminal is not None and
         getattr(ctx.stationed_terminal, "is_alive", lambda: False)())
        or
        (ctx.interactive_session is not None and
         getattr(ctx.interactive_session, "is_alive", lambda: False)())
    )
    if not has_live_session:
        stripped = command.strip()
        _is_bare_cd = stripped == "cd" or (
            stripped.startswith("cd ")
            and not any(op in stripped for op in ("&&", "||", ";", "|", ">", "<", "`", "$("))
        )
        _is_bare_clear = stripped == "clear" or (
            stripped.startswith("clear ")
            and not any(op in stripped for op in ("&&", "||", ";", "|"))
        )
        if _is_bare_cd:
            path = stripped[3:].strip() if stripped.startswith("cd ") else os.path.expanduser("~")
            if ctx.depth > 0:
                return {
                    "ok": False,
                    "error": (
                        "A sub-agent cannot change the process-global cwd. "
                        "Pass cwd to shell.exec or use 'cd <path> && <command>'."
                    ),
                    "returncode": -1,
                }
            try:
                os.chdir(path)
                return {"ok": True, "result": f"cd → {os.getcwd()}", "returncode": 0}
            except Exception as e:
                return {"ok": False, "error": f"cd error: {e}", "returncode": -1}
        if _is_bare_clear:
            import sys as _sys
            _sys.stdout.write("\033[2J\033[H")
            _sys.stdout.flush()
            return {"ok": True, "result": "", "returncode": 0}

    # Marker-poll inside an existing PTY session.
    # Used by stationed terminals (preferred) or one-shot interactive sessions.
    session = ctx.stationed_terminal or ctx.interactive_session
    if session is not None:
        import uuid as _uuid
        import re as _re
        try:
            marker_id = _uuid.uuid4().hex[:8]
            start_marker = f"__CMD_BEGIN_{marker_id}__"
            end_marker = f"__CMD_END_{marker_id}__"
            wrapped = f"echo {start_marker}; {command} 2>&1; __laintas_rc=$?; echo {end_marker}:$__laintas_rc"
            try:
                old_len = len(session.raw_output)
            except AttributeError:
                old_len = len(session.full_output)
            session.send_keys(wrapped + "\n")
            poll_start = time.time()
            cmd_output = ""
            returncode = -1
            poll_budget = max(timeout, 10.0)
            while time.time() - poll_start < poll_budget:
                time.sleep(0.08)
                session.read_output(timeout=0.1)
                try:
                    raw = session.raw_output
                except AttributeError:
                    raw = session.full_output
                new_content = raw[old_len:] if old_len > 0 else raw
                # The end marker is preceded by an echoed `:$rc` literal in the
                # input line (variable name, not expanded). The real output
                # has `:<digits>` after expansion. Match digits to skip the
                # echoed input line.
                end_match = _re.search(
                    rf'{_re.escape(end_marker)}:(\d+)', new_content
                )
                if end_match:
                    returncode = int(end_match.group(1))
                    # The echoed input line has the start_marker followed by `;`.
                    # The real output has it followed by \r, \n, or \r\n.
                    # We look for the marker followed by a line break or end of
                    # buffer, falling back to the last occurrence to skip the
                    # echoed input.
                    starts = list(_re.finditer(
                        rf'{_re.escape(start_marker)}(?=[\r\n]|$)', new_content
                    ))
                    if starts:
                        # Prefer occurrence that comes before end_match
                        valid = [m for m in starts if m.end() < end_match.start()]
                        chosen = valid[-1] if valid else starts[-1]
                        # Skip any trailing whitespace/CR/LF after the marker
                        body_start = chosen.end()
                        while body_start < len(new_content) and new_content[body_start] in '\r\n':
                            body_start += 1
                        cmd_output = new_content[body_start:end_match.start()]
                        # Strip trailing CR/LF before end marker
                        cmd_output = cmd_output.rstrip('\r\n').strip()
                    else:
                        # Fallback: split on start_marker, take everything
                        # between the LAST start and the end marker
                        parts = new_content.rsplit(start_marker, 1)
                        if len(parts) > 1:
                            tail = parts[1].split(end_marker, 1)[0]
                            cmd_output = tail.strip('\r\n').strip()
                    break
                if not session.is_alive():
                    cmd_output = new_content
                    break
            # Only use the full buffer as fallback when we never found the
            # markers (returncode == -1). When markers were found and the
            # extracted output is legitimately empty (e.g., `cd /tmp` has no
            # stdout), keep cmd_output empty.
            if returncode == -1 and not cmd_output:
                cmd_output = new_content if 'new_content' in locals() else ""
            if ctx.deps:
                cmd_output = ctx.deps.strip_ansi(cmd_output)
            return {"ok": returncode == 0, "result": cmd_output.strip() or "(no output)",
                    "returncode": returncode,
                    "via": "stationed" if ctx.stationed_terminal else "interactive"}
        except Exception:
            pass  # Fall through to subprocess

    # Direct subprocess execution
    try:
        import subprocess as _sp
        result = _sp.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        return {"ok": result.returncode == 0, "result": output or "(no output)",
                "returncode": result.returncode, "via": "subprocess"}
    except _sp.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out ({timeout}s): {command[:120]}",
                "returncode": -1}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "returncode": -1}


# ── Browser live-view debug tools (P1) ──────────────────────────────────

def _bi_browser_debug_open(params: dict, ctx: ToolCtx) -> dict:
    """Open a headless-browser live-view session for debugging.

    Spawns Xvfb + Chrome + x11vnc on the host and starts a background WS
    bridge to the backend /vnc relay.  Returns connection details.  The WS
    relay will show connected=False until the backend deploys /vnc — that's
    expected; the host stack (Chrome + CDP + VNC) is up regardless.
    """
    try:
        import browser_session as _bs
    except ImportError:
        return {"ok": False, "error": "browser_session module not available"}

    url = params.get("url", "about:blank").strip() or "about:blank"
    name = params.get("name") or None
    width = int(params.get("width", 1280) or 1280)
    height = int(params.get("height", 800) or 800)

    backend = os.environ.get("LAINTAS_BACKEND", "http://localhost:8000")
    agent_id = ctx.agent_id or "debug"
    session_id = f"browser-{int(__import__('time').time() * 1000)}"

    sess = _bs.BrowserSession(
        backend_url=backend,
        agent_id=agent_id,
        agent_secret="debug",
        session_id=session_id,
        url=url,
        width=width,
        height=height,
    )
    try:
        sess.start()
    except Exception as e:
        sess.close()
        return {"ok": False, "error": f"start failed: {e}"}

    registered = _bs.register_browser_session(sess, name=name)

    # Probe CDP to prove Chrome is really up.
    cdp_ver = "?"
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"{sess.cdp_endpoint()}/json/version", timeout=3) as r:
            import json as _json
            ver = _json.loads(r.read().decode("utf-8", "replace"))
        cdp_ver = ver.get("Browser", "?")
    except Exception:
        pass

    return {
        "ok": True,
        "result": (
            f"Browser session '{registered}' is up.\n"
            f"  url      : {sess.url}\n"
            f"  display  :{sess.display_n}\n"
            f"  cdp      : {sess.cdp_endpoint()}\n"
            f"  cdp ver  : {cdp_ver}\n"
            f"  vnc      : 127.0.0.1:{sess.rfb_port}\n"
            f"  ws relay : {'connected' if sess.ws_connected() else 'retrying (backend /vnc not deployed)'}\n"
            f"  chrome   : pid {sess._chrome.pid if sess._chrome else '-'}"
        ),
        "name": registered,
        "cdp_endpoint": sess.cdp_endpoint(),
        "rfb_port": sess.rfb_port,
        "display": sess.display_n,
    }


def _bi_browser_debug_close(params: dict, ctx: ToolCtx) -> dict:
    """Close a headless-browser session by name."""
    try:
        import browser_session as _bs
    except ImportError:
        return {"ok": False, "error": "browser_session module not available"}

    name = params.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "missing 'name'"}
    ok = _bs.unregister_browser_session(name)
    return {"ok": ok, "result": f"closed '{name}'" if ok else f"no session named '{name}'"}


def _bi_browser_debug_list(params: dict, ctx: ToolCtx) -> dict:
    """List active browser sessions."""
    try:
        import browser_session as _bs
    except ImportError:
        return {"ok": False, "error": "browser_session module not available"}

    sessions = _bs.get_all_browser_sessions()
    if not sessions:
        return {"ok": True, "result": "no active browser sessions"}

    lines = []
    with _bs._browser_lock:
        for nm, sess in _bs._browser_sessions.items():
            lines.append(
                f"  {nm}: url={sess.url} display=:{sess.display_n} "
                f"cdp={sess.cdp_endpoint()} vnc=:{sess.rfb_port} "
                f"alive={sess.is_alive()} ws={sess.ws_connected()}"
            )
    return {"ok": True, "result": f"{len(sessions)} session(s):\n" + "\n".join(lines)}


# ── Browser automation tools (P2) ───────────────────────────────────────

def _browser_resolve_session(params: dict):
    """Resolve a BrowserSession from params['session'], most recent session,
    or auto-create a default about:blank session when none exists."""
    try:
        import browser_session as _bs
    except ImportError:
        return None, "browser_session module not available"

    name = params.get("session", "").strip()
    if name:
        sess = _bs.get_browser_session(name)
        if sess is None:
            return None, f"no browser session named '{name}'"
        if not sess.is_alive():
            return None, f"browser session '{name}' is not alive"
        return sess, name

    with _bs._browser_lock:
        if _bs._browser_sessions:
            name = list(_bs._browser_sessions.keys())[-1]
            sess = _bs._browser_sessions[name]
            if not sess.is_alive():
                return None, f"browser session '{name}' is not alive"
            return sess, name

    backend = os.environ.get("LAINTAS_BACKEND", "http://localhost:8000")
    session_id = f"browser-{int(time.time() * 1000)}"
    width = int(params.get("width", 1280) or 1280)
    height = int(params.get("height", 800) or 800)
    sess = _bs.BrowserSession(
        backend_url=backend, agent_id="debug", agent_secret="debug",
        session_id=session_id, url="about:blank", width=width, height=height,
    )
    try:
        sess.start()
    except Exception as e:
        sess.close()
        return None, f"failed to auto-create browser session: {e}"
    name = _bs.register_browser_session(sess, name="default")
    return sess, name


def _browser_check_action(action: str, params: dict, ctx: ToolCtx):
    """Check browser-action policy and request approval if needed.

    Returns None if the action may proceed, or a dict {ok:False, error:...}
    to short-circuit the tool call.
    """
    try:
        import policy as _policy
    except ImportError:
        return None  # no policy module → allow

    decision = _policy.evaluate_browser_action(
        action, params, agent_id=ctx.agent_id)
    if decision.action == "deny":
        return {"ok": False, "error": f"blocked by policy: {decision.reason}"}
    if decision.action == "needs_approval":
        # Build a human-readable command string for the approval prompt.
        url = params.get("url", "")
        selector = params.get("selector", "")
        if action == "navigate":
            cmd = f"browser.navigate {url}"
        elif action == "click":
            cmd = f"browser.click {selector}"
        elif action == "type":
            cmd = f"browser.type {selector}"
        elif action == "evaluate":
            cmd = f"browser.evaluate {params.get('script', '')[:200]}"
        elif action == "select":
            cmd = f"browser.select {selector}"
        elif action == "press_key":
            cmd = f"browser.press_key {params.get('key', '')}"
        else:
            cmd = f"browser.{action}"

        approve_fn = getattr(ctx.deps, "request_command_approval", None) if ctx.deps else None
        if approve_fn is None:
            # No approval callback available (e.g., running outside agent loop).
            # In enforce mode this means we can't ask → block.
            return {"ok": False, "error": f"action requires approval but no approval callback is available: {cmd}"}
        approved = approve_fn(cmd, decision.reason)
        if not approved:
            return {"ok": False, "error": f"action not approved: {cmd}", "_user_denied": True}
    return None


def _browser_resolve_selector(params: dict):
    """Resolve 'ref' or 'selector' parameter to a CSS selector string.

    Returns (selector_string, None) on success, or (None, error_msg) on failure.
    If 'ref' is provided, it takes priority over 'selector'.
    """
    ref = params.get("ref")
    if ref is not None:
        try:
            n = int(ref)
            if n < 1:
                return None, f"invalid ref value (must be >= 1): {ref}"
            return f'[data-laintas-ref="{n}"]', None
        except (ValueError, TypeError):
            return None, f"invalid ref value (must be a number): {ref}"
    selector = params.get("selector", "").strip()
    if not selector:
        return None, "missing 'selector' or 'ref' — call browser.snapshot first to get ref numbers"
    return selector, None


def _browser_antibot_delay():
    """Sleep a random anti-bot delay before browser actions."""
    try:
        from agent_loop import get_runtime_config
        lo = float(get_runtime_config("browser_action_delay_min"))
        hi = float(get_runtime_config("browser_action_delay_max"))
        if hi > 0 and hi > lo:
            import random
            import time
            time.sleep(random.uniform(lo, hi))
    except Exception:
        pass


def _browser_auto_snapshot(sess, max_chars: int = 2000) -> str:
    """Generate a compact page snapshot for auto-return after actions."""
    try:
        try:
            from agent_loop import get_runtime_config
            wait_s = float(get_runtime_config("browser_post_action_wait") or 0)
            if wait_s > 0:
                time.sleep(min(wait_s, 5.0))
        except Exception:
            pass
        page = sess.get_page()
        url = page.url
        title = page.title()
        text = page.inner_text("body")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"
        result = f"url: {url}\ntitle: {title}\n\n{text}"
        try:
            refs = sess.inject_refs()
            if refs:
                ref_lines = []
                for r in refs[:30]:
                    parts = [f"[{r['ref']}] <{r['tag']}>"]
                    if r.get("text"):
                        parts.append(f"text={r['text'][:60]}")
                    if r.get("href"):
                        parts.append(f"href={r['href'][:60]}")
                    if r.get("placeholder"):
                        parts.append(f"placeholder={r['placeholder'][:60]}")
                    if r.get("role"):
                        parts.append(f"role={r['role']}")
                    if r.get("type"):
                        parts.append(f"type={r['type']}")
                    if r.get("value"):
                        parts.append(f"value={r['value'][:60]}")
                    ref_lines.append(" | ".join(parts))
                result += "\n\n── Interactive elements (ref) ──\n" + "\n".join(ref_lines)
        except Exception:
            pass
        return result
    except Exception as e:
        return f"(snapshot failed: {e})"


def _browser_should_auto_snapshot() -> bool:
    try:
        from agent_loop import get_runtime_config
        return bool(get_runtime_config("browser_auto_snapshot"))
    except Exception:
        return True


def _bi_browser_open(params: dict, ctx: ToolCtx) -> dict:
    """Open a new headless-browser session with live-view relay."""
    blocked = _browser_check_action("navigate", params, ctx)
    if blocked:
        return blocked
    try:
        import browser_session as _bs
    except ImportError:
        return {"ok": False, "error": "browser_session module not available"}

    url = params.get("url", "about:blank").strip() or "about:blank"
    name = params.get("name") or None
    width = int(params.get("width", 1280) or 1280)
    height = int(params.get("height", 800) or 800)

    backend = os.environ.get("LAINTAS_BACKEND", "http://localhost:8000")
    agent_id = ctx.agent_id or "debug"
    session_id = f"browser-{int(__import__('time').time() * 1000)}"

    sess = _bs.BrowserSession(
        backend_url=backend, agent_id=agent_id, agent_secret="debug",
        session_id=session_id, url=url, width=width, height=height,
    )
    try:
        sess.start()
    except Exception as e:
        sess.close()
        return {"ok": False, "error": f"start failed: {e}"}

    registered = _bs.register_browser_session(sess, name=name)
    return {
        "ok": True,
        "result": (
            f"Browser session '{registered}' is up.\n"
            f"  url      : {sess.url}\n"
            f"  display  :{sess.display_n}\n"
            f"  cdp      : {sess.cdp_endpoint()}\n"
            f"  vnc      : 127.0.0.1:{sess.rfb_port}\n"
            f"  ws relay : {'connected' if sess.ws_connected() else 'retrying (backend /vnc not deployed)'}"
        ),
        "name": registered,
        "cdp_endpoint": sess.cdp_endpoint(),
        "rfb_port": sess.rfb_port,
    }


def _bi_browser_close(params: dict, ctx: ToolCtx) -> dict:
    """Close a browser session by name."""
    try:
        import browser_session as _bs
    except ImportError:
        return {"ok": False, "error": "browser_session module not available"}
    name = params.get("name", params.get("session", "")).strip()
    if not name:
        # Close the most recent session.
        with _bs._browser_lock:
            if not _bs._browser_sessions:
                return {"ok": False, "error": "no active browser session"}
            name = list(_bs._browser_sessions.keys())[-1]
    ok = _bs.unregister_browser_session(name)
    return {"ok": ok, "result": f"closed '{name}'" if ok else f"no session named '{name}'"}


def _bi_browser_navigate(params: dict, ctx: ToolCtx) -> dict:
    """Navigate to a URL."""
    blocked = _browser_check_action("navigate", params, ctx)
    if blocked:
        return blocked
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    url = params.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    # SSRF / scheme guard — refuse loopback/private/link-local/metadata targets.
    if url not in ("about:blank",) and not url.startswith("about:"):
        import browser_session as _bs
        try:
            url = _bs.validate_browse_url(url)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    wait_until = params.get("wait_until", "domcontentloaded")
    timeout = int(params.get("timeout", 30) or 30) * 1000
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        page.goto(url, wait_until=wait_until, timeout=timeout)
        title = page.title()
        result = f"navigated to {url}\ntitle: {title}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result, "title": title, "url": page.url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_click(params: dict, ctx: ToolCtx) -> dict:
    """Click an element by CSS selector or ref number."""
    blocked = _browser_check_action("click", params, ctx)
    if blocked:
        return blocked
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    selector, serr = _browser_resolve_selector(params)
    if selector is None:
        return {"ok": False, "error": serr}
    timeout = int(params.get("timeout", 10) or 10) * 1000
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        el = page.wait_for_selector(selector, state="visible", timeout=timeout)
        if el is None:
            return {"ok": False, "error": f"element not found: {selector}"}
        el.click()
        result = f"clicked: {selector}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_type(params: dict, ctx: ToolCtx) -> dict:
    """Type text into an element identified by CSS selector or ref number."""
    blocked = _browser_check_action("type", params, ctx)
    if blocked:
        return blocked
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    selector, serr = _browser_resolve_selector(params)
    if selector is None:
        return {"ok": False, "error": serr}
    text = params.get("text", "")
    if not text:
        return {"ok": False, "error": "missing 'text'"}
    delay = int(params.get("delay", 0) or 0)
    clear = params.get("clear", True)
    timeout = int(params.get("timeout", 10) or 10) * 1000
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        el = page.wait_for_selector(selector, state="visible", timeout=timeout)
        if el is None:
            return {"ok": False, "error": f"element not found: {selector}"}
        if clear:
            el.fill("")
        if delay > 0:
            el.type(text, delay=delay)
        else:
            el.type(text)
        result = f"typed {len(text)} chars into {selector}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_screenshot(params: dict, ctx: ToolCtx) -> dict:
    """Take a screenshot and save to a file. Returns the file path."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    full_page = params.get("full_page", False)
    path = params.get("path", "").strip()
    if not path:
        import tempfile as _tf
        path = _tf.mktemp(prefix="browser-shot-", suffix=".png")
    try:
        page = sess.get_page()
        page.screenshot(path=path, full_page=full_page)
        import os as _os
        size = _os.path.getsize(path)
        return {"ok": True, "result": f"screenshot saved: {path} ({size} bytes)", "path": path}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_query(params: dict, ctx: ToolCtx) -> dict:
    """Query DOM elements by CSS selector and return their text/attributes."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    selector = params.get("selector", "").strip()
    if not selector:
        return {"ok": False, "error": "missing 'selector'"}
    limit = int(params.get("limit", 20) or 20)
    attribute = params.get("attribute", "").strip()
    try:
        page = sess.get_page()
        elements = page.query_selector_all(selector)
        if not elements:
            return {"ok": True, "result": f"no elements matched: {selector}", "count": 0}
        results = []
        for el in elements[:limit]:
            entry = {"tag": el.evaluate("e => e.tagName.toLowerCase()")}
            if attribute:
                val = el.get_attribute(attribute)
                entry[attribute] = val or ""
            else:
                for attr in ("href", "src", "value", "placeholder", "type", "id", "class", "name", "role", "aria-label"):
                    val = el.get_attribute(attr)
                    if val:
                        entry[attr] = val
            entry["text"] = (el.inner_text() or "").strip()[:500]
            results.append(entry)
        lines = []
        for r in results:
            parts = [f"  {r['tag']}"]
            for k, v in r.items():
                if k == "tag":
                    continue
                if v:
                    parts.append(f"{k}={v[:120]}")
            lines.append(" | ".join(parts))
        return {"ok": True, "result": f"{len(results)} element(s):\n" + "\n".join(lines),
                "count": len(results), "elements": results}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_snapshot(params: dict, ctx: ToolCtx) -> dict:
    """Return a text snapshot of the page: URL, title, visible text, and
    numbered refs for interactive elements."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    max_chars = int(params.get("max_chars", 5000) or 5000)
    try:
        page = sess.get_page()
        text = page.inner_text("body")
        url = page.url
        title = page.title()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"
        result = f"url: {url}\ntitle: {title}\n\n{text}"
        refs = []
        try:
            refs = sess.inject_refs()
            if refs:
                ref_lines = []
                for r in refs:
                    parts = [f"[{r['ref']}] <{r['tag']}>"]
                    if r.get("text"):
                        parts.append(f"text={r['text'][:80]}")
                    if r.get("href"):
                        parts.append(f"href={r['href'][:80]}")
                    if r.get("placeholder"):
                        parts.append(f"placeholder={r['placeholder'][:80]}")
                    if r.get("role"):
                        parts.append(f"role={r['role']}")
                    if r.get("type"):
                        parts.append(f"type={r['type']}")
                    if r.get("value"):
                        parts.append(f"value={r['value'][:80]}")
                    if r.get("aria_label"):
                        parts.append(f"aria-label={r['aria_label'][:80]}")
                    ref_lines.append(" | ".join(parts))
                result += "\n\n── Interactive elements (use ref number in browser.click/type/select) ──\n"
                result += "\n".join(ref_lines)
        except Exception:
            pass
        return {"ok": True, "result": result, "url": url, "title": title, "refs": refs}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_scroll(params: dict, ctx: ToolCtx) -> dict:
    """Scroll the page by coordinates or scroll an element into view."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    selector = params.get("selector", "").strip()
    x = params.get("x")
    y = params.get("y")
    try:
        page = sess.get_page()
        if selector:
            el = page.query_selector(selector)
            if el is None:
                return {"ok": False, "error": f"element not found: {selector}"}
            el.scroll_into_view_if_needed()
            return {"ok": True, "result": f"scrolled '{selector}' into view"}
        dx = int(x) if x is not None else 0
        dy = int(y) if y is not None else 0
        page.mouse.wheel(dx, dy)
        return {"ok": True, "result": f"scrolled by ({dx}, {dy})"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_evaluate(params: dict, ctx: ToolCtx) -> dict:
    """Evaluate a JavaScript expression on the page and return the result."""
    blocked = _browser_check_action("evaluate", params, ctx)
    if blocked:
        return blocked
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    script = params.get("script", "").strip()
    if not script:
        return {"ok": False, "error": "missing 'script'"}
    try:
        page = sess.get_page()
        result = page.evaluate(script)
        return {"ok": True, "result": str(result)[:5000], "value": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_press_key(params: dict, ctx: ToolCtx) -> dict:
    """Press a keyboard key (e.g., Enter, Tab, Escape, ArrowDown)."""
    blocked = _browser_check_action("press_key", params, ctx)
    if blocked:
        return blocked
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    key = params.get("key", "").strip()
    if not key:
        return {"ok": False, "error": "missing 'key'"}
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        page.keyboard.press(key)
        result = f"pressed: {key}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_get_url(params: dict, ctx: ToolCtx) -> dict:
    """Get the current page URL."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    try:
        page = sess.get_page()
        url = page.url
        return {"ok": True, "result": url, "url": url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_get_title(params: dict, ctx: ToolCtx) -> dict:
    """Get the current page title."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    try:
        page = sess.get_page()
        title = page.title()
        return {"ok": True, "result": title, "title": title}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_wait_for(params: dict, ctx: ToolCtx) -> dict:
    """Wait for an element to reach a state (visible/hidden/attached/detached)."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    selector = params.get("selector", "").strip()
    if not selector:
        return {"ok": False, "error": "missing 'selector'"}
    state = params.get("state", "visible")
    timeout = int(params.get("timeout", 15) or 15) * 1000
    try:
        page = sess.get_page()
        el = page.wait_for_selector(selector, state=state, timeout=timeout)
        if state in ("hidden", "detached"):
            return {"ok": True, "result": f"element '{selector}' is now {state}"}
        if el is None:
            return {"ok": False, "error": f"element not found: {selector}"}
        return {"ok": True, "result": f"element '{selector}' is {state}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_select(params: dict, ctx: ToolCtx) -> dict:
    """Select an option in a <select> element by value or label."""
    blocked = _browser_check_action("select", params, ctx)
    if blocked:
        return blocked
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    selector, serr = _browser_resolve_selector(params)
    if selector is None:
        return {"ok": False, "error": serr}
    value = params.get("value", "").strip()
    label = params.get("label", "").strip()
    if not value and not label:
        return {"ok": False, "error": "missing 'value' or 'label'"}
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        el = page.query_selector(selector)
        if el is None:
            return {"ok": False, "error": f"select element not found: {selector}"}
        if label:
            selected = el.select_option(label=label)
        else:
            selected = el.select_option(value=value)
        result = f"selected '{selected}' in {selector}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_go_back(params: dict, ctx: ToolCtx) -> dict:
    """Navigate back in browser history."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        page.go_back(wait_until="domcontentloaded", timeout=15000)
        result = f"went back, now at: {page.url}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result, "url": page.url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bi_browser_go_forward(params: dict, ctx: ToolCtx) -> dict:
    """Navigate forward in browser history."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    _browser_antibot_delay()
    try:
        page = sess.get_page()
        page.go_forward(wait_until="domcontentloaded", timeout=15000)
        result = f"went forward, now at: {page.url}"
        if _browser_should_auto_snapshot():
            result += "\n\n" + _browser_auto_snapshot(sess)
        return {"ok": True, "result": result, "url": page.url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── Browser testing: runtime-error capture + assertion ──────────────────────
def _bi_browser_get_console(params: dict, ctx: ToolCtx) -> dict:
    """Return console messages captured from the page (optional level filter)."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    level = (params.get("level") or "all").strip()
    msgs = sess.get_console(level)
    if not msgs:
        return {"ok": True, "result": "(no console messages)", "count": 0}
    shown = msgs[-200:]
    lines = [f"  [{m['type']}] {m['text']}" + (f"  ({m['location']})" if m.get('location') else "") for m in shown]
    return {"ok": True, "result": f"{len(msgs)} console message(s):\n" + "\n".join(lines),
            "count": len(msgs), "messages": shown}


def _bi_browser_get_errors(params: dict, ctx: ToolCtx) -> dict:
    """Runtime-problems digest: uncaught JS exceptions + console.error +
    failed/4xx-5xx network requests. The key 'did the page break' test signal."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    page_errs = sess.get_page_errors()
    console_errs = sess.get_console("error")
    net_errs = sess.get_network_errors()
    total = len(page_errs) + len(console_errs) + len(net_errs)
    if total == 0:
        return {"ok": True, "clean": True, "count": 0,
                "result": "No runtime errors captured (no JS exceptions, console errors, or failed/4xx-5xx requests)."}
    lines = []
    if page_errs:
        lines.append(f"JS exceptions ({len(page_errs)}):")
        lines += [f"  - {e['message']}" for e in page_errs[-20:]]
    if console_errs:
        lines.append(f"console.error ({len(console_errs)}):")
        lines += [f"  - {e['text']}" + (f"  ({e['location']})" if e.get('location') else "") for e in console_errs[-20:]]
    if net_errs:
        lines.append(f"network failures ({len(net_errs)}):")
        lines += [f"  - {e.get('status','FAIL')} {e['method']} {e['url']}" + (f" ({e.get('failure')})" if e.get('failure') else "") for e in net_errs[-20:]]
    return {"ok": True, "clean": False, "count": total,
            "result": f"{total} runtime problem(s):\n" + "\n".join(lines),
            "page_errors": page_errs, "console_errors": console_errs, "network_errors": net_errs}


def _expect_result(passed: bool, expectation: str, actual: str) -> dict:
    return {"ok": True, "pass": bool(passed),
            "result": ("PASS" if passed else "FAIL") + f": expected {expectation}; {actual}",
            "expectation": expectation, "actual": actual}


def _bi_browser_expect(params: dict, ctx: ToolCtx) -> dict:
    """Assert a condition about the page; returns ok=True with pass=True/False.
    Conditions: url_contains / title_contains (page-level), or selector + one of
    text / state(visible|hidden) / count (default: element exists)."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    try:
        page = sess.get_page()
        url_c = params.get("url_contains")
        if url_c is not None:
            return _expect_result(url_c in (page.url or ""), f"url contains {url_c!r}", f"url is {page.url!r}")
        title_c = params.get("title_contains")
        if title_c is not None:
            t = page.title() or ""
            return _expect_result(title_c in t, f"title contains {title_c!r}", f"title is {t!r}")
        selector, serr = _browser_resolve_selector(params)
        if serr:
            return {"ok": False, "error": serr}
        if not selector:
            return {"ok": False, "error": "expect needs selector + a condition (text/state/count), or url_contains/title_contains"}
        els = page.query_selector_all(selector)
        if params.get("count") is not None:
            want = int(params["count"])
            return _expect_result(len(els) == want, f"{selector} count == {want}", f"found {len(els)}")
        state = params.get("state")
        if state in ("visible", "hidden"):
            visible = bool(els) and els[0].is_visible()
            ok = visible if state == "visible" else (not visible)
            return _expect_result(ok, f"{selector} is {state}", f"visible={visible}, matched={len(els)}")
        text = params.get("text")
        if text is not None:
            if not els:
                return _expect_result(False, f"{selector} text contains {text!r}", "selector not found")
            actual = (els[0].inner_text() or "")
            return _expect_result(text in actual, f"{selector} text contains {text!r}", f"text is {actual.strip()[:200]!r}")
        return _expect_result(len(els) > 0, f"{selector} exists", f"matched {len(els)}")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Steps a test flow may run, mapped to the existing single-action handlers.
# Each step is {"action": <name>, ...params}; params pass straight through.
_TEST_FLOW_ACTIONS = {
    "navigate": _bi_browser_navigate,
    "click": _bi_browser_click,
    "type": _bi_browser_type,
    "select": _bi_browser_select,
    "press_key": _bi_browser_press_key,
    "scroll": _bi_browser_scroll,
    "wait_for": _bi_browser_wait_for,
    "evaluate": _bi_browser_evaluate,
    "expect": _bi_browser_expect,
}


def _bi_browser_test_flow(params: dict, ctx: ToolCtx) -> dict:
    """Run a multi-step browser test and return a pass/fail report.

    Each step is {"action": navigate|click|type|select|press_key|scroll|
    wait_for|evaluate|expect, ...params}. A step fails when its handler returns
    ok=False, or (for 'expect') pass=False. Execution stops at the first failed
    step, a screenshot is captured, and — when check_errors is set — captured
    runtime errors (JS exceptions / console.error / failed requests) also fail
    the flow. Returns a structured report with per-step results."""
    sess, err = _browser_resolve_session(params)
    if sess is None:
        return {"ok": False, "error": err}
    steps = params.get("steps")
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "test_flow needs a non-empty 'steps' array"}
    session_name = params.get("session")
    check_errors = params.get("check_errors", True)
    shot_on_fail = params.get("screenshot_on_failure", True)
    if params.get("clear_captures", True):
        try:
            sess.clear_captures()
        except Exception:
            pass

    results = []
    failed_at = None
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            failed_at = i
            results.append({"step": i, "action": None, "pass": False, "detail": "step is not an object"})
            break
        action = (step.get("action") or "").strip()
        handler = _TEST_FLOW_ACTIONS.get(action)
        if handler is None:
            failed_at = i
            results.append({"step": i, "action": action, "pass": False,
                            "detail": f"unknown action {action!r}; valid: {', '.join(sorted(_TEST_FLOW_ACTIONS))}"})
            break
        sp = {k: v for k, v in step.items() if k != "action"}
        if session_name and "session" not in sp:
            sp["session"] = session_name
        try:
            r = handler(sp, ctx)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ok = bool(r.get("ok"))
        passed = ok and (r.get("pass", True) is not False)
        detail = r.get("result") or r.get("error") or ""
        results.append({"step": i, "action": action, "pass": passed, "detail": detail})
        if not passed:
            failed_at = i
            break

    # Runtime-error gate: only when the steps themselves all passed.
    error_digest = None
    if check_errors and failed_at is None:
        ed = _bi_browser_get_errors({"session": session_name} if session_name else {}, ctx)
        if ed.get("ok") and not ed.get("clean", True):
            error_digest = ed.get("result")
            results.append({"step": len(steps), "action": "check_errors", "pass": False, "detail": error_digest})
            failed_at = len(steps)

    passed_all = failed_at is None
    shot_path = None
    if not passed_all and shot_on_fail:
        s = _bi_browser_screenshot({"session": session_name} if session_name else {}, ctx)
        if s.get("ok"):
            shot_path = s.get("path")

    ran = len(results)
    n_pass = sum(1 for r in results if r["pass"])
    summary = ("PASS" if passed_all else "FAIL") + f": {n_pass}/{ran} step(s) passed"
    lines = [summary]
    for r in results:
        mark = "✓" if r["pass"] else "✗"
        lines.append(f"  {mark} [{r['step']}] {r['action']}: {str(r['detail'])[:300]}")
    if shot_path:
        lines.append(f"  failure screenshot: {shot_path}")
    return {"ok": True, "pass": passed_all, "failed_at": failed_at,
            "result": "\n".join(lines), "steps": results,
            "screenshot": shot_path, "errors": error_digest}


def register_builtin_tools() -> None:
    """Idempotent — safe to call multiple times."""
    builtins = [
        Tool(
            name="mem.read",
            description="Read the agent's persistent .laintas/memory.json file in full.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_mem_read,
        ),
        Tool(
            name="mem.save",
            description="Save a persistent memory that survives across sessions. "
                        "Types: user (profile/preferences), feedback (corrections/confirmations), "
                        "project (goals/deadlines), reference (external resources). "
                        "Use this to remember important facts for future conversations.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short kebab-case slug (e.g., 'user-role')"},
                    "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                            "description": "memory category"},
                    "description": {"type": "string", "description": "one-line summary for the index"},
                    "body": {"type": "string", "description": "full memory content (markdown)"},
                    "scope": {"type": "string", "enum": ["user", "project"],
                              "description": "defaults to user for user/feedback and project for project/reference"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1,
                                   "description": "durable importance, default 0.5"},
                },
                "required": ["name", "type", "description", "body"],
            },
            invoke=_bi_mem_save,
        ),
        Tool(
            name="mem.delete",
            description="Delete a persistent memory entry by name.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "memory slug to delete"},
                },
                "required": ["name"],
            },
            invoke=_bi_mem_delete,
        ),
        Tool(
            name="mem.list",
            description="List all persistent memories, optionally filtered by type.",
            schema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                            "description": "filter by type (omit for all)"},
                },
            },
            invoke=_bi_mem_list,
        ),
        Tool(
            name="skill.list",
            description="List available skills with short descriptions and loaded status. "
                        "Use this when deciding whether specialized instructions are available.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_skill_list,
        ),
        Tool(
            name="skill.load",
            description="Load a named skill's full instructions into subsequent context. "
                        "Call before starting specialized work when the skill catalog has a relevant skill.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name from the catalog"},
                },
                "required": ["name"],
            },
            invoke=_bi_skill_load,
        ),
        Tool(
            name="skill.unload",
            description="Unload a previously loaded skill: drop its tools and stop injecting its "
                        "instructions. Call when you are done with that specialized work to free "
                        "context. The skill stays available and can be re-loaded later.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name to unload"},
                },
                "required": ["name"],
            },
            invoke=_bi_skill_unload,
        ),
        Tool(
            name="fs.read",
            description="Read a file as UTF-8 text. Use offset/limit for large files. "
                        "Output is prefixed with line numbers (cat -n style) so the AI "
                        "can refer to specific lines in follow-up fs.edit calls. "
                        "Prefer this over `cat` for source files.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "absolute or cwd-relative"},
                    "offset": {"type": "integer", "default": 1,
                                "description": "1-based starting line"},
                    "limit": {"type": "integer", "default": 2000,
                                "description": "max lines to return"},
                    "max_bytes": {"type": "integer", "default": 200000,
                                  "description": "hard byte cap on the returned payload"},
                    "line_numbers": {"type": "boolean", "default": True,
                                       "description": "prepend each line with 'N→ '"},
                },
                "required": ["path"],
            },
            invoke=_bi_fs_read,
        ),
        Tool(
            name="fs.write",
            description="Overwrite a file with the provided UTF-8 string content.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            invoke=_bi_fs_write,
        ),
        Tool(
            name="fs.delete",
            description="Delete one file, symlink, or directory through the security policy. "
                        "Non-empty directories require recursive=true. The target is inspected "
                        "again after approval and deletion is cancelled if it changed.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "absolute or cwd-relative target"},
                    "recursive": {"type": "boolean", "default": False,
                                  "description": "required for non-empty directories"},
                },
                "required": ["path"],
            },
            invoke=_bi_fs_delete,
        ),
        Tool(
            name="fs.ls",
            description="List files in a directory (one level). Returns name/type/size.",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
            invoke=_bi_fs_ls,
        ),
        Tool(
            name="time.now",
            description="Return the current epoch time and ISO-8601 timestamp.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_time_now,
        ),
        Tool(
            name="fs.edit",
            description="Exact string replacement in a file. Replace old_string with new_string. "
                        "Use replace_all:true if the string is not unique. "
                        "This is the primary tool for editing code — prefer it over fs.write.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "absolute or cwd-relative path"},
                    "old_string": {"type": "string", "description": "exact text to find and replace"},
                    "new_string": {"type": "string", "description": "replacement text"},
                    "replace_all": {"type": "boolean", "default": False,
                                    "description": "replace all occurrences (required if not unique)"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            invoke=_bi_fs_edit,
        ),
        Tool(
            name="fs.multi_edit",
            description="Apply multiple sequential exact-string edits to one file atomically. "
                        "Edits run in order; if any fails, the file is left untouched. "
                        "Use this when one fs.edit call would conflict with another in the "
                        "same file (e.g. rename + import update).",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean", "default": False},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            invoke=_bi_fs_multi_edit,
        ),
        Tool(
            name="fs.diff",
            description="Compute a unified diff between two files, or between a file and an "
                        "inline text payload. Use to preview changes before fs.edit, or to "
                        "explain what changed between two file revisions. To inspect git "
                        "working-tree changes for one file, pass {\"path\":\"...\"}.",
            schema={
                "type": "object",
                "properties": {
                    "a": {"type": "string", "description": "path to file A"},
                    "path": {"type": "string",
                             "description": "compatibility alias: git diff for this path when b/b_text are omitted"},
                    "b": {"type": "string", "description": "path to file B (alt to b_text)"},
                    "b_text": {"type": "string",
                                "description": "inline text to compare against A"},
                    "context": {"type": "integer", "default": 3},
                    "label_a": {"type": "string"},
                    "label_b": {"type": "string"},
                },
                "required": [],
            },
            invoke=_bi_fs_diff,
        ),
        Tool(
            name="shell.exec",
            description="Run a non-interactive shell command via subprocess (no PTY). "
                        "Returns stdout, stderr, exit code, duration. Use for one-shot "
                        "commands that need structured output (jq, grep with parsing, "
                        "python -c). Prefer the bare `command` field for interactive or "
                        "PTY-driven work — that path streams output and supports more.",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                    "stdin": {"type": "string"},
                },
                "required": ["command"],
            },
            invoke=_bi_shell_exec,
        ),
        Tool(
            name="fs.grep",
            description="Search files for a regex pattern. Returns matching lines with file, "
                        "line number, and content. Respects include/exclude glob filters.",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "regex pattern to search for"},
                    "path": {"type": "string", "default": ".", "description": "file or directory to search"},
                    "include": {"type": "string", "default": "**/*",
                                "description": "comma-separated glob patterns to include"},
                    "exclude": {"type": "string", "default": "",
                                "description": "comma-separated glob patterns to exclude"},
                    "max_results": {"type": "integer", "default": 100},
                    "case_sensitive": {"type": "boolean", "default": True},
                },
                "required": ["pattern"],
            },
            invoke=_bi_fs_grep,
        ),
        Tool(
            name="fs.glob",
            description="Find files matching glob pattern(s). Returns paths with type and size. "
                        "Use for discovering files by name pattern, extension, or directory.",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "default": "**/*",
                                "description": "comma-separated glob patterns (e.g., '**/*.py' or 'src/**/*.ts')"},
                    "path": {"type": "string", "default": ".", "description": "base directory for search"},
                    "max_results": {"type": "integer", "default": 200},
                },
                "required": [],
            },
            invoke=_bi_fs_glob,
        ),
        Tool(
            name="web.search",
            description="Search the web and return results with title, URL, and snippet. "
                        "Use this for finding documentation, solutions, or current information.",
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "search query"},
                    "max_results": {"type": "integer", "default": 10, "description": "max results (1-20)"},
                },
                "required": ["query"],
            },
            invoke=_bi_web_search,
        ),
        Tool(
            name="web.fetch",
            description="Fetch a URL and extract its text content. "
                        "Use for reading documentation pages, API references, or any web content.",
            schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_bytes": {"type": "integer", "default": 65536,
                                  "description": "max response bytes to process"},
                    "timeout": {"type": "integer", "default": 15,
                                "description": "request timeout in seconds"},
                },
                "required": ["url"],
            },
            invoke=_bi_web_fetch,
        ),
        Tool(
            name="task.create",
            description="Create an executable Step in the active WorkGraph. "
                        "In ACT mode, decompose the approved plan into specific steps "
                        "with clear names. Tasks have status (pending→in_progress→completed), "
                        "dependencies (blocks/blockedBy), progress (0-100), "
                        "notes, and metadata. Use session_only=true for ephemeral "
                        "tasks that won't persist across sessions.",
            schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Brief, actionable title — describe the actual work, not the user's raw request (e.g., 'Refactor auth module', NOT 'help me refactor')"},
                    "description": {"type": "string", "default": "",
                                    "description": "Detailed description of what needs to be done"},
                    "metadata": {"type": "object", "default": {},
                                 "description": "Arbitrary metadata (tags, priority, etc.)"},
                    "session_only": {"type": "boolean", "default": False,
                                     "description": "If true, task exists only in this session"},
                    "parent_task_id": {"type": "string",
                                       "description": "Optional hierarchy parent; does not create an execution dependency"},
                },
                "required": ["subject"],
            },
            invoke=_bi_task_create,
        ),
        Tool(
            name="task.update",
            description="Update a WorkGraph Step's status, description, dependencies, progress, "
                        "notes, or metadata. Use addSubtask to create a child task "
                        "with automatic dependency linking.",
            schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task ID to update"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"],
                              "description": "New status"},
                    "subject": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description"},
                    "progress": {"type": "integer", "minimum": 0, "maximum": 100,
                                 "description": "Progress percentage (0-100)"},
                    "notes": {"type": "string",
                              "description": "Append a progress note (timestamped)"},
                    "addSubtask": {
                        "type": ["string", "object"],
                        "description": "Create a child task linked to this one. "
                                       "String = subject, or {subject, description}",
                    },
                    "addBlocks": {"type": "array", "items": {"type": "string"},
                                 "description": "Task IDs that this task blocks"},
                    "addBlockedBy": {"type": "array", "items": {"type": "string"},
                                    "description": "Task IDs that block this task"},
                },
                "required": ["id"],
            },
            invoke=_bi_task_update,
        ),
        Tool(
            name="task.list",
            description="List tasks, optionally filtered by status. "
                        "Use available=true to get tasks ready to work on (not blocked).",
            schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"],
                              "description": "Filter by status"},
                    "available": {"type": "boolean", "default": False,
                                 "description": "Show only unblocked pending tasks"},
                },
            },
            invoke=_bi_task_list,
        ),
        Tool(
            name="task.get",
            description="Get full details of a single task by ID.",
            schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task ID"},
                },
                "required": ["id"],
            },
            invoke=_bi_task_get,
        ),
        Tool(
            name="plan.read",
            description="Read the current plan (or a specific plan by name). "
                        "Returns the full plan document.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Plan name (omit for current)"},
                },
            },
            invoke=_bi_plan_read,
        ),
        Tool(
            name="plan.update",
            description="Create a new immutable revision of the current plan and refresh "
                        "its Markdown projection. Any prior approval is invalidated.",
            schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full plan document (markdown)"},
                },
                "required": ["content"],
            },
            invoke=_bi_plan_update,
        ),
        Tool(
            name="plan.list",
            description="List all saved plans.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_plan_list,
        ),
        Tool(
            name="plan.submit",
            description="Submit the current immutable plan revision for explicit "
                        "user review. Call only after exploration, risks, concrete "
                        "steps, and verification criteria are complete. This does "
                        "not approve or execute the plan.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_plan_submit,
        ),
        # ── Prompt optimization tools ──────────────────────────────
        Tool(
            name="prompt.feedback",
            description="Capture user feedback about prompt behavior and spawn a "
                        "background optimizer sub-agent to draft a patch. Use when "
                        "the user reports a recurring behavioral issue that a prompt "
                        "change could fix (e.g., 'you rarely reply', 'you don't verify').",
            schema={
                "type": "object",
                "properties": {
                    "description": {"type": "string",
                        "description": "What the user observed and wants improved"},
                },
                "required": ["description"],
            },
            invoke=_bi_prompt_feedback,
        ),
        Tool(
            name="prompt.draft",
            description="Write a prompt-optimization candidate to disk. Used by the "
                        "optimizer sub-agent after diagnosing a cli.prop deficiency. "
                        "The patch is an additive <prompt_opt_patch> block — do NOT "
                        "include the wrapper tags in the 'patch' parameter.",
            schema={
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "string",
                        "description": "The feedback id this patch addresses"},
                    "patch": {"type": "string",
                        "description": "Patch block contents; omit/empty for a documented model limitation"},
                    "rationale": {"type": "string",
                        "description": "1-3 sentences: what deficiency, how the patch fixes it"},
                },
                "required": ["feedback_id", "rationale"],
            },
            invoke=_bi_prompt_draft,
        ),
        Tool(
            name="prompt.review",
            description="Read a prompt-optimization candidate (or the active one) "
                        "to review its patch and rationale before applying.",
            schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Candidate id (omit for active)"},
                },
            },
            invoke=_bi_prompt_review,
        ),
        Tool(
            name="prompt.structured_feedback",
            description="Capture a structured failure report (v3 template) and "
                        "spawn a background optimizer sub-agent. The optimizer "
                        "triages whether the fix belongs in cli.prop or a skill "
                        "based on the failure category. Use when the user reports "
                        "a specific behavioral failure with a clear cause.",
            schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What the user asked for"},
                    "expected": {"type": "string", "description": "What should have happened"},
                    "actual": {"type": "string", "description": "What actually happened"},
                    "category": {"type": "string",
                        "enum": ["Objective unclear", "Missing tool-use rule",
                                 "Missing completion criteria", "Weak safety boundary",
                                 "Bad output format", "Too much ambiguity",
                                 "Model capability limitation",
                                 "Tool/environment limitation"],
                        "description": "Failure category (determines triage: cli.prop vs skill)"},
                    "minimal_fix": {"type": "string", "description": "Proposed minimal fix"},
                    "regression_tests": {"type": "string", "description": "Tests to rerun after fix"},
                },
                "required": ["task", "actual", "category"],
            },
            invoke=_bi_prompt_structured_feedback,
        ),
        Tool(
            name="prompt.skill_patch",
            description="Draft a skill-optimization patch. Used by the optimizer "
                        "sub-agent after diagnosing that a skill's SKILL.md or "
                        "skill.py is the root cause of a failure. Two modes: "
                        "'append' adds a <skill_opt_patch> block to the file "
                        "(best for SKILL.md instruction tweaks); 'replace' does "
                        "an exact string replacement (best for skill.py code fixes "
                        "or targeted SKILL.md edits). The user reviews and applies.",
            schema={
                "type": "object",
                "properties": {
                    "feedback_id": {"type": "string", "description": "The feedback id this patch addresses"},
                    "skill_name": {"type": "string", "description": "Skill directory name"},
                    "skill_file": {"type": "string", "default": "SKILL.md",
                        "description": "File to patch: 'SKILL.md' or 'skill.py'"},
                    "mode": {"type": "string", "enum": ["append", "replace"],
                        "default": "append",
                        "description": "append: add block to end; replace: string replacement"},
                    "patch": {"type": "string",
                        "description": "Content to append (append mode). Do NOT include <skill_opt_patch> wrapper."},
                    "old_string": {"type": "string",
                        "description": "String to find (replace mode). Must be unique in the file."},
                    "new_string": {"type": "string",
                        "description": "Replacement string (replace mode)"},
                    "rationale": {"type": "string",
                        "description": "1-3 sentences: which skill deficiency, how the patch fixes it"},
                },
                "required": ["feedback_id", "skill_name", "rationale"],
            },
            invoke=_bi_prompt_skill_patch,
        ),
        Tool(
            name="prompt.lab_draft",
            description="Draft a project-scoped Prompt Lab overlay and regression "
                        "cases after diagnosing a captured incident. This never "
                        "activates the patch; only the user can activate it.",
            schema={
                "type": "object",
                "properties": {
                    "branch_id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string",
                                "description": "Minimal additive prompt instructions; no wrapper tags or template variables"},
                    "rationale": {"type": "string"},
                    "diagnosis": {"type": "string",
                                  "description": "Root-cause hypotheses, including model/prompt/skill/policy distinctions"},
                    "tests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "input": {"type": "string"},
                                "expected": {"type": "string"},
                                "forbidden": {"type": "string"},
                            },
                            "required": ["name", "input", "expected"],
                        },
                    },
                },
                "required": ["branch_id", "title", "content", "rationale", "diagnosis", "tests"],
            },
            invoke=_bi_prompt_lab_draft,
        ),
        Tool(
            name="evolve.lab_draft",
            description="Draft a project-scoped feature/extension candidate in "
                        "Evolution Lab. This writes only a candidate and never "
                        "activates executable code.",
            schema={
                "type": "object",
                "properties": {
                    "branch_id": {"type": "string"},
                    "title": {"type": "string"},
                    "target_type": {"type": "string",
                                    "enum": ["extension", "commands", "loop"]},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "operation": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    "tests": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["branch_id", "title", "target_type", "name", "files"],
            },
            invoke=_bi_evolve_lab_draft,
        ),
        # ── Agent tools ─────────────────────────────────────────────
        Tool(
            name="agent.spawn",
            description="Spawn an in-process child agent to handle a sub-task. "
                        "The child runs in its own thread and posts results to your inbox. "
                        "Supports specialized roles (explorer, architect, reviewer, "
                        "silent-failure-hunter, simplifier, tester) and parallel spawning "
                        "via the 'tasks' parameter.",
            schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task for the child agent"},
                    "name": {"type": "string", "description": "Optional short name for the child"},
                    "role": {"type": "string",
                             "description": "Specialized role: explorer, architect, reviewer, "
                                            "silent-failure-hunter, simplifier, tester"},
                    "tasks": {
                        "type": "array",
                        "description": "Parallel spawn: list of {task, role, name} objects",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "role": {"type": "string"},
                                "name": {"type": "string"},
                            },
                            "required": ["task"],
                        },
                    },
                    "wait": {"type": "boolean", "default": False,
                             "description": "Block until all children complete (parallel mode)"},
                    "timeout": {"type": "number", "default": 120,
                                "description": "Seconds to wait for each child (with wait=true)"},
                },
                "required": [],
            },
            invoke=_bi_agent_spawn,
        ),
        Tool(
            name="agent.tell",
            description="Send a message to another agent's inbox.",
            schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Target agent ID"},
                    "message": {"type": "string", "description": "Message content (JSON or text)"},
                },
                "required": ["agent_id", "message"],
            },
            invoke=_bi_agent_tell,
        ),
        Tool(
            name="agent.station",
            description="Station yourself at a named terminal so your commands run there.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "default": "main", "description": "Terminal name"},
                },
            },
            invoke=_bi_agent_station,
        ),
        Tool(
            name="agent.abort",
            description="Abort another agent's execution.",
            schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Target agent ID"},
                },
                "required": ["agent_id"],
            },
            invoke=_bi_agent_abort,
        ),
        Tool(
            name="agent.wait",
            description="Wait for another agent to finish (blocking, max 300s).",
            schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Target agent ID"},
                    "timeout": {"type": "number", "default": 300, "description": "Max seconds to wait"},
                },
                "required": ["agent_id"],
            },
            invoke=_bi_agent_wait,
        ),
        Tool(
            name="agent.hire",
            description="Register a new agent slot. Returns the new agent's ID.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_agent_hire,
        ),
        Tool(
            name="agent.list",
            description="List all agents and their statuses.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_agent_list,
        ),
        Tool(
            name="agent.rename",
            description="Rename the current agent.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "New name"},
                },
                "required": ["name"],
            },
            invoke=_bi_agent_rename,
        ),
        Tool(
            name="agent.switch",
            description="Switch to a different agent identity.",
            schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Target agent ID"},
                },
                "required": ["agent_id"],
            },
            invoke=_bi_agent_switch,
        ),
        # ── HWO spawn primitives ────────────────────────────────────
        Tool(
            name="spawn",
            description=(
                "Spawn a sub-agent for a delegated task and WAIT for it to complete (blocking). "
                "If the concurrency cap is reached the sub-agent queues and starts when a slot frees. "
                "Give COMPLETE instructions in goal: file paths, conventions, constraints. "
                "10-loop limit per sub-agent."
            ),
            schema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Natural-language task. Give the sub-agent everything it needs."},
                    "context": {"type": "string", "description": "Extra context injected into the sub-agent's system prompt."},
                },
                "required": ["goal"],
            },
            invoke=_bi_spawn,
        ),
        Tool(
            name="spawn_parallel",
            description=(
                "Spawn multiple sub-agents in PARALLEL and wait for ALL to finish. "
                "Returns a combined structured report. Max 6 agents per batch. "
                "Each member must work on DIFFERENT files — decompose by file boundaries. "
                "Agents beyond the concurrency cap queue automatically."
            ),
            schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "Tasks to run in parallel. Each agent gets one task. Max 6.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "goal": {"type": "string", "description": "Complete task description for this agent."},
                                "hint": {"type": "string", "description": "Task-specific extra context."},
                            },
                            "required": ["goal"],
                        },
                        "minItems": 1,
                        "maxItems": 6,
                    },
                },
                "required": ["tasks"],
            },
            invoke=_bi_spawn_parallel,
        ),
        Tool(
            name="spawn_chain",
            description=(
                "Run a SEQUENTIAL pipeline of sub-agents (2-6 steps) with automatic handoff. "
                "Each step receives a handoff document from the previous step. "
                "Use for dependent work: analyze → implement → verify. "
                "If a step fails the chain aborts. Returns the final step output + per-step summary."
            ),
            schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Pipeline steps, executed in order. 2-6 steps.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "goal": {"type": "string", "description": "Complete task description for this step."},
                                "hint": {"type": "string", "description": "Step-specific extra context."},
                            },
                            "required": ["goal"],
                        },
                        "minItems": 2,
                        "maxItems": 6,
                    },
                },
                "required": ["steps"],
            },
            invoke=_bi_spawn_chain,
        ),
        Tool(
            name="await_spawns",
            description=(
                "Wait for spawned sub-agents to finish and collect their results. "
                "If agent_ids is omitted, waits for ALL children of the current agent."
            ),
            schema={
                "type": "object",
                "properties": {
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific agent IDs to wait for. Omit to await all children.",
                    },
                },
            },
            invoke=_bi_await_spawns,
        ),
        Tool(
            name="hwo",
            description=HWO_TOOL_DESCRIPTION,
            schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["run", "compile"],
                        "default": "run",
                        "description": "Whether to execute or only parse/validate the workflow.",
                    },
                    "path": {"type": "string", "description": "Path to a .hwo workflow file."},
                },
                "required": ["path"],
            },
            invoke=_bi_hwo,
        ),
        Tool(
            name="agent_send",
            description=(
                "Send a message to a sibling or parent agent within the current HWO workflow, "
                "addressed by name (see your [TEAM MANIFEST]). Cannot reach descendants."
            ),
            schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient agent name (sibling or parent)."},
                    "message": {"type": "string", "description": "Message text to send."},
                },
                "required": ["to", "message"],
            },
            invoke=_bi_hwo_agent_send,
        ),
        Tool(
            name="agent_receive",
            description=(
                "Block until a message arrives from a specific HWO teammate (or anyone, if "
                "'from' is omitted). Default timeout 60s, max 300s."
            ),
            schema={
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "Only accept a message from this agent name. Omit to accept from anyone."},
                    "timeout": {"type": "number", "default": 60, "description": "Seconds to wait before giving up."},
                },
            },
            invoke=_bi_hwo_agent_receive,
        ),
        Tool(
            name="agent_return",
            description=(
                "Record an explicit return value for this HWO step, overriding its natural "
                "reply. Use this to report a result to your parent without leaking it into "
                "[WORKFLOW CONTEXT] for later siblings — e.g. a private vote or decision that "
                "only the parent should see. After calling this, give a brief final reply and "
                "make no more tool calls."
            ),
            schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "The value to report back to the parent."},
                },
                "required": ["value"],
            },
            invoke=_bi_hwo_agent_return,
        ),
        # ── Terminal tools ──────────────────────────────────────────
        Tool(
            name="terminal.send",
            description="Send a command/keystrokes to a named sub-terminal.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name"},
                    "command": {"type": "string", "description": "Command or keystrokes to send"},
                },
                "required": ["name", "command"],
            },
            invoke=_bi_terminal_send,
        ),
        Tool(
            name="terminal.terminate",
            description="Terminate and destroy a named sub-terminal.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name"},
                },
                "required": ["name"],
            },
            invoke=_bi_terminal_terminate,
        ),
        Tool(
            name="terminal.create",
            description="Create a new named sub-terminal running a laintas-cli instance.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name"},
                },
                "required": ["name"],
            },
            invoke=_bi_terminal_create,
        ),
        Tool(
            name="terminal.list",
            description="List all named sub-terminals and their statuses.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_terminal_list,
        ),
        Tool(
            name="terminal.exec",
            description=(
                "Run an arbitrary shell command in a background sub-terminal. "
                "Optionally set a trigger regex: any new output line matching the "
                "pattern will push a watch.trigger event to the agent's inbox."
            ),
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique terminal name"},
                    "command": {"type": "string", "description": "Shell command to run"},
                    "trigger": {
                        "type": "string",
                        "description": "Optional regex — matching output lines fire inbox events",
                    },
                },
                "required": ["name", "command"],
            },
            invoke=_bi_terminal_exec,
        ),
        Tool(
            name="terminal.watch",
            description=(
                "Set or clear a trigger on an existing sub-terminal. "
                "When pattern is non-empty, matching output lines push watch.trigger "
                "events to the agent's inbox. Empty pattern clears the trigger."
            ),
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name"},
                    "pattern": {"type": "string", "description": "Regex to match (empty = clear)"},
                },
                "required": ["name", "pattern"],
            },
            invoke=_bi_terminal_watch,
        ),
        # ── Session tools ───────────────────────────────────────────
        Tool(
            name="session.close",
            description="Close the current one-off interactive PTY session and capture its output.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_session_close,
        ),
        Tool(
            name="session.keys",
            description="Send keystrokes to the current interactive PTY session.",
            schema={
                "type": "object",
                "properties": {
                    "keys": {"type": "string", "description": "Keystroke sequence to send"},
                },
                "required": ["keys"],
            },
            invoke=_bi_session_keys,
        ),
        # ── Utility tools ───────────────────────────────────────────
        Tool(
            name="sleep",
            description="Sleep for N seconds (e.g., after starting a dev server). Cap: 30s.",
            schema={
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "default": 1, "description": "Seconds to sleep"},
                },
                "required": ["seconds"],
            },
            invoke=_bi_sleep,
        ),
        Tool(
            name="task.continue",
            description=(
                "Keep working when you have nothing concrete to run THIS turn but "
                "the task is not finished (still reasoning or planning the next "
                "step). Call this instead of returning an empty tool_calls list — "
                "an empty list ends the turn and hands control back to the user. "
                "No arguments, no side effects."
            ),
            schema={"type": "object", "properties": {}},
            invoke=_bi_task_continue,
        ),
        Tool(
            name="session.continue",
            description=(
                "Signal that the user is resuming prior work in the current live "
                "session (e.g. they said \"继续\", \"continue\", \"接着\"). Call this "
                "when you determine from <active_tasks> or <objective> that the user "
                "wants to pick up an unfinished task, NOT start a new one. After "
                "calling, proceed with the actual task steps in subsequent turns. "
                "Optional 'reason' explains why you are continuing."
            ),
            schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you are continuing (optional)."},
                },
            },
            invoke=_bi_session_continue,
        ),
        Tool(
            name="task.complete",
            description=(
                "Affirmatively signal the user's task is fully finished. Call "
                "this — and ONLY this — when there is no remaining work and no "
                "further tool call is needed. In autonomous/execute mode this is "
                "the only way to end normally: simply not calling a tool does NOT "
                "mean done. Put your final result summary in `summary`."
            ),
            schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Final result summary for the user."},
                },
            },
            invoke=_bi_task_complete,
        ),
        # ── Browser live-view debug tools (P1) ───────────────────────
        Tool(
            name="browser._debug_open",
            description=(
                "[DEBUG/P1] Open a headless-browser live-view session. "
                "Spawns Xvfb+Chrome+x11vnc on the host and bridges to the "
                "backend /vnc relay. Returns CDP endpoint, VNC port, and "
                "display number. The WS relay shows 'retrying' until the "
                "backend deploys /vnc — that's expected; Chrome+CDP+VNC "
                "are up regardless."
            ),
            schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": "about:blank",
                            "description": "initial URL to navigate to"},
                    "name": {"type": "string", "description": "session name (auto-named if omitted)"},
                    "width": {"type": "integer", "default": 1280},
                    "height": {"type": "integer", "default": 800},
                },
                "required": ["url"],
            },
            invoke=_bi_browser_debug_open,
        ),
        Tool(
            name="browser._debug_close",
            description="[DEBUG/P1] Close a headless-browser session by name.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "session name to close"},
                },
                "required": ["name"],
            },
            invoke=_bi_browser_debug_close,
        ),
        Tool(
            name="browser._debug_list",
            description="[DEBUG/P1] List active browser sessions and their status.",
            schema={"type": "object", "properties": {}},
            invoke=_bi_browser_debug_list,
        ),
        # ── Browser automation tools (P2) ───────────────────────────
        Tool(
            name="browser.open",
            description=(
                "Open a new headless-browser session with live-view relay. "
                "Spawns Xvfb+Chrome+x11vnc and bridges to the backend /vnc. "
                "Returns session name, CDP endpoint, and VNC port. "
                "Use browser.navigate/click/type/etc. to interact afterwards."
            ),
            schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": "about:blank",
                            "description": "initial URL"},
                    "name": {"type": "string", "description": "session name (auto-named if omitted)"},
                    "width": {"type": "integer", "default": 1280},
                    "height": {"type": "integer", "default": 800},
                },
                "required": ["url"],
            },
            invoke=_bi_browser_open,
        ),
        Tool(
            name="browser.close",
            description="Close a browser session and tear down its Chrome/Xvfb/x11vnc stack.",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "session name (closes most recent if omitted)"},
                    "session": {"type": "string", "description": "alias for 'name'"},
                },
            },
            invoke=_bi_browser_close,
        ),
        Tool(
            name="browser.navigate",
            description="Navigate the browser to a URL. Returns the page title.",
            schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"],
                                    "default": "domcontentloaded"},
                    "timeout": {"type": "integer", "default": 30, "description": "navigation timeout in seconds"},
                },
                "required": ["url"],
            },
            invoke=_bi_browser_navigate,
        ),
        Tool(
            name="browser.click",
            description="Click an element identified by CSS selector or by ref from browser.snapshot.",
            schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the element to click"},
                    "ref": {"type": ["integer", "string"], "description": "interactive element ref number from browser.snapshot"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "timeout": {"type": "integer", "default": 10, "description": "wait timeout in seconds"},
                },
            },
            invoke=_bi_browser_click,
        ),
        Tool(
            name="browser.type",
            description="Type text into an input element identified by CSS selector or by ref from browser.snapshot. "
                        "Optionally clears the field first (default: yes).",
            schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input element"},
                    "ref": {"type": ["integer", "string"], "description": "interactive element ref number from browser.snapshot"},
                    "text": {"type": "string", "description": "text to type"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "delay": {"type": "integer", "default": 0, "description": "delay between keystrokes (ms)"},
                    "clear": {"type": "boolean", "default": True, "description": "clear the field before typing"},
                    "timeout": {"type": "integer", "default": 10, "description": "wait timeout in seconds"},
                },
                "required": ["text"],
            },
            invoke=_bi_browser_type,
        ),
        Tool(
            name="browser.screenshot",
            description="Take a screenshot and save to a file. Returns the file path. "
                        "The AI cannot see the image — use browser.snapshot for text content.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "full_page": {"type": "boolean", "default": False, "description": "capture full scrollable page"},
                    "path": {"type": "string", "description": "output file path (auto-generated if omitted)"},
                },
            },
            invoke=_bi_browser_screenshot,
        ),
        Tool(
            name="browser.query",
            description="Query DOM elements by CSS selector. Returns tag, text, and key "
                        "attributes (href, src, value, placeholder, etc.) for each match. "
                        "Use this to understand page structure before clicking/typing.",
            schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "limit": {"type": "integer", "default": 20, "description": "max elements to return"},
                    "attribute": {"type": "string", "description": "return only this attribute (omit for all)"},
                },
                "required": ["selector"],
            },
            invoke=_bi_browser_query,
        ),
        Tool(
            name="browser.snapshot",
            description="Return a text snapshot of the page: URL, title, visible body text, and "
                        "numbered refs for interactive elements. Use refs with browser.click/type/select.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "max_chars": {"type": "integer", "default": 5000, "description": "max text length"},
                },
            },
            invoke=_bi_browser_snapshot,
        ),
        Tool(
            name="browser.scroll",
            description="Scroll the page by (x, y) pixels or scroll an element into view by selector.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "x": {"type": "integer", "default": 0, "description": "horizontal scroll delta"},
                    "y": {"type": "integer", "default": 0, "description": "vertical scroll delta"},
                    "selector": {"type": "string", "description": "if set, scroll this element into view instead"},
                },
            },
            invoke=_bi_browser_scroll,
        ),
        Tool(
            name="browser.evaluate",
            description="Evaluate a JavaScript expression on the page and return the result. "
                        "Use for custom DOM queries or actions not covered by other browser.* tools.",
            schema={
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "JavaScript to evaluate"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
                "required": ["script"],
            },
            invoke=_bi_browser_evaluate,
        ),
        Tool(
            name="browser.press_key",
            description="Press a keyboard key (e.g., Enter, Tab, Escape, ArrowDown, Control+c).",
            schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "key name (e.g., Enter, Tab, Escape)"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
                "required": ["key"],
            },
            invoke=_bi_browser_press_key,
        ),
        Tool(
            name="browser.get_url",
            description="Get the current page URL.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
            },
            invoke=_bi_browser_get_url,
        ),
        Tool(
            name="browser.get_title",
            description="Get the current page title.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
            },
            invoke=_bi_browser_get_title,
        ),
        Tool(
            name="browser.wait_for",
            description="Wait for an element to reach a state (visible/hidden/attached/detached). "
                        "Useful for waiting for dynamic content to load.",
            schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                    "state": {"type": "string", "enum": ["visible", "hidden", "attached", "detached"],
                              "default": "visible"},
                    "timeout": {"type": "integer", "default": 15, "description": "wait timeout in seconds"},
                },
                "required": ["selector"],
            },
            invoke=_bi_browser_wait_for,
        ),
        Tool(
            name="browser.select",
            description="Select an option in a <select> dropdown by value or label, using CSS selector or ref.",
            schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the <select> element"},
                    "ref": {"type": ["integer", "string"], "description": "interactive element ref number from browser.snapshot"},
                    "value": {"type": "string", "description": "option value to select"},
                    "label": {"type": "string", "description": "option label to select (use if no value)"},
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
            },
            invoke=_bi_browser_select,
        ),
        Tool(
            name="browser.go_back",
            description="Navigate back in browser history.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
            },
            invoke=_bi_browser_go_back,
        ),
        Tool(
            name="browser.go_forward",
            description="Navigate forward in browser history.",
            schema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "session name (uses most recent if omitted)"},
                },
            },
            invoke=_bi_browser_go_forward,
        ),
        Tool(
            name="browser.get_errors",
            description="Runtime-error digest for testing: uncaught JS exceptions + console.error "
                        "messages + failed/4xx-5xx network requests captured since the page loaded. "
                        "Returns clean=true when none. Call after navigating/interacting to verify "
                        "the page didn't break.",
            schema={"type": "object", "properties": {
                "session": {"type": "string", "description": "session name (most recent if omitted)"},
            }},
            invoke=_bi_browser_get_errors,
        ),
        Tool(
            name="browser.get_console",
            description="Console messages captured from the page. Optional level filter "
                        "(error/warning/log/info/debug); omit or 'all' for everything.",
            schema={"type": "object", "properties": {
                "session": {"type": "string", "description": "session name (most recent if omitted)"},
                "level": {"type": "string", "enum": ["all", "error", "warning", "log", "info", "debug"], "default": "all"},
            }},
            invoke=_bi_browser_get_console,
        ),
        Tool(
            name="browser.expect",
            description="Assert a condition about the page for testing. Returns pass=true/false. "
                        "Use selector + one of: text (element text contains), state (visible|hidden), "
                        "count (number of matches); or page-level url_contains / title_contains. "
                        "With selector and no condition, asserts the element exists.",
            schema={"type": "object", "properties": {
                "session": {"type": "string", "description": "session name (most recent if omitted)"},
                "selector": {"type": "string", "description": "CSS selector to assert about"},
                "ref": {"type": "integer", "description": "element ref number (alternative to selector)"},
                "text": {"type": "string", "description": "assert the element's text contains this"},
                "state": {"type": "string", "enum": ["visible", "hidden"], "description": "assert visibility"},
                "count": {"type": "integer", "description": "assert this many elements match"},
                "url_contains": {"type": "string", "description": "assert the page URL contains this"},
                "title_contains": {"type": "string", "description": "assert the page title contains this"},
            }},
            invoke=_bi_browser_expect,
        ),
        Tool(
            name="browser.test_flow",
            description="Run a multi-step website test and get a pass/fail report. "
                        "Give 'steps': an ordered array of {action, ...params} where action is one of "
                        "navigate|click|type|select|press_key|scroll|wait_for|evaluate|expect "
                        "(same params as the matching browser.* tool; use 'expect' steps for assertions). "
                        "Stops at the first failed step, auto-captures a screenshot, and (unless "
                        "check_errors=false) fails the flow on captured runtime errors. Returns "
                        "pass=true/false, failed_at, per-step results, and the screenshot path.",
            schema={"type": "object", "properties": {
                "session": {"type": "string", "description": "session name (most recent if omitted); applied to every step"},
                "steps": {"type": "array", "description": "ordered test steps",
                          "items": {"type": "object", "properties": {
                              "action": {"type": "string",
                                         "enum": ["navigate", "click", "type", "select", "press_key",
                                                  "scroll", "wait_for", "evaluate", "expect"]},
                          }, "required": ["action"]}},
                "check_errors": {"type": "boolean", "default": True,
                                 "description": "after steps pass, fail the flow if runtime errors were captured"},
                "screenshot_on_failure": {"type": "boolean", "default": True},
                "clear_captures": {"type": "boolean", "default": True,
                                   "description": "clear captured console/errors before running (default true)"},
            }, "required": ["steps"]},
            invoke=_bi_browser_test_flow,
        ),
    ]
    for t in builtins:
        _registry.register(t)


# Auto-register at import — REPL bootstrap and skill loader rely on this.
register_builtin_tools()
