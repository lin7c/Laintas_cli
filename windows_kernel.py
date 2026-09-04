"""windows_kernel.py — getting `helpwo-kernel.exe` onto the machine.

The `win.*` tools need a kernel running on the Windows side. Asking the user
to find a download page, run an installer and come back is a capability that
technically exists and practically does not, so this fetches and installs it.

What is automatic and what is not
---------------------------------
Downloading and installing is automatic **once asked for** — one command, no
page to find, no file to pick, and the whole thing including the hash check
and the silent install runs without further questions.

Starting it with control over the machine is **not** automatic, and must not
become so. The kernel's two tiers exist because reading every window and
driving every application cannot be bounded by the workspace folder; a CLI
that installed the kernel and quietly started it with `--allow-machine-write`
would have made that decision on the user's behalf, and the tiers would be
decoration. So: install on request, start on request, and the tier is always
a word the user typed.

Where the bytes come from
-------------------------
`helpwo.laintas.com/downloads/`, the same place Helpwo's own runtime page
links to, with `latest.json` naming the current build and `<file>.sha256`
carrying its digest. The kernel repository is private, so its GitHub release
assets are not the public channel — this is.

The digest is checked before anything is executed. A truncated download and a
substituted one look identical to a program that only checks the HTTP status.

Two things a first real Windows run taught this file, both of which look like
the feature being broken rather than the environment being itself:

  * **Send a real User-Agent.** `urllib`'s default is `Python-urllib/3.x`,
    which the CDN in front of the download host answers with 403 — the same
    request from a browser succeeds, so it reads as "the file is missing".
  * **Console output is not UTF-8.** Windows programs write in the machine's
    OEM code page; `tasklist` reporting no match on a Chinese install starts
    with byte 0xD0. Decoding goes through `winbridge.decode`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import winbridge

DOWNLOAD_ORIGIN = os.environ.get(
    "LAINTAS_KERNEL_DOWNLOAD_ORIGIN",
    "https://helpwo.laintas.com/downloads").rstrip("/")

#: Where the kernel's own installer puts it. Per-user, no administrator.
INSTALL_DIRNAME = "HelpwoKernel"
KERNEL_EXE = "helpwo-kernel.exe"

#: Sanity bound on the installer. The real one is around 63 MB; this exists
#: so a redirected or replaced URL cannot stream indefinitely into the
#: user's temp directory.
MAX_INSTALLER_BYTES = 200 * 1024 * 1024

#: Sent on every request to the download host. `urllib`'s default UA is a
#: known crawler signature and the CDN in front of that host rejects it with
#: 403 — a failure that reads as a missing file and cost a real install run.
USER_AGENT_TEMPLATE = "laintas-cli/{version} (+https://cli.laintas.com)"

DOWNLOAD_TIMEOUT = 60
#: NSIS silent installs are quick, but a machine with aggressive endpoint
#: protection can spend a while on a 63 MB executable before letting it run.
INSTALL_TIMEOUT = 600

_SAFE_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.exe$")


def _request(url: str) -> urllib.request.Request:
    try:
        from version import __version__ as cli_version
    except Exception:
        cli_version = "0.0.0"
    return urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT_TEMPLATE.format(
            version=cli_version)})


class KernelInstallError(RuntimeError):
    """Anything that stopped the install, phrased for the user."""


@dataclass
class Release:
    version: str
    asset: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{DOWNLOAD_ORIGIN}/{self.asset}"


# -- what is on the machine ---------------------------------------------

def install_dir() -> Optional[Path]:
    base = winbridge.localappdata()
    return (base / INSTALL_DIRNAME) if base else None


def kernel_exe() -> Optional[Path]:
    """The installed kernel, or None."""
    folder = install_dir()
    if folder is None:
        return None
    candidate = folder / KERNEL_EXE
    return candidate if candidate.is_file() else None


def installed_version() -> Optional[str]:
    """Ask the installed kernel what it is. None when it will not say."""
    exe = kernel_exe()
    if exe is None:
        return None
    try:
        done = subprocess.run([str(exe), "--version"], capture_output=True,
                              timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError):
        return None
    text = winbridge.decode(done.stdout or done.stderr or b"").strip()
    match = re.search(r"\d+\.\d+\.\d+", text)
    return match.group(0) if match else None


# -- what is published ---------------------------------------------------

def latest() -> Release:
    """Read the published pointer. Raises with a usable message."""
    url = f"{DOWNLOAD_ORIGIN}/latest.json"
    try:
        with urllib.request.urlopen(_request(url),
                                    timeout=DOWNLOAD_TIMEOUT) as response:
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise KernelInstallError(
            f"could not reach {url}: {exc}") from exc
    asset = str(payload.get("asset") or "")
    digest = str(payload.get("sha256") or "").lower()
    version = str(payload.get("version") or "")
    # The asset name becomes part of a URL and a filename on disk. It comes
    # from the network, so it is validated rather than trusted — a name with
    # a path separator in it would write outside the download directory.
    if not _SAFE_ASSET.match(asset):
        raise KernelInstallError(f"the published asset name is not usable: {asset!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise KernelInstallError("the published listing carries no usable checksum")
    return Release(version=version, asset=asset, sha256=digest)


# -- installing ----------------------------------------------------------

def _download(release: Release, into: Path,
              progress: Optional[Callable[[int, int], None]]) -> Path:
    target = into / release.asset
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(_request(release.url),
                                    timeout=DOWNLOAD_TIMEOUT) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total and total > MAX_INSTALLER_BYTES:
                raise KernelInstallError(
                    f"the installer is {total} bytes, which is larger than "
                    f"anything this should be downloading")
            with open(partial, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_INSTALLER_BYTES:
                        raise KernelInstallError(
                            "the download kept going past the size limit")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress:
                        progress(written, total)
    except (urllib.error.URLError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise KernelInstallError(f"the download failed: {exc}") from exc

    if digest.hexdigest() != release.sha256:
        # Deleted, not kept for inspection: a file that failed its checksum
        # is one nobody should be able to run by accident afterwards.
        partial.unlink(missing_ok=True)
        raise KernelInstallError(
            "the download does not match its published checksum; nothing was "
            "installed")
    partial.replace(target)
    return target


def _run_installer(installer: Path, windows_path: str):
    """Run the downloaded installer, whatever the mount will allow.

    Two ways, because `/mnt/c` is not an ordinary filesystem. Python creates
    a file without the execute bit, and a DrvFs mounted with `metadata` (the
    default this CLI's own distribution uses) keeps that faithfully — so
    exec'ing the freshly downloaded installer fails with EACCES even though
    Windows would happily run it. `chmod` fixes that case.

    It does not fix every case: a `/mnt` mounted `noexec`, which some
    hardened setups do, refuses regardless of the bits. There the answer is
    to stop asking Linux to execute it at all and hand the Windows path to
    `cmd.exe`, which is the process that was always going to run it.
    """
    try:
        installer.chmod(0o755)
    except OSError:
        # A mount without metadata reports 0777 and ignores this. Nothing to
        # report either way — the attempt below is the real test.
        pass
    try:
        return subprocess.run([str(installer), "/S"], capture_output=True,
                              timeout=INSTALL_TIMEOUT, cwd="/")
    except PermissionError:
        return subprocess.run(["cmd.exe", "/c", windows_path, "/S"],
                              capture_output=True, timeout=INSTALL_TIMEOUT,
                              cwd="/")


def install(progress: Optional[Callable[[int, int], None]] = None,
            force: bool = False) -> dict:
    """Download, verify and install the kernel. Returns what happened."""
    if not winbridge.in_wsl():
        raise KernelInstallError(
            "the Windows kernel is only useful on the Windows build of this "
            "CLI, which runs inside WSL")

    release = latest()
    have = installed_version()
    if have and have == release.version and not force:
        return {"action": "kept", "version": have,
                "path": str(kernel_exe() or "")}

    temp = winbridge.windows_temp()
    if temp is None:
        raise KernelInstallError(
            "could not find the Windows temp directory; is interop enabled "
            "in wsl.conf?")
    folder = temp / "laintas-kernel"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise KernelInstallError(f"could not write to {folder}: {exc}") from exc

    installer = _download(release, folder, progress)
    windows_installer = winbridge.to_windows_path(installer)
    if windows_installer is None:
        raise KernelInstallError(
            "the installer landed somewhere Windows cannot run it from")

    try:
        done = _run_installer(installer, windows_installer)
    except subprocess.TimeoutExpired as exc:
        raise KernelInstallError(
            "the installer did not finish in ten minutes; run it yourself "
            f"from {windows_installer}") from exc
    except OSError as exc:
        raise KernelInstallError(
            f"could not run the installer: {exc}. Run it yourself from "
            f"{windows_installer}") from exc

    exe = kernel_exe()
    if exe is None:
        detail = winbridge.decode(done.stderr or done.stdout or b"").strip()[:400]
        raise KernelInstallError(
            "the installer ran but the kernel is not where it should be"
            + (f": {detail}" if detail else "")
            + f". Try running {windows_installer} yourself.")

    installer.unlink(missing_ok=True)
    return {"action": "upgraded" if have else "installed",
            "version": installed_version() or release.version,
            "previous": have, "path": str(exe)}


# -- running -------------------------------------------------------------

def start(tier: str = "workspace", root: Optional[str] = None) -> dict:
    """Start the kernel in its own Windows console window.

    Its own window, not a background process, for the reason its README
    gives: the console *is* the connection, closing it is how a user revokes
    access in a hurry, and the first run signs in through a browser. A kernel
    hidden behind this CLI would be one the user cannot see or stop.
    """
    exe = kernel_exe()
    if exe is None:
        raise KernelInstallError(
            "the kernel is not installed; run /windows install first")

    flags: list[str] = []
    if tier == "read":
        flags.append("--allow-machine-read")
    elif tier == "write":
        flags.append("--allow-machine-write")
    elif tier != "workspace":
        raise KernelInstallError(
            f"unknown tier {tier!r}; expected workspace, read or write")
    if root:
        flags += ["--root", root]

    windows_exe = winbridge.to_windows_path(exe)
    if windows_exe is None:
        raise KernelInstallError("the kernel is not on a Windows drive")

    # `start` gives it a console of its own and returns immediately. The
    # empty string is the window title `start` otherwise steals the first
    # quoted argument for.
    argv = ["cmd.exe", "/c", "start", "", windows_exe, *flags]
    try:
        subprocess.Popen(argv, cwd="/", stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise KernelInstallError(f"could not start the kernel: {exc}") from exc
    return {"started": windows_exe, "tier": tier, "flags": flags}


def running() -> bool:
    """Whether a kernel process exists on the Windows side."""
    if not winbridge.in_wsl():
        return False
    try:
        done = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {KERNEL_EXE}", "/NH"],
            capture_output=True, timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError):
        return False
    # "no tasks match" is a localised sentence in the OEM code page, which is
    # why this decodes defensively and then only looks for an ASCII name.
    return KERNEL_EXE.lower() in winbridge.decode(done.stdout or b"").lower()


def stop() -> bool:
    """Ask the kernel to exit. Returns whether anything was running."""
    if not running():
        return False
    try:
        subprocess.run(["taskkill.exe", "/IM", KERNEL_EXE, "/F"],
                       capture_output=True, timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# -- status --------------------------------------------------------------

def status() -> dict:
    """Everything `/windows` needs to print, gathered once."""
    import windows_host

    host = windows_host.get_host()
    connected = bool(host and host.connected)
    tiers = host.tiers() if connected else {}
    return {
        "wsl": winbridge.in_wsl(),
        "installed": bool(kernel_exe()),
        "path": str(kernel_exe() or ""),
        "version": installed_version(),
        "processRunning": running(),
        "connected": connected,
        "tiers": tiers,
        "tools": __import__("windows_tools").registered_names(),
    }
