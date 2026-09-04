"""winbridge.py — the small set of facts about the Windows side of WSL.

Three questions come up wherever this CLI touches the machine it is running
inside of: am I in WSL, where is the user's Windows profile, and how does a
path translate between the two namespaces. They were answered inline in
`windows_host.py` first; `windows_kernel.py` needed the same answers, and a
second copy of a path guess is how the two drift.

Everything here goes through Windows' own tools — `cmd.exe` for environment
variables, `wslpath` for translation — rather than assembling
`/mnt/c/Users/<name>`. The user may have moved their profile, the system
drive may not be C, and a guessed path that happens to exist is worse than
none: it silently writes to the wrong place.

Answers are cached for the life of the process. Each one costs an interop
process launch, which on a cold WSL boot is not fast, and none of them
changes while the CLI is running.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
_cache: dict[str, Optional[Path]] = {}

#: Interop launches are slow on a cold distribution but never long. A wait
#: this size distinguishes "slow" from "wsl.conf has interop disabled and
#: this will never return".
INTEROP_TIMEOUT = 15


def in_wsl() -> bool:
    """True when this process is inside a WSL distribution."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text()
    except OSError:
        return False
    return "microsoft" in release.lower()


def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=INTEROP_TIMEOUT,
                              # cmd.exe warns and falls back to
                              # C:\Windows when the working directory is a
                              # UNC path, which every /home path is from
                              # Windows' point of view.
                              cwd="/")
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip()


def to_wsl_path(windows_path: str) -> Optional[Path]:
    """`C:\\Users\\x` → `/mnt/c/Users/x`."""
    if not windows_path:
        return None
    out = _run(["wslpath", "-u", windows_path])
    return Path(out) if out else None


def to_windows_path(path: Path) -> Optional[str]:
    """`/mnt/c/Users/x` → `C:\\Users\\x`.

    Returns None for a path with no Windows equivalent — anything on the
    distribution's own ext4. Callers that are about to hand a path to a
    Windows program need that answer, not a `\\\\wsl$\\…` string that half of
    them mishandle.
    """
    out = _run(["wslpath", "-w", str(path)])
    if not out or out.startswith("\\\\"):
        return None
    return out


def _env(name: str) -> Optional[Path]:
    raw = _run(["cmd.exe", "/c", f"echo %{name}%"])
    if not raw or raw.startswith("%"):
        return None
    return to_wsl_path(raw)


def _cached(key: str, resolve) -> Optional[Path]:
    with _lock:
        if key in _cache:
            return _cache[key]
    value = resolve()
    with _lock:
        _cache[key] = value
    return value


def localappdata() -> Optional[Path]:
    """`%LOCALAPPDATA%`, as a path this side can read and write."""
    override = os.environ.get("LAINTAS_WINDOWS_LOCALAPPDATA")
    if override:
        return Path(override)
    if not in_wsl():
        return None
    return _cached("localappdata", lambda: _env("LOCALAPPDATA"))


def windows_temp() -> Optional[Path]:
    """`%TEMP%`. Downloads bound for a Windows program go here.

    Not `/tmp`: a file on the distribution's ext4 is reachable from Windows
    only through `\\\\wsl$`, and running an installer from there fails on
    machines where endpoint protection blocks execution over a network
    path — which is what that share looks like to it.
    """
    override = os.environ.get("LAINTAS_WINDOWS_TEMP")
    if override:
        return Path(override)
    if not in_wsl():
        return None
    return _cached("temp", lambda: _env("TEMP"))


def reset_cache() -> None:
    """Test hook. Nothing in production has a reason to call this."""
    with _lock:
        _cache.clear()
