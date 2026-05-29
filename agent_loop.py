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
import agent_persistence     # Cross-session agent state persistence
import agent_roles           # Specialized agent roles (explorer, reviewer, etc.)
import workflow_engine        # Structured multi-phase workflow engine
import task_manager          # Structured task tracking (session + persisted)
import paths                 # Centralized path management

# Path to laintas_cli.py for spawning child terminals
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LAINTAS_CLI = os.path.join(_SCRIPT_DIR, "laintas_cli.py")


# ── Constants ──────────────────────────────────────────────────────────
MAX_LOOPS = 10
MAX_TOKENS = 8192
MAX_DEBUG_ENTRIES = 50

# Mutable defaults — these are the "factory" values; runtime overrides stored in _runtime_config
_DEFAULT_CONFIG = {
    "max_loops": 30,
    "max_tokens": 8192,
    "max_debug_entries": 50,
    "loop_delay": 1.5,           # seconds between loop iterations
    "output_truncate": 3000,      # chars — lastOutput tail truncation
    "poll_timeout": 10.0,         # seconds — wait for first command output
    "terminal_tail_lines": 20,    # lines — sub-terminal snapshot
    "heartbeat_interval": 30,     # seconds — agent heartbeat
    "staleness_limit": 3,         # consecutive no-tool steps before auto-exit
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
    if info.session is not None:
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
    # ── Pool architecture fields ───────────────────────────────────────
    role: str = "pool"                        # pool | deployed | primary | subagent
    parent_terminal: Optional[str] = None     # terminal that spawned this agent
    home_terminal: Optional[str] = None       # terminal this agent is deployed to

_agent_registry: dict[str, AgentInfo] = {}
_agent_counter: int = 0
_current_agent_id: Optional[str] = None
# Phase 2: protect registry + parent/child link mutations from concurrent
# spawn/abort/send across threads. RLock so a method that takes the lock
# may call another locked method without deadlocking.
_registry_lock = threading.RLock()


def register_agent(name: str = None, depth: int = 0,
                   parent_id: Optional[str] = None,
                   role: str = "pool",
                   load_existing: bool = False) -> AgentInfo:
    """Create and register a new AI agent. Returns the AgentInfo.

    If load_existing=True and a persisted state file exists for the given
    name, restore chat_history/state/role from disk so the agent picks up
    where it left off.
    """
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
            role=role,
        )
        if load_existing:
            data = agent_persistence.load_agent_state(agent_id)
            if data is not None:
                agent_persistence.apply_persisted_state(info, data)
                # Caller-supplied role wins if explicitly different from "pool"
                if role and role != "pool":
                    info.role = role
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


def get_pool_agents() -> list:
    """Return idle pool agents (role='pool', no home_terminal)."""
    with _registry_lock:
        return sorted(
            (a for a in _agent_registry.values()
             if getattr(a, "role", "pool") == "pool"
             and not getattr(a, "home_terminal", None)),
            key=lambda a: a.created_at,
        )


def get_deployed_agents() -> list:
    """Return all deployed agents (role='deployed')."""
    with _registry_lock:
        return sorted(
            (a for a in _agent_registry.values()
             if getattr(a, "role", "pool") == "deployed"),
            key=lambda a: a.created_at,
        )


def get_or_hire_pool_agent() -> AgentInfo:
    """Return the first available pool agent; auto-hire one if pool is empty."""
    pool = get_pool_agents()
    if pool:
        return pool[0]
    return register_agent(depth=0, role="pool")


def _format_deployment(a: Optional["AgentInfo"]) -> str:
    """Human-readable deployment status for prompts and /agents output."""
    if a is None:
        return "unknown"
    role = getattr(a, "role", "pool")
    home = getattr(a, "home_terminal", None) or a.stationed_terminal
    if role == "primary":
        return "primary"
    if role == "deployed":
        return f"deployed→{home or '?'}"
    if role == "pool":
        return "pool (idle)"
    if role == "subagent":
        return f"subagent (depth {a.depth})"
    return role


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
        agent.home_terminal = terminal_name
        if agent.role != "primary":
            agent.role = "deployed"
        if term:
            if agent_id not in term.stationed_agent_ids:
                term.stationed_agent_ids.append(agent_id)
            term.stationed_agent_id = term.stationed_agent_ids[0]  # keep first as legacy
    try:
        agent_persistence.save_agent_state(agent)
    except Exception:
        pass
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
            agent.home_terminal = None
            if agent.role == "deployed":
                agent.role = "pool"
                agent.status = "idle"
        else:
            agent = None
    if agent is not None:
        try:
            agent_persistence.save_agent_state(agent)
        except Exception:
            pass


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
                   events_cb=None,
                   role: Optional[str] = None) -> Optional[str]:
    """Start an in-process child agent in its own thread.

    The child:
      - inherits depth = parent.depth + 1
      - has its own chat_history, state, inbox, abort_event
      - reports back to the parent via a 'child-done' / 'child-error' message
        delivered to the parent's inbox

    If `role` is provided (e.g. "explorer", "reviewer"), the child agent
    gets a specialized system prompt and tool whitelist via AgentRole.

    Returns the child's agent_id, or None if the parent doesn't exist.
    """
    parent = get_agent(parent_id)
    if parent is None:
        return None

    # Auto-generate name from role if not provided
    if not name and role:
        role_instance = agent_roles.get_role(role)
        name = f"{role}-{parent.depth + 1}-{_agent_counter + 1}" if role_instance else name

    child = register_agent(name=name, depth=parent.depth + 1,
                           parent_id=parent_id, role="subagent")
    child.parent_terminal = (
        getattr(parent, "home_terminal", None)
        or getattr(parent, "parent_terminal", None)
        or "term0"
    )
    child.status = "running"

    # Inject role into child state so run_agent_loop picks it up
    if role:
        child.state["_role_name"] = role
        role_obj = agent_roles.get_role(role)
        if role_obj:
            # Prepend role description to the task for context
            task = (
                f"[Role: {role_obj.name} — {role_obj.description}]\n\n"
                f"{task}"
            )

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
                "role": role or "general",
                "summary": child.last_reply or "(no reply)",
            })
        except Exception as e:
            child.status = "error"
            send_to_agent(parent_id, {
                "from": child.id,
                "kind": "child-error",
                "role": role or "general",
                "error": repr(e),
            })

    t = threading.Thread(target=_runner, daemon=True,
                         name=f"laintas-agent-{child.id}")
    child.thread = t
    t.start()
    return child.id


def spawn_subagents_parallel(parent_id: str, tasks: list[dict], deps,
                              session: Optional[dict] = None,
                              events_cb=None) -> list[str]:
    """Start multiple sub-agents in parallel.

    tasks: [{"task": "...", "role": "explorer", "name": "explorer-1"}, ...]
    Returns list of child agent IDs.
    """
    child_ids = []
    for t in tasks:
        cid = spawn_subagent(
            parent_id=parent_id,
            task=t.get("task", ""),
            deps=deps,
            name=t.get("name"),
            session=session,
            events_cb=events_cb,
            role=t.get("role"),
        )
        if cid:
            child_ids.append(cid)
    return child_ids


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
    build_subterminal_cmd: Optional[Callable[..., str]] = None


# ── Structured Memory System (.laintas/memory.json) ───────────────────────
# Project memory stores a JSON array of entries: [{"id": N, "content": "...", "created": "...", "updated": "..."}]
# AI reads/writes these via the mem.* tools (mem.read, mem.save, mem.delete, mem.list).

_MEMORY_FILE = ".laintas/memory.json"


def _read_memory(deps: LoopDeps) -> list[dict]:
    """Read and parse .laintas/memory.json as a JSON array of entries. Returns [] on failure."""
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

    Enhanced behavior diagnostics — surfaces the AI's own patterns so it can
    self-correct. Returns list of warning strings for <warnings> block.

    Checks:
    1. Same exact command 3+ consecutive times
    2. 3+ consecutive failures
    3. 7+ read-only steps without edit/build/run
    4. Exploration drift: 5+ steps with no file writes AND no tool variety
    5. Tool stagnation: same tool 5+ consecutive times with similar args
    6. Context amnesia: re-reading files already in _files_seen
    7. Goal drift: shortTermMemory mentions actions unrelated to original task
    """
    history = state.get("terminalHistory", [])
    warnings: list[str] = []

    if len(history) < 3:
        return warnings

    # 1. Same exact command 3+ consecutive times
    last_cmds = [(h.get("command") or "").strip() for h in history[-3:]]
    if last_cmds[0] and last_cmds[0] == last_cmds[1] == last_cmds[2]:
        warnings.append(
            f"You have run `{last_cmds[0][:80]}` 3 times in a row with the same result. "
            f"The task is done. Return tool_calls: [] and state your final answer in reply."
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
    WRITE_TOOLS = {"fs.write", "fs.edit", "fs.multi_edit", "shell.exec"}
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

    # 4. Exploration drift: 5+ steps with no file writes AND low tool variety
    if len(history) >= 5:
        last5 = history[-5:]
        has_write = False
        tool_names = set()
        for h in last5:
            tool = h.get("tool", "")
            cmd = (h.get("command") or "").strip()
            if tool in WRITE_TOOLS or any(cmd.startswith(f"/tool {w}") for w in WRITE_TOOLS):
                has_write = True
            if tool:
                tool_names.add(tool)
        if not has_write and len(tool_names) <= 2 and len(history) >= 5:
            warnings.append(
                f"Exploration drift: {len(history)} steps with no writes and only "
                f"{len(tool_names)} tool type(s). Broaden your approach or start "
                f"making changes based on what you've learned."
            )

    # 5. Tool stagnation: same tool 5+ consecutive times with similar args
    if len(history) >= 5:
        last5_tools = [(h.get("tool", ""), (h.get("command") or "")[:60]) for h in history[-5:]]
        if (all(t[0] == last5_tools[0][0] for t in last5_tools)
                and last5_tools[0][0]
                and len(set(t[1] for t in last5_tools)) <= 2):
            warnings.append(
                f"Tool stagnation: you've used `{last5_tools[0][0]}` 5 times "
                f"with very similar arguments. Try a different tool or approach."
            )

    # 6. Context amnesia: re-reading files already in _files_seen
    files_seen = state.get("_files_seen", [])
    if files_seen and len(history) >= 2:
        last_entry = history[-1]
        cmd = (last_entry.get("command") or "").strip()
        # Check if this is a read of a known file
        for fp in files_seen[-20:]:  # check recent files
            if fp and fp in cmd and any(cmd.startswith(p) for p in
                    ("fs.read", "/tool fs.read", "cat ", "head ", "tail ")):
                warnings.append(
                    f"Context amnesia: you're re-reading `{fp}` which you already "
                    f"examined earlier. Refer to your previous output instead of "
                    f"re-reading."
                )
                break

    # 7. Goal drift: shortTermMemory mentions actions unrelated to task
    if original_input and len(history) >= 10:
        task_keywords = set(re.findall(r'\w+', original_input.lower()))
        task_keywords -= {"the", "a", "an", "is", "to", "and", "or", "in", "of",
                          "for", "with", "on", "at", "by", "it", "this", "that"}
        if len(task_keywords) >= 3:
            recent_memory = state.get("shortTermMemory", "")[-500:]
            memory_words = set(re.findall(r'\w+', recent_memory.lower()))
            overlap = task_keywords & memory_words
            # If less than 20% of task keywords appear in recent memory, possible drift
            if len(overlap) < max(2, len(task_keywords) * 0.2):
                warnings.append(
                    f"Possible goal drift: recent actions seem unrelated to the "
                    f"original task '{original_input[:60]}'. "
                    f"Refocus on the original objective."
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

    Enhanced with:
    - <workflow_phase> section (when a workflow is active)
    - <behavior_diagnostics> section (enhanced loop warnings)
    - <role_identity> section (for sub-agents with specialized roles)
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

    # Workflow phase section
    workflow_block = ""
    wf = workflow_engine.get_active_workflow()
    if wf and not wf.completed:
        current = wf.current
        if current:
            workflow_block = (
                f"\n<workflow_phase>\n"
                f"  workflow: {wf.name} — {wf.description}\n"
                f"  progress: {wf.progress_str}\n"
                f"  current: {current.name} — {current.description}\n"
            )
            # Show auto-spawn hint if phase has spawn_agents
            if current.spawn_agents:
                workflow_block += f"  hint: consider spawning {', '.join(current.spawn_agents)} agent(s)\n"
            if current.requires_user_input:
                workflow_block += "  ⚠️  This phase requires user confirmation before advancing.\n"
            workflow_block += "</workflow_phase>\n"

    # Role identity section (for sub-agents)
    role_block = ""
    role_name = state.get("_role_name")
    if role_name:
        role_obj = agent_roles.get_role(role_name)
        if role_obj:
            role_block = (
                f"\n<role_identity>\n"
                f"  You are operating as: {role_obj.name} ({role_obj.description})\n"
            )
            if role_obj.allowed_tools:
                role_block += f"  Allowed tools: {', '.join(role_obj.allowed_tools)}\n"
            if role_obj.confidence_threshold > 0:
                role_block += f"  Confidence threshold: only report findings >= {role_obj.confidence_threshold}/100\n"
            role_block += "</role_identity>\n"

    # Active tasks section
    tasks_snapshot = task_manager.get_active_tasks_snapshot()
    tasks_block = ""
    if tasks_snapshot:
        tasks_block = f"\n<active_tasks>\n{tasks_snapshot}\n</active_tasks>\n"

    return f"""<task>
{original_input}
</task>

<progress>
step {loop+1}/{max_loops} — {n_steps} command(s) executed so far
</progress>
{warnings_block}{files_block}{workflow_block}{role_block}{tasks_block}
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
    """Clear .laintas/loop.py cache so it reloads on next use."""
    global _loop_cmd_handler_cache, _loop_cmd_mtime_cache
    _loop_cmd_handler_cache = None
    _loop_cmd_mtime_cache = 0


def _load_loop_commands():
    """Load .laintas/loop.py and return handle_loop_command() if defined."""
    global _loop_cmd_handler_cache, _loop_cmd_mtime_cache
    try:
        path = str(paths.project_file(paths.CWD_LOOP))
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
    """Execute a command in the parent process context.

    Prefer term0's persistent bash session (marker-poll) so cd, export,
    aliases all persist. Fall back to subprocess.run if term0 is dead.
    This function is only called from _process_parent_cmd_marker for the
    parent() loop command at depth 0.
    """
    # Try term0's marker-poll path
    term0_info = get_terminal("term0")
    if (term0_info and term0_info.session
            and getattr(term0_info.session, 'is_alive', lambda: False)()):
        try:
            return _marker_poll_simple(term0_info.session, cmd)
        except Exception:
            pass
    # Fallback: subprocess.run (cd won't persist, but it's a last resort)
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


def _marker_poll_simple(session, command: str, timeout: float = 30) -> str:
    """Run a command through a persistent bash via marker-poll. Returns output string.

    Lightweight version of tools.py's _bi_shell_exec marker-poll.
    Also syncs CWD to the parent process after execution.
    """
    import uuid as _uuid
    import re as _re

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

    while time.time() - poll_start < timeout:
        time.sleep(0.08)
        session.read_output(timeout=0.1)
        try:
            raw = session.raw_output
        except AttributeError:
            raw = session.full_output
        new_content = raw[old_len:] if old_len > 0 else raw

        end_match = _re.search(rf'{_re.escape(end_marker)}:(\d+)', new_content)
        if end_match:
            starts = list(_re.finditer(
                rf'{_re.escape(start_marker)}(?=[\r\n]|$)', new_content))
            if starts:
                valid = [m for m in starts if m.end() < end_match.start()]
                chosen = valid[-1] if valid else starts[-1]
                body_start = chosen.end()
                while body_start < len(new_content) and new_content[body_start] in '\r\n':
                    body_start += 1
                cmd_output = new_content[body_start:end_match.start()]
                cmd_output = cmd_output.rstrip('\r\n').strip()
            # Sync CWD after command
            try:
                _sync_cwd_from_session(session)
            except Exception:
                pass
            return cmd_output or "(no output)"
        if not session.is_alive():
            break

    return cmd_output or "(no output)"


def _sync_cwd_from_session(session) -> None:
    """Sync parent process CWD from a persistent bash session via marker-poll pwd."""
    import uuid as _uuid
    import re as _re

    marker_id = _uuid.uuid4().hex[:8]
    start_marker = f"__CMD_BEGIN_{marker_id}__"
    end_marker = f"__CMD_END_{marker_id}__"
    wrapped = f"echo {start_marker}; pwd; echo {end_marker}"

    try:
        old_len = len(session.raw_output)
    except AttributeError:
        old_len = len(session.full_output)

    session.send_keys(wrapped + "\n")
    poll_start = time.time()

    while time.time() - poll_start < 2.0:
        time.sleep(0.1)
        session.read_output(timeout=0.1)
        try:
            raw = session.raw_output
        except AttributeError:
            raw = session.full_output
        new_content = raw[old_len:] if old_len > 0 else raw

        end_match = _re.search(rf'{_re.escape(end_marker)}', new_content)
        if end_match:
            starts = list(_re.finditer(
                rf'{_re.escape(start_marker)}(?=[\r\n]|$)', new_content))
            if starts:
                valid = [m for m in starts if m.end() < end_match.start()]
                chosen = valid[-1] if valid else starts[-1]
                body_start = chosen.end()
                while body_start < len(new_content) and new_content[body_start] in '\r\n':
                    body_start += 1
                pwd_result = new_content[body_start:end_match.start()].strip().rstrip('\r\n')
                if pwd_result and os.path.isdir(pwd_result) and pwd_result != os.getcwd():
                    os.chdir(pwd_result)
            break
        if not session.is_alive():
            break


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


def _salient_arg(name: str, arguments: dict) -> str:
    """Pick the most user-meaningful argument from a tool call.

    Used for: terminalHistory entry labels (so file-tracking regex matches),
    REPL streaming preview (so the user sees `git diff` not `shell.exec`),
    and per-call hook context.
    """
    if not isinstance(arguments, dict):
        return name
    if name == "shell.exec":
        return arguments.get("command", "") or ""
    if name == "terminal.send":
        return f'{arguments.get("name", "?")}: {arguments.get("command", "")}'
    if name in ("fs.read", "fs.write", "fs.edit", "fs.multi_edit", "fs.diff"):
        return arguments.get("path", "") or ""
    if name == "fs.grep":
        return f'{arguments.get("pattern", "")} in {arguments.get("path", "")}'
    if name == "fs.glob":
        return arguments.get("pattern", "") or ""
    if name == "fs.ls":
        return arguments.get("path", ".") or "."
    if name == "agent.spawn":
        return (arguments.get("task") or "")[:80]
    if name == "agent.tell":
        return f'{arguments.get("agent_id", "?")}: {(arguments.get("message") or "")[:60]}'
    if name in ("agent.wait", "agent.abort", "agent.switch"):
        return arguments.get("agent_id", "") or ""
    if name in ("web.fetch",):
        return arguments.get("url", "") or ""
    if name == "web.search":
        return arguments.get("query", "") or ""
    if name == "mem.save":
        return (arguments.get("content") or arguments.get("name", ""))[:80]
    if name == "sleep":
        return f'{arguments.get("seconds", 1)}s'
    try:
        return f'{name} {json.dumps(arguments, ensure_ascii=False)[:60]}'
    except (TypeError, ValueError):
        return name


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


def _render_tool_catalog(state: dict, loop: int) -> str:
    """Full catalog on turn 1 (or after an unknown-tool error); short reminder
    afterwards. Saves ~5KB × max_loops per task on follow-up turns."""
    if loop == 0 or state.get("_force_full_catalog_next"):
        state["_force_full_catalog_next"] = False
        return tools_mod.get_registry().describe_for_prompt()
    return tools_mod.get_registry().describe_short_reminder()


def _render_tool_catalog_enhanced(state: dict, loop: int, depth: int = 0) -> str:
    """Layered tool catalog rendering:
    - Tools allowed by current workflow phase / role: full description
    - Other tools: name only
    - Role catalog appended when at depth > 0

    Falls back to the standard catalog when no workflow/role is active.
    """
    role_name = state.get("_role_name")
    wf_active = workflow_engine.get_active_workflow() is not None

    # If neither workflow nor role is active, use the standard catalog
    if not wf_active and not role_name:
        base = _render_tool_catalog(state, loop)
        # Append role catalog for sub-agents
        if depth > 0:
            role_section = agent_roles.describe_roles_for_prompt()
            if role_section:
                base += f"\n\n{role_section}"
        return base

    # Build layered rendering
    registry = tools_mod.get_registry()
    all_tools = registry.list()

    # Determine allowed tools from workflow + role
    allowed = None  # None means all allowed
    if wf_active:
        current_phase = workflow_engine.get_active_workflow().current
        if current_phase and current_phase.allowed_tools:
            allowed = set(current_phase.allowed_tools)
    if role_name:
        role = agent_roles.get_role(role_name)
        if role and role.allowed_tools:
            role_tools = set(role.allowed_tools)
            allowed = role_tools if allowed is None else allowed & role_tools

    if allowed is None:
        # No restrictions — full catalog
        base = _render_tool_catalog(state, loop)
    else:
        # Layered: allowed tools get full description, others get name-only
        lines = []
        if loop == 0 or state.get("_force_full_catalog_next"):
            state["_force_full_catalog_next"] = False
            allowed_lines = []
            other_lines = []
            for t in all_tools:
                if t.name in allowed:
                    allowed_lines.append(f"  {t.name}: {t.description[:120]}")
                else:
                    other_lines.append(t.name)
            lines.append("=== ALLOWED TOOLS (this phase/role) ===")
            lines.extend(allowed_lines)
            if other_lines:
                lines.append(f"\n=== OTHER TOOLS (blocked) ===")
                lines.append(f"  {', '.join(other_lines[:30])}")
        else:
            allowed_names = [t.name for t in all_tools if t.name in allowed]
            lines.append(f"Allowed tools: {', '.join(allowed_names)}")
        base = "\n".join(lines)

    # Append role catalog for sub-agents
    if depth > 0:
        role_section = agent_roles.describe_roles_for_prompt()
        if role_section:
            base += f"\n\n{role_section}"

    return base


def _format_parallel_results(inbox_msgs: list) -> str:
    """Aggregate child-done / child-error messages into a structured block.

    Returns a formatted string for {{parallelResults}}, or empty if none.
    """
    if not inbox_msgs:
        return ""

    results = []
    for msg in inbox_msgs:
        if not isinstance(msg, dict):
            continue
        kind = msg.get("kind", "")
        if kind not in ("child-done", "child-error"):
            continue
        from_agent = msg.get("from", "unknown")
        status = msg.get("status", "unknown")
        if kind == "child-done":
            summary = msg.get("summary", "(no summary)")
            results.append(
                f"[{from_agent}] ✅ {status}\n{summary[:500]}"
            )
        else:
            error = msg.get("error", "(no error)")
            results.append(
                f"[{from_agent}] ❌ error: {error[:300]}"
            )

    if not results:
        return ""

    header = f"## Sub-Agent Results ({len(results)} agent(s) completed)"
    return f"{header}\n\n" + "\n\n---\n\n".join(results)


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
    # Workflow phase may override max_loops (e.g. implementation phase gets more)
    _wf_max = workflow_engine.get_phase_max_loops()
    if _wf_max > 0:
        max_loops = _wf_max
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

        # 1. Read .laintas/memory.json (project memory)
        memory_entries = _read_memory(deps)

        # 2. Build global memory string for system prompt
        if memory_entries:
            global_memory_lines = []
            for e in memory_entries:
                global_memory_lines.append(f"[{e['id']}] {e['content']}")
            global_memory_str = '\n'.join(global_memory_lines)
        else:
            global_memory_str = "(empty)"

        # 3. Read .laintas/cli.prop system prompt
        prompt_template = deps.read_file(str(paths.project_file(paths.CWD_CLI_PROP))) or ""
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

        # Terminal / deployment context
        terminal_name_str = (
            getattr(current_agent, "home_terminal", None)
            or (current_agent.stationed_terminal if current_agent else None)
            or "(none)"
        )
        parent_terminal_str = (
            getattr(current_agent, "parent_terminal", None)
            if current_agent else None
        ) or "(none)"
        deployment_status_str = _format_deployment(current_agent)

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
            .replace("{{terminalName}}", terminal_name_str) \
            .replace("{{parentTerminal}}", parent_terminal_str) \
            .replace("{{deploymentStatus}}", deployment_status_str) \
            .replace("{{tools}}", _render_tool_catalog_enhanced(state, loop, depth))

        # ── Extended template variables (from agent_roles, workflow_engine, etc.) ──

        # {{workflowPhase}} — active workflow phase guidance
        workflow_section = workflow_engine.render_workflow_section()
        system_prompt = system_prompt.replace("{{workflowPhase}}", workflow_section)

        # {{rolePrompt}} — specialized role system prompt (for sub-agents)
        role_name = state.get("_role_name")
        role_prompt = agent_roles.get_role_system_prompt(role_name) if role_name else ""
        system_prompt = system_prompt.replace("{{rolePrompt}}", role_prompt)

        # {{confidenceGuidance}} — confidence scoring instructions (for reviewer roles)
        if role_name and agent_roles.get_role(role_name) and agent_roles.get_role(role_name).confidence_threshold > 0:
            threshold = agent_roles.get_role(role_name).confidence_threshold
            confidence_guidance = (
                f"## Confidence Filtering\n"
                f"You are operating as a specialized reviewer. Rate each finding "
                f"0-100 confidence. Only report findings with confidence >= {threshold}. "
                f"Quality over quantity — do not report low-confidence issues."
            )
        else:
            confidence_guidance = ""
        system_prompt = system_prompt.replace("{{confidenceGuidance}}", confidence_guidance)

        # {{skillContext}} — activated skill bodies (placeholder; skills.py handles)
        system_prompt = system_prompt.replace("{{skillContext}}",
                                               state.get("_skill_context", ""))

        # {{parallelResults}} — aggregated sub-agent results from inbox
        parallel_results_str = _format_parallel_results(inbox_msgs)
        system_prompt = system_prompt.replace("{{parallelResults}}", parallel_results_str)

        # {{behaviorDiagnostics}} — empty placeholder (filled in user message)
        system_prompt = system_prompt.replace("{{behaviorDiagnostics}}", "")

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
        tool_calls = response.get("tool_calls") or []
        debug_entry.command = ", ".join(tc.get("name", "?") for tc in tool_calls) if tool_calls else ""
        debug_entry.done = response.get("done", len(tool_calls) == 0)
        debug_entry.error = response.get("error", False)
        debug_entry.billing = response.get("_billing", {})

        if response.get("error"):
            if events_cb is not None:
                deps.console.print(f"[red]{response['reply']}[/red]")
            state["shortTermMemory"] += f"\n  -Error: {response['reply']}"
            add_debug_log(debug_entry)
            break

        reply = response.get("reply") or ""
        done = response.get("done", len(tool_calls) == 0)
        billing = response.get("_billing", {})

        # ── JSON repair: model produced something that looked like JSON but
        # didn't parse. Nudge it once or twice instead of giving up. ──
        if response.get("_repair_needed"):
            attempts = state.get("_repair_count", 0)
            if attempts < 2:
                state["_repair_count"] = attempts + 1
                raw_excerpt = (response.get("_raw") or "")[:200]
                state["shortTermMemory"] += (
                    f"\n  ⚠ Malformed JSON (attempt {attempts+1}/2). "
                    f"Raw start: {raw_excerpt!r}"
                )
                if events_cb is not None:
                    deps.console.print(
                        f"[yellow]Response was not valid JSON — retrying (attempt {attempts+1}/2)[/yellow]")
                # Skip dispatch this turn; next iteration's user_input will
                # carry an inline error reminder built by _build_user_message
                # (which reads state['_repair_count']).
                add_debug_log(debug_entry)
                if loop < max_loops - 1:
                    try:
                        time.sleep(0.5)
                    except KeyboardInterrupt:
                        break
                memory_entries = _read_memory(deps)
                user_input = _build_user_message(
                    original_input, state, memory_entries, chat_history, loop, max_loops,
                )
                # Append repair nudge to user_input directly so the model sees it
                user_input += (
                    f"\n\n<json_error>Your previous response did not parse as valid JSON. "
                    f"Emit exactly ONE JSON object: "
                    f'{{\"reply\": \"...\", \"tool_calls\": [{{\"name\": \"...\", \"arguments\": {{...}}}}]}}.'
                    f" No prose outside the object. No code fences. "
                    f"Raw start of last attempt: {raw_excerpt!r}</json_error>"
                )
                continue
            else:
                state["shortTermMemory"] += "\n  -Error: JSON repair gave up after 2 attempts"
                add_debug_log(debug_entry)
                break
        else:
            state["_repair_count"] = 0

        # ── Detect "silent failure": model generated tokens but backend
        # extracted nothing. This means the model's output didn't match the
        # expected JSON schema. Bail out immediately with a useful message.
        if not reply and not tool_calls:
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
                # Flush immediately — don't wait for tool_calls or done
                events_cb(pending_events)
                pending_events.clear()

        # ── Handle JSON parse failure: nudge the model ──
        # When model outputs pure prose instead of JSON, show a subtle hint
        # and inject a reminder into the next turn.
        _nudge_needed = False
        if response.get("_parse_failed"):
            _nudge_needed = True
            _parse_fail_count = state.get("_parse_fail_count", 0) + 1
            state["_parse_fail_count"] = _parse_fail_count
            if events_cb is not None:
                if _parse_fail_count == 1:
                    deps.console.print(f"[dim yellow](formatting issue — asking AI to retry)[/dim yellow]")
                elif _parse_fail_count >= 3:
                    deps.console.print(f"[yellow]AI failed to use JSON format {_parse_fail_count} times. Consider rephrasing your request.[/yellow]")
                    # Reset counter to avoid spamming
                    state["_parse_fail_count"] = 0
        else:
            # Reset counter on successful parse
            state["_parse_fail_count"] = 0

        # 7. Show billing if available (only in interactive mode)
        if billing:
            cost = billing.get("costCents", 0)
            balance = billing.get("balanceCents", 0)
            if cost > 0:
                billing_text = f"${cost / 100:.2f} · balance ${balance / 100:.2f}"
                if events_cb is not None:
                    deps.console.print(f"[dim]({billing_text})[/dim]")
                    pending_events.append({"type": "system", "kind": "billing", "content": billing_text})
                    events_cb(pending_events)
                    pending_events.clear()

        # 8. Dispatch tool_calls through the unified tool registry.
        # Per-call: pre_tool hook → (shell-flavored) policy + pre_command → loop_command override
        # → registry.invoke → post_tool hook → (shell-flavored) post_command + display
        # → __PARENT_CMD__ marker scan → terminalHistory row (one per call).
        MAX_TC_PER_TURN = 8
        if len(tool_calls) > MAX_TC_PER_TURN:
            _truncated_n = len(tool_calls)
            tool_calls = tool_calls[:MAX_TC_PER_TURN]
            state["shortTermMemory"] += (
                f"\n  ⚠ Emitted {_truncated_n} tool calls; only first {MAX_TC_PER_TURN} ran. "
                f"Be more selective next turn."
            )

        formatted_outputs: list[str] = []
        per_call_rows: list[dict] = []

        if tool_calls:
            # Resolve stationed terminal for this agent (if any)
            _stationed_session = None
            if current_agent and getattr(current_agent, "stationed_terminal", None):
                _stationed_term_info = get_terminal(current_agent.stationed_terminal)
                if _stationed_term_info and _stationed_term_info.session and _stationed_term_info.session.is_alive():
                    _stationed_session = _stationed_term_info.session

            for idx, tc in enumerate(tool_calls):
                name = tc.get("name", "")
                arguments = tc.get("arguments", {}) or {}
                if not name:
                    continue
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}

                call_id = f"call_{loop+1:02d}_{idx+1:02d}"
                salient = _salient_arg(name, arguments)
                is_shell_flavored = name in ("shell.exec", "terminal.send")

                # ── Role / Workflow tool filtering ──
                _role_name = state.get("_role_name")
                if not agent_roles.is_tool_allowed_for_role(name, _role_name):
                    result = {"ok": False,
                              "error": f"BLOCKED: tool '{name}' not allowed for role '{_role_name}'",
                              "tool": name, "returncode": -1}
                    formatted_outputs.append(
                        _format_tool_result_for_loop(name, result, int(get_runtime_config("output_truncate") or 3000)))
                    per_call_rows.append({
                        "command": salient, "output": result.get("error", ""),
                        "returncode": -1, "tool": name, "call_id": call_id,
                    })
                    continue
                if not workflow_engine.is_tool_allowed_in_workflow(name):
                    result = {"ok": False,
                              "error": f"BLOCKED: tool '{name}' not allowed in current workflow phase",
                              "tool": name, "returncode": -1}
                    formatted_outputs.append(
                        _format_tool_result_for_loop(name, result, int(get_runtime_config("output_truncate") or 3000)))
                    per_call_rows.append({
                        "command": salient, "output": result.get("error", ""),
                        "returncode": -1, "tool": name, "call_id": call_id,
                    })
                    continue

                # ── pre_tool hook (universal, can block) ──
                pre_tool_allowed, _ = hooks_mod.trigger("pre_tool", {
                    "tool": name, "args": arguments, "agent_id": agent_id,
                    "depth": depth, "call_id": call_id, "loop": loop + 1,
                })
                if not pre_tool_allowed:
                    result = {"ok": False, "error": "blocked by pre_tool hook",
                              "tool": name, "returncode": -1}
                else:
                    # ── Shell-flavored: policy + pre_command + .laintas/loop.py ──
                    skip_invoke = False
                    if is_shell_flavored:
                        policy_ok, policy_reason, policy_approval = _check_policy(
                            salient, agent_id=agent_id, events_cb=events_cb, deps=deps)
                        if not policy_ok:
                            result = {"ok": False, "error": f"BLOCKED: {policy_reason}",
                                      "tool": name, "returncode": -1, "policy": "deny"}
                            skip_invoke = True
                        elif policy_approval:
                            state["shortTermMemory"] += f"\n  ⚠ Policy: {policy_reason}"

                        if not skip_invoke:
                            cmd_allowed, _ = hooks_mod.trigger("pre_command", {
                                "command": salient, "depth": depth, "agent_id": agent_id,
                                "tool": name, "call_id": call_id,
                            })
                            if not cmd_allowed:
                                result = {"ok": False, "error": "BLOCKED by pre_command hook",
                                          "tool": name, "returncode": -1}
                                skip_invoke = True

                        # .laintas/loop.py user override (only for shell.exec)
                        if not skip_invoke and name == "shell.exec":
                            loop_handler = _load_loop_commands()
                            if loop_handler:
                                _loop_ctx = {
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
                                    _override = loop_handler(salient, _loop_ctx)
                                except Exception as e:
                                    _override = None
                                    if events_cb is not None:
                                        deps.console.print(f"[red].laintas/loop.py error: {e}[/red]")
                                if isinstance(_override, str):
                                    result = {"ok": True, "result": _override,
                                              "tool": name, "returncode": 0,
                                              "via": "loop_command"}
                                    skip_invoke = True

                    if not skip_invoke:
                        # Build ToolCtx with all loop context, including stationed
                        tool_ctx = tools_mod.ToolCtx(
                            deps=deps, agent_id=agent_id, session=session,
                            events_cb=events_cb, cwd=os.getcwd(),
                            interactive_session=interactive_session,
                            stationed_terminal=_stationed_session,
                            get_terminal=get_terminal,
                            get_all_terminals=get_all_terminals,
                            register_terminal=register_terminal,
                            unregister_terminal=unregister_terminal,
                            get_agent=get_agent, get_all_agents=get_all_agents,
                            get_current_agent=get_current_agent,
                            station_agent=station_agent,
                            unstation_agent=unstation_agent,
                            send_to_agent=send_to_agent,
                            wait_for_agent=wait_for_agent,
                            abort_agent=abort_agent,
                            spawn_subagent=spawn_subagent,
                            rename_agent=rename_agent,
                            switch_to_agent=switch_to_agent,
                            register_agent_fn=register_agent,
                            depth=depth,
                        )

                        result = tools_mod.get_registry().invoke(name, arguments, tool_ctx)

                        # Sync back interactive_session (tools may create/close sessions)
                        if tool_ctx.interactive_session != interactive_session:
                            interactive_session = tool_ctx.interactive_session

                        # __PARENT_CMD__ marker handling for shell.exec via session
                        if name == "shell.exec" and result.get("via") in ("stationed", "interactive"):
                            _cleaned, _parent_result = _process_parent_cmd_marker(result.get("result", "") or "")
                            if _parent_result is not None:
                                result["result"] = (_cleaned or "").rstrip() + f"\n[parent] {_parent_result}"

                # ── Format result for AI prompt ──
                truncate = int(get_runtime_config("output_truncate") or 3000)
                formatted = _format_tool_result_for_loop(name, result, truncate)
                formatted_outputs.append(formatted)

                # If the tool name wasn't recognized, re-show the full catalog
                # on the next turn so the model can self-correct.
                _err = (result.get("error") or "") if isinstance(result, dict) else ""
                if "not found" in _err and "tool" in _err.lower():
                    state["_force_full_catalog_next"] = True

                _rc = result.get("returncode", 0 if result.get("ok") else -1)

                # ── post_tool hook (universal) ──
                hooks_mod.trigger("post_tool", {
                    "tool": name, "ok": result.get("ok", False),
                    "call_id": call_id, "loop": loop + 1,
                    "returncode": _rc,
                })
                # ── post_command hook (shell-flavored only, per-call) ──
                if is_shell_flavored:
                    hooks_mod.trigger("post_command", {
                        "command": salient, "output": formatted[:1000],
                        "returncode": _rc, "loop": loop + 1, "done": False,
                        "tool": name, "call_id": call_id,
                    })

                # ── Per-call terminalHistory row ──
                per_call_rows.append({
                    "command": salient,
                    "output": formatted,
                    "returncode": _rc,
                    "tool": name,
                    "call_id": call_id,
                })

                # Track files this call read/touched
                _track_files_in_command(salient, state.setdefault("_files_seen", []))

                # Echo into chat_history as a knowledge entry (interactive mode only;
                # gives the model a structured record of past tool calls without
                # bloating execute-mode history).
                if events_cb is not None:
                    chat_history.append({
                        "role": "knowledge",
                        "content": f"[{call_id}] {name}({salient[:60]}) → {formatted[:400]}",
                    })

                # ── Debug + events ──
                debug_entry.exec_command = f"/tool {name}"
                debug_entry.exec_returncode = _rc
                try:
                    debug_entry.exec_stdout = json.dumps(result, ensure_ascii=False, default=str)[:2000]
                except (TypeError, ValueError):
                    debug_entry.exec_stdout = str(result)[:2000]
                if not hasattr(debug_entry, "tool_calls_log"):
                    debug_entry.tool_calls_log = []
                debug_entry.tool_calls_log.append({
                    "name": name, "call_id": call_id,
                    "ok": result.get("ok", False), "via": result.get("via", "registry"),
                })

                if events_cb is not None:
                    ok_mark = "[green]✓[/green]" if result.get("ok") else "[red]✗[/red]"
                    _hint = salient[:80] if salient else name
                    deps.console.print(
                        f"  {ok_mark} [dim cyan]{name}[/dim cyan] [dim]{_hint}[/dim]")
                    pending_events.append({"type": "system", "kind": "tool",
                                            "content": name,
                                            "meta": {"ok": result.get("ok", False),
                                                     "call_id": call_id,
                                                     "salient": salient[:200]}})
                    pending_events.append({"type": "system", "kind": "output",
                                            "content": formatted[:2000]})
                    events_cb(pending_events)
                    pending_events.clear()

                    # Display panels for shell.exec (mirror old UX)
                    if name == "shell.exec":
                        if result.get("via") in ("stationed", "interactive"):
                            try:
                                _alive = (_stationed_session.is_alive() if _stationed_session
                                          else (interactive_session.is_alive() if interactive_session else False))
                                deps.display_sub_terminal_preview(
                                    salient, formatted[:2000],
                                    depth=depth + 1, alive=_alive)
                            except Exception:
                                pass
                        elif result.get("via") in ("subprocess", "parent", "loop_command"):
                            try:
                                deps.display_command_output(salient, _rc, formatted, depth=depth + 1)
                            except Exception:
                                pass

        # Concat all per-call outputs into lastOutput so the next prompt's fallback
        # rendering and shortTermMemory see every result, not just the last.
        if formatted_outputs:
            state["lastOutput"] = ("\n---\n".join(formatted_outputs))[: int(get_runtime_config("output_truncate") or 3000) * 2]

        # 10. Update state — append per-call rows (or one no-op row if no tool_calls)
        tool_names_for_log = [r["tool"] for r in per_call_rows]
        action_desc_short = ", ".join(tool_names_for_log) if tool_names_for_log else ""
        state["shortTermMemory"] += \
            f"\n  Step {loop+1}: {reply} | tools: {action_desc_short} | result: {state.get('lastOutput','')[:200]}"
        state["terminalHistory"].extend(per_call_rows)

        # ── Error analysis: detect patterns + suggest recovery ──
        last_output = state.get("lastOutput", "")
        last_rc = debug_entry.exec_returncode if hasattr(debug_entry, 'exec_returncode') else -1
        error_info = _analyze_error(last_output, last_rc)
        if error_info["category"] != "none":
            state["shortTermMemory"] += \
                f"\n  🔍 Error detected [{error_info['category']}]: {error_info['suggestion']}"
            # ── Hooks: on_error ──
            hooks_mod.trigger("on_error", {
                "command": action_desc_short,
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
        # Count steps where the AI produced NO reply AND NO tool_calls as idle.
        # A conversational reply (text without tool calls) is real work and
        # resets the counter, same as a tool call would.
        if not tool_calls and not reply:
            stale_count += 1
            if stale_count >= staleness_limit:
                if events_cb is not None and deps is not None:
                    deps.console.print(f"[dim]Task appears complete ({stale_count} idle steps). Exiting.[/dim]")
                    # Show raw response on idle exit so users can diagnose backend issues
                    if not tool_calls and not reply:
                        raw = debug_entry.response_raw
                        if raw:
                            deps.console.print(f"[dim yellow]Last backend response: {json.dumps(raw, ensure_ascii=False, default=str)[:500]}[/dim yellow]")
                if events_cb is not None and pending_events:
                    events_cb(pending_events)
                    pending_events.clear()
                break
        else:
            stale_count = 0  # reset on any output (tool call or conversational reply)

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

        # ── Inject nudge if model failed to use JSON format ──
        if _nudge_needed:
            user_input += (
                "\n\n<json_reminder>IMPORTANT: Your previous response was plain text. "
                "You MUST respond with a valid JSON object in this exact format:\n"
                '```json\n{\n  "reply": "your text response here",\n  "tool_calls": [\n    '
                '{"name": "tool.name", "arguments": {...}}\n  ]\n}\n```\n'
                "If you have no actions to take, return: {\"reply\": \"your text\", \"tool_calls\": []}\n"
                "Do NOT output plain text or prose. ONLY output valid JSON.</json_reminder>"
            )

    # Clean up session only when NOT managed by REPL (existing_session=None)
    # When REPL manages the session, it handles lifecycle externally.
    if existing_session is None and interactive_session is not None:
        interactive_session.close()

    # Safety flush: push any remaining pending events (e.g. if loop exited
    # via staleness, interrupt, or max_loops without reaching the done-block flush)
    if events_cb is not None and pending_events:
        events_cb(pending_events)
        pending_events.clear()

    # Persist agent state so a future session can restore the chat history.
    if self_info is not None:
        try:
            self_info.chat_history = chat_history
            self_info.state = state
            agent_persistence.save_agent_state(self_info)
        except Exception:
            pass

    if step_replies:
        return {"success": done, "msg": "\n\n".join(step_replies), "state": state,
                "session": interactive_session}
    return {"success": done, "msg": reply, "state": state,
            "session": interactive_session}
