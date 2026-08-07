"""Shared release download, verification and installation primitives.

Used by the personal CLI updater (``/v update``), the Enterprise CLI
installer (``/v enterprise cli``), and the gateway bundle installer
(``/v enterprise gateway``).

This module is PRIVATE — it ships inside the laintas-cli source tree and
is never published separately. Enterprise distribution paths through this
module are authenticated; the raw source is not exposed to end users.
"""
from __future__ import annotations

import base64
import hashlib
import os
import platform
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

# ── hashing ────────────────────────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    """SHA-256 of a byte buffer, hex-encoded."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """SHA-256 of a file on disk, hex-encoded."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksums(data: bytes) -> dict[str, str]:
    """Parse a release SHA256SUMS file into ``{asset: lowercase digest}``."""
    checksums: dict[str, str] = {}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SHA256SUMS.txt is not valid UTF-8") from exc
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError("SHA256SUMS.txt contains a malformed line")
        digest, name = parts
        name = name.lstrip("* ")
        digest = digest.lower()
        if (len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or not name):
            raise ValueError("SHA256SUMS.txt contains an invalid entry")
        checksums[name] = digest
    return checksums


# ── verification ───────────────────────────────────────────────────────────

def verify_sha256(data: bytes, expected: str, label: str = "payload") -> None:
    """Raise :class:`RuntimeError` if the SHA-256 does not match."""
    actual = sha256_bytes(data)
    expected = expected.lower()
    if len(expected) != 64 or actual != expected:
        raise RuntimeError(f"{label} checksum verification failed.")


def verify_ed25519(data: bytes, signature_b64: str, public_key_der: bytes,
                   label: str = "payload") -> None:
    """Raise :class:`RuntimeError` if the Ed25519 signature does not verify.

    ``public_key_der`` is a raw DER-encoded Ed25519 public key.
    """
    try:
        from cryptography.hazmat.primitives.serialization import \
            load_der_public_key
    except ImportError as exc:
        raise RuntimeError(
            "cryptography is required for enterprise release verification"
        ) from exc
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        load_der_public_key(public_key_der).verify(
            signature, bytes.fromhex(sha256_bytes(data)))
    except Exception as exc:
        raise RuntimeError(
            f"{label} signature verification failed.") from exc


# ── platform detection ─────────────────────────────────────────────────────

def platform_id() -> str:
    """Return a release asset platform tag like ``linux-amd64``."""
    system = {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(
        platform.system().lower())
    machine = platform.machine().lower()
    arch = (
        "amd64" if machine in ("x86_64", "amd64")
        else "arm64" if machine in ("aarch64", "arm64")
        else None
    )
    if not system or not arch:
        raise RuntimeError(
            f"Not available for {platform.system()} {platform.machine()}")
    return f"{system}-{arch}"


# ── download (progress bar) ────────────────────────────────────────────────

def _open_tty():
    """Open /dev/tty for direct writes, bypassing prompt_toolkit."""
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return None, 0
    width = 80
    try:
        import struct
        import fcntl
        import termios
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        _rows, cols = struct.unpack("hhhh", packed)[:2]
        if cols > 0:
            width = cols
    except Exception:
        pass
    return fd, width


def _write_tty(fd: int, text: str) -> None:
    try:
        os.write(fd, text.encode("utf-8", errors="replace"))
    except OSError:
        pass


def _format_download_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (f"{size:.0f} {unit}" if unit == "B"
                    else f"{size:.1f} {unit}")
        size /= 1024
    return f"{size:.1f} GB"


def _live_hostile_stdout(console_file) -> bool:
    """True when Rich animations can't render on the active output."""
    for stream in (console_file, None):
        if stream is None:
            continue
        name = type(stream).__name__
        module = type(stream).__module__ or ""
        if (name == "StdoutProxy"
                or module.startswith("prompt_toolkit.")
                or module == "repl_mirror"):
            return True
    return False


def download(url: str, *,
             label: str = "downloading",
             session: Optional[requests.Session] = None,
             timeout: int = 30,
             console=None) -> bytes:
    """Download *url* into memory, showing a progress bar.

    When *session* is provided its headers/cookies/verify settings are
    used; otherwise a plain ``requests.get`` call is made.
    """
    s = session or requests
    resp = s.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()
    _len = resp.headers.get("Content-Length")
    total = int(_len) if _len and _len.isdigit() else None

    try:
        from rich.console import Console as _Console
        from rich.progress import (BarColumn, DownloadColumn, Progress,
                                   TextColumn, TimeRemainingColumn,
                                   TransferSpeedColumn)
    except Exception:
        return resp.content

    con = console if console is not None else _Console()
    if not getattr(con, "is_terminal", False):
        try:
            con.print(f"  ↓ {label}")
        except Exception:
            pass
        return resp.content

    # prompt_toolkit ➜ direct /dev/tty output
    if _live_hostile_stdout(getattr(con, "file", None)):
        buf = bytearray()
        started = time.monotonic()
        tty_fd, tty_width = _open_tty()
        if tty_fd is not None:
            def _render(final: bool = False) -> None:
                dl = len(buf)
                now = time.monotonic()
                elapsed = max(now - started, 0.001)
                speed = _format_download_size(int(dl / elapsed)) + "/s"
                if total:
                    pct = min(100, int(dl * 100 / total))
                    if final and dl >= (total or 0):
                        pct = 100
                    detail = (f"{pct:3d}% · "
                              f"{_format_download_size(dl)}/"
                              f"{_format_download_size(total)}")
                else:
                    detail = _format_download_size(dl)
                mark = "✓" if final else "↓"
                line = f"\r  {mark} {label}  {detail}  {speed}"
                if tty_width > 0:
                    line = line[:tty_width]
                _write_tty(tty_fd, line + ("\n" if final else ""))
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        buf.extend(chunk)
                        _render()
                _render(final=True)
                return bytes(buf)
            finally:
                try:
                    os.close(tty_fd)
                except OSError:
                    pass
        # fallback — buffered
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    # Rich Progress bar
    buf = bytearray()
    with Progress(
        TextColumn("  [#3fb950]↓[/#3fb950] "
                   "[#6b7d6b]{task.description}[/#6b7d6b]"),
        BarColumn(bar_width=None, complete_style="#3fb950",
                  finished_style="#4ade80", pulse_style="#3fb950"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=con, transient=True,
        redirect_stdout=False, redirect_stderr=False,
    ) as progress:
        task = progress.add_task(label, total=total)
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                buf.extend(chunk)
                progress.update(task, advance=len(chunk))
        if total is None:
            progress.update(task, total=len(buf), completed=len(buf))
    return bytes(buf)


# ── atomic install ─────────────────────────────────────────────────────────

def atomic_write(data: bytes, target: Path, *, mode: int = 0o755) -> None:
    """Write *data* to *target* atomically (tempfile + ``os.replace``)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(
        prefix=".laintas-release-", dir=str(target.parent))
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        staged.chmod(mode)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def extract_archive(data: bytes, target_dir: Path) -> None:
    """Extract a tar.gz archive into *target_dir*, with path-safety checks."""
    import io
    import tarfile

    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if ".." in name or name.startswith("/"):
                raise RuntimeError(f"Unsafe archive path: {name}")
            target = target_dir / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tf.extractfile(member)
                if source is None:
                    raise RuntimeError(
                        f"Cannot extract {name}: not a regular file")
                with open(target, "wb") as dst:
                    dst.write(source.read())