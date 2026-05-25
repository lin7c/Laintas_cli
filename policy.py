"""
Security policy engine for laintas_cli.

Evaluates every command before execution against a configurable rule set.
Three-tier decision model:
  - allow: pass through immediately
  - needs_approval: pause and wait for user confirmation
  - deny: block execution entirely

Config: ~/.laintas_cli_policy.json (auto-created with safe defaults on first load)
Audit log: ~/.laintas_cli_audit.log (JSONL, one line per decision)

Reloads config on mtime change, like .extra_command.py — zero-restart updates.
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CONFIG_PATH = Path.home() / ".laintas_cli_policy.json"
AUDIT_PATH = Path.home() / ".laintas_cli_audit.log"

# ── Default safe policy ──────────────────────────────────────────────────
_DEFAULT_CONFIG = {
    "mode": "audit",  # "audit" | "enforce" | "disabled"
    "allow": [
        r"^ls(\s|$)", r"^dir(\s|$)", r"^pwd$", r"^cd(\s|$)",
        r"^cat\s", r"^head\s", r"^tail\s", r"^less\s",
        r"^grep\s", r"^find\s", r"^which\s", r"^type\s",
        r"^echo\s", r"^printf\s", r"^date$", r"^whoami$",
        r"^id$", r"^uname\s", r"^hostname$", r"^env$",
        r"^printenv", r"^wc\s", r"^sort\s", r"^uniq\s",
        r"^cut\s", r"^tr\s", r"^awk\s", r"^sed\s",
        r"^diff\s", r"^cmp\s", r"^file\s", r"^stat\s",
        r"^du\s", r"^df\s", r"^free\s", r"^top\s",
        r"^ps\s", r"^pgrep\s", r"^kill\s+-l", r"^jobs$",
        r"^fg\s", r"^bg\s", r"^history", r"^alias\s",
        r"^man\s", r"^info\s", r"^whatis\s",
        r"^python", r"^node\s", r"^npm\s", r"^npx\s",
        r"^pip\s", r"^pip3\s", r"^cargo\s", r"^go\s",
        r"^git\s+status", r"^git\s+log", r"^git\s+diff",
        r"^git\s+branch", r"^git\s+stash", r"^git\s+remote",
        r"^curl\s", r"^wget\s", r"^tar\s", r"^zip\s",
        r"^unzip\s", r"^gzip\s", r"^gunzip\s",
        r"^docker\s+ps", r"^docker\s+images", r"^docker\s+logs",
        r"^make\s", r"^cmake\s", r"^gcc\s", r"^g\+\+\s",
        r"^clang", r"^rustc\s", r"^javac\s",
        r"^ssh\s", r"^scp\s", r"^rsync\s",
        r"^systemctl\s+status", r"^journalctl\s",
        r"^apt\s+list", r"^apt\s+search", r"^apt-cache\s",
        r"^dpkg\s+-l", r"^rpm\s+-q",
        r"^mount$", r"^df\s", r"^lsblk$", r"^blkid$",
        r"^ip\s", r"^ifconfig$", r"^netstat\s", r"^ss\s",
        r"^ping\s", r"^traceroute\s", r"^nslookup\s", r"^dig\s",
        r"^tmux\s", r"^screen\s",
        # Meta-commands are always safe (handled before PTY)
        r"^/(?:term|t|session|keys|station|terminate|send|hire|agents|spawn|tell|wait|abort|tool|config|reload|scan|clear|debug|memory|prop|cwd|help|login|name|exit|quit)\b",
    ],
    "needs_approval": [
        r"^git\s+push", r"^git\s+commit", r"^git\s+reset",
        r"^git\s+rebase", r"^git\s+merge", r"^git\s+checkout",
        r"^npm\s+install\s+-g", r"^npm\s+uninstall",
        r"^pip\s+install\s", r"^pip\s+uninstall",
        r"^apt\s+install", r"^apt\s+remove", r"^apt\s+purge",
        r"^apt-get\s", r"^yum\s+install", r"^yum\s+remove",
        r"^dnf\s+install", r"^dnf\s+remove",
        r"^brew\s+install", r"^brew\s+uninstall",
        r"^snap\s+install", r"^snap\s+remove",
        r"^docker\s+(?:rm|rmi|stop|kill|restart|exec|run)",
        r"^systemctl\s+(?:start|stop|restart|enable|disable|mask)",
        r"^service\s+\S+\s+(?:start|stop|restart)",
        r"^chmod\s", r"^chown\s", r"^chgrp\s",
        r"^useradd\s", r"^userdel\s", r"^usermod\s",
        r"^groupadd\s", r"^groupdel\s",
        r"^ln\s+-s\s+/", r"^ln\s+.*/(?:etc|bin|sbin|boot)",
        r"^dd\s", r"^mkfs\s", r"^fdisk\s", r"^parted\s",
        r"^crontab\s", r"^at\s",
        r"^shutdown\s", r"^reboot\s", r"^halt\s", r"^poweroff\s",
        r"^iptables\s", r"^nft\s", r"^ufw\s", r"^firewall-cmd\s",
        r"^openssl\s", r"^gpg\s", r"^ssh-keygen\s",
        r"^curl\s+.*\|.*(?:bash|sh|python)", r"^wget\s+.*-O\s+-",
    ],
    "deny": [
        r"^rm\s+-rf\s+/", r"^rm\s+-rf\s+~", r"^rm\s+-rf\s+\$HOME",
        r"^rm\s+-rf\s+/boot", r"^rm\s+-rf\s+/etc",
        r"^rm\s+-rf\s+/home", r"^rm\s+-rf\s+/root",
        r"^rm\s+-rf\s+/usr", r"^rm\s+-rf\s+/var",
        r"^rm\s+-rf\s+/bin", r"^rm\s+-rf\s+/sbin",
        r"^rm\s+-rf\s+/lib", r"^rm\s+-rf\s+/dev",
        r"^rm\s+-rf\s+/proc", r"^rm\s+-rf\s+/sys",
        r"^rm\s+-rf\s+/opt", r"^rm\s+-rf\s+/tmp/\*",
        r"^:\$\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\};",  # fork bomb
        r"^mkfs\s+/dev/", r"^dd\s+if=.*of=/dev/",
        r"^>\/dev/sda", r"^cat\s+/dev/.*>.*/dev/",
        r"^chmod\s+-R\s+777\s+/", r"^chmod\s+777\s+/",
        r"^chown\s+-R\s+\S+\s+/", r"^chown\s+\S+\s+/",
        r"^sudo\s+rm\s+-rf\s+/", r"^sudo\s+su\b",
        r"^wget\s+.*\|\s*(?:ba)?sh", r"^curl\s+.*\|\s*(?:ba)?sh",
    ],
    "allowedRoots": ["/root/laintas_cli", "/tmp", "/home", "/root/Helpwo"],
    "maxCommandLength": 10000,
    "blockSudo": True,
}


class PolicyDecision:
    __slots__ = ("action", "rule", "reason")
    def __init__(self, action: str, rule: str = "", reason: str = ""):
        self.action = action   # "allow" | "deny" | "needs_approval"
        self.rule = rule       # the regex that matched
        self.reason = reason   # human-readable explanation


# ── Module-level cache ────────────────────────────────────────────────────
_config: dict | None = None
_config_mtime: float = 0.0
_audit_lock = threading.Lock()


def _load_config(force: bool = False) -> dict:
    """Load policy config from disk, with mtime caching. Auto-creates defaults."""
    global _config, _config_mtime

    if not force and CONFIG_PATH.exists():
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if _config is not None and mtime == _config_mtime:
                return _config
            _config_mtime = mtime
        except OSError:
            pass

    if not CONFIG_PATH.exists():
        _write_default_config()

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
        # Fill in missing keys from defaults
        for key, val in _DEFAULT_CONFIG.items():
            if key not in cfg:
                cfg[key] = val
        _config = cfg
        if CONFIG_PATH.exists():
            _config_mtime = CONFIG_PATH.stat().st_mtime
    except (OSError, json.JSONDecodeError) as e:
        _config = dict(_DEFAULT_CONFIG)
    return _config


def _write_default_config() -> None:
    """Write the default safe policy to disk."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _write_audit(entry: dict) -> None:
    """Append a JSONL line to the audit log."""
    with _audit_lock:
        try:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _compile_rules(patterns: list) -> list[re.Pattern]:
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error:
            pass
    return compiled


def evaluate(command: str, cwd: str = None,
             req_id: str = None, agent_id: str = None) -> PolicyDecision:
    """Evaluate a command against the security policy.

    Returns a PolicyDecision with action in {"allow", "deny", "needs_approval"}.
    Side-effect: writes an audit log entry for every non-trivial decision.
    """
    cfg = _load_config()
    mode = cfg.get("mode", "audit")

    stripped = command.strip()

    # ── Basic sanity checks (always enforced) ──────────────────────────
    max_len = cfg.get("maxCommandLength", 10000)
    if len(stripped) > max_len:
        _write_audit(_audit_entry(
            command, "deny", f"command too long ({len(stripped)} > {max_len})",
            cwd, req_id, agent_id,
        ))
        return PolicyDecision("deny", "", f"Exceeds max command length ({max_len} chars)")

    if cfg.get("blockSudo", True) and re.match(r'^sudo\s', stripped):
        _write_audit(_audit_entry(
            command, "needs_approval", "sudo detected", cwd, req_id, agent_id,
        ))
        if mode == "enforce":
            return PolicyDecision("needs_approval", "sudo", "sudo commands require approval")

    # ── Check deny list first (takes precedence) ───────────────────────
    deny_rules = _compile_rules(cfg.get("deny", []))
    for rule in deny_rules:
        if rule.search(stripped):
            reason = f"Matched deny rule: {rule.pattern}"
            _write_audit(_audit_entry(command, "deny", reason, cwd, req_id, agent_id))
            if mode == "enforce":
                return PolicyDecision("deny", rule.pattern, reason)
            elif mode == "audit":
                # In audit mode, deny rules still block
                return PolicyDecision("deny", rule.pattern, reason)

    # ── Check needs_approval list ──────────────────────────────────────
    approval_rules = _compile_rules(cfg.get("needs_approval", []))
    for rule in approval_rules:
        if rule.search(stripped):
            reason = f"Matched approval rule: {rule.pattern}"
            _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
            if mode == "enforce":
                return PolicyDecision("needs_approval", rule.pattern, reason)
            # In audit mode, approval rules are advisory (allow with warning)

    # ── Path boundary check for write operations ────────────────────────
    path_decision = _check_paths(stripped, cwd, cfg.get("allowedRoots", []))
    if path_decision is not None:
        _write_audit(_audit_entry(command, path_decision.action,
                                  path_decision.reason, cwd, req_id, agent_id))
        if mode in ("enforce", "audit"):
            return path_decision

    # ── Allow by default ───────────────────────────────────────────────
    _write_audit(_audit_entry(command, "allow", "default allow",
                              cwd, req_id, agent_id))
    return PolicyDecision("allow")


def _check_paths(command: str, cwd: str | None,
                 allowed_roots: list) -> PolicyDecision | None:
    """Check if a write operation targets paths outside allowedRoots.

    Extracts file paths from well-known commands that write to disk:
    rm, mv, cp, touch, mkdir, cat >, > redirect, dd of=, ln, chmod, chown.
    Returns PolicyDecision('deny') if any path escapes, None if ok.
    """
    if not allowed_roots:
        return None

    cwd_path = Path(cwd or os.getcwd()).resolve()
    allowed = [Path(r).resolve() for r in allowed_roots]

    # Extract paths from the command
    paths = _extract_paths(command)
    for p in paths:
        try:
            resolved = (cwd_path / p).resolve()
        except (ValueError, OSError):
            continue
        # Check if resolved path is inside any allowed root
        ok = False
        for root in allowed:
            try:
                resolved.relative_to(root)
                ok = True
                break
            except ValueError:
                pass
        if not ok:
            return PolicyDecision(
                "deny",
                "",
                f"Path '{p}' resolves to '{resolved}' outside allowedRoots",
            )
    return None


def _extract_paths(command: str) -> list:
    """Extract likely filesystem paths from a command string.

    Handles: standalone paths, paths after common command prefixes,
    redirect targets (> file, >> file), and pipe destinations.
    """
    import shlex as _shlex
    paths = []
    try:
        tokens = _shlex.split(command)
    except ValueError:
        tokens = command.split()

    write_cmds = {"rm", "mv", "cp", "touch", "mkdir", "rmdir",
                  "chmod", "chown", "chgrp", "ln", "dd",
                  "cat", "tee", "tar", "zip", "gzip", "bzip2",
                  "install", "rsync", "scp"}

    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        # Flag arguments (don't treat as paths)
        if tok.startswith("-"):
            continue
        # Redirect: >file or >>file (handled as separate token by shlex)
        if tok in (">", ">>", "2>", "&>"):
            continue
        # Command that takes path arguments
        cmd = tokens[0].rsplit("/", 1)[-1] if tokens else ""
        if cmd in write_cmds and i > 0:
            if not tok.startswith("-") and not tok.startswith("$"):
                paths.append(tok)
        # of= in dd
        if tok.startswith("of="):
            paths.append(tok[3:])
        # Redirect targets after > are separate tokens
        if i > 0 and tokens[i-1] in (">", ">>", "2>", "&>"):
            if not tok.startswith("/dev/"):
                paths.append(tok)

    return paths


def needs_approval(command: str, cwd: str = None) -> bool:
    """Quick check: does this command need user approval?"""
    decision = evaluate(command, cwd)
    return decision.action == "needs_approval"


def is_allowed(command: str, cwd: str = None) -> bool:
    """Quick check: is this command allowed to run?"""
    decision = evaluate(command, cwd)
    return decision.action in ("allow", "needs_approval")


def _audit_entry(command: str, action: str, reason: str,
                 cwd: str | None, req_id: str | None,
                 agent_id: str | None) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "command": command[:500],
        "reason": reason,
        "cwd": cwd or os.getcwd(),
        "reqId": req_id or "",
        "agentId": agent_id or "unknown",
    }


def reload_config() -> dict:
    """Force-reload config from disk. Returns the new config."""
    return _load_config(force=True)


def get_config() -> dict:
    """Return the current policy config (possibly cached)."""
    return _load_config()
