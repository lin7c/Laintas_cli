#!/usr/bin/env python3
"""AI Agent Loop for laintas_cli — extracted from laintas_cli.py."""

import os
import re
import json
import queue
import shlex
import subprocess
import sys
import threading
import time
import uuid
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

import tools as tools_mod   # ToolRegistry singleton + ToolCtx
import policy as policy_mod  # Security policy engine
import memory_system         # Cross-session persistent memory
import hooks as hooks_mod    # Extensible hook system
import plan_mode             # Structured planning before execution

# Path to laintas_cli.py for spawning child terminals
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LAINTAS_CLI = os.path.join(_SCRIPT_DIR, "laintas_cli.py")


# ── Constants ──────────────────────────────────────────────────────────
MAX_LOOPS = 10
MAX_TOKENS = 2000
MAX_DEBUG_ENTRIES = 50

# Mutable defaults — these are the "factory" values; runtime overrides stored in _runtime_config
_DEFAULT_CONFIG = {
    "max_loops": 30,
    "max_tokens": 2000,
    "max_debug_entries": 50,
    "loop_delay": 1.5,           # seconds between loop iterations
    "output_truncate": 3000,      # chars — lastOutput tail truncation
    "poll_timeout": 10.0,         # seconds — wait for first command output
    "terminal_tail_lines": 20,    # lines — sub-terminal snapshot
    "heartbeat_interval": 30,     # seconds — agent heartbeat
    "staleness_limit": 3,         # consecutive no-command steps before auto-exit
}

_runtime_config: dict[str, object] = {}


def get_runtime_config(key: str):
    """Read a runtime config value, falling back to default."""
    if key in _runtime_config:
        return _runtime_config[key]
    return _DEFAULT_CONFIG.get(key)


def set_runtime_config(key: str, value) -> bool:
    """Set a runtime config value. Returns True if key is valid."""
    if key not in _DEFAULT_CONFIG:
        return False
    _runtime_config[key] = value
    return True


def list_runtime_config() -> dict:
    """Return {key: current_value, ...} for all config keys."""
    return {k: get_runtime_config(k) for k in _DEFAULT_CONFIG}


def reset_runtime_config():
    """Clear all runtime overrides."""
    _runtime_config.clear()


# ── Debug System ───────────────────────────────────────────────────────

@dataclass
class DebugEntry:
    """Single agent interaction record, mirrors Helpwo's DebugLogEntry."""
    timestamp: str = ""
    loop: int = 0
    user_input: str = ""
    current_path: str = ""
    context_sizes: dict = field(default_factory=dict)  # {global, local, prompt}
    request_body: dict = field(default_factory=dict)
    response_raw: dict = field(default_factory=dict)
    reply: str = ""
    command: str = ""
    memory: str = ""
    done: bool = False
    exec_command: str = ""
    exec_stdout: str = ""
    exec_stderr: str = ""
    exec_returncode: int = 0
    session_command: str = ""    # command when interactive session active
    error: bool = False
    billing: dict = field(default_factory=dict)


@dataclass
class TerminalInfo:
    """Metadata about a persistent named sub-terminal."""
    name: str
    command: str
    session: Any  # SubTerminalSession
    created_at: float
    created_by: str  # "depth=0"
    stationed_agent_id: Optional[str] = None  # deprecated, use stationed_agent_ids
    stationed_agent_ids: list = field(default_factory=list)


_debug_logs: list[DebugEntry] = []
_debug_loop_counter: int = 0

# Terminal registry — persistent named sub-terminals
_terminal_registry: dict[str, TerminalInfo] = {}
_terminal_counter: int = 0


def add_debug_log(entry: DebugEntry) -> None:
    """Prepend entry to debug log, cap at configured max."""
    global _debug_logs
    _debug_logs.insert(0, entry)
    max_entries = int(get_runtime_config("max_debug_entries"))
    if len(_debug_logs) > max_entries:
        del _debug_logs[max_entries:]


def clear_debug_logs() -> None:
    """Clear all debug entries and reset counter."""
    global _debug_logs, _debug_loop_counter
    _debug_logs = []
    _debug_loop_counter = 0


def next_debug_loop() -> int:
    """Increment and return the debug loop counter."""
    global _debug_loop_counter
    _debug_loop_counter += 1
    return _debug_loop_counter


def get_debug_logs() -> list:
    """Return the current debug logs list."""
    return _debug_logs


# ── Terminal Registry ──────────────────────────────────────────────────

def register_terminal(session, command: str, depth: int, name: str = None) -> str:
    """Register a persistent sub-terminal. Auto-generates name as 'term<N>' if none given.
    If the name already exists, closes the old terminal and replaces it.
    Returns the assigned name.
    """
    global _terminal_registry, _terminal_counter
    _terminal_counter += 1
    if name is None:
        name = f"term{_terminal_counter}"
    if name in _terminal_registry:
        try:
            _terminal_registry[name].session.close()
        except Exception:
            pass
    info = TerminalInfo(
        name=name,
        command=command,
        session=session,
        created_at=time.time(),
        created_by=f"depth={depth}",
    )
    _terminal_registry[name] = info
    return name


def unregister_terminal(name: str) -> bool:
    """Close and remove a terminal by name. Returns True if it existed."""
    info = _terminal_registry.pop(name, None)
    if info is None:
        return False
    try:
        info.session.close()
    except Exception:
        pass
    return True


def get_terminal(name: str) -> Optional[TerminalInfo]:
    """Get a terminal by name, or None."""
    return _terminal_registry.get(name)


def get_all_terminals() -> list:
    """Return all registered terminals sorted by creation time."""
    return sorted(_terminal_registry.values(), key=lambda t: t.created_at)


def close_all_terminals() -> None:
    """Close and remove ALL registered terminals (cascading cleanup)."""
    for info in list(_terminal_registry.values()):
        try:
            info.session.close()
        except Exception:
            pass
    _terminal_registry.clear()


def rename_terminal(old_name: str, new_name: str) -> bool:
    """Rename a terminal. Returns True on success, False if old_name not found."""
    info = _terminal_registry.pop(old_name, None)
    if info is None:
        return False
    if new_name in _terminal_registry:
        try:
            _terminal_registry[new_name].session.close()
        except Exception:
            pass
    info.name = new_name
    _terminal_registry[new_name] = info
    return True


# ── Agent Registry ──────────────────────────────────────────────────────

@dataclass
class AgentInfo:
    """Metadata about a logical AI agent managed by the REPL."""
    id: str
    name: str
    stationed_terminal: Optional[str] = None
    chat_history: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    created_at: float = 0.0
    # ── Phase 2: in-process sub-agent fields ──────────────────────────
    depth: int = 0
    parent_id: Optional[str] = None
    child_ids: list = field(default_factory=list)
    inbox: Any = field(default_factory=lambda: queue.Queue(maxsize=1000))
    thread: Optional[Any] = None              # threading.Thread, None for primary
    status: str = "idle"                      # idle / running / done / aborted / error
    last_reply: str = ""
    abort_event: Any = field(default_factory=threading.Event)

_agent_registry: dict[str, AgentInfo] = {}
_agent_counter: int = 0
_current_agent_id: Optional[str] = None
# Phase 2: protect registry + parent/child link mutations from concurrent
# spawn/abort/send across threads. RLock so a method that takes the lock
# may call another locked method without deadlocking.
_registry_lock = threading.RLock()


def register_agent(name: str = None, depth: int = 0,
                   parent_id: Optional[str] = None) -> AgentInfo:
    """Create and register a new AI agent. Returns the AgentInfo."""
    global _agent_registry, _agent_counter
    with _registry_lock:
        _agent_counter += 1
        agent_id = name if name else f"AI-{_agent_counter}"
        if agent_id in _agent_registry:
            unregister_agent(agent_id)
        info = AgentInfo(
            id=agent_id,
            name=agent_id,
            chat_history=[],
            state={"shortTermMemory": "", "lastReply": "", "lastOutput": ""},
            created_at=time.time(),
            depth=depth,
            parent_id=parent_id,
        )
        _agent_registry[agent_id] = info
        if parent_id and parent_id in _agent_registry:
            parent = _agent_registry[parent_id]
            if agent_id not in parent.child_ids:
                parent.child_ids.append(agent_id)
        return info


def unregister_agent(agent_id: str) -> bool:
    """Remove an agent. Returns True if it existed."""
    global _current_agent_id
    with _registry_lock:
        if _current_agent_id == agent_id:
            _current_agent_id = None
        info = _agent_registry.pop(agent_id, None)
        if info is None:
            return False
        # Unlink from parent's child_ids
        if info.parent_id and info.parent_id in _agent_registry:
            parent = _agent_registry[info.parent_id]
            if agent_id in parent.child_ids:
                parent.child_ids.remove(agent_id)
        return True


def get_agent(agent_id: str) -> Optional[AgentInfo]:
    with _registry_lock:
        return _agent_registry.get(agent_id)


def get_all_agents() -> list:
    with _registry_lock:
        return sorted(_agent_registry.values(), key=lambda a: a.created_at)


def get_current_agent() -> Optional[AgentInfo]:
    with _registry_lock:
        if _current_agent_id:
            return _agent_registry.get(_current_agent_id)
        return None


def switch_to_agent(agent_id: str) -> bool:
    """Switch the active agent. Returns True on success."""
    global _current_agent_id
    with _registry_lock:
        if agent_id not in _agent_registry:
            return False
        _current_agent_id = agent_id
        return True


def set_current_agent_id(agent_id: str) -> None:
    global _current_agent_id
    with _registry_lock:
        _current_agent_id = agent_id


def rename_agent(agent_id: str, new_name: str) -> bool:
    with _registry_lock:
        info = _agent_registry.get(agent_id)
        if info is None:
            return False
        info.name = new_name
        return True


def station_agent(agent_id: str, terminal_name: str) -> bool:
    """Station an agent in a terminal. Multiple agents can share one terminal."""
    with _registry_lock:
        agent = _agent_registry.get(agent_id)
        term = _terminal_registry.get(terminal_name)
        if agent is None:
            return False
        # Remove from old terminal's list
        if agent.stationed_terminal and agent.stationed_terminal in _terminal_registry:
            old_term = _terminal_registry[agent.stationed_terminal]
            if agent_id in old_term.stationed_agent_ids:
                old_term.stationed_agent_ids.remove(agent_id)
            old_term.stationed_agent_id = old_term.stationed_agent_ids[0] if old_term.stationed_agent_ids else None
        agent.stationed_terminal = terminal_name
        if term:
            if agent_id not in term.stationed_agent_ids:
                term.stationed_agent_ids.append(agent_id)
            term.stationed_agent_id = term.stationed_agent_ids[0]  # keep first as legacy
        return True


def unstation_agent(agent_id: str) -> None:
    with _registry_lock:
        agent = _agent_registry.get(agent_id)
        if agent and agent.stationed_terminal:
            term = _terminal_registry.get(agent.stationed_terminal)
            if term:
                if agent_id in term.stationed_agent_ids:
                    term.stationed_agent_ids.remove(agent_id)
                term.stationed_agent_id = term.stationed_agent_ids[0] if term.stationed_agent_ids else None
            agent.stationed_terminal = None


def close_all_agents() -> None:
    """Clean up all agent registrations. Signals abort to running children first."""
    with _registry_lock:
        for info in list(_agent_registry.values()):
            try:
                info.abort_event.set()
            except Exception:
                pass
        _agent_registry.clear()
        global _current_agent_id
        _current_agent_id = None


# ── Phase 2: in-process sub-agent control plane ────────────────────────

def send_to_agent(agent_id: str, message: dict) -> bool:
    """Drop a JSON-serializable dict into the target agent's inbox.

    Returns False if the agent doesn't exist or the inbox is full.
    Non-blocking by design — callers that need ack should use a reply id
    and poll their own inbox for the response.
    """
    info = get_agent(agent_id)
    if info is None:
        return False
    try:
        info.inbox.put_nowait(message)
        return True
    except queue.Full:
        return False


def recv_from_inbox(agent_id: str, timeout: float = 0.0) -> Optional[dict]:
    """Pop one message from the agent's inbox. timeout=0 → non-blocking."""
    info = get_agent(agent_id)
    if info is None:
        return None
    try:
        if timeout > 0:
            return info.inbox.get(timeout=timeout)
        return info.inbox.get_nowait()
    except queue.Empty:
        return None


def drain_inbox(agent_id: str) -> list:
    """Pop ALL pending messages atomically. Returns [] if none / no agent."""
    info = get_agent(agent_id)
    if info is None:
        return []
    msgs: list = []
    while True:
        try:
            msgs.append(info.inbox.get_nowait())
        except queue.Empty:
            break
    return msgs


def abort_agent(agent_id: str) -> bool:
    """Signal the target agent to stop at the next loop iteration boundary.

    Does not kill subprocesses started by the agent — those need a separate
    cleanup pass via the terminal registry.
    """
    info = get_agent(agent_id)
    if info is None:
        return False
    info.abort_event.set()
    info.status = "aborted"
    return True


def wait_for_agent(agent_id: str, timeout: float = 30.0) -> Optional[AgentInfo]:
    """Block until the target agent finishes (status in {done, aborted, error}).

    Returns the final AgentInfo, or None if timed out / agent missing.
    """
    info = get_agent(agent_id)
    if info is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if info.status in ("done", "aborted", "error"):
            return info
        time.sleep(0.1)
    return None


def spawn_subagent(parent_id: str, task: str, deps,
                   name: Optional[str] = None,
                   session: Optional[dict] = None,
                   events_cb=None) -> Optional[str]:
    """Start an in-process child agent in its own thread.

    The child:
      - inherits depth = parent.depth + 1
      - has its own chat_history, state, inbox, abort_event
      - reports back to the parent via a 'child-done' / 'child-error' message
        delivered to the parent's inbox

    Returns the child's agent_id, or None if the parent doesn't exist.
    """
    parent = get_agent(parent_id)
    if parent is None:
        return None
    child = register_agent(name=name, depth=parent.depth + 1,
                           parent_id=parent_id)
    child.status = "running"

    def _runner():
        try:
            result = run_agent_loop(
                deps, task, session or {}, child.state,
                child.chat_history,
                events_cb=events_cb,
                depth=child.depth,
                agent_id=child.id,
            )
            child.last_reply = (result.get("state") or {}).get("lastReply", "") if isinstance(result, dict) else ""
            child.status = "aborted" if child.abort_event.is_set() else "done"
            send_to_agent(parent_id, {
                "from": child.id,
                "kind": "child-done",
                "status": child.status,
                "summary": child.last_reply or "(no reply)",
            })
        except Exception as e:
            child.status = "error"
            send_to_agent(parent_id, {
                "from": child.id,
                "kind": "child-error",
                "error": repr(e),
            })

    t = threading.Thread(target=_runner, daemon=True,
                         name=f"laintas-agent-{child.id}")
    child.thread = t
    t.start()
    return child.id


def build_agents_tree() -> str:
    """Render the agent hierarchy as an ASCII tree, rooted at any agent
    that has no parent (or whose parent is missing)."""
    agents = get_all_agents()
    by_id = {a.id: a for a in agents}
    roots = [a for a in agents if not a.parent_id or a.parent_id not in by_id]

    lines: list = []
    def _walk(a: AgentInfo, prefix: str, is_last: bool):
        branch = "└─ " if is_last else "├─ "
        st = f" [{a.status}]" if a.status != "idle" else ""
        st += f" depth={a.depth}"
        if a.stationed_terminal:
            st += f" station={a.stationed_terminal}"
        if a.inbox.qsize() > 0:
            st += f" inbox={a.inbox.qsize()}"
        lines.append(f"{prefix}{branch}{a.id}{st}")
        children = [by_id[cid] for cid in a.child_ids if cid in by_id]
        for i, c in enumerate(children):
            extension = "    " if is_last else "│   "
            _walk(c, prefix + extension, i == len(children) - 1)

    for i, r in enumerate(roots):
        st = f" [{r.status}]" if r.status != "idle" else ""
        st += f" depth={r.depth}"
        if r.inbox.qsize() > 0:
            st += f" inbox={r.inbox.qsize()}"
        lines.append(f"{r.id}{st}")
        children = [by_id[cid] for cid in r.child_ids if cid in by_id]
        for j, c in enumerate(children):
            _walk(c, "", j == len(children) - 1)
    return "\n".join(lines) if lines else "(no agents)"


# ── Dependencies Container ─────────────────────────────────────────────

@dataclass
class LoopDeps:
    """External dependencies injected from laintas_cli."""
    read_file: Callable[[str], Optional[str]]
    append_file: Callable[[str, str], None]
    write_file: Callable[[str, str], None]
    strip_ansi: Callable[[str], str]
    generate_prompt: Callable[[], str]
    call_backend: Callable[..., dict]
    SubTerminalSession: type
    display_command_output: Callable[..., None]
    display_sub_terminal_preview: Callable[..., None]
    console: Any  # rich.console.Console
    Markdown: type  # rich.markdown.Markdown
    pty_passthrough: Optional[Callable[..., dict]] = None


# ── Structured Memory System (.helpwo) ──────────────────────────────────
# .helpwo stores a JSON array of entries: [{"id": N, "content": "...", "created": "...", "updated": "..."}]
# AI can append, modify (~N:), or delete (-N) entries via the "memory" field.

_MEMORY_FILE = ".helpwo"


def _read_memory(deps: LoopDeps) -> list[dict]:
    """Read and parse .helpwo as a JSON array of entries. Returns [] on failure."""
    raw = deps.read_file(_MEMORY_FILE)
    if not raw or not raw.strip():
        return []
    try:
        entries = json.loads(raw)
        if isinstance(entries, list):
            return entries
    except json.JSONDecodeError:
        pass
    # Legacy plain-text: wrap as single entry
    text = raw.strip()
    if text:
        return [{"id": 1, "content": text, "created": datetime.now().isoformat(), "updated": datetime.now().isoformat()}]
    return []


def _write_memory(deps: LoopDeps, entries: list[dict]) -> None:
    """Write entries to .helpwo as pretty-printed JSON."""
    deps.write_file(_MEMORY_FILE, json.dumps(entries, ensure_ascii=False, indent=2))


def _format_memory(entries: list[dict]) -> str:
    """Format entries for prompt display."""
    if not entries:
        return "(empty)"
    return "\n".join(f"  [{e['id']}] {e['content']}" for e in entries)


def _apply_memory(deps: LoopDeps, instruction: str) -> str:
    """Apply a CRUD instruction to .helpwo memory.

    Instruction formats:
      "text"        → append new entry (auto-assigned id)
      "~N: text"    → update (modify) entry with id N
      "-N"          → delete entry with id N
    Returns a description of what was done.
    """
    entries = _read_memory(deps)
    stripped = instruction.strip()
    if not stripped:
        return ""

    # -- Delete: "-N" --
    m = re.match(r'^-(\d+)$', stripped)
    if m:
        eid = int(m.group(1))
        before = len(entries)
        entries = [e for e in entries if e.get("id") != eid]
        if len(entries) < before:
            for i, e in enumerate(entries, 1):
                e["id"] = i
            _write_memory(deps, entries)
            return f"Deleted memory entry [{eid}]"
        return f"Entry [{eid}] not found"

    # -- Modify: "~N: new text" --
    m = re.match(r'^~(\d+)\s*:\s*(.+)$', stripped)
    if m:
        eid = int(m.group(1))
        new_content = m.group(2).strip()
        for e in entries:
            if e.get("id") == eid:
                e["content"] = new_content
                e["updated"] = datetime.now().isoformat()
                _write_memory(deps, entries)
                return f"Modified memory entry [{eid}]"
        return f"Entry [{eid}] not found"

    # -- Append: plain text --
    max_id = max((e.get("id", 0) for e in entries), default=0)
    entries.append({
        "id": max_id + 1,
        "content": stripped,
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
    })
    _write_memory(deps, entries)
    return f"Added memory entry [{max_id + 1}]"


# ── Context Builders (3 clean sections) ──────────────────────────────────

_MAX_TERMINAL_LINES = 100
_MAX_HISTORY_ENTRIES = 8       # compress when terminalHistory exceeds this
_COMPRESSION_KEEP_RECENT = 4   # always keep this many recent entries uncompressed
_MAX_RETRIES = 2               # automatic retries for transient failures
_CONSECUTIVE_FAILURE_LIMIT = 3  # warn AI after this many consecutive failures

# ── Error pattern recognition ──────────────────────────────────────────
# Maps regex patterns to (category, suggestion) tuples.
_ERROR_PATTERNS = [
    (r"(?:command not found|not recognized as an internal)", "missing_command",
     "Command not found. Check the command name or install the package."),
    (r"(?:Permission denied|Operation not permitted|EACCES)", "permission",
     "Permission denied. Check file permissions or consider using a different path."),
    (r"(?:No such file or directory|ENOENT|cannot access.*No such)", "missing_file",
     "File or directory not found. Verify the path exists."),
    (r"(?:Network is unreachable|Could not resolve host|Temporary failure in name resolution|getaddrinfo failed)", "network",
     "Network unavailable. This may be transient; retrying may help."),
    (r"(?:Connection refused|Connection reset|ECONNREFUSED|ECONNRESET)", "connection",
     "Connection refused. The service may not be running; check the port/host."),
    (r"(?:timed out|ETIMEDOUT|Timeout)", "timeout",
     "Operation timed out. The service may be slow or unresponsive."),
    (r"(?:No space left on device|ENOSPC)", "disk_full",
     "Disk is full. Free up space before retrying."),
    (r"(?:Resource temporarily unavailable|EAGAIN|try again)", "transient",
     "Transient resource issue. Retrying after a short wait may resolve it."),
    (r"(?:syntax error|unexpected token|invalid syntax)", "syntax",
     "Syntax error in command. Check quoting, escaping, and special characters."),
    (r"(?:ModuleNotFoundError|ImportError|No module named)", "missing_module",
     "Python module not found. Install it with pip install."),
    (r"(?:fatal:|error:|FAILED)", "error",
     "An error was reported in the output. Review the error message above."),
]


def _analyze_error(output: str, returncode: int) -> dict:
    """Classify a command failure and suggest fixes.

    Returns {category, suggestion, retryable, output_snippet}.
    """
    if returncode == 0 and not output.strip():
        return {"category": "none", "suggestion": "", "retryable": False, "output_snippet": ""}

    snippet = output[:500] if output else "(no output)"

    for pattern, category, suggestion in _ERROR_PATTERNS:
        if re.search(pattern, output, re.IGNORECASE):
            retryable = category in ("transient", "network", "connection", "timeout")
            return {
                "category": category,
                "suggestion": suggestion,
                "retryable": retryable,
                "output_snippet": snippet,
            }

    # Non-zero exit but no recognized pattern
    if returncode != 0 and returncode is not None and returncode != -1:
        return {
            "category": "unknown_failure",
            "suggestion": f"Command exited with code {returncode}. Review output for details.",
            "retryable": False,
            "output_snippet": snippet,
        }

    return {"category": "none", "suggestion": "", "retryable": False, "output_snippet": snippet}


def _maybe_retry_suggestion(state: dict) -> str:
    """Generate a hint for the AI about recent consecutive failures.

    If the last N commands all failed, suggest a different approach.
    """
    history = state.get("terminalHistory", [])
    if len(history) < 2:
        return ""

    recent = history[-_CONSECUTIVE_FAILURE_LIMIT:]
    failures = 0
    for entry in recent:
        output = entry.get("output", "")
        cmd = entry.get("command", "")
        if not cmd:
            continue
        analysis = _analyze_error(output, -1)
        if analysis["category"] != "none":
            failures += 1

    if failures >= _CONSECUTIVE_FAILURE_LIMIT:
        return (f"\n⚠️  The last {failures} commands all failed. "
                f"Consider a different approach or checking the error messages above.")

    return ""


def _summarize_old_entries(old_entries: list) -> dict:
    """Extract structured signals from older history entries.

    Returns {
      "lines":          list[str]   — one line per old step or grouped run
      "files_touched":  list[str]   — files whose path appears as edit/cat target
      "error_steps":    int         — count of steps that errored
      "total_old":      int         — len(old_entries)
    }
    Repeated identical commands run consecutively are grouped into one
    "(×N)" line so the prompt isn't dominated by `ls; ls; ls`.
    """
    lines: list[str] = []
    files_touched: list[str] = []
    error_steps = 0

    # Group consecutive identical commands.
    i = 0
    n = len(old_entries)
    while i < n:
        entry = old_entries[i]
        cmd = (entry.get("command") or "").strip()
        rc = entry.get("returncode")
        output = entry.get("output", "") or ""

        # Look ahead for repeats
        j = i + 1
        while j < n:
            next_cmd = (old_entries[j].get("command") or "").strip()
            if next_cmd == cmd and old_entries[j].get("returncode") == rc:
                j += 1
            else:
                break
        run_len = j - i

        # Identify error vs success
        err = _analyze_error(output, rc if rc is not None else -1)
        is_error = err.get("category") not in ("none", None)
        if is_error:
            error_steps += run_len

        # Pull file paths from common edit/read commands
        m = re.search(r'(?:fs\.(?:edit|read|write|multi_edit)|cat|head|tail|less|vim|nano)\s+(?:[^"\']*"path"\s*:\s*"([^"]+)")?', cmd)
        if m and m.group(1):
            files_touched.append(m.group(1))
        else:
            # Bare-word filename heuristic: last token if it looks like a path
            parts = cmd.split()
            if parts and ("/" in parts[-1] or "." in parts[-1]) and not parts[-1].startswith("-"):
                files_touched.append(parts[-1])

        cmd_short = cmd[:100] + ("…" if len(cmd) > 100 else "")
        rc_tag = f" rc={rc}" if rc not in (None, -1) else ""
        run_tag = f" (×{run_len})" if run_len > 1 else ""
        step_label = f"[{i + 1}{'-' + str(j) if run_len > 1 else ''}]"

        if is_error:
            # Show errors verbatim (truncated to 240 chars) — signal-rich
            err_snip = err.get("output_snippet", "")[:240].replace("\n", " ⏎ ")
            lines.append(f"  {step_label} ✗ {cmd_short}{rc_tag}{run_tag} → {err_snip}")
        else:
            lines.append(f"  {step_label} ✓ {cmd_short}{rc_tag}{run_tag}")

        i = j

    return {
        "lines": lines,
        "files_touched": list(dict.fromkeys(files_touched))[-10:],  # dedupe, keep last 10
        "error_steps": error_steps,
        "total_old": n,
    }


def _compress_terminal_history(history: list) -> str:
    """Summarize older terminal steps into a compact progress log.

    When terminalHistory grows beyond _MAX_HISTORY_ENTRIES, the oldest entries
    are compressed into a structured digest (errors verbatim, successes
    grouped). The most recent _COMPRESSION_KEEP_RECENT entries are always
    preserved in full so the AI keeps fresh context.
    """
    if len(history) <= _MAX_HISTORY_ENTRIES:
        return ""

    old_entries = history[:-_COMPRESSION_KEEP_RECENT]
    recent_entries = history[-_COMPRESSION_KEEP_RECENT:]
    digest = _summarize_old_entries(old_entries)

    lines = [
        f"[DIGEST — Steps 1-{digest['total_old']} "
        f"(errors:{digest['error_steps']})]"
    ]
    if digest["files_touched"]:
        lines.append(f"  files seen: {', '.join(digest['files_touched'])}")
    lines.extend(digest["lines"])
    lines.append("")
    lines.append(f"[RECENT — Steps {len(old_entries) + 1}-{len(history)}]")

    for idx, entry in enumerate(recent_entries, len(old_entries) + 1):
        output = entry.get("output", "")
        cmd_label = (entry.get("command", "") or "")[:120]
        rc = entry.get("returncode")
        rc_tag = f" rc={rc}" if rc not in (None, -1) else ""
        err = _analyze_error(output, rc if rc is not None else -1)
        err_tag = f"  [error:{err['category']}]" if err.get("category") not in ("none", None) else ""

        out_lines = output.split('\n')
        if len(out_lines) > _MAX_TERMINAL_LINES:
            output = f"...(truncated, last {_MAX_TERMINAL_LINES} lines)...\n" + \
                     '\n'.join(out_lines[-_MAX_TERMINAL_LINES:])
        lines.append(f"--- Step {idx}: {cmd_label}{rc_tag}{err_tag} ---")
        lines.append(output if output.strip() else "(no output)")

    return '\n'.join(lines)


def _compress_conversation(chat_history: list, max_messages: int = 20) -> list:
    """Compress conversation history by summarizing oldest messages.

    Returns the (possibly compressed) conversation list for prompt display.
    Old user/AI message pairs are coalesced into a knowledge entry.
    """
    if len(chat_history) <= max_messages:
        return chat_history

    old = chat_history[:-max_messages]
    recent = chat_history[-max_messages:]

    # Summarize old messages into a compact knowledge entry
    user_msgs = [m.get("content", "")[:200] for m in old if m.get("role") == "user"]
    ai_actions = [m.get("content", "")[:200] for m in old if m.get("role") == "assistant"]

    summary = f"[Earlier context: {len(user_msgs)} user messages, "
    if user_msgs:
        summary += f"started with '{user_msgs[0][:80]}', "
    if ai_actions:
        summary += f"AI performed {len(ai_actions)} actions]"

    knowledge = [{"role": "knowledge", "content": summary}]
    return knowledge + recent


def _build_terminal_section(state: dict) -> str:
    """Section 1: recent terminal outputs with automatic compression.

    Each step is rendered with its command, exit code (when known), and
    output. Errors are flagged inline so the AI doesn't have to re-classify
    them. When history grows large, older steps are compressed into a
    one-line digest while recent steps stay verbatim.
    """
    history = state.get('terminalHistory', [])
    if not history:
        return state.get('lastOutput', 'Ready to begin.')

    compressed = _compress_terminal_history(history)
    if compressed:
        return compressed

    parts = []
    recent = history[-5:]
    offset = len(history) - len(recent)
    for i, entry in enumerate(recent, 1):
        output = entry.get('output', '')
        rc = entry.get('returncode')
        cmd_label = entry.get('command', '')[:120]

        # Inline error classification — saves the AI a turn of analysis.
        err = _analyze_error(output, rc if rc is not None else -1)
        err_tag = ""
        if err.get("category") not in ("none", None):
            err_tag = f"  [error:{err['category']}]"

        rc_tag = ""
        if rc is not None and rc != -1:
            rc_tag = f" rc={rc}"

        lines = output.split('\n')
        if len(lines) > _MAX_TERMINAL_LINES:
            output = "...(truncated, showing last %d lines)...\n" % _MAX_TERMINAL_LINES + \
                     '\n'.join(lines[-_MAX_TERMINAL_LINES:])
        parts.append(f"--- Step {offset + i}: {cmd_label}{rc_tag}{err_tag} ---")
        parts.append(output if output.strip() else "(no output)")
    return '\n'.join(parts)


def _build_memory_section(global_entries: list, state: dict, chat_history: list) -> str:
    """Section 2: session memory (short-term) + learned knowledge."""
    parts = []

    # Session memory (shortTermMemory from state)
    stm = state.get('shortTermMemory', '').strip()
    if stm:
        parts.append("[Session Memory]")
        for line in stm.split('\n'):
            line = line.strip()
            if line:
                parts.append(f"  {line}")

    # Learned knowledge (chat_history KNOWLEDGE entries)
    knowledge = [m for m in (chat_history or []) if m.get('role') == 'knowledge']
    if knowledge:
        parts.append("[Learned Knowledge]")
        for k in knowledge[-5:]:  # last 5 entries max
            content = k.get('content', '')[:500]
            if content:
                parts.append(f"  {content}")

    return '\n'.join(parts) if parts else "(empty)"


def _build_conversation_section(chat_history: list) -> str:
    """Section 3: recent conversation between user and AI (compressed when large)."""
    if not chat_history:
        return "(no history)"
    # Compress old messages into summary knowledge entries
    compressed = _compress_conversation(chat_history)
    recent = compressed[-20:]
    lines = []
    for m in recent:
        role = m.get('role', '?')
        content = m.get('content', '')
        if isinstance(content, list):
            content = ' '.join(str(c.get('text', c)) for c in content if isinstance(c, dict))
        content = str(content)[:300]
        label = "User" if role == "user" else ("Context" if role == "knowledge" else "AI")
        lines.append(f"  [{label}] {content}")
    return '\n'.join(lines) if lines else "(no history)"


def get_terminals_snapshot() -> str:
    """Collect latest 20 lines from each alive named terminal."""
    terminals = get_all_terminals()
    if not terminals:
        return ""
    terminals = [t for t in terminals if t.session is not None]
    if not terminals:
        return ""
    alive = [t for t in terminals if t.session and t.session.is_alive()]
    dead = [t for t in terminals if not (t.session and t.session.is_alive())]
    if not alive and not dead:
        return ""
    lines = []
    if alive:
        lines.append("[SUB-TERMINALS — Alive]")
        for t in alive:
            output = t.session.full_output or ""
            n = int(get_runtime_config("terminal_tail_lines"))
            tail = '\n'.join(output.split('\n')[-n:])
            st_info = f" [stationed: {', '.join(t.stationed_agent_ids)}]" if t.stationed_agent_ids else ""
            lines.append(f"  {t.name} ({t.command}){st_info}:")
            if tail.strip():
                for tl in tail.split('\n'):
                    lines.append(f"    | {tl}")
            else:
                lines.append("    (no output yet)")
    if dead:
        lines.append("[SUB-TERMINALS — Dead]")
        for t in dead:
            lines.append(f"  {t.name} ({t.command})")
    return '\n'.join(lines)


# ── AI Agent Loop ──────────────────────────────────────────────────────

def _detect_loop_warnings(state: dict, original_input: str) -> list[str]:
    """Detect stuck / repetitive behaviour and return human-readable warnings.

    Surfaces the AI's own pattern back to it so it can break out: identical
    commands run >=3 times, >=N consecutive errors, or excessive read-only
    exploration without any acting verb.
    """
    history = state.get("terminalHistory", [])
    warnings: list[str] = []

    if len(history) < 3:
        return warnings

    # 1. Same exact command 3+ consecutive times
    last_cmds = [(h.get("command") or "").strip() for h in history[-3:]]
    if last_cmds[0] and last_cmds[0] == last_cmds[1] == last_cmds[2]:
        warnings.append(
            f"You have run `{last_cmds[0][:80]}` 3 times in a row. "
            f"This usually means you're stuck — change approach or set done=true."
        )

    # 2. 3+ consecutive failures (any commands)
    recent = history[-3:]
    fail_count = 0
    for h in recent:
        err = _analyze_error(h.get("output", ""),
                             h.get("returncode") if h.get("returncode") is not None else -1)
        if err.get("category") not in ("none", None):
            fail_count += 1
    if fail_count >= 3:
        warnings.append(
            f"The last {fail_count} commands all failed. "
            f"Re-read the error output above and change strategy — "
            f"do not repeat with the same parameters."
        )

    # 3. Many read-only steps with no edit action — pure exploration drift
    READ_ONLY = {"ls", "cat", "head", "tail", "grep", "find", "pwd", "which",
                 "type", "file", "stat", "wc", "tree", "echo"}
    if len(history) >= 8:
        last8 = history[-8:]
        readonly_count = 0
        for h in last8:
            cmd = (h.get("command") or "").strip()
            if not cmd:
                continue
            first_token = cmd.split()[0] if cmd.split() else ""
            # /tool fs.read / fs.grep / fs.glob / fs.ls also count as read-only
            if first_token in READ_ONLY or cmd.startswith(("/tool fs.read",
                                                          "/tool fs.grep",
                                                          "/tool fs.glob",
                                                          "/tool fs.ls")):
                readonly_count += 1
        if readonly_count >= 7:
            warnings.append(
                "You have done 7+ read-only steps without any edit/build/run "
                "action. Either start acting on what you've learned, or set "
                "done=true and report findings."
            )

    return warnings


def _track_files_in_command(cmd: str, seen: list) -> None:
    """Extract file paths the command appears to read/write and append to `seen`.

    Dedupes (keeps insertion order) and caps at 30 entries. Recognises:
      - /tool fs.read|edit|write|multi_edit {"path": "..."}
      - cat / head / tail / less / vim / nano / cp / mv <path>
      - fs.grep / fs.glob have variable file targets — skipped (too noisy)
    """
    if not cmd:
        return

    found: list[str] = []

    # /tool fs.<verb> {"path": "..."}
    m = re.search(r'/tool\s+fs\.(?:read|edit|write|multi_edit)\s+(\{.*\})', cmd)
    if m:
        try:
            payload = json.loads(m.group(1))
            p = payload.get("path")
            if isinstance(p, str) and p:
                found.append(p)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Bare shell file commands
    parts = cmd.split()
    if parts:
        first = parts[0].rsplit("/", 1)[-1]
        if first in ("cat", "head", "tail", "less", "vim", "nano", "view",
                     "cp", "mv", "touch", "stat", "file", "wc"):
            for tok in parts[1:]:
                if tok.startswith("-"):
                    continue
                if "/" in tok or "." in tok or tok.isidentifier():
                    found.append(tok)
                    break  # only the first path arg

    for p in found:
        if p in seen:
            # Move-to-end so MRU stays visible
            try:
                seen.remove(p)
            except ValueError:
                pass
        seen.append(p)

    # Cap
    if len(seen) > 30:
        del seen[: len(seen) - 30]


def _build_user_message(original_input: str, state: dict, memory_entries: list,
                        chat_history: list, loop: int, max_loops: int) -> str:
    """Compose the user-message body for one agent iteration.

    Section order matters for LLM attention. Recent recommendations and our
    own observations: task first, then the freshest signal (last command +
    output), then progressively older / more-derived context (history,
    memory, sibling terminals). This is the inverse of the old layout where
    the task was buried at the bottom.
    """
    terminal_section = _build_terminal_section(state)
    conversation_section = _build_conversation_section(chat_history)
    memory_section = _build_memory_section(memory_entries, state, chat_history)
    terminals_snapshot = get_terminals_snapshot()
    n_steps = len(state.get('terminalHistory', []))
    warnings = _detect_loop_warnings(state, original_input)
    files_seen = state.get("_files_seen", [])

    warnings_block = ""
    if warnings:
        bullets = "\n".join(f"  - {w}" for w in warnings)
        warnings_block = f"\n<warnings>\n{bullets}\n</warnings>\n"

    files_block = ""
    if files_seen:
        files_block = f"\n<files_seen>\n  {', '.join(files_seen[-15:])}\n</files_seen>\n"

    return f"""<task>
{original_input}
</task>

<progress>
step {loop+1}/{max_loops} — {n_steps} command(s) executed so far
</progress>
{warnings_block}{files_block}
<recent_terminal_output>
{terminal_section}
</recent_terminal_output>

<conversation>
{conversation_section}
</conversation>

<session_memory>
{memory_section}
</session_memory>

<sub_terminals>
{terminals_snapshot or "(none)"}
</sub_terminals>"""


def _detect_lang(text: str) -> str:
    """Detect the user's language from input text. Returns a language code."""
    import re
    if re.search(r'[一-鿿㐀-䶿豈-﫿]', text):
        return "ZH"
    if re.search(r'[぀-ゟ゠-ヿ]', text):
        return "JA"
    if re.search(r'[가-힯ᄀ-ᇿ]', text):
        return "KO"
    return "EN"


_loop_cmd_handler_cache = None
_loop_cmd_mtime_cache = 0


def clear_loop_command_cache():
    """Clear .loop_command.py cache so it reloads on next use."""
    global _loop_cmd_handler_cache, _loop_cmd_mtime_cache
    _loop_cmd_handler_cache = None
    _loop_cmd_mtime_cache = 0


def _load_loop_commands():
    """Load .loop_command.py and return handle_loop_command() if defined."""
    global _loop_cmd_handler_cache, _loop_cmd_mtime_cache
    try:
        path = os.path.join(os.getcwd(), ".loop_command.py")
        mtime = os.path.getmtime(path)
        if _loop_cmd_handler_cache is not None and mtime == _loop_cmd_mtime_cache:
            return _loop_cmd_handler_cache
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        ns = {}
        exec(compile(src, path, "exec"), ns)
        handler = ns.get("handle_loop_command")
        _loop_cmd_handler_cache = handler
        _loop_cmd_mtime_cache = mtime
        return handler
    except Exception:
        _loop_cmd_handler_cache = None
        _loop_cmd_mtime_cache = 0
        return None


def _execute_parent_command(cmd: str) -> str:
    """Execute a command in the parent process context so side effects
    (cd, clear, etc.) apply to the parent terminal, not a child PTY."""
    stripped = cmd.strip()
    if stripped in ("cd",) or stripped.startswith("cd "):
        path = stripped[3:].strip() if stripped.startswith("cd ") else os.path.expanduser("~")
        try:
            os.chdir(path)
            return f"cd → {os.getcwd()}"
        except Exception as e:
            return f"cd error: {e}"
    if stripped in ("clear",) or stripped.startswith("clear "):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        return ""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
            cwd=os.getcwd(),
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return f"Parent command timed out: {cmd}"
    except Exception as e:
        return f"Parent command error: {e}"


def _check_policy(command: str, agent_id: str = None,
                  req_id: str = None, events_cb=None,
                  deps=None) -> tuple:
    """Evaluate security policy for a command before execution.

    Returns (allowed: bool, reason: str, needs_approval: bool).
    Side-effect: logs audit entry, prints warning/error via deps.console.
    """
    decision = policy_mod.evaluate(command, os.getcwd(),
                                   req_id=req_id, agent_id=agent_id)
    if decision.action == "deny":
        msg = f"[bold red]BLOCKED:[/bold red] {decision.reason}"
        if events_cb is not None and deps is not None:
            deps.console.print(msg)
        return False, decision.reason, False
    if decision.action == "needs_approval":
        msg = f"[bold yellow]APPROVAL REQUIRED:[/bold yellow] {decision.reason}"
        if events_cb is not None and deps is not None:
            deps.console.print(msg)
        return True, decision.reason, True
    return True, "", False


def _process_parent_cmd_marker(cmd_output: str) -> tuple:
    """Scan sub-terminal output for __PARENT_CMD__:<cmd> markers.
    Execute any found commands in the parent context and return
    (cleaned_output, parent_result | None)."""
    import re as _re
    m = _re.search(r'__PARENT_CMD__:(.*?)(?:\n|$)', cmd_output)
    if not m:
        return cmd_output, None
    cmd = m.group(1).strip()
    cleaned = _re.sub(r'__PARENT_CMD__:[^\n]*\n?', '', cmd_output).strip()
    result = _execute_parent_command(cmd)
    return cleaned, result


def _format_tool_result_for_loop(tool_name: str, result: dict, max_chars: int) -> str:
    """Render a tool result as the string the AI sees in [recent_terminal_output].

    Heuristics, in priority order:
      - error → "[tool error] <name>: <error>"
      - result is a string → return the string verbatim (most common case;
        the structured metadata isn't useful to the AI for fs.read / shell.exec)
      - result is a primitive → str(result)
      - result is a list/dict → pretty-printed JSON of just the `result` field
        plus a short metadata footer ("matches=N truncated=true")
      - no `result` key → pretty-print the whole dict
    """
    if not isinstance(result, dict):
        return str(result)[:max_chars]

    if not result.get("ok", True):
        err = result.get("error") or "(no error message)"
        return f"[tool error] {tool_name}: {err}"

    payload = result.get("result")
    if payload is None:
        # No `result` key — dump everything (minus the redundant tool/ok fields).
        clone = {k: v for k, v in result.items() if k not in ("tool", "ok")}
        try:
            text = json.dumps(clone, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(clone)
        return text[:max_chars]

    # Build a one-line footer of "interesting" metadata fields so the AI sees
    # truncation / counts without needing to parse a giant dict.
    meta_keys = (
        "truncated", "byte_truncated", "lines_returned", "total_lines",
        "matches", "files_scanned", "replacements", "exit_code",
        "duration_ms", "count", "path", "url", "size",
    )
    meta_bits = []
    for k in meta_keys:
        if k in result and result[k] not in (None, "", False, 0):
            v = result[k]
            if isinstance(v, str) and len(v) > 80:
                v = v[:77] + "..."
            meta_bits.append(f"{k}={v}")
    footer = f"\n[{' '.join(meta_bits)}]" if meta_bits else ""

    if isinstance(payload, str):
        body = payload
    elif isinstance(payload, (int, float, bool)):
        body = str(payload)
    else:
        try:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            body = str(payload)

    text = body + footer
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...(truncated to {max_chars} chars)"
    return text


def run_agent_loop(
    deps: LoopDeps,
    original_input: str,
    session: dict,
    state: dict,
    chat_history: list = None,
    events_cb = None,          # callable(list[dict]) — push events to backend
    existing_session = None,   # Optional[InteractiveSession] — reuse existing PTY
    depth: int = 0,            # 0=user terminal, 1+=sub-agent
    agent_id: str = None,      # Phase 2: explicit agent identity (thread-safe;
                               # falls back to get_current_agent() if None)
) -> dict:
    """Run the autonomous agent loop (mirrors AutonomousKernel.ts).

    If events_cb is provided, all outputs are collected as structured events
    and pushed via the callback for real-time streaming to Helpwo UI.

    If existing_session is provided, it is reused instead of creating a new
    PTY session. The caller (REPL) manages its lifecycle.

    depth=0: user's terminal — output streams directly (stream_output=True)
    depth>=1: sub-agent — output captured and shown in indented panels
    """
    state = dict(state)  # copy
    state.setdefault("shortTermMemory", "")
    state.setdefault("lastReply", "")
    state.setdefault("lastOutput", "")
    state.setdefault("terminalHistory", [])
    chat_history = chat_history or []

    step_replies = []
    user_input = original_input
    pending_events: list[dict] = []
    done = False
    reply = ""
    interactive_session = existing_session  # InteractiveSession | SubTerminalSession | None

    # In execute/non-interactive mode, suppress Rich console output.
    # Child laintas terminals capture PTY output; Rich markup pollutes it.
    if events_cb is None:
        class _QuietConsole:
            def print(self, *a, **kw): pass
            def status(self, *a, **kw):
                from contextlib import nullcontext
                return nullcontext()
            def __getattr__(self, name):
                return lambda *a, **kw: None
        deps.console = _QuietConsole()
        deps.display_command_output = lambda *a, **kw: None
        deps.display_sub_terminal_preview = lambda *a, **kw: None

    max_loops = int(get_runtime_config("max_loops"))
    # Phase 2: lookup own AgentInfo once for the lifetime of this loop call.
    # Sub-agent threads MUST pass agent_id explicitly — relying on the global
    # _current_agent_id is racy when multiple agents run concurrently.
    staleness_limit = int(get_runtime_config("staleness_limit"))
    stale_count = 0
    self_info = get_agent(agent_id) if agent_id else None
    for loop in range(max_loops):
        _loop_id = next_debug_loop()

        # ── Phase 2: abort check + inbox drain ────────────────────────
        if self_info is not None:
            if self_info.abort_event.is_set():
                state["lastReply"] = "(aborted by control plane)"
                self_info.status = "aborted"
                break
            inbox_msgs = drain_inbox(self_info.id)
        else:
            inbox_msgs = []
        if inbox_msgs:
            state["_inbox"] = inbox_msgs   # JSONified into prompt below

        # 1. Read .helpwo memory (structured)
        memory_entries = _read_memory(deps)

        # 2. Build global memory string for system prompt
        if memory_entries:
            global_memory_lines = []
            for e in memory_entries:
                global_memory_lines.append(f"[{e['id']}] {e['content']}")
            global_memory_str = '\n'.join(global_memory_lines)
        else:
            global_memory_str = "(empty)"

        # 3. Read .cli.prop system prompt
        prompt_template = deps.read_file(".cli.prop") or ""
        if not prompt_template:
            prompt_template = deps.generate_prompt()

        # 4. Build system prompt
        # Phase 2: prefer self_info (passed-in agent_id) over the global
        # _current_agent_id so sub-agent threads can't trample each other.
        current_agent = self_info if self_info is not None else get_current_agent()
        agent_name = current_agent.name if current_agent else "Laintas CLI"
        agent_id_str = current_agent.id if current_agent else "unknown"

        # Inbox messages this iteration, rendered as a compact JSON block.
        if inbox_msgs:
            try:
                inbox_str = json.dumps(inbox_msgs, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                inbox_str = str(inbox_msgs)
        else:
            inbox_str = "(empty)"

        # Children + parent metadata
        if current_agent:
            children = []
            for cid in current_agent.child_ids:
                ca = get_agent(cid)
                if ca:
                    children.append(f"{ca.id} [{ca.status}]")
            children_str = ", ".join(children) if children else "(none)"
            parent_str = current_agent.parent_id or "(none)"
        else:
            children_str = "(none)"
            parent_str = "(none)"

        system_prompt = prompt_template \
            .replace("{{globalMemory}}", global_memory_str) \
            .replace("{{persistentMemory}}", memory_system.get_memory_context()) \
            .replace("{{planMode}}", plan_mode.get_plan_prompt()) \
            .replace("{{agentName}}", agent_name) \
            .replace("{{agentId}}", agent_id_str) \
            .replace("{{currentPath}}", os.getcwd()) \
            .replace("{{activeFile}}", "None") \
            .replace("{{depth}}", str(depth)) \
            .replace("{{nextDepth}}", str(depth + 1)) \
            .replace("{{inbox}}", inbox_str) \
            .replace("{{children}}", children_str) \
            .replace("{{parent}}", parent_str) \
            .replace("{{tools}}", tools_mod.get_registry().describe_for_prompt())

        # 5. Build user message via the structured-section helper.
        terminal_section = _build_terminal_section(state)
        memory_section = _build_memory_section(memory_entries, state, chat_history)
        conversation_section = _build_conversation_section(chat_history)
        terminals_snapshot = get_terminals_snapshot()
        user_input = _build_user_message(
            original_input, state, memory_entries, chat_history, loop, max_loops,
        )

        # ── Debug: create entry before API call ──
        debug_entry = DebugEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            loop=_loop_id,
            user_input=user_input[:2000],
            current_path=os.getcwd(),
            context_sizes={
                "memory": len(memory_section),
                "terminal": len(terminal_section),
                "conversation": len(conversation_section),
                "terminals": len(terminals_snapshot),
                "prompt": len(system_prompt),
            },
            request_body={
                "message": user_input[:2000],
                "currentPath": os.getcwd(),
                "history": chat_history[-20:] if chat_history else [],
                "promptLen": len(system_prompt),
                "promptPreview": system_prompt[:500],
                "memorySection": memory_section[:500],
            },
        )

        # 5. Call backend (skip spinner in non-interactive/execute mode)
        lang = _detect_lang(original_input)
        if events_cb is not None:
            # Streaming render: use rich.live.Live to render the reply as it arrives
            # via on_chunk. Falls back to spinner if backend doesn't accept on_chunk.
            stream_state = {"reply": "", "command": "", "started": False}
            try:
                from rich.live import Live
                from rich.spinner import Spinner
                from rich.console import Group
                from rich.text import Text

                def _render():
                    parts = []
                    if stream_state["reply"]:
                        parts.append(deps.Markdown(stream_state["reply"]))
                    else:
                        parts.append(Spinner("dots", text=Text("AI thinking...", style="bold green")))
                    if stream_state["command"]:
                        cmd_preview = stream_state["command"]
                        if len(cmd_preview) > 120:
                            cmd_preview = cmd_preview[:117] + "..."
                        parts.append(Text(f"→ {cmd_preview}", style="dim cyan"))
                    return Group(*parts)

                with Live(_render(), console=deps.console, refresh_per_second=12, transient=False) as live:
                    def _on_chunk(field, value):
                        if field == "reply":
                            stream_state["reply"] += value
                        elif field == "command":
                            stream_state["command"] = value
                        stream_state["started"] = True
                        try: live.update(_render())
                        except Exception: pass

                    try:
                        response = deps.call_backend(
                            session=session,
                            message=user_input,
                            system_prompt=system_prompt,
                            current_path=os.getcwd(),
                            history=chat_history,
                            on_chunk=_on_chunk,
                            lang=lang,
                        )
                    except TypeError:
                        # Backend doesn't support on_chunk — fall back
                        response = deps.call_backend(
                            session=session,
                            message=user_input,
                            system_prompt=system_prompt,
                            current_path=os.getcwd(),
                            history=chat_history,
                            lang=lang,
                        )
            except ImportError:
                with deps.console.status("[bold green]AI thinking...[/bold green]", spinner="dots"):
                    try:
                        response = deps.call_backend(
                            session=session,
                            message=user_input,
                            system_prompt=system_prompt,
                            current_path=os.getcwd(),
                            history=chat_history,
                            lang=lang,
                        )
                    except TypeError:
                        response = deps.call_backend(
                            session=session,
                            message=user_input,
                            system_prompt=system_prompt,
                            current_path=os.getcwd(),
                            history=chat_history,
                            lang=lang,
                        )
            # Mark that the streaming Live already rendered the reply — avoid
            # re-printing it below.
            _reply_already_rendered = bool(stream_state.get("reply"))
        else:
            _reply_already_rendered = False
            response = deps.call_backend(
                session=session,
                message=user_input,
                system_prompt=system_prompt,
                current_path=os.getcwd(),
                history=chat_history,
                lang=lang,
            )

        # ── Debug: capture AI response ──
        debug_entry.response_raw = response
        debug_entry.reply = response.get("reply", "") or ""
        debug_entry.command = (response.get("command") or "").strip()
        debug_entry.memory = (response.get("memory") or "").strip()
        debug_entry.done = response.get("done", False)
        debug_entry.error = response.get("error", False)
        debug_entry.billing = response.get("_billing", {})

        if response.get("error"):
            if events_cb is not None:
                deps.console.print(f"[red]{response['reply']}[/red]")
            state["shortTermMemory"] += f"\n  -Error: {response['reply']}"
            add_debug_log(debug_entry)
            break

        reply = response.get("reply") or ""
        command = (response.get("command") or "").strip()
        memory = (response.get("memory") or "").strip()
        done = response.get("done", False)
        billing = response.get("_billing", {})

        # ── Detect "silent failure": model generated tokens but backend
        # extracted nothing into reply/command/memory. This means the model's
        # output didn't match the expected JSON schema. Retrying wastes tokens
        # — bail out immediately with a useful message.
        if not reply and not command and not memory and not done:
            completion_tokens = (billing or {}).get("completionTokens", 0)
            if completion_tokens > 0:
                msg = (
                    f"AI generated {completion_tokens} tokens but backend extracted no fields. "
                    f"The model likely produced output that didn't match the JSON schema "
                    f"(e.g. plain text instead of `{{\"reply\": ...}}`). "
                    f"Try rephrasing or shortening the prompt."
                )
                if events_cb is not None:
                    deps.console.print(f"[yellow]{msg}[/yellow]")
                state["shortTermMemory"] += f"\n  -Error: {msg}"
                add_debug_log(debug_entry)
                break

        # 6. Print AI reply (only in interactive mode)
        if reply:
            if events_cb is not None and not _reply_already_rendered:
                deps.console.print(deps.Markdown(reply))
            step_replies.append(reply)
            state["lastReply"] = reply
            if events_cb is not None:
                pending_events.append({"type": "ai", "content": reply})

        # 7. Show billing if available (only in interactive mode)
        if billing:
            cost = billing.get("costCents", 0)
            balance = billing.get("balanceCents", 0)
            if cost > 0:
                billing_text = f"${cost / 100:.2f} · balance ${balance / 100:.2f}"
                if events_cb is not None:
                    deps.console.print(f"[dim]({billing_text})[/dim]")
                    pending_events.append({"type": "system", "kind": "billing", "content": billing_text})

        # 8. Write memory to .helpwo (append / modify / delete)
        if memory:
            mem_result = _apply_memory(deps, memory)
            if events_cb is not None:
                deps.console.print(f"[dim cyan]{mem_result}[/dim cyan]")
                pending_events.append({"type": "system", "kind": "memory", "content": mem_result[:200]})

        # 9. Handle command dispatch (close_session/send_keys normalized to /session close, /keys)

        # ── Pre-check: .loop_command.py custom handler ──
        # Called BEFORE the if-elif chain so that when the handler returns None
        # (passes), the command falls through to the stationed/unstationed split.
        loop_result = None
        loop_exception = None
        if command:
            loop_handler = _load_loop_commands()
            if loop_handler:
                ctx = {
                    "deps": deps, "state": state, "debug_entry": debug_entry,
                    "chat_history": chat_history,
                    "interactive_session_ref": [interactive_session],
                    "events_cb": events_cb, "pending_events_ref": [pending_events],
                    "get_terminal": get_terminal, "get_all_terminals": get_all_terminals,
                    "register_terminal": register_terminal,
                    "unregister_terminal": unregister_terminal,
                    "close_all_terminals": close_all_terminals,
                    "get_agent": get_agent, "get_all_agents": get_all_agents,
                    "get_current_agent": get_current_agent,
                    "switch_to_agent": switch_to_agent,
                    "station_agent": station_agent, "unstation_agent": unstation_agent,
                    "get_config": get_runtime_config,
                    "set_config": set_runtime_config,
                    "list_config": list_runtime_config,
                    "reset_config": reset_runtime_config,
                    "depth": depth,
                }
                try:
                    loop_result = loop_handler(command, ctx)
                except Exception as e:
                    loop_exception = e

        if loop_result is not None:
            state["lastOutput"] = loop_result
            debug_entry.exec_command = command
            if events_cb is not None:
                pending_events.append({"type": "system", "kind": "command", "content": command})
        elif loop_exception is not None:
            if events_cb is not None:
                deps.console.print(f"[red].loop_command.py error: {loop_exception}[/red]")
            state["lastOutput"] = f".loop_command.py error: {loop_exception}"
        elif command:
            # ── Meta-command: /session close ────────────────────────────
            if re.match(r'^/session\s+close\s*$', command):
                if interactive_session is not None:
                    deps.console.print(f"[dim yellow]Closing session: {interactive_session.command[:60]}[/dim yellow]")
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "close_session", "content": interactive_session.command[:200]})
                    interactive_session.close()
                    cmd_output = deps.strip_ansi(interactive_session.full_output)
                    debug_entry.exec_command = command
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = interactive_session.returncode
                    state["lastOutput"] = cmd_output.strip() or "(no output)"
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                        events_cb(pending_events)
                        pending_events.clear()
                    deps.display_command_output(interactive_session.command, interactive_session.returncode, cmd_output, depth=depth + 1)
                    interactive_session = None
                else:
                    deps.console.print("[yellow]Warning: no active session to close[/yellow]")
                    state["lastOutput"] = "Warning: no active session to close"
                    debug_entry.exec_command = command
                    debug_entry.exec_returncode = -1

            # ── Meta-command: /keys <text> ─────────────────────────────
            elif (_meta_keys := re.match(r'^/keys\s+(.*)$', command)):
                keys = _meta_keys.group(1)
                if interactive_session is not None:
                    _ks = keys[:60]
                    _suf = "" if len(keys) <= 60 else "..."
                    deps.console.print(f"[dim yellow]> {_ks}{_suf}[/dim yellow]")
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "send_keys", "content": keys[:200]})
                    interactive_session.send_keys(keys)
                    time.sleep(0.3)
                    new_output = interactive_session.read_output(timeout=0.5)
                    cmd_output = deps.strip_ansi(interactive_session.full_output)
                    debug_entry.exec_command = command
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = interactive_session.returncode
                    debug_entry.session_command = interactive_session.command
                    state["lastOutput"] = cmd_output.strip() or "(no output)"
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                        events_cb(pending_events)
                        pending_events.clear()
                    if not interactive_session.is_alive():
                        deps.display_command_output(interactive_session.command, interactive_session.returncode, cmd_output, depth=depth + 1)
                        interactive_session = None
                    else:
                        deps.display_command_output(interactive_session.command, -1, new_output, depth=depth + 1)
                else:
                    deps.console.print("[yellow]Warning: /keys ignored (no active interactive session)[/yellow]")
                    state["lastOutput"] = "Warning: /keys ignored (no active interactive session)"
                    debug_entry.exec_command = command
                    debug_entry.exec_returncode = -1

            # ── Meta-command: /station [name] ────────────────────────────
            elif (_meta_station := re.match(r'^/(?:station|st)(?:\s+(\S+))?\s*$', command)):
                if _meta_station and _meta_station.group(1):
                    name = _meta_station.group(1)
                else:
                    name = "main"

                existing_term = get_terminal(name)
                if existing_term and existing_term.session and not existing_term.session.is_alive():
                    unregister_terminal(name)
                    existing_term = None
                if existing_term is None:
                    register_terminal(None, name, depth, name=name)

                current_agent = get_current_agent()
                if current_agent:
                    station_agent(current_agent.id, name)
                label = "current terminal" if name == "main" else f"terminal {name}"
                state["lastOutput"] = f"Stationed in {label}"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
                    deps.console.print(
                        f"[dim green]Stationed in {label}[/dim green]")
                    pending_events.append({"type": "system", "kind": "command", "content": command})
                    events_cb(pending_events)
                    pending_events.clear()

            # ── Meta-command: /terminate <name> ─────────────────────────
            elif (_meta_terminate := re.match(r'^/terminate\s+(\S+)\s*$', command)):
                target = _meta_terminate.group(1)
                term = get_terminal(target)
                if term:
                    for aid in list(term.stationed_agent_ids):
                        unstation_agent(aid)
                if unregister_terminal(target):
                    if events_cb is not None:
                        deps.console.print(
                            f"[dim yellow]- Terminated [bold]{target}[/bold][/dim yellow]")
                    state["lastOutput"] = f"Terminated {target}"
                else:
                    state["lastOutput"] = f"Warning: terminal '{target}' not found"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "close_session", "content": target})
                    events_cb(pending_events)
                    pending_events.clear()

            # ── Meta-command: /send <name> <cmd> ────────────────────────
            elif (_meta_send := re.match(r'^/send\s+(\S+)\s+(.+)$', command)):
                target = _meta_send.group(1)
                keys = _meta_send.group(2)
                term = get_terminal(target)
                if term is None:
                    state["lastOutput"] = f"Error: terminal '{target}' not found"
                    if events_cb is not None:
                        deps.console.print(f"[yellow]Warning: terminal '{target}' not found[/yellow]")
                elif not (term.session and term.session.is_alive()):
                    state["lastOutput"] = f"Warning: terminal '{target}' is dead"
                    if events_cb is not None:
                        deps.console.print(f"[yellow]Warning: terminal '{target}' is dead[/yellow]")
                else:
                    _ks = keys[:60]
                    _suf = "" if len(keys) <= 60 else "..."
                    if events_cb is not None:
                        deps.console.print(f"[dim yellow]> /send {target} {_ks}{_suf}[/dim yellow]")
                    term.session.send_keys(keys + "\n")
                    time.sleep(0.3)
                    term.session.read_output(timeout=0.5)
                    cmd_output = deps.strip_ansi(term.session.full_output)
                    state["lastOutput"] = cmd_output.strip() or "(no output)"
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = term.session.returncode
                    debug_entry.session_command = f"{target}: {keys}"
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "send_keys", "content": keys[:200]})
                        pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                        events_cb(pending_events)
                        pending_events.clear()
                    if not (term.session and term.session.is_alive()):
                        if events_cb is not None:
                            deps.console.print(f"[dim yellow]Terminal [bold]{target}[/bold] exited[/dim yellow]")
                debug_entry.exec_command = command
                debug_entry.exec_returncode = -1

            # ── Meta-command: /hire ─────────────────────────────────────
            elif re.match(r'^/hire\s*$', command):
                agent_info = register_agent(depth=depth)
                if events_cb is not None:
                    deps.console.print(
                        f"[dim green]+ Hired [bold]{agent_info.id}[/bold][/dim green]")
                state["lastOutput"] = f"Hired {agent_info.id}"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "command", "content": command})
                    events_cb(pending_events)
                    pending_events.clear()

            # ── Meta-command: /agents [name|name <n>|tree] ──────────────
            elif (_meta_agents := re.match(r'^/agents(?:\s+(.+))?$', command)):
                rest = (_meta_agents.group(1) or "").strip()
                if not rest:
                    agents = get_all_agents()
                    current = self_info or get_current_agent()
                    lines = ["Available agents:"]
                    for a in agents:
                        marker = " <-- self" if (current and a.id == current.id) else ""
                        st = f" [stationed: {a.stationed_terminal}]" if a.stationed_terminal else ""
                        st += f" [{a.status}]" if a.status != "idle" else ""
                        lines.append(f"  {a.id}: {a.name}{st}{marker}")
                    state["lastOutput"] = "\n".join(lines)
                    if events_cb is not None:
                        deps.console.print("\n".join(lines))
                elif rest == "tree":
                    tree = build_agents_tree()
                    state["lastOutput"] = tree
                    if events_cb is not None:
                        deps.console.print(tree)
                elif rest.startswith("name "):
                    new_name = rest[5:].strip()
                    current = self_info or get_current_agent()
                    if current and rename_agent(current.id, new_name):
                        state["lastOutput"] = f"Agent renamed to {new_name}"
                        if events_cb is not None:
                            deps.console.print(f"[green]Agent renamed to [bold]{new_name}[/bold][/green]")
                    else:
                        state["lastOutput"] = "No current agent to rename"
                else:
                    if switch_to_agent(rest):
                        agent = get_agent(rest)
                        state["lastOutput"] = f"Switched to {agent.name} ({agent.id})"
                        if events_cb is not None:
                            deps.console.print(
                                f"[green]Switched to [bold]{agent.name}[/bold][/green]")
                    else:
                        state["lastOutput"] = f"Agent '{rest}' not found"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0

            # ── Meta-command: /spawn [name:] <task> ─────────────────────
            # In-process sub-agent. Parent continues; child runs in its own
            # thread and posts {kind:"child-done"|"child-error"} to parent inbox.
            elif (_meta_spawn := re.match(r'^/spawn\s+(?:(\S+):\s+)?(.+)$', command)):
                child_name = _meta_spawn.group(1)
                task = _meta_spawn.group(2).strip()
                parent_id = (self_info.id if self_info else
                             (get_current_agent().id if get_current_agent() else None))
                if parent_id is None:
                    state["lastOutput"] = "Error: /spawn requires a parent agent context"
                else:
                    child_id = spawn_subagent(
                        parent_id=parent_id, task=task, deps=deps,
                        name=child_name, session=session, events_cb=events_cb,
                    )
                    if child_id is None:
                        state["lastOutput"] = f"Error: spawn failed (parent '{parent_id}' not found)"
                    else:
                        msg = f"Spawned child agent '{child_id}' (parent={parent_id}, task={task[:60]})"
                        state["lastOutput"] = msg
                        if events_cb is not None:
                            deps.console.print(f"[dim green]{msg}[/dim green]")
                            pending_events.append({"type": "system", "kind": "spawn",
                                                    "content": child_id,
                                                    "meta": {"parent": parent_id, "task": task[:200]}})
                            events_cb(pending_events)
                            pending_events.clear()
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0

            # ── Meta-command: /tell <agent_id> <message...> ─────────────
            elif (_meta_tell := re.match(r'^/tell\s+(\S+)\s+(.+)$', command)):
                target_id = _meta_tell.group(1)
                payload = _meta_tell.group(2)
                # Try parse as JSON for structured payloads; else wrap as text.
                try:
                    body = json.loads(payload)
                    if not isinstance(body, dict):
                        body = {"kind": "msg", "text": payload}
                except (ValueError, TypeError):
                    body = {"kind": "msg", "text": payload}
                body.setdefault("from", self_info.id if self_info else "unknown")
                ok = send_to_agent(target_id, body)
                state["lastOutput"] = (f"Sent to {target_id}: {payload[:120]}"
                                       if ok else
                                       f"Error: agent '{target_id}' not found or inbox full")
                if events_cb is not None and ok:
                    pending_events.append({"type": "system", "kind": "tell",
                                            "content": target_id,
                                            "meta": {"payload": payload[:500]}})
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0 if ok else -1

            # ── Meta-command: /wait <agent_id> [timeout] ────────────────
            # Blocks this agent's loop until target finishes. Default 30s cap.
            elif (_meta_wait_a := re.match(r'^/wait\s+(\S+)(?:\s+(\d+(?:\.\d+)?))?\s*$', command)):
                target_id = _meta_wait_a.group(1)
                t_raw = _meta_wait_a.group(2)
                try:
                    t = float(t_raw) if t_raw else 30.0
                except ValueError:
                    t = 30.0
                t = max(0.5, min(t, 300.0))   # clamp 0.5s–5min
                if events_cb is not None:
                    deps.console.print(f"[dim]⏳ waiting for {target_id} (≤{t}s)…[/dim]")
                final = wait_for_agent(target_id, timeout=t)
                if final is None:
                    state["lastOutput"] = f"Timeout / not found: {target_id}"
                    debug_entry.exec_returncode = -1
                else:
                    state["lastOutput"] = (f"Agent {target_id} {final.status}: "
                                            f"{(final.last_reply or '(no reply)')[:500]}")
                    debug_entry.exec_returncode = 0
                debug_entry.exec_command = command

            # ── Meta-command: /abort <agent_id> ─────────────────────────
            elif (_meta_abort := re.match(r'^/abort\s+(\S+)\s*$', command)):
                target_id = _meta_abort.group(1)
                ok = abort_agent(target_id)
                state["lastOutput"] = (f"Abort signal sent to {target_id}"
                                        if ok else
                                        f"Error: agent '{target_id}' not found")
                if events_cb is not None and ok:
                    pending_events.append({"type": "system", "kind": "abort",
                                            "content": target_id})
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0 if ok else -1

            # ── Meta-command: /tool <name> <json_params> ────────────────
            # Dispatches to the global tool registry (builtins + skills + MCP).
            # JSON params are optional; missing/invalid → invoke with {}.
            elif (_meta_tool := re.match(r'^/tool\s+(\S+)(?:\s+(.+))?$', command)):
                tool_name = _meta_tool.group(1)
                raw_params = (_meta_tool.group(2) or "").strip()
                try:
                    params = json.loads(raw_params) if raw_params else {}
                    if not isinstance(params, dict):
                        params = {"value": params}
                except (ValueError, TypeError):
                    params = {"raw": raw_params}
                ctx = tools_mod.ToolCtx(
                    deps=deps,
                    agent_id=(self_info.id if self_info else None),
                    session=session,
                    events_cb=events_cb,
                    cwd=os.getcwd(),
                )
                # Human-readable status hint: "reading foo.py", "editing bar.py", etc.
                _action_map = {
                    "fs.read": "reading", "fs.edit": "editing", "fs.write": "writing",
                    "fs.multi_edit": "editing", "fs.diff": "diffing", "fs.glob": "globbing",
                    "fs.grep": "grepping", "fs.ls": "listing", "shell.exec": "running",
                    "web.fetch": "fetching", "web.search": "searching",
                }
                _verb = _action_map.get(tool_name, "invoking")
                _hint = params.get("path") or params.get("pattern") or params.get("query") or params.get("command") or tool_name
                if isinstance(_hint, str) and len(_hint) > 60: _hint = _hint[:57] + "..."
                _status_msg = f"[bold cyan]{_verb}[/bold cyan] [dim]{_hint}[/dim]"
                _t0 = time.time()
                if events_cb is not None:
                    try:
                        with deps.console.status(_status_msg, spinner="dots"):
                            result = tools_mod.get_registry().invoke(tool_name, params, ctx)
                    except Exception:
                        result = tools_mod.get_registry().invoke(tool_name, params, ctx)
                else:
                    result = tools_mod.get_registry().invoke(tool_name, params, ctx)
                _dur_ms = int((time.time() - _t0) * 1000)
                truncate = int(get_runtime_config("output_truncate") or 3000)
                pretty_for_ai = _format_tool_result_for_loop(tool_name, result, truncate)
                # Keep the full structured result for the debug log so devs
                # can inspect what the tool actually returned.
                try:
                    pretty_raw = json.dumps(result, ensure_ascii=False, indent=2, default=str)
                except (TypeError, ValueError):
                    pretty_raw = str(result)
                state["lastOutput"] = pretty_for_ai
                if events_cb is not None:
                    ok_mark = "[green]✓[/green]" if result.get("ok") else "[red]✗[/red]"
                    _meta = ""
                    if isinstance(result.get("result"), dict):
                        _r = result["result"]
                        if "lines_returned" in _r:
                            _meta = f" ({_r['lines_returned']} lines)"
                        elif "matches" in _r:
                            _meta = f" ({len(_r.get('matches') or [])} matches)"
                    deps.console.print(
                        f"  {ok_mark} [dim cyan]{tool_name}[/dim cyan] [dim]{_hint}{_meta} · {_dur_ms}ms[/dim]")
                    pending_events.append({"type": "system", "kind": "tool",
                                            "content": tool_name,
                                            "meta": {"params": raw_params[:500],
                                                     "ok": result.get("ok", False)}})
                    pending_events.append({"type": "system", "kind": "output",
                                            "content": pretty_for_ai[:2000]})
                    events_cb(pending_events)
                    pending_events.clear()
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0 if result.get("ok") else -1
                debug_entry.exec_stdout = pretty_raw[:2000]

            # ── Meta-command: /term <name> (create sub-terminal, no agent) ─
            elif (_meta_term := re.match(r'^/term\s+(\S+)\s*$', command)):
                name = _meta_term.group(1)
                existing = get_terminal(name)
                if existing and existing.session and not existing.session.is_alive():
                    unregister_terminal(name)
                    existing = None
                if existing is not None:
                    state["lastOutput"] = f"Terminal '{name}' already exists."
                else:
                    lain_cmd = f"{sys.executable} {_LAINTAS_CLI} --simple-prompt"
                    sub = deps.SubTerminalSession(lain_cmd)
                    sub.start()
                    time.sleep(0.3)
                    if sub.is_alive():
                        sub.read_output(timeout=0.3)
                    register_terminal(sub, "laintas-cli", depth, name=name)
                    state["lastOutput"] = f"Created sub-terminal {name} (no agent stationed)"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "command", "content": command})
                    events_cb(pending_events)
                    pending_events.clear()

            # ── Meta-command: /term, /t (list terminals) ───────────────
            elif re.match(r'^/(?:term|t)\s*$', command):
                state["lastOutput"] = get_terminals_snapshot() or "(no sub-terminals)"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "command", "content": command})
                    events_cb(pending_events)
                    pending_events.clear()

            # ── Meta-command: wait(N) / sleep(N) ────────────────────────
            elif (_meta_wait := re.match(r'^(?:wait|sleep)\((\d+(?:\.\d+)?)\)\s*$', command)):
                duration = float(_meta_wait.group(1))
                deps.console.print(f"[dim]⏳ {duration}s ...[/dim]")
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "command", "content": command})
                time.sleep(duration)
                # Update state with latest sub-terminal output if any
                if interactive_session is not None and interactive_session.is_alive():
                    state["lastOutput"] = interactive_session.full_output
                else:
                    state["lastOutput"] = f"Waited {duration}s"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
            else:
                # ── Normal command: stationed or sub-terminal ──
                current_agent = get_current_agent()
                stationed_name = current_agent.stationed_terminal if current_agent else None
                stationed_term = get_terminal(stationed_name) if stationed_name else None

                # ── Hooks: pre_command (can block execution) ──────────────────
                hook_allowed, hook_msgs = hooks_mod.trigger("pre_command", {
                    "command": command,
                    "depth": depth,
                    "agent_id": agent_id,
                    "stationed": bool(stationed_term and stationed_term.session and stationed_term.session.is_alive()),
                })
                if not hook_allowed:
                    state["lastOutput"] = "BLOCKED by pre_command hook"
                    debug_entry.exec_command = command
                    debug_entry.exec_returncode = -1
                    debug_entry.exec_stdout = state["lastOutput"]

                # ── Security policy check (applies to both stationed and unstationed) ──
                policy_ok, policy_reason, policy_approval = _check_policy(
                    command, agent_id=agent_id, events_cb=events_cb, deps=deps)
                if not policy_ok:
                    state["lastOutput"] = f"BLOCKED: {policy_reason}"
                    debug_entry.exec_command = command
                    debug_entry.exec_returncode = -1
                    debug_entry.exec_stdout = state["lastOutput"]
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "output",
                                               "content": state["lastOutput"][:2000]})
                        events_cb(pending_events)
                        pending_events.clear()
                elif policy_approval:
                    state["shortTermMemory"] += f"\n  ⚠ Policy: {policy_reason}"

                if not hook_allowed or not policy_ok:
                    pass  # skip execution, let state update + debug log handle it
                elif stationed_term and depth == 0:
                    # ── Stationed at depth 0: execute in the parent terminal,
                    # exactly like user-typed input. cd/clear must stay in
                    # _execute_parent_command (pty_passthrough runs them in a
                    # child shell, so cwd changes wouldn't stick).
                    if events_cb is not None:
                        deps.console.print(f"[dim]> [{stationed_name}] {command[:80]}[/dim]")
                        pending_events.append({"type": "system", "kind": "command", "content": command})

                    stripped = command.strip()
                    is_parent_state = (stripped == "cd" or stripped.startswith("cd ")
                                       or stripped == "clear" or stripped.startswith("clear "))
                    if is_parent_state or deps.pty_passthrough is None:
                        result = _execute_parent_command(command)
                        returncode = 0
                    else:
                        pt_result = deps.pty_passthrough(command)
                        result = pt_result.get("stdout", "") or ""
                        returncode = pt_result.get("returncode", 0)

                    state["lastOutput"] = result
                    debug_entry.exec_command = command
                    debug_entry.exec_returncode = returncode

                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "output", "content": result[:2000]})
                        events_cb(pending_events)
                        pending_events.clear()

                elif stationed_term and stationed_term.session and stationed_term.session.is_alive():
                    # ── Stationed at depth > 0: send_keys + marker in persistent shell ──
                    if events_cb is not None:
                        deps.console.print(f"[dim]> [{stationed_name}] {command[:80]}[/dim]")
                        pending_events.append({"type": "system", "kind": "command", "content": command})

                    session = stationed_term.session
                    marker_id = uuid.uuid4().hex[:8]
                    start_marker = f"__CMD_BEGIN_{marker_id}__"
                    end_marker = f"__CMD_END_{marker_id}__"

                    # Wrap command: capture stderr and return code between markers
                    wrapped = f"echo {start_marker}; {command} 2>&1; __laintas_rc=$?; echo {end_marker}:$__laintas_rc"

                    # Track output length before sending
                    try:
                        old_len = len(session.raw_output)
                    except Exception:
                        old_len = len(session.full_output)

                    session.send_keys(wrapped + "\n")

                    # Poll for end marker
                    cmd_output = ""
                    returncode = -1
                    poll_start = time.time()
                    poll_timeout = float(get_runtime_config("poll_timeout"))

                    while time.time() - poll_start < poll_timeout:
                        time.sleep(0.3)
                        session.read_output(timeout=0.3)

                        try:
                            raw = session.raw_output
                        except Exception:
                            raw = session.full_output

                        new_content = raw[old_len:] if old_len > 0 else raw

                        # Look for end marker with return code (must be on its own line)
                        end_match = re.search(
                            rf'(?:^|\r?\n){re.escape(end_marker)}:(\d+)',
                            new_content, re.MULTILINE
                        )
                        if end_match:
                            returncode = int(end_match.group(1))
                            # Extract output between start marker and end marker
                            start_match = re.search(
                                rf'(?:^|\r?\n){re.escape(start_marker)}\s*\r?\n',
                                new_content, re.MULTILINE
                            )
                            if start_match:
                                cmd_output = new_content[start_match.end():end_match.start()].strip()
                            else:
                                # Fallback: split on marker text
                                parts = new_content.split(start_marker, 1)
                                if len(parts) > 1:
                                    cmd_output = parts[1].split(end_marker, 1)[0].strip()
                            break

                        if not session.is_alive():
                            try:
                                cmd_output = session.raw_output[old_len:] if old_len > 0 else session.raw_output
                            except Exception:
                                cmd_output = session.full_output[old_len:] if old_len > 0 else session.full_output
                            break

                    if not cmd_output:
                        # Timeout — command may still be running (e.g., claude, vim)
                        try:
                            tail = session.raw_output[old_len:] if old_len > 0 else session.raw_output
                        except Exception:
                            tail = session.full_output[old_len:] if old_len > 0 else session.full_output
                        cmd_output = tail

                    clean_output = deps.strip_ansi(cmd_output).strip()
                    state["lastOutput"] = clean_output or "(command running...)"
                    debug_entry.exec_command = command
                    debug_entry.session_command = command
                    debug_entry.exec_stdout = deps.strip_ansi(cmd_output)
                    debug_entry.exec_returncode = returncode

                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "output", "content": state["lastOutput"][:2000]})
                        events_cb(pending_events)
                        pending_events.clear()

                else:
                    # ── Unstationed: spawn one-off sub-terminal ──
                    if interactive_session is not None:
                        if events_cb is not None:
                            deps.console.print(f"[dim yellow]Closing previous session: {interactive_session.command[:60]}[/dim yellow]")
                            pending_events.append({"type": "system", "kind": "close_session", "content": interactive_session.command[:200]})
                        interactive_session.close()
                        interactive_session = None

                    if events_cb is not None:
                        _sub_msg = f"[dim]→ {command[:80]} [sub-terminal][/dim]"
                        deps.console.print(_sub_msg)
                        pending_events.append({"type": "system", "kind": "command", "content": command})

                    if depth == 0:
                        terminal_cmd = f"{sys.executable} {_LAINTAS_CLI} --execute {shlex.quote(command)} --depth 1"
                    else:
                        terminal_cmd = command

                    sub = deps.SubTerminalSession(terminal_cmd)
                    sub.start()
                    interactive_session = sub

                    cmd_output = ""
                    poll_start = time.time()
                    while time.time() - poll_start < float(get_runtime_config("poll_timeout")):
                        time.sleep(0.3)
                        chunk = sub.read_output(timeout=0.3)
                        if chunk:
                            cmd_output = sub.full_output
                            if len(cmd_output.strip()) > 50:
                                break
                        if not sub.is_alive():
                            cmd_output = sub.full_output
                            break

                    cleaned, parent_result = _process_parent_cmd_marker(cmd_output)
                    cmd_output = deps.strip_ansi(cleaned)
                    state["lastOutput"] = parent_result if parent_result else (cmd_output.strip() or "(no output)")
                    debug_entry.exec_command = command
                    debug_entry.session_command = command
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = sub.returncode if sub.returncode is not None else -1

                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                        events_cb(pending_events)
                        pending_events.clear()

                    if events_cb is not None:
                        deps.display_sub_terminal_preview(command, cmd_output, depth=depth + 1, alive=sub.is_alive())

        # 10. Update state
        action_desc = command if command else ""
        state["shortTermMemory"] += \
            f"\n  Step {loop+1}: {reply} | cmd: {action_desc} | result: {state['lastOutput'][:200]}"
        state["terminalHistory"].append({
            "command": action_desc,
            "output": state['lastOutput'],
            "returncode": getattr(debug_entry, "exec_returncode", None),
        })

        # Track files the AI has read/touched so the prompt can surface them
        # and the AI doesn't re-read needlessly.
        if action_desc:
            seen = state.setdefault("_files_seen", [])
            _track_files_in_command(action_desc, seen)

        # ── Error analysis: detect patterns + suggest recovery ──
        last_output = state.get("lastOutput", "")
        last_rc = debug_entry.exec_returncode if hasattr(debug_entry, 'exec_returncode') else -1
        error_info = _analyze_error(last_output, last_rc)
        if error_info["category"] != "none":
            state["shortTermMemory"] += \
                f"\n  🔍 Error detected [{error_info['category']}]: {error_info['suggestion']}"
            # ── Hooks: on_error ──
            hooks_mod.trigger("on_error", {
                "command": action_desc,
                "output": last_output[:500],
                "returncode": last_rc,
                "category": error_info["category"],
                "loop": loop + 1,
            })
            if error_info["retryable"] and state.get("_retry_count", 0) < _MAX_RETRIES:
                state["_retry_count"] = state.get("_retry_count", 0) + 1
                state["shortTermMemory"] += \
                    f" (auto-retry {state['_retry_count']}/{_MAX_RETRIES})"
        else:
            state["_retry_count"] = 0  # reset on success

        # ── Consecutive failure warning ──
        fail_hint = _maybe_retry_suggestion(state)
        if fail_hint:
            state["shortTermMemory"] += fail_hint

        # ── Hooks: post_command (after each execution step) ──
        if action_desc:
            hooks_mod.trigger("post_command", {
                "command": action_desc,
                "output": state.get("lastOutput", "")[:1000],
                "returncode": debug_entry.exec_returncode if hasattr(debug_entry, 'exec_returncode') else -1,
                "loop": loop + 1,
                "done": done,
            })

        # ── Debug: persist this loop's entry ──
        add_debug_log(debug_entry)

        if done:
            # Close sub-terminal if still running, show final report
            if interactive_session is not None:
                cmd_output = interactive_session.full_output
                debug_entry.exec_stdout = cmd_output
                debug_entry.exec_returncode = interactive_session.returncode
                state["lastOutput"] = cmd_output
                interactive_session.close()
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                    deps.display_command_output(
                        interactive_session.command,
                        interactive_session.returncode,
                        cmd_output,
                        depth=depth + 1,
                    )
                interactive_session = None
            if events_cb is not None and pending_events:
                events_cb(pending_events)
                pending_events.clear()
            break

        # ── Staleness tracking: auto-exit when AI stops producing output ──
        # Count steps where the AI produced NO reply AND NO command as idle.
        # A conversational reply (text without a command) is real work and
        # resets the counter, same as a command would.
        if not command and not reply:
            stale_count += 1
            if stale_count >= staleness_limit:
                if events_cb is not None and deps is not None:
                    deps.console.print(f"[dim]Task appears complete ({stale_count} idle steps). Exiting.[/dim]")
                    # Show raw response on idle exit so users can diagnose backend issues
                    if not command and not reply:
                        raw = debug_entry.response_raw
                        if raw:
                            deps.console.print(f"[dim yellow]Last backend response: {json.dumps(raw, ensure_ascii=False, default=str)[:500]}[/dim yellow]")
                break
        else:
            stale_count = 0  # reset on any output (command or conversational reply)

        # 11. Delay between steps (interruptible)
        if loop < max_loops - 1:
            try:
                time.sleep(float(get_runtime_config("loop_delay")))
            except KeyboardInterrupt:
                deps.console.print("\n[yellow]Agent loop interrupted.[/yellow]")
                break

        # 12. Prepare next input — rebuild via the structured-section helper.
        memory_entries = _read_memory(deps)  # re-read in case AI wrote memory
        user_input = _build_user_message(
            original_input, state, memory_entries, chat_history, loop, max_loops,
        )

    # Clean up session only when NOT managed by REPL (existing_session=None)
    # When REPL manages the session, it handles lifecycle externally.
    if existing_session is None and interactive_session is not None:
        interactive_session.close()

    if step_replies:
        return {"success": done, "msg": "\n\n".join(step_replies), "state": state,
                "session": interactive_session}
    return {"success": done, "msg": reply, "state": state,
            "session": interactive_session}
