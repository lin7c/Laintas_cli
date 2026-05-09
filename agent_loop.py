#!/usr/bin/env python3
"""AI Agent Loop for laintas_cli — extracted from laintas_cli.py."""

import os
import re
import json
import shlex
import subprocess
import sys
import time
import uuid
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

# Path to laintas_cli.py for spawning child terminals
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LAINTAS_CLI = os.path.join(_SCRIPT_DIR, "laintas_cli.py")


# ── Constants ──────────────────────────────────────────────────────────
MAX_LOOPS = 10
MAX_TOKENS = 2000
MAX_DEBUG_ENTRIES = 50

# Mutable defaults — these are the "factory" values; runtime overrides stored in _runtime_config
_DEFAULT_CONFIG = {
    "max_loops": 10,
    "max_tokens": 2000,
    "max_debug_entries": 50,
    "loop_delay": 1.5,           # seconds between loop iterations
    "output_truncate": 3000,      # chars — lastOutput tail truncation
    "poll_timeout": 10.0,         # seconds — wait for first command output
    "terminal_tail_lines": 20,    # lines — sub-terminal snapshot
    "heartbeat_interval": 30,     # seconds — agent heartbeat
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

_agent_registry: dict[str, AgentInfo] = {}
_agent_counter: int = 0
_current_agent_id: Optional[str] = None


def register_agent(name: str = None, depth: int = 0) -> AgentInfo:
    """Create and register a new AI agent. Returns the AgentInfo."""
    global _agent_registry, _agent_counter
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
    )
    _agent_registry[agent_id] = info
    return info


def unregister_agent(agent_id: str) -> bool:
    """Remove an agent. Returns True if it existed."""
    global _current_agent_id
    if _current_agent_id == agent_id:
        _current_agent_id = None
    return _agent_registry.pop(agent_id, None) is not None


def get_agent(agent_id: str) -> Optional[AgentInfo]:
    return _agent_registry.get(agent_id)


def get_all_agents() -> list:
    return sorted(_agent_registry.values(), key=lambda a: a.created_at)


def get_current_agent() -> Optional[AgentInfo]:
    if _current_agent_id:
        return _agent_registry.get(_current_agent_id)
    return None


def switch_to_agent(agent_id: str) -> bool:
    """Switch the active agent. Returns True on success."""
    global _current_agent_id
    if agent_id not in _agent_registry:
        return False
    _current_agent_id = agent_id
    return True


def set_current_agent_id(agent_id: str) -> None:
    global _current_agent_id
    _current_agent_id = agent_id


def rename_agent(agent_id: str, new_name: str) -> bool:
    info = _agent_registry.get(agent_id)
    if info is None:
        return False
    info.name = new_name
    return True


def station_agent(agent_id: str, terminal_name: str) -> bool:
    """Station an agent in a terminal. Multiple agents can share one terminal."""
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
    agent = _agent_registry.get(agent_id)
    if agent and agent.stationed_terminal:
        term = _terminal_registry.get(agent.stationed_terminal)
        if term:
            if agent_id in term.stationed_agent_ids:
                term.stationed_agent_ids.remove(agent_id)
            term.stationed_agent_id = term.stationed_agent_ids[0] if term.stationed_agent_ids else None
        agent.stationed_terminal = None


def close_all_agents() -> None:
    """Clean up all agent registrations."""
    _agent_registry.clear()
    global _current_agent_id
    _current_agent_id = None


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


def _build_terminal_section(state: dict) -> str:
    """Section 1: recent terminal outputs (last 5 steps)."""
    history = state.get('terminalHistory', [])
    if not history:
        return state.get('lastOutput', 'Ready to begin.')
    parts = []
    recent = history[-5:]
    offset = len(history) - len(recent)
    for i, entry in enumerate(recent, 1):
        output = entry.get('output', '')
        lines = output.split('\n')
        if len(lines) > _MAX_TERMINAL_LINES:
            output = f"...(truncated)...\n" + '\n'.join(lines[-_MAX_TERMINAL_LINES:])
        cmd_label = entry.get('command', '')[:80]
        parts.append(f"--- Step {offset + i}: {cmd_label} ---")
        parts.append(output)
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
    """Section 3: recent conversation between user and AI (last 20 messages)."""
    if not chat_history:
        return "(no history)"
    recent = chat_history[-20:]
    lines = []
    for m in recent:
        role = m.get('role', '?')
        content = m.get('content', '')
        if role == 'knowledge':
            continue  # already shown in MEMORY SYSTEM
        if isinstance(content, list):
            content = ' '.join(str(c.get('text', c)) for c in content if isinstance(c, dict))
        content = str(content)[:300]
        label = "User" if role == "user" else "AI"
        lines.append(f"  [{label}] {content}")
    return '\n'.join(lines) if lines else "(no history)"


def get_terminals_snapshot() -> str:
    """Collect latest 20 lines from each alive named terminal."""
    terminals = get_all_terminals()
    if not terminals:
        return ""
    alive = [t for t in terminals if t.session.is_alive()]
    dead = [t for t in terminals if not t.session.is_alive()]
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


def run_agent_loop(
    deps: LoopDeps,
    original_input: str,
    session: dict,
    state: dict,
    chat_history: list = None,
    events_cb = None,          # callable(list[dict]) — push events to backend
    existing_session = None,   # Optional[InteractiveSession] — reuse existing PTY
    depth: int = 0,            # 0=user terminal, 1+=sub-agent
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
    for loop in range(max_loops):
        _loop_id = next_debug_loop()

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
        current_agent = get_current_agent()
        agent_name = current_agent.name if current_agent else "Laintas CLI"
        agent_id = current_agent.id if current_agent else "unknown"
        system_prompt = prompt_template \
            .replace("{{globalMemory}}", global_memory_str) \
            .replace("{{agentName}}", agent_name) \
            .replace("{{agentId}}", agent_id) \
            .replace("{{currentPath}}", os.getcwd()) \
            .replace("{{activeFile}}", "None") \
            .replace("{{depth}}", str(depth)) \
            .replace("{{nextDepth}}", str(depth + 1))

        # 5. Build user message: 5 sections (terminal | conversation | memory | terminals | task)
        terminal_section = _build_terminal_section(state)
        memory_section = _build_memory_section(memory_entries, state, chat_history)
        conversation_section = _build_conversation_section(chat_history)
        terminals_snapshot = get_terminals_snapshot()
        user_input = f"""[TERMINAL OUTPUT]
{terminal_section}

[CONVERSATION HISTORY]
{conversation_section}

[MEMORY SYSTEM]
{memory_section}

[SUB-TERMINALS]
{terminals_snapshot or "(none)"}

Task: {original_input}

Progress: step {loop+1}/{max_loops} — {len(state.get('terminalHistory', []))} commands executed so far."""

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
        if events_cb is not None:
            with deps.console.status("[bold green]AI thinking...[/bold green]", spinner="dots"):
                response = deps.call_backend(
                    session=session,
                    message=user_input,
                    system_prompt=system_prompt,
                    current_path=os.getcwd(),
                    history=chat_history,
                )
        else:
            response = deps.call_backend(
                session=session,
                message=user_input,
                system_prompt=system_prompt,
                current_path=os.getcwd(),
                history=chat_history,
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

        # 6. Print AI reply (only in interactive mode)
        if reply:
            if events_cb is not None:
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

        if command:
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

            # ── Meta-command: /station <name> ────────────────────────────
            elif (_meta_station := re.match(r'^/station\s+(\S+)\s*$', command)):
                name = _meta_station.group(1)
                existing_term = get_terminal(name)
                if existing_term and not existing_term.session.is_alive():
                    unregister_terminal(name)
                    existing_term = None
                if existing_term is None:
                    lain_cmd = f"{sys.executable} {_LAINTAS_CLI} --simple-prompt"
                    sub = deps.SubTerminalSession(lain_cmd)
                    sub.start()
                    time.sleep(0.3)
                    if sub.is_alive():
                        sub.read_output(timeout=0.3)
                    register_terminal(sub, "laintas-cli", depth, name=name)
                current_agent = get_current_agent()
                if current_agent:
                    station_agent(current_agent.id, name)
                if events_cb is not None:
                    deps.console.print(
                        f"[dim green]Stationed in terminal [bold]{name}[/bold][/dim green]")
                state["lastOutput"] = f"Stationed in terminal {name}"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
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
                elif not term.session.is_alive():
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
                    if not term.session.is_alive():
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

            # ── Meta-command: /agents [name|name <n>] ───────────────────
            elif (_meta_agents := re.match(r'^/agents(?:\s+(.+))?$', command)):
                rest = (_meta_agents.group(1) or "").strip()
                if not rest:
                    agents = get_all_agents()
                    current = get_current_agent()
                    lines = ["Available agents:"]
                    for a in agents:
                        marker = " <-- current" if (current and a.id == current.id) else ""
                        st = f" [stationed: {a.stationed_terminal}]" if a.stationed_terminal else ""
                        lines.append(f"  {a.id}: {a.name}{st}{marker}")
                    state["lastOutput"] = "\n".join(lines)
                    if events_cb is not None:
                        deps.console.print("\n".join(lines))
                elif rest.startswith("name "):
                    new_name = rest[5:].strip()
                    current = get_current_agent()
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
                # ── fall through to state-update / done-check ──

            # ── .loop_command.py custom handler ────────────────────────────
            elif (loop_handler := _load_loop_commands()):
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
                    result = loop_handler(command, ctx)
                    if result is not None:
                        state["lastOutput"] = result
                        debug_entry.exec_command = command
                        if events_cb is not None:
                            pending_events.append({"type": "system", "kind": "command", "content": command})
                    else:
                        # handler passed → treat as normal command
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
                        debug_entry.exec_stdout = cmd_output
                        debug_entry.exec_returncode = sub.returncode if sub.returncode is not None else -1
                        debug_entry.session_command = command
                        if events_cb is not None:
                            pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                            events_cb(pending_events)
                            pending_events.clear()
                        if events_cb is not None:
                            deps.display_command_output(command, sub.returncode, cmd_output, depth=depth + 1)
                except Exception as e:
                    if events_cb is not None:
                        deps.console.print(f"[red].loop_command.py error: {e}[/red]")
                    state["lastOutput"] = f".loop_command.py error: {e}"

            else:
                # ── Normal command: stationed or sub-terminal ──
                current_agent = get_current_agent()
                stationed_name = current_agent.stationed_terminal if current_agent else None
                stationed_term = get_terminal(stationed_name) if stationed_name else None

                if stationed_term and stationed_term.session.is_alive():
                    # ── Stationed: run command in persistent PTY with markers ──
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

        # 11. Delay between steps (interruptible)
        if loop < max_loops - 1:
            try:
                time.sleep(float(get_runtime_config("loop_delay")))
            except KeyboardInterrupt:
                deps.console.print("\n[yellow]Agent loop interrupted.[/yellow]")
                break

        # 12. Prepare next input — rebuild sections with updated state
        memory_entries = _read_memory(deps)  # re-read in case AI wrote memory
        terminal_section = _build_terminal_section(state)
        memory_section = _build_memory_section(memory_entries, state, chat_history)
        conversation_section = _build_conversation_section(chat_history)
        terminals_snapshot = get_terminals_snapshot()
        user_input = f"""[TERMINAL OUTPUT]
{terminal_section}

[CONVERSATION HISTORY]
{conversation_section}

[MEMORY SYSTEM]
{memory_section}

[SUB-TERMINALS]
{terminals_snapshot or "(none)"}

Task: {original_input}

Progress: step {loop+1}/{max_loops} — {len(state.get('terminalHistory', []))} commands executed so far."""

    # Clean up session only when NOT managed by REPL (existing_session=None)
    # When REPL manages the session, it handles lifecycle externally.
    if existing_session is None and interactive_session is not None:
        interactive_session.close()

    if step_replies:
        return {"success": done, "msg": "\n\n".join(step_replies), "state": state,
                "session": interactive_session}
    return {"success": done, "msg": reply, "state": state,
            "session": interactive_session}
