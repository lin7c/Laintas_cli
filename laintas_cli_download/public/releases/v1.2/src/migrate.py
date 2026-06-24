"""
migrate.py -- Auto-migrate old laintas_cli files to the new unified layout.

Migration strategy:
    1. Copy old file/dir to new location (cross-filesystem safe)
    2. Rename old file/dir to <name>.bak (preserves data, avoids re-migration)
    3. Each item is independent — one failure doesn't block others
    4. Idempotent: skips if new path already exists or old path is gone

Old layout (pre-2026):
    ~/.laintas_cli_session.json     →  ~/.laintas/session.json
    ~/.laintas_cli_config.json      →  ~/.laintas/config.json
    ~/.laintas_cli_history          →  ~/.laintas/history
    ~/.laintas_cli_policy.json      →  ~/.laintas/policy.json
    ~/.laintas_cli_audit.log        →  ~/.laintas/audit.log
    ~/.laintas_cli_hooks.json       →  ~/.laintas/hooks.json
    ~/.laintas_cli_hooks.py         →  ~/.laintas/hooks.py
    ~/.laintas_cli_mcp.json         →  ~/.laintas/mcp.json
    ~/.laintas_cli_tasks.json       →  ~/.laintas/tasks.json
    ~/.laintas_cli_memory/          →  ~/.laintas/memory/
    ~/.laintas_cli_plans/           →  ~/.laintas/plans/
    ~/.laintas_cli_agents/          →  ~/.laintas/agents/
    ~/.laintas_cli_skills/          →  ~/.laintas/skills/

    <cwd>/.cli.prop                 →  <cwd>/.laintas/cli.prop
    <cwd>/.helpwo                   →  <cwd>/.laintas/memory.json
    <cwd>/.extra_command.py         →  <cwd>/.laintas/commands.py
    <cwd>/.loop_command.py          →  <cwd>/.laintas/loop.py
"""

import shutil
from pathlib import Path
from typing import Optional

import paths


# ── Home directory migration map ─────────────────────────────────────────

def _home_migrations() -> list[tuple[Path, Path, str]]:
    """Return list of (old_path, new_path, description) for home files."""
    home = Path.home()
    return [
        (home / ".laintas_cli_session.json", paths.SESSION_FILE, "session credentials"),
        (home / ".laintas_cli_config.json",  paths.CONFIG_FILE,  "global config"),
        (home / ".laintas_cli_history",      paths.HISTORY_FILE, "command history"),
        (home / ".laintas_cli_policy.json",  paths.POLICY_FILE,  "security policy"),
        (home / ".laintas_cli_audit.log",    paths.AUDIT_FILE,   "audit log"),
        (home / ".laintas_cli_hooks.json",   paths.HOOKS_FILE,   "hooks config"),
        (home / ".laintas_cli_hooks.py",     paths.PYTHON_HOOKS_FILE, "Python hooks"),
        (home / ".laintas_cli_mcp.json",     paths.MCP_FILE,     "MCP config"),
        (home / ".laintas_cli_tasks.json",   paths.TASKS_FILE,   "task list"),
        (home / ".laintas_cli_memory",       paths.MEMORY_DIR,   "memory directory"),
        (home / ".laintas_cli_plans",        paths.PLANS_DIR,    "plans directory"),
        (home / ".laintas_cli_agents",       paths.AGENTS_DIR,   "agents directory"),
        (home / ".laintas_cli_skills",       paths.SKILLS_DIR,   "skills directory"),
    ]


# ── CWD migration map ────────────────────────────────────────────────────

def _cwd_migrations() -> list[tuple[Path, Path, str]]:
    """Return list of (old_path, new_path, description) for cwd files."""
    cwd = Path.cwd()
    proj = paths.project_dir()
    result = []
    for old_name, new_name in paths._OLD_CWD_FILES.items():
        result.append((
            cwd / old_name,
            proj / new_name,
            f"project file {old_name}",
        ))
    return result


# ── Core migration logic ─────────────────────────────────────────────────

def _migrate_one(old: Path, new: Path, desc: str) -> Optional[str]:
    """Migrate a single file or directory.

    Returns:
        None        — skipped (old missing or new already exists)
        "migrated"  — successfully copied and backed up
        "error: …"  — failed with reason
    """
    if not old.exists():
        return None
    if new.exists():
        # For directories: if the new dir is empty (created by ensure_home),
        # remove it so we can copy the old content in its place.
        if new.is_dir() and not any(new.iterdir()):
            new.rmdir()
        else:
            return None

    try:
        # Ensure parent directory exists
        new.parent.mkdir(parents=True, exist_ok=True)

        if old.is_dir():
            shutil.copytree(str(old), str(new))
        else:
            shutil.copy2(str(old), str(new))

        # Preserve original permissions on the new copy
        try:
            shutil.copystat(str(old), str(new))
        except OSError:
            pass

        # Rename old to .bak (not delete — safety net)
        bak = old.with_suffix(old.suffix + ".bak")
        old.rename(bak)

        return "migrated"

    except Exception as e:
        return f"error: {e}"


# ── Public API ───────────────────────────────────────────────────────────

def migrate_all(verbose: bool = True) -> dict:
    """Run all migrations (home + cwd).

    Args:
        verbose: If True, print each migration action to stdout.

    Returns:
        dict with keys: migrated, skipped, errors — each a list of descriptions.
    """
    # Ensure the new home directory tree exists first
    paths.ensure_home()

    results = {"migrated": [], "skipped": [], "errors": []}

    all_migrations = _home_migrations() + _cwd_migrations()

    for old, new, desc in all_migrations:
        outcome = _migrate_one(old, new, desc)

        if outcome is None:
            results["skipped"].append(desc)
        elif outcome == "migrated":
            results["migrated"].append(desc)
            if verbose:
                print(f"  [migrate] {desc}: {old.name} → {new}")
        else:
            results["errors"].append(f"{desc}: {outcome}")
            if verbose:
                print(f"  [migrate] ERROR {desc}: {outcome}")

    # Summary
    if verbose and results["migrated"]:
        n = len(results["migrated"])
        print(f"  [migrate] Done — {n} item(s) migrated to {paths.LAINTAS_HOME}")

    return results


def needs_migration() -> bool:
    """Check if any old files exist that need migration."""
    for old, _, _ in _home_migrations():
        if old.exists():
            return True
    for old, _, _ in _cwd_migrations():
        if old.exists():
            return True
    return False
