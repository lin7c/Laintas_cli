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
from pathlib import Path
from typing import Optional

import requests

from download_progress import (
    format_size as _format_download_size,
    live_hostile_stdout as _live_hostile_stdout,
    open_tty as _open_tty,
    stream_download,
    write_tty as _write_tty,
)


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



def download(url: str, *, label: str = "downloading",
             session: Optional[requests.Session] = None,
             timeout: int = 30, console=None) -> bytes:
    """Download using the shared meter and the supplied session's credentials."""
    resp = (session or requests).get(url, timeout=timeout, stream=True)
    try:
        resp.raise_for_status()
    except BaseException:
        resp.close()
        raise
    return stream_download(resp, label=label, console=console,
                           tty_open=_open_tty, tty_write=_write_tty)


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