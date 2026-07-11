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
    return repo_root(cwd) is not None


def _file_hash(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _relevant_files(root: str) -> set:
    """Tracked + untracked-but-not-gitignored relative paths. Using git to
    enumerate (instead of os.walk) keeps node_modules/venv/.git out for free
    and matches exactly what a `git add -A` would pick up.

    `.laintas/` is always excluded even if the project's own .gitignore
    hasn't been set up to cover it yet (it's documented convention, not
    enforced) — it holds laintas_cli's own runtime state, including nested
    worktrees created for grandchild sub-agents, which must never be picked
    up as "content" to seed into or merge back from a worktree."""
    files = set()
    rc, out, _ = _git(root, "ls-files")
    if rc == 0:
        files.update(l for l in out.splitlines() if l.strip())
    rc, out, _ = _git(root, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        files.update(l for l in out.splitlines() if l.strip())
    return {f for f in files if f != ".laintas" and not f.startswith(".laintas/")}


def _changed_vs_head(root: str) -> List[str]:
    """Relative paths of every file that differs from HEAD or is untracked
    (what `git add -A` would stage right now). Excludes `.laintas/` for the
    same reason as _relevant_files — it's laintas_cli's own runtime state
    (including nested worktrees), never project content to replicate."""
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
        if rel == ".laintas" or rel.startswith(".laintas/"):
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

    return {"applied": applied, "conflicts": conflicts}


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
    return ok
