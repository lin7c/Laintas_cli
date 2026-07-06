"""Self-update for laintas-cli — powers the ``/v`` meta-command.

``/v``            show the local version and check GitHub Releases for a newer one
``/v update``     download and apply an available update
``/v update --force``  re-apply the latest even if versions already match

Update source
-------------
Releases are published to **GitHub Releases** (the all-CI pipeline), so the
updater reads from there. GitHub serves a stable rolling URL for the newest
non-prerelease release::

    https://github.com/<owner>/<repo>/releases/latest/download/<asset>

Each release carries a ``manifest.json`` asset::

    {
      "version": "1.3.0",
      "released": "2026-06-28",
      "files": {"laintas_cli.py": {"sha256": "...", "size": 301072},
                "agent_tools/catalog.json": {"sha256": "...", "size": 11848}, ...}
    }

For **source installs** the updater diffs each file's sha256 (manifest vs local,
subdir paths included) to decide whether anything changed; if so it downloads
the single ``src_manifest.zip`` asset once, extracts it, verifies each changed
file's hash, then swaps them in with a ``.bak`` backup + rollback. (GitHub has no
per-file download, so the whole source bundle is fetched as one zip — still only
when something actually changed.)

For **frozen (PyInstaller) installs** the individual ``.py`` files don't exist on
disk, so the updater replaces the whole binary from the platform tarball/exe
asset.
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

# GitHub Releases is the distribution channel (the CI release pipeline publishes
# here). Override LAINTAS_DOWNLOAD_BASE to point at a fork/mirror or a local
# static server for testing.
DEFAULT_DOWNLOAD_BASE = "https://github.com/lin7c/Laintas_cli/releases"
_TIMEOUT = 30


# ── environment / install introspection ──────────────────────────────────

def download_base() -> str:
    """Root URL the updater fetches from (override for testing/staging)."""
    return os.environ.get("LAINTAS_DOWNLOAD_BASE", DEFAULT_DOWNLOAD_BASE).rstrip("/")


def update_channel() -> str:
    """Which release to track. 'latest' (default) follows the newest published
    release; a tag like 'v1.3.0' (or '1.3.0') pins to that release."""
    return os.environ.get("LAINTAS_UPDATE_CHANNEL", "latest")


def _asset_url(channel: str, asset: str) -> str:
    """Build a GitHub Releases asset URL for the given channel.

    'latest' → the rolling ``/releases/latest/download/<asset>`` redirect;
    a version/tag → the immutable ``/releases/download/<tag>/<asset>``.
    """
    base = download_base()
    if channel in ("", "latest"):
        return f"{base}/latest/download/{asset}"
    tag = channel if channel.startswith("v") else f"v{channel}"
    return f"{base}/download/{tag}/{asset}"


def manifest_url() -> str:
    return _asset_url(update_channel(), "manifest.json")


def src_zip_url(channel: str) -> str:
    """URL of the full source bundle asset for a channel."""
    return _asset_url(channel, "src_manifest.zip")


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
    """Reject anything that could escape the install dir.

    Subdir paths ARE allowed (vendored packages like ``agent_tools/…`` ship in
    the manifest), but every component must be a plain name — no ``..``, no
    absolute paths, no backslashes, no dot-entries.
    """
    if not name or "\\" in name or os.path.isabs(name):
        return False
    for part in name.split("/"):
        if not part or part == "." or part == ".." or part.startswith("."):
            return False
    return True


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
    """Download the source bundle once, extract, verify, and swap in the changed
    files atomically (with subdir support + rollback).

    GitHub Releases has no per-file download, so the whole ``src_manifest.zip``
    is fetched in one request — but still only when ``changed_files`` is
    non-empty. ``log`` is a callable(str) for progress output. Returns True on
    success.
    """
    base = install_dir()
    if not os.access(base, os.W_OK):
        log(f"[red]No write permission for {base}.[/red]")
        log("[yellow]Re-run with sufficient privileges (e.g. sudo) to update in place.[/yellow]")
        return False

    tmpdir = tempfile.mkdtemp(prefix="laintas-update-")
    try:
        # 1) download + extract the source bundle
        log("  ↓ src_manifest.zip")
        try:
            data = _download(src_zip_url(channel_dir))
        except Exception as e:
            log(f"[red]Could not download source bundle: {e}[/red]")
            return False
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile:
            log("[red]Source bundle is not a valid zip. Aborting.[/red]")
            return False

        # 2) verify every changed file against the manifest BEFORE writing
        staged = []  # (name, extracted_path)
        for name, remote_sha in changed_files:
            src_path = os.path.join(tmpdir, name)
            if not os.path.exists(src_path):
                log(f"[red]{name} is missing from the source bundle. Aborting.[/red]")
                return False
            if remote_sha and _sha256_file(src_path) != remote_sha:
                log(f"[red]Checksum mismatch for {name}. Aborting.[/red]")
                return False
            staged.append((name, src_path))

        # 3) back up originals, then copy new files in (subdirs created as needed)
        applied = []  # (dest, had_original)
        try:
            for name, src_path in staged:
                dest = os.path.join(base, name)
                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                had = os.path.exists(dest)
                if had:
                    shutil.copy2(dest, dest + ".bak")
                shutil.copy2(src_path, dest)
                applied.append((dest, had))
        except OSError as e:
            log(f"[red]Write failed ({e}); rolling back…[/red]")
            for dest, had in applied:
                bak = dest + ".bak"
                if had and os.path.exists(bak):
                    shutil.move(bak, dest)
                elif not had and os.path.exists(dest):
                    try:
                        os.remove(dest)  # remove a freshly-created file
                    except OSError:
                        pass
            return False

        # 4) clean up backups on success
        for dest, had in applied:
            bak = dest + ".bak"
            if had and os.path.exists(bak):
                try:
                    os.remove(bak)
                except OSError:
                    pass
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def apply_frozen_update(manifest: dict, channel_dir: str, log) -> Optional[str]:
    """Replace the running PyInstaller binary. Returns path to new binary, or None.

    On POSIX the running executable can be unlinked and rewritten in place.
    """
    target = os.path.abspath(sys.executable)
    if sys.platform == "darwin":
        asset = "laintas-cli_macos.tar.gz"
    else:
        asset = "laintas-cli_linux.tar.gz"
    url = _asset_url(channel_dir, asset)

    log(f"  ↓ {asset}")
    data = _download(url)

    tmpdir = tempfile.mkdtemp(prefix="laintas-update-")
    try:
        archive = os.path.join(tmpdir, asset)
        with open(archive, "wb") as fh:
            fh.write(data)
        import tarfile
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(tmpdir)
        # GitHub tarball lays the binary out at the archive root (./laintas-cli
        # alongside ./install.sh); older layouts nested it one level deeper.
        extracted = os.path.join(tmpdir, "laintas-cli")
        if not os.path.exists(extracted):
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
