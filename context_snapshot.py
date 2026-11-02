"""Private, content-addressed snapshots of locally assembled model context.

The store is deliberately independent of the session runtime.  It records only
JSON-compatible values supplied by its caller and can therefore be used by a
context inspector without importing or reconstructing provider requests.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import paths


DEFAULT_MAX_CONVERSATIONS = 200
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_SCHEMA_VERSION = 1
_MISSING = object()
# Retention only needs ordering and blob references, not full call metadata.
# Share this bounded cache across the short-lived module-level store objects.
_RETENTION_CACHE = OrderedDict()
_RETENTION_CACHE_LOCK = threading.Lock()
_RETENTION_CACHE_LIMIT = 512


class ContextSnapshotError(Exception):
    """Base class for context snapshot failures."""


class ContextSnapshotNotFound(ContextSnapshotError):
    """The requested conversation or call does not exist."""


class ContextSnapshotCorrupt(ContextSnapshotError):
    """A snapshot record is missing, malformed, or fails verification."""


class ContextSnapshotSecurityError(ContextSnapshotError):
    """The store contains an unsafe filesystem object such as a symlink."""


class ContextSnapshotSerializationError(ContextSnapshotError, TypeError):
    """A caller supplied a value that cannot be represented as strict JSON."""


# Short aliases are useful to callers that do not want to encode the module
# name into exception handling.
SnapshotError = ContextSnapshotError
NotFoundError = ContextSnapshotNotFound
CorruptRecordError = ContextSnapshotCorrupt


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContextSnapshotSerializationError(
            f"context material is not JSON serializable: {exc}"
        ) from exc
    return text.encode("utf-8")


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ContextSnapshotStore:
    """A bounded private store rooted at ``SESSIONS_DIR/contexts`` by default."""

    def __init__(
        self,
        root: Optional[Union[str, Path]] = None,
        *,
        max_conversations: Optional[int] = DEFAULT_MAX_CONVERSATIONS,
        max_bytes: Optional[int] = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = Path(root) if root is not None else paths.SESSIONS_DIR / "contexts"
        self.max_conversations = self._limit("max_conversations", max_conversations)
        self.max_bytes = self._limit("max_bytes", max_bytes)

    @staticmethod
    def _limit(name: str, value: Optional[int]) -> Optional[int]:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None")
        return value

    @property
    def blobs_dir(self) -> Path:
        return self.root / "blobs"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    def _ensure_dir(self, directory: Path) -> None:
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ContextSnapshotSecurityError(
                    f"snapshot directory is not a real directory: {directory}"
                )
            os.chmod(directory, 0o700, follow_symlinks=False)
        except ContextSnapshotError:
            raise
        except OSError as exc:
            raise ContextSnapshotSecurityError(
                f"cannot secure snapshot directory {directory}: {exc}"
            ) from exc

    def _ensure_layout(self) -> None:
        self._ensure_dir(self.root)
        self._ensure_dir(self.blobs_dir)
        self._ensure_dir(self.sessions_dir)

    @contextmanager
    def _write_guard(self):
        """Serialize append/GC across agents so uncommitted blobs survive."""
        self._ensure_layout()
        try:
            fd = os.open(self.root / ".write.lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
        except OSError as exc:
            raise ContextSnapshotSecurityError("cannot open snapshot write lock") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ContextSnapshotSecurityError("unsafe snapshot write lock")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _session_dir(self, session_id: str, *, create: bool = False) -> Path:
        directory = self.sessions_dir / _key(session_id)
        if create:
            self._ensure_dir(directory)
        return directory

    def _manifest_path(self, session_id: str, conversation_id: str) -> Path:
        return self._session_dir(session_id) / f"{_key(conversation_id)}.json"

    @staticmethod
    def _check_identifier(name: str, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    def _read_bytes(self, path: Path) -> bytes:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ContextSnapshotNotFound(f"snapshot record is missing: {path.name}") from exc
        except OSError as exc:
            raise ContextSnapshotCorrupt(f"cannot inspect snapshot record {path.name}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ContextSnapshotSecurityError(f"refusing unsafe snapshot record: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise ContextSnapshotSecurityError(f"refusing unsafe snapshot record: {path}")
                return handle.read()
        except ContextSnapshotError:
            raise
        except OSError as exc:
            raise ContextSnapshotCorrupt(f"cannot read snapshot record {path.name}: {exc}") from exc

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(self._read_bytes(path).decode("utf-8"))
        except ContextSnapshotError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContextSnapshotCorrupt(f"invalid snapshot JSON in {path.name}") from exc

    def _atomic_write(self, path: Path, data: bytes, *, replace: bool = True) -> None:
        self._ensure_dir(path.parent)
        if path.exists() or path.is_symlink():
            try:
                info = path.lstat()
            except OSError as exc:
                raise ContextSnapshotSecurityError(f"cannot inspect destination {path}") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ContextSnapshotSecurityError(f"refusing to replace unsafe path: {path}")
            if not replace:
                return

        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o600)
            # Recheck immediately before replacement. Store directories are
            # private, so another uid cannot race this check and rename.
            if path.is_symlink():
                raise ContextSnapshotSecurityError(f"refusing to replace symlink: {path}")
            os.replace(temporary, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except ContextSnapshotError:
            raise
        except OSError as exc:
            raise ContextSnapshotError(f"cannot write snapshot record {path}: {exc}") from exc
        finally:
            try:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass

    def _put_blob(self, value: Any) -> str:
        return self._put_blob_bytes(_json_bytes(value))

    def _put_blob_bytes(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.blobs_dir / f"{digest}.json"
        if path.exists() or path.is_symlink():
            existing = self._read_bytes(path)
            if hashlib.sha256(existing).hexdigest() != digest or existing != data:
                raise ContextSnapshotCorrupt(f"content-addressed blob {digest} is invalid")
            try:
                os.chmod(path, 0o600, follow_symlinks=False)
            except OSError as exc:
                raise ContextSnapshotSecurityError(f"cannot secure blob {digest}: {exc}") from exc
            return digest
        self._atomic_write(path, data, replace=False)
        return digest

    def _load_blob(self, digest: Any) -> Any:
        if not isinstance(digest, str) or len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest):
            raise ContextSnapshotCorrupt("manifest contains an invalid blob hash")
        path = self.blobs_dir / f"{digest}.json"
        try:
            data = self._read_bytes(path)
        except ContextSnapshotNotFound as exc:
            raise ContextSnapshotCorrupt(f"snapshot blob {digest} is missing") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise ContextSnapshotCorrupt(f"snapshot blob {digest} failed verification")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContextSnapshotCorrupt(f"snapshot blob {digest} is invalid JSON") from exc

    def _validate_manifest(
        self, manifest: Any, *, session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(manifest, dict) or manifest.get("schema_version") != _SCHEMA_VERSION:
            raise ContextSnapshotCorrupt("invalid context conversation manifest")
        if not isinstance(manifest.get("session_id"), str) or not isinstance(
                manifest.get("conversation_id"), str):
            raise ContextSnapshotCorrupt("manifest identifiers are invalid")
        if session_id is not None and manifest["session_id"] != session_id:
            raise ContextSnapshotCorrupt("manifest session identifier does not match its path")
        if conversation_id is not None and manifest["conversation_id"] != conversation_id:
            raise ContextSnapshotCorrupt("manifest conversation identifier does not match its path")
        calls = manifest.get("calls")
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            raise ContextSnapshotCorrupt("manifest calls are invalid")
        return manifest

    def append_call(
        self,
        session_id: str,
        conversation_id: Optional[str] = None,
        system_prompt: Any = _MISSING,
        messages: Any = _MISSING,
        tool_schemas: Any = _MISSING,
        metadata: Any = None,
        system_sections: Any = None,
        gateway_context_receipt: Any = None,
        *,
        turn_id: Optional[str] = None,
        tools: Any = _MISSING,
        structured_system_sections: Any = _MISSING,
        call_id: Optional[str] = None,
        expand: bool = True,
    ) -> Dict[str, Any]:
        """Append one model call and return a detached snapshot.

        ``turn_id`` aliases ``conversation_id`` and ``tools`` aliases
        ``tool_schemas`` to accommodate provider-facing call sites. Set
        ``expand=False`` to return references without reading blobs back.
        """
        session_id = self._check_identifier("session_id", session_id)
        if conversation_id is None:
            conversation_id = turn_id
        elif turn_id is not None and turn_id != conversation_id:
            raise ValueError("conversation_id and turn_id disagree")
        conversation_id = self._check_identifier("conversation_id", conversation_id)
        if system_prompt is _MISSING or messages is _MISSING:
            raise TypeError("system_prompt and messages are required")
        if tool_schemas is _MISSING:
            tool_schemas = tools
        elif tools is not _MISSING and tools != tool_schemas:
            raise ValueError("tool_schemas and tools disagree")
        if tool_schemas is _MISSING:
            raise TypeError("tool_schemas (or tools) is required")
        if structured_system_sections is not _MISSING:
            if system_sections is not None and structured_system_sections != system_sections:
                raise ValueError("system_sections aliases disagree")
            system_sections = structured_system_sections
        if call_id is None:
            call_id = uuid.uuid4().hex
        call_id = self._check_identifier("call_id", call_id)

        # Encoding freezes and validates every field before touching storage.
        # Keep large blobs encoded instead of decoding and re-encoding them.
        encoded = {
            "system_prompt": _json_bytes(system_prompt),
            "messages": _json_bytes(messages),
            "tool_schemas": _json_bytes(tool_schemas),
            "metadata": _json_bytes(metadata),
            "system_sections": _json_bytes(system_sections),
            "gateway_context_receipt": _json_bytes(gateway_context_receipt),
        }
        with self._write_guard():
            return self._append_encoded(session_id, conversation_id, call_id,
                                        encoded, expand=expand)

    def _append_encoded(self, session_id, conversation_id, call_id, encoded, *, expand):
        session_dir = self._session_dir(session_id, create=True)
        path = session_dir / f"{_key(conversation_id)}.json"
        now = _utc_now()
        sequence = time.time_ns()
        if path.exists() or path.is_symlink():
            manifest = self._validate_manifest(
                self._read_json(path), session_id=session_id,
                conversation_id=conversation_id,
            )
            if any(call.get("call_id") == call_id for call in manifest["calls"]):
                raise ContextSnapshotError(f"call_id already exists: {call_id}")
        else:
            manifest = {
                "schema_version": _SCHEMA_VERSION,
                "session_id": session_id,
                "conversation_id": conversation_id,
                "created_at": now,
                "created_sequence": sequence,
                "calls": [],
            }

        call = {
            "call_id": call_id,
            "created_at": now,
            "sequence": sequence,
            "system_prompt_blob": self._put_blob_bytes(encoded["system_prompt"]),
            "messages_blob": self._put_blob_bytes(encoded["messages"]),
            "tool_schemas_blob": self._put_blob_bytes(encoded["tool_schemas"]),
            "metadata": json.loads(encoded["metadata"]),
            "system_sections": json.loads(encoded["system_sections"]),
            "gateway_context_receipt": json.loads(encoded["gateway_context_receipt"]),
        }
        manifest["calls"].append(call)
        manifest["updated_at"] = now
        manifest["updated_sequence"] = sequence
        self._atomic_write(path, _json_bytes(manifest))
        result = self._expand_call(call) if expand else call
        self._apply_retention()
        return result

    def _manifest_paths(self, session_id: Optional[str] = None) -> List[Path]:
        base = self._session_dir(session_id) if session_id is not None else self.sessions_dir
        try:
            if not base.exists():
                return []
            if base.is_symlink() or not base.is_dir():
                raise ContextSnapshotSecurityError(f"unsafe snapshot session directory: {base}")
            if session_id is not None:
                return sorted(base.glob("*.json"))
            paths_found: List[Path] = []
            for directory in base.iterdir():
                if directory.is_symlink():
                    raise ContextSnapshotSecurityError(
                        f"unsafe snapshot session directory: {directory}"
                    )
                if directory.is_dir():
                    paths_found.extend(directory.glob("*.json"))
            return sorted(paths_found)
        except ContextSnapshotError:
            raise
        except OSError as exc:
            raise ContextSnapshotError(f"cannot enumerate snapshot store: {exc}") from exc

    def list_conversations(self, session_id: str) -> List[Dict[str, Any]]:
        """Return valid conversation summaries newest first; missing is empty."""
        session_id = self._check_identifier("session_id", session_id)
        if not self.root.exists():
            return []
        summaries = []
        for path in self._manifest_paths(session_id):
            try:
                manifest = self._validate_manifest(self._read_json(path), session_id=session_id)
                summaries.append({
                    "session_id": manifest["session_id"],
                    "conversation_id": manifest["conversation_id"],
                    "call_count": len(manifest["calls"]),
                    "created_at": manifest.get("created_at"),
                    "updated_at": manifest.get("updated_at"),
                    "updated_sequence": manifest.get("updated_sequence", 0),
                })
            except (ContextSnapshotCorrupt, ContextSnapshotNotFound):
                # Listing is an availability-oriented operation. A bad record
                # must not hide otherwise inspectable conversations.
                continue
        summaries.sort(
            key=lambda item: (item.get("updated_sequence", 0), item["conversation_id"]),
            reverse=True,
        )
        return copy.deepcopy(summaries)

    def _load_manifest_by_id(self, session_id: str, conversation_id: str) -> Dict[str, Any]:
        path = self._manifest_path(session_id, conversation_id)
        return self._validate_manifest(
            self._read_json(path), session_id=session_id, conversation_id=conversation_id,
        )

    def _expand_call(self, call: Dict[str, Any]) -> Dict[str, Any]:
        required = ("system_prompt_blob", "messages_blob", "tool_schemas_blob")
        if not all(name in call for name in required):
            raise ContextSnapshotCorrupt("call is missing content references")
        return {
            "call_id": call.get("call_id"),
            "created_at": call.get("created_at"),
            "system_prompt": self._load_blob(call["system_prompt_blob"]),
            "messages": self._load_blob(call["messages_blob"]),
            "tool_schemas": self._load_blob(call["tool_schemas_blob"]),
            "metadata": copy.deepcopy(call.get("metadata")),
            "system_sections": copy.deepcopy(call.get("system_sections")),
            "gateway_context_receipt": copy.deepcopy(call.get("gateway_context_receipt")),
        }

    def _expand_manifest(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "session_id": manifest["session_id"],
            "conversation_id": manifest["conversation_id"],
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "calls": [self._expand_call(call) for call in manifest["calls"]],
        }

    def expand_call(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """Expand a detached raw call returned with ``expand=False``."""
        if not isinstance(call, dict):
            raise ContextSnapshotCorrupt("call record is invalid")
        return self._expand_call(copy.deepcopy(call))

    def expand_conversation(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Expand a detached raw conversation returned with ``expand=False``."""
        checked = self._validate_manifest(copy.deepcopy(manifest))
        return self._expand_manifest(checked)

    def load_conversation(
        self, session_id: str, newest_index: int = 1, *,
        conversation_id: Optional[str] = None, expand: bool = True,
    ) -> Dict[str, Any]:
        """Load a conversation; index 1 is the most recently appended one."""
        session_id = self._check_identifier("session_id", session_id)
        if isinstance(newest_index, bool) or not isinstance(newest_index, int) or newest_index < 1:
            raise ValueError("newest_index must be a positive integer")
        if conversation_id is None:
            conversations = self.list_conversations(session_id)
            if newest_index > len(conversations):
                raise ContextSnapshotNotFound(
                    f"conversation {newest_index} does not exist for session {session_id}"
                )
            conversation_id = conversations[newest_index - 1]["conversation_id"]
        else:
            conversation_id = self._check_identifier("conversation_id", conversation_id)
        manifest = self._load_manifest_by_id(session_id, conversation_id)
        return self._expand_manifest(manifest) if expand else copy.deepcopy(manifest)

    def load_call(
        self, session_id: str, newest_index: int = 1, call_index: int = 1, *,
        conversation_id: Optional[str] = None, call_id: Optional[str] = None,
        expand: bool = True,
    ) -> Dict[str, Any]:
        """Load one call; ``call_index=1`` selects the newest call."""
        if isinstance(call_index, bool) or not isinstance(call_index, int) or call_index < 1:
            raise ValueError("call_index must be a positive integer")
        manifest = self.load_conversation(
            session_id, newest_index, conversation_id=conversation_id, expand=False,
        )
        calls = manifest["calls"]
        if call_id is not None:
            matches = [call for call in calls if call.get("call_id") == call_id]
            if not matches:
                raise ContextSnapshotNotFound(f"call does not exist: {call_id}")
            call = matches[0]
        else:
            if call_index > len(calls):
                raise ContextSnapshotNotFound(f"call {call_index} does not exist")
            call = calls[-call_index]
        return self._expand_call(call) if expand else copy.deepcopy(call)

    @staticmethod
    def _safe_unlink(path: Path) -> bool:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return False
            path.unlink()
            return True
        except (FileNotFoundError, OSError):
            return False

    def _store_bytes(self) -> int:
        total = 0
        if not self.root.exists() or self.root.is_symlink():
            return total
        try:
            for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
                dirnames[:] = [
                    name for name in dirnames
                    if not (Path(directory) / name).is_symlink()
                ]
                for name in filenames:
                    path = Path(directory) / name
                    try:
                        info = path.lstat()
                        if stat.S_ISREG(info.st_mode):
                            total += info.st_size
                    except OSError:
                        continue
        except OSError:
            return total
        return total

    def _retention_metadata(self, path):
        try:
            info = path.lstat()
        except OSError as exc:
            raise ContextSnapshotCorrupt("cannot inspect retention record") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ContextSnapshotSecurityError("unsafe retention record")
        key = str(path.absolute())
        signature = (info.st_dev, info.st_ino, info.st_size,
                     info.st_mtime_ns, info.st_ctime_ns)
        with _RETENTION_CACHE_LOCK:
            cached = _RETENTION_CACHE.get(key)
            if cached is not None and cached[0] == signature:
                _RETENTION_CACHE.move_to_end(key)
                return cached[1]
        manifest = self._validate_manifest(self._read_json(path))
        references = frozenset(
            call[field] for call in manifest["calls"]
            for field in ("system_prompt_blob", "messages_blob", "tool_schemas_blob")
            if isinstance(call.get(field), str))
        result = (manifest.get("updated_sequence", 0), references)
        with _RETENTION_CACHE_LOCK:
            _RETENTION_CACHE[key] = (signature, result)
            _RETENTION_CACHE.move_to_end(key)
            while len(_RETENTION_CACHE) > _RETENTION_CACHE_LIMIT:
                _RETENTION_CACHE.popitem(last=False)
        return result

    def _apply_retention(self) -> None:
        """Evict oldest conversations, never traversing outside ``root``."""
        records = []
        corrupt_seen = False
        for path in self._manifest_paths():
            try:
                sequence, references = self._retention_metadata(path)
                records.append((sequence, path, references))
            except ContextSnapshotError:
                corrupt_seen = True
        records.sort(key=lambda item: (item[0], str(item[1])))

        if self.max_conversations is not None:
            for record in list(records):
                if len(records) <= self.max_conversations:
                    break
                if self._safe_unlink(record[1]):
                    records.remove(record)

        # Count the store once, including unknown files. Track successful
        # removals instead of walking the whole tree for every eviction.
        total = self._store_bytes() if self.max_bytes is not None else 0
        references = Counter(digest for _, _, refs in records for digest in refs)
        blobs = {}
        for path in self.blobs_dir.glob("*.json"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                blobs[path.stem] = (path, info.st_size)

        def remove_blob(digest):
            nonlocal total
            blob = blobs.get(digest)
            if blob is not None and self._safe_unlink(blob[0]):
                total -= blob[1]
                blobs.pop(digest, None)

        if not corrupt_seen:
            for digest in list(blobs):
                if not references[digest]:
                    remove_blob(digest)

        if self.max_bytes is not None:
            for _, path, refs in records:
                if total <= self.max_bytes:
                    break
                try:
                    size = path.lstat().st_size
                except OSError:
                    continue
                if not self._safe_unlink(path):
                    continue
                total -= size
                if not corrupt_seen:
                    for digest in refs:
                        references[digest] -= 1
                        if not references[digest]:
                            remove_blob(digest)


# Module-level helpers keep integration call sites small while preserving an
# injectable root for tests and alternate runtimes.
def append_call(session_id: str, conversation_id: Optional[str] = None, *args: Any,
                root: Optional[Union[str, Path]] = None,
                max_conversations: Optional[int] = DEFAULT_MAX_CONVERSATIONS,
                max_bytes: Optional[int] = DEFAULT_MAX_BYTES, **kwargs: Any) -> Dict[str, Any]:
    return ContextSnapshotStore(
        root, max_conversations=max_conversations, max_bytes=max_bytes,
    ).append_call(session_id, conversation_id, *args, **kwargs)


def list_conversations(session_id: str, *, root: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    return ContextSnapshotStore(root).list_conversations(session_id)


def load_conversation(session_id: str, newest_index: int = 1, *,
                      root: Optional[Union[str, Path]] = None,
                      conversation_id: Optional[str] = None,
                      expand: bool = True) -> Dict[str, Any]:
    return ContextSnapshotStore(root).load_conversation(
        session_id, newest_index, conversation_id=conversation_id, expand=expand,
    )


def load_call(session_id: str, newest_index: int = 1, call_index: int = 1, *,
              root: Optional[Union[str, Path]] = None,
              conversation_id: Optional[str] = None,
              call_id: Optional[str] = None,
              expand: bool = True) -> Dict[str, Any]:
    return ContextSnapshotStore(root).load_call(
        session_id, newest_index, call_index, conversation_id=conversation_id,
        call_id=call_id, expand=expand,
    )


ContextStore = ContextSnapshotStore
