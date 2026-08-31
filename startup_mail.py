"""The session's messages, kept behind the L> mark.

The startup banner used to spray four or five dim advisory lines above the
first prompt — the tips row, the training-data opt-in, the "previous session
in this directory" hint, the Helpwo link hint, the update notice. None of them
are urgent and all of them push the actual prompt down the screen, so they are
collected here instead and read on demand with Alt+0.

The item list is in-memory and session-scoped: these are notices *about this
session*, re-posted from scratch on every start. Items are keyed, so a notice
re-posted during the session updates in place rather than piling up.

What *does* outlive the process is the read state. Every start re-posts the
same standing advisories, so without a receipt the tips row and the training
opt-in come back unread forever and the mark never goes quiet — reading them
would mean nothing. A receipt records the key together with a digest of what
was read, so a notice whose *content* changed (a new version to update to, a
different resume checkpoint) is new information and shows up unread again.

Receipts are opt-in per process: ``enable_persistence()`` turns them on, and
the CLI calls it once at startup. A library import — or a test — that never
calls it stays purely in-memory and touches no file.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Rich markup for the CLI's compact logo mark — green "L", red ">".
LOGO_MARK = "[bold green]L[/bold green][bold red]>[/bold red]"

#: Plain-text form for places that cannot render Rich markup.
LOGO_MARK_PLAIN = "L>"

#: Level → Rich style for the title of a mail item.
LEVEL_STYLES = {
    "info": "muted",
    "good": "green",
    "warn": "yellow",
    "alert": "red",
}


@dataclass
class MailItem:
    """One advisory notice waiting behind the logo."""

    key: str
    title: str
    body: str = ""
    action: str = ""
    level: str = "info"
    ts: float = field(default_factory=time.time)
    read: bool = False
    #: Optional stable content identity for the cross-session read receipt;
    #: empty means "hash the rendered text".
    digest: str = ""


_items: list[MailItem] = []
#: Startup posts some notices from a background thread (the resume and
#: recovery advisories are computed off the critical path), while the prompt's
#: right-hand mark reads the count on every render.
_lock = threading.RLock()


#: Receipts are dropped once they are this old, so a key that stops being
#: posted (an extension that was uninstalled) does not linger forever.
_RECEIPT_MAX_AGE = 90 * 86400

#: Belt-and-braces bound in case a caller invents keys in a loop.
_RECEIPT_MAX_ITEMS = 500

#: ``None`` disables persistence entirely — the default, so importing this
#: module never touches the disk on its own.
_store_path: Path | None = None
_receipts: dict[str, dict] = {}


def _digest(title: str, body: str, action: str, level: str) -> str:
    """Content identity of a notice: what the reader would have seen."""
    raw = "\x00".join((title, body, action, level))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def enable_persistence(path=None) -> None:
    """Remember read state across restarts, in *path* (default ~/.laintas).

    Safe to call more than once and safe to call late: notices already posted
    are re-checked against the receipts that just loaded, so the order of
    startup work does not decide whether the mark goes quiet.
    """
    global _store_path
    if path is None:
        try:
            import paths
            path = paths.MESSAGES_READ_FILE
        except Exception:
            return
    with _lock:
        _store_path = Path(path)
        _load_receipts()
        for item in _items:
            if not item.read and _receipt_matches(item):
                item.read = True


def disable_persistence() -> None:
    """Go back to memory-only. Used by tests; leaves the file untouched."""
    global _store_path
    with _lock:
        _store_path = None
        _receipts.clear()


def _load_receipts() -> None:
    """Read the receipt file. Any problem with it means "nothing is read"."""
    _receipts.clear()
    if _store_path is None:
        return
    try:
        raw = json.loads(_store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    cutoff = time.time() - _RECEIPT_MAX_AGE
    for key, entry in (raw.get("read") or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            ts = float(entry.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        digest = entry.get("digest")
        if isinstance(key, str) and isinstance(digest, str):
            _receipts[key] = {"digest": digest, "ts": ts}


def _save_receipts() -> None:
    """Write the receipts out. Best effort: a mailbox is not worth a crash."""
    if _store_path is None:
        return
    if len(_receipts) > _RECEIPT_MAX_ITEMS:
        for key in sorted(_receipts, key=lambda k: _receipts[k]["ts"])[
                :len(_receipts) - _RECEIPT_MAX_ITEMS]:
            del _receipts[key]
    tmp = _store_path.with_name(_store_path.name + f".{os.getpid()}.tmp")
    try:
        _store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"version": 1, "read": _receipts}, indent=2),
            encoding="utf-8")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(_store_path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _receipt_matches(item: "MailItem") -> bool:
    """Whether *item* is the same notice a receipt was filed for."""
    entry = _receipts.get(item.key)
    return bool(entry) and entry.get("digest") == _item_digest(item)


def _item_digest(item: "MailItem") -> str:
    return item.digest or _digest(item.title, item.body, item.action, item.level)


def _file_receipt(item: "MailItem") -> None:
    """Record that *item* was read, and persist it."""
    if _store_path is None:
        return
    _receipts[item.key] = {"digest": _item_digest(item), "ts": time.time()}
    _save_receipts()


def post(key: str, title: str, body: str = "", action: str = "",
         level: str = "info", digest: str = "") -> MailItem:
    """Add or replace the notice stored under *key*.

    Replacing keeps the original position so the inbox order stays stable,
    but resets ``read`` — an updated notice is new information.

    A notice already read in an earlier session comes back read, unless its
    content changed. Pass *digest* when the rendered text carries something
    incidental — "10 hour(s) ago" is not a new message an hour later — and it
    becomes the identity used for that comparison instead of the text.
    """
    with _lock:
        key = str(key or "").strip() or f"item-{len(_items) + 1}"
        item = MailItem(key=key, title=str(title or ""), body=str(body or ""),
                        action=str(action or ""),
                        level=level if level in LEVEL_STYLES else "info",
                        digest=str(digest or ""))
        item.read = _receipt_matches(item)
        for index, existing in enumerate(_items):
            if existing.key == key:
                _items[index] = item
                return item
        _items.append(item)
        return item


def items() -> list[MailItem]:
    """All notices, oldest first."""
    with _lock:
        return list(_items)


def unread() -> list[MailItem]:
    with _lock:
        return [item for item in _items if not item.read]


def unread_count() -> int:
    with _lock:
        return sum(1 for item in _items if not item.read)


def count() -> int:
    with _lock:
        return len(_items)


def mark_all_read() -> int:
    """Mark everything read; returns how many items changed."""
    changed = 0
    with _lock:
        for item in _items:
            if not item.read:
                item.read = True
                changed += 1
        if _store_path is not None:
            for item in _items:
                _receipts[item.key] = {"digest": _item_digest(item),
                                       "ts": time.time()}
            _save_receipts()
    return changed


def get(key: str) -> MailItem | None:
    """One notice by key, or None."""
    with _lock:
        for item in _items:
            if item.key == key:
                return item
    return None


def index_of(key: str) -> int:
    """Position of *key* in the list, or -1."""
    with _lock:
        for position, item in enumerate(_items):
            if item.key == key:
                return position
    return -1


def mark_read(key: str) -> bool:
    """Mark one notice read. True when it existed and changed."""
    with _lock:
        for item in _items:
            if item.key == key:
                already = item.read
                item.read = True
                _file_receipt(item)
                return not already
    return False


def delete(key: str, *, remember: bool = True) -> bool:
    """Remove one notice. True when it existed.

    Dismissing is a stronger statement than reading, so it files the same
    receipt: the notice must not come back unread on the next start. Pass
    ``remember=False`` to drop an item without recording anything.
    """
    with _lock:
        for position, item in enumerate(_items):
            if item.key == key:
                if remember:
                    _file_receipt(item)
                del _items[position]
                return True
    return False


def clear(*, remember: bool = False) -> None:
    """Empty the list. ``remember=True`` files a receipt for each item first."""
    with _lock:
        if remember and _store_path is not None:
            now = time.time()
            for item in _items:
                _receipts[item.key] = {"digest": _item_digest(item), "ts": now}
            _save_receipts()
        _items.clear()


def has_unread() -> bool:
    """Whether the prompt has anything to advertise."""
    return unread_count() > 0


def mark_text(unread: int | None = None) -> str:
    """Plain text of the status mark: "L> 6".

    The count is unread, never the total: the mark exists to say something
    arrived, and a permanent badge over messages already read is noise on
    every line of the session. The prompt drops the mark entirely once the
    count reaches zero — /messages and Alt+0 are still the way back in.

    Plain because the right prompt measures this string to fit the row; the
    two colours are applied by the renderer, which splits it back apart.
    """
    if unread is None:
        unread = unread_count()
    return f"{LOGO_MARK_PLAIN} {unread}" if unread else LOGO_MARK_PLAIN
