"""
app_host.py -- Applications hosted in their own sub-terminal.

An application (Helpwo, or anything registered with /app) runs in a dedicated
sub-terminal: a nested laintas_cli process whose primary agent serves only
that application. The main terminal's conversation is never the one an
application talks to, and closing the sub-terminal ends the application, its
agent and any process it started, together.

This module is the part both commands share and that needs no REPL:

  * per-workspace launch state (token, port, agent id, conversation id) —
    stable when the application persists, fresh when it does not;
  * the runtime handshake file the nested process writes once it is serving,
    which is how the parent terminal learns the URL without a socket;
  * manifests for /app and the trust records that gate them;
  * the prompt section that tells the nested agent whom it serves;
  * the application's own child process, if the manifest starts one.

Helpwo is deliberately not a manifest. It is its own product with its own
command; it only borrows the launch/state machinery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import json_store
import paths


HELPWO_APP = "helpwo"
# Names an /app manifest may not take: "helpwo" has its own command, and
# term0 is the CLI's own terminal.
RESERVED_APP_NAMES = frozenset({HELPWO_APP, "term0"})
APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

PERSISTENCE_NONE = "none"
PERSISTENCE_WORKSPACE = "workspace"
PERSISTENCE_MODES = (PERSISTENCE_NONE, PERSISTENCE_WORKSPACE)

# A trusted application's token controls its runtime, like the Helpwo token.
SESSION_ALLOWED_KINDS = frozenset({
    "chat", "abort", "approval-response", "exec", "query", "delegate",
    "webtest", "analyze_site", "term-open", "term-new", "term-close",
    "disconnect", "rtc-offer", "rtc-ice", "rtc-close",
})
APP_ALLOWED_KINDS = SESSION_ALLOWED_KINDS | {"session-open", "session-close", "session-list"}

# One end user's identity as the application names it. The application owns
# its users; the CLI only needs a stable, path-safe key.
USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Tools a session agent always has: without them it cannot finish a turn.
SESSION_BASE_TOOLS = frozenset({"task.complete", "time.now"})
SESSION_DEFAULT_TOOLS = ("shell.exec",)
MAX_SESSIONS_LIMIT = 200

# Prefix every app terminal's registry command carries, so /app and /helpwo can
# tell their own sub-terminal from a user's /term of the same name.
TERMINAL_COMMAND_PREFIX = "laintas-cli app:"

_MAX_PROMPT_CHARS = 8000
_MAX_DESCRIPTION_CHARS = 300
_MAX_COMMAND_CHARS = 2000


# ── directories ─────────────────────────────────────────────────────────

def _home() -> Path:
    # Read at call time: paths.LAINTAS_HOME may be redirected after import.
    return Path(paths.LAINTAS_HOME)


def state_root() -> Path:
    return _home() / "app-state"


def user_manifest_dir() -> Path:
    return _home() / "apps"


def project_manifest_dir(cwd: str) -> Path:
    return Path(cwd) / ".laintas" / "apps"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def terminal_command(app: str) -> str:
    return f"{TERMINAL_COMMAND_PREFIX}{app}"


def is_app_terminal(info, app: str) -> bool:
    """True when a registry TerminalInfo is this application's sub-terminal."""
    return str(getattr(info, "command", "") or "") == terminal_command(app)


# ── launch state ────────────────────────────────────────────────────────

def workspace_key(root: str) -> str:
    real = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]


def state_dir(app: str, root: str) -> Path:
    return state_root() / app / workspace_key(root)


def _state_file(directory: Path) -> Path:
    return directory / "state.json"


def _runtime_file(directory: Path) -> Path:
    return directory / "runtime.json"


def load_state(directory: Path) -> dict:
    data = json_store.load_json(_state_file(Path(directory)), dict)
    return data if isinstance(data, dict) else {}


def save_state(directory: Path, data: dict) -> None:
    directory = Path(directory)
    _ensure_private_dir(directory)
    json_store.save_json_atomic(_state_file(directory), data, mode=0o600)


def ensure_state(app: str, root: str, persistent: bool) -> tuple[Path, dict]:
    """Return (state dir, launch state) for one application in one folder.

    Persistent: everything the browser and the agent key their data by stays
    the same across launches — the token (so the cookie keeps working), the
    port (so the browser origin, and with it IndexedDB, is the same), the
    bridge agent id (Helpwo stores its conversations under it) and the
    conversation id the nested agent saves to.

    Not persistent: the same shape, regenerated on every launch.
    """
    directory = state_dir(app, root)
    key = workspace_key(root)
    previous = load_state(directory) if persistent else {}
    if previous.get("persistent") is not True:
        previous = {}
    suffix = key if persistent else f"{key}-{secrets.token_hex(4)}"
    state = {
        "app": app,
        "root": os.path.realpath(os.path.abspath(root)),
        "persistent": bool(persistent),
        "token": previous.get("token") or secrets.token_urlsafe(32),
        "port": previous.get("port"),
        "local_agent_id": previous.get("local_agent_id") or f"local-{app}-{suffix}",
        "conversation_id": previous.get("conversation_id") or f"app-{app}-{suffix}",
        "terminal_id": previous.get("terminal_id") or f"app-{app}-{suffix}",
        "remote_agent_id": previous.get("remote_agent_id") or "",
        "created_at": previous.get("created_at") or time.time(),
    }
    save_state(directory, state)
    return directory, state


def update_state(directory: Path, **fields) -> dict:
    data = load_state(directory)
    data.update(fields)
    save_state(directory, data)
    return data


# ── runtime handshake (nested process → parent terminal) ────────────────

def write_runtime(directory: Path, launch_id: str, **fields) -> None:
    directory = Path(directory)
    _ensure_private_dir(directory)
    payload = {"launch_id": launch_id, "pid": os.getpid(),
               "updated_at": time.time()}
    payload.update(fields)
    json_store.save_json_atomic(_runtime_file(directory), payload, mode=0o600)


def read_runtime(directory: Path) -> dict:
    data = json_store.load_json(_runtime_file(Path(directory)), dict)
    return data if isinstance(data, dict) else {}


def clear_runtime(directory: Path) -> None:
    try:
        _runtime_file(Path(directory)).unlink()
    except OSError:
        pass


def wait_runtime(directory: Path, launch_id: str, timeout: float = 45.0,
                 alive: Optional[Callable[[], bool]] = None,
                 poll: float = 0.25) -> dict:
    """Wait for the nested process of THIS launch to report ready or error.

    A runtime file from an earlier launch carries a different launch_id and is
    ignored, so a stale "ready" can never be mistaken for the new process.
    Returns the runtime dict, or {"status": "exited"|"timeout"}.
    """
    deadline = time.monotonic() + timeout
    while True:
        data = read_runtime(directory)
        if (data.get("launch_id") == launch_id
                and data.get("status") in ("ready", "error")):
            return data
        if alive is not None and not alive():
            return {"status": "exited"}
        if time.monotonic() >= deadline:
            return {"status": "timeout"}
        time.sleep(poll)


# ── manifests ───────────────────────────────────────────────────────────

@dataclass
class AppManifest:
    name: str
    description: str = ""
    command: str = ""
    prompt: str = ""
    persistence: str = PERSISTENCE_NONE
    port: Optional[int] = None
    session_tools: list = field(default_factory=lambda: list(SESSION_DEFAULT_TOOLS))
    auto_approve: bool = False
    max_sessions: int = 10
    session_idle_minutes: int = 30
    agent: dict = field(default_factory=dict)
    agents: list = field(default_factory=list)
    terminals: list = field(default_factory=list)
    scope: str = "user"          # user | project
    source: str = ""
    raw: dict = field(default_factory=dict)

    def digest(self) -> str:
        """Identity trust is granted to: the content AND where it lives.

        A project that ships a manifest with a trusted name but different
        content, or the same content from another folder, is a different
        application and must be trusted on its own.
        """
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        material = f"{os.path.realpath(self.source)}\0{canonical}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_manifest(data, source: str, scope: str = "user"
                   ) -> tuple[Optional[AppManifest], str]:
    """Validate one manifest. Returns (manifest, "") or (None, reason)."""
    if not isinstance(data, dict):
        return None, "manifest must be a JSON object"
    unknown = set(data) - {"name", "description", "command", "prompt",
                           "persistence", "port", "session_tools",
                           "auto_approve", "max_sessions",
                           "session_idle_minutes", "agent", "agents", "terminals"}
    if unknown:
        return None, f"unknown field(s): {', '.join(sorted(unknown))}"
    name = data.get("name")
    if not isinstance(name, str) or not APP_NAME_RE.fullmatch(name):
        return None, "name must match [a-z0-9][a-z0-9._-]{0,31}"
    if name in RESERVED_APP_NAMES:
        return None, f"'{name}' is reserved"
    description = data.get("description", "")
    command = data.get("command", "")
    prompt = data.get("prompt", "")
    for label, value, limit in (("description", description, _MAX_DESCRIPTION_CHARS),
                                ("command", command, _MAX_COMMAND_CHARS),
                                ("prompt", prompt, _MAX_PROMPT_CHARS)):
        if not isinstance(value, str):
            return None, f"{label} must be a string"
        if len(value) > limit:
            return None, f"{label} is longer than {limit} characters"
    persistence = data.get("persistence", PERSISTENCE_NONE)
    if persistence not in PERSISTENCE_MODES:
        return None, f"persistence must be one of {', '.join(PERSISTENCE_MODES)}"
    port = data.get("port")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool)
                             or not 1 <= port <= 65535):
        return None, "port must be an integer 1-65535"
    session_tools = data.get("session_tools", list(SESSION_DEFAULT_TOOLS))
    if (not isinstance(session_tools, list)
            or not all(isinstance(item, str) for item in session_tools)):
        return None, "session_tools must be a list of tool names"
    reason = validate_tool_names(session_tools)
    if reason:
        return None, f"session_tools: {reason}"
    reason = validate_topology(data)
    if reason:
        return None, reason
    auto_approve = data.get("auto_approve", False)
    if not isinstance(auto_approve, bool):
        return None, "auto_approve must be true or false"
    max_sessions = data.get("max_sessions", 10)
    if (not isinstance(max_sessions, int) or isinstance(max_sessions, bool)
            or not 1 <= max_sessions <= MAX_SESSIONS_LIMIT):
        return None, f"max_sessions must be an integer 1-{MAX_SESSIONS_LIMIT}"
    idle = data.get("session_idle_minutes", 30)
    if (not isinstance(idle, int) or isinstance(idle, bool)
            or not 0 <= idle <= 7 * 24 * 60):
        return None, "session_idle_minutes must be an integer 0-10080 (0 = never)"
    return AppManifest(
        name=name, description=description.strip(), command=command.strip(),
        prompt=prompt.strip(), persistence=persistence, port=port,
        session_tools=list(dict.fromkeys(session_tools)),
        auto_approve=auto_approve, max_sessions=max_sessions,
        session_idle_minutes=idle,
        agent=data.get("agent", {}), agents=data.get("agents", []),
        terminals=data.get("terminals", []),
        scope=scope, source=str(source), raw=dict(data),
    ), ""


def validate_tool_names(value):
    """Use the actual catalogue rather than a second, drifting whitelist."""
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        return "must be an array of tool names"
    if value == ["*"]:
        return ""
    from tools import get_registry
    unknown = set(value) - {tool.name for tool in get_registry().list()}
    return f"unknown tools: {', '.join(sorted(unknown))}" if unknown else ""


def validate_topology(data):
    agent = data.get("agent", {})
    agents, terminals = data.get("agents", []), data.get("terminals", [])
    if not isinstance(agent, dict):
        return "agent must be an object"
    for label, items in (("agents", agents), ("terminals", terminals)):
        if not isinstance(items, list) or len(items) > 200 or not all(isinstance(x, dict) for x in items):
            return f"{label} must be an array of at most 200 objects"
        names = [x.get("name") for x in items]
        if any(not isinstance(x, str) or not USER_ID_RE.fullmatch(x)
               or x in {"primary", "term0", ".", "..", "current", "here"} for x in names):
            return f"{label}: invalid or reserved name"
        if len(set(names)) != len(names):
            return f"{label}: duplicate name"
    terminal_names = {x["name"] for x in terminals}
    occupied = set()
    parents = {"primary"}
    profile_fields = {"prompt", "profile", "title", "description", "model", "provider",
                      "tools", "denied_tools", "capability_tags"}
    for index, spec in enumerate([agent] + agents):
        extra = set(spec) - (profile_fields | ({"name", "parent", "terminal"} if index else set()))
        if extra:
            return f"agent: unknown fields: {', '.join(sorted(extra))}"
        for key in profile_fields - {"tools", "denied_tools", "capability_tags"}:
            if key in spec and (not isinstance(spec[key], str) or len(spec[key]) > _MAX_PROMPT_CHARS):
                return f"agent.{key} must be a string of at most {_MAX_PROMPT_CHARS} characters"
        for key in ("tools", "denied_tools"):
            if key in spec:
                reason = validate_tool_names(spec[key])
                if reason or (key == "denied_tools" and "*" in spec[key]):
                    return f"agent.{key}: {reason or 'list individual denied tools'}"
        tags = spec.get("capability_tags", [])
        if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
            return "agent.capability_tags must be an array of strings"
        if spec.get("profile"):
            import agent_roles
            if agent_roles.get_role(spec["profile"]) is None:
                return f"unknown agent profile: {spec['profile']}"
        if index:
            parent = spec.get("parent", "primary")
            if not isinstance(parent, str) or parent not in parents:
                return "agent.parent must name primary or an earlier agent"
            parents.add(spec["name"])
            terminal = spec.get("terminal")
            if terminal is not None:
                if not isinstance(terminal, str) or terminal not in terminal_names:
                    return "agent.terminal must name a declared terminal"
                if terminal in occupied:
                    return "only one agent may be stationed in each terminal"
                occupied.add(terminal)
    for spec in terminals:
        if set(spec) - {"name", "command", "cwd"}:
            return "terminal fields are name, command, cwd"
        for key in ("command", "cwd"):
            if key in spec and (not isinstance(spec[key], str) or not spec[key].strip()):
                return f"terminal.{key} must be a non-empty string"
    return ""


def configure_runtime(manifest, runtime, create_terminal, *, session=False):
    """Materialize the manifest through the same registry/deployment as /station.

    Return a rollback callback for startup failures. No assignments are started.
    """
    from copy import deepcopy
    from station_service import service_for
    import agent_roles
    primary = runtime.get_agent("primary")
    if primary is None:
        raise ValueError("no primary agent in the application")
    old = (deepcopy(primary.profile), primary.base_model, primary.base_provider)
    created_agents, created_terminals = [], []

    def rollback():
        for name in reversed(created_agents):
            runtime.unregister_agent(name)
        for name in reversed(created_terminals):
            runtime.unregister_terminal(name)
        primary.profile, primary.base_model, primary.base_provider = old

    def configure(info, spec, fallback=None):
        role = agent_roles.get_role(spec.get("profile")) if spec.get("profile") else None
        allowed = spec.get("tools", list(role.allowed_tools) if role and role.allowed_tools else fallback)
        if allowed == ["*"]:
            allowed = None
        if session and allowed is not None:
            allowed = sorted(set(allowed) | SESSION_BASE_TOOLS)
        info.profile = runtime.EmployeeProfile(
            title=spec.get("title", role.name if role else "General Agent"),
            description=spec.get("description", role.description if role else ""),
            specialist_role=spec.get("profile"), prompt=spec.get("prompt", ""),
            capability_tags=spec.get("capability_tags", [role.name] if role else []),
            tool_policy=runtime.AgentToolPolicy(allowed_tools=allowed,
                                               denied_tools=spec.get("denied_tools", [])))
        info.base_model = spec.get("model", "")
        info.base_provider = spec.get("provider", "")

    try:
        configure(primary, manifest.agent, manifest.session_tools if session else None)
        for spec in manifest.terminals:
            name = spec["name"]
            if runtime.get_terminal(name) is not None:
                raise ValueError(f"terminal '{name}' already exists")
            sub = create_terminal(spec)
            try:
                if not sub.is_alive():
                    raise RuntimeError(f"terminal '{name}' did not start")
                runtime.register_terminal(sub, sub.command, primary.depth + 1,
                                          name=name, parent_terminal="term0")
            except BaseException:
                sub.close()
                raise
            created_terminals.append(name)
        for spec in manifest.agents:
            parent = runtime.get_agent(spec.get("parent", "primary"))
            info = runtime.register_agent(name=spec["name"], parent_id=parent.id,
                                          depth=parent.depth + 1, replace_existing=False)
            created_agents.append(info.id)
            configure(info, spec, parent.profile.tool_policy.allowed_tools)
            if spec.get("terminal"):
                result = service_for(runtime).deploy(info.id, spec["terminal"],
                    owner_id=parent.id, create_terminal=lambda name: None)
                if not result.ok:
                    raise RuntimeError(result.message)
        return rollback
    except BaseException:
        rollback()
        raise


def discover_manifests(cwd: str) -> tuple[dict[str, AppManifest], list[str]]:
    """All manifests visible from cwd, by name. Project entries shadow user ones.

    Returns (manifests, problems) — a broken file is reported, never fatal.
    """
    found: dict[str, AppManifest] = {}
    problems: list[str] = []
    for scope, directory in (("user", user_manifest_dir()),
                             ("project", project_manifest_dir(cwd))):
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError) as exc:
                problems.append(f"{path}: {exc}")
                continue
            manifest, reason = parse_manifest(data, str(path), scope)
            if manifest is None:
                problems.append(f"{path}: {reason}")
                continue
            found[manifest.name] = manifest
    return found, problems


# ── per-user sessions ───────────────────────────────────────────────────

def session_terminal_name(app: str, user: str) -> str:
    return f"{app}.u.{user}"


def session_dirs(app_state_dir: Path, user: str, persistent: bool) -> tuple[Path, Path]:
    """(home, work) for one user's session sub-terminal.

    A separate LAINTAS_HOME is the isolation: memory, durable rules, skills,
    agent files, sessions and event logs all live under the home or the working
    directory, so nothing one user's agent learns or writes is loaded into
    another's, and nothing of the operator's is loaded into either. A
    persistent application keeps the folder; otherwise it is thrown away when
    the session closes.
    """
    base = Path(app_state_dir) / ("users" if persistent else "sessions")
    leaf = user if persistent else f"{user}-{secrets.token_hex(4)}"
    root = base / leaf
    home, work = root / "home", root / "work"
    for directory in (root, home, work):
        _ensure_private_dir(directory)
    return home, work


def link_credentials(home: Path) -> None:
    """Let the session reach the model backend as the operator's account.

    Only the login and backend profiles are shared — they decide who is billed,
    which is the operator's (and the application's) business, not the user's.
    Nothing else from the operator's home is visible to a session.
    """
    for name in ("session.json", "backends.json"):
        source = _home() / name
        target = Path(home) / name
        if not source.exists() or target.exists() or target.is_symlink():
            continue
        try:
            target.symlink_to(source)
        except OSError:
            pass


def remove_session_dir(home: Path) -> None:
    import shutil
    root = Path(home).parent
    if root.parent.name != "sessions":
        return  # persistent sessions keep their folder
    shutil.rmtree(root, ignore_errors=True)


# ── trust ───────────────────────────────────────────────────────────────

def _trust_file() -> Path:
    return state_root() / "trust.json"


def _load_trust() -> dict:
    data = json_store.load_json(_trust_file(), dict)
    return data if isinstance(data, dict) else {}


def is_trusted(manifest: AppManifest) -> bool:
    record = _load_trust().get(manifest.name)
    return isinstance(record, dict) and record.get("digest") == manifest.digest()


def trust(manifest: AppManifest) -> None:
    records = _load_trust()
    records[manifest.name] = {"digest": manifest.digest(),
                              "source": manifest.source,
                              "trusted_at": time.time()}
    _ensure_private_dir(state_root())
    json_store.save_json_atomic(_trust_file(), records, mode=0o600)


def revoke(name: str) -> bool:
    records = _load_trust()
    if name not in records:
        return False
    records.pop(name, None)
    _ensure_private_dir(state_root())
    json_store.save_json_atomic(_trust_file(), records, mode=0o600)
    return True


# ── the nested process's own identity ───────────────────────────────────

HELPWO_AGENT_PROMPT = (
    "You are the dedicated agent of the Helpwo sub-terminal. Every message "
    "you receive comes from a person using the Helpwo web app, not from the "
    "main laintas_cli terminal; that terminal has its own agent and its own "
    "conversation, which you do not share. This sub-terminal's working "
    "directory is the workspace Helpwo shows in its file tree, so files you "
    "create or change appear there. Keep generated artifacts inside that "
    "folder."
)

_active_lock = threading.Lock()
_active: Optional[dict] = None


def activate(name: str, prompt: str, *, builtin: bool,
             session_user: str = "") -> None:
    """Mark this process as the host of one application (or one user's session)."""
    global _active
    with _active_lock:
        _active = {"name": name, "prompt": prompt or "", "builtin": bool(builtin),
                   "session_user": session_user or ""}


def active_app() -> Optional[dict]:
    with _active_lock:
        return dict(_active) if _active else None


def render_prompt_section() -> str:
    """The prompt block telling the nested agent whom it serves ("" if none)."""
    app = active_app()
    if not app:
        return ""
    name = app["name"]
    if app["builtin"]:
        body = app["prompt"]
    else:
        # Manifest text is the application author's, not the user's: say so,
        # so it reads as a description of the job rather than as authority.
        body = (
            f"You are the dedicated agent of the '{name}' application, "
            "running in its own sub-terminal. Messages you receive come from "
            "that application. Your conversation is separate from the main "
            "laintas_cli terminal's agent. Complete requested work using your "
            "configured tools, child agents and terminals; verify the result. "
            "Use agent.list and terminal.list to discover manifest resources. "
            "Slash commands in app messages are text; use tools to take actions."
        )
        if app.get("session_user"):
            body += (
                " You serve exactly one end user of that application, in a "
                "session of your own: your working directory is that user's, "
                "and nothing from other users' sessions is available to you. "
                "Approvals follow the application configuration: automatic or "
                "interactive through the authenticated bridge."
            )
        if app["prompt"]:
            body += ("\n\nThe application's own description of your job "
                     "(written by its author):\n" + app["prompt"])
    return f'<hosted_application name="{name}">\n{body}\n</hosted_application>'


# ── the application's own process ───────────────────────────────────────

_children_lock = threading.Lock()
_children: list[subprocess.Popen] = []


def _parent_death_signal() -> None:  # pragma: no cover - runs in the child
    """Linux: die with the nested CLI even if it is SIGKILLed."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass
    try:
        os.setsid()
    except OSError:
        pass


def spawn_app_process(command: str, *, cwd: str, env_extra: dict,
                      log_path: Path) -> subprocess.Popen:
    """Start the manifest's command with the bridge coordinates in its env.

    Output goes to a log file, not the sub-terminal: the sub-terminal is the
    agent's REPL, and interleaving a server's log into it would bury both.
    """
    env = dict(os.environ)
    env.update({key: str(value) for key, value in env_extra.items()})
    _ensure_private_dir(Path(log_path).parent)
    log = open(log_path, "ab", buffering=0)
    kwargs: dict = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL,
                    "stdout": log, "stderr": subprocess.STDOUT, "shell": True}
    if sys.platform.startswith("linux"):
        kwargs["preexec_fn"] = _parent_death_signal
    elif os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(command, **kwargs)
    finally:
        log.close()  # the child holds its own descriptor
    with _children_lock:
        _children.append(proc)
    return proc


def stop_app_processes(timeout: float = 3.0) -> None:
    """Terminate every process this module started (whole process groups)."""
    with _children_lock:
        procs = list(_children)
        _children.clear()
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            pass
    deadline = time.monotonic() + timeout
    for proc in procs:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
            continue
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass
