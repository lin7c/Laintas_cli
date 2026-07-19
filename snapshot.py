"""Git-based session snapshots / undo for laintas_cli.

Lets the agent (or user) revert the working-tree changes made during a session,
backed by git — non-destructively. A checkpoint is a *dangling* commit that
captures the full working tree (tracked + untracked, minus .gitignored) WITHOUT
touching the user's index or stash (it uses a throwaway ``GIT_INDEX_FILE``).
Undo restores the working tree to a checkpoint (it restores modified/deleted
files; it never deletes files created since the checkpoint, so undo is safe).

This is pure git + filesystem, so it lives entirely client-side (the gateway
never sees the repo). Checkpoints are recorded in ``~/.laintas/checkpoints.json``
keyed by repo working directory.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import List, Optional

import json_store
import paths

_MAX_PER_REPO = 25


def _git(cwd: str, *args: str, env: Optional[dict] = None, timeout: int = 30):
    """Run a git command; return (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def repo_root(cwd: str) -> Optional[str]:
    """The git work-tree root containing ``cwd``, or None if not a repo."""
    rc, out, _ = _git(cwd, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else None


# ── checkpoint store (~/.laintas/checkpoints.json) ─────────────────────────
def _store_path():
    return paths.LAINTAS_HOME / "checkpoints.json"


def _load_store() -> dict:
    return json_store.load_json(_store_path(), default=dict)


def _save_store(store: dict) -> None:
    # Atomic (temp file + fsync + rename) via json_store — this used to be a
    # direct write_text(), which could truncate/corrupt checkpoints.json if
    # the process died mid-write. That would be particularly bad here since
    # this file backs the "undo my agent's damage" safety net.
    try:
        paths.ensure_home()
        json_store.save_json_atomic(_store_path(), store)
    except OSError:
        pass


def _record(root: str, sha: str, label: str) -> None:
    store = _load_store()
    entries = store.setdefault(root, [])
    entries.append({"sha": sha, "label": label or "", "ts": time.time()})
    if len(entries) > _MAX_PER_REPO:
        del entries[:len(entries) - _MAX_PER_REPO]
    _save_store(store)


def list_for(cwd: str) -> List[dict]:
    """Checkpoints recorded for the repo containing ``cwd`` (newest last)."""
    root = repo_root(cwd)
    if not root:
        return []
    return _load_store().get(root, [])


def latest(cwd: str) -> Optional[dict]:
    entries = list_for(cwd)
    return entries[-1] if entries else None


# ── create / restore ───────────────────────────────────────────────────────
def create(cwd: str, label: str = "") -> Optional[dict]:
    """Capture the current working tree as a dangling commit. Returns the
    recorded checkpoint ``{sha, label, ts}`` or None (not a repo / git failure).
    Non-destructive: uses a throwaway index, doesn't touch the real index/stash."""
    root = repo_root(cwd)
    if not root:
        return None
    # A stray ~/.git is surprisingly common on development machines. Treating
    # the entire home directory as one repository makes `git add -A` traverse
    # caches, credentials, package stores and every nested checkout before the
    # first model request. Besides the severe REPL delay, that is far broader
    # than an automatic undo checkpoint should ever be. Explicit project repos
    # below the home directory continue to work normally.
    try:
        if os.path.realpath(root) == os.path.realpath(os.path.expanduser("~")):
            return None
    except OSError:
        pass
    # A throwaway index path that does NOT yet exist — git creates a fresh, valid
    # index there (an empty pre-created file is rejected as a corrupt index).
    fd, idx_path = tempfile.mkstemp(prefix="laintas_snap_", suffix=".idx")
    os.close(fd)
    os.unlink(idx_path)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": idx_path}
        # Stage the entire working tree into the throwaway index (respects
        # .gitignore — node_modules etc. are skipped).
        rc, _, _ = _git(root, "add", "-A", env=env)
        if rc != 0:
            return None
        rc, tree, _ = _git(root, "write-tree", env=env)
        if rc != 0 or not tree:
            return None
        _, head, _ = _git(root, "rev-parse", "HEAD")
        msg = f"laintas snapshot: {label}" if label else "laintas snapshot"
        args = ["commit-tree", tree, "-m", msg]
        if head:
            args = ["commit-tree", tree, "-p", head, "-m", msg]
        rc, sha, _ = _git(root, *args, env=env)
        if rc != 0 or not sha:
            return None
        _record(root, sha, label)
        return {"sha": sha, "label": label or "", "ts": time.time()}
    finally:
        try:
            os.unlink(idx_path)
        except OSError:
            pass


def restore(cwd: str, sha: Optional[str] = None) -> tuple:
    """Restore the working tree to a checkpoint (the latest if ``sha`` omitted).

    Returns ``(ok, message)``. Restores modified/deleted files to the snapshot;
    does NOT delete files created since the snapshot (so undo can't lose new
    work). A safety checkpoint of the CURRENT state is taken first so the undo
    itself is reversible.
    """
    root = repo_root(cwd)
    if not root:
        return False, "not a git repository"
    if sha is None:
        last = latest(cwd)
        if not last:
            return False, "no checkpoints for this repository"
        sha = last["sha"]
    # verify the snapshot commit still exists (not GC'd)
    rc, _, _ = _git(root, "cat-file", "-e", f"{sha}^{{commit}}")
    if rc != 0:
        return False, f"checkpoint {sha[:10]} no longer exists (git gc?)"
    # safety net: snapshot current state before overwriting it
    create(cwd, "pre-undo auto")
    rc, _, err = _git(root, "restore", "--source", sha, "--worktree", "--", ".")
    if rc != 0:
        # fall back for older git
        rc, _, err = _git(root, "checkout", sha, "--", ".")
        if rc != 0:
            return False, f"restore failed: {err}"
    return True, f"restored working tree to checkpoint {sha[:10]}"
