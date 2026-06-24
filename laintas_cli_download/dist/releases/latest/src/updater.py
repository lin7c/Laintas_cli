"""Self-update for laintas-cli — powers the ``/v`` meta-command.

``/v``            show the local version and check the download site for a newer one
``/v update``     download and apply an available update
``/v update --force``  re-apply the latest even if versions already match

Update strategy
---------------
The download site publishes a ``manifest.json`` next to the release artifacts::

    {
      "version": "1.2.0",
      "released": "2026-06-24",
      "notes": "what changed",
      "files": {"laintas_cli.py": {"sha256": "...", "size": 301072}, ...}
    }

For **source installs** the updater diffs each file's sha256 against the local
copy and downloads **only the files that actually changed** (``{base}/src/<name>``),
verifies each hash, then swaps them in atomically with a ``.bak`` backup. This is
the "partial update" path — an unchanged install fetches only the ~few-KB
manifest, never the source.

For **frozen (PyInstaller) installs** the individual ``.py`` files don't exist on
disk, so a partial update is impossible; the updater falls back to replacing the
whole binary from the platform tarball/exe.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
from typing import Optional

import requests

try:
    import certifi
    _CA_BUNDLE = certifi.where()
except Exception:  # pragma: no cover - certifi always present per requirements
    _CA_BUNDLE = True

try:
    from version import __version__ as LOCAL_VERSION
except Exception:  # pragma: no cover
    LOCAL_VERSION = "0.0.0"

DEFAULT_DOWNLOAD_BASE = "https://cli.laintas.com"
_TIMEOUT = 30


# ── environment / install introspection ──────────────────────────────────

def download_base() -> str:
    """Root URL the updater fetches from (override for testing/staging)."""
    return os.environ.get("LAINTAS_DOWNLOAD_BASE", DEFAULT_DOWNLOAD_BASE).rstrip("/")


def manifest_url() -> str:
    # The live site serves immutable versioned dirs but also a rolling
    # "latest" staging copy; the updater always reads "latest" so a fresh
    # release is visible without bumping a URL in the client.
    base = os.environ.get("LAINTAS_UPDATE_CHANNEL", "latest")
    return f"{download_base()}/releases/{base}/manifest.json"


def file_url(channel_dir: str, filename: str) -> str:
    return f"{download_base()}/releases/{channel_dir}/src/{filename}"


def is_frozen() -> bool:
    """True when running as a PyInstaller one-file binary."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> str:
    """Directory that holds the installed .py modules (source installs)."""
    return os.path.dirname(os.path.abspath(__file__))


def _version_tuple(v: str):
    parts = []
    for chunk in str(v).strip().split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str) -> bool:
    return _version_tuple(remote) > _version_tuple(local)


# ── hashing / http ───────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_manifest() -> dict:
    """Fetch and parse the remote manifest. Raises on network/format error."""
    resp = requests.get(manifest_url(), timeout=_TIMEOUT, verify=_CA_BUNDLE)
    resp.raise_for_status()
    data = resp.json()
    if "version" not in data or "files" not in data:
        raise ValueError("manifest.json is missing required keys (version, files)")
    return data


def _download(url: str) -> bytes:
    resp = requests.get(url, timeout=_TIMEOUT, verify=_CA_BUNDLE)
    resp.raise_for_status()
    return resp.content


# ── update planning ──────────────────────────────────────────────────────

def _safe_name(name: str) -> bool:
    """Reject anything that could escape the install dir."""
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and ".." not in name
        and not name.startswith(".")
        and not os.path.isabs(name)
    )


def plan_changed_files(manifest: dict) -> list:
    """Return [(filename, remote_sha)] for files that differ from the local copy.

    A file is "changed" if it is missing locally or its sha256 mismatches.
    """
    base = install_dir()
    changed = []
    for name, meta in manifest.get("files", {}).items():
        if not _safe_name(name):
            continue
        remote_sha = (meta or {}).get("sha256", "")
        local_path = os.path.join(base, name)
        if not os.path.exists(local_path):
            changed.append((name, remote_sha))
            continue
        try:
            if _sha256_file(local_path) != remote_sha:
                changed.append((name, remote_sha))
        except OSError:
            changed.append((name, remote_sha))
    return changed


# ── apply ────────────────────────────────────────────────────────────────

def apply_source_update(manifest: dict, changed_files: list, channel_dir: str,
                        log) -> bool:
    """Download only the changed files, verify, and swap them in atomically.

    ``log`` is a callable(str) for progress output. Returns True on success.
    """
    base = install_dir()
    if not os.access(base, os.W_OK):
        log(f"[red]No write permission for {base}.[/red]")
        log("[yellow]Re-run with sufficient privileges (e.g. sudo) to update in place.[/yellow]")
        return False

    staged = {}  # filename -> temp path with verified new content
    tmpdir = tempfile.mkdtemp(prefix="laintas-update-")
    try:
        # 1) download + verify everything BEFORE touching the install dir
        for name, remote_sha in changed_files:
            log(f"  ↓ {name}")
            data = _download(file_url(channel_dir, name))
            got = _sha256_bytes(data)
            if remote_sha and got != remote_sha:
                log(f"[red]Checksum mismatch for {name} "
                    f"(expected {remote_sha[:12]}…, got {got[:12]}…). Aborting.[/red]")
                return False
            tmp_path = os.path.join(tmpdir, name)
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            staged[name] = tmp_path

        # 2) back up originals, then move new files in
        applied = []
        try:
            for name, tmp_path in staged.items():
                dest = os.path.join(base, name)
                if os.path.exists(dest):
                    shutil.copy2(dest, dest + ".bak")
                shutil.move(tmp_path, dest)
                applied.append(name)
        except OSError as e:
            log(f"[red]Write failed ({e}); rolling back…[/red]")
            for name in applied:
                bak = os.path.join(base, name + ".bak")
                if os.path.exists(bak):
                    shutil.move(bak, os.path.join(base, name))
            return False

        # 3) clean up backups on success
        for name in applied:
            bak = os.path.join(base, name + ".bak")
            if os.path.exists(bak):
                try:
                    os.remove(bak)
                except OSError:
                    pass
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def apply_frozen_update(manifest: dict, channel_dir: str, log) -> Optional[str]:
    """Replace the running PyInstaller binary. Returns path to new binary, or None.

    On POSIX the running executable can be unlinked and rewritten in place. On
    Windows a running .exe is locked, so we download alongside it and leave the
    user to swap it (handled by the caller's messaging).
    """
    target = os.path.abspath(sys.executable)
    if os.name == "nt":
        asset = "laintas_cli.exe"
        url = f"{download_base()}/releases/{channel_dir}/{asset}"
    else:
        asset = "laintas-cli_linux.tar.gz"
        url = f"{download_base()}/releases/{channel_dir}/{asset}"

    log(f"  ↓ {asset}")
    data = _download(url)

    tmpdir = tempfile.mkdtemp(prefix="laintas-update-")
    try:
        if os.name == "nt":
            new_exe = os.path.join(tmpdir, "laintas_cli.exe")
            with open(new_exe, "wb") as fh:
                fh.write(data)
            # Can't overwrite the running exe; drop it next to the current one.
            staged = target + ".new"
            shutil.copy2(new_exe, staged)
            log(f"[yellow]Windows can't replace a running .exe. "
                f"New binary saved as:[/yellow] {staged}")
            log("[yellow]Close laintas-cli, then replace the old .exe with it.[/yellow]")
            return None
        else:
            archive = os.path.join(tmpdir, asset)
            with open(archive, "wb") as fh:
                fh.write(data)
            import tarfile
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(tmpdir)
            extracted = os.path.join(tmpdir, "laintas-cli", "laintas-cli")
            if not os.path.exists(extracted):
                log("[red]Unexpected tarball layout; aborting.[/red]")
                return None
            if not os.access(os.path.dirname(target), os.W_OK):
                log(f"[red]No write permission for {os.path.dirname(target)}.[/red]")
                log("[yellow]Re-run with sudo to replace the binary in place.[/yellow]")
                return None
            # Replace in place: unlink the running file (the open fd keeps the
            # current process alive), then move the new one into its path.
            try:
                os.remove(target)
            except OSError:
                pass
            shutil.move(extracted, target)
            os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            return target
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
