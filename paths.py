"""
paths.py -- Centralized path management for laintas_cli.

All file and directory paths used by laintas_cli are defined here.
Other modules import from this module instead of constructing paths directly.

Layout:
    ~/.laintas/                          # Home configuration (LAINTAS_HOME env override)
    ├── config.json                      # Global settings
    ├── session.json                     # Authentication session
    ├── history                          # REPL command history
    ├── policy.json                      # Security policy rules
    ├── ppos_policy.json                 # PPOS autonomous-action policy and counters
    ├── audit.log                        # Command audit trail
    ├── hooks.json                       # Shell-based hook definitions
    ├── hooks.py                         # Python function hooks
    ├── mcp.json                         # MCP server configurations
    ├── tasks.json                       # Structured task list
    ├── messages_read.json               # Read receipts for the L> message list
    ├── memory/                          # Cross-session persistent memory
    │   ├── MEMORY.md                    # Memory index
    │   └── *.md                         # Individual memory files
    ├── plans/                           # Saved execution plans
    │   ├── _state.json                  # Plan mode state
    │   └── *.md                         # Plan files
    ├── agents/                          # Agent state persistence
    │   └── <agent_id>.json              # Per-agent state
    ├── prompts/                         # Prompt self-optimization
    │   ├── feedback.jsonl              # User feedback entries (append-only)
    │   ├── _state.json                  # Active optimization session state
    │   └── candidates/                  # Draft prompt patches (<id>.md)
    └── skills/                          # User-installed skills
        └── <name>/                      # Skill directories

    <cwd>/.laintas/                      # Per-project configuration
    ├── cli.prop                         # AI system prompt template
    ├── modes.json                       # Declarative custom agent modes
    ├── memory.json                      # Project-scoped structured memory
    ├── commands.py                      # User-defined custom slash commands
    └── loop.py                          # User-defined loop command interceptor
"""

import hashlib
import os
import stat
import uuid
from pathlib import Path


# ── Home Directory (global config) ───────────────────────────────────────

LAINTAS_HOME = Path(os.environ.get("LAINTAS_HOME", str(Path.home() / ".laintas")))


def _safe_instance_id(value: str) -> str:
    """Return a short filesystem/API-safe process or terminal id."""
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in value)
    safe = safe.strip(".-_")
    return safe[:64] or f"pid-{os.getpid()}-{uuid.uuid4().hex[:8]}"


# A process id must remain unique across restarts because the remote agent API
# uses it to distinguish registrations, heartbeats and unregister events.
PROCESS_INSTANCE_ID = _safe_instance_id(
    os.environ.get("LAINTAS_INSTANCE_ID")
    or f"pid-{os.getpid()}-{uuid.uuid4().hex[:8]}"
)


def _terminal_identity_source() -> str:
    """Return a stable source string for the current logical terminal.

    Terminal-emulator identifiers are preferred when available.  The tty and
    POSIX session id remain stable when the CLI exits and is relaunched from
    the same shell, while differing between concurrently open terminals.
    """
    explicit = os.environ.get("LAINTAS_TERMINAL_ID", "").strip()
    if explicit:
        return f"explicit:{explicit}"

    for name in (
        "TERM_SESSION_ID", "WT_SESSION", "TMUX_PANE", "WEZTERM_PANE",
        "KITTY_WINDOW_ID",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return f"env:{name}:{value}"

    try:
        tty_name = os.ttyname(0)
    except (AttributeError, OSError):
        # os.ttyname does not exist on Windows at all, so the AttributeError
        # matters as much as the OSError — without it this module cannot even
        # be imported there. os.getsid below already guards both.
        tty_name = ""
    try:
        session_id = str(os.getsid(0))
    except (AttributeError, OSError):
        session_id = ""
    if tty_name or session_id:
        return f"tty:{tty_name}|sid:{session_id}"

    # Non-interactive wrappers normally keep the same parent while repeatedly
    # launching the CLI.  This fallback is intentionally not global: unrelated
    # launchers must not share mutable terminal preferences.
    return f"parent:{os.getppid()}"


def _derive_terminal_id() -> str:
    explicit = os.environ.get("LAINTAS_TERMINAL_ID", "").strip()
    if explicit:
        return _safe_instance_id(explicit)
    digest = hashlib.sha256(_terminal_identity_source().encode("utf-8")).hexdigest()
    return f"term-{digest[:24]}"


# Stable for repeated CLI launches in one terminal, isolated across terminals.
TERMINAL_ID = _derive_terminal_id()


def child_terminal_id(name: str, parent: str = "term0") -> str:
    """Derive a stable, collision-resistant id for a CLI-created child PTY."""
    source = f"{TERMINAL_ID}\0{parent}\0{name}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"term-{digest[:24]}"


# Backward-compatible name for integrations that correctly need process-level
# identity.  Durable terminal state must use TERMINAL_ID explicitly.
INSTANCE_ID = PROCESS_INSTANCE_ID

# Flat files
CONFIG_FILE       = LAINTAS_HOME / "config.json"
SESSION_FILE      = LAINTAS_HOME / "session.json"
HISTORY_FILE      = LAINTAS_HOME / "history"
POLICY_FILE       = LAINTAS_HOME / "policy.json"
AUDIT_FILE        = LAINTAS_HOME / "audit.log"
HOOKS_FILE        = LAINTAS_HOME / "hooks.json"
PYTHON_HOOKS_FILE = LAINTAS_HOME / "hooks.py"
MCP_FILE          = LAINTAS_HOME / "mcp.json"
BACKENDS_FILE     = LAINTAS_HOME / "backends.json"
PPOS_POLICY_FILE  = LAINTAS_HOME / "ppos_policy.json"
TRUST_FILE        = LAINTAS_HOME / "trust.json"
TASKS_FILE        = LAINTAS_HOME / "tasks.json"
MESSAGES_READ_FILE = LAINTAS_HOME / "messages_read.json"
INTERACTIVE_COMMANDS_FILE = LAINTAS_HOME / "interactive_commands.json"

# Subdirectories
MEMORY_DIR    = LAINTAS_HOME / "memory"
MEMORY_INDEX  = MEMORY_DIR / "MEMORY.md"
PLANS_DIR     = LAINTAS_HOME / "plans"
PLANS_STATE   = PLANS_DIR / "_state.json"
AGENTS_DIR    = LAINTAS_HOME / "agents"
SKILLS_DIR    = LAINTAS_HOME / "skills"
SESSIONS_DIR  = LAINTAS_HOME / "sessions"
INSTANCES_DIR = LAINTAS_HOME / "instances"   # cross-instance peer registry (0700)
WRITES_DIR    = LAINTAS_HOME / "writes"      # cross-instance write log (0700)
SESSION_LOCKS_DIR = LAINTAS_HOME / "session_locks"  # per-session ownership leases (0700)

# Prompt self-optimization: feedback log + candidate drafts + applied-patch state.
# Lives under ~/.laintas/ (NOT per-cwd) so it survives /reload (which only wipes
# the 4 files in _ALL_CWD_FILES under <cwd>/.laintas/).
PROMPTS_DIR          = LAINTAS_HOME / "prompts"
PROMPT_CANDIDATES_DIR = PROMPTS_DIR / "candidates"
PROMPT_FEEDBACK_LOG  = PROMPTS_DIR / "feedback.jsonl"
PROMPT_OPT_STATE     = PROMPTS_DIR / "_state.json"


# ── Per-Project Directory (cwd-scoped) ───────────────────────────────────

_PROJECT_SUBDIR = ".laintas"

# File names within the project directory
CWD_CLI_PROP = "cli.prop"
CWD_MEMORY   = "memory.json"
CWD_COMMANDS  = "commands.py"
CWD_LOOP      = "loop.py"
CWD_RULES     = "rules.json"

# /reload resets generated runtime customization only. Durable user rules are
# explicit project state and must survive reloads just like WorkGraph data.
_ALL_CWD_FILES = (CWD_CLI_PROP, CWD_MEMORY, CWD_COMMANDS, CWD_LOOP)

# Old names (for migration and backward-compat detection)
_OLD_CWD_FILES = {
    ".cli.prop":         CWD_CLI_PROP,
    ".helpwo":           CWD_MEMORY,
    ".extra_command.py": CWD_COMMANDS,
    ".loop_command.py":  CWD_LOOP,
}


def project_dir() -> Path:
    """Return the .laintas/ directory in the current working directory.

    This is a function (not a constant) because os.chdir() can change
    the cwd during a session.
    """
    return Path.cwd() / _PROJECT_SUBDIR


def project_file(name: str) -> Path:
    """Return a specific file inside the project .laintas/ directory.

    Example: project_file("cli.prop") → Path("<cwd>/.laintas/cli.prop")
    """
    return project_dir() / name


def extensions_dir() -> Path:
    """Project-local, user-created runtime extensions."""
    return project_dir() / "extensions"


def global_extensions_dir() -> Path:
    """Machine-wide extensions, loaded from every working directory.

    Project-local extensions belong to one tree; an extension that governs the
    account — the Enterprise organisation package is the reason this exists —
    must not stop applying because the user cd'd somewhere else.
    """
    return LAINTAS_HOME / "extensions"


def evolution_lab_dir() -> Path:
    """Project-local Evolution Lab branches, candidates, tests and history."""
    return project_dir() / "evolution-lab"


# ── Initialization ───────────────────────────────────────────────────────

def _home_owner_ok() -> bool:
    """Return True if LAINTAS_HOME (or its symlink target) is safe to use.

    A symlinked or otherwise attacker-controlled LAINTAS_HOME would let a
    less-privileged user redirect auth tokens (session.json), audit logs,
    and policy rules to an arbitrary location. Refuse quietly here; the
    fallback paths in callers already degrade to in-memory defaults when
    writes fail.
    """
    try:
        resolved = LAINTAS_HOME.resolve(strict=False)
    except OSError:
        return True  # let mkdir/chmod below surface the real error
    try:
        if hasattr(os, "getuid"):
            rstat = resolved.stat()
            if rstat.st_uid not in (os.getuid(), 0):
                import sys
                print(f"[paths] WARNING: LAINTAS_HOME resolves to {resolved} "
                      f"(uid={rstat.st_uid}), not owned by you "
                      f"(uid={os.getuid()}); refusing to use it.",
                      file=sys.stderr)
                return False
    except OSError:
        pass
    return True


def ensure_home() -> None:
    """Create ~/.laintas/ and all subdirectories with correct permissions.

    Safe to call multiple times (uses exist_ok=True).
    Sets 0o700 on the root directory for privacy.
    """
    if not _home_owner_ok():
        raise RuntimeError(
            f"LAINTAS_HOME ({LAINTAS_HOME}) resolves to a directory not "
            f"owned by the current user; refusing to write secrets there. "
            f"Set LAINTAS_HOME to a safe path or fix ownership."
        )
    LAINTAS_HOME.mkdir(parents=True, exist_ok=True)
    # chmod the real directory (follows symlinks by default, but we already
    # verified ownership above so this is safe).
    try:
        os.chmod(str(LAINTAS_HOME), 0o700)
    except OSError:
        pass
    for d in (MEMORY_DIR, PLANS_DIR, AGENTS_DIR, SKILLS_DIR, SESSIONS_DIR,
              PROMPTS_DIR, PROMPT_CANDIDATES_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(d), 0o700)
        except OSError:
            pass
    for private_file in (
        CONFIG_FILE, SESSION_FILE, POLICY_FILE, PPOS_POLICY_FILE, HOOKS_FILE, PYTHON_HOOKS_FILE,
        MCP_FILE, BACKENDS_FILE, TRUST_FILE, INTERACTIVE_COMMANDS_FILE,
        MESSAGES_READ_FILE,
    ):
        ensure_private_file(private_file)


def ensure_private_file(path: Path) -> bool:
    """Apply private permissions without following attacker-controlled symlinks."""
    try:
        if not path.exists():
            return True
        if path.is_symlink():
            return False
        if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
            return False
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return True
    except OSError:
        return False


def ensure_project_dir() -> Path:
    """Create <cwd>/.laintas/ if it doesn't exist. Returns the directory path."""
    d = project_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        if not d.is_symlink() and (
                not hasattr(os, "getuid") or d.stat().st_uid == os.getuid()):
            d.chmod(stat.S_IRWXU)
    except OSError:
        pass
    return d
