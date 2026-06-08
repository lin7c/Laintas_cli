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
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import paths


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


# ── Registry ───────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, overwrite: bool = True) -> bool:
        if not overwrite and tool.name in self._tools:
            return False
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

        Returns shape: {ok: bool, result?: any, error?: str, tool: name}.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"tool '{name}' not found", "tool": name}
        try:
            out = tool.invoke(params or {}, ctx)
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

                # Build a minimal valid example invocation.
                example_params: dict = {}
                for pname in required:
                    pinfo = props.get(pname, {}) if isinstance(props.get(pname), dict) else {}
                    ptype = pinfo.get("type", "string")
                    if ptype == "integer" or ptype == "number":
                        example_params[pname] = 0
                    elif ptype == "boolean":
                        example_params[pname] = False
                    elif ptype == "array":
                        example_params[pname] = []
                    elif ptype == "object":
                        example_params[pname] = {}
                    else:
                        example_params[pname] = "<...>"
                try:
                    example_json = json.dumps(example_params, ensure_ascii=False)
                except (TypeError, ValueError):
                    example_json = "{}"

                desc = (t.description or "").strip().replace("\n", " ")
                if len(desc) > 240:
                    desc = desc[:237] + "..."

                lines.append(f"- {t.name} — {desc}")
                if req_parts:
                    lines.append(f"    required: {', '.join(req_parts)}")
                if opt_parts:
                    lines.append(f"    optional: {', '.join(opt_parts)}")
                lines.append(f"    usage: /tool {t.name} {example_json}")
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
            f"Emit JSON: {{\"reply\": \"...\", \"tool_calls\": [{{\"name\": \"...\", \"arguments\": {{...}}}}]}}"
        )


# Module-level singleton — every consumer hits this.
_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _registry


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
    if not name or not body:
        return {"ok": False, "error": "missing 'name' or 'body'"}
    ok, msg = _mem_sys.write_memory(name, mem_type, description, body)
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


def _bi_fs_read(params: dict, ctx: ToolCtx) -> dict:
    """Read a file as UTF-8 with optional line range and cat-style numbering.

    params:
      path:       file path (required)
      offset:     1-based starting line (default 1)
      limit:      max lines to return (default 2000)
      max_bytes:  hard byte cap on returned payload (default 200_000)
      line_numbers: prepend each line with "N→ " (default True)

    Behaviour mirrors Claude Code's Read tool: prefer offset/limit over
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


def _bi_fs_write(params: dict, ctx: ToolCtx) -> dict:
    path = params.get("path")
    content = params.get("content", "")
    if not path:
        return {"ok": False, "error": "missing 'path'"}
    abs_path = os.path.abspath(os.path.join(ctx.cwd or os.getcwd(), path)) \
        if not os.path.isabs(path) else path
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "result": f"wrote {len(content)} bytes", "path": abs_path}
    except OSError as e:
        return {"ok": False, "error": str(e)}


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
    task = _task_mgr.create_task(subject, description,
                                  metadata=params.get("metadata"),
                                  session_only=params.get("session_only", False),
                                  parent_task_id=params.get("parent_task_id"))
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
    ok, msg, task = _task_mgr.update_task(str(task_id), **kwargs)
    return {"ok": ok, "result": task if ok else None, "error": "" if ok else msg}


def _bi_task_list(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    status = params.get("status") or None
    available = params.get("available", False)
    if available:
        tasks = _task_mgr.get_available_tasks()
    else:
        tasks = _task_mgr.list_tasks(status=status)
    return {"ok": True, "result": tasks, "count": len(tasks)}


def _bi_task_get(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    task_id = params.get("id", "")
    if not task_id:
        return {"ok": False, "error": "missing 'id'"}
    task = _task_mgr.get_task(str(task_id))
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


def _bi_fs_edit(params: dict, ctx: ToolCtx) -> dict:
    """Exact string replacement in a file (like Claude Code's Edit tool).

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
    if count == 0:
        return {"ok": False, "error": "old_string not found in file",
                "hint": "Check exact whitespace and indentation"}

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

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "result": f"Replaced {count} occurrence(s) in {abs_path}",
        "path": abs_path,
        "replacements": count,
    }


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

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(working)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "result": f"Applied {len(applied)} edits to {abs_path}",
            "path": abs_path, "edits_applied": applied}


def _bi_fs_diff(params: dict, ctx: ToolCtx) -> dict:
    """Compute a unified diff between two files or between a file and a string.

    params:
      a:        path to file A (required)
      b:        path to file B (optional)
      b_text:   raw text to compare against A (alternative to b)
      context:  context lines (default 3)
      label_a:  display label for A (default = path)
      label_b:  display label for B (default = path or "<inline>")
    """
    import difflib

    a = params.get("a")
    if not a:
        return {"ok": False, "error": "missing 'a'"}
    b = params.get("b")
    b_text = params.get("b_text")
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

    diff = difflib.unified_diff(
        a_text.splitlines(keepends=True),
        b_text_resolved.splitlines(keepends=True),
        fromfile=label_a, tofile=label_b, n=n,
    )
    body = "".join(diff)
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
            for cid in child_ids:
                info = _al.wait_for_agent(cid, timeout=params.get("timeout", 120.0))
                if info:
                    results.append(f"[{cid}] {info.status}: {info.last_reply[:200] if info.last_reply else '(no reply)'}")
                else:
                    results.append(f"[{cid}] timeout or not found")
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
    target_agent = None
    if ctx.get_current_agent is not None:
        target_agent = ctx.get_current_agent()
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
        if sub.is_alive():
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
    info = ctx.wait_for_agent(target_id, timeout)
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
    current = ctx.get_current_agent() if ctx.get_current_agent else None
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
    if ctx.rename_agent is None or ctx.get_current_agent is None:
        return {"ok": False, "error": "rename not available"}
    current = ctx.get_current_agent()
    if current and ctx.rename_agent(current.id, new_name):
        return {"ok": True, "result": f"Renamed to {new_name}"}
    return {"ok": False, "error": "no current agent to rename"}


def _bi_agent_switch(params: dict, ctx: ToolCtx) -> dict:
    """Switch to a different agent identity."""
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
    if sub.is_alive():
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
                        "explain what changed between two file revisions.",
            schema={
                "type": "object",
                "properties": {
                    "a": {"type": "string", "description": "path to file A"},
                    "b": {"type": "string", "description": "path to file B (alt to b_text)"},
                    "b_text": {"type": "string",
                                "description": "inline text to compare against A"},
                    "context": {"type": "integer", "default": 3},
                    "label_a": {"type": "string"},
                    "label_b": {"type": "string"},
                },
                "required": ["a"],
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
            description="Create a new task for structured work tracking. "
                        "Tasks have status (pending→in_progress→completed), "
                        "dependencies (blocks/blockedBy), progress (0-100), "
                        "notes, and metadata. Use session_only=true for ephemeral "
                        "tasks that won't persist across sessions.",
            schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Brief, actionable title"},
                    "description": {"type": "string", "default": "",
                                    "description": "Detailed description of what needs to be done"},
                    "metadata": {"type": "object", "default": {},
                                 "description": "Arbitrary metadata (tags, priority, etc.)"},
                    "session_only": {"type": "boolean", "default": False,
                                     "description": "If true, task exists only in this session"},
                    "parent_task_id": {"type": "string",
                                       "description": "Auto-link as blockedBy this parent task"},
                },
                "required": ["subject"],
            },
            invoke=_bi_task_create,
        ),
        Tool(
            name="task.update",
            description="Update a task's status, description, dependencies, progress, "
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
            description="Update the current plan's content. Use during plan mode to "
                        "document your findings, architecture decisions, and implementation steps.",
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
    ]
    for t in builtins:
        _registry.register(t)


# Auto-register at import — REPL bootstrap and skill loader rely on this.
register_builtin_tools()
