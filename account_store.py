"""Account profiles; independent of paths so selection precedes CLI imports.

Credentials live in individual profiles. Legacy task data is never attributed
by observing the account signed in today: adoption is an explicit operation.
"""
import copy
import functools
import hashlib
import os
from pathlib import Path
import re
import time
import threading
import uuid

import file_lock
import json_store

_runtime_lock = threading.RLock()
_transitioning = False


def freeze_admissions(check_idle):
    global _transitioning
    with _runtime_lock:
        if not check_idle():
            raise AccountError("An Agent is running; account switch cancelled")
        _transitioning = True


def cancel_transition():
    global _transitioning
    with _runtime_lock:
        _transitioning = False


def admission(rejected):
    """Serialize admission with account switching, before task state changes."""
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with _runtime_lock:
                if _transitioning:
                    return rejected
                return function(*args, **kwargs)
        return wrapped
    return decorate


class AccountError(ValueError):
    pass


def profile_dir(root, user_id):
    uid = str(user_id or "").strip()
    if not uid:
        raise AccountError("An account needs a verified userId")
    return Path(root) / "accounts" / hashlib.sha256(uid.encode()).hexdigest()


def profiles(root):
    out = []
    for path in (Path(root) / "accounts").glob("*/profile.json"):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        item = json_store.load_json(path, {})
        if (isinstance(item, dict) and isinstance(item.get("userId"), str) and item["userId"]
                and profile_dir(root, item["userId"]) == path.parent):
            out.append(item)
    return sorted(out, key=lambda p: (str(p.get("alias") or p.get("email") or ""), p["userId"]))


def resolve(root, selector):
    value = str(selector or "").strip()
    rows = profiles(root)
    exact = [p for p in rows if p["userId"] == value]
    matches = exact or [p for p in rows if value in (p.get("alias"), p.get("email"))]
    if len(matches) != 1:
        raise AccountError(f"{'Ambiguous' if matches else 'Unknown'} account {value!r}; use /account list")
    return matches[0]


def remember(root, session, alias=None):
    uid = str((session or {}).get("userId") or "").strip()
    directory = profile_dir(root, uid)
    if alias is not None and not re.fullmatch(r"[\w.-]{1,64}", str(alias)):
        raise AccountError("Account aliases use 1-64 letters, digits, '.', '-' or '_'")
    with file_lock.guard(Path(root) / "accounts" / ".profiles.lock"):
        if alias is not None:
            for row in profiles(root):
                if row["userId"] != uid and (row.get("alias") == alias or row["userId"] == alias):
                    raise AccountError(f"Account alias {alias!r} is already in use")
        old = json_store.load_json(directory / "profile.json", {})
        if not isinstance(old, dict):
            old = {}
        row = {"userId": uid, "name": str(session.get("userName") or ""),
               "email": str(session.get("userEmail") or ""),
               "alias": str(alias if alias is not None else old.get("alias") or ""),
               "updated_at": time.time()}
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        json_store.save_json_atomic(directory / "profile.json", row, mode=0o600, account_independent=True)
        json_store.save_json_atomic(directory / "session.json", session, mode=0o600, account_independent=True)
    return row


def select(root, terminal_id, user_id):
    # Per-terminal selection; default is only a seed for a new terminal.
    resolve(root, user_id)
    terminal_key = hashlib.sha256(str(terminal_id).encode()).hexdigest()
    with file_lock.guard(Path(root) / "accounts" / ".profiles.lock"):
        destinations = [Path(root) / "accounts" / "terminals" / f"{terminal_key}.json",
                        Path(root) / "accounts" / "default.json"]
        previous = {}
        for path in destinations:
            try:
                previous[path] = path.read_bytes()
            except FileNotFoundError:
                previous[path] = None
        receipt = {"root": Path(root), "selection_id": uuid.uuid4().hex, "previous": previous}
        value = {"userId": user_id, "selectionId": receipt["selection_id"]}
        try:
            for path in destinations:
                json_store.save_json_atomic(path, value, mode=0o600, account_independent=True)
        except BaseException:
            _rollback_selection_locked(receipt)
            raise
        return receipt


def _rollback_selection_locked(receipt):
    for path, old in receipt["previous"].items():
        current = json_store.load_json(path, {})
        if not isinstance(current, dict) or current.get("selectionId") != receipt["selection_id"]:
            continue  # A different CLI has advanced this selection; preserve it.
        if old is None:
            path.unlink(missing_ok=True)
            continue
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
        try:
            with temporary.open("wb") as handle:
                handle.write(old)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def rollback_selection(receipt):
    with file_lock.guard(receipt["root"] / "accounts" / ".profiles.lock"):
        _rollback_selection_locked(receipt)


def launch_account(root, terminal_id, argv):
    explicit = None
    for index, arg in enumerate(argv):
        if arg == "--account":
            if index + 1 >= len(argv):
                raise AccountError("--account needs an account alias, email or userId")
            explicit = argv[index + 1]
        elif arg.startswith("--account="):
            explicit = arg.split("=", 1)[1]
    inherited = os.environ.get("LAINTAS_ACCOUNT_ID", "").strip()
    if explicit is not None or inherited:
        return resolve(root, explicit if explicit is not None else inherited)["userId"]
    key = hashlib.sha256(str(terminal_id).encode()).hexdigest()
    for path in (Path(root) / "accounts" / "terminals" / f"{key}.json",
                 Path(root) / "accounts" / "default.json"):
        data = json_store.load_json(path, {})
        if isinstance(data, dict) and data.get("userId"):
            return resolve(root, data["userId"])["userId"]
    # Migrate credentials only, once. Existing tasks remain unassigned.
    legacy_path = Path(root) / "session.json"
    legacy = {} if legacy_path.is_symlink() else json_store.load_json(legacy_path, {})
    if isinstance(legacy, dict) and legacy.get("userId"):
        directory = profile_dir(root, legacy["userId"])
        if not (directory / "profile.json").exists():
            remember(root, legacy)
        return legacy["userId"]
    return ""


def owns(payload, user_id):
    owner = str((payload or {}).get("owner_user_id") or "")
    return owner == str(user_id or "")


def stamp(state, user_id):
    import paths
    paths.require_account_selected()
    uid = str(user_id or "")
    existing = str(state.get("owner_user_id") or "")
    if existing and existing != uid:
        raise AccountError("Task belongs to another account; accept a handoff to continue it")
    if not uid:
        return state  # Preserve legacy/external library state without relabelling.
    state["owner_user_id"] = uid
    state.setdefault("created_by", uid)
    return state


def frozen_auth(session, user_id=None):
    result = copy.deepcopy(session or {})
    if user_id is not None and str(result.get("userId") or "") != str(user_id):
        raise AccountError("Authentication does not match the task's execution account")
    return result
