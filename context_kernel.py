"""Context Kernel v1 — reference implementation.

Implements the contract in agent_gateway/CONTEXT_KERNEL_SPEC.md:

- §1 canonical items: normalize_item / validate_items / to_wire
- §2 the fit function: fit
- §5 the scrub contract: scrub
- §3 summary items: summary_item
- §4 envelopes: envelope

Stdlib only. The record layer is never mutated by this module: every
function that transforms items works on deep copies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

KERNEL_VERSION = 1

_ROLES = ("system", "user", "assistant", "tool")

ELISION_NOTE = "Older result elided; full output is on the record."
_TOOL_ELIDE_THRESHOLD = 1200   # elide tool bodies longer than this (§2 step 2)
_TOOL_KEEP = 600               # head/tail characters kept when eliding
_TAIL_GROUPS = 4               # trailing groups exempt from elision/dropping


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dump(value) -> str:
    """Compact JSON dump — the size currency of §2."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonical_json(value) -> str:
    """Canonical JSON (sorted keys) — the hash currency of §4."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _size(items) -> int:
    return len(_dump(items))


def _part_text(part) -> str:
    if isinstance(part, dict):
        if part.get("type") == "elision":
            return part.get("note") or ""
        return part.get("text") or ""
    return ""


def _content_text(content) -> str:
    """Total text of a content value (string or Part list)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_part_text(p) for p in content)
    return ""


# ---------------------------------------------------------------------------
# §1 canonical items
# ---------------------------------------------------------------------------

def normalize_item(item) -> dict:
    """Normalize one item to canonical form; returns a deep copy.

    A string content becomes a single text Part. Enforces the §1
    invariants that are checkable on one item: a tool item must carry
    tool_call_id; tool_calls may appear only on assistant items.
    """
    if not isinstance(item, dict):
        raise ValueError("item must be a dict")
    out = copy.deepcopy(item)
    role = out.get("role")
    if role not in _ROLES:
        raise ValueError(f"bad role: {role!r}")
    if role == "tool" and not out.get("tool_call_id"):
        raise ValueError("tool item requires tool_call_id")
    if "tool_calls" in out and role != "assistant":
        raise ValueError("tool_calls only allowed on assistant items")
    content = out.get("content")
    if isinstance(content, str):
        out["content"] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or "type" not in part:
                raise ValueError("content parts must be dicts with a type")
    elif content is not None:
        raise ValueError("content must be a string or a list of parts")
    return out


def validate_items(items) -> None:
    """Enforce the cross-item §1 invariant: every tool item's
    tool_call_id must match a tool call in an *earlier* assistant item."""
    seen = set()
    for item in items:
        role = item.get("role")
        if role == "assistant":
            for call in item.get("tool_calls") or []:
                cid = call.get("id")
                if cid:
                    seen.add(cid)
        elif role == "tool":
            cid = item.get("tool_call_id")
            if not cid:
                raise ValueError("tool item requires tool_call_id")
            if cid not in seen:
                raise ValueError(
                    f"tool item references unknown tool_call_id {cid!r}")


def to_wire(item) -> dict:
    """Serialize one canonical item to OpenAI wire form.

    - content parts flatten to a plain string when all parts are text,
      otherwise a parts list (text parts as {"type": "text", "text"});
    - tool_calls become {"id", "type": "function", "function":
      {"name", "arguments": <JSON string of the input object>}};
    - a tool item becomes {"role": "tool", "tool_call_id", "content"};
    - reasoning never goes on the wire.
    """
    norm = normalize_item(item)
    role = norm["role"]
    parts = norm.get("content")
    if role == "tool":
        return {"role": "tool",
                "tool_call_id": norm["tool_call_id"],
                "content": _content_text(parts)}
    out = {"role": role}
    if parts is not None:
        if all(p.get("type") == "text" for p in parts):
            out["content"] = "".join(p.get("text") or "" for p in parts)
        else:
            out["content"] = [
                {"type": "text", "text": p.get("text") or ""}
                if p.get("type") == "text" else copy.deepcopy(p)
                for p in parts
            ]
    if role == "assistant" and norm.get("tool_calls"):
        wire_calls = []
        for call in norm["tool_calls"]:
            inp = call.get("input")
            arguments = inp if isinstance(inp, str) else json.dumps(
                inp if inp is not None else {}, ensure_ascii=False)
            wire_call = {
                "id": call.get("id"),
                "type": "function",
                "function": {"name": call.get("name"),
                             "arguments": arguments},
            }
            # Provider extras a canonical call carries (e.g. extra_content)
            # ride through to the wire unchanged.
            for k, v in call.items():
                if k not in ("id", "name", "input"):
                    wire_call[k] = copy.deepcopy(v)
            wire_calls.append(wire_call)
        out["tool_calls"] = wire_calls
    return out


def to_wire_lossless(item):
    """Wire form that touches ONLY the tool_calls of an item.

    Unlike to_wire (which rebuilds the item for the model boundary and
    drops local fields), this preserves every other key and the content
    exactly as stored — the right conversion when a canonical snapshot
    goes back to a runtime that keeps its own fields on messages
    (e.g. the CLI's input_kind). Items without canonical tool calls are
    returned as an unchanged deep copy."""
    if not isinstance(item, dict):
        return item
    out = copy.deepcopy(item)
    calls = []
    for c in out.get("tool_calls") or []:
        if not isinstance(c, dict) or "function" in c:
            calls.append(c)  # already wire-shaped
            continue
        inp = c.get("input")
        arguments = inp if isinstance(inp, str) else json.dumps(
            inp if inp is not None else {}, ensure_ascii=False)
        wire_call = {"id": c.get("id"), "type": "function",
                     "function": {"name": c.get("name"), "arguments": arguments}}
        for k, v in c.items():
            if k not in ("id", "name", "input"):
                wire_call[k] = v
        calls.append(wire_call)
    if calls:
        out["tool_calls"] = calls
    return out


def input_from_arguments(raw):
    """The canonical input object for a wire call's arguments. Nothing is
    thrown away: arguments that are not valid JSON (a truncated stream)
    become {"raw": <text>}, a JSON value that is not an object becomes
    {"value": <it>}."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"raw": raw}
    else:
        parsed = raw
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def from_wire(item):
    """Canonical form of an OpenAI wire item (the inverse of to_wire).

    Lenient by design — snapshots in the wild carry local roles and extra
    fields: anything unrecognized passes through untouched, non-dict items
    pass through as-is, and only tool_calls are restructured (the wire's
    arguments JSON string becomes the canonical input object). Extra keys
    on a wire call (e.g. extra_content) are preserved on the canonical
    call. The arguments' exact bytes are not: canonical form is for
    crossing products, and a runtime that resends its own history (prompt
    cache prefixes) keeps the wire form instead."""
    if not isinstance(item, dict):
        return item
    out = copy.deepcopy(item)
    calls = []
    for c in out.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        if "input" in c and "function" not in c:
            calls.append(c)  # already canonical
            continue
        fn = c.get("function") or {}
        call = {"id": c.get("id") or "",
                "name": fn.get("name") or c.get("name") or "",
                "input": input_from_arguments(fn.get("arguments"))}
        for k, v in c.items():
            if k not in ("id", "type", "function", "name", "input"):
                call[k] = v
        calls.append(call)
    if calls:
        out["tool_calls"] = calls
    return out


# ---------------------------------------------------------------------------
# §2 fit
# ---------------------------------------------------------------------------

def _groups(items):
    """Group indices: a group is an item plus any immediately following
    tool items (a call and its results are never split)."""
    groups = []
    for i, m in enumerate(items):
        if m.get("role") == "tool" and groups:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def fit(items, limit_chars, *, keep_prefix=0):
    """Fit an item list into limit_chars (compact-JSON characters).

    Returns a fitted deep copy, or None when even the never-drop set
    exceeds the limit. Never mutates the input. Lenient about shape:
    wire-shaped items (OpenAI tool_calls, plain-string tool content,
    reasoning_content keys) are fitted without prior normalization.
    Steps, in the fixed §2 order:
      1. already fits -> return as-is;
      2. elide old tool bodies (>1200 chars -> head 600 + elision Part
         + tail 600) and strip reasoning/reasoning_content from old
         assistant items — "old" = all but the last 4 groups;
      3. drop whole middle groups until it fits, never dropping the
         first keep_prefix items, the first/last user items, system
         items, or the last 4 groups;
      4. still over -> None.
    """
    out = copy.deepcopy(list(items))
    if _size(out) <= limit_chars:
        return out

    # Step 2 — elision + reasoning strip over the old groups.
    old = set()
    for group in _groups(out)[:-_TAIL_GROUPS]:
        old.update(group)
    for i in old:
        m = out[i]
        if m.get("role") == "assistant":
            m.pop("reasoning", None)
            m.pop("reasoning_content", None)
        elif m.get("role") == "tool":
            text = _content_text(m.get("content"))
            if len(text) > _TOOL_ELIDE_THRESHOLD:
                original = len(text)
                if isinstance(m.get("content"), list):
                    for p in m["content"]:
                        if (isinstance(p, dict)
                                and p.get("type") == "elision"
                                and isinstance(p.get("original_chars"), int)):
                            # Re-eliding an elided body must not shrink
                            # the recorded original size (idempotence).
                            original = max(original, p["original_chars"])
                    m["content"] = [
                        {"type": "text", "text": text[:_TOOL_KEEP]},
                        {"type": "elision", "note": ELISION_NOTE,
                         "original_chars": original},
                        {"type": "text", "text": text[-_TOOL_KEEP:]},
                    ]
                else:
                    # String content stays a string (the wire form several
                    # consumers require for tool messages): the elision
                    # marker is embedded as text instead of a Part.
                    m["content"] = (
                        text[:_TOOL_KEEP]
                        + "\n[" + ELISION_NOTE + "]\n"
                        + text[-_TOOL_KEEP:]
                    )
    if _size(out) <= limit_chars:
        return out

    # Step 3 — drop whole middle groups.
    groups = _groups(out)
    first_user = next(
        (i for i, m in enumerate(out) if m.get("role") == "user"), -1)
    last_user = max(
        (i for i, m in enumerate(out) if m.get("role") == "user"),
        default=-1)
    removable = [
        g for g in groups[:-_TAIL_GROUPS]
        if all(i >= keep_prefix
               and i not in (first_user, last_user)
               and out[i].get("role") != "system" for i in g)
    ]
    costs = {i: len(_dump(m)) + 1 for i, m in enumerate(out)}
    total = 1 + sum(costs.values())  # == len(_dump(out)) for non-empty out
    removed = set()
    for g in removable:
        if total <= limit_chars:
            break
        removed.update(g)
        total -= sum(costs[i] for i in g)
    if total > limit_chars:
        return None
    return [m for i, m in enumerate(out) if i not in removed]


# ---------------------------------------------------------------------------
# §3 summaries
# ---------------------------------------------------------------------------

def summary_item(text) -> dict:
    """A system item whose content is a single summary Part (§3)."""
    return {"role": "system",
            "content": [{"type": "summary", "text": text}]}


# ---------------------------------------------------------------------------
# §4 envelopes
# ---------------------------------------------------------------------------

def envelope(item, *, actor, seq, at, flags=None) -> dict:
    """Wrap an item in a record-layer envelope (§4). The hash is the
    sha256 of the canonical JSON of the item."""
    env = {
        "seq": seq,
        "at": at,
        "actor": actor,
        "hash": hashlib.sha256(
            canonical_json(item).encode("utf-8")).hexdigest(),
    }
    if flags is not None:
        env["flags"] = copy.deepcopy(flags)
    env["item"] = copy.deepcopy(item)
    return env




def _needs_repair(call) -> bool:
    if not isinstance(call, dict):
        return False
    fn = call.get("function")
    if isinstance(fn, dict):
        return not isinstance(fn.get("arguments"), str)
    return "input" in call and "name" in call


def repair_wire(item):
    """SPEC §9 ingress repair: return the item with any non-wire tool
    calls fixed to wire form. A well-formed wire item is returned with
    its content byte-identical (nothing re-serialized) — a relay must
    not disturb upstream prompt-cache prefixes. Canonical {"name",
    "input"} calls become wire calls; a wire call whose arguments is
    not a string is re-serialized from its value. Unrecognized shapes
    pass through untouched.

    The common case — no tool calls, or only well-formed ones — returns
    the item itself without copying: this runs on every message of every
    chat request."""
    if not isinstance(item, dict):
        return item
    calls = item.get("tool_calls")
    if not isinstance(calls, list) or not any(map(_needs_repair, calls)):
        return item
    out = copy.deepcopy(item)
    calls = out["tool_calls"]
    fixed = []
    for c in calls:
        if isinstance(c, dict) and isinstance(c.get("function"), dict):
            fn = c["function"]
            args = fn.get("arguments")
            if not isinstance(args, str):
                fn["arguments"] = json.dumps(
                    args if args is not None else {},
                    ensure_ascii=False)
            fixed.append(c)
        elif isinstance(c, dict) and "input" in c and "name" in c:
            fixed.append({
                "id": c.get("id", ""),
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c["input"],
                                            ensure_ascii=False),
                },
            })
        else:
            fixed.append(c)
    out["tool_calls"] = fixed
    return out
# ---------------------------------------------------------------------------
# §5 scrub
# ---------------------------------------------------------------------------

# Names whose value is a credential whatever it looks like. "cookies"
# goes whole; "headers" does not — its authorization/cookie entries are
# removed by name and the rest (Accept, Content-Type) is not secret.
_SCRUB_KEYS = {
    "token", "session_token", "authorization", "proxy_authorization",
    "cookie", "cookies", "set_cookie", "api_key", "apikey", "x_api_key",
    "password", "passwd", "secret", "client_secret",
    "access_token", "refresh_token", "agent_secret",
}
# Suffix-named keys are ambiguous (max_token, next_page_token): only a
# string value that looks like a credential goes.
_SCRUB_SUFFIXES = ("_token", "_secret", "_password", "_api_key")
_SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-./+=:]{16,}$")

REDACTED = "[REDACTED]"

_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_PRIVKEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
_KEYLIKE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|xox[abprs]-[A-Za-z0-9-]{8,}|"
    r"AKIA[A-Z0-9]{12,})", re.ASCII)
# A labelled assignment of a literal: `password = "hunter22"`,
# `API_KEY=AbC123xyz`, `"refresh_token": "…"`. Code that merely
# mentions the name is left alone — comparisons (`token == x`), calls
# and expressions (`getToken()`, `request.form["pw"]`), type
# annotations (`password: Optional[str]`), and bare lowercase
# identifiers (`token = undefined_var`). Only the value is replaced;
# the label stays so the text still reads. ASCII word/space classes on
# purpose: the TypeScript port must match byte for byte (JS \b is ASCII).
_LABELLED_RE = re.compile(
    r"\b((?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"auth[_-]?token|client[_-]?secret|secret|password|passwd|token)"
    r"[\"']?)([ \t\n\r\f\v]*[=:](?!=)[ \t\n\r\f\v]*)"
    r"(?:\"([^\" \t\n\r\f\v]{6,})\"|'([^' \t\n\r\f\v]{6,})'|"
    r"([A-Za-z0-9_\-./+]{6,}={0,2})(?![^ \t\n\r\f\v;,&'\")]))",
    re.IGNORECASE | re.ASCII)


def _labelled_sub(m) -> str:
    label, sep, dq, sq, bare = m.groups()
    if dq is not None:
        return f'{label}{sep}"{REDACTED}"'
    if sq is not None:
        return f"{label}{sep}'{REDACTED}'"
    if not re.search(r"[0-9A-Z]", bare):
        return m.group(0)
    return f"{label}{sep}{REDACTED}"


def _scrub_text(text: str) -> str:
    text = _PRIVKEY_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _KEYLIKE_RE.sub(REDACTED, text)
    text = _LABELLED_RE.sub(_labelled_sub, text)
    return text


def _scrub_key(name: str, value) -> bool:
    nk = name.lower().replace("-", "_")
    if nk in _SCRUB_KEYS:
        return not isinstance(value, (bool, int, float)) and value is not None
    if nk.endswith(_SCRUB_SUFFIXES):
        return isinstance(value, str) and bool(_SECRET_VALUE_RE.match(value))
    return False


def scrub(value):
    """Apply the §5 scrub contract (cross-boundary exits only).

    Dict keys whose normalized name (lowercase, '-' -> '_') is in the
    §5 field set are removed (numbers/booleans/null kept: a count named
    "token" is not a credential); keys ending _token/_secret/_password/
    _api_key are removed only when their value is a credential-shaped
    string. In strings, secret-shaped spans (private-key blocks, JWTs,
    known key prefixes, labelled literal assignments) are replaced with
    [REDACTED] — never deleted silently, never touching code that only
    names a secret. Idempotent. Returns a scrubbed copy; the input is not
    mutated.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _scrub_key(k, v):
                continue
            out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v) for v in value)
    if isinstance(value, str):
        return _scrub_text(value)
    return value
