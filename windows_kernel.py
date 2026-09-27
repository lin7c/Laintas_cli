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
import shutil
import subprocess
import tempfile
import time
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
    # The official installer lets the user move the application. Its registry
    # entry is authoritative; assuming LOCALAPPDATA hides those installations
    # from update/uninstall and can leave a second copy behind.
    if winbridge.in_wsl():
        registered = winbridge.registered_kernel_dir()
        if registered is not None:
            return registered
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
    if done.returncode != 0:
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
    if not isinstance(payload, dict):
        raise KernelInstallError("the published listing must be an object")
    asset = str(payload.get("asset") or "")
    digest = str(payload.get("sha256") or "").lower()
    version = str(payload.get("version") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise KernelInstallError("the published listing carries no usable version")
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
    except BaseException as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, (urllib.error.URLError, OSError)):
            raise KernelInstallError(f"the download failed: {exc}") from exc
        raise

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
    """Install without granting access or starting a new kernel session."""
    return _install(progress, force, resume=False)


def update(progress: Optional[Callable[[int, int], None]] = None,
           force: bool = False) -> dict:
    """Update and restore the connected session's existing access tier."""
    return _install(progress, force, resume=True)


def _connected_tier() -> Optional[str]:
    import windows_host
    host = windows_host.get_host()
    if not host or not host.connected:
        return None
    tiers = host.tiers()
    return ("write" if tiers.get("machineWrite") else
            "read" if tiers.get("machineRead") else "workspace")


def _install(progress, force: bool, *, resume: bool) -> dict:
    if not winbridge.in_wsl():
        raise KernelInstallError(
            "the Windows kernel is only useful on the Windows build of this "
            "CLI, which runs inside WSL")

    release = latest()
    have = installed_version()
    if have and tuple(map(int, have.split("."))) >= tuple(map(int, release.version.split("."))) and not force:
        return {"action": "kept", "version": have,
                "path": str(kernel_exe() or "")}

    temp = winbridge.windows_temp()
    if temp is None:
        raise KernelInstallError(
            "could not find the Windows temp directory; is interop enabled "
            "in wsl.conf?")
    try:
        folder = Path(tempfile.mkdtemp(prefix="laintas-kernel-", dir=temp))
    except OSError as exc:
        raise KernelInstallError(f"could not write to {temp}: {exc}") from exc

    try:
        installer = _download(release, folder, progress)
        windows_installer = winbridge.to_windows_path(installer)
        if windows_installer is None:
            raise KernelInstallError(
                "the installer landed somewhere Windows cannot run it from")
        # Do not interrupt a working kernel until the replacement is verified.
        was_running = running()
        tier = _connected_tier() if resume and was_running else None
        if was_running:
            stop()
        try:
            try:
                done = _run_installer(installer, windows_installer)
            except subprocess.TimeoutExpired as exc:
                raise KernelInstallError(
                    "the installer timed out; check Windows, then retry /windows update") from exc
            except OSError as exc:
                raise KernelInstallError(
                    f"could not run the installer at {windows_installer}: {exc}") from exc
            finally:
                # The installer may have put it somewhere else (custom path).
                _forget_install_dir()
            if done.returncode != 0:
                detail = winbridge.decode(done.stderr or done.stdout or b"").strip()[:400]
                raise KernelInstallError(
                    f"the installer failed (exit {done.returncode})"
                    + (f": {detail}" if detail else "")
                    + "; retry /windows update after resolving the error")
            exe = kernel_exe()
            actual = installed_version()
            if exe is None or actual != release.version:
                raise KernelInstallError(
                    f"installation could not be verified: expected v{release.version}, "
                    f"found {actual or 'no readable version'}. Retry /windows update --force")
        except KernelInstallError as exc:
            if was_running:
                # It was stopped for the update and is still stopped; saying
                # only "the installer failed" leaves the user wondering why
                # their Windows tools vanished.
                raise KernelInstallError(
                    f"{exc} (the kernel was stopped for the update; "
                    "/windows start brings it back)") from exc
            raise
        result = {"action": "upgraded" if have else "installed",
                  "version": actual, "previous": have, "path": str(exe),
                  "restartNeeded": was_running}
        if tier is not None:
            try:
                start(tier)
                result.update(restarted=True, tier=tier, restartNeeded=False)
            except KernelInstallError as exc:
                result["warning"] = f"Updated, but restart failed: {exc}"
        return result
    finally:
        # Best effort: an installer Windows still holds open must not turn a
        # finished install into a reported failure, or hide the real error.
        shutil.rmtree(folder, ignore_errors=True)


def _forget_install_dir() -> None:
    winbridge.forget("kernel_dir")


def uninstall() -> dict:
    """Stop and run the official uninstaller; user workspaces stay untouched."""
    if not winbridge.in_wsl():
        raise KernelInstallError("Kernel uninstall requires Windows / WSL")
    folder = install_dir()
    uninstaller = folder / "uninstall.exe" if folder else None
    if uninstaller is None or not uninstaller.is_file():
        if kernel_exe() is None:
            return {"action": "absent"}
        raise KernelInstallError(
            "the uninstaller is missing; repair with /windows install --force, "
            "then retry /windows uninstall")
    windows_path = winbridge.to_windows_path(uninstaller)
    if windows_path is None:
        raise KernelInstallError("the uninstaller is not on a Windows drive")
    stop()
    try:
        done = _run_installer(uninstaller, windows_path)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KernelInstallError(f"could not uninstall the kernel: {exc}") from exc
    if done.returncode != 0:
        raise KernelInstallError(f"the uninstaller failed (exit {done.returncode})")
    # NSIS may copy itself to TEMP and let the original process exit first.
    # Wait for that worker's effects instead of announcing premature success.
    deadline = time.monotonic() + 60
    while uninstaller.exists() or (folder / KERNEL_EXE).exists():
        if time.monotonic() >= deadline:
            raise KernelInstallError(
                "uninstall has not finished; check its Windows window, "
                "then run /windows status")
        time.sleep(0.2)
    _forget_install_dir()
    import windows_host
    import windows_tools
    windows_host.stop_host()
    windows_tools.unregister()
    return {"action": "uninstalled"}


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
    # No captured pipes: `start` lets the kernel inherit cmd's handles, so a
    # captured stdout stays open for as long as the kernel runs and `run`
    # would wait out its timeout — then report a launch that succeeded as a
    # failure. The exit status of `start` itself is all this needs.
    try:
        done = subprocess.run(argv, cwd="/", stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KernelInstallError(f"could not start the kernel: {exc}") from exc
    if done.returncode != 0:
        raise KernelInstallError(
            f"Windows could not launch the kernel (exit {done.returncode})")
    return {"started": windows_exe, "tier": tier, "flags": flags}


def running() -> bool:
    """Whether a kernel process exists on the Windows side."""
    if not winbridge.in_wsl():
        return False
    try:
        done = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {KERNEL_EXE}", "/NH"],
            capture_output=True, timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError) as exc:
        raise KernelInstallError(f"could not check kernel processes: {exc}") from exc
    if getattr(done, "returncode", 0) != 0:
        raise KernelInstallError("Windows could not list kernel processes; check WSL interop")
    # "no tasks match" is a localised sentence in the OEM code page, which is
    # why this decodes defensively and then only looks for an ASCII name.
    return KERNEL_EXE.lower() in winbridge.decode(done.stdout or b"").lower()


def stop() -> bool:
    """Ask the kernel to exit. Returns whether anything was running."""
    if not running():
        return False
    try:
        done = subprocess.run(["taskkill.exe", "/IM", KERNEL_EXE, "/F"],
                              capture_output=True, timeout=30, cwd="/")
    except (OSError, subprocess.SubprocessError) as exc:
        raise KernelInstallError(f"could not stop the kernel: {exc}") from exc
    if running():
        detail = winbridge.decode(done.stderr or done.stdout or b"").strip()[:400]
        raise KernelInstallError("the kernel is still running"
                                 + (f": {detail}" if detail else ""))
    return True


def ensure_started(tier: str = "workspace", *, restart: bool = False,
                   progress=None) -> dict:
    """An explicit start request also installs if missing; never stacks windows."""
    if tier not in ("workspace", "read", "write"):
        raise KernelInstallError("expected workspace, read or write")
    if not winbridge.in_wsl():
        raise KernelInstallError("Kernel start requires Windows / WSL")
    if kernel_exe() is None:
        install(progress=progress)
    import windows_host
    if windows_host.start_host() is None:
        raise KernelInstallError("could not open the local connection; retry /windows start")
    if running():
        if not restart and _connected_tier() == tier:
            return {"action": "running", "tier": tier}
        stop()
    return {"action": "started", **start(tier)}


# -- status --------------------------------------------------------------

def status() -> dict:
    """Everything `/windows` needs to print, gathered once."""
    import windows_host

    host = windows_host.get_host()
    connected = bool(host and host.connected)
    tiers = host.tiers() if connected else {}
    exe = kernel_exe()
    return {
        "wsl": winbridge.in_wsl(),
        "installed": bool(exe),
        "path": str(exe or ""),
        "version": installed_version(),
        "processRunning": running(),
        "connected": connected,
        "tiers": tiers,
        "tools": __import__("windows_tools").registered_names(),
    }
