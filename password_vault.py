"""password_vault: encrypted credential storage backing /password.

Implements the storage half of docs/password-vault-design.md:

- AES-256-GCM (audited `cryptography` package) with a fresh random nonce per
  encryption; the format version is bound as AEAD associated data.
- scrypt (memory-hard) passphrase derivation with a random 32-byte salt;
  KDF parameters live in a plaintext header inside the vault file.
- vault.bin is the single source of truth and is replaced atomically
  (write-temp + fsync + rename), so a crash leaves either the complete old
  or the complete new state, never a mixture.
- meta.json is a disposable cache of public metadata (opaque id,
  user-written description, approved HTTPS origins) so listings work while
  the vault is locked. It is rebuilt from vault.bin on unlock and after
  every mutation. Usernames, passwords, and notes live only inside the
  encrypted blob and are never written anywhere else.
- The derived key exists only in process memory, auto-locks after
  AUTOLOCK_SECONDS of inactivity, and references are dropped on lock/exit.
  Python cannot guarantee memory erasure; references are simply discarded.
- Everything fails closed: truncated files, tampered ciphertext, version
  mismatch, or malformed JSON are hard errors, never silent partial loads.

This module never prints, logs, or returns secret values except through
get_secret(), which exists for a future trusted broker process and must
never be wired to a model-facing tool.
"""

from __future__ import annotations

import base64
import fcntl
import functools
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
import weakref
from datetime import datetime, timezone
from hashlib import scrypt

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_FORMAT = 2
_MAGIC = b"LPV1\n"
# The AAD deliberately stays b"password-vault:1": format 2 reuses the
# format-1 AEAD binding so v1 vaults remain decryptable and migrate
# transparently on unlock. The in-blob "format" field distinguishes the
# payload versions.
_AAD = b"password-vault:1"
_KINDS = ("login", "secret")
NONCE_BYTES = 12
SALT_BYTES = 32
AUTOLOCK_SECONDS = 600

_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
# hashlib.scrypt requires maxmem >= 128*r*n; give comfortable headroom so
# unlock never dies on the OpenSSL default limit.
_SCRYPT_MAXMEM = 128 * _SCRYPT_R * _SCRYPT_N * 4

_DESCRIPTION_MAX = 200
_ID_RE = re.compile(r"^cred_[0-9a-f]{12}$")

#: Sentinel for update_entry(): leave this field unchanged.
KEEP = object()


class VaultError(RuntimeError):
    """Vault state is unusable (corrupt, tampered, or wrong format)."""


class VaultAuthError(VaultError):
    """Decryption failed: wrong passphrase or tampered ciphertext."""


class VaultNotInitialized(VaultError):
    """No vault exists yet."""


class VaultExists(VaultError):
    """A vault already exists where one was about to be created."""


class VaultLocked(RuntimeError):
    """A secret operation was requested while the vault is locked."""


def default_vault_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".laintas", "password-vault")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_origin(origin: str):
    """Canonicalize to exactly 'https://host' or 'https://host:port'.

    Returns None for anything that is not an explicit HTTPS origin: http,
    wildcards, userinfo, paths, queries, fragments, or bad ports.
    Subdomains are NOT expanded — origin matching is exact by design.
    """
    if not isinstance(origin, str):
        return None
    text = origin.strip()
    if not text:
        return None
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in text):
        return None
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or "@" in parsed.netloc:
        return None
    host = (parsed.hostname or "").lower()
    if not host or any(ch in host for ch in "*\\%") or port == 0:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    try:
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        else:
            host = host.encode("idna").decode("ascii")
            labels = host.rstrip(".").split(".")
            if len(host) > 253 or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels):
                return None
    except (ValueError, UnicodeError):
        return None
    if parsed.netloc.endswith(":"):
        return None
    if port is None or port == 443:
        return f"https://{host}"
    return f"https://{host}:{port}"


def _valid_id(value) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _clean_description(description) -> str:
    if not isinstance(description, str):
        raise VaultError("description must be text")
    text = (description or "").strip()
    if not text:
        raise VaultError("description must not be empty")
    if len(text) > _DESCRIPTION_MAX:
        raise VaultError(f"description longer than {_DESCRIPTION_MAX} characters")
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in text):
        raise VaultError("description must be a single line")
    return text


def _clean_origins(origins) -> list:
    """Validate user-supplied origins at write time; returns canonical list."""
    if isinstance(origins, str) or not isinstance(origins, (list, tuple)) or not origins:
        raise VaultError("origins must be a non-empty list of exact HTTPS origins")
    canon = []
    for origin in origins:
        normalized = normalize_origin(origin if isinstance(origin, str) else "")
        if normalized is None:
            raise VaultError(
                f"not an exact HTTPS origin: {origin!r} "
                "(no http, wildcards, paths, userinfo, or subdomain expansion)")
        if normalized not in canon:
            canon.append(normalized)
    return canon


def _validate_origins_list(origins, *, allow_empty: bool = False) -> None:
    """Validate stored origins: canonical, no duplicates; non-empty unless
    explicitly allowed (secret entries may have no origin at all)."""
    if not isinstance(origins, list):
        raise VaultError("entry origins must be a list")
    if not origins and not allow_empty:
        raise VaultError("entry origins must be a non-empty list")
    seen = set()
    for origin in origins:
        if not isinstance(origin, str) or normalize_origin(origin) != origin:
            raise VaultError(f"entry origin is not canonical: {origin!r}")
        if origin in seen:
            raise VaultError("duplicate entry origin")
        seen.add(origin)


def _validate_kdf(kdf) -> dict:
    if not isinstance(kdf, dict):
        raise VaultError("vault header: kdf block missing")
    if set(kdf) != {"algo", "salt", "n", "r", "p", "dklen"}:
        raise VaultError("vault header: unexpected kdf fields")
    if kdf["algo"] != "scrypt":
        raise VaultError("vault header: unsupported kdf algorithm")
    for name in ("n", "r", "p", "dklen"):
        if type(kdf[name]) is not int:
            raise VaultError("vault header: non-integer kdf parameter")
    if not (2 ** 14 <= kdf["n"] <= 2 ** 22) or kdf["n"] & (kdf["n"] - 1):
        raise VaultError("vault header: kdf n out of accepted range")
    if not (1 <= kdf["r"] <= 32) or not (1 <= kdf["p"] <= 8):
        raise VaultError("vault header: kdf r/p out of accepted range")
    if kdf["dklen"] != _KEY_LEN:
        raise VaultError("vault header: unsupported key length")
    # Header is untrusted: bound both peak memory and total CPU work before
    # asking OpenSSL to derive anything.
    if (128 * kdf["n"] * kdf["r"] + 256 * kdf["r"] * kdf["p"]
            >= _SCRYPT_MAXMEM or kdf["n"] * kdf["r"] * kdf["p"]
            > _SCRYPT_N * _SCRYPT_R * 8):
        raise VaultError("vault header: kdf resource limit exceeded")
    try:
        salt = base64.b64decode(kdf["salt"], validate=True)
    except Exception:
        raise VaultError("vault header: salt is not valid base64")
    if len(salt) < 16:
        raise VaultError("vault header: salt too short")
    return kdf


def _new_kdf() -> dict:
    return {
        "algo": "scrypt",
        "salt": base64.b64encode(secrets.token_bytes(SALT_BYTES)).decode("ascii"),
        "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P, "dklen": _KEY_LEN,
    }


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    try:
        return scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                      maxmem=_SCRYPT_MAXMEM, dklen=dklen)
    except (ValueError, MemoryError):
        raise VaultError("vault key derivation failed") from None


def _split_vault_file(raw: bytes):
    """Split vault.bin into (kdf dict, nonce, ciphertext)."""
    if not raw.startswith(_MAGIC):
        raise VaultError("vault file: bad magic (not a password vault or wrong format)")
    rest = raw[len(_MAGIC):]
    newline = rest.find(b"\n")
    if newline < 0 or newline > 4096:
        raise VaultError("vault file: header is corrupt")
    # ValueError covers JSONDecodeError and UnicodeDecodeError: a tampered
    # or corrupt header must surface as VaultError, never as a raw exception.
    try:
        kdf = json.loads(rest[:newline].decode("utf-8"))
    except ValueError:
        raise VaultError("vault file: header is not valid JSON")
    _validate_kdf(kdf)
    body = rest[newline + 1:]
    if len(body) <= NONCE_BYTES:
        raise VaultError("vault file: payload is truncated")
    return kdf, body[:NONCE_BYTES], body[NONCE_BYTES:]


def _serialize_vault(kdf: dict, key: bytes, blob: dict) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)  # unique per encryption, always
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(blob).encode("utf-8"), _AAD)
    return _MAGIC + json.dumps(kdf).encode("utf-8") + b"\n" + nonce + ciphertext


def _decrypt_blob(kdf: dict, key: bytes, nonce: bytes, ciphertext: bytes) -> dict:
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
    except InvalidTag:
        raise VaultAuthError("vault decryption failed (wrong passphrase or tampered file)")
    try:
        blob = json.loads(plaintext.decode("utf-8"))
    except ValueError:
        raise VaultError("vault payload is not valid JSON")
    return _migrate_blob(blob)


_LOGIN_FIELDS = {"kind", "description", "origins", "username", "password",
                 "notes", "created", "updated"}
_SECRET_FIELDS = {"kind", "description", "origins", "secret", "notes",
                  "created", "updated"}
_LEGACY_V1_FIELDS = {"description", "origins", "username", "password",
                     "notes", "created", "updated"}


def _validate_entry(eid: str, entry) -> None:
    if not _valid_id(eid):
        raise VaultError("vault payload: bad entry id")
    if not isinstance(entry, dict):
        raise VaultError("vault payload: entry must be an object")
    kind = entry.get("kind")
    if kind == "login":
        if set(entry) != _LOGIN_FIELDS:
            raise VaultError("vault payload: unexpected entry fields")
    elif kind == "secret":
        if set(entry) != _SECRET_FIELDS:
            raise VaultError("vault payload: unexpected entry fields")
    else:
        raise VaultError("vault payload: unknown entry kind")
    if _clean_description(entry["description"]) != entry["description"]:
        raise VaultError("vault payload: non-canonical description")
    _validate_origins_list(entry["origins"],
                           allow_empty=(kind == "secret"))
    for field in ("created", "updated", "notes"):
        if not isinstance(entry[field], str):
            raise VaultError("vault payload: bad entry field type")
    if kind == "login":
        for field in ("username", "password"):
            if not isinstance(entry[field], str):
                raise VaultError("vault payload: bad entry field type")
    elif not isinstance(entry["secret"], str) or not entry["secret"]:
        raise VaultError("vault payload: empty secret value")


def _validate_legacy_v1_entry(eid: str, entry) -> bool:
    """True when the entry has the exact format-1 login shape."""
    if (not isinstance(entry, dict) or set(entry) != _LEGACY_V1_FIELDS
            or not _valid_id(eid)):
        return False
    if _clean_description(entry["description"]) != entry["description"]:
        return False
    try:
        _validate_origins_list(entry["origins"])
    except VaultError:
        return False
    return all(isinstance(entry[f], str)
               for f in ("username", "password", "notes", "created", "updated"))


def _migrate_blob(blob):
    """Validate any supported blob version and return (v2 blob, changed).

    Format 1 entries were logins by definition: each gains kind="login".
    A malformed legacy entry raises VaultError — migration never invents
    data for an entry it cannot interpret.
    """
    if not isinstance(blob, dict):
        raise VaultError("vault payload: not an object")
    if blob.get("format") == VAULT_FORMAT:
        if set(blob) != {"format", "entries"}:
            raise VaultError("vault payload: unexpected fields")
        if not isinstance(blob.get("entries"), dict):
            raise VaultError("vault payload: entries must be an object")
        for eid, entry in blob["entries"].items():
            _validate_entry(eid, entry)
        return blob, False
    if blob.get("format") != 1:
        raise VaultError("vault payload: unsupported format")
    if set(blob) != {"format", "entries"}:
        raise VaultError("vault payload: unexpected fields")
    entries = blob.get("entries")
    if not isinstance(entries, dict):
        raise VaultError("vault payload: entries must be an object")
    migrated = {}
    for eid, entry in entries.items():
        if not _validate_legacy_v1_entry(eid, entry):
            raise VaultError(f"vault payload: un-migratable legacy entry {eid}")
        migrated[eid] = {"kind": "login", **entry}
    return {"format": VAULT_FORMAT, "entries": migrated}, True


def _validate_blob(blob) -> dict:
    """Validate a current-format (v2) blob in place."""
    if not isinstance(blob, dict):
        raise VaultError("vault payload: not an object")
    if blob.get("format") != VAULT_FORMAT:
        raise VaultError("vault payload: unsupported format")
    if set(blob) != {"format", "entries"}:
        raise VaultError("vault payload: unexpected fields")
    entries = blob.get("entries")
    if not isinstance(entries, dict):
        raise VaultError("vault payload: entries must be an object")
    for eid, entry in entries.items():
        _validate_entry(eid, entry)
    return blob


def _validate_cache(data) -> dict:
    if not isinstance(data, dict) or data.get("format") != VAULT_FORMAT:
        raise VaultError("metadata cache: unsupported format")
    if set(data) != {"format", "entries"}:
        raise VaultError("metadata cache: unexpected fields")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise VaultError("metadata cache: entries must be a list")
    ids = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise VaultError("metadata cache: entry must be an object")
        if set(entry) != {"id", "kind", "description", "origins",
                          "created", "updated"}:
            raise VaultError("metadata cache: unexpected entry fields")
        if not _valid_id(entry["id"]):
            raise VaultError("metadata cache: bad entry id")
        if entry["id"] in ids:
            raise VaultError("metadata cache: duplicate entry id")
        ids.add(entry["id"])
        if entry["kind"] not in _KINDS:
            raise VaultError("metadata cache: unknown entry kind")
        if _clean_description(entry["description"]) != entry["description"]:
            raise VaultError("metadata cache: non-canonical description")
        _validate_origins_list(entry["origins"],
                               allow_empty=(entry["kind"] == "secret"))
        for field in ("created", "updated"):
            if not isinstance(entry[field], str) or not entry[field]:
                raise VaultError("metadata cache: bad timestamp")
    return data


def _public_view(eid: str, entry: dict) -> dict:
    return {
        "id": eid,
        "kind": entry["kind"],
        "description": entry["description"],
        "origins": list(entry["origins"]),
        "created": entry["created"],
        "updated": entry["updated"],
    }


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write bytes to path atomically: temp file + fsync + rename.

    Exclusive random temp creation avoids following a predictable symlink.
    Ordinary failures clean up; a process crash may leave an encrypted orphan.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=directory)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class _FileLock:
    """Advisory whole-vault lock so two CLI processes cannot interleave
    read-modify-write cycles on the same files."""

    def __init__(self, path: str, exclusive: bool):
        self._path = path
        self._exclusive = exclusive
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def _serialized(method):
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return guarded


class PasswordVault:
    """Encrypted credential vault. One instance per process is enough.

    Metadata (id/description/origins) is public by design and readable while
    locked through the disposable cache. Secret operations require unlock()
    and auto-lock after AUTOLOCK_SECONDS of inactivity.
    """

    def __init__(self, directory: str = None):
        self._dir = directory or default_vault_dir()
        self._blob_path = os.path.join(self._dir, "vault.bin")
        self._meta_path = os.path.join(self._dir, "meta.json")
        self._lock_path = os.path.join(self._dir, ".lock")
        self._key = None   # derived AES key while unlocked
        self._kdf = None   # kdf header dict while unlocked
        self._last_access = 0.0
        self._state_lock = threading.RLock()
        self._timer = None
        self.cache_warning = False

    # ── state ──────────────────────────────────────────────────────────

    def exists(self) -> bool:
        return os.path.isfile(self._blob_path)

    @_serialized
    def is_unlocked(self) -> bool:
        if self._key is not None and time.monotonic() - self._last_access >= AUTOLOCK_SECONDS:
            self.lock()
        return self._key is not None

    @_serialized
    def lock(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._key = None
        self._kdf = None

    def _touch(self) -> None:
        self._last_access = time.monotonic()
        if self._timer is not None:
            self._timer.cancel()
        owner = weakref.ref(self)

        def expire():
            vault = owner()
            if vault is not None:
                with vault._state_lock:
                    if vault._timer is timer:
                        vault.lock()

        timer = threading.Timer(AUTOLOCK_SECONDS, expire)
        timer.daemon = True
        timer.name = "password-vault-autolock"
        self._timer = timer
        timer.start()

    def _require_unlocked(self) -> None:
        if not self.is_unlocked():
            raise VaultLocked("vault is locked")
        self._touch()

    # ── internal readers/writers ───────────────────────────────────────

    def _read_raw(self) -> bytes:
        try:
            with open(self._blob_path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            raise VaultNotInitialized("no vault exists yet")

    def _decrypt_current(self, kdf, nonce, ciphertext):
        # An unlocked key must remain paired with the header it authenticated
        # at unlock. Otherwise a changed salt could be copied into a new write
        # while still encrypting with the old key, making the vault unreadable.
        if kdf != self._kdf:
            self.lock()
            raise VaultAuthError("vault changed; unlock again")
        try:
            return _decrypt_blob(kdf, self._key, nonce, ciphertext)
        except VaultAuthError:
            self.lock()
            raise

    def _rebuild_cache(self, blob: dict) -> None:
        cache = {
            "format": VAULT_FORMAT,
            "entries": [_public_view(eid, entry)
                        for eid, entry in blob["entries"].items()],
        }
        _atomic_write(self._meta_path, json.dumps(cache, indent=2).encode("utf-8"))

    def _refresh_cache(self, blob: dict) -> None:
        """A disposable cache failure must not turn a committed write into failure."""
        try:
            self._rebuild_cache(blob)
        except OSError:
            self.cache_warning = True
            # Do not leave an old list pretending to describe the new vault.
            try:
                os.unlink(self._meta_path)
            except OSError:
                pass
        else:
            self.cache_warning = False

    # ── lifecycle ──────────────────────────────────────────────────────

    @_serialized
    def create(self, passphrase: str) -> None:
        """Initialize a new vault. Leaves it unlocked with zero entries."""
        if not passphrase:
            raise VaultError("passphrase must not be empty")
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        os.chmod(self._dir, 0o700)
        with _FileLock(self._lock_path, exclusive=True):
            if os.path.isfile(self._blob_path):
                raise VaultExists("a vault already exists")
            kdf = _new_kdf()
            salt = base64.b64decode(kdf["salt"], validate=True)
            key = _derive_key(passphrase, salt, kdf["n"], kdf["r"], kdf["p"], kdf["dklen"])
            blob = {"format": VAULT_FORMAT, "entries": {}}
            _atomic_write(self._blob_path, _serialize_vault(kdf, key, blob))
            self._refresh_cache(blob)
        self._kdf = kdf
        self._key = key
        self._touch()

    @_serialized
    def unlock(self, passphrase: str) -> bool:
        """Verify the passphrase and unlock. False on wrong passphrase or
        tampered ciphertext (both fail closed); hard error on corrupt
        structure. Also refreshes the public metadata cache."""
        self.lock()
        if not os.path.isfile(self._blob_path):
            raise VaultNotInitialized("no vault exists yet")
        with _FileLock(self._lock_path, exclusive=True):
            kdf, nonce, ciphertext = _split_vault_file(self._read_raw())
            salt = base64.b64decode(kdf["salt"], validate=True)
            key = _derive_key(passphrase, salt, kdf["n"], kdf["r"], kdf["p"], kdf["dklen"])
            try:
                blob, migrated = _decrypt_blob(kdf, key, nonce, ciphertext)
            except VaultAuthError:
                return False
            if migrated:
                # Transparent v1→v2 migration: persist the upgraded blob
                # atomically under the same passphrase before rebuilding
                # the public cache. A crash leaves the old v1 file — the
                # next unlock simply migrates again.
                _atomic_write(self._blob_path,
                              _serialize_vault(kdf, key, blob))
            self._refresh_cache(blob)
        self._kdf = kdf
        self._key = key
        self._touch()
        return True

    @_serialized
    def change_passphrase(self, new_passphrase: str) -> None:
        """Re-encrypt under a new passphrase with a fresh salt.

        Single-file atomic replace: a crash leaves the vault usable under
        either the old or the new passphrase, never under neither.
        """
        if not new_passphrase:
            raise VaultError("passphrase must not be empty")
        self._require_unlocked()
        with _FileLock(self._lock_path, exclusive=True):
            kdf, nonce, ciphertext = _split_vault_file(self._read_raw())
            blob, _ = self._decrypt_current(kdf, nonce, ciphertext)
            new_kdf = _new_kdf()
            new_salt = base64.b64decode(new_kdf["salt"], validate=True)
            new_key = _derive_key(new_passphrase, new_salt,
                                  new_kdf["n"], new_kdf["r"], new_kdf["p"], new_kdf["dklen"])
            _atomic_write(self._blob_path, _serialize_vault(new_kdf, new_key, blob))
            # Commit key state immediately with the authoritative vault. A
            # disposable cache write failure must not leave us using the old key.
            self._kdf = new_kdf
            self._key = new_key
            self._touch()
            self._refresh_cache(blob)
        self._kdf = new_kdf
        self._key = new_key
        self._touch()

    # ── mutations (unlock required) ────────────────────────────────────

    @_serialized
    def _mutate(self, apply) -> None:
        """Run apply(blob) under the exclusive lock, then persist atomically.

        The blob is re-read from disk inside the lock, so a concurrent
        process's committed writes are never lost. If the vault was
        re-encrypted under a different passphrase in the meantime, our key
        fails authentication and the operation fails closed.
        """
        self._require_unlocked()
        with _FileLock(self._lock_path, exclusive=True):
            kdf, nonce, ciphertext = _split_vault_file(self._read_raw())
            blob, _ = self._decrypt_current(kdf, nonce, ciphertext)
            apply(blob)
            _atomic_write(self._blob_path, _serialize_vault(kdf, self._key, blob))
            self._refresh_cache(blob)

    def add_entry(self, description, origins, username=None, password=None,
                  notes="", *, kind="login", secret=None) -> str:
        """Add an entry. kind="login" keeps the classic shape (username +
        password + required origins); kind="secret" stores one arbitrary
        hidden value (API key, token, private key…) with optional origins.
        The positional signature stays compatible with phase-1 callers.
        """
        description = _clean_description(description)
        notes = str(notes)
        if kind == "login":
            if username is None or password is None:
                raise VaultError("login entries need both username and password")
            canon = _clean_origins(origins)
            username = str(username)
            password = str(password)
            secret_value = None
        elif kind == "secret":
            if secret is None or not str(secret):
                raise VaultError("secret entries need a non-empty secret value")
            canon = _clean_origins(origins) if origins else []
            secret_value = str(secret)
            username = password = None
        else:
            raise VaultError(f"unknown entry kind: {kind!r}")
        now = utc_now()
        result = {}

        def apply(blob):
            entries = blob["entries"]
            eid = "cred_" + secrets.token_hex(6)
            while eid in entries:
                eid = "cred_" + secrets.token_hex(6)
            entry = {
                "kind": kind,
                "description": description, "origins": canon,
                "notes": notes, "created": now, "updated": now,
            }
            if kind == "login":
                entry["username"] = username
                entry["password"] = password
            else:
                entry["secret"] = secret_value
            entries[eid] = entry
            result["id"] = eid

        self._mutate(apply)
        return result["id"]

    def update_entry(self, entry_id, *, description=KEEP, origins=KEEP,
                     username=KEEP, password=KEEP, notes=KEEP,
                     secret=KEEP) -> bool:
        """Update fields of one entry. Kind is immutable: the login and
        secret field sets differ, so changing shape means delete + re-add.
        """
        found = {}

        def apply(blob):
            entry = blob["entries"].get(entry_id)
            if entry is None:
                return
            kind = entry["kind"]
            # Validate everything before touching the entry: an exception
            # here aborts _mutate before any write, so a rejected update
            # can never leave a half-changed vault.
            new = {}
            if description is not KEEP:
                new["description"] = _clean_description(description)
            if origins is not KEEP:
                if origins:
                    new["origins"] = _clean_origins(origins)
                elif kind == "secret":
                    new["origins"] = []
                else:
                    raise VaultError("login entries need at least one origin")
            if username is not KEEP:
                if kind != "login":
                    raise VaultError("only login entries have a username")
                new["username"] = str(username)
            if password is not KEEP:
                if kind != "login":
                    raise VaultError("only login entries have a password")
                new["password"] = str(password)
            if secret is not KEEP:
                if kind != "secret":
                    raise VaultError("only secret entries have a secret value")
                if not str(secret):
                    raise VaultError("secret value must not be empty")
                new["secret"] = str(secret)
            if notes is not KEEP:
                new["notes"] = str(notes)
            entry.update(new)
            entry["updated"] = utc_now()
            found["ok"] = True

        self._mutate(apply)
        return bool(found)

    def delete_entry(self, entry_id) -> bool:
        found = {}

        def apply(blob):
            if blob["entries"].pop(entry_id, None) is not None:
                found["ok"] = True

        self._mutate(apply)
        return bool(found)

    # ── reads ───────────────────────────────────────────────────────────

    @_serialized
    def list_entries(self) -> list:
        """Public metadata only: id, description, origins, timestamps.

        Works while locked (reads the disposable cache). Never contains
        usernames, passwords, or notes.
        """
        # Check before touching the lock file: on a machine with no vault
        # the directory itself does not exist, and os.open(O_CREAT) cannot
        # create parents. Missing vault must surface as VaultNotInitialized,
        # never as FileNotFoundError.
        if not os.path.isfile(self._blob_path):
            raise VaultNotInitialized("no vault exists yet")
        with _FileLock(self._lock_path, exclusive=False):
            if not os.path.isfile(self._blob_path):
                raise VaultNotInitialized("no vault exists yet")
            if self.is_unlocked():
                kdf, nonce, ciphertext = _split_vault_file(self._read_raw())
                blob, _ = self._decrypt_current(kdf, nonce, ciphertext)
                # Read the source of truth without extending the unlock timer:
                # automatic UI refreshes are not user activity.
                return [_public_view(eid, entry)
                        for eid, entry in blob["entries"].items()]
            if self.cache_warning:
                raise VaultError("metadata cache unavailable; unlock to read entries")
            try:
                with open(self._meta_path, "rb") as fh:
                    raw = fh.read()
            except FileNotFoundError:
                raise VaultError("metadata cache is missing; unlock the vault to rebuild it")
        try:
            cache = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise VaultError("metadata cache is not valid JSON")
        return [dict(entry) for entry in _validate_cache(cache)["entries"]]

    @_serialized
    def get_secret(self, entry_id) -> dict:
        """Return username/password/notes for one entry.

        For a future trusted broker process only. Never wire this to a
        model-facing tool and never print the result.
        """
        self._require_unlocked()
        with _FileLock(self._lock_path, exclusive=False):
            kdf, nonce, ciphertext = _split_vault_file(self._read_raw())
            blob, _ = self._decrypt_current(kdf, nonce, ciphertext)
        entry = blob["entries"].get(entry_id)
        if entry is None:
            raise VaultError("no such entry")
        if entry["kind"] == "login":
            return {"kind": "login", "username": entry["username"],
                    "password": entry["password"], "notes": entry["notes"]}
        return {"kind": "secret", "secret": entry["secret"],
                "notes": entry["notes"]}
