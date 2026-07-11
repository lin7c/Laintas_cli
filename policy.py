"""
Security policy engine for laintas_cli.

Evaluates every command before execution against a configurable rule set.
Three-tier decision model:
  - allow: pass through immediately
  - needs_approval: pause and wait for user confirmation
  - deny: block execution entirely

Config: ~/.laintas/policy.json (auto-created with safe defaults on first load)
Audit log: ~/.laintas/audit.log (JSONL, one line per decision)

Reloads config on mtime change, like .laintas/commands.py — zero-restart updates.
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


import paths

CONFIG_PATH = paths.POLICY_FILE
AUDIT_PATH = paths.AUDIT_FILE

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
        r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
        r"\bxargs\s+(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
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
    ],
    "maxCommandLength": 10000,
    "blockSudo": True,
    # ── File-write rules (fs.write / fs.edit / fs.multi_edit) ──────────
    # Sensitive paths are always blocked regardless of mode (except "disabled").
    "denyFileWrite": [
        r"\.env(?:\.\w+)?$", r"/\.ssh/", r"(?:^|/)id_rsa(?:\.pub)?$",
        r"(?:^|/)id_ed25519(?:\.pub)?$", r"\.pem$", r"\.key$",
        r"(?:^|/)credentials\.json$", r"/\.git/config$", r"\.netrc$",
        r"/\.aws/credentials$",
    ],
    # ── Browser-action rules (browser.* tools) ──────────────────────────
    # Read-only browser tools (snapshot, query, get_url, get_title,
    # screenshot, wait_for, scroll, go_back, go_forward) are always allowed.
    # Actions listed here need user approval in "enforce" mode (advisory
    # in "audit" mode, same as shell-command approval rules).
    "browserApprovalActions": [
        "navigate", "click", "type", "evaluate", "select", "press_key",
    ],
    # URLs that are always denied for browser.navigate (applies in all modes
    # except "disabled"). Empty by default — add patterns like:
    #   r"^https?://localhost", r"^https?://127\.0\.0\.1",
    #   r"^https?://10\.", r"^https?://192\.168\.",
    "browserDenyUrlPatterns": [],
    # JavaScript patterns that are always denied for browser.evaluate
    # (applies in all modes except "disabled"). Blocks code exfiltration
    # and persistence vectors by default.
    "browserDenyJsPatterns": [
        r"\brequire\s*\(", r"\bimport\s*\(",
        r"\bfetch\s*\(", r"\bXMLHttpRequest\b",
        r"\beval\s*\(", r"\bFunction\s*\(",
        r"\blocalStorage\b", r"\bsessionStorage\b",
        r"document\.cookie", r"\bnavigator\.clipboard\b",
    ],
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

    if not paths.ensure_private_file(CONFIG_PATH):
        _config = dict(_DEFAULT_CONFIG)
        _config["mode"] = "enforce"
        return _config

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
        # Fill in missing keys from defaults
        for key, val in _DEFAULT_CONFIG.items():
            if key not in cfg:
                cfg[key] = val
        # Platform-specific allowedRoots for new configs
        if "allowedRoots" not in cfg:
            cfg["allowedRoots"] = ["/root/laintas_cli", "/tmp", "/home", "/root/Helpwo"]
        # Migrate old configs: move rules that changed category
        cfg = _migrate_config(cfg)
        _config = cfg
        if CONFIG_PATH.exists():
            _config_mtime = CONFIG_PATH.stat().st_mtime
    except (OSError, json.JSONDecodeError) as e:
        _config = dict(_DEFAULT_CONFIG)
        _config["allowedRoots"] = ["/root/laintas_cli", "/tmp", "/home", "/root/Helpwo"]
    return _config


def _migrate_config(cfg: dict) -> dict:
    """Migrate old config rules to new positions.

    When rules are reclassified (e.g. deny → needs_approval), old saved configs
    still carry them in the wrong list. This function strips deprecated patterns
    and re-adds them to the correct list.
    """
    # v2026-05-27: download pipe-to-shell moved from deny to needs_approval
    _deny_to_approval = [
        r"^wget\s+.*\|\s*(?:ba)?sh",
        r"^curl\s+.*\|\s*(?:ba)?sh",
    ]
    deny_list = cfg.get("deny", [])
    approval_list = cfg.get("needs_approval", [])
    changed = False
    for rule in _deny_to_approval:
        if rule in deny_list:
            deny_list = [r for r in deny_list if r != rule]
            changed = True
        if rule not in approval_list:
            approval_list.append(rule)
            changed = True
    # v2026-07-01: deletion was previously allowed inside allowedRoots.  It is
    # destructive regardless of location, so existing configs must inherit the
    # new confirmation rule as well as newly-created configs.
    _required_approval = [
        r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
        r"\bxargs\s+(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
    ]
    for rule in _required_approval:
        if rule not in approval_list:
            approval_list.append(rule)
            changed = True
    if changed:
        cfg["deny"] = deny_list
        cfg["needs_approval"] = approval_list
        # Persist the migration
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return cfg


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
            if AUDIT_PATH.exists() and not paths.ensure_private_file(AUDIT_PATH):
                return
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            paths.ensure_private_file(AUDIT_PATH)
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


def is_delete_command(command: str) -> bool:
    """Return whether *command* invokes a common destructive delete utility.

    Callers use this to require a fresh Yes/No decision for deletion instead
    of honoring the session-wide "approve all commands" shortcut.
    """
    stripped = (command or "").strip()
    parent_match = re.fullmatch(r"parent\((.*)\)", stripped, re.DOTALL)
    if parent_match:
        stripped = parent_match.group(1).strip()
    stripped = re.sub(r"^sudo(?:\s+-\S+)*\s+", "", stripped)
    patterns = (
        r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
        r"\bxargs\s+(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
    )
    return any(re.search(pattern, stripped) for pattern in patterns)


def evaluate(command: str, cwd: str = None,
             req_id: str = None, agent_id: str = None) -> PolicyDecision:
    """Evaluate a command against the security policy.

    Returns a PolicyDecision with action in {"allow", "deny", "needs_approval"}.
    Side-effect: writes an audit log entry for every non-trivial decision.
    """
    cfg = _load_config()
    mode = cfg.get("mode", "audit")

    stripped = command.strip()
    # `parent(...)` is a laintas shell override whose body executes in the
    # parent process.  Policy must inspect the body at every entry point
    # (/send, direct REPL, and agent tools), not the wrapper text.
    parent_match = re.fullmatch(r"parent\((.*)\)", stripped, re.DOTALL)
    if parent_match:
        stripped = parent_match.group(1).strip()

    # ── Select platform-specific rule sets ─────────────────────────────
    _plat = {"allow": [], "needs_approval": [], "deny": []}

    # Merge user rules with platform defaults (ensures security rules survive config upgrades)
    def _merged_rules(key):
        seen = set()
        merged = []
        for r in cfg.get(key, []) + _plat.get(key, []):
            if r not in seen:
                seen.add(r)
                merged.append(r)
        return merged

    # ── Basic sanity checks (always enforced) ──────────────────────────
    max_len = cfg.get("maxCommandLength", 10000)
    if len(stripped) > max_len:
        _write_audit(_audit_entry(
            command, "deny", f"command too long ({len(stripped)} > {max_len})",
            cwd, req_id, agent_id,
        ))
        return PolicyDecision("deny", "", f"Exceeds max command length ({max_len} chars)")

    sudo_detected = bool(
        cfg.get("blockSudo", True) and re.match(r'^sudo\s', stripped))
    if sudo_detected:
        _write_audit(_audit_entry(
            command, "needs_approval", "sudo detected", cwd, req_id, agent_id,
        ))

    # ── Check deny list first (takes precedence) ───────────────────────
    deny_rules = _compile_rules(_merged_rules("deny"))
    for rule in deny_rules:
        if rule.search(stripped):
            reason = f"Matched deny rule: {rule.pattern}"
            _write_audit(_audit_entry(command, "deny", reason, cwd, req_id, agent_id))
            if mode == "enforce":
                return PolicyDecision("deny", rule.pattern, reason)
            elif mode == "audit":
                # In audit mode, deny rules still block
                return PolicyDecision("deny", rule.pattern, reason)

    # ── Destructive delete utilities: always-ask tier ───────────────────
    # rm/rmdir/unlink/shred (direct or via xargs) are treated like
    # evaluate_file_delete() — approval is required in BOTH audit and
    # enforce mode, not just enforce. Previously audit mode folded this
    # into the "advisory" needs_approval loop below and silently allowed
    # it; that let a shell-issued `rm -rf` slip through with zero prompt
    # the same way the fs.delete tool did (2026-07-11 incident). "disabled"
    # mode still bypasses everything, same as every other check in this
    # function (mode == "disabled" never matches an "enforce"/"audit"
    # branch, so it silently falls through to allow).
    if mode != "disabled" and is_delete_command(stripped):
        reason = "delete command always requires approval (audit and enforce modes alike)"
        _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # ── Check needs_approval list ──────────────────────────────────────
    # Deny rules take precedence over sudo approval.  Otherwise approving
    # `sudo rm -rf /` would bypass the destructive-command deny list.
    if sudo_detected and mode == "enforce":
        return PolicyDecision(
            "needs_approval", "sudo", "sudo commands require approval")

    approval_rules = _compile_rules(_merged_rules("needs_approval"))
    for rule in approval_rules:
        if rule.search(stripped):
            reason = f"Matched approval rule: {rule.pattern}"
            _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
            if mode == "enforce":
                return PolicyDecision("needs_approval", rule.pattern, reason)
            # In audit mode, approval rules are advisory (allow with warning)

    # ── Path boundary check for write operations ────────────────────────
    # Dynamically add CWD to allowed roots (user working in a directory implies intent to write there)
    allowed_roots = list(cfg.get("allowedRoots", []))
    if cwd:
        cwd_str = str(cwd)
        if cwd_str not in allowed_roots:
            allowed_roots.append(cwd_str)

    path_decision = _check_paths(stripped, cwd, allowed_roots)
    if path_decision is not None:
        _write_audit(_audit_entry(command, path_decision.action,
                                  path_decision.reason, cwd, req_id, agent_id))
        # In enforce mode: ask user for approval (needs_approval)
        # In audit mode: allow with warning (don't block)
        if mode == "enforce":
            return path_decision
        # audit mode: log but allow

    # ── Allow by default ───────────────────────────────────────────────
    _write_audit(_audit_entry(command, "allow", "default allow",
                              cwd, req_id, agent_id))
    return PolicyDecision("allow")


def _check_paths(command: str, cwd: str | None,
                 allowed_roots: list) -> PolicyDecision | None:
    """Check if a write operation targets paths outside allowedRoots.

    Extracts file paths from well-known commands that write to disk:
    rm, mv, cp, touch, mkdir, cat >, > redirect, dd of=, ln, chmod, chown.
    Returns PolicyDecision('needs_approval') if any path escapes, None if ok.
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
                "needs_approval",
                "",
                f"Path '{p}' resolves to '{resolved}' outside allowedRoots",
            )
    return None


def _extract_paths(command: str) -> list:
    """Extract likely filesystem paths from a command string.

    Handles: standalone paths, paths after common command prefixes,
    redirect targets (> file, >> file), and pipe destinations.
    Supports POSIX paths.
    """
    import shlex as _shlex
    paths = []
    try:
        tokens = _shlex.split(command)
    except ValueError:
        tokens = command.split()

    write_cmds = {"rm", "mv", "cp", "touch", "mkdir", "rmdir",
                  "chmod", "chown", "chgrp", "ln", "dd",
                  "tee", "zip", "gzip", "bzip2",
                  "install", "rsync", "scp"}

    for i, tok in enumerate(tokens):
        # Flag arguments (don't treat as paths)
        if tok.startswith("-"):
            continue
        # Short /letter flags: /s, /f, /q — never real paths on any platform
        if len(tok) == 2 and tok[0] == "/" and tok[1].isalpha():
            continue
        # Redirect: >file or >>file (handled as separate token by shlex)
        if tok in (">", ">>", "2>", "&>"):
            continue

        is_path = False

        # POSIX path: starts with / and looks like a real path (not a flag)
        if tok.startswith("/") and ("/" in tok[1:] or len(tok) > 2):
            is_path = True

        if is_path and not tok.startswith("$"):
            # Only extract paths from write commands (cat is read-only; cat > is handled by redirect)
            cmd = tokens[0].rsplit("/", 1)[-1] if tokens else ""
            if cmd in write_cmds:
                paths.append(tok)
            continue

        # Command that takes path arguments (fallback for relative paths)
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


def evaluate_file_write(path: str, cwd: str | None = None,
                        req_id: str | None = None, agent_id: str | None = None) -> PolicyDecision:
    """Evaluate a file-write target (fs.write / fs.edit / fs.multi_edit) against policy.

    Path-based counterpart to evaluate(): same three-tier model, but matches
    against the resolved target path instead of a shell command string.
      - denyFileWrite patterns always block (except in "disabled" mode).
      - paths outside allowedRoots need approval in "enforce" mode, warn-and-allow
        in "audit" mode (mirrors the command path-boundary check in evaluate()).
      - "enforce" mode requires approval for every write, matching mainstream
        editor-agent behavior (diff preview + confirm before applying).
    """
    cfg = _load_config()
    mode = cfg.get("mode", "audit")

    try:
        abs_path = str(Path(path).resolve())
    except OSError:
        abs_path = path
    label = f"write {abs_path}"

    if mode != "disabled":
        deny_patterns = list(dict.fromkeys(
            cfg.get("denyFileWrite", []) + _DEFAULT_CONFIG["denyFileWrite"]))
        for pattern in deny_patterns:
            try:
                hit = re.search(pattern, abs_path)
            except re.error:
                continue
            if hit:
                reason = f"Matched denyFileWrite rule: {pattern}"
                _write_audit(_audit_entry(label, "deny", reason, cwd, req_id, agent_id))
                return PolicyDecision("deny", pattern, reason)

    # ── Path boundary: writes outside allowedRoots ──────────────────────
    allowed_roots = list(cfg.get("allowedRoots", []))
    cwd_str = str(cwd or os.getcwd())
    if cwd_str not in allowed_roots:
        allowed_roots.append(cwd_str)

    if allowed_roots:
        in_bounds = False
        for root in allowed_roots:
            try:
                Path(abs_path).relative_to(Path(root).resolve())
                in_bounds = True
                break
            except (ValueError, OSError):
                continue
        if not in_bounds:
            reason = f"'{abs_path}' is outside allowedRoots"
            _write_audit(_audit_entry(label, "needs_approval", reason, cwd, req_id, agent_id))
            if mode == "enforce":
                return PolicyDecision("needs_approval", "", reason)
            # audit mode: log but allow (mirrors evaluate()'s command path check)

    # ── Enforce mode: every write needs explicit approval ───────────────
    if mode == "enforce":
        reason = "enforce mode requires approval for all file writes"
        _write_audit(_audit_entry(label, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    _write_audit(_audit_entry(label, "allow", "default allow", cwd, req_id, agent_id))
    return PolicyDecision("allow")


def evaluate_file_delete(path: str, cwd: str | None = None,
                         req_id: str | None = None,
                         agent_id: str | None = None) -> PolicyDecision:
    """Evaluate deletion of one filesystem target.

    Deletion is an always-ask tier, decoupled from the general audit/enforce
    posture: sensitive credential/config targets remain denied, and every
    other deletion requires explicit confirmation in BOTH audit and enforce
    mode (previously audit mode silently auto-allowed deletions — see the
    2026-07-11 incident where a goal-less agent turn deleted a project file
    with zero prompt). Only "disabled" mode bypasses policy entirely.
    """
    cfg = _load_config()
    mode = cfg.get("mode", "audit")
    if mode == "disabled":
        return PolicyDecision("allow")

    try:
        abs_path = str(Path(path).resolve(strict=False))
    except OSError:
        abs_path = path
    label = f"delete {abs_path}"

    deny_patterns = list(dict.fromkeys(
        cfg.get("denyFileWrite", []) + _DEFAULT_CONFIG["denyFileWrite"]))
    for pattern in deny_patterns:
        try:
            hit = re.search(pattern, abs_path)
        except re.error:
            continue
        if hit:
            reason = f"Matched protected delete target: {pattern}"
            _write_audit(_audit_entry(
                label, "deny", reason, cwd, req_id, agent_id))
            return PolicyDecision("deny", pattern, reason)

    reason = "deletion always requires approval (audit and enforce modes alike)"
    _write_audit(_audit_entry(
        label, "needs_approval", reason, cwd, req_id, agent_id))
    return PolicyDecision("needs_approval", "", reason)


# `evaluate` runs arbitrary JS in the page context — the browser-automation
# analog of shell.exec's "run anything", not a routine testing action like
# navigate/click/type. Unlike those (left mode-dependent so automated test
# flows aren't interrupted by a prompt on every click), `evaluate` is always
# an always-ask tier regardless of audit/enforce, matching the fs.delete /
# shell-delete-command treatment.
_ALWAYS_ASK_BROWSER_ACTIONS = {"evaluate"}


def evaluate_browser_action(action: str, params: dict,
                            req_id: str = None,
                            agent_id: str = None) -> PolicyDecision:
    """Evaluate a browser.* tool action against the security policy.

    Counterpart to evaluate() / evaluate_file_write(), but for headless-browser
    automation.  Same three-tier model:
      - allow: proceed immediately
      - needs_approval: ask the user (enforce mode always; audit mode only
        for the always-ask tier — see _ALWAYS_ASK_BROWSER_ACTIONS)
      - deny: block entirely

    Read-only actions (snapshot, query, get_url, get_title, screenshot,
    wait_for, scroll, go_back, go_forward) are always allowed.
    Actions in browserApprovalActions need approval in enforce mode (plus
    always-ask actions in audit mode too).
    URL and JS deny patterns always block (except in disabled mode).
    """
    cfg = _load_config()
    mode = cfg.get("mode", "audit")

    if mode == "disabled":
        return PolicyDecision("allow")

    label = f"browser.{action}"

    # ── Deny: URL patterns for navigate/open ────────────────────────────
    url = params.get("url", "")
    if url and action in ("navigate", "open"):
        deny_url = list(dict.fromkeys(
            cfg.get("browserDenyUrlPatterns", []) +
            _DEFAULT_CONFIG["browserDenyUrlPatterns"]))
        for pattern in deny_url:
            try:
                if re.search(pattern, url):
                    reason = f"URL matched deny pattern: {pattern}"
                    _write_audit(_audit_entry(label, "deny", reason, None, req_id, agent_id))
                    return PolicyDecision("deny", pattern, reason)
            except re.error:
                continue

    # ── Deny: JS patterns for evaluate ──────────────────────────────────
    script = params.get("script", "")
    if script and action == "evaluate":
        deny_js = list(dict.fromkeys(
            cfg.get("browserDenyJsPatterns", []) +
            _DEFAULT_CONFIG["browserDenyJsPatterns"]))
        for pattern in deny_js:
            try:
                if re.search(pattern, script):
                    reason = f"JS matched deny pattern: {pattern}"
                    _write_audit(_audit_entry(label, "deny", reason, None, req_id, agent_id))
                    return PolicyDecision("deny", pattern, reason)
            except re.error:
                continue

    # ── Approval actions ────────────────────────────────────────────────
    approval_actions = list(dict.fromkeys(
        cfg.get("browserApprovalActions", []) +
        _DEFAULT_CONFIG["browserApprovalActions"]))

    if action in approval_actions:
        # Build a human-readable summary for the approval prompt.
        if action == "navigate":
            summary = f"Navigate to {url}"
        elif action == "click":
            summary = f"Click element: {params.get('selector', '')}"
        elif action == "type":
            text = params.get("text", "")
            summary = f"Type {len(text)} chars into {params.get('selector', '')}"
        elif action == "evaluate":
            script_preview = script[:200]
            summary = f"Evaluate JS: {script_preview}"
        elif action == "select":
            summary = f"Select '{params.get('value', params.get('label', ''))}' in {params.get('selector', '')}"
        elif action == "press_key":
            summary = f"Press key: {params.get('key', '')}"
        else:
            summary = f"browser.{action}"

        reason = f"browser.{action} needs approval ({summary})"
        _write_audit(_audit_entry(label, "needs_approval", reason, None, req_id, agent_id))
        if mode == "enforce" or action in _ALWAYS_ASK_BROWSER_ACTIONS:
            return PolicyDecision("needs_approval", "", reason)
        # audit mode: log but allow (except always-ask actions handled above)

    # ── Allow by default ────────────────────────────────────────────────
    _write_audit(_audit_entry(label, "allow", "default allow", None, req_id, agent_id))
    return PolicyDecision("allow")


def evaluate_email_send(req_id: str = None, agent_id: str = None) -> PolicyDecision:
    """Evaluate a mail.send_to_user tool call. There is no risky-target
    dimension to check (recipient is always the caller's own verified
    account email, resolved server-side) — this is purely an always-ask
    tier, same treatment as fs.delete / browser.evaluate, since it's an
    irreversible action with an external, human-facing side effect.
    """
    cfg = _load_config()
    mode = cfg.get("mode", "audit")

    if mode == "disabled":
        _write_audit(_audit_entry("mail.send_to_user", "allow", "disabled mode", None, req_id, agent_id))
        return PolicyDecision("allow")

    reason = "sending email always requires approval (audit and enforce modes alike)"
    _write_audit(_audit_entry("mail.send_to_user", "needs_approval", reason, None, req_id, agent_id))
    return PolicyDecision("needs_approval", "", reason)


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


def set_mode(mode: str) -> tuple[bool, str]:
    """Set the policy mode and persist to disk.

    Returns (ok, message). mode must be one of: audit, enforce, disabled.
    """
    mode = (mode or "").strip().lower()
    valid = {"audit", "enforce", "disabled"}
    if mode not in valid:
        return False, f"Invalid mode '{mode}'. Valid: {', '.join(sorted(valid))}"
    cfg = _load_config(force=True)
    old = cfg.get("mode", "audit")
    cfg["mode"] = mode
    tmp = CONFIG_PATH.with_suffix(".tmp")
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(CONFIG_PATH)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"Failed to write config: {e}"
    # Update cache so subsequent evaluate() calls see the new mode immediately.
    global _config, _config_mtime
    _config = cfg
    try:
        _config_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        pass
    return True, f"Policy mode: {old} → {mode}"
