"""peer_coordination.py — cross-instance file-conflict coordination.

Multiple laintas_cli processes on the same machine, started in the same
working directory, are completely unaware of each other.  That allows two
silent failure modes:

  1. Stale read: instance B read file X earlier; instance A rewrites X;
     B's in-context snapshot of X is now wrong and B keeps editing based
     on it.
  2. Lost update: A and B both fs.write X; whichever lands last silently
     discards the other's change.

This module adds a lazy, file-system based coordination layer:

  L0 registry      — every instance registers
                     ~/.laintas/instances/<cwd_hash>/<id>.json and refreshes
                     its mtime as a heartbeat; peers discover each other by
                     scanning that directory.
  L1 fingerprints  — fs.read records a per-file etag; fs.write / fs.edit
                     verify the etag still matches before landing (CAS) and
                     refuse loudly otherwise.  fs.read reports when a file
                     changed since the instance last read it.
  L2 write log     — successful writes are appended to a per-day jsonl so a
                     change can be attributed to the instance that made it.

Everything is inert for a single instance: the only costs are one atomic
registration write at startup, one mtime refresh and one tiny listdir per
agent turn.  Fingerprint tracking, the write log and the conflict checks
activate only after a second live peer is detected (or an explicit
`peer_coordination: off` runtime config forces them off).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Optional

import paths

# Tuneables
_HEARTBEAT_STALE_SECS = 600      # a registration file this old is dead (10 min)
_INSTANCE_SCAN_INTERVAL = 5.0    # min seconds between registry rescans
_HEARTBEAT_INTERVAL = 15.0       # min seconds between own mtime refreshes
_FP_CACHE_LIMIT = 2000           # LRU cap on tracked read fingerprints
_WRITES_RETENTION_DAYS = 3
_WRITE_ENTRY_MAX_BYTES = 65536   # cap a single log line
_ETAG_CONTENT_CAP = 262144       # files ≤256KB get a content hash in their etag


def _cwd_hash(cwd: str) -> str:
    return hashlib.sha256(os.path.realpath(cwd).encode()).hexdigest()[:16]


def file_etag(abs_path: str) -> str:
    """File identity: stat fields (dev/ino/mode/size/mtime_ns) + content hash.

    Content is included (up to a size cap) so same-size writes that land
    within the filesystem's timestamp granularity are still detected — the
    test env showed mtime_ns identical for two consecutive same-size writes.
    Files larger than the cap rely on stat alone (documented limitation).
    Empty string when the path cannot be stat'ed (e.g. does not exist yet).
    """
    try:
        st = os.lstat(abs_path)
    except OSError:
        return ""
    digest = hashlib.sha256()
    digest.update(
        f"{st.st_dev}\0{st.st_ino}\0{st.st_mode}\0{st.st_size}\0"
        f"{st.st_mtime_ns}\0".encode("utf-8", errors="surrogateescape"))
    if stat.S_ISREG(st.st_mode) and st.st_size <= _ETAG_CONTENT_CAP:
        try:
            with open(abs_path, "rb") as f:
                digest.update(f.read())
        except OSError:
            pass
    return digest.hexdigest()


class _PeerCoordinator:
    def __init__(self) -> None:
        self._enabled = False
        self._instance_id = paths.PROCESS_INSTANCE_ID
        self._cwd_hash = ""
        self._reg_file: Optional[Path] = None
        self._read_fps: dict[str, str] = {}    # realpath -> etag
        self._fp_order: list[str] = []         # LRU order of realpaths
        self._last_scan = 0.0
        self._last_heartbeat = 0.0

    # ── lifecycle ────────────────────────────────────────────────────
    def register(self, cwd: str) -> None:
        """Register this instance (called once at process startup)."""
        self._cwd_hash = _cwd_hash(cwd)
        try:
            reg_dir = paths.INSTANCES_DIR / self._cwd_hash
            reg_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(reg_dir, 0o700)
            except OSError:
                pass
            payload = {
                "instance_id": self._instance_id,
                "pid": os.getpid(),
                "cwd": cwd,
                "started_at": time.time(),
            }
            tmp = reg_dir / f".{self._instance_id}.tmp"
            dest = reg_dir / f"{self._instance_id}.json"
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(str(tmp), str(dest))
            self._reg_file = dest
        except OSError:
            self._reg_file = None   # best-effort: coordination silently off

    def unregister(self) -> None:
        """Remove this instance's registration (process exit)."""
        if self._reg_file is not None:
            try:
                self._reg_file.unlink(missing_ok=True)
            except OSError:
                pass
            self._reg_file = None

    # ── activation / deactivation ────────────────────────────────────
    def maybe_update(self) -> bool:
        """Re-scan peer registrations; returns whether coordination is active.

        Called once per agent turn.  Also refreshes this instance's own
        heartbeat so a busy instance stays visible to peers.
        """
        if _explicitly_off():
            if self._enabled:
                self._enabled = False
                self._read_fps.clear()
                self._fp_order.clear()
            return False
        self._heartbeat()
        now = time.monotonic()
        if now - self._last_scan < _INSTANCE_SCAN_INTERVAL:
            return self._enabled
        self._last_scan = now
        want = len(self._active_peers()) >= 2
        if want != self._enabled:
            self._enabled = want
            if not want:
                self._read_fps.clear()
                self._fp_order.clear()
        return self._enabled

    def enabled(self) -> bool:
        """Cheap in-memory check used on hot paths (fs.read / fs.write)."""
        return self._enabled

    # ── registry scan ────────────────────────────────────────────────
    def _active_peers(self) -> list[str]:
        """Instance ids whose registration files are fresh (not stale)."""
        if not self._cwd_hash:
            return []
        reg_dir = paths.INSTANCES_DIR / self._cwd_hash
        try:
            entries = list(reg_dir.glob("*.json"))
        except OSError:
            return []
        now = time.time()
        active = []
        for p in entries:
            try:
                if now - p.stat().st_mtime > _HEARTBEAT_STALE_SECS:
                    continue
            except OSError:
                continue
            active.append(p.stem)
        return active

    def _heartbeat(self) -> None:
        if self._reg_file is None:
            return
        now = time.time()
        if now - self._last_heartbeat < _HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        try:
            os.utime(self._reg_file, None)
        except OSError:
            pass

    # ── L1: read fingerprints (CAS) ──────────────────────────────────
    def note_read(self, abs_path: str) -> dict:
        """Record the current etag of abs_path; report if it changed since
        the last read by this instance.

        Returns {"changed": bool, "prev": str} — changed is True when the
        file was previously read by this instance and its etag differs now.
        Zero overhead (no tracking) while coordination is inactive.
        """
        if not self._enabled:
            return {"changed": False, "prev": ""}
        real = os.path.realpath(abs_path)
        cur = file_etag(real)
        prev = self._read_fps.get(real, "")
        self._set_fp(real, cur)
        return {"changed": bool(prev) and prev != cur, "prev": prev}

    def note_write(self, abs_path: str) -> None:
        """Refresh the tracked fingerprint after this instance wrote the
        file, so its own subsequent CAS checks don't false-positive."""
        if not self._enabled:
            return
        real = os.path.realpath(abs_path)
        self._set_fp(real, file_etag(real))

    def assert_unchanged(self, abs_path: str) -> Optional[str]:
        """CAS check before a write.  Returns None if safe to write, or an
        error message when the file changed since this instance last read it.
        Never-read files and inactive coordination always pass."""
        if not self._enabled:
            return None
        real = os.path.realpath(abs_path)
        prev = self._read_fps.get(real, "")
        if not prev:
            return None          # never read it — nothing to protect
        if prev != file_etag(real):
            return ("file changed since this instance last read it "
                    "(another instance/process modified it) — re-read and retry")
        return None

    def _set_fp(self, real: str, etag: str) -> None:
        if real not in self._read_fps:
            self._fp_order.append(real)
            if len(self._fp_order) > _FP_CACHE_LIMIT:
                old = self._fp_order.pop(0)
                self._read_fps.pop(old, None)
        self._read_fps[real] = etag

    # ── L2: write log ────────────────────────────────────────────────
    def log_write(self, abs_path: str, op: str) -> None:
        """Append one write event to the per-day log (attribution only)."""
        if not self._enabled:
            return
        try:
            day = time.strftime("%Y%m%d")
            log_dir = paths.WRITES_DIR / self._cwd_hash
            log_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(log_dir, 0o700)
            except OSError:
                pass
            entry = {
                "t": time.time(),
                "instance_id": self._instance_id,
                "pid": os.getpid(),
                "path": abs_path,
                "etag": file_etag(abs_path),
                "op": op,
            }
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            if len(line) > _WRITE_ENTRY_MAX_BYTES:
                return
            with open(log_dir / f"{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _gc_writes(log_dir)
        except OSError:
            pass


_coord: Optional[_PeerCoordinator] = None

# Session leases this process currently owns: {(cwd_hash, session_id), ...}.
# Tracked so release_all_leases() can clean up on process exit.
_held_leases: set = set()


def get_coord() -> _PeerCoordinator:
    global _coord
    if _coord is None:
        _coord = _PeerCoordinator()
    return _coord


# ── explicit override (`peer_coordination: off` runtime config) ────────
_OFF_OVERRIDE: Optional[bool] = None


def _explicitly_off() -> bool:
    """True when the user forced coordination off via runtime config.

    Read lazily and cached so the hot paths never pay for an import.
    """
    global _OFF_OVERRIDE
    if _OFF_OVERRIDE is None:
        try:
            from agent_loop import get_runtime_config
            _OFF_OVERRIDE = (get_runtime_config("peer_coordination") == "off")
        except Exception:
            _OFF_OVERRIDE = False
    return _OFF_OVERRIDE


def _gc_writes(log_dir: Path) -> None:
    """Drop write-log days older than the retention window.

    Keyed on the YYYYMMDD filename prefix, not mtime: appending to today's
    file refreshes its mtime, which would make an mtime-based check keep
    stale files forever.
    """
    try:
        cutoff = time.strftime(
            "%Y%m%d",
            time.localtime(time.time() - _WRITES_RETENTION_DAYS * 86400))
        for p in log_dir.glob("*.jsonl"):
            name = p.stem
            if len(name) == 8 and name.isdigit() and name < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass


# ── Session Lease (single-owner for resumed sessions) ──────────────────
#
# A resumed session must not be held by two live instances at once — both
# would write the same <cwd_hash>_session_<id>.json and last-writer-wins
# would silently drop one side's progress.  We solve this at the source by
# giving each logical session a single-owner lease (a lock file recording
# the owning pid), checked *before* a /resume is allowed to take over.
#
# Stale-lock recovery uses pid liveness (os.kill(pid, 0)) rather than a
# timeout: an active instance may legitimately go long stretches without
# writing, so mtime would wrongly declare it dead.  This mirrors the
# pidlock / OpenClaw approach to stale session locks.

# A lease is only meaningful when another instance might contend for the
# session.  Single-instance runs still create the lease (cheap, one atomic
# write) so that a second instance starting later sees it; the check itself
# is what stays lazy.


def _pid_alive(pid: int) -> bool:
    """POSIX liveness probe: True if a process with this pid exists."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user
    except OSError:
        return False
    return True


def acquire_session_lease(cwd: str, session_id: str) -> dict:
    """Try to take ownership of a logical session for this instance.

    Returns {"ok": True, "owner": None} on success (lease created or this
    instance already owns it), or {"ok": False, "owner": {...}} when another
    *live* instance currently owns the session.  A stale lease (owner pid
    dead) is broken and taken over automatically.
    """
    try:
        sid = _normalize_session_id(session_id)
        if not sid:
            return {"ok": False, "owner": None,
                    "error": "empty session id"}
        lock_dir = paths.SESSION_LOCKS_DIR / _cwd_hash(cwd)
        lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(lock_dir, 0o700)
        except OSError:
            pass
        lock_path = lock_dir / f"{sid}.lock"
        owner = _read_lease(lock_path)
        if owner is not None and owner.get("instance_id") == _coord_instance_id():
            _held_leases.add((_cwd_hash(cwd), sid))
            return {"ok": True, "owner": None}   # already ours
        if owner is not None and _pid_alive(int(owner.get("pid") or 0)):
            return {"ok": False, "owner": owner}  # held by a live peer
        # No owner, or a stale owner (pid dead) → take over.
        payload = {
            "instance_id": _coord_instance_id(),
            "pid": os.getpid(),
            "acquired_at": time.time(),
        }
        tmp = lock_dir / f".{sid}.{uuid_hex()}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(str(tmp), str(lock_path))
        _held_leases.add((_cwd_hash(cwd), sid))
        return {"ok": True, "owner": None, "took_over": owner is not None}
    except OSError:
        return {"ok": True, "owner": None}   # best-effort: don't block resume


def release_session_lease(cwd: str, session_id: str) -> None:
    """Release ownership of a session (only if this instance owns it)."""
    try:
        sid = _normalize_session_id(session_id)
        if not sid:
            return
        lock_path = paths.SESSION_LOCKS_DIR / _cwd_hash(cwd) / f"{sid}.lock"
        owner = _read_lease(lock_path)
        if owner is not None and owner.get("instance_id") == _coord_instance_id():
            lock_path.unlink(missing_ok=True)
        _held_leases.discard((_cwd_hash(cwd), sid))
    except OSError:
        pass


def release_all_leases() -> None:
    """Release every session lease this instance holds (process exit)."""
    for cwd_hash, sid in list(_held_leases):
        try:
            lock_path = paths.SESSION_LOCKS_DIR / cwd_hash / f"{sid}.lock"
            owner = _read_lease(lock_path)
            if owner is not None and owner.get("instance_id") == _coord_instance_id():
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    _held_leases.clear()


def _read_lease(lock_path: Path) -> Optional[dict]:
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _coord_instance_id() -> str:
    return paths.PROCESS_INSTANCE_ID


def _normalize_session_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(value or ""))[:64]
    return safe or ""


def uuid_hex() -> str:
    return uuid.uuid4().hex[:8]
