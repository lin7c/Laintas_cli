"""Git-worktree isolation for concurrently spawned sub-agents.

Every AI-spawned sub-agent (agent.spawn / spawn_parallel / spawn_chain) used to
share the exact same ``ctx.cwd`` as its parent — literally the process's
``os.getcwd()``, which is one value for the whole process regardless of how
many agent threads are running. Two sub-agents editing overlapping files (or
even a sub-agent racing its own parent) had no isolation at all: last write
wins, silently.

This mirrors opencode's Worktree service (``git worktree add`` per isolated
task) with one deliberate difference: opencode seeds a new worktree from
committed HEAD only. Here, a spawned sub-agent is almost always meant to
continue the user's *current* work-in-progress, not start from a clean
commit — so ``create_isolated_worktree`` also replicates the parent's
uncommitted tracked changes and untracked files into the new worktree.

When the sub-agent finishes, ``merge_worktree_back`` copies its edits back
into the parent tree file-by-file, but only where the parent tree hasn't
diverged from the snapshot taken at worktree-creation time for that exact
path. Anything that conflicts (parent touched the same file while the child
was working) is left untouched in the worktree and reported, rather than
silently overwritten in either direction.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class WorktreeError(Exception):
    pass


# Serializes read-then-write access to a given repo's working tree across
# concurrently finishing sub-agents. Two children merging back at nearly the
# same instant both read a file's current hash before either writes — without
# this, that read-then-write window is itself an unguarded race (the exact
# class of bug this module exists to close). Keyed by repo_root so unrelated
# repos never contend with each other.
_repo_locks: Dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _lock_for(repo_root: str) -> threading.Lock:
    with _repo_locks_guard:
        lock = _repo_locks.get(repo_root)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[repo_root] = lock
        return lock


def _git(cwd: str, *args: str, timeout: int = 60):
    """Run git and return (code, stdout, stderr) with stdout UNSTRIPPED —
    `git status --porcelain` depends on fixed-width leading status columns,
    so trimming the whole blob (not just trailing newline) corrupts the
    first line's parsing. Only strip stderr and trim the trailing newline."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "")
        if out.endswith("\n"):
            out = out[:-1]
        return proc.returncode, out, (proc.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def repo_root(cwd: str) -> Optional[str]:
    rc, out, _ = _git(cwd, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else None


def is_git_repo(cwd: str) -> bool:
    """True only when cwd is inside a git repo WITH at least one commit.

    A bare ``.git`` directory with no commits (e.g. ``git init`` run but
    nothing committed yet) returns False - ``git worktree add`` requires a
    HEAD commit to branch from, so worktree isolation is impossible there.
    Without this check, spawn_subagent treats the empty repo as isolatable,
    create_isolated_worktree raises WorktreeError("cannot resolve HEAD"),
    and by design (agent_loop.py:3117) that error is fatal instead of
    silently falling back to the shared cwd."""
    root = repo_root(cwd)
    if root is None:
        return False
    rc, _, _ = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    return rc == 0


def _file_hash(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# The one path inside `.laintas/` that is project content, not runtime state.
_CONTRACT_PREFIX = ".laintas/contract/"


def _relevant_files(root: str) -> set:
    """Tracked + untracked-but-not-gitignored relative paths. Using git to
    enumerate (instead of os.walk) keeps node_modules/venv/.git out for free
    and matches exactly what a `git add -A` would pick up.

    `.laintas/` is always excluded even if the project's own .gitignore
    hasn't been set up to cover it yet (it's documented convention, not
    enforced) — it holds laintas_cli's own runtime state, including nested
    worktrees created for grandchild sub-agents, which must never be picked
    up as "content" to seed into or merge back from a worktree.

    `.laintas/contract/` is the exception, and has to be: it is the API
    agreement with the frontend, not runtime state. An agent sent into a
    worktree to build an endpoint needs to read what it agreed to and needs
    its `implement`/`verify` result to come back out — excluding it would
    leave the contract permanently stale on one side of every worktree."""
    files = set()
    rc, out, _ = _git(root, "ls-files")
    if rc == 0:
        files.update(l for l in out.splitlines() if l.strip())
    rc, out, _ = _git(root, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        files.update(l for l in out.splitlines() if l.strip())
    return {f for f in files
            if f.startswith(_CONTRACT_PREFIX)
            or (f != ".laintas" and not f.startswith(".laintas/"))}


def _changed_vs_head(root: str) -> List[str]:
    """Relative paths of every file that differs from HEAD or is untracked
    (what `git add -A` would stage right now). Excludes `.laintas/` for the
    same reason as _relevant_files — it's laintas_cli's own runtime state
    (including nested worktrees), never project content to replicate — with
    the same `.laintas/contract/` exception, so an endpoint built in a
    worktree brings its contract update back with it."""
    rc, out, err = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if rc != 0:
        raise WorktreeError(f"git status failed: {err}")
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        rest = line[3:]
        if " -> " in rest:  # renames
            rest = rest.split(" -> ", 1)[1]
        rel = rest.strip().strip('"')
        if not rel.startswith(_CONTRACT_PREFIX) and (
                rel == ".laintas" or rel.startswith(".laintas/")):
            continue
        paths.append(rel)
    return paths


@dataclass
class WorktreeInfo:
    path: str
    branch: str
    repo_root: str
    base_commit: str
    baseline_hashes: Dict[str, str] = field(default_factory=dict)


def create_isolated_worktree(base_cwd: str, label: str = "agent") -> WorktreeInfo:
    """Create a git worktree seeded from base_cwd's CURRENT state (including
    uncommitted/untracked changes). Raises WorktreeError on any failure — the
    caller decides whether to fall back to the shared cwd."""
    root = repo_root(base_cwd)
    if not root:
        raise WorktreeError(f"{base_cwd} is not inside a git repository")

    rc, head, err = _git(root, "rev-parse", "HEAD")
    if rc != 0:
        raise WorktreeError(f"cannot resolve HEAD: {err}")

    worktrees_dir = os.path.join(root, ".laintas", "worktrees")
    os.makedirs(worktrees_dir, exist_ok=True)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or "agent"))[:40] or "agent"
    name = f"{slug}-{uuid.uuid4().hex[:8]}"
    path = os.path.join(worktrees_dir, name)
    branch = f"laintas/{name}"

    rc, out, err = _git(root, "worktree", "add", "-b", branch, path, head, timeout=90)
    if rc != 0:
        raise WorktreeError(f"git worktree add failed: {err or out}")

    # Stamp ownership as soon as the tree exists, before the WIP replication
    # below — a crash partway through that copy still leaves a checkout on
    # disk, and it should be reclaimable like any other orphan. Registering
    # the root is part of the same guarantee: a later CLI has to be able to
    # find this repo without being launched from inside it.
    _write_owner(root, name)
    _register_root(root)

    # Replicate the parent's uncommitted WIP into the new worktree so the
    # child continues real work instead of a stale clean checkout. Locked
    # against concurrent merge_worktree_back calls from sibling agents
    # finishing at the same instant — both read-then-copy the parent tree,
    # so without a shared lock the read and the copy could interleave with
    # another agent's write.
    with _lock_for(root):
        try:
            changed = _changed_vs_head(root)
        except WorktreeError:
            changed = []
        for rel in changed:
            src = os.path.join(root, rel)
            dst = os.path.join(path, rel)
            if not os.path.exists(src):
                if os.path.lexists(dst):
                    os.remove(dst)
                continue
            os.makedirs(os.path.dirname(dst) or path, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass

    # Full baseline snapshot = parent's exact state at this instant, for
    # every file relevant to a future merge-back diff.
    baseline = {}
    for rel in _relevant_files(path):
        h = _file_hash(os.path.join(path, rel))
        if h:
            baseline[rel] = h

    return WorktreeInfo(path=path, branch=branch, repo_root=root,
                        base_commit=head, baseline_hashes=baseline)


def _merge_write_allowed(dest_path: str, repo_root: str) -> bool:
    """Policy check for one merge-back destination. Fails closed on error."""
    try:
        import policy
    except Exception:
        return True
    try:
        return policy.evaluate_file_write(dest_path, repo_root).action != "deny"
    except Exception:
        return False


def merge_worktree_back(info: WorktreeInfo) -> dict:
    """Copy the child's changes back into repo_root wherever the parent tree
    hasn't diverged from `info.baseline_hashes` for that path. Returns
    {"applied": [...], "conflicts": [...]}. Never raises — best-effort.

    Locked per repo_root: two sibling agents finishing at nearly the same
    instant both do a read-current-hash-then-copy per file: without a lock,
    that check-then-act window could interleave between two concurrent
    merges the same way the original shared-cwd bug did, just narrowed to a
    much smaller window instead of eliminated."""
    applied: List[str] = []
    conflicts: List[str] = []
    blocked: List[str] = []

    with _lock_for(info.repo_root):
        try:
            current = _relevant_files(info.path)
        except Exception:
            current = set()
        all_paths = set(info.baseline_hashes) | current

        for rel in sorted(all_paths):
            child_path = os.path.join(info.path, rel)
            child_hash = _file_hash(child_path) if os.path.exists(child_path) else None
            baseline_hash = info.baseline_hashes.get(rel)
            if child_hash == baseline_hash:
                continue  # child never touched this file

            parent_path = os.path.join(info.repo_root, rel)
            # The merge is a real write to the user's tree, performed by
            # shutil.copy2 rather than fs.write — so it is the one file-write
            # path that never met the policy engine. Evaluate the DESTINATION
            # (the child was only ever evaluated against its worktree copy):
            # a deny rule that protects ~/.ssh or a key file must protect it
            # here too. Approval-tier decisions are not re-asked — the child
            # already obtained one for this exact content — but a deny is a
            # deny, and the file stays behind in the worktree, reported.
            if not _merge_write_allowed(parent_path, info.repo_root):
                blocked.append(rel)
                continue
            parent_hash = _file_hash(parent_path) if os.path.exists(parent_path) else None
            if parent_hash != baseline_hash:
                # Parent tree moved on this exact path since the worktree was
                # created — don't silently clobber either side.
                conflicts.append(rel)
                continue

            try:
                if child_hash is None:
                    if os.path.exists(parent_path):
                        os.remove(parent_path)
                else:
                    os.makedirs(os.path.dirname(parent_path) or info.repo_root, exist_ok=True)
                    shutil.copy2(child_path, parent_path)
                applied.append(rel)
            except OSError:
                conflicts.append(rel)

    return {"applied": applied, "conflicts": conflicts, "blocked": blocked}


def remove_worktree(info: WorktreeInfo) -> bool:
    """Remove the worktree directory and its throwaway branch. Best-effort."""
    rc, out, err = _git(info.repo_root, "worktree", "remove", "--force", info.path)
    ok = rc == 0
    if not ok and os.path.isdir(info.path):
        try:
            shutil.rmtree(info.path, ignore_errors=True)
            _git(info.repo_root, "worktree", "prune")
        except OSError:
            pass
    _git(info.repo_root, "branch", "-D", info.branch)
    _clear_owner(info.repo_root, os.path.basename(info.path.rstrip(os.sep)))
    return ok


# ── Orphan reclamation ─────────────────────────────────────────────────────
# `remove_worktree` is correct but only runs when the spawn frame reaches its
# teardown. Three paths skip it: a merge conflict and a merge exception both
# deliberately leave the checkout "for manual review", and a killed CLI never
# gets to run teardown at all. None of those is ever revisited, so worktrees
# accumulate — one deployment reached 78 of them, 8.8 GB, and every recursive
# grep in that repo returned each hit 78 times.
#
# The reaper does not override the deliberate keeps; it puts a clock on them.
# What survives is the *content* (a patch), not a full checkout of the repo.

_OWNERS_DIRNAME = ".owners"
_REAPED_DIRNAME = ".reaped"
DEFAULT_ORPHAN_GRACE_SECONDS = 6 * 3600
MAX_REAPED_ARCHIVES = 50

# Orphans live under the repo a sub-agent worked in, which has nothing to do
# with the cwd the next CLI happens to start from. Scoping the sweep to
# `os.getcwd()` meant a CLI launched from a non-repo directory resolved no
# repo root and returned before looking at anything — 31 orphans and 3.6 GB
# accumulated under a repo one level down while startup reported success.
# Each repo that ever hosts a worktree registers itself here instead, so the
# sweep covers where the worktrees are rather than where the shell was.
_ROOTS_DIRNAME = "worktree_roots"


def _roots_dir() -> str:
    try:
        import paths
        home = str(paths.LAINTAS_HOME)
    except Exception:
        home = os.environ.get("LAINTAS_HOME") or os.path.join(
            os.path.expanduser("~"), ".laintas")
    return os.path.join(home, _ROOTS_DIRNAME)


def _root_marker(root: str) -> str:
    key = hashlib.sha1(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_roots_dir(), key)


def _register_root(root: str) -> None:
    """Note that `root` hosts worktrees, for later sweeps.

    One file per repo rather than one shared list: sub-agents register
    concurrently from separate processes, and a read-modify-write on a shared
    file would drop entries under exactly that race — and a dropped entry is
    a repo that never gets swept, which is the bug this exists to fix.
    """
    try:
        os.makedirs(_roots_dir(), mode=0o700, exist_ok=True)
        path = _root_marker(root)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(os.path.realpath(root))
        os.replace(tmp, path)
    except OSError:
        pass          # registration is best-effort; an unswept repo is not fatal


def _forget_root(root: str) -> None:
    try:
        os.remove(_root_marker(root))
    except OSError:
        pass


def known_worktree_roots() -> List[str]:
    """Every registered repo root that still exists on disk."""
    roots = []
    try:
        entries = sorted(os.listdir(_roots_dir()))
    except OSError:
        return roots
    for entry in entries:
        if entry.endswith(".tmp"):
            continue
        marker = os.path.join(_roots_dir(), entry)
        try:
            with open(marker, encoding="utf-8") as fh:
                root = fh.read().strip()
        except OSError:
            continue
        if root and os.path.isdir(root):
            roots.append(root)
        else:
            try:
                os.remove(marker)         # repo deleted → stop tracking it
            except OSError:
                pass
    return roots


def _worktrees_dir(root: str) -> str:
    return os.path.join(root, ".laintas", "worktrees")


def _owner_path(root: str, name: str) -> str:
    return os.path.join(_worktrees_dir(root), _OWNERS_DIRNAME, f"{name}.json")


def _boot_id() -> str:
    """This boot's unique id, or "" where the kernel does not expose one.

    A pid only identifies a process within one boot. After a reboot the same
    number is handed out again from 1, so an owner record written before a
    crash-and-reboot would otherwise vouch for a completely unrelated process.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _pid_starttime(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat: clock ticks since boot at process start.

    (pid, starttime) is unique for a boot even though pid alone is not, which
    is what lets a recycled pid be told apart from the original owner.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        # comm (field 2) is parenthesised and may itself contain spaces, so
        # split after the last ')' rather than on whitespace from the start.
        tail = data[data.rindex(")") + 2:].split()
        return tail[19]                  # field 22 == index 19 of the tail
    except (OSError, ValueError, IndexError):
        return ""


def _has_cmdline(pid: int) -> bool:
    """True if pid is a userspace process. Kernel threads have no cmdline.

    Sole gate for legacy records that predate starttime stamping. Three of
    those in one deployment carried ``"pid": 2`` — kthreadd, which never
    exits, so those worktrees were immortal under a bare liveness check.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return bool(fh.read().strip(b"\x00"))
    except OSError:
        return False


def _write_owner(root: str, name: str) -> None:
    """Record which live process owns a worktree.

    Age alone cannot tell a crashed run from a slow one, and reaping a
    still-running sub-agent's checkout out from under it is far worse than
    leaving a stale directory another few hours.

    The record pins the owner to one boot and one process start, so a reused
    pid cannot inherit a dead agent's claim on a checkout.
    """
    import json
    path = _owner_path(root, name)
    pid = os.getpid()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": pid,
                       "started": time.time(),
                       "boot_id": _boot_id(),
                       "starttime": _pid_starttime(pid)}, fh)
    except OSError:
        pass          # ownership is an optimisation; the age gate still applies


def _clear_owner(root: str, name: str) -> None:
    try:
        os.remove(_owner_path(root, name))
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True if `pid` is running. Unknown states answer True (do not reap)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # alive, owned by another user
    except OSError:
        return True                      # unknown → assume alive
    return True


def _owner_alive(owner: dict) -> bool:
    """True if the process that claimed a worktree is still that same process.

    Liveness alone is not identity. A bare `pid` can be reused by an unrelated
    process, survive a reboot as a different one entirely, or — as happened
    here — name a kernel thread that never exits, which pins the worktree
    forever. Each recorded field that is present must agree; a field that is
    absent (older record) falls back to the weaker check below it.
    """
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False                     # pid 0/1 are never a sub-agent

    boot = owner.get("boot_id")
    current_boot = _boot_id()
    if boot and current_boot and boot != current_boot:
        return False                     # written before a reboot → gone

    recorded_start = owner.get("starttime")
    if recorded_start:
        # Strongest check: exact process identity within this boot.
        return _pid_starttime(pid) == recorded_start

    # Legacy record with only a pid. Require at minimum that the pid names a
    # userspace process — a kernel thread is never a sub-agent, whatever the
    # liveness call says.
    return _pid_alive(pid) and _has_cmdline(pid)


def _cwds_in_use() -> set:
    """Every directory some live process is sitting in, resolved once.

    Backstop for worktrees created before ownership records existed: without
    it, the age gate alone could reap a long-running legacy sub-agent.
    """
    seen = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return seen
    for pid in pids:
        try:
            seen.add(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
    return seen


def _archive_before_reap(path: str, root: str, name: str) -> List[str]:
    """Save an orphan's unique content as patches before the tree is deleted.

    A worktree kept "for manual review" holds real work. Deleting it outright
    to reclaim disk would discard that; a diff keeps it at a thousandth of the
    size, which is what makes reaping safe enough to do automatically.
    """
    written: List[str] = []
    out_dir = os.path.join(_worktrees_dir(root), _REAPED_DIRNAME)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return written

    rc, head, _ = _git(root, "rev-parse", "HEAD")
    pieces = [("diff", ("diff", "HEAD"))]
    if rc == 0 and head:
        # Commits the parent does not have. Usually none — the checkout is a
        # snapshot, not a branch someone pushed to — but it costs one call.
        pieces.append(("patch", ("format-patch", "--stdout", f"{head.strip()}..HEAD")))

    for suffix, args in pieces:
        try:
            rc2, body, _ = _git(path, *args, timeout=120)
        except Exception:
            continue
        if rc2 != 0 or not (body or "").strip():
            continue
        dest = os.path.join(out_dir, f"{name}.{suffix}")
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(body)
            written.append(dest)
        except OSError:
            pass
    return written


def _prune_reaped_archives(root: str, keep: int = MAX_REAPED_ARCHIVES) -> None:
    """Cap the archive so reclaiming disk does not start its own slow leak."""
    out_dir = os.path.join(_worktrees_dir(root), _REAPED_DIRNAME)
    try:
        entries = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
    except OSError:
        return
    files = [(os.path.getmtime(f), f) for f in entries if os.path.isfile(f)]
    files.sort(reverse=True)
    for _, stale in files[keep:]:
        try:
            os.remove(stale)
        except OSError:
            pass


def reap_orphan_worktrees(base_cwd: str, *,
                          grace_seconds: int = DEFAULT_ORPHAN_GRACE_SECONDS,
                          dry_run: bool = False) -> dict:
    """Remove worktrees whose owning process is gone. Best-effort, never raises.

    A worktree is reaped only when BOTH gates agree: its owner is provably
    dead (or unrecorded), AND it is older than `grace_seconds`. Requiring both
    means a recycled PID or a missing record costs a delay, never a deletion.
    """
    result = {"reaped": [], "kept": [], "archived": [], "error": ""}
    try:
        root = repo_root(base_cwd)
        if not root:
            return result
        wt_dir = _worktrees_dir(root)
        if not os.path.isdir(wt_dir):
            return result

        import json
        now = time.time()
        me = os.getpid()
        in_use = None                     # scanned lazily; only legacy needs it

        for entry in sorted(os.listdir(wt_dir)):
            if entry.startswith("."):     # .owners / .reaped are not worktrees
                continue
            path = os.path.join(wt_dir, entry)
            if not os.path.isdir(path):
                continue

            owner = None
            try:
                with open(_owner_path(root, entry), encoding="utf-8") as fh:
                    owner = json.load(fh)
            except (OSError, ValueError):
                owner = None

            if owner is not None:
                if owner.get("pid") == me or _owner_alive(owner):
                    result["kept"].append(path)
                    continue
                born = float(owner.get("started") or 0)
            else:
                # No record: created by an older build, or the record was lost.
                # Fall back to mtime, and refuse to touch anything a live
                # process is sitting in.
                if in_use is None:
                    in_use = _cwds_in_use()
                if path in in_use or os.path.realpath(path) in in_use:
                    result["kept"].append(path)
                    continue
                try:
                    born = os.path.getmtime(path)
                except OSError:
                    result["kept"].append(path)
                    continue

            if now - born < grace_seconds:
                result["kept"].append(path)
                continue
            if dry_run:
                result["reaped"].append(path)
                continue

            result["archived"].extend(_archive_before_reap(path, root, entry))
            remove_worktree(WorktreeInfo(path=path, branch=f"laintas/{entry}",
                                         repo_root=root, base_commit=""))
            _clear_owner(root, entry)
            result["reaped"].append(path)

        if result["reaped"] and not dry_run:
            _git(root, "worktree", "prune")
            _prune_reaped_archives(root)
    except Exception as exc:              # never let housekeeping break startup
        result["error"] = str(exc)
    return result


def reap_all_worktree_roots(base_cwd: Optional[str] = None, **kwargs) -> dict:
    """Reap orphans in every repo known to host worktrees.

    The startup entry point. `base_cwd`'s repo is swept too even if it never
    registered (worktrees created by an older build), and a root whose
    worktrees directory is gone is dropped from the registry so it is not
    re-scanned forever.
    """
    merged = {"reaped": [], "kept": [], "archived": [], "error": "", "roots": []}
    roots = known_worktree_roots()
    if base_cwd:
        own = repo_root(base_cwd)
        if own and os.path.realpath(own) not in {os.path.realpath(r) for r in roots}:
            roots.append(own)

    errors = []
    for root in roots:
        if not os.path.isdir(_worktrees_dir(root)):
            _forget_root(root)
            continue
        merged["roots"].append(root)
        result = reap_orphan_worktrees(root, **kwargs)
        for key in ("reaped", "kept", "archived"):
            merged[key].extend(result.get(key) or [])
        if result.get("error"):
            errors.append(f"{root}: {result['error']}")
    merged["error"] = "; ".join(errors)
    return merged
