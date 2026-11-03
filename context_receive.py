"""Opening a context handed over through the Laintas inbox.

A ``context_handed`` inbox message carries no content itself — its
action link names a travel envelope (Context Kernel SPEC §14) in
whichever product sent it. Reading the message in the CLI offers to
import that envelope as a session here: the brokered read signs the
owner's area (or, for an Issues hand, the exporter's area under the
recorded grant), the items become this CLI's wire history — foreign
system items demoted to reference notes, the whole record fitted by
the one kernel — and the session lands through the same save/activate
machinery a received task handoff uses. Only the context travels;
the environment is this machine's own.
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import parse_qs, quote, urlparse

import account_store
import context_kernel as kernel
import official_messages
import paths

# What an inbox notice can hand this CLI: every origin the gateway's
# broker serves. A "handoff" notice names a /handoff checkpoint, which
# the CLI takes with /handoff receive, not through the broker.
ORIGINS = ("helpwo", "laintas-ai", "laintas-cli", "issues")
_IMPORTED_NOTE = "[imported context]\n"
_ORIGIN_LABELS = {"helpwo": "Helpwow", "laintas-ai": "Laintas AI",
                  "laintas-cli": "Laintas CLI",
                  "issues": "Laintas Issues", "handoff": "Laintas CLI"}
_FIT_CHARS = 400_000
_TIMEOUT = 12


def parse_context_ref(action: str):
    """(origin, envelope_id, from_user_id) from a message action URL.

    The link is the Helpwow (or Laintas AI) open URL; its ``context``
    query parameter is the reference, ``cfrom`` names the exporter on
    a cross-account Issues hand. Returns None for any other action.
    """
    if not action:
        return None
    try:
        query = parse_qs(urlparse(action).query)
    except Exception:
        return None
    raw = (query.get("context") or [""])[0]
    if ":" not in raw:
        return None
    origin, env_id = raw.split(":", 1)
    if origin not in ORIGINS or not env_id:
        return None
    return origin, env_id, (query.get("cfrom") or [""])[0]


def announce_hand(session: dict, base_url: str, origin: str, env_id: str,
                  target: str, title: str = "") -> bool:
    """Tell the account inbox about an envelope this CLI just stored
    (a handoff sync). Best effort: a sync never fails on its notice."""
    args = official_messages.auth_args(session)
    if not args:
        return False
    try:
        import requests
        response = requests.post(
            f"{base_url.rstrip('/')}/api/context/hands",
            json={"origin": origin, "id": env_id, "target": target,
                  "title": title},
            timeout=_TIMEOUT, allow_redirects=False, **args)
        return response.status_code == 200
    except Exception:
        return False


class SendError(RuntimeError):
    """The gateway refused or could not take the conversation; the
    message is the one to show."""


def context_items(history: list) -> list:
    """This session's chat_history as the items the gateway seals:
    wire form as the model was sent it, with the CLI's own roles
    translated. Knowledge items (a compaction summary, rules) travel as
    system notes, which every opener demotes to reference; the note an
    import left behind stays with the session it describes."""
    items = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "knowledge":
            text = kernel._content_text(message.get("content"))
            if not text or text.startswith(_IMPORTED_NOTE):
                continue
            message = {"role": "system", "content": message.get("content")}
        # The model tag and the provider's opaque reasoning state mean
        # something only to the model that wrote them.
        items.append({k: v for k, v in message.items()
                      if k not in ("_reasoning_model", "reasoning_details")})
    return items


def send_context(session: dict, base_url: str, target: str,
                 history: list) -> str:
    """Hand this session's conversation to another product (§14).

    The gateway is the one sealer: it normalizes, validates, scrubs and
    names the envelope by content, stores it in the shared store and
    drops the hand in the inbox. Returns the envelope id; raises
    SendError with a message to show.
    """
    args = official_messages.auth_args(session)
    if not args:
        raise SendError("Sign in to hand a conversation over.")
    try:
        import requests
        response = requests.post(
            f"{base_url.rstrip('/')}/api/context/envelopes",
            json={"origin": "laintas-cli", "target": target,
                  "items": context_items(history)},
            timeout=_TIMEOUT + 18, allow_redirects=False, **args)
    except Exception as exc:
        raise SendError("The hand-over service is unreachable right now.") from exc
    if response.status_code == 200:
        env_id = (response.json() or {}).get("id")
        if env_id:
            return str(env_id)
    reason = {413: "This conversation is too large to hand over, or your storage is full.",
              400: "This conversation cannot be handed over.",
              401: "Sign in to hand a conversation over."}
    try:
        detail = (response.json() or {}).get("detail") or ""
    except Exception:
        detail = ""
    raise SendError(" ".join(filter(None, [
        reason.get(response.status_code, "The hand-over service is unavailable right now."),
        detail])))


def fetch_envelope(session: dict, base_url: str, origin: str, env_id: str,
                   from_user: str = "") -> dict | None:
    """The envelope behind a hand, through the gateway's brokered
    read, or None when it is not (or no longer) there."""
    args = official_messages.auth_args(session)
    if not args:
        return None
    try:
        import requests
        url = (f"{base_url.rstrip('/')}/api/context/envelopes/"
               f"{origin}/{env_id}/url")
        if from_user:
            url += f"?from={quote(from_user, safe='')}"
        response = requests.get(url, timeout=_TIMEOUT,
                                allow_redirects=False, **args)
        if response.status_code != 200:
            return None
        signed = (response.json() or {}).get("url")
        if not signed:
            return None
        got = requests.get(signed, timeout=_TIMEOUT + 8)
        if got.status_code != 200:
            return None
        envelope = got.json()
    except Exception:
        return None
    if (not isinstance(envelope, dict) or envelope.get("kernel") != 1
            or envelope.get("kind") != "conversation"
            or not isinstance(envelope.get("items"), list)
            or not envelope["items"]):
        return None
    return envelope


def wire_history(envelope: dict) -> list:
    """Canonical items -> this CLI's chat_history (wire form).

    Foreign system items are reference material, not instructions:
    they land as user messages wrapped in a carried-context note, the
    same demotion every other opener applies. The record is fitted by
    the kernel; when even the fitted view is over budget, the oldest
    part of the record goes (_fit_tail).

    Carried thinking was written by another model: it lands as
    ``reasoning_content`` with an empty model tag, so the request
    builder (agent_loop._project_reasoning) folds it into the text as
    a labelled block for every model instead of replaying it.
    """
    origin = str(envelope.get("origin") or "another Laintas product")
    fitted = _fit_tail(envelope["items"])
    history = []
    for item in fitted:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "system":
            text = kernel._content_text(item.get("content"))
            if text:
                history.append({
                    "role": "user",
                    "content": (f"<carried-context origin=\"{origin}\" "
                                f"kind=\"system\">\n{text}\n"
                                f"</carried-context>"),
                })
            continue
        try:
            message = kernel.to_wire_lossless(item)
        except Exception:
            continue
        if message.get("role") == "assistant":
            reasoning = message.pop("reasoning", None)
            if isinstance(reasoning, str) and reasoning.strip():
                message["reasoning_content"] = reasoning
                message["_reasoning_model"] = ""
        history.append(message)
    return history


def _fit_tail(items: list) -> list:
    """The record fitted to the budget. fit() never drops the first or
    last user item or the last groups; when even those are over, the
    oldest part goes: the earliest start that fits, found by halving,
    never on a tool item whose call was cut."""
    def attempt(start):
        while start < len(items) and start and isinstance(items[start], dict) \
                and items[start].get("role") == "tool":
            start += 1
        try:
            return kernel.fit(items[start:], _FIT_CHARS) if start < len(items) else None
        except Exception:
            return None
    best = attempt(0)
    if best:
        return best
    lo, hi = 1, len(items) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        fitted = attempt(mid)
        if fitted:
            best, hi = fitted, mid - 1
        else:
            lo = mid + 1
    return best or []


def session_history(envelope: dict, origin: str):
    """The chat_history an imported session starts from: the fitted
    record plus a note that the environment stayed behind. None when the
    record has no user words to continue from."""
    history = wire_history(envelope)
    if not any(m.get("role") == "user" for m in history):
        return None
    label = _ORIGIN_LABELS.get(origin, origin)
    history.append({
        "role": "knowledge",
        "content": (_IMPORTED_NOTE
                    + f"This conversation was handed over from {label} through "
                    "the Laintas inbox and imported here. The environment "
                    "did not travel with it: files, shell and browser state "
                    "belong to the product it came from. Verify anything "
                    "here before relying on it."),
    })
    return history


def land(session: dict, base_url: str, ref, cwd: str) -> dict:
    """Import the referenced envelope as a session for this account.

    Returns {ok, activated, title, session_id} or {ok: False, reason}
    with reason one of: not_found, empty, conflict, unavailable.
    Landing on the same hand twice resolves to the same session.
    """
    origin, env_id, from_user = ref
    envelope = fetch_envelope(session, base_url, origin, env_id, from_user)
    if envelope is None:
        return {"ok": False, "reason": "not_found"}
    history = session_history(envelope, origin)
    if history is None:
        return {"ok": False, "reason": "empty"}
    user_id = paths.ACCOUNT_USER_ID
    if not user_id:
        return {"ok": False, "reason": "unavailable"}
    sid = hashlib.sha256(
        f"{user_id}\0ctx:{origin}:{env_id}".encode()).hexdigest()[:32]
    # The title names the conversation itself: the first real user
    # words of the envelope — not the carried system note, which only
    # became a user message for lack of a better seat.
    title = ""
    for item in envelope["items"]:
        if isinstance(item, dict) and item.get("role") == "user":
            title = " ".join(
                kernel._content_text(item.get("content")).split())[:80]
            if title:
                break
    state = {"_session_id": sid}
    account_store.stamp(state, user_id)
    blob = {"id": sid, "session_id": sid, "kind": "autosave", "cwd": cwd,
            "agent_id": "primary", "owner_user_id": user_id,
            "timestamp": time.time(),
            "title": title or "Imported context",
            "state": state, "chat_history": history, "tasks": []}
    import account_tasks
    try:
        blob = account_tasks.save_received(blob)
    except account_store.AccountError:
        return {"ok": False, "reason": "conflict"}
    except Exception:
        return {"ok": False, "reason": "unavailable"}
    activated = False
    try:
        activated = bool(account_tasks.activate(blob))
    except Exception:
        activated = False
    return {"ok": True, "activated": activated,
            "title": blob["title"], "session_id": sid}
