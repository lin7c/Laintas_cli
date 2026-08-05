"""Named browser identities: signed-in sessions the agent can reuse.

The point is automation that runs as *you*: sign in to a site once by hand,
name that identity, and later tasks can read the pages behind that login
without signing in again.

Four things this gets right that a bare cookie jar does not:

1. **It stores more than cookies.** Plenty of sites keep their session in
   localStorage, so a cookie-only export logs you straight back out. An
   identity holds Playwright's storage_state — cookies *and* localStorage.

2. **It pins the exit.** A session issued to one IP and replayed from another
   is at best logged out and at worst flagged. Google's abuse exemption is the
   blunt version of this: the cookie literally contains the IP it was issued
   for. Every identity records the egress it was created through and refuses
   to be used through a different one.

3. **Credentials are never ambient.** web.fetch does not attach an identity
   unless the caller names one *and* the URL is inside that identity's own
   domain list. An agent that reads untrusted pages must not be one injected
   instruction away from spending your logged-in session on an attacker's URL.

4. **The model never sees the values.** Nothing here returns a cookie value to
   a tool result. What is listed is names, domains, and freshness.

Storage lives in ~/.laintas/identities/<name>.json, 0600, one file per
identity so a single one can be revoked by deleting it.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

try:
    from paths import LAINTAS_HOME
except Exception:  # minimal/test contexts without the paths module
    from pathlib import Path
    LAINTAS_HOME = Path(os.environ.get("LAINTAS_HOME", str(Path.home() / ".laintas")))

IDENTITY_DIR = LAINTAS_HOME / "identities"

_LOCK = threading.RLock()
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class IdentityError(Exception):
    pass


def _get_config(key: str, default: Any = None) -> Any:
    try:
        from agent_loop import get_runtime_config
        val = get_runtime_config(key)
        if val is not None:
            return val
    except Exception:
        pass
    return default


def enabled() -> bool:
    """Identities are opt-in: they hold live logins to the user's accounts.

    Its own switch rather than the cookie-jar one. A user turning on
    search_cookie_enabled is asking for a solved CAPTCHA to be remembered;
    that is not the same decision as letting automated fetches act as their
    signed-in Google account, and it should not silently grant it.
    """
    return bool(_get_config("identity_enabled", False))


def _path(name: str):
    return IDENTITY_DIR / f"{name}.json"


def validate_name(name: str) -> str:
    value = str(name or "").strip().lower()
    if not _NAME_RE.match(value):
        raise IdentityError(
            f"invalid identity name {name!r}: use letters, digits, '.', '-', '_'")
    return value


# ── egress identity ──────────────────────────────────────────────────


def current_egress() -> str:
    """How this machine reaches the internet right now.

    Delegates to web_search so the cookie store and this one cannot disagree
    about what counts as the same exit.
    """
    try:
        import web_search
        return web_search.current_egress()
    except Exception:
        proxy = (os.environ.get("LAINTAS_HTTP_PROXY") or "").strip()
        return proxy or "direct"


def egress_matches(stored: str, current: str = "") -> bool:
    current = current or current_egress()
    if not stored:
        return True  # created before the exit was recorded; do not block
    return stored == current


# ── domain scoping ───────────────────────────────────────────────────


def registrable(host: str) -> str:
    host = str(host or "").strip().lower().rstrip(".")
    if host.startswith("["):  # IPv6 literal
        return host
    return host.lstrip(".")


def domain_covers(domain: str, host: str) -> bool:
    """True when `host` is `domain` or one of its subdomains."""
    domain = registrable(domain)
    host = registrable(host)
    if not domain or not host:
        return False
    return host == domain or host.endswith("." + domain)


# ── records ──────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _blank(name: str) -> dict:
    return {
        "name": name,
        "domains": [],
        "egress": current_egress(),
        "user_agent": "",
        "storage_state": {"cookies": [], "origins": []},
        "created": _now(),
        "updated": _now(),
        "last_used": 0.0,
        "probe": {},          # {"url": ..., "expect": ...}
        "last_probe": {},     # {"at": ts, "ok": bool, "detail": str}
    }


def _read(name: str) -> dict | None:
    try:
        with open(_path(name), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("name"):
        return None
    data.setdefault("storage_state", {"cookies": [], "origins": []})
    data.setdefault("domains", [])
    data.setdefault("probe", {})
    data.setdefault("last_probe", {})
    data.setdefault("egress", "")
    return data


def _write(record: dict) -> None:
    name = record["name"]
    with _LOCK:
        IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(IDENTITY_DIR, 0o700)
        except OSError:
            pass
        target = _path(name)
        tmp = str(target) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)


def save(name: str, storage_state: dict, *, domains=None,
         user_agent: str = "", egress: str = "", probe: dict | None = None) -> dict:
    """Create or update an identity from a Playwright storage_state."""
    name = validate_name(name)
    record = _read(name) or _blank(name)
    if isinstance(storage_state, dict):
        record["storage_state"] = {
            "cookies": [c for c in (storage_state.get("cookies") or [])
                        if isinstance(c, dict)],
            "origins": [o for o in (storage_state.get("origins") or [])
                        if isinstance(o, dict)],
        }
    if domains is not None:
        record["domains"] = sorted({registrable(d) for d in domains if registrable(d)})
    if not record["domains"]:
        record["domains"] = sorted(_domains_in(record["storage_state"]))
    if user_agent:
        record["user_agent"] = user_agent
    record["egress"] = egress or record.get("egress") or current_egress()
    if probe is not None:
        record["probe"] = probe
    record["updated"] = _now()
    _write(record)
    return record


def _domains_in(storage_state: dict) -> set:
    found = set()
    for cookie in storage_state.get("cookies") or []:
        host = registrable(cookie.get("domain", ""))
        if host:
            found.add(host)
    for origin in storage_state.get("origins") or []:
        value = str(origin.get("origin") or "")
        if "://" in value:
            found.add(registrable(value.split("://", 1)[1].split("/", 1)[0].split(":")[0]))
    return found


def load(name: str) -> dict | None:
    try:
        return _read(validate_name(name))
    except IdentityError:
        return None


def delete(name: str) -> bool:
    try:
        name = validate_name(name)
    except IdentityError:
        return False
    with _LOCK:
        try:
            _path(name).unlink()
            return True
        except OSError:
            return False


def names() -> list[str]:
    try:
        return sorted(p.stem for p in IDENTITY_DIR.glob("*.json"))
    except OSError:
        return []


def describe(name: str) -> dict | None:
    """A safe summary — never any cookie or storage values.

    This is what tool results and `/identity list` show. Returning the values
    themselves would put live session tokens into the transcript, which is sent
    to the model provider.
    """
    record = load(name)
    if record is None:
        return None
    state = record.get("storage_state") or {}
    return {
        "name": record["name"],
        "domains": list(record.get("domains") or []),
        "cookies": len(state.get("cookies") or []),
        "origins": len(state.get("origins") or []),
        "egress": record.get("egress", ""),
        "egress_matches_now": egress_matches(record.get("egress", "")),
        "updated": record.get("updated", 0),
        "last_used": record.get("last_used", 0),
        "last_probe": record.get("last_probe") or {},
        "has_probe": bool((record.get("probe") or {}).get("url")),
    }


def describe_all() -> list[dict]:
    return [d for d in (describe(n) for n in names()) if d]


def touch(name: str) -> None:
    record = load(name)
    if record is None:
        return
    record["last_used"] = _now()
    _write(record)


def record_probe(name: str, ok: bool, detail: str = "") -> None:
    record = load(name)
    if record is None:
        return
    record["last_probe"] = {"at": _now(), "ok": bool(ok), "detail": detail[:200]}
    _write(record)


# ── use ──────────────────────────────────────────────────────────────


def authorize(name: str, url: str) -> tuple[dict | None, str]:
    """Decide whether this identity may be used for this URL.

    Returns (record, "") when it may, or (None, reason). Both failure modes
    matter: a URL outside the identity's domains is the injection case, and a
    changed exit is the "the session will not work and using it looks like
    theft" case.
    """
    if not enabled():
        return None, ("saved logins are off — turn on /config identity_enabled "
                      "to let fetches use them")
    record = load(name)
    if record is None:
        return None, f"no identity named '{name}'"

    try:
        import urllib.parse
        host = registrable(urllib.parse.urlparse(url).hostname or "")
    except ValueError:
        host = ""
    if not host:
        return None, f"cannot tell which host {url!r} belongs to"

    domains = record.get("domains") or []
    if not any(domain_covers(d, host) for d in domains):
        return None, (f"identity '{name}' is limited to {', '.join(domains) or '(none)'} "
                      f"and this URL is on {host}")

    stored_egress = record.get("egress", "")
    if not egress_matches(stored_egress):
        return None, (f"identity '{name}' was created through {stored_egress!r} but this "
                      f"machine is now going out through {current_egress()!r}; a session "
                      f"replayed from a different exit will be rejected or flagged")
    return record, ""


def cookies_for_requests(name: str, url: str) -> tuple[list[dict], str]:
    """Cookies to attach to one HTTP request, or ([], reason).

    Only the cookies whose own domain covers the target host are returned —
    an identity that happens to hold two sites' logins must not send one
    site's cookies to the other.
    """
    record, reason = authorize(name, url)
    if record is None:
        return [], reason
    import urllib.parse
    host = registrable(urllib.parse.urlparse(url).hostname or "")
    out = []
    for cookie in record.get("storage_state", {}).get("cookies") or []:
        domain = registrable(cookie.get("domain", ""))
        if domain and domain_covers(domain, host):
            out.append(cookie)
    return out, ""
