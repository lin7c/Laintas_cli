"""Official Laintas messages, delivered into the L> inbox.

The CLI already has a message list with a read state that survives restarts
(``startup_mail``), so an announcement published on laintas.com does not need a
second inbox here — it needs to become one of those items. That is all this
module does: fetch what the account can see, and post each message under a
stable key with the server's content digest.

The digest is the part that matters. ``startup_mail`` records read receipts
against it, so a message re-posted on every start stays read, while one whose
text was edited on the site comes back unread — which is exactly what
"we changed the announcement" should mean.

Everything here is best-effort and off the critical path. A missing network, a
logged-out session, or a self-hosted backend simply means no official messages
this session; the last successful fetch is cached so a flight-mode start still
shows what was already published.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import startup_mail

import paths

#: Where the last successful fetch is kept, so an offline start is not silent.
CACHE_FILE = paths.account_path("LAINTAS_HOME") / "messages.json"

#: Session cookie names, newest first. Exported because `verify_session` in the
#: CLI reads the same credential — two copies of this list would drift, and the
#: symptom would be a silently signed-out feature rather than an error.
SESSION_COOKIE_NAMES = (
    "__Secure-laintas-v2.session_token",
    "laintas-v2.session_token",
    "__Secure-better-auth.session_token",
    "better-auth.session_token",
)

#: Site level → the four styles `startup_mail` knows.
_LEVELS = {"info": "info", "notice": "warn", "critical": "alert"}

#: An announcement is a paragraph, not a document. Anything longer is the site's
#: job; the inbox shows enough to decide whether to open the link.
_BODY_MAX = 1200

_TIMEOUT = 5

#: Deletions the site has not confirmed yet (offline, signed out, a 5xx).
#: Retried on the next sync, and kept out of the list until then, so a
#: message deleted here does not come back just because the network was down.
PENDING_DELETES_FILE = paths.account_path("LAINTAS_HOME") / "messages-deleted.json"

#: Only ever posted from one thread, but the cache write is not, so guard it.
_LOCK = threading.RLock()

#: What the last sync used, so a delete from the inbox can reach the same site.
_remote: dict = {"session": None, "base_url": ""}
#: L> key (``official:<slug>``) → server id (``o:<n>`` / ``s:<n>``).
_ids: dict[str, str] = {}

KEY_PREFIX = "official:"


def auth_args(session: dict) -> dict | None:
    """Request kwargs carrying this session's credential, or None if signed out."""
    cookies = (session or {}).get("cookies") or {}
    for name in SESSION_COOKIE_NAMES:
        token = cookies.get(name, "")
        if token:
            return {"cookies": {name: token}}
    headers = (session or {}).get("headers") or {}
    if headers.get("Authorization"):
        return {"headers": {"Authorization": headers["Authorization"]}}
    return None


def _load_cache() -> list[dict]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_cache(messages: list[dict]) -> None:
    with _LOCK:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(messages, handle, ensure_ascii=False)
            os.replace(tmp, CACHE_FILE)
        except Exception:
            pass


def _load_pending() -> list[str]:
    try:
        with open(PENDING_DELETES_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [str(i) for i in data if isinstance(i, str)] if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending(ids: list[str]) -> None:
    with _LOCK:
        try:
            if not ids:
                PENDING_DELETES_FILE.unlink(missing_ok=True)
                return
            PENDING_DELETES_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = PENDING_DELETES_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(sorted(set(ids)), handle)
            os.replace(tmp, PENDING_DELETES_FILE)
        except Exception:
            pass


def _delete_on_site(session: dict, base_url: str, message_id: str) -> bool:
    """True once the site has the delete (or the message is already gone)."""
    args = auth_args(session)
    if not args or not base_url:
        return False
    try:
        import requests
        response = requests.post(
            f"{base_url.rstrip('/')}/api/messages/delete",
            json={"id": message_id},
            timeout=_TIMEOUT, allow_redirects=False, **args,
        )
        # 200 with deleted=0 means "nothing of yours by that id" — done either way.
        return response.status_code == 200
    except Exception:
        return False


def _flush_pending(session: dict, base_url: str) -> list[str]:
    """Retry unconfirmed deletes; returns the ones still pending."""
    with _LOCK:
        pending = _load_pending()
        if not pending:
            return []
        left = [i for i in pending if not _delete_on_site(session, base_url, i)]
        _save_pending(left)
        return left


def delete(keys) -> int:
    """Delete these L> items from the account's inbox on the site as well.

    Takes L> keys; anything that is not an official message is ignored, so a
    caller can pass whatever it just removed locally. The request runs in the
    background — the local list has already changed, and a failure only means
    the delete is retried on the next start.
    """
    ids = [_ids[k] for k in keys if isinstance(k, str) and k.startswith(KEY_PREFIX) and k in _ids]
    if not ids:
        return 0
    with _LOCK:
        _save_pending(_load_pending() + ids)
    session, base_url = _remote["session"], _remote["base_url"]
    if session and base_url:
        threading.Thread(target=_flush_pending, args=(session, base_url),
                         daemon=True, name="official-messages-delete").start()
    return len(ids)


def fetch(session: dict, base_url: str, *, lang: str = "zh") -> list[dict] | None:
    """The account's live messages, or None when the site could not be asked."""
    args = auth_args(session)
    if not args:
        return None
    try:
        import requests  # imported here so the module is importable without it
        response = requests.get(
            f"{base_url.rstrip('/')}/api/messages",
            params={"lang": "en" if lang == "en" else "zh"},
            timeout=_TIMEOUT, allow_redirects=False, **args,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:
        return None
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return [m for m in messages if isinstance(m, dict) and m.get("slug")] if messages else []


def post(messages: list[dict]) -> int:
    """Put each message in the L> list. Returns how many were posted.

    Keyed by slug so a message re-posted on the next start updates in place,
    and digested by the server's own fingerprint so an edited message becomes
    unread again while an unchanged one stays read.
    """
    posted = 0
    pending = set(_load_pending())
    for message in messages:
        if str(message.get("id") or "") in pending:
            continue
        slug = str(message.get("slug") or "").strip()
        title = str(message.get("title") or "").strip()
        if not slug or not title:
            continue
        body = str(message.get("body") or "").strip()[:_BODY_MAX]
        action = str(message.get("actionUrl") or "").strip()
        digest = str(message.get("digest") or "")
        message_id = str(message.get("id") or "")
        if message_id:
            _ids[f"{KEY_PREFIX}{slug}"] = message_id
        startup_mail.post(
            f"official:{slug}", title, body,
            action=f"laintas.com{action}" if action.startswith("/") else action,
            level=_LEVELS.get(str(message.get("level")), "info"),
            # Fall back to hashing the rendered text only if the site did not
            # send a digest; never leave it empty, or the receipt cannot match.
            digest=f"official:{slug}:{digest}" if digest else "",
        )
        posted += 1
    return posted


def sync(session: dict, base_url: str, *, lang: str = "zh") -> int:
    """Fetch and post, falling back to the last successful fetch when offline."""
    _remote.update(session=session, base_url=base_url)
    # Deletes first, so the list fetched next already reflects them.
    _flush_pending(session, base_url)
    messages = fetch(session, base_url, lang=lang)
    if messages is None:
        return post(_load_cache())
    _save_cache(messages)
    return post(messages)
