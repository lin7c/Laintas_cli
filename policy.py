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
import shlex
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


import paths
import json_store

CONFIG_PATH = paths.account_path("POLICY_FILE")
AUDIT_PATH = paths.account_path("AUDIT_FILE")

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
        r"^git\s+show", r"^git\s+blame", r"^git\s+remote(?:\s+-v)?$",
        # Only the READ-ONLY forms of stash/branch belong here. Bare
        # `git stash` moves the whole working tree away and `git branch -D`
        # deletes a branch; listing them as blanket-safe was wrong.
        r"^git\s+stash\s+(?:list|show)\b",
        r"^git\s+branch\s*$", r"^git\s+branch\s+(?:-a|-r|-l|-v|-vv|--list)\b",
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
        # Interpreter -c/-e flags execute arbitrary code and bypass every
        # shell-level rule below; require approval so the user sees them.
        r"^python[23]?\s+(?:.+?\s)?-c\b",
        r"^python[23]?m\s+\S",
        r"^node\s+(?:.+?\s)?-e\b",
        r"^node\s+--eval\b",
        r"^perl\s+(?:.+?\s)?-e\b",
        r"^ruby\s+(?:.+?\s)?-e\b",
        r"^php\s+(?:.+?\s)?-r\b",
        r"^bash\s+-c\b", r"^sh\s+-c\b", r"^zsh\s+-c\b",
        r"^git\s+push", r"^git\s+commit", r"^git\s+reset",
        r"^git\s+rebase", r"^git\s+merge", r"^git\s+checkout",
        # Working-tree / history mutations that were missing entirely.
        # The subset that cannot be undone is ALSO caught by
        # is_destructive_git_command() below, which asks in audit mode too.
        r"^git\s+clean\b", r"^git\s+stash\b", r"^git\s+restore\b",
        r"^git\s+switch\b", r"^git\s+cherry-pick\b", r"^git\s+revert\b",
        r"^git\s+am\b", r"^git\s+apply\b", r"^git\s+filter-branch\b",
        r"^git\s+tag\s+-d\b", r"^git\s+update-ref\b", r"^git\s+reflog\b",
        r"^git\s+worktree\s+(?:add|remove|prune)\b",
        r"^git\s+submodule\s+(?:deinit|update)\b",
        r"^git\s+remote\s+(?:add|remove|rm|set-url|rename)\b",
        r"^git\s+config\s+(?!--get|--list|-l\b)",
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
        # Protect credential paths whether quoted or passed directly.
        r"\.env(?:\.\w+)?$", r"[/\\]\.ssh[/\\]", r"(?:^|[/\\])id_rsa(?:\.pub)?$",
        r"(?:^|[/\\])id_ed25519(?:\.pub)?$", r"\.pem$", r"\.key$",
        r"(?:^|[/\\])credentials\.json$", r"[/\\]\.git[/\\]config$", r"\.netrc$",
        r"[/\\]\.aws[/\\]credentials$",
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
        # S6 (bughunt): two more exfiltration vectors the list missed.
        # sendBeacon is fire-and-forget (works during unload, no response
        # needed); WebSocket is a persistent channel both fetch and XHR
        # patterns never see.
        r"\bsendBeacon\s*\(", r"\bnew\s+WebSocket\b", r"\bWebSocket\s*\(",
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
# Version of the organisation policy baked into the cached config. -1 means
# "not yet resolved", which is distinct from 0 ("resolved: no org policy").
_config_org_version: int = -1


# ── Organisation policy contributors ─────────────────────────────────────
#
# Something outside this module — today the Enterprise organisation extension —
# may tighten the local policy. It registers here rather than assigning to a
# module global, for two reasons learned the hard way:
#
#   1. A single assignable slot silently loses whichever contributor loaded
#      first. Extensions are plural by design; the tool registry already solved
#      this with source tags and `unregister_source`, and this is the same shape.
#   2. A contributor must also report its *version*, because that version is
#      part of the config cache key. When only the merge function was wired, a
#      newly published organisation policy did not invalidate anything and took
#      effect on the next unrelated edit of the member's own config — which is
#      exactly what the cache key was introduced to prevent.
#
# Contract: `apply_fn(cfg) -> cfg` may only make the policy stricter, and
# `version_fn() -> int` must increase whenever `apply_fn` would produce a
# different result.
_org_contributors: "dict[str, tuple]" = {}


def register_policy_contributor(source: str, apply_fn, version_fn=None) -> None:
    """Register (or replace) an organisation policy contributor."""
    if not callable(apply_fn):
        raise TypeError("apply_fn must be callable")
    _org_contributors[str(source)] = (apply_fn, version_fn)
    reload_config()


def unregister_policy_contributor(source: str) -> bool:
    """Remove a contributor and drop its rules from the active config."""
    removed = _org_contributors.pop(str(source), None) is not None
    if removed:
        reload_config()
    return removed


def policy_contributors() -> list[str]:
    return sorted(_org_contributors)


def _current_org_version() -> int:
    """Combined version of every contributor; 0 when there are none.

    Summed rather than maxed so that two contributors moving in opposite
    directions still change the total — the number only has to differ, and a max
    would hide one contributor rolling back while another moved forward.
    """
    total = 0
    for _apply, version_fn in _org_contributors.values():
        if version_fn is None:
            continue
        try:
            total += int(version_fn() or 0)
        except Exception:
            # A contributor that cannot report its version must not take the
            # policy engine down with it; treat it as "unknown, assume changed".
            total += 1
    return total


_audit_lock = threading.Lock()
_config_lock = threading.Lock()
def _load_config(force: bool = False) -> dict:
    """Load policy config from disk, with mtime caching. Auto-creates defaults."""
    global _config, _config_mtime, _config_org_version

    org_version = _current_org_version()

    with _config_lock:
        if not force and CONFIG_PATH.exists():
            try:
                mtime = CONFIG_PATH.stat().st_mtime
                # An org policy change must invalidate the cache even when the
                # local file is untouched — otherwise a tightened company policy
                # would not take effect until the member edited their own config.
                if (_config is not None and mtime == _config_mtime
                        and org_version == _config_org_version):
                    return _config
                _config_mtime = mtime
            except OSError:
                pass
        _config_org_version = org_version

        if not CONFIG_PATH.exists():
            _write_default_config()

        if not paths.ensure_private_file(CONFIG_PATH):
            fallback = dict(_DEFAULT_CONFIG)
            fallback["mode"] = "enforce"
            _config = _apply_org_policy(fallback)
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
            if "allowedRoots" not in cfg:
                cfg["allowedRoots"] = _default_allowed_roots()
            # Migrate old configs: move rules that changed category
            cfg = _migrate_config(cfg)
            _config = _apply_org_policy(cfg)
            if CONFIG_PATH.exists():
                _config_mtime = CONFIG_PATH.stat().st_mtime
        except (OSError, json.JSONDecodeError) as e:
            _config = dict(_DEFAULT_CONFIG)
            _config["allowedRoots"] = _default_allowed_roots()
        return _config


def command_parse_unresolved(risks) -> bool:
    """True when the analyser could not determine what a segment will execute."""
    try:
        import command_parse
        return command_parse.RISK_UNRESOLVED in (risks or set())
    except Exception:
        return False


def command_parse_opaque(risks) -> bool:
    """True when the unreadable part is a payload, not the command word."""
    try:
        import command_parse
        return command_parse.RISK_OPAQUE_PAYLOAD in (risks or set())
    except Exception:
        return False


def _command_variants(command: str) -> tuple:
    """Every command this line will actually run, plus how it was obfuscated.

    Returns ``(variants, risks)``. The original string is always first, so a bug
    in decomposition can only ever *add* matches — it can never silently remove a
    match that the old string-only rules already caught.
    """
    try:
        import command_parse
        return command_parse.effective_commands(command), command_parse.obfuscation_risks(command)
    except Exception:
        # A parser failure must not open a hole: fall back to the raw string,
        # which is exactly the pre-existing behaviour.
        return [command], set()


def _apply_org_policy(cfg: dict) -> dict:
    """Run every registered contributor over the local config.

    Order is by source name so the result does not depend on load order. A
    contributor that raises is skipped rather than allowed to blank the policy:
    losing one organisation's rules is bad, losing all of them is worse.
    """
    for source in sorted(_org_contributors):
        apply_fn, _version_fn = _org_contributors[source]
        try:
            result = apply_fn(cfg)
        except Exception:
            continue
        if isinstance(result, dict):
            cfg = result
    return cfg


def _default_allowed_roots() -> list:
    """Where writes are ordinary rather than notable, on THIS machine.

    This used to be the literal list `["/root/laintas_cli", "/tmp", "/home",
    "/root/Helpwo"]` — one developer's box, under a comment claiming to be
    platform-specific. Anywhere else it named directories that do not exist and
    omitted the user's actual home, so what a fresh install gated depended on
    who ran it.

    Mounted external documents are not implicitly added to trusted roots.
    """
    import tempfile

    roots: list = []
    candidates = [str(Path.home()), tempfile.gettempdir()]
    candidates.append("/tmp")
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in roots:
            roots.append(candidate)
    return roots


def _migrate_config(cfg: dict) -> dict:
    """Migrate old config rules to new positions.

    When rules are reclassified, old saved configs still carry them in the
    wrong list. Strip deprecated patterns and re-add them correctly.
    """
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
    # v2026-07-19: interpreter -c/-e flags execute arbitrary code and bypass
    # every shell-level rule. Existing configs must inherit the new approval
    # rules; without this, a saved config from before today silently allows
    # `python -c "..."` / `node -e "..."` / `bash -c "..."` without any gate.
    _required_approval += [
        r"^python[23]?\s+(?:.+?\s)?-c\b",
        r"^python[23]?m\s+\S",
        r"^node\s+(?:.+?\s)?-e\b",
        r"^node\s+--eval\b",
        r"^perl\s+(?:.+?\s)?-e\b",
        r"^ruby\s+(?:.+?\s)?-e\b",
        r"^php\s+(?:.+?\s)?-r\b",
        r"^bash\s+-c\b", r"^sh\s+-c\b", r"^zsh\s+-c\b",
    ]
    for rule in _required_approval:
        if rule not in approval_list:
            approval_list.append(rule)
            changed = True
    # Refresh the file-write denylist for previously saved configurations.
    write_deny_list = cfg.get("denyFileWrite", [])
    for rule in _DEFAULT_CONFIG["denyFileWrite"]:
        if rule not in write_deny_list:
            write_deny_list.append(rule)
            changed = True
    if changed:
        cfg["deny"] = deny_list
        cfg["needs_approval"] = approval_list
        cfg["denyFileWrite"] = write_deny_list
        # Persist the migration atomically (mode 0o600 - policy rules are
        # security-sensitive; a tampered config could weaken the gate).
        try:
            json_store.save_json_atomic(CONFIG_PATH, cfg, mode=0o600)
        except OSError:
            pass
    return cfg


def _write_default_config() -> None:
    """Write the default safe policy to disk."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        json_store.save_json_atomic(CONFIG_PATH, _DEFAULT_CONFIG, mode=0o600)
    except OSError:
        pass


_AUDIT_MAX_BYTES = 10 * 1024 * 1024  # 10MB; rotate to audit.log.1


def _write_audit(entry: dict) -> None:
    """Append a JSONL line to the audit log, with size-based rotation."""
    with _audit_lock:
        try:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            if AUDIT_PATH.exists():
                if not paths.ensure_private_file(AUDIT_PATH):
                    return
                # Rotate if over the size cap
                try:
                    if AUDIT_PATH.stat().st_size > _AUDIT_MAX_BYTES:
                        rotated = AUDIT_PATH.with_suffix(".log.1")
                        try:
                            rotated.unlink(missing_ok=True)
                        except OSError:
                            pass
                        AUDIT_PATH.rename(rotated)
                except OSError:
                    pass
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


# Cache compiled rules keyed by (rule_type, config_mtime) to avoid
# recompiling regexes on every evaluate() call.
_compiled_cache: dict[tuple[str, float, int], list[re.Pattern]] = {}


def _get_compiled_rules(key: str, cfg: dict) -> list[re.Pattern]:
    """Return compiled rules for `key`, cached by config mtime."""
    # Org policy contributes rules, so its version belongs in the cache key —
    # otherwise a tightened company policy would keep hitting stale compiles.
    cache_key = (key, _config_mtime, _config_org_version)
    cached = _compiled_cache.get(cache_key)
    if cached is not None:
        return cached
    compiled = _compile_rules(cfg.get(key, []))
    _compiled_cache[cache_key] = compiled
    # Prune stale entries to prevent unbounded growth across config reloads.
    if len(_compiled_cache) > 20:
        stale = [k for k in _compiled_cache
                 if k[1] != _config_mtime or k[2] != _config_org_version]
        for k in stale:
            del _compiled_cache[k]
    return compiled


def _unwrap_parent(command: str) -> str:
    """Strip all ``parent(...)`` wrappers from *command*.

    ``parent(...)`` is a laintas shell override whose body executes in the
    parent process.  Policy must inspect the body at every entry point
    (/send, direct REPL, and agent tools), not the wrapper text.  Nested
    wrappers like ``parent(parent(rm -rf /))`` must be fully unwrapped -
    previously only one level was stripped, hiding the inner command from
    every rule below.
    """
    stripped = (command or "").strip()
    for _ in range(16):  # bounded loop - 16 levels is absurd, anything more is an attack
        m = re.fullmatch(r"parent\((.*)\)", stripped, re.DOTALL)
        if not m:
            break
        stripped = m.group(1).strip()
    return stripped


# Matches the `git` binary plus any global options that may sit between it and
# the subcommand. Without this, `git -C /repo clean -fd` and
# `git --git-dir=x reset --hard` walk straight past a naive `^git\s+clean`.
# The leading alternation lets a command hide behind `;`, `&&` or a newline.
_GIT_PREFIX = (
    r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?git"
    r"(?:\s+(?:-[cC]\s+\S+|--(?:git-dir|work-tree|exec-path|namespace)=\S+"
    r"|--no-pager|--paginate|--bare|--literal-pathspecs))*\s+"
)


#: Recursive readers, each with: the options that actually bound the WALK, and
#: how many positional arguments come before the path.
#:
#: `du` has an empty tuple on purpose. `-s` and `-d N` bound what it PRINTS and
#: not what it visits -- GNU du walks the whole tree either way, which is why
#: `du -sh` on five directories of a mounted drive sat for a minute and had to
#: be killed. An option that shortens the output is not a ceiling on the cost.
_RECURSIVE_READERS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("find", (r"-maxdepth\b", r"-prune\b", r"-quit\b"), 0),
    ("du", (), 0),
    ("ncdu", (), 0),
    ("tree", (r"(?:^|\s)-L\s*\d",), 0),
    # Pattern first, path second.
    ("rg", (r"--max-?depth\b",), 1),
    ("ag", (r"--depth\b",), 1),
)

#: Recursion carried in a flag rather than in the command's nature. Neither has
#: a depth option at all, so there is nothing that could bound them.
_RECURSIVE_FLAGGED: tuple[tuple[str, str, int], ...] = (
    ("grep", r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?grep\s+(?:-\w*[rR]\b|--recursive\b)", 1),
    ("ls", r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?ls\s+(?:-\w*R\b|--recursive\b)", 0),
    ("cp", r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?cp\s+(?:-\w*[rR]\b|--recursive\b)", 0),
)


def _recursion_target(command: str, name: str, skip: int) -> str:
    """The directory a recursive command was pointed at, as written.

    `skip` is how many positional arguments belong to the command before the
    path does: `grep -r PATTERN DIR` has one, `find DIR` has none. Reading the
    pattern as the path is how `grep -r needle /somewhere` looked local.
    """
    match = re.search(
        r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?" + name + r"\b([^\n;&|]*)", command)
    if match is None:
        return ""
    seen = 0
    tokens = match.group(1).split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            # An option that takes a value swallows the next token, which is
            # otherwise mistaken for the path.
            if token in ("-e", "--regexp", "--include", "--exclude", "-f",
                         "--file", "-m", "--max-count", "-g", "--glob",
                         # du's own value-taking options: without these,
                         # `du -d 1 /data` reads "1" as the directory.
                         "-d", "--max-depth", "-t", "--threshold",
                         "-X", "--exclude-from", "-L", "--level"):
                index += 2
                continue
            index += 1
            continue
        if seen < skip:
            seen += 1
            index += 1
            continue
        return token
    return "."


def is_unbounded_recursion(command: str, cwd=None) -> bool:
    """Whether *command* walks a tree of unknown size with no ceiling.

    Not a list of slow filesystems, and not a list of big directories: both
    lists are endless, and neither is knowable from here. The shape of the
    command is knowable. A recursive walk with no depth bound, rooted OUTSIDE
    the directory the user is working in, is a command whose cost nobody in
    this process can estimate -- the tree may be a project, a home directory,
    a mounted disk, a network share, or a whole machine, and the ones that
    hang look identical to the ones that return instantly until they do not.

    Inside the working directory it stays friction-free: that tree is the
    user's own, they chose it, and its scale is the thing they are least
    likely to be surprised by.

    The precedent is `_check_paths`: writing inside cwd is ordinary, writing
    outside it is worth one question. This applies the same boundary to
    reading, where the cost is the traversal rather than the target.
    """
    stripped = _unwrap_parent(command)
    stripped = re.sub(r"^sudo(?:\s+-\S+)*\s+", "", stripped)

    for name, bounded_by, skip in _RECURSIVE_READERS:
        if not re.search(r"(?:^|[;&|]\s*|\n\s*)(?:\S*/)?" + name + r"\s", stripped):
            continue
        if any(re.search(flag, stripped) for flag in bounded_by):
            continue
        if _escapes_working_directory(_recursion_target(stripped, name, skip), cwd):
            return True

    for name, pattern, skip in _RECURSIVE_FLAGGED:
        if re.search(pattern, stripped) and _escapes_working_directory(
                _recursion_target(stripped, name, skip), cwd):
            return True
    return False


def _escapes_working_directory(target: str, cwd=None) -> bool:
    """True when *target* is not inside the directory the user is working in.

    An empty or unparseable target means the command took its default, which
    is the working directory -- inside, and therefore not asked about.
    """
    if not target:
        return False
    target = target.strip().strip("'\"")
    if not target or target.startswith("-"):
        return False
    try:
        base = Path(cwd or os.getcwd()).resolve()
        resolved = (base / os.path.expanduser(target)).resolve()
    except (ValueError, OSError):
        return True
    try:
        resolved.relative_to(base)
        return False
    except ValueError:
        return True


def is_destructive_git_command(command: str) -> bool:
    """Return whether *command* is a git operation that destroys work.

    The companion of :func:`is_delete_command` for git. These lose uncommitted
    changes, unreachable commits, or state other people already fetched — and
    unlike the ordinary ``needs_approval`` git rules (which are advisory in
    "audit" mode, the default), a match here asks in BOTH modes and does not
    honour the session-wide "approve all commands" shortcut.

    Deliberately NOT listed: ``commit``, ``merge``, ``rebase`` of local-only
    work, and ``revert`` — they add to history rather than destroying it and
    are recoverable through the reflog. Read-only forms (``clean --dry-run``,
    ``restore --staged``) are excluded so ordinary inspection stays friction-free.
    """
    stripped = _unwrap_parent(command)
    stripped = re.sub(r"^sudo(?:\s+-\S+)*\s+", "", stripped)

    def _git(subcommand: str) -> bool:
        return re.search(_GIT_PREFIX + subcommand, stripped) is not None

    # `git clean` deletes untracked files outright; with -x/-X it takes
    # gitignored ones too (.env, local config), which the session checkpoint
    # in snapshot.py deliberately does not capture. Nothing recovers those.
    # -n/--dry-run only reports, so it stays out of the way.
    if _git(r"clean\b") and not re.search(
            r"\bclean\b[^\n;&|]*(?:\s-\w*n\b|\s--dry-run\b)", stripped):
        return True

    # Discards every uncommitted change in the working tree.
    if _git(r"reset\b[^\n;&|]*\s--hard\b"):
        return True

    # `git checkout -- <path>` / `git checkout .` / `-f` overwrite the working
    # tree from the index. Plain `git checkout <branch>` is only needs_approval.
    if _git(r"checkout\b[^\n;&|]*\s(?:--(?:\s|$)|-f\b|--force\b)") or _git(r"checkout\s+\.(?:\s|$)"):
        return True

    # `git restore <path>` throws away working-tree edits. `--staged` alone
    # only unstages, which is recoverable.
    if _git(r"restore\b") and not re.search(
            r"\brestore\b[^\n;&|]*\s--staged\b[^\n;&|]*", stripped):
        return True
    if _git(r"restore\b[^\n;&|]*\s--worktree\b"):
        return True

    # Rewrites or deletes a ref other people may already have fetched.
    if _git(r"push\b[^\n;&|]*\s(?:-f\b|--force\b|--force-with-lease\b|--delete\b|--mirror\b)"):
        return True
    if _git(r"push\b[^\n;&|]*\s:\S+"):  # refspec form of a remote branch delete
        return True

    # Branch/tag/ref deletion and history surgery.
    if _git(r"branch\b[^\n;&|]*\s-(?:D\b|d\b[^\n;&|]*\s--force\b)"):
        return True
    if _git(r"tag\b[^\n;&|]*\s-d\b"):
        return True
    if _git(r"update-ref\b[^\n;&|]*\s-d\b"):
        return True
    if _git(r"filter-branch\b") or _git(r"filter-repo\b"):
        return True
    if _git(r"reflog\s+(?:delete|expire)\b"):
        return True

    # Drops stashed work permanently (`git stash list` cannot bring it back).
    if _git(r"stash\s+(?:drop|clear)\b"):
        return True

    # Removes a sibling agent's isolated worktree along with its edits.
    if _git(r"worktree\s+remove\b[^\n;&|]*\s(?:-f\b|--force\b)"):
        return True

    return False


def is_delete_command(command: str) -> bool:
    """Return whether *command* invokes a common destructive delete utility.

    Callers use this to require a fresh Yes/No decision for deletion instead
    of honoring the session-wide "approve all commands" shortcut.
    """
    stripped = _unwrap_parent(command)
    stripped = re.sub(r"^sudo(?:\s+-\S+)*\s+", "", stripped)
    patterns = (
        # Shell control-flow keywords are command boundaries too. Without
        # these, `for d in ...; do rm -rf "$d"; done` bypasses the always-ask
        # tier even though the same rm outside a loop is caught.
        r"(?:^|[;&|]\s*|\n\s*|\b(?:do|then|else)\s+)(?:\S*/)?"
        r"(?:rm|rmdir|unlink|shred)(?:\s|$)",
        r"\bxargs\s+(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
        r"(?:^|[;&|]\s*|\n\s*)find(?:\s|$)[^\n;&|]*\s-delete(?:\s|$)",
        r"(?:^|[;&|]\s*|\n\s*)find(?:\s|$)[^\n;&|]*\s-exec(?:dir)?\s+(?:\S*/)?(?:rm|rmdir|unlink|shred)(?:\s|$)",
    )
    return any(re.search(pattern, stripped) for pattern in patterns)


def is_unsafe_shared_temp_cleanup(command: str) -> bool:
    """Catch shared-temp sweeps capable of selecting the root itself.

    GNU find evaluates its starting point. Therefore `find /tmp ... -name
    'tmp*'` can yield `/tmp`; feeding that result to rm recursively deletes the
    shared root. A depth guard makes owned-child sweeps safe, so preserve those.
    """
    stripped = _unwrap_parent(command)
    # S2 (bughunt): `/tmp/.`, `/tmp/./` and `/tmp//` are the same starting
    # point as `/tmp` to find(1), but the old lookahead only accepted an
    # optional single trailing slash — every one of them walked past the
    # shared-root sweep check. `[\./]*` covers the dot-and-slash spellings
    # that normalise to the root itself; a real subdirectory (`/tmp/x`)
    # still does not match. Plain `*`, not possessive `*+`: that syntax is
    # Python 3.11+ and setup.py still supports 3.10, where it raised on
    # every evaluate(). The class is one char plus an optional dot, so
    # there is no backtracking to prevent.
    shared_root = r"(?:/tmp|/var/tmp|\$\{?TMPDIR\}?)(?:[\\/][.]?)*(?=\s|$)"
    find_from_root = re.search(
        rf"(?:^|[;&|()]|\$\()\s*find\s+{shared_root}", stripped)
    if not find_from_root:
        return False
    if re.search(r"\s-mindepth\s+[1-9]\d*\b", stripped):
        return False
    return is_delete_command(stripped)


# ── Runtime state protection (~/.laintas) ────────────────────────────────
# The runtime's own state directory holds the live event stream, sessions,
# memories, the cross-instance registry AND policy.json — the configuration
# this very gate reads. Nothing protected it: `echo '{"mode":"disabled"}' >
# ~/.laintas/policy.json` was an ordinary allow, so the approval gate could
# switch itself off with one redirect, and `mv ~/.laintas/sessions …`,
# `sed -i … events.jsonl` or `chmod -R 000 ~/.laintas` all ran with no prompt.
#
# Reads deliberately stay free: inspecting events.jsonl is routine work, and
# gating it would make the guard too noisy to keep.

# Utilities whose every operand is either consumed or overwritten, so any of
# them landing inside the state dir is a mutation of it.
_STATE_WRITE_UTIL_RE = re.compile(
    r"(?:^|[;&|]\s*|\bdo\s+|\bthen\s+|\bxargs\s+)"
    r"(?:[^\s/]*/)?"
    r"(?:mv|cp|truncate|chmod|chown|chgrp|ln|install|rsync|shred)"
    r"(?:\s|$)")

# `sed -i` / `perl -i` rewrite a file in place, and plain `sed` sits on the
# always-allow list — without the -i test this would either miss the mutation
# or gate every read-only sed in the repo. The flag itself is tested per token
# below: a lookahead cannot see it, because `sed\s+` has already consumed the
# space that `\s-i\b` would need.
_STATE_INPLACE_EDIT_RE = re.compile(
    r"(?:^|[;&|]\s*|\bdo\s+|\bthen\s+)(?:[^\s/]*/)?(?:sed|perl)(?=\s|$)")


def _is_inplace_flag(token: str) -> bool:
    """True for the sed/perl flag that rewrites the file instead of stdout.

    Covers ``-i``, the GNU suffix form ``-i.bak``, and the combined short-flag
    spellings (``-ni``, ``-pi``, ``-Ei``) — while leaving ``-e``/``-n``/``-p``
    alone, since a read-only sed is ordinary work and must not prompt.
    """
    if token.startswith("--"):
        return token.split("=", 1)[0] == "--in-place"
    if not token.startswith("-") or token == "-":
        return False
    letters = re.split(r"[.=]", token[1:], 1)[0]
    return bool(letters) and letters.isalpha() and "i" in letters

# Operators where only the TARGET can clobber state. Scanning operands instead
# would catch `cat ~/.laintas/events.jsonl > /tmp/out`, an ordinary read.
_STATE_WRITE_TARGET_RES = (
    re.compile(r'''(?<!>)>(?:>|\||&)?\s*('[^']*'|"[^"]*"|[^\s;&|]+)'''),
    re.compile(r"(?:^|[;&|]\s*|\bdo\s+|\bthen\s+)tee\s+(?:-\S+\s+)*(\S+)"),
    re.compile(r"\bdd\b[^\n;&|]*\bof=(\S+)"),
)

# Shell variables this line binds, so `P=~/.laintas/policy.json; echo x > $P`
# is caught as the overwrite it is. Without this the guard is one assignment
# away from useless — and that assignment is the first thing an agent reaches
# for when a literal path gets refused.
_STATE_ASSIGN_RE = re.compile(r"(?<![\w$.-])([A-Za-z_][A-Za-z0-9_]*)=(?!=)")
_STATE_FOR_RE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+([^;&|]*)",
                           re.IGNORECASE)


# Resolved roots, cached against the raw `paths` values they came from. The
# key is what makes this safe under rebinding: account switching and the test
# suite both reassign paths.LAINTAS_HOME, and a changed value misses the cache
# instead of silently gating the old tree.
_state_roots_cache: tuple = ()
_state_roots_cache_key: tuple | None = None


def _runtime_state_roots() -> tuple:
    """Every directory that holds the runtime's own state, as resolved paths.

    Two, because they are not the same tree once an account is selected:
    ``ROOT_HOME`` stays ``~/.laintas`` and contains *every* account profile
    plus the cross-instance registry, while ``LAINTAS_HOME`` is rebound to the
    active profile inside it. Gating only the active profile would leave
    ``rm -rf ~/.laintas`` — all accounts, all sessions — as an ordinary
    command, and a ``~``-spelled target would resolve outside the guard.

    ``resolve()`` costs ~40µs of syscalls and this runs once per operand on
    every shell command, so the result is cached per distinct binding. A
    symlink re-pointed underneath the state dir mid-process would go unseen;
    doing that already needs the write access this gate exists to protect.
    """
    global _state_roots_cache, _state_roots_cache_key
    key = (str(getattr(paths, "ROOT_HOME", "") or ""),
           str(getattr(paths, "LAINTAS_HOME", "") or ""))
    if key == _state_roots_cache_key:
        return _state_roots_cache
    roots: list = []
    for value in key:
        if not value:
            continue
        try:
            resolved = Path(value).resolve(strict=False)
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    _state_roots_cache_key = key
    _state_roots_cache = tuple(roots)
    return _state_roots_cache


def _state_compare_path(abs_path: str) -> str:
    """Normalize *abs_path* for prefix comparison, avoiding syscalls if possible.

    An absolute path with no ``..`` component is already canonical for prefix
    purposes. Only the shapes that can escape a prefix — relative paths, and
    ``..`` traversal — pay for a real ``resolve()``.
    """
    if abs_path.startswith("/") and "/../" not in abs_path \
            and not abs_path.endswith("/.."):
        return os.path.normpath(abs_path)
    try:
        return str(Path(abs_path).resolve(strict=False))
    except OSError:
        return abs_path


def is_runtime_state_path(abs_path: str) -> bool:
    """True when *abs_path* is a runtime state dir or anything inside one."""
    roots = _runtime_state_roots()
    if not roots:
        return False
    target = _state_compare_path(abs_path)
    for root in roots:
        root_text = str(root)
        if target == root_text or target.startswith(root_text + "/"):
            return True
    return False


def is_policy_config_path(abs_path: str) -> bool:
    """True for the policy file itself — the gate's own configuration.

    Rewriting it can set ``mode: disabled`` and turn off every approval check
    in the process, so it gets its own reason string rather than the generic
    runtime-state one.
    """
    try:
        target = Path(abs_path).resolve(strict=False)
    except OSError:
        return False
    candidates = set()
    for candidate in (CONFIG_PATH,):
        try:
            candidates.add(Path(candidate).resolve(strict=False))
        except (OSError, TypeError):
            continue
    try:
        candidates.add(Path(paths.account_path("POLICY_FILE")).resolve(strict=False))
    except Exception:
        pass
    return target in candidates


def _expand_state_token(token: str) -> str:
    """Resolve one shell token that may spell the state dir via ~ or $HOME."""
    token = token.strip().strip("'\"")
    if not token:
        return ""
    bases = []
    laintas_home = getattr(paths, "LAINTAS_HOME", None)
    if laintas_home:
        bases.append(("$LAINTAS_HOME", laintas_home))
        bases.append(("${LAINTAS_HOME}", laintas_home))
    user_home = Path.home()
    bases += [("$HOME", user_home), ("${HOME}", user_home), ("~", user_home)]
    for name, base in bases:
        if token == name:
            return str(base)
        for sep in ("/", "\\"):
            if token.startswith(name + sep):
                return str(Path(base) / token[len(name) + 1:])
    return token


def _token_may_be_state_path(token: str, cwd: str | None = None) -> bool:
    """Cheap pre-filter: could *token* denote a path inside the state dir?

    ``is_runtime_state_path`` pays a real ``resolve()`` syscall for anything
    that is not already absolute, and an assignment-heavy command
    (``CC=gcc CFLAGS=-O2 LDFLAGS=-lm … make``) hands it a dozen bare words —
    measured at ~1ms of added latency on the hot path before this filter.
    Separators, ``~`` and ``$`` qualify directly. Bare names also qualify
    when execution is already inside runtime state.
    """
    return bool(token) and ("/" in token or "~" in token or "$" in token
                            or (cwd is not None and is_runtime_state_path(cwd)))


def _state_operand_path(token: str, cwd: str | None) -> str:
    target = _expand_state_token(token)
    if target and cwd and not os.path.isabs(target):
        target = os.path.join(cwd, target)
    return target


def _state_bound_vars(command: str, cwd: str | None = None) -> frozenset:
    """Names of shell variables *command* binds to a runtime-state path.

    Covers plain and ``export``ed assignments plus ``for … in`` loop variables.
    A glob counts: ``for f in ~/.laintas/sessions/*.json`` binds *f* to files
    that are state, and the state root is an ancestor of the glob either way.
    """
    bound: set = set()
    for match in _STATE_ASSIGN_RE.finditer(command):
        value = re.split(r"[\s;&|]", command[match.end():], 1)[0]
        if _token_may_be_state_path(value, cwd) \
                and is_runtime_state_path(_state_operand_path(value, cwd)):
            bound.add(match.group(1))
    for match in _STATE_FOR_RE.finditer(command):
        for word in match.group(2).split():
            if _token_may_be_state_path(word, cwd) \
                    and is_runtime_state_path(_state_operand_path(word, cwd)):
                bound.add(match.group(1))
                break
    return frozenset(bound)


def _token_is_state(token: str, var_map: frozenset,
                    cwd: str | None = None) -> bool:
    """True when *token* denotes a path inside the runtime state dir.

    Three spellings, because agents use all three: a literal path, a
    ``~``/``$HOME``/``$LAINTAS_HOME`` prefix, and a variable bound earlier on
    the same line. ``$P/sub`` counts too — it is still inside the tree.
    """
    token = token.strip().strip("'\"")
    if not token:
        return False
    if var_map:
        for name in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", token):
            if name in var_map:
                return True
    if not _token_may_be_state_path(token, cwd):
        return False
    return is_runtime_state_path(_state_operand_path(token, cwd))


def is_runtime_state_mutation(command: str, cwd: str | None = None) -> bool:
    """True when *command* writes, relocates or re-permissions runtime state.

    Two shapes, checked separately because they need different operands:

    * redirection / ``tee`` / ``dd of=`` — only the target can clobber state,
      so ``cat ~/.laintas/events.jsonl > /tmp/out`` stays a free read;
    * in-place or relocating utilities (``mv``, ``cp``, ``sed -i``, ``chmod``,
      …) — every operand is consumed or overwritten, so any of them inside the
      state dir counts. That gates ``cp`` *out* of the state dir too; it is a
      rare operation and one prompt is cheaper than the hole.

    Deletion utilities are not listed here: ``is_delete_command`` already puts
    every ``rm``/``find -delete`` in the always-ask tier, in both modes.
    """
    if not _runtime_state_roots():
        return False
    stripped = _unwrap_parent(command)
    stripped = re.sub(r"^sudo(?:\s+-\S+)*\s+", "", stripped)
    # Inspect every possible literal working directory in the chain. Keeping
    # both sides of conditional cd commands is conservative: a failed cd must
    # not make us forget the original directory. The existing command parser
    # also exposes cd commands inside shell wrappers.
    from command_parse import analyze
    directories = [str(cwd or os.getcwd())]
    for segment in analyze(stripped).segments:
        if segment.program != "cd":
            continue
        try:
            words = shlex.split(segment.raw)[1:]
        except ValueError:
            continue
        operands = [word for word in words if not word.startswith("-")]
        target = operands[0] if operands else str(Path.home())
        for base in list(directories):
            if "$" in target and _token_is_state(target, _state_bound_vars(stripped, base), base):
                for root in _runtime_state_roots():
                    if str(root) not in directories:
                        directories.append(str(root))
            directory = _state_compare_path(_state_operand_path(target, base))
            if directory not in directories:
                directories.append(directory)
                if len(directories) > 32:
                    # Avoid exponential expansion of conditional relative cd
                    # chains. A chain this ambiguous needs human review.
                    return True

    return any(_state_mutation_in_cwd(stripped, base) for base in directories)


def _state_mutation_in_cwd(stripped: str, cwd: str) -> bool:
    var_map = _state_bound_vars(stripped, cwd)

    for pattern in _STATE_WRITE_TARGET_RES:
        for match in pattern.finditer(stripped):
            if _token_is_state(match.group(1), var_map, cwd):
                return True

    for pattern in (_STATE_WRITE_UTIL_RE, _STATE_INPLACE_EDIT_RE):
        inplace_only = pattern is _STATE_INPLACE_EDIT_RE
        for match in pattern.finditer(stripped):
            rest = stripped[match.end():]
            # One command's operands only; a later `;`-separated command gets
            # its own match.
            segment = re.split(r"[;&|]|\bdo\b|\bthen\b", rest, 1)[0]
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            if inplace_only and not any(_is_inplace_flag(t) for t in tokens):
                continue          # read-only sed/perl: ordinary work, no prompt
            for token in tokens:
                if token.startswith("-"):
                    continue
                if _token_is_state(token, var_map, cwd):
                    return True
    return False


# ── Credential material ───────────────────────────────────────────────────
# The shell is the hole the file-write rules do not cover. `denyFileWrite`
# above protects the fs.* tools, but a command line reaches the same files and
# `cat`, `grep`, `find` and `python` are all on the always-allow list — so
# `cat ~/.ssh/id_rsa`, and `cat "…/User Data/Default/Network/Cookies"`, ran
# with no prompt at all on either platform.
#
# Shell commands can read credential stores outside the workspace. Apply
# credential policy to the command itself, independently of fs.* tools.
#
# The tier is *ask*, not *deny* — where Claude Code, Codex CLI and Copilot CLI
# have all settled. Reading a `.env` is ordinary work in half of all
# repositories, and a guard that refuses it teaches people to switch the guard
# off. What is denied is the one unambiguous shape below: reading credential
# material and sending it somewhere in the same breath.

#: POSIX credential stores. Matched against the command line, so a path
#: counts wherever it appears — an argument, a redirect, a `grep -r` root.
_CREDENTIAL_PATTERNS_POSIX = (
    r"/\.ssh/",
    r"(?:^|[\s/'\"])id_(?:rsa|dsa|ecdsa|ed25519)\b",
    r"\.(?:pem|pfx|p12|jks|keystore)\b",
    r"(?:^|[/\s])\.?(?:netrc|npmrc|pypirc)\b",
    r"/\.aws/credentials\b",
    r"/\.config/gcloud/", r"/\.azure/", r"/\.kube/config\b",
    r"/\.docker/config\.json\b",
    r"/\.gnupg/", r"/\.password-store/",
    r"/\.config/gh/hosts\.yml\b",
    r"(?:^|[/\s'\"])\.env(?:\.[\w.-]+)?(?=$|[\s'\";|&])",
    r"/etc/shadow\b", r"/\.local/share/keyrings/",
)

_CREDENTIAL_RE = tuple(
    re.compile(pattern)
    for pattern in _CREDENTIAL_PATTERNS_POSIX
)

#: Commands that can put bytes onto the network. Not a denial on their own —
#: `curl` is on the always-allow list and stays there. This matters only when
#: credential material is named in the same command line.
_EGRESS_RE = re.compile(
    r"(?i)(?:^|[;&|(]\s*|\n\s*|\$\(|`|\|\s*)"
    r"(?:\S*[/\\])?"
    r"(?:curl|wget|nc|ncat|netcat|socat|scp|sftp|ftp|telnet)"
    r"(?:\s|$)"
)


def names_credential_material(command: str) -> bool:
    """Whether *command* names a credential store, or reads one out.

    Deliberately textual. The point is to notice that a secret is in play
    before anything runs, and resolving the paths first would mean touching
    the filesystem on behalf of a command nobody has approved yet.
    """
    stripped = _unwrap_parent(command)
    return any(rule.search(stripped) for rule in _CREDENTIAL_RE)


def is_credential_exfiltration(command: str) -> bool:
    """Credential material and a way off the machine, in one command line.

    `curl -F f=@~/.aws/credentials https://…` is not a judgement call, and not
    something a user should have to adjudicate inside an approval prompt while
    they are waiting on an answer. It is the one shape here refused outright.
    """
    stripped = _unwrap_parent(command)
    return names_credential_material(stripped) and bool(_EGRESS_RE.search(stripped))


def evaluate(command: str, cwd: str = None,
             req_id: str = None, agent_id: str = None,
             *, strict: bool = False) -> PolicyDecision:
    """Evaluate a command against the security policy.

    Returns a PolicyDecision with action in {"allow", "deny", "needs_approval"}.
    Side-effect: writes an audit log entry for every non-trivial decision.

    When *strict* is True, ``needs_approval`` decisions are returned as-is
    instead of being downgraded to ``allow`` in audit mode.  Used by callers
    that supervise untrusted/remote sources (where the audit-mode advisory
    fallthrough must NOT silently let through commands the rules flagged).
    """
    import private_files
    privacy_error = private_files.execution_error()
    if privacy_error:
        return PolicyDecision(action='deny', reason=privacy_error)
    cfg = _load_config()
    mode = cfg.get("mode", "audit")
    if strict and mode == "audit":
        # Treat as enforce for rule matching, but preserve the user's
        # configured mode for the audit-log text.
        effective_mode = "enforce"
    else:
        effective_mode = mode

    stripped = _unwrap_parent(command)

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

    # ── Decompose before matching ──────────────────────────────────────
    # Rules are matched against every command this line will actually run, not
    # just the string as typed. `\rm`, `"rm"`, `$X`, `eval "…"` and `… | sh` all
    # reach the same binary; matching the raw text alone is the bug class that
    # walked past ten of eleven agents in Adversa's 2026 survey.
    variants, obf_risks = _command_variants(stripped)

    # ── Check deny list first (takes precedence) ───────────────────────
    deny_rules = _get_compiled_rules("deny", cfg)
    for rule in deny_rules:
        for variant in variants:
            if rule.search(variant):
                disguised = variant != stripped
                reason = f"Matched deny rule: {rule.pattern}"
                if disguised:
                    reason += f" (resolved from obfuscated form as: {variant})"
                _write_audit(_audit_entry(command, "deny", reason, cwd, req_id, agent_id))
                if mode in ("enforce", "audit"):
                    # Deny blocks in audit mode too — unchanged behaviour.
                    return PolicyDecision("deny", rule.pattern, reason)

    # ── Unresolvable command words ─────────────────────────────────────
    # `$(…)` or an unknown variable in command position means we cannot say what
    # will run. Guessing "probably fine" is how these guards fail; the honest
    # answer is to put a human in the loop. Approval rather than denial, because
    # command substitution is also ordinary shell usage in legitimate work.
    if mode != "disabled" and command_parse_unresolved(obf_risks):
        # Two different unknowns, and telling them apart matters to whoever
        # reads the prompt: a command word that does not exist yet, versus a
        # command we can read perfectly that hands work to a file or blob we
        # cannot. Reporting the second as the first describes a substitution
        # that is not in the command, and the agent then rewrites the quoting
        # instead of showing the script.
        if command_parse_opaque(obf_risks):
            reason = ("Runs a script or encoded payload whose contents cannot "
                      "be read from the command line")
        else:
            reason = ("Command name is produced at runtime (substitution or variable); "
                      "what will execute cannot be determined before it runs")
        _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # Shared temporary roots are multi-tenant state. This is denied rather
    # than approved because the target set is dynamic and cannot be reviewed
    # reliably: the exact incident command selected `/tmp` itself and erased
    # tmux's control socket while its detached processes kept running.
    if mode != "disabled" and is_unsafe_shared_temp_cleanup(stripped):
        reason = ("unsafe shared-temp cleanup: find starts at the shared root "
                  "without -mindepth 1 and feeds a delete operation")
        _write_audit(_audit_entry(command, "deny", reason,
                                  cwd, req_id, agent_id))
        return PolicyDecision("deny", "shared-temp-root", reason)

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
    # Across variants, not just the typed string: the delete may be the payload
    # of a wrapper (`bash -c "rm -rf x"`), where the
    # boundary anchors in is_delete_command cannot see it. The destructive-git
    # check below has always done this; the delete tier had not, which left the
    # always-ask guarantee one quote away from being optional.
    if mode != "disabled" and any(is_delete_command(v) for v in variants):
        reason = "delete command always requires approval (audit and enforce modes alike)"
        _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # ── Destructive git: same always-ask tier ───────────────────────────
    # `git clean -fdx`, `reset --hard`, `checkout -- .`, `push --force`,
    # `branch -D`, `stash drop`… destroy work exactly like `rm` does, but the
    # ordinary git rules in "needs_approval" are ADVISORY in audit mode (the
    # default), so before this check they ran with no prompt at all. Checked
    # across command_parse variants so an obfuscated form cannot slip past.
    if mode != "disabled" and any(is_destructive_git_command(v) for v in variants):
        reason = ("destructive git command always requires approval "
                  "(audit and enforce modes alike)")
        _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # ── Runtime state mutation: same always-ask tier ────────────────────
    # The shell counterpart of the evaluate_file_write() check above. Without
    # it the fs.* gate is decorative: `echo '{"mode":"disabled"}' >
    # ~/.laintas/policy.json` was an ordinary allow, so the approval gate could
    # switch itself off with one redirect, and `mv ~/.laintas/sessions …`,
    # `sed -i … events.jsonl` or `chmod -R 000 ~/.laintas` all ran silently.
    # Checked across variants so a wrapper (`bash -c "…"`) cannot hide it.
    # Reads stay free — inspecting events.jsonl is routine work.
    if mode != "disabled" and any(is_runtime_state_mutation(v, cwd) for v in variants):
        reason = ("command writes runtime state under ~/.laintas; always "
                  "requires approval (audit and enforce modes alike)")
        _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # ── Credential material: deny the exfiltration shape, ask about the rest ─
    # Checked across variants for the same reason the delete tier is: the read
    # may be the payload of a wrapper (`bash -c "cat ~/.ssh/id_rsa"`,
    # another shell wrapper), where matching the typed line alone
    # sees nothing. Placed after the delete and git tiers so a command that is
    # both keeps the more specific prompt.
    if mode != "disabled" and any(is_credential_exfiltration(v) for v in variants):
        reason = ("reads credential material and sends it off the machine in "
                  "the same command")
        _write_audit(_audit_entry(command, "deny", reason, cwd, req_id, agent_id))
        return PolicyDecision("deny", "credential-exfiltration", reason)

    if mode != "disabled" and any(names_credential_material(v) for v in variants):
        reason = ("touches credential material (keys, cookies, tokens or a "
                  "credential store) — always asks, in audit and enforce "
                  "modes alike")
        _write_audit(_audit_entry(command, "needs_approval", reason,
                                  cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "credential-access", reason)

    # An unbounded walk of a tree nobody sized is the shape that killed a
    # session outright: `fs.glob` over a drive root never returned, and the
    # native tools now bound themselves. A shell command has nobody to stop
    # it, so the ceiling has to be the user's answer.
    if mode != "disabled" and any(is_unbounded_recursion(v, cwd) for v in variants):
        reason = ("recursive walk with no depth bound, outside the working "
                  "directory: bound it (-maxdepth, a named subdirectory) or "
                  "approve the full scan")
        _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # ── Check needs_approval list ──────────────────────────────────────
    # Deny rules take precedence over sudo approval.  Otherwise approving
    # `sudo rm -rf /` would bypass the destructive-command deny list.
    if sudo_detected and effective_mode == "enforce":
        return PolicyDecision(
            "needs_approval", "sudo", "sudo commands require approval")

    approval_rules = _get_compiled_rules("needs_approval", cfg)
    for rule in approval_rules:
        # S1 (bughunt): match every variant, not just the string as typed.
        # A rule written with a literal single space (`rm -rf\s`) was walked
        # past by `rm  -rf` (double space). The deny loop above already
        # matches per-variant; approval rules get the same treatment. The
        # original string is always variants[0], so existing matches keep
        # firing — this can only add, never remove, an approval.
        for variant in variants:
            if rule.search(variant):
                reason = f"Matched approval rule: {rule.pattern}"
                if variant != stripped:
                    reason += f" (resolved from obfuscated form as: {variant})"
                _write_audit(_audit_entry(command, "needs_approval", reason, cwd, req_id, agent_id))
                if effective_mode == "enforce":
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
        if effective_mode == "enforce":
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


# ── Runtime sandbox write grants ──────────────────────────────────────────
# Process-local directory prefixes that trusted caller code (an extension,
# after showing the user the exact paths) has pre-approved for agent file
# writes - e.g. blindpick's disposable git-worktree sandboxes. Enforce mode
# prompts on every write, and a background sub-agent's write cannot render
# an interactive prompt on the main UI thread without breaking it. Grants
# are evaluated AFTER denyFileWrite and never reopen a protected target;
# they vanish with the CLI process.

_sandbox_write_prefixes: list[str] = []
_sandbox_write_lock = threading.Lock()


def grant_file_write_prefix(prefix: str) -> str:
    """Pre-approve file writes under *prefix* for this process.

    Returns the normalized granted prefix ("" when the path is unusable).
    """
    try:
        normalized = str(Path(prefix).resolve(strict=False))
    except OSError:
        return ""
    if not normalized or normalized == "/":
        return ""
    with _sandbox_write_lock:
        if normalized not in _sandbox_write_prefixes:
            _sandbox_write_prefixes.append(normalized)
    return normalized


def revoke_file_write_prefix(prefix: str) -> None:
    """Drop one prefix from the runtime sandbox grant set."""
    try:
        normalized = str(Path(prefix).resolve(strict=False))
    except OSError:
        normalized = str(prefix)
    with _sandbox_write_lock:
        _sandbox_write_prefixes[:] = [
            p for p in _sandbox_write_prefixes if p != normalized]


def _sandbox_write_allowed(abs_path: str) -> bool:
    with _sandbox_write_lock:
        prefixes = tuple(_sandbox_write_prefixes)
    for prefix in prefixes:
        try:
            Path(abs_path).relative_to(prefix)
            return True
        except (ValueError, OSError):
            continue
    return False


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

    # ── Runtime state (~/.laintas): always-ask tier ─────────────────────
    # Placed BEFORE the sandbox grant on purpose, mirroring the deny loop: a
    # prefix grant exists to let disposable worktree sandboxes write freely,
    # and must never reopen the runtime's own live state. policy.json gets its
    # own reason string because rewriting it can set `mode: disabled` and turn
    # off every approval check in this process — the gate must not be editable
    # by the thing it gates.
    if mode != "disabled" and is_runtime_state_path(abs_path):
        if is_policy_config_path(abs_path):
            reason = ("writing the policy file itself always requires approval "
                      "— it configures this gate and can disable it")
        else:
            reason = ("writing runtime state under ~/.laintas always requires "
                      "approval (live events, sessions, memories, registry)")
        _write_audit(_audit_entry(label, "needs_approval", reason, cwd, req_id, agent_id))
        return PolicyDecision("needs_approval", "", reason)

    # ── Runtime sandbox grant: trusted caller pre-approved this prefix ──
    # Placed after the deny loop on purpose: a grant must never reopen a
    # protected target (.ssh, keys, .git/config ...).
    if _sandbox_write_allowed(abs_path):
        _write_audit(_audit_entry(
            label, "allow", "runtime sandbox write grant", cwd, req_id, agent_id))
        return PolicyDecision("allow")

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



def evaluate_terminal_open(cwd: str = None, req_id: str = None, agent_id: str = None) -> PolicyDecision:
    """Evaluate a term-open request (Helpwo asking to attach a real, raw PTY).

    Opening is intentionally not a per-connection approval boundary. The user
    already opted in locally with `disable_remote_terminal=false`; the gateway
    independently verifies account ownership and issues a one-use browser
    ticket. A prompt rendered in the same web session cannot protect against a
    stolen web session and makes direct device-picker connections unreliable.

    The open remains audit-logged in every policy mode. Commands dispatched by
    AI/remote-exec and destructive file operations retain their own policy and
    approval checks; this exception applies only to creating the interactive
    PTY transport.
    """
    label = "terminal.open"
    mode = _load_config().get("mode", "audit")
    _write_audit(_audit_entry(label, "allow", f"direct terminal connection ({mode} mode)", cwd, req_id, agent_id))
    return PolicyDecision("allow")


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

    S5 (bughunt): two rules here.
      1. The file written back is the RAW LOCAL config. _load_config()
         returns the org-merged view; persisting that froze the
         organisation's rules into the user's local file, so an org
         policy update later looked unchanged (and the local file
         betrayed where it came from).
      2. A request that weakens the effective mode below what the org
         policy enforces is refused — org policy is a floor, not a
         suggestion the local file can vote away.
    """
    mode = (mode or "").strip().lower()
    valid = {"audit", "enforce", "disabled"}
    if mode not in valid:
        return False, f"Invalid mode '{mode}'. Valid: {', '.join(sorted(valid))}"
    strength = {"disabled": 0, "audit": 1, "enforce": 2}
    cfg = _load_config(force=True)          # effective (org-merged) view
    old = cfg.get("mode", "audit")
    # Raw local config, exactly what the file holds. When it cannot be read,
    # start from the shipped defaults (as _load_config does) — never from the
    # merged view, which would freeze org rules into the local file.
    local = None
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                local = raw
    except (OSError, json.JSONDecodeError):
        local = None
    if local is None:
        local = dict(_DEFAULT_CONFIG)
        local["allowedRoots"] = _default_allowed_roots()
    # Org floor: apply org policy over the proposed mode; if it lifts the
    # mode stricter than requested, the request is a weakening attempt.
    probe = dict(local)
    probe["mode"] = mode
    effective = _apply_org_policy(dict(probe)).get("mode", mode)
    if strength.get(effective, 0) > strength.get(mode, 0):
        return False, (f"Cannot set mode '{mode}': organisation policy "
                       f"enforces '{effective}'. Org policy is a floor, "
                       f"not a default.")
    local["mode"] = mode
    cfg = local
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
    # The cache holds the EFFECTIVE config: the file is local-only, but every
    # evaluate() must still see the org rules. Caching the raw local dict here
    # (with a matching mtime) silently dropped org policy until the next edit.
    global _config, _config_mtime, _config_org_version
    with _config_lock:
        _config = _apply_org_policy(dict(cfg))
        _config_org_version = _current_org_version()
        try:
            _config_mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            pass
    return True, f"Policy mode: {old} → {mode}"
