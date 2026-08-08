"""Laintas shared storage — the file channel between this CLI and Helpwo.

Helpwo mounts the same storage as a cloud folder ("Laintas Storage"), so a file
pushed from here appears in the Helpwo file tree, and anything Helpwo writes
there can be pulled back down. It is per-account, server-side and persistent —
unlike ``/connect``, which shares a live folder only while the CLI is running
and streams it peer-to-peer.

Everything goes through the agent_gateway's ``/api/storage/*`` endpoints using
the caller's existing Laintas session. Bytes themselves never pass through the
gateway: it hands back a presigned URL and the transfer runs straight to
object storage.

The module is deliberately display-free — it returns values or raises
:class:`SharedStorageError`, and the REPL command owns all the printing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

# The gateway that serves /api/storage for the official backend. A custom or
# self-hosted backend serves it from its own origin.
OFFICIAL_STORAGE_ORIGIN = "https://helpwo.laintas.com"

TIMEOUT = 30
TRANSFER_TIMEOUT = 600


class SharedStorageError(Exception):
    """Any failure worth showing the user verbatim."""


@dataclass(frozen=True)
class Entry:
    path: str
    name: str
    type: str          # 'file' | 'folder'
    size: int
    modified: str

    @property
    def is_dir(self) -> bool:
        return self.type == "folder"


@dataclass(frozen=True)
class Usage:
    tier: str
    used_bytes: int
    free_bytes: int
    max_bytes: int
    overage_bytes: int
    est_cost_cents: int
    max_file_bytes: int


def storage_origin(profile) -> str:
    """Where /api/storage lives for this backend profile."""
    if profile.sends_laintas_credentials:
        return OFFICIAL_STORAGE_ORIGIN
    return profile.origin


def clean_remote_path(raw: str) -> str:
    """Client-side mirror of the gateway's path rule, so an obviously bad path
    is refused before a round trip. The gateway re-validates regardless — this
    is convenience, not security."""
    path = (raw or "").replace("\\", "/").strip().strip("/")
    if not path:
        raise SharedStorageError("A remote path is required")
    segments = []
    for segment in path.split("/"):
        if not segment or segment in (".", ".."):
            raise SharedStorageError(f"Invalid remote path: {raw!r}")
        segments.append(segment)
    return "/".join(segments)


def _problem(resp: requests.Response) -> str:
    """Read the gateway's RFC 9457 problem body, falling back to the status."""
    try:
        body = resp.json()
        parts = [body.get("title"), body.get("detail")]
        text = " — ".join(p for p in parts if p)
        if text:
            return text
    except ValueError:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


class SharedStorage:
    """Authenticated client for one backend profile + session."""

    def __init__(self, profile, session: Optional[dict]):
        import backend_profiles
        self._headers, self._cookies = backend_profiles.request_auth(profile, session)
        self._base = storage_origin(profile)

    def _call(self, method: str, path: str, **kwargs):
        url = f"{self._base}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers, cookies=self._cookies,
                timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise SharedStorageError(f"Could not reach {self._base}: {exc}") from exc
        if resp.status_code == 401:
            raise SharedStorageError("Not signed in — run /login first.")
        if not resp.ok:
            raise SharedStorageError(_problem(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise SharedStorageError(f"Non-JSON reply from {path}") from exc

    # ── Queries ──

    def usage(self) -> Usage:
        data = self._call("GET", "/api/storage/usage")
        return Usage(
            tier=data.get("tier", "free"),
            used_bytes=data.get("used_bytes", 0),
            free_bytes=data.get("free_bytes", 0),
            max_bytes=data.get("max_bytes", 0),
            overage_bytes=data.get("overage_bytes", 0),
            est_cost_cents=data.get("est_cost_cents", 0),
            max_file_bytes=data.get("max_file_bytes", 0),
        )

    def list(self, prefix: str = "") -> list[Entry]:
        params = {"prefix": prefix} if prefix else None
        data = self._call("GET", "/api/storage/list", params=params)
        return [
            Entry(
                path=item.get("path", ""),
                name=item.get("name", ""),
                type=item.get("type", "file"),
                size=item.get("size", 0),
                modified=item.get("modified", ""),
            )
            for item in data.get("files", [])
        ]

    # ── Transfers ──

    def push_file(self, local_path: str, remote_path: str) -> int:
        """Upload one local file. Returns the byte count written."""
        remote = clean_remote_path(remote_path)
        try:
            size = os.path.getsize(local_path)
        except OSError as exc:
            raise SharedStorageError(f"Cannot read {local_path}: {exc}") from exc

        content_type = _guess_type(local_path)
        presign = self._call("POST", "/api/storage/presign-upload", json={
            "path": remote, "size": size, "content_type": content_type,
        })

        # Straight to object storage — deliberately without the Laintas
        # session, which the storage host has no business seeing.
        try:
            with open(local_path, "rb") as handle:
                resp = requests.put(
                    presign["upload_url"], data=handle,
                    headers={"Content-Type": content_type,
                             "Content-Length": str(size)},
                    timeout=TRANSFER_TIMEOUT)
        except (OSError, requests.RequestException) as exc:
            raise SharedStorageError(f"Upload of {local_path} failed: {exc}") from exc
        if not resp.ok:
            raise SharedStorageError(
                f"Upload of {local_path} rejected by storage (HTTP {resp.status_code})")
        return size

    def pull_file(self, remote_path: str, local_path: str) -> int:
        """Download one file. Returns the byte count written."""
        remote = clean_remote_path(remote_path)
        data = self._call("GET", "/api/storage/download", params={"path": remote})

        parent = os.path.dirname(os.path.abspath(local_path))
        os.makedirs(parent, exist_ok=True)
        written = 0
        try:
            with requests.get(data["download_url"], stream=True,
                              timeout=TRANSFER_TIMEOUT) as resp:
                if not resp.ok:
                    raise SharedStorageError(
                        f"Download of {remote} failed (HTTP {resp.status_code})")
                # Write to a temporary name so an interrupted transfer cannot
                # leave a half file sitting where the real one should be.
                tmp = f"{local_path}.part"
                with open(tmp, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        handle.write(chunk)
                        written += len(chunk)
                os.replace(tmp, local_path)
        except (OSError, requests.RequestException) as exc:
            raise SharedStorageError(f"Download of {remote} failed: {exc}") from exc
        return written

    # ── Mutations ──

    def mkdir(self, path: str) -> None:
        self._call("POST", "/api/storage/mkdir", json={"path": clean_remote_path(path)})

    def remove(self, path: str) -> int:
        data = self._call("DELETE", "/api/storage/delete",
                          params={"path": clean_remote_path(path)})
        return data.get("deleted", 0)

    def move(self, src: str, dest: str) -> None:
        self._call("POST", "/api/storage/move", json={
            "from": clean_remote_path(src), "to": clean_remote_path(dest)})

    def copy(self, src: str, dest: str) -> None:
        self._call("POST", "/api/storage/copy", json={
            "from": clean_remote_path(src), "to": clean_remote_path(dest)})


_TEXT_EXTENSIONS = {
    ".md": "text/markdown", ".txt": "text/plain", ".json": "application/json",
    ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/typescript",
    ".tsx": "text/typescript", ".jsx": "text/javascript", ".css": "text/css",
    ".html": "text/html", ".csv": "text/csv", ".yml": "text/yaml",
    ".yaml": "text/yaml", ".toml": "text/plain", ".sh": "text/x-shellscript",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".pdf": "application/pdf", ".zip": "application/zip",
}


def _guess_type(path: str) -> str:
    return _TEXT_EXTENSIONS.get(os.path.splitext(path)[1].lower(),
                                "application/octet-stream")


def walk_local(root: str) -> list[tuple[str, str]]:
    """(absolute local path, path relative to `root`) for every file under a
    directory. Symlinks are not followed — a link out of the tree would upload
    something the user did not mean to share."""
    collected = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))
                       and d not in (".git", "node_modules", "__pycache__", ".laintas")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            collected.append((full, os.path.relpath(full, root).replace(os.sep, "/")))
    return sorted(collected)


def human_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 ** 2:
        return f"{count / 1024:.1f} KB"
    if count < 1024 ** 3:
        return f"{count / 1024 ** 2:.1f} MB"
    return f"{count / 1024 ** 3:.2f} GB"
