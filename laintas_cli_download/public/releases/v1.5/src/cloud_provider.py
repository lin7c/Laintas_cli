"""
Cloud storage provider for laintas_cli.
Supports GitHub (repo API) and Google Drive, sharing the same file space as Helpwo.
Config stored in ~/.laintas/cloud.json
"""

import json
import os
import time
import base64
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from paths import LAINTAS_HOME

CLOUD_CONFIG_FILE = LAINTAS_HOME / "cloud.json"


# ── Config persistence ─────────────────────────────────────────────────────────

def load_cloud_config() -> Optional[Dict[str, Any]]:
    try:
        with open(CLOUD_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_cloud_config(cfg: Dict[str, Any]) -> None:
    LAINTAS_HOME.mkdir(parents=True, exist_ok=True)
    tmp = str(CLOUD_CONFIG_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CLOUD_CONFIG_FILE)


def clear_cloud_config() -> None:
    try:
        CLOUD_CONFIG_FILE.unlink()
    except Exception:
        pass


# ── GitHub provider ────────────────────────────────────────────────────────────

class GitHubProvider:
    """File operations against a GitHub repo via the Contents API."""

    def __init__(self, token: str, owner: str, repo: str, branch: str = "main"):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self._tree_cache: Optional[List[Dict]] = None
        self._sha_cache: Dict[str, str] = {}

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _api(self, method: str, path: str, **kwargs):
        url = f"https://api.github.com{path}"
        r = _requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)
        return r

    def _invalidate(self):
        self._tree_cache = None

    def _get_tree(self) -> List[Dict]:
        if self._tree_cache is not None:
            return self._tree_cache
        r = self._api("GET", f"/repos/{self.owner}/{self.repo}/git/trees/{self.branch}?recursive=1")
        r.raise_for_status()
        self._tree_cache = [t for t in r.json().get("tree", []) if t.get("type") == "blob"]
        return self._tree_cache

    def _path_to_abs(self, cwd: str, path: str) -> str:
        """Resolve a path relative to cwd into a repo-root-relative path."""
        if os.path.isabs(path):
            return path.lstrip("/")
        full = os.path.normpath(os.path.join(cwd.lstrip("/"), path))
        return full.lstrip("/")

    def ls(self, cwd: str, path: str = ".") -> Dict:
        dir_path = self._path_to_abs(cwd, path)
        if dir_path == ".":
            dir_path = ""
        try:
            tree = self._get_tree()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        entries = []
        seen_dirs = set()
        for item in tree:
            p = item["path"]
            if dir_path:
                if not p.startswith(dir_path + "/"):
                    continue
                rel = p[len(dir_path) + 1:]
            else:
                rel = p
            parts = rel.split("/")
            if len(parts) == 1:
                self._sha_cache[p] = item.get("sha", "")
                entries.append({"name": parts[0], "type": "file", "size": item.get("size")})
            elif parts[0] not in seen_dirs:
                seen_dirs.add(parts[0])
                entries.append({"name": parts[0], "type": "dir", "size": None})
        return {"ok": True, "result": sorted(entries, key=lambda e: (e["type"] == "file", e["name"])), "path": "/" + (dir_path or "")}

    def read(self, cwd: str, path: str, offset: int = 1, limit: int = 2000) -> Dict:
        repo_path = self._path_to_abs(cwd, path)
        try:
            r = self._api("GET", f"/repos/{self.owner}/{self.repo}/contents/{repo_path}?ref={self.branch}")
            r.raise_for_status()
            data = r.json()
            self._sha_cache[repo_path] = data.get("sha", "")
            content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
            lines = content.split("\n")
            total = len(lines)
            start = max(0, offset - 1)
            end = min(start + limit, total)
            selected = lines[start:end]
            width = len(str(end))
            body = "\n".join(f"{(start + i + 1):>{width}}→{ln}" for i, ln in enumerate(selected))
            return {"ok": True, "result": body, "path": "/" + repo_path, "total_lines": total,
                    "lines_returned": len(selected), "truncated": end < total, "offset": offset}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def write(self, cwd: str, path: str, content: str) -> Dict:
        repo_path = self._path_to_abs(cwd, path)
        encoded = base64.b64encode(content.encode("utf-8")).decode()
        sha = self._sha_cache.get(repo_path)
        if not sha:
            # Try to get current SHA
            r = self._api("GET", f"/repos/{self.owner}/{self.repo}/contents/{repo_path}?ref={self.branch}")
            if r.status_code == 200:
                sha = r.json().get("sha", "")
                self._sha_cache[repo_path] = sha
        payload = {
            "message": f"helpwo: update {repo_path}",
            "content": encoded,
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        try:
            r = self._api("PUT", f"/repos/{self.owner}/{self.repo}/contents/{repo_path}", json=payload)
            r.raise_for_status()
            new_sha = r.json().get("content", {}).get("sha", "")
            if new_sha:
                self._sha_cache[repo_path] = new_sha
            self._invalidate()
            return {"ok": True, "result": f"wrote {len(content)} bytes to {repo_path}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def describe(self) -> str:
        return f"GitHub: {self.owner}/{self.repo} ({self.branch})"


# ── Google Drive provider ──────────────────────────────────────────────────────

class GoogleDriveProvider:
    """File operations against Google Drive using the v3 REST API."""

    DRIVE_API = "https://www.googleapis.com/drive/v3"
    UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, access_token: str, refresh_token: str = "",
                 client_id: str = "", expires_at: float = 0):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.expires_at = expires_at
        self._root_id: Optional[str] = None
        self._id_cache: Dict[str, str] = {}  # path -> Drive file ID

    def _ensure_token(self):
        if self.refresh_token and self.client_id and time.time() > self.expires_at - 60:
            # Refresh using Google's token endpoint (requires client_secret)
            # The client_secret is not stored in CLI for security; token refresh
            # falls back to re-auth if expired. Helpwo handles refresh via its own flow.
            pass

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def _api(self, method: str, url: str, **kwargs):
        self._ensure_token()
        return _requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)

    def _get_root_folder_id(self) -> str:
        if self._root_id:
            return self._root_id
        # Find or create "Helpwo" folder in Drive root
        r = self._api("GET", f"{self.DRIVE_API}/files", params={
            "q": "name='Helpwo' and mimeType='application/vnd.google-apps.folder' and 'root' in parents and trashed=false",
            "fields": "files(id,name)",
        })
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            self._root_id = files[0]["id"]
        else:
            cr = self._api("POST", f"{self.DRIVE_API}/files", json={
                "name": "Helpwo",
                "mimeType": "application/vnd.google-apps.folder",
            })
            cr.raise_for_status()
            self._root_id = cr.json()["id"]
        return self._root_id

    def _resolve_folder_id(self, path: str) -> Optional[str]:
        """Resolve a repo-relative directory path to its Drive folder ID."""
        if not path or path == "/":
            return self._get_root_folder_id()
        parts = path.strip("/").split("/")
        parent_id = self._get_root_folder_id()
        for part in parts:
            r = self._api("GET", f"{self.DRIVE_API}/files", params={
                "q": f"name='{part}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false",
                "fields": "files(id)",
            })
            files = r.json().get("files", [])
            if not files:
                return None
            parent_id = files[0]["id"]
        return parent_id

    def _path_to_abs(self, cwd: str, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(cwd, path))

    def ls(self, cwd: str, path: str = ".") -> Dict:
        abs_path = self._path_to_abs(cwd, path)
        try:
            folder_id = self._resolve_folder_id(abs_path)
            if not folder_id:
                return {"ok": False, "error": f"Directory not found: {abs_path}"}
            r = self._api("GET", f"{self.DRIVE_API}/files", params={
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "files(id,name,mimeType,size)",
                "pageSize": 1000,
            })
            r.raise_for_status()
            entries = []
            for f in r.json().get("files", []):
                is_dir = f["mimeType"] == "application/vnd.google-apps.folder"
                entries.append({
                    "name": f["name"],
                    "type": "dir" if is_dir else "file",
                    "size": int(f.get("size", 0)) if not is_dir else None,
                })
                full_path = abs_path.rstrip("/") + "/" + f["name"]
                self._id_cache[full_path] = f["id"]
            return {"ok": True, "result": sorted(entries, key=lambda e: (e["type"] == "file", e["name"])), "path": abs_path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_file_id(self, abs_path: str) -> Optional[str]:
        if abs_path in self._id_cache:
            return self._id_cache[abs_path]
        parent = os.path.dirname(abs_path)
        name = os.path.basename(abs_path)
        parent_id = self._resolve_folder_id(parent)
        if not parent_id:
            return None
        r = self._api("GET", f"{self.DRIVE_API}/files", params={
            "q": f"name='{name}' and '{parent_id}' in parents and trashed=false",
            "fields": "files(id)",
        })
        files = r.json().get("files", [])
        if files:
            self._id_cache[abs_path] = files[0]["id"]
            return files[0]["id"]
        return None

    def read(self, cwd: str, path: str, offset: int = 1, limit: int = 2000) -> Dict:
        abs_path = self._path_to_abs(cwd, path)
        try:
            file_id = self._get_file_id(abs_path)
            if not file_id:
                return {"ok": False, "error": f"File not found: {abs_path}"}
            r = self._api("GET", f"{self.DRIVE_API}/files/{file_id}?alt=media")
            r.raise_for_status()
            content = r.text
            lines = content.split("\n")
            total = len(lines)
            start = max(0, offset - 1)
            end = min(start + limit, total)
            selected = lines[start:end]
            width = len(str(end))
            body = "\n".join(f"{(start + i + 1):>{width}}→{ln}" for i, ln in enumerate(selected))
            return {"ok": True, "result": body, "path": abs_path, "total_lines": total,
                    "lines_returned": len(selected), "truncated": end < total, "offset": offset}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _ensure_parent_folder(self, abs_path: str) -> Optional[str]:
        parent = os.path.dirname(abs_path)
        parts = parent.strip("/").split("/")
        parent_id = self._get_root_folder_id()
        current_path = ""
        for part in parts:
            if not part:
                continue
            current_path += "/" + part
            if current_path in self._id_cache:
                parent_id = self._id_cache[current_path]
                continue
            r = self._api("GET", f"{self.DRIVE_API}/files", params={
                "q": f"name='{part}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false",
                "fields": "files(id)",
            })
            files = r.json().get("files", [])
            if files:
                parent_id = files[0]["id"]
            else:
                cr = self._api("POST", f"{self.DRIVE_API}/files", json={
                    "name": part,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                })
                cr.raise_for_status()
                parent_id = cr.json()["id"]
            self._id_cache[current_path] = parent_id
        return parent_id

    def write(self, cwd: str, path: str, content: str) -> Dict:
        abs_path = self._path_to_abs(cwd, path)
        name = os.path.basename(abs_path)
        try:
            file_id = self._get_file_id(abs_path)
            encoded = content.encode("utf-8")
            if file_id:
                r = self._api("PATCH",
                    f"{self.UPLOAD_API}/files/{file_id}?uploadType=media",
                    data=encoded,
                    headers={**self._headers(), "Content-Type": "text/plain; charset=utf-8"},
                )
                r.raise_for_status()
            else:
                parent_id = self._ensure_parent_folder(abs_path)
                meta = json.dumps({"name": name, "parents": [parent_id]}).encode()
                boundary = "----------laintas_upload"
                body = (
                    f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                    + meta + f"\r\n--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n".encode()
                    + encoded + f"\r\n--{boundary}--".encode()
                )
                r = self._api("POST",
                    f"{self.UPLOAD_API}/files?uploadType=multipart",
                    data=body,
                    headers={**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
                )
                r.raise_for_status()
                self._id_cache[abs_path] = r.json().get("id", "")
            return {"ok": True, "result": f"wrote {len(encoded)} bytes to {abs_path}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def describe(self) -> str:
        return "Google Drive: Helpwo folder"


# ── Active provider singleton ──────────────────────────────────────────────────

_active_provider = None


def get_active_provider():
    global _active_provider
    if _active_provider is not None:
        return _active_provider
    cfg = load_cloud_config()
    if not cfg:
        return None
    if not _HAS_REQUESTS:
        return None
    kind = cfg.get("type")
    if kind == "github":
        _active_provider = GitHubProvider(
            token=cfg["token"], owner=cfg["owner"],
            repo=cfg["repo"], branch=cfg.get("branch", "main"),
        )
    elif kind == "gdrive":
        _active_provider = GoogleDriveProvider(
            access_token=cfg["accessToken"],
            refresh_token=cfg.get("refreshToken", ""),
            client_id=cfg.get("clientId", ""),
            expires_at=cfg.get("expiresAt", 0),
        )
    return _active_provider


def set_active_provider(provider, cfg: Dict[str, Any]) -> None:
    global _active_provider
    _active_provider = provider
    save_cloud_config(cfg)


def disconnect() -> None:
    global _active_provider
    _active_provider = None
    clear_cloud_config()
