"""Persistent, shared cookie store for web.search, web.fetch and the browser.

Why this exists: a site that answers a CAPTCHA or a login wall hands out a
cookie, and without somewhere to keep it the very next request walks into the
same wall. Solving a challenge once — by hand, in the live-view browser —
should be enough to keep that host readable afterwards, from the cheap HTTP
path as much as from the browser.

Cookies are stored, not the whole Chrome profile. Persisting the profile
directory would work too, but Chrome locks a profile to one running instance,
so two sessions (or a session and a crashed session's leftovers) fight over it.
A cookie file has no such constraint and is equally readable by requests.

Everything here is gated on /config search_cookie_enabled (default off) and
optionally narrowed by search_cookie_domains. This is the user's own machine
and their own logged-in sessions: keeping the store opt-in, inspectable and
easy to wipe matters more than convenience.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Iterable

try:
    from paths import LAINTAS_HOME
except Exception:  # paths is unavailable in some minimal test contexts
    from pathlib import Path
    LAINTAS_HOME = Path(os.environ.get("LAINTAS_HOME", str(Path.home() / ".laintas")))

COOKIE_FILE = LAINTAS_HOME / "cookies.json"

_LOCK = threading.RLock()

# One cookie == {name, value, domain, path, secure, httpOnly, expires, egress}
# "expires" is a unix timestamp; 0/absent means a session cookie.
_KEYS = ("name", "value", "domain", "path", "secure", "httpOnly", "expires", "egress")

# Cookies that record "this client already passed the wall". These are the only
# ones kept automatically, because they are the only ones whose whole purpose is
# to be replayed by an unattended client.
#
# Anything else a browser picks up while clearing a challenge — above all a
# session cookie from a site the user signed into while they were there — is a
# *credential*, and a credential that attaches itself to every later request is
# one injected instruction away from being spent on somebody else's URL. Those
# have to be captured deliberately, as a named identity.
_CLEARANCE_NAMES = frozenset({
    "cf_clearance",              # Cloudflare "I am not a bot" clearance
    "__cf_bm",                   # Cloudflare bot management
    "google_abuse_exemption",    # Google, after a hand-solved interstitial
    "consent",                   # Google/YouTube consent wall
    "datadome",                  # DataDome
    "reese84",                   # Kasada / F5
    "_px3", "_pxhd", "px-cookie",  # PerimeterX
})
_CLEARANCE_PREFIXES = ("incap_ses_", "visid_incap_", "nlbi_")  # Imperva


def _extra_clearance_names() -> frozenset:
    """User-added clearance cookie names, for walls we do not know about."""
    raw = _get_config("search_cookie_names", "") or ""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw).replace(",", " ").split()
    return frozenset(name.strip().lower() for name in items if name.strip())


def is_clearance(name: str) -> bool:
    """True when this cookie is a challenge clearance rather than a credential."""
    key = str(name or "").strip().lower()
    if not key:
        return False
    if key in _CLEARANCE_NAMES or key in _extra_clearance_names():
        return True
    return any(key.startswith(prefix) for prefix in _CLEARANCE_PREFIXES)


def current_egress() -> str:
    """The exit these cookies are valid for. See web_search.current_egress."""
    try:
        import web_search
        return web_search.current_egress()
    except Exception:
        return (os.environ.get("LAINTAS_HTTP_PROXY") or "").strip() or "direct"


def egress_matches(stored: str, current: str = "") -> bool:
    if not stored:
        return True  # written before exits were recorded; do not discard
    return stored == (current or current_egress())


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
    return bool(_get_config("search_cookie_enabled", False))


def _allowed_domains() -> list[str]:
    """Domain allowlist from /config search_cookie_domains ([] = all)."""
    raw = _get_config("search_cookie_domains", "") or ""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw).replace(",", " ").split()
    return [d.strip().lstrip(".").lower() for d in items if d.strip()]


def domain_allowed(domain: str) -> bool:
    """True when this cookie's domain may be stored.

    An empty allowlist means "any domain". A listed domain also covers its
    subdomains, so "example.com" admits "login.example.com".
    """
    allow = _allowed_domains()
    if not allow:
        return True
    host = (domain or "").lstrip(".").lower()
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in allow)


def _normalize(cookie: dict) -> dict | None:
    name = str(cookie.get("name") or "").strip()
    domain = str(cookie.get("domain") or "").strip()
    if not name or not domain:
        return None
    expires = cookie.get("expires") or cookie.get("expiry") or 0
    try:
        expires = float(expires)
    except (TypeError, ValueError):
        expires = 0.0
    if expires < 0:
        expires = 0.0
    return {
        "name": name,
        "value": str(cookie.get("value") or ""),
        "domain": domain,
        "path": str(cookie.get("path") or "/"),
        "secure": bool(cookie.get("secure", False)),
        "httpOnly": bool(cookie.get("httpOnly", cookie.get("http_only", False))),
        "expires": expires,
        "egress": str(cookie.get("egress") or current_egress()),
    }


def _is_expired(cookie: dict, now: float | None = None) -> bool:
    expires = cookie.get("expires") or 0
    if not expires:
        return False  # session cookie — kept for this process's lifetime
    return float(expires) <= (now if now is not None else time.time())


def _key(cookie: dict) -> tuple:
    return (cookie["domain"].lstrip(".").lower(), cookie["path"], cookie["name"])


def load(all_egress: bool = False) -> list[dict]:
    """Unexpired stored cookies valid for the current exit. Never raises.

    all_egress=True returns everything regardless of exit — for listing and
    clearing, where hiding a cookie the user is trying to delete would be
    worse than showing one they cannot currently use.
    """
    with _LOCK:
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    now = time.time()
    here = current_egress()
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cookie = _normalize(item)
        if not cookie or _is_expired(cookie, now):
            continue
        if not domain_allowed(cookie["domain"]):
            continue
        if not all_egress and not egress_matches(cookie.get("egress", ""), here):
            continue
        out.append(cookie)
    return out


def save(cookies: Iterable[dict]) -> int:
    """Replace the store. Returns the number written. Never raises."""
    now = time.time()
    keep: dict[tuple, dict] = {}
    for item in cookies or []:
        cookie = _normalize(item if isinstance(item, dict) else {})
        if not cookie or _is_expired(cookie, now) or not domain_allowed(cookie["domain"]):
            continue
        keep[_key(cookie)] = cookie
    payload = list(keep.values())
    with _LOCK:
        try:
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(COOKIE_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, COOKIE_FILE)
        except OSError:
            return 0
    return len(payload)


def merge(cookies: Iterable[dict]) -> int:
    """Upsert cookies into the store. Returns the number of new/changed ones.

    Reads every exit's cookies, not just this one's: save() rewrites the whole
    file, so merging while filtered would quietly delete everything earned
    through a proxy the moment one request went out direct.
    """
    existing = {_key(c): c for c in load(all_egress=True)}
    changed = 0
    now = time.time()
    for item in cookies or []:
        cookie = _normalize(item if isinstance(item, dict) else {})
        if not cookie or _is_expired(cookie, now) or not domain_allowed(cookie["domain"]):
            continue
        key = _key(cookie)
        if existing.get(key) != cookie:
            changed += 1
        existing[key] = cookie
    if changed:
        save(existing.values())
    return changed


def clear(domain: str = "") -> int:
    """Drop cookies for one domain (and its subdomains), or all of them."""
    if not domain:
        with _LOCK:
            try:
                existing = len(load(all_egress=True))
                COOKIE_FILE.unlink()
            except OSError:
                return 0
        return existing
    host = domain.lstrip(".").lower()
    kept, dropped = [], 0
    for cookie in load(all_egress=True):
        cookie_host = cookie["domain"].lstrip(".").lower()
        if cookie_host == host or cookie_host.endswith("." + host):
            dropped += 1
        else:
            kept.append(cookie)
    if dropped:
        save(kept)
    return dropped


def summary() -> list[tuple[str, int]]:
    """(domain, cookie count) pairs, so the user can see what is being kept.

    Everything stored, not just what is usable from this exit — a cookie the
    user cannot use today is still one they may want to know about or delete.
    """
    counts: dict[str, int] = {}
    for cookie in load(all_egress=True):
        host = cookie["domain"].lstrip(".").lower()
        counts[host] = counts.get(host, 0) + 1
    return sorted(counts.items())


def stats() -> dict:
    """Totals plus how many are usable from the exit in use right now."""
    here = current_egress()
    stored = load(all_egress=True)
    usable = [c for c in stored if egress_matches(c.get("egress", ""), here)]
    return {
        "total": len(stored),
        "usable": len(usable),
        "egress": here,
        "domains": len({c["domain"].lstrip(".").lower() for c in stored}),
    }


# ── requests.cookies.RequestsCookieJar bridge ────────────────────────


def into_jar(jar) -> int:
    """Load the store into a requests cookie jar. Returns cookies added."""
    added = 0
    for cookie in load():
        try:
            jar.set(cookie["name"], cookie["value"],
                    domain=cookie["domain"], path=cookie["path"],
                    secure=cookie["secure"],
                    expires=int(cookie["expires"]) or None)
            added += 1
        except Exception:
            continue
    return added


def from_jar(jar) -> list[dict]:
    """Snapshot a requests cookie jar as store records."""
    out = []
    for cookie in jar or []:
        record = _normalize({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": bool(cookie.secure),
            "expires": cookie.expires or 0,
        })
        if record:
            out.append(record)
    return out
