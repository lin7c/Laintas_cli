"""child_registry.py — every process group this CLI starts, and how they end.

The kernel does not end a process's children when it dies. What usually looks
like "children die with the CLI" is the terminal: a hangup sends SIGHUP to the
foreground process group, and a PTY child gets one when its master closes. A
child started in its own session on pipes (``start_new_session=True`` —
shell.exec, local/P2P exec, hosted apps, the browser stack) has no terminal, so
nothing ever signals it. It only dies on SIGPIPE the next time it writes, which
a quiet server or anything writing to /dev/null never does.

Esc already kills such a group (tools._ProcessGroupOwner), but only while the
CLI is alive to act on it. Any exit that skips the cleanup cascade — SIGKILL,
OOM, the hard ``os._exit`` paths — used to leave them running for good. On the
dev box that was eleven browser stacks, ~1.4 GB RSS plus ~2 GB swap, two days
after their CLIs were gone.

So every spawner registers the group here, and the registry keeps two copies:

  * in memory, for ``kill_all()`` — the one teardown every exit path calls,
    including the hard ones, since it signals and never waits on a library;
  * a ledger file per CLI process under ``~/.laintas/run/``, for
    ``reap_dead_owners()`` — run at startup, it kills the groups of any CLI
    that died without running either.

Identity is (boot, pid, start time) throughout, never a bare pid, so a recycled
pid can never make a live stranger look like one of ours. The boot id matters
as much as the rest: /proc start times are ticks since *boot*, so without it a
ledger left behind by an OOM kill would, after a reboot, match whatever now
happens to sit at those numbers. A ledger from another boot is stale by
definition — it is deleted, never acted on.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from typing import Optional

from paths import LAINTAS_HOME

RUN_DIR = LAINTAS_HOME / "run"

_lock = threading.RLock()
_groups: dict[int, dict] = {}       # pgid -> {"start": ticks, "kind": str}


def _stat(pid: int) -> Optional[tuple[int, int, int]]:
    """(ppid, pgrp, start ticks) of a live pid, or None. Linux /proc only."""
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            data = fh.read()
        fields = data[data.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None                 # a zombie holds nothing but its slot
        return int(fields[1]), int(fields[2]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def start_ticks(pid: int) -> Optional[int]:
    stat = _stat(pid)
    return stat[2] if stat else None


def boot_id() -> int:
    """Seconds-since-epoch of the last boot, from /proc/stat's btime. 0 when it
    cannot be read — which makes every ledger look like another boot's, i.e.
    fails towards reaping nothing rather than towards killing strangers."""
    try:
        with open("/proc/stat", "r") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


_BOOT = boot_id()
_SELF = (_BOOT, os.getpid(), start_ticks(os.getpid()))


def _ledger_path():
    boot, pid, start = _SELF
    return RUN_DIR / f"{boot}-{pid}-{start}.json"


def _write_ledger() -> None:
    """Rewrite this process's ledger. Called with _lock held."""
    path = _ledger_path()
    try:
        if not _groups:
            path.unlink(missing_ok=True)
            return
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(RUN_DIR, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump({str(g): v for g, v in _groups.items()}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def register(pgid: int, kind: str) -> None:
    """Record a process group this CLI owns. ``pgid`` is the leader's pid for
    anything started with start_new_session / setsid / pty.fork."""
    own_group = getattr(os, "getpgrp", lambda: -1)()
    if not pgid or pgid <= 1 or pgid == own_group:
        return                          # never our own group, never init's
    start = start_ticks(pgid)
    if start is None:
        return                          # already gone
    with _lock:
        # Owners that end a group by signal alone (a PTY closed with SIGHUP)
        # never unregister it; drop entries whose group has fully exited so a
        # long session's ledger does not grow without bound.
        for old, info in list(_groups.items()):
            if (start_ticks(old) != info.get("start")
                    and not _members(old, int(info.get("start") or 0))):
                del _groups[old]
        _groups[pgid] = {"start": start, "kind": kind}
        _write_ledger()


def unregister(pgid: int) -> None:
    """The owner ended the group itself (or it is meant to outlive us)."""
    with _lock:
        if _groups.pop(pgid, None) is not None:
            _write_ledger()


def owned() -> dict[int, dict]:
    with _lock:
        return dict(_groups)


def _members(pgid: int, leader_start: int) -> list[int]:
    """Live processes still in group ``pgid`` that started no earlier than its
    leader — i.e. the leader and its descendants. The start filter is only a
    sanity bound (nothing predating the leader can descend from it); it does
    *not* tell our group from a later one that reuses the number, which is what
    ``_group_identity`` is for."""
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        stat = _stat(int(entry))
        if stat and stat[1] == pgid and stat[2] >= leader_start:
            found.append(int(entry))
    return found


def _group_identity(pgid: int, leader_start: int) -> Optional[str]:
    """Whether group ``pgid`` is still the one we created. ``"leader"`` when
    its leader is alive and its start time matches, ``"orphans"`` when the
    leader is gone but the group can only be ours, and None when we cannot
    tell — in which case nothing is signalled.

    The kernel does not reuse a process group id while the group is non-empty,
    so a live member is proof of the same group as long as no *other* process
    leads it. The one way to lose the identity is the group emptying and its
    number being taken by a new leader, which is exactly the case ruled out
    below.
    """
    leader = _stat(pgid)
    if leader and leader[2] == leader_start:
        return "leader"
    if leader and leader[1] == pgid:
        return None                     # a different process leads it now
    # No such pid, or that pid does not lead this group: nothing can have
    # created group `pgid` since ours, so surviving members are ours.
    return "orphans"


def _end_group(pgid: int, leader_start: int, grace: float) -> bool:
    """SIGTERM the group, SIGKILL whatever is left after ``grace``."""
    members = _members(pgid, leader_start)
    if not members:
        return False
    identity = _group_identity(pgid, leader_start)
    if identity is None:
        return False                    # not provably ours — leave it alone

    def _signal(sig):
        if identity == "leader":
            try:
                os.killpg(pgid, sig)
                return
            except OSError:
                pass
        # Leader gone: killpg still reaches the orphans, but signal them one by
        # one so a leader that exits mid-teardown cannot widen the blast.
        for pid in _members(pgid, leader_start):
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    _signal(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and _members(pgid, leader_start):
        time.sleep(0.05)
    if _members(pgid, leader_start):
        _signal(signal.SIGKILL)
    return True


def kill_all(grace: float = 1.0) -> int:
    """End every registered group now. For every exit path, hard ones
    included: it only signals processes and never waits on anything else."""
    with _lock:
        groups = dict(_groups)
        _groups.clear()
        _write_ledger()
    ended = 0
    for pgid, info in groups.items():
        try:
            ended += _end_group(pgid, int(info.get("start") or 0), grace)
        except Exception:
            pass
    return ended


def reap_dead_owners(grace: float = 2.0) -> int:
    """End the groups recorded by CLIs that are no longer running. Returns the
    number of groups ended. Best-effort, never raises."""
    ended = 0
    try:
        names = os.listdir(RUN_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = RUN_DIR / name
        try:
            boot_s, pid_s, start_s = name[:-5].split("-", 2)
            owner = (int(boot_s), int(pid_s), int(start_s))
        except ValueError:
            # Not a ledger we can identify (including the two-part names
            # written before boot ids): its pids and start times mean nothing
            # to us, so throw it away rather than signal on it.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if owner[0] != _BOOT:
            # Another boot's ledger. Start times are ticks since boot, so none
            # of its numbers can be checked against anything alive now, and
            # its processes are long gone with that boot anyway.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        # Our own pid and start time can be on disk from before an exec()
        # restart (/v update): that image's handles are gone, so its groups
        # are as orphaned as a dead CLI's.
        if owner != _SELF and start_ticks(owner[1]) == owner[2]:
            continue                    # owner alive
        try:
            with open(path, "r") as fh:
                groups = json.load(fh)
        except (OSError, ValueError):
            groups = {}
        pending = list(groups.items()) if isinstance(groups, dict) else []
        if owner == _SELF:
            # This is the file register() writes, so anything we have taken
            # ownership of since the read is in _groups — and live. Filtering
            # under the lock closes the window between reading the ledger and
            # acting on it.
            with _lock:
                pending = [(g, i) for g, i in pending
                           if _int_or_none(g) not in _groups]
        for pgid_s, info in pending:
            try:
                ended += _end_group(int(pgid_s), int(info.get("start") or 0), grace)
            except Exception:
                pass
        if owner == _SELF:
            with _lock:
                _write_ledger()         # never unlink our own live ledger
            continue
        try:
            path.unlink()
        except OSError:
            pass
    return ended


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
