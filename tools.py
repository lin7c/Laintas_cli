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
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


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
    content = deps.read_file(".helpwo") or ""
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
                                  metadata=params.get("metadata"))
    return {"ok": True, "result": task}


def _bi_task_update(params: dict, ctx: ToolCtx) -> dict:
    if _task_mgr is None:
        return {"ok": False, "error": "task_manager module not available"}
    task_id = params.get("id", "")
    if not task_id:
        return {"ok": False, "error": "missing 'id'"}
    kwargs = {}
    for k in ("status", "subject", "description", "metadata",
              "addBlocks", "addBlockedBy", "removeBlocks", "removeBlockedBy"):
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


def _bi_shell_exec(params: dict, ctx: ToolCtx) -> dict:
    """Execute a shell command via subprocess and return stdout/stderr/exit.

    For one-shot non-interactive commands that don't need a PTY (grep, jq,
    python -c, etc.). For interactive or long-running work prefer a normal
    shell command in `command` so the loop's PTY/streaming path handles it.

    params:
      command:  shell command string (required)
      cwd:      working directory (default = ctx.cwd or process cwd)
      timeout:  seconds (default 30, max 300)
      stdin:    optional stdin payload
    """
    import subprocess as _sp
    cmd = params.get("command")
    if not cmd:
        return {"ok": False, "error": "missing 'command'"}
    cwd = params.get("cwd") or ctx.cwd or os.getcwd()
    timeout = max(1, min(int(params.get("timeout", 30) or 30), 300))
    stdin = params.get("stdin")

    # Local policy check — same engine used elsewhere, but we degrade
    # gracefully if the module isn't importable in this context.
    try:
        import policy as _policy
        decision = _policy.evaluate(cmd, cwd, agent_id=ctx.agent_id)
        if decision.action == "deny":
            return {"ok": False, "error": f"Blocked by policy: {decision.reason}",
                    "policy": "deny"}
    except Exception:
        pass

    try:
        t0 = time.time()
        proc = _sp.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, input=stdin,
        )
        duration_ms = int((time.time() - t0) * 1000)
    except _sp.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s", "timeout": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Cap each stream to keep loop context bounded.
    cap = 8192
    so = proc.stdout or ""
    se = proc.stderr or ""
    so_trim = (so[:cap] + f"\n...(truncated {len(so) - cap} bytes)") if len(so) > cap else so
    se_trim = (se[:cap] + f"\n...(truncated {len(se) - cap} bytes)") if len(se) > cap else se

    return {
        "ok": proc.returncode == 0,
        "result": so_trim,
        "stderr": se_trim,
        "exit_code": proc.returncode,
        "duration_ms": duration_ms,
        "cwd": cwd,
    }


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
    """Search the web using DuckDuckGo HTML (no API key needed).

    Returns list of {title, url, snippet} results.
    """
    import urllib.request
    import urllib.parse
    import urllib.error

    query = params.get("query", "").strip()
    if not query:
        return {"ok": False, "error": "missing 'query'"}

    max_results = min(max(int(params.get("max_results", 10)), 1), 20)

    try:
        # DuckDuckGo HTML search (non-JS, no API key)
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=_WEB_FETCH_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Search request failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Parse DuckDuckGo HTML results
    results = []
    # Each result is in a div with class "result"
    # Title is in <a class="result__a">
    # Snippet is in <a class="result__snippet">
    # URL is in <a class="result__url">

    # Simple regex-based extraction
    import re as _re_html
    # Split on result boundaries
    blocks = _re_html.split(r'<div[^>]*class="[^"]*result[^"]*"[^>]*>', html)

    for block in blocks:
        if len(results) >= max_results:
            break

        # Extract title + link
        title_m = _re_html.search(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            block, _re_html.DOTALL)
        if not title_m:
            continue

        href = title_m.group(1)
        title = _re_html.sub(r'<[^>]+>', '', title_m.group(2)).strip()
        title = _re_html.sub(r'&[a-z]+;', lambda m: {
            '&amp;': '&', '&lt;': '<', '&gt;': '>',
            '&quot;': '"', '&#x27;': "'", '&apos;': "'",
        }.get(m.group(0), m.group(0)), title)

        # Extract snippet
        snippet_m = _re_html.search(
            r'<[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
            block, _re_html.DOTALL)
        snippet = ""
        if snippet_m:
            snippet = _re_html.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()
            snippet = _re_html.sub(r'&[a-z]+;', lambda m: {
                '&amp;': '&', '&lt;': '<', '&gt;': '>',
                '&quot;': '"', '&#x27;': "'", '&apos;': "'",
            }.get(m.group(0), m.group(0)), snippet)

        results.append({
            "title": title,
            "url": href,
            "snippet": snippet[:500],
        })

    return {"ok": True, "result": results, "query": query, "count": len(results)}


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


def register_builtin_tools() -> None:
    """Idempotent — safe to call multiple times."""
    builtins = [
        Tool(
            name="mem.read",
            description="Read the agent's persistent .helpwo memory file in full.",
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
                        "dependencies (blocks/blockedBy), and metadata.",
            schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Brief, actionable title"},
                    "description": {"type": "string", "default": "",
                                    "description": "Detailed description of what needs to be done"},
                    "metadata": {"type": "object", "default": {},
                                 "description": "Arbitrary metadata (tags, priority, etc.)"},
                },
                "required": ["subject"],
            },
            invoke=_bi_task_create,
        ),
        Tool(
            name="task.update",
            description="Update a task's status, description, dependencies, or metadata.",
            schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Task ID to update"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"],
                              "description": "New status"},
                    "subject": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description"},
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
    ]
    for t in builtins:
        _registry.register(t)


# Auto-register at import — REPL bootstrap and skill loader rely on this.
register_builtin_tools()
