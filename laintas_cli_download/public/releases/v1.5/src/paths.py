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
    ├── audit.log                        # Command audit trail
    ├── hooks.json                       # Shell-based hook definitions
    ├── hooks.py                         # Python function hooks
    ├── mcp.json                         # MCP server configurations
    ├── tasks.json                       # Structured task list
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

import os
import stat
from pathlib import Path


# ── Home Directory (global config) ───────────────────────────────────────

LAINTAS_HOME = Path(os.environ.get("LAINTAS_HOME", str(Path.home() / ".laintas")))

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
TRUST_FILE        = LAINTAS_HOME / "trust.json"
TASKS_FILE        = LAINTAS_HOME / "tasks.json"
INTERACTIVE_COMMANDS_FILE = LAINTAS_HOME / "interactive_commands.json"

# Subdirectories
MEMORY_DIR    = LAINTAS_HOME / "memory"
MEMORY_INDEX  = MEMORY_DIR / "MEMORY.md"
PLANS_DIR     = LAINTAS_HOME / "plans"
PLANS_STATE   = PLANS_DIR / "_state.json"
AGENTS_DIR    = LAINTAS_HOME / "agents"
SKILLS_DIR    = LAINTAS_HOME / "skills"
SESSIONS_DIR  = LAINTAS_HOME / "sessions"

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


def evolution_lab_dir() -> Path:
    """Project-local Evolution Lab branches, candidates, tests and history."""
    return project_dir() / "evolution-lab"


# ── Initialization ───────────────────────────────────────────────────────

def ensure_home() -> None:
    """Create ~/.laintas/ and all subdirectories with correct permissions.

    Safe to call multiple times (uses exist_ok=True).
    Sets 0o700 on the root directory for privacy.
    """
    LAINTAS_HOME.mkdir(parents=True, exist_ok=True)
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
        CONFIG_FILE, SESSION_FILE, POLICY_FILE, HOOKS_FILE, PYTHON_HOOKS_FILE,
        MCP_FILE, BACKENDS_FILE, TRUST_FILE, INTERACTIVE_COMMANDS_FILE,
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
    return d
