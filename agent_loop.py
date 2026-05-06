#!/usr/bin/env python3
"""AI Agent Loop for laintas_cli — extracted from laintas_cli.py."""

import os
import re
import shlex
import sys
import time
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


# ── Dependencies Container ─────────────────────────────────────────────

@dataclass
class LoopDeps:
    """External dependencies injected from laintas_cli."""
    read_file: Callable[[str], Optional[str]]
    append_file: Callable[[str, str], None]
    strip_ansi: Callable[[str], str]
    generate_prompt: Callable[[], str]
    call_backend: Callable[..., dict]
    SubTerminalSession: type
    display_command_output: Callable[..., None]
    display_sub_terminal_preview: Callable[..., None]
    console: Any  # rich.console.Console
    Markdown: type  # rich.markdown.Markdown


# ── Execution State Builder ────────────────────────────────────────────

def build_execution_state(state: dict) -> str:
    """Build execution state snapshot (mirrors AutonomousKernel.ts)."""
    last_output = state.get('lastOutput', 'Ready to begin.')
    truncate = int(get_runtime_config("output_truncate"))
    if len(last_output) > truncate:
        last_output = "...(truncated)...\n" + last_output[-truncate:]
    terminals_snapshot = get_terminals_snapshot()
    result = f"""
[STEP_EXECUTED_HISTORY]
{{
{state.get('shortTermMemory', '')}
}}

[PHYSICAL_OBSERVATION]
Last Result: {last_output}
""".strip()
    if terminals_snapshot:
        result += '\n\n' + terminals_snapshot
    return result


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
            lines.append(f"  {t.name} ({t.command}):")
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

        # 1. Read .helpwo memory
        global_memory = deps.read_file(".helpwo") or ""

        # 2. Read .cli.prop system prompt
        prompt_template = deps.read_file(".cli.prop") or ""
        if not prompt_template:
            prompt_template = deps.generate_prompt()

        # 3. Build conversation history text
        conversation_text = ""
        if chat_history and len(chat_history) > 0:
            recent = chat_history[-20:]
            conversation_text = "\n".join(
                f"{'User' if m.get('role') == 'user' else 'AI'}: {m.get('content', '')}"
                for m in recent
            )

        # 4. Build system prompt (same template vars as Helpwo)
        system_prompt = prompt_template \
            .replace("{{currentPath}}", os.getcwd()) \
            .replace("{{activeFile}}", "None") \
            .replace("{{globalMemory}}", global_memory or "(empty)") \
            .replace("{{lastOutput}}", build_execution_state(state)) \
            .replace("{{conversationHistory}}", conversation_text or "(no conversation history)") \
            .replace("{{depth}}", str(depth)) \
            .replace("{{nextDepth}}", str(depth + 1))

        # ── Debug: create entry before API call ──
        debug_entry = DebugEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            loop=_loop_id,
            user_input=user_input[:2000],
            current_path=os.getcwd(),
            context_sizes={
                "global": len(global_memory or ""),
                "conversation": len(conversation_text or ""),
                "prompt": len(system_prompt),
            },
            request_body={
                "message": user_input[:2000],
                "currentPath": os.getcwd(),
                "history": chat_history[-20:] if chat_history else [],
                "promptLen": len(system_prompt),
                "promptPreview": system_prompt[:500],
                "globalContext": (global_memory or "(empty)")[:500],
                "conversationContext": conversation_text or "(no history)",
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

        reply = response.get("reply", "")
        command = response.get("command", "").strip()
        memory = response.get("memory", "").strip()
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

        # 8. Write memory to .helpwo
        if memory:
            if events_cb is not None:
                deps.console.print(f"[dim cyan]Memory: {memory[:100]}...[/dim cyan]")
            deps.append_file(".helpwo", memory)
            if events_cb is not None:
                pending_events.append({"type": "system", "kind": "memory", "content": memory[:200]})

        # 9. Handle command dispatch (close_session/send_keys normalized to /session close, /keys)

        if command:
            # ── Meta-command: /session close ────────────────────────────
            if re.match(r'^/session\s+close\s*$', command):
                if interactive_session is not None:
                    deps.console.print(f"[dim yellow]Closing session: {interactive_session.command[:60]}[/dim yellow]")
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "close_session", "content": interactive_session.command[:200]})
                    interactive_session.close()
                    cmd_output = interactive_session.full_output
                    debug_entry.exec_command = command
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = interactive_session.returncode
                    state["lastOutput"] = cmd_output
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
                    deps.console.print(f"[dim yellow]> {keys[:60]}...[/dim yellow]")
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "send_keys", "content": keys[:200]})
                    interactive_session.send_keys(keys)
                    time.sleep(0.3)
                    new_output = interactive_session.read_output(timeout=0.5)
                    cmd_output = interactive_session.full_output
                    debug_entry.exec_command = command
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = interactive_session.returncode
                    debug_entry.session_command = interactive_session.command
                    state["lastOutput"] = cmd_output
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

            # ── Meta-command: /term new <name> <shell-command> ──────────
            elif (_meta_term_new := re.match(r'^/term\s+new\s+(\S+)\s+(.+)$', command)):
                requested_name = _meta_term_new.group(1)
                term_cmd = _meta_term_new.group(2).strip()
                # Launch as a persistent laintas terminal (like term0)
                wrapped = f"{sys.executable} {_LAINTAS_CLI} --depth {depth + 1} --simple-prompt"
                sub = deps.SubTerminalSession(wrapped)
                sub.start()
                time.sleep(0.5)  # wait for laintas REPL to start
                if sub.is_alive():
                    sub.read_output(timeout=0.3)
                    sub.send_keys(term_cmd + "\n")
                assigned_name = register_terminal(sub, term_cmd, depth, name=requested_name)
                if assigned_name != requested_name:
                    if events_cb is not None:
                        deps.console.print(
                            f"[dim yellow]Name '{requested_name}' taken; "
                            f"assigned '{assigned_name}'[/dim yellow]")
                if events_cb is not None:
                    deps.console.print(
                        f"[dim green]+ Terminal [bold]{assigned_name}[/bold]: "
                        f"{term_cmd[:80]}[/dim green]")
                state["lastOutput"] = f"Created terminal {assigned_name}: {term_cmd}"
                debug_entry.exec_command = command
                debug_entry.session_command = term_cmd
                debug_entry.exec_stdout = sub.full_output
                debug_entry.exec_returncode = -1
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "command", "content": command})
                    events_cb(pending_events)
                    pending_events.clear()

            # ── Meta-command: /term send <name> <keys> ──────────────────
            elif (_meta_term_send := re.match(r'^/term\s+send\s+(\S+)\s+(.*)$', command)):
                target_name = _meta_term_send.group(1)
                keys = _meta_term_send.group(2)
                term = get_terminal(target_name)
                if term is None:
                    deps.console.print(f"[yellow]Warning: terminal '{target_name}' not found[/yellow]")
                    state["lastOutput"] = f"Error: terminal '{target_name}' not found"
                elif not term.session.is_alive():
                    deps.console.print(f"[yellow]Warning: terminal '{target_name}' is no longer alive[/yellow]")
                    state["lastOutput"] = f"Warning: terminal '{target_name}' is dead"
                else:
                    deps.console.print(f"[dim yellow]> /term send {target_name} {keys[:60]}...[/dim yellow]")
                    term.session.send_keys(keys)
                    time.sleep(0.3)
                    term.session.read_output(timeout=0.5)
                    cmd_output = term.session.full_output
                    state["lastOutput"] = cmd_output
                    debug_entry.exec_command = command
                    debug_entry.exec_stdout = cmd_output
                    debug_entry.exec_returncode = term.session.returncode
                    debug_entry.session_command = term.command
                    if events_cb is not None:
                        pending_events.append({"type": "system", "kind": "send_keys", "content": keys[:200]})
                        pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                        events_cb(pending_events)
                        pending_events.clear()
                    if not term.session.is_alive():
                        deps.console.print(f"[dim yellow]Terminal [bold]{target_name}[/bold] exited[/dim yellow]")

            # ── Meta-command: /term close <name> ─────────────────────────
            elif (_meta_term_close := re.match(r'^/term\s+close\s+(\S+)\s*$', command)):
                target_name = _meta_term_close.group(1)
                if unregister_terminal(target_name):
                    deps.console.print(f"[dim yellow]- Terminal [bold]{target_name}[/bold] closed[/dim yellow]")
                    state["lastOutput"] = f"Closed terminal {target_name}"
                else:
                    deps.console.print(f"[yellow]Warning: terminal '{target_name}' not found[/yellow]")
                    state["lastOutput"] = f"Warning: terminal '{target_name}' not found"
                debug_entry.exec_command = command
                debug_entry.exec_returncode = 0
                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "close_session", "content": target_name})
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
                    "interactive_session_ref": [interactive_session],
                    "events_cb": events_cb, "pending_events_ref": [pending_events],
                    "get_terminal": get_terminal, "get_all_terminals": get_all_terminals,
                    "register_terminal": register_terminal,
                    "unregister_terminal": unregister_terminal,
                    "close_all_terminals": close_all_terminals,
                    "get_config": get_runtime_config,
                    "set_config": set_runtime_config,
                    "list_config": list_runtime_config,
                    "reset_config": reset_runtime_config,
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
                        state["lastOutput"] = cmd_output if cmd_output.strip() else "(no output)"
                        debug_entry.exec_command = command
                        debug_entry.exec_stdout = cmd_output
                        debug_entry.exec_returncode = sub.returncode
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
                # ── Normal command: start in sub-terminal (unnamed session) ──
                # Close any existing session first
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

                # depth 0: commands run through a child laintas terminal
                # depth > 0: system command → execute directly
                if depth == 0:
                    terminal_cmd = f"{sys.executable} {_LAINTAS_CLI} --execute {shlex.quote(command)} --depth 1"
                else:
                    terminal_cmd = command

                sub = deps.SubTerminalSession(terminal_cmd)
                sub.start()
                interactive_session = sub

                # Poll for output: wait up to 10s for first bytes
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

                debug_entry.exec_command = command
                debug_entry.session_command = command
                debug_entry.exec_stdout = cmd_output
                debug_entry.exec_returncode = -1
                state["lastOutput"] = cmd_output

                if events_cb is not None:
                    pending_events.append({"type": "system", "kind": "output", "content": cmd_output[:2000]})
                    events_cb(pending_events)
                    pending_events.clear()

                if events_cb is not None:
                    deps.display_sub_terminal_preview(command, cmd_output, depth=depth + 1, alive=sub.is_alive())

        # 10. Update state
        action_desc = command if command else ""
        state["shortTermMemory"] += \
            f"\n  -shortTermMemory Step: Replay=`{reply}` {action_desc} -> Result=\"{state['lastOutput']}\""

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

        # 12. Prepare next input (same pattern as AutonomousKernel.ts)
        terminals_snapshot = get_terminals_snapshot()
        terminals_block = f"\n\n[Named Sub-Terminals]\n{terminals_snapshot}" if terminals_snapshot else ""
        user_input = f"""[STATUS — Loop {loop + 1}/{max_loops}]
Current location: {os.getcwd()}
Last command output: {state['lastOutput']}{terminals_block}

Overall objective (this is the long-term goal, NOT a new instruction):
{original_input}

Your last response: {state['lastReply']}
→ Continue from here. What is the single next step?"""

    # Clean up session only when NOT managed by REPL (existing_session=None)
    # When REPL manages the session, it handles lifecycle externally.
    if existing_session is None and interactive_session is not None:
        interactive_session.close()

    if step_replies:
        return {"success": done, "msg": "\n\n".join(step_replies), "state": state,
                "session": interactive_session}
    return {"success": done, "msg": reply, "state": state,
            "session": interactive_session}
