"""Web search and fetch with engine chain, proxy, cookie, and structured errors.

Engine chain (auto): Google -> DuckDuckGo -> laintas_search
  - Google/DDG: free HTML scraping, best-effort with proxy+cookie
  - laintas_search: paid API fallback, always reliable

web.fetch escalates rather than giving up, cheapest rung first:
  1. plain HTTP           — the normal case
  2. browser fingerprint  — curl_cffi, when installed; beats handshake-based
                            bot walls without paying for a browser
  3. rendered browser     — for client-rendered pages and harder challenges
  4. the user            — leaves the browser on the challenge so a human can
                            solve it once, in the live view; the cookies they
                            earn are reused afterwards, including by rung 1
  5. Wayback snapshot     — clearly labelled as an archive, not the live page
Each reply names the rung it came from in "transport", and a failure lists
every rung it tried.

Proxy: LAINTAS_HTTP_PROXY env or /config search_proxy, one setting shared by
search, fetch and the headless browser (see browser_egress_overrides).
  - http://, https://, socks5://, socks5h://
  - /config search_proxy_mode picks off / auto (per-host, learned) / always
  - laintas_search is reached directly first, then retried via the proxy

Cookie: persistent jar in ~/.laintas/cookies.json (see cookie_store), opt-in via
/config search_cookie_enabled, optionally narrowed by search_cookie_domains.
Shared by web.search, web.fetch and the render browser — a challenge solved by
hand has to count everywhere or the user just gets asked again.

Every URL, on every redirect hop, is checked against the SSRF rules in
_guard_url: this runs on the user's own machine, where "fetch this URL" would
otherwise reach their router, their LAN and cloud metadata endpoints.

Structured errors: each engine failure is classified so the AI can tell the
user *why* search degraded (captcha / rate_limited / network / proxy_error /
consent_page / empty / degraded / api_error).
"""
from __future__ import annotations

import html as _html
import os
import re as _re
import threading
import time
import urllib.parse
import uuid
from enum import Enum
from typing import Any

import requests

# ── Shared state ──────────────────────────────────────────────────────

_COOKIE_JAR: requests.cookies.RequestsCookieJar | None = None
_COOKIE_LOCK = threading.Lock()

# Fast-fail cache: {engine_name: expiry_timestamp}
# After an engine fails, skip it for this many seconds to avoid wasting time.
_FAST_FAIL_TTL = 300  # 5 minutes
_FAST_FAIL: dict[str, float] = {}
_FAST_FAIL_LOCK = threading.Lock()

_WEB_FETCH_TIMEOUT = 15
_MAX_RESPONSE_BYTES = 2_000_000

# ── Structured error types ───────────────────────────────────────────


class SearchErrorType(str, Enum):
    CAPTCHA = "captcha"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    PROXY_ERROR = "proxy_error"
    CONSENT_PAGE = "consent_page"
    EMPTY = "empty"
    DEGRADED = "degraded"
    API_ERROR = "api_error"


# ── Config helpers ───────────────────────────────────────────────────


def _get_config(key: str, default: Any = None) -> Any:
    """Read from /config runtime config, falling back to env, then default."""
    try:
        from agent_loop import get_runtime_config
        val = get_runtime_config(key)
        if val is not None:
            return val
    except Exception:
        pass
    return default


def _get_proxy() -> str | None:
    """The one proxy setting for this host: /config search_proxy, else env.

    Single source of truth for web.search, web.fetch AND the headless browser
    (browser_session.egress_from_env consults browser_egress_overrides below).
    Before this, search and the browser read different variables, so a page that
    escalated from an HTTP fetch to a browser render silently went direct and
    failed for anyone who needed the proxy to reach it at all.

    LAINTAS_BROWSER_PROXY still wins for the browser alone, for hosts that want
    the two to differ.
    """
    proxy = _get_config("search_proxy", "")
    if proxy:
        return str(proxy).strip() or None
    return (os.environ.get("LAINTAS_HTTP_PROXY") or "").strip() or None


def _proxy_mode() -> str:
    """off | auto | always. auto = direct first, proxy only for hosts that
    have already proven unreachable without it."""
    mode = str(_get_config("search_proxy_mode", "") or
               os.environ.get("LAINTAS_HTTP_PROXY_MODE", "") or "auto").lower()
    return mode if mode in ("off", "auto", "always") else "auto"


# Hosts that failed direct and succeeded (or are assumed to succeed) via the
# proxy. Learned at runtime so a user behind a national firewall pays the
# direct-connection timeout once per host per hour, not on every request.
_PROXY_HOSTS: dict[str, float] = {}
_PROXY_HOSTS_TTL = 3600
_PROXY_HOSTS_LOCK = threading.Lock()


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_needs_proxy(host: str) -> bool:
    if not host:
        return False
    with _PROXY_HOSTS_LOCK:
        expiry = _PROXY_HOSTS.get(host)
        if expiry is None:
            return False
        if time.time() < expiry:
            return True
        del _PROXY_HOSTS[host]
        return False


def _mark_host_needs_proxy(host: str) -> None:
    if not host:
        return
    with _PROXY_HOSTS_LOCK:
        _PROXY_HOSTS[host] = time.time() + _PROXY_HOSTS_TTL


def _proxy_for_host(host: str) -> str | None:
    """The proxy to use for this host right now, per the routing mode."""
    proxy = _get_proxy()
    if not proxy:
        return None
    mode = _proxy_mode()
    if mode == "off":
        return None
    if mode == "always":
        return proxy
    return proxy if _host_needs_proxy(host) else None


def current_egress() -> str:
    """A stable label for how this machine reaches the internet right now.

    The single definition of "same exit", used by both stores. Cookies that
    encode the address they were issued to — Cloudflare clearance, and Google's
    abuse exemption, which literally contains "IP=<addr>" — are worthless and
    suspicious when replayed from anywhere else, so both stores have to agree
    on what "elsewhere" means.

    The proxy URL rather than the public IP: resolving the address would cost a
    round trip on every check, and a rotating residential pool would look like
    a new identity on every request even though the route never changed.
    """
    if _proxy_mode() == "off":
        return "direct"
    proxy = _get_proxy()
    return str(proxy) if proxy else "direct"


def browser_egress_overrides() -> dict:
    """Proxy settings for the headless browser, from the unified config.

    Called by browser_session.egress_from_env(). Returns {} when nothing is
    configured. Deliberately has no way to pass a proxy in per call: pointing
    the browser at an arbitrary proxy must stay a decision made by whoever
    starts the process, never one the agent can make from a page it just read.
    """
    if _proxy_mode() == "off":
        return {}
    proxy = _get_proxy()
    return {"proxy": proxy} if proxy else {}


def _cookie_enabled() -> bool:
    return bool(_get_config("search_cookie_enabled", False))


def _get_cookie_jar() -> requests.cookies.RequestsCookieJar | None:
    """Return the shared cookie jar if enabled, else None.

    Seeded from the persistent store, so a challenge solved by hand in the
    live-view browser (last session, or five minutes ago) still counts here.
    """
    global _COOKIE_JAR
    if not _cookie_enabled():
        return None
    with _COOKIE_LOCK:
        if _COOKIE_JAR is None:
            jar = requests.cookies.RequestsCookieJar()
            # Inject Google consent cookie to bypass consent wall
            jar.set("CONSENT", "YES+", domain=".google.com", path="/")
            try:
                import cookie_store
                cookie_store.into_jar(jar)
            except Exception:
                pass
            _COOKIE_JAR = jar
        return _COOKIE_JAR


def persist_cookies() -> int:
    """Write the live jar back to the persistent store. Returns cookies kept."""
    if not _cookie_enabled():
        return 0
    with _COOKIE_LOCK:
        jar = _COOKIE_JAR
    if jar is None:
        return 0
    try:
        import cookie_store
        return cookie_store.merge(cookie_store.from_jar(jar))
    except Exception:
        return 0


def adopt_cookies(cookies: list) -> dict:
    """Take *clearance* cookies from the browser into the jar and the store.

    The hinge of the unlock flow: a wall cleared by hand has to reach the cheap
    HTTP path or the user is asked to clear it again on the next request.

    Only clearance cookies are taken. The unlock prompt invites the user to
    "solve the challenge or sign in", and a login done at that moment leaves a
    session cookie sitting in the same browser. Adopting it would make that
    account credential ambient — attached to every later fetch of the domain,
    with no identity named and no domain check — which is precisely what the
    identity store exists to prevent. Those are reported, not kept: the user
    can promote them deliberately with /identity capture.

    Returns {"kept": n, "skipped": n, "skipped_domains": [...]}.
    """
    result = {"kept": 0, "skipped": 0, "skipped_domains": []}
    if not cookies:
        return result
    try:
        import cookie_store
    except Exception:
        return result

    clearance, skipped_domains = [], set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if cookie_store.is_clearance(cookie.get("name")):
            clearance.append(cookie)
        else:
            result["skipped"] += 1
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if domain:
                skipped_domains.add(domain)
    result["skipped_domains"] = sorted(skipped_domains)

    if not clearance:
        return result

    result["kept"] = cookie_store.merge(clearance)
    jar = _get_cookie_jar()
    if jar is not None:
        for cookie in clearance:
            try:
                if not cookie_store.domain_allowed(str(cookie.get("domain") or "")):
                    continue
                jar.set(str(cookie.get("name")), str(cookie.get("value") or ""),
                        domain=str(cookie.get("domain") or ""),
                        path=str(cookie.get("path") or "/"))
            except Exception:
                continue
    return result


def clear_cookie_jar(persistent: bool = True) -> int:
    """Clear all cookies. Returns the number of cookies removed."""
    global _COOKIE_JAR
    removed = 0
    with _COOKIE_LOCK:
        if _COOKIE_JAR is not None:
            removed = len(_COOKIE_JAR)
            _COOKIE_JAR = None
    if persistent:
        try:
            import cookie_store
            removed += cookie_store.clear()
        except Exception:
            pass
    return removed


def _is_fast_failed(engine: str) -> bool:
    """Check if an engine is in fast-fail cooldown."""
    with _FAST_FAIL_LOCK:
        expiry = _FAST_FAIL.get(engine)
        if expiry is None:
            return False
        if time.time() < expiry:
            return True
        del _FAST_FAIL[engine]
        return False


def _mark_fast_fail(engine: str) -> None:
    """Mark an engine as fast-failed for _FAST_FAIL_TTL seconds."""
    with _FAST_FAIL_LOCK:
        _FAST_FAIL[engine] = time.time() + _FAST_FAIL_TTL


# ── HTTP helpers ─────────────────────────────────────────────────────

_UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _UA_BROWSER,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN,zh;q=0.8",
}


def _build_session(host: str = "", force_proxy: bool = False) -> requests.Session:
    """Build a requests.Session with proxy and cookie jar configured.

    The proxy is chosen per host by the routing mode, so "auto" users pay for
    the proxy only on the hosts that actually need it.
    """
    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)

    proxy = _get_proxy() if force_proxy else _proxy_for_host(host)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    jar = _get_cookie_jar()
    if jar is not None:
        session.cookies = jar

    return session


def _proxy_retry_available(host: str, already_proxied: bool) -> bool:
    """True when a failed direct request is worth retrying through the proxy."""
    if already_proxied or _proxy_mode() == "off":
        return False
    return bool(_get_proxy()) and not _host_needs_proxy(host)


def _request(method: str, url: str, *, host: str = "", on_session=None, **kwargs):
    """One HTTP request, retried through the proxy if the direct attempt fails.

    Returns (response, error_type_or_None, session). The session is returned so
    callers that follow redirects by hand keep the same connection, cookie jar
    and proxy decision across hops.

    A host is only remembered as needing the proxy once the proxied attempt has
    actually worked — a failure on both paths teaches us nothing.
    """
    host = host or _host_of(url)
    proxied = bool(_proxy_for_host(host))
    session = _build_session(host=host)
    if on_session is not None:
        on_session(session)
    try:
        return session.request(method, url, **kwargs), None, session
    except requests.RequestException as direct_err:
        if not _proxy_retry_available(host, proxied):
            return None, _classify_request_error(direct_err), session

    session = _build_session(host=host, force_proxy=True)
    if on_session is not None:
        on_session(session)
    try:
        response = session.request(method, url, **kwargs)
    except requests.RequestException as proxy_err:
        return None, _classify_request_error(proxy_err), session
    _mark_host_needs_proxy(host)
    return response, None, session


def _classify_request_error(exc: Exception) -> SearchErrorType:
    """Classify a requests exception into a SearchErrorType."""
    if isinstance(exc, requests.exceptions.ProxyError):
        return SearchErrorType.PROXY_ERROR
    if isinstance(exc, requests.exceptions.SSLError):
        return SearchErrorType.NETWORK
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return SearchErrorType.NETWORK
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return SearchErrorType.NETWORK
    if isinstance(exc, requests.exceptions.ConnectionError):
        # Could be proxy or network; check if proxy is configured
        if _get_proxy():
            return SearchErrorType.PROXY_ERROR
        return SearchErrorType.NETWORK
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return SearchErrorType.NETWORK
    return SearchErrorType.NETWORK


# ── HTML helpers ─────────────────────────────────────────────────────


def _clean_text(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = _re.sub(r'<script[^>]*>.*?</script>', '', text,
                   flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r'<style[^>]*>.*?</style>', '', text,
                   flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r'<[^>]+>', '', text)
    text = _html.unescape(text)
    return _re.sub(r'\s+', ' ', text).strip()


def _dedupe(results: list[dict], max_results: int) -> list[dict]:
    """Remove duplicates by URL, cap at max_results."""
    seen = set()
    out = []
    for item in results:
        url = item.get("url", "").strip()
        title = item.get("title", "").strip()
        if not url or not title or url in seen:
            continue
        seen.add(url)
        out.append(item)
        if len(out) >= max_results:
            break
    return out


# ── Google search ────────────────────────────────────────────────────

# Markers that indicate Google consent/interstitial pages (no real results)
_CONSENT_MARKERS = [
    "consent.google.com",
    "Sorry, images are not yet available",
    "Before you continue to Google Search",
    "Our privacy policy has changed",
]


def _detect_consent(html: str) -> bool:
    """Check if Google returned a consent/interstitial page."""
    lower = _strip_scripts(html).lower()
    # Consent pages are typically large but have no result divs
    has_results = 'id="search"' in lower or 'class="g"' in lower
    if has_results:
        return False
    for marker in _CONSENT_MARKERS:
        if marker.lower() in lower:
            return True
    # If page is large but has no search results div, likely consent
    if len(html) > 50000 and 'id="search"' not in lower and 'class="g"' not in lower:
        return True
    return False


def _strip_scripts(html: str) -> str:
    """Drop <script>/<style> bodies before looking for page-level markers.

    Ordinary result pages carry inline configuration that mentions challenges
    by name — Bing ships a "captchaSuccessPostMessage" key on every search —
    so scanning raw HTML for the word reports a CAPTCHA on pages that are
    serving perfectly good results.
    """
    for tag in ("script", "style"):
        html = _re.sub(rf'<{tag}\b[^>]*>.*?</{tag}>', ' ', html,
                       flags=_re.DOTALL | _re.IGNORECASE)
    return html


def _detect_captcha(html: str, engine: str) -> bool:
    """Check if the page is a CAPTCHA challenge."""
    lower = _strip_scripts(html).lower()
    captcha_markers = [
        "captcha",
        "unusual traffic",
        "select all squares",
        "select all images",
        "are you a robot",
        "verify you are human",
        "recaptcha",
        "am i a robot",
    ]
    # DuckDuckGo specific
    if engine == "duckduckgo":
        captcha_markers.append("squares containing a duck")
        captcha_markers.append("anunusual request")
    for marker in captcha_markers:
        if marker in lower:
            return True
    return False


def _parse_google(html: str, max_results: int) -> list[dict]:
    """Parse Google search results from HTML."""
    results = []

    # Google results are in <div class="g"> or <div class="tF2Cxc"> blocks.
    # The class must match as a whole space-separated token: a substring match
    # would also split on every class merely *containing* a "g" ("logo",
    # "heading", "image"), shredding each result into fragments and losing the
    # snippet that follows its title.
    blocks = _re.split(
        r'<div[^>]*class="(?:[^"]*\s)?(?:g|tF2Cxc)(?:\s[^"]*)?"[^>]*>', html)

    for block in blocks:
        # Extract title + link from <a href="..." > ... </a> within <h3>
        title_m = _re.search(
            r'<a[^>]*href="(/url\?q=)?([^"&]+)[^"]*"[^>]*>\s*'
            r'<(?:h3|span)[^>]*>(.*?)</(?:h3|span)>',
            block, _re.DOTALL | _re.IGNORECASE)
        if not title_m:
            # Fallback: any <a> with <h3>
            title_m = _re.search(
                r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                block, _re.DOTALL | _re.IGNORECASE)
            if title_m:
                href = _html.unescape(title_m.group(1))
                title_html = title_m.group(2)
            else:
                continue
        else:
            # If it's a /url?q= redirect, extract the actual URL
            if title_m.group(1):  # /url?q= prefix
                raw_url = title_m.group(2)
                # The URL might be truncated by our regex; re-extract properly
                full_m = _re.search(
                    r'<a[^>]*href="/url\?q=([^&"]+)', block, _re.IGNORECASE)
                href = _html.unescape(full_m.group(1)) if full_m else raw_url
            else:
                href = _html.unescape(title_m.group(2))
            title_html = title_m.group(3)

        title = _clean_text(title_html)
        if not title:
            continue

        # Skip Google-internal links
        if href.startswith("/") or "google.com" in href:
            continue

        # Extract snippet
        snippet = ""
        snippet_m = _re.search(
            r'<span[^>]*class="[^"]*(?:aCOpRe|st|IsZvec)[^"]*"[^>]*>(.*?)</span>',
            block, _re.DOTALL)
        if snippet_m:
            snippet = _clean_text(snippet_m.group(1))[:500]
        else:
            # Fallback: any <span> with meaningful text after the link
            snippet_m = _re.search(r'<span[^>]*>(.*?)</span>', block, _re.DOTALL)
            if snippet_m:
                snippet = _clean_text(snippet_m.group(1))[:500]

        results.append({"title": title, "url": href, "snippet": snippet})

    return _dedupe(results, max_results)


def _search_google(query: str, max_results: int,
                   region: str | None = None,
                   timelimit: str | None = None,
                   ) -> tuple[list[dict], SearchErrorType | None]:
    """Search Google via HTML scraping. Returns (results, error_or_None)."""
    language, country = _laintas_locale(region)
    params = {
        "q": query,
        "num": str(min(max_results + 2, 20)),
        "hl": language if language != "auto" else "en",
    }
    if country != "any":
        params["cr"] = "country" + country
    if language != "auto":
        params["lr"] = "lang_" + language
    tbs = {"d": "qdr:d", "w": "qdr:w", "m": "qdr:m", "y": "qdr:y"}.get(
        str(timelimit or "").strip().lower())
    if tbs:
        params["tbs"] = tbs
    url = "https://www.google.com/search?" + urllib.parse.urlencode(params)

    resp, err, _session = _request("GET", url, host="www.google.com",
                                   timeout=_WEB_FETCH_TIMEOUT, allow_redirects=True)
    if err is not None:
        return [], err

    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED

    html = resp.text

    if _detect_captcha(html, "google"):
        return [], SearchErrorType.CAPTCHA
    if _detect_consent(html):
        return [], SearchErrorType.CONSENT_PAGE

    results = _parse_google(html, max_results)
    if not results:
        return [], SearchErrorType.EMPTY

    return results, None


# ── DuckDuckGo search ────────────────────────────────────────────────


_DDG_TITLE_RE = _re.compile(
    r'<a[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
    _re.DOTALL)
_DDG_SNIPPET_RE = _re.compile(
    r'<[^>]*class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</(?:a|div)>',
    _re.DOTALL)


def _parse_duckduckgo(html: str, max_results: int) -> list[dict]:
    """Parse DuckDuckGo HTML search results.

    Titles and snippets are paired by document order rather than by splitting
    on the result container. Splitting there is what went wrong before: the
    container class was matched as a substring, so inner elements like
    result__extras and result__url counted as boundaries too, and every snippet
    ended up in a different fragment from its title. Every result then came
    back with an empty snippet — still a list of links, but with nothing for
    the model to judge which link is worth fetching.
    """
    titles = [(m.start(), m.group(1), m.group(2)) for m in _DDG_TITLE_RE.finditer(html)]
    snippets = [(m.start(), m.group(1)) for m in _DDG_SNIPPET_RE.finditer(html)]

    results = []
    for index, (position, raw_href, title_html) in enumerate(titles):
        next_position = titles[index + 1][0] if index + 1 < len(titles) else len(html)
        snippet = ""
        for snippet_position, snippet_html in snippets:
            if position < snippet_position < next_position:
                snippet = _clean_text(snippet_html)[:500]
                break

        href = _html.unescape(raw_href)
        # DDG wraps outbound links as /l/?uddg=<encoded target>.
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            href = qs["uddg"][0]

        results.append({
            "title": _clean_text(title_html),
            "url": href,
            "snippet": snippet,
        })
    return _dedupe(results, max_results)


def _search_duckduckgo(query: str, max_results: int,
                       region: str | None = None,
                       timelimit: str | None = None,
                       ) -> tuple[list[dict], SearchErrorType | None]:
    """Search DuckDuckGo via HTML scraping."""
    form = {"q": query}
    region_code = str(region or "").strip().lower()
    if region_code and region_code != "wt-wt" and _re.fullmatch(r"[a-z]{2}-[a-z]{2}", region_code):
        form["kl"] = region_code
    date_filter = str(timelimit or "").strip().lower()
    if date_filter in ("d", "w", "m", "y"):
        form["df"] = date_filter
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})

    resp, err, _session = _request("POST", url, host="html.duckduckgo.com",
                                   timeout=_WEB_FETCH_TIMEOUT,
                                   allow_redirects=True, data=form)
    if err is not None:
        return [], err

    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED

    html = resp.text

    if _detect_captcha(html, "duckduckgo"):
        return [], SearchErrorType.CAPTCHA

    results = _parse_duckduckgo(html, max_results)
    if not results:
        return [], SearchErrorType.EMPTY

    return results, None


# ── cn.bing search ───────────────────────────────────────────────────
#
# Kept for users inside China, where Google is unreachable and DuckDuckGo is
# unreliable. Ranked below both because its result quality is worse, but for
# the network conditions it serves it is often the only one of the three that
# answers at all.

# Bing decides whether to render results at all from the fetch-metadata
# headers a real navigation carries. Without them it answers 200 with the page
# frame and an empty result list — measured here as 0 results with them absent
# and 10 with them present, on the same query and IP. The UA platform makes no
# difference; these headers do.
_BING_HEADERS = {
    "Referer": "https://cn.bing.com/",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_BING_ITEM_RE = _re.compile(
    r'<li[^>]*class="(?:[^"]*\s)?b_algo(?:\s[^"]*)?"[^>]*>', _re.IGNORECASE)
_BING_TITLE_RE = _re.compile(
    r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>', _re.DOTALL)
_BING_SNIPPET_RE = _re.compile(r'<p[^>]*>(.*?)</p>', _re.DOTALL)


def _parse_bing(html: str, max_results: int) -> list[dict]:
    """Parse cn.bing result blocks.

    The class is matched as a whole token for the same reason as the other two
    parsers: a substring match splits on every unrelated class that happens to
    contain the marker, separating each title from its snippet.
    """
    results = []
    for block in _BING_ITEM_RE.split(html):
        title_m = _BING_TITLE_RE.search(block)
        if not title_m:
            continue
        snippet_m = _BING_SNIPPET_RE.search(block)
        results.append({
            "title": _clean_text(title_m.group(2)),
            "url": _html.unescape(title_m.group(1)),
            "snippet": _clean_text(snippet_m.group(1))[:500] if snippet_m else "",
        })
    return _dedupe(results, max_results)


def _search_bing(query: str, max_results: int,
                 region: str | None = None,
                 timelimit: str | None = None,
                 ) -> tuple[list[dict], SearchErrorType | None]:
    """Search cn.bing via HTML scraping."""
    language, country = _laintas_locale(region)
    market = "zh-CN"
    if country != "any" and language != "auto":
        market = f"{language}-{country}"
    params = {"q": query, "mkt": market}
    # Bing's freshness filter: ez1 = past day, ez2 = past week, ez3 = past
    # month. It has no equivalent for "past year", so that one is left off
    # rather than silently narrowed to a month.
    freshness = {"d": "ez1", "w": "ez2", "m": "ez3"}.get(
        str(timelimit or "").strip().lower())
    if freshness:
        params["filters"] = 'ex1:"%s"' % freshness
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode(params)

    resp, err, _session = _request("GET", url, host="cn.bing.com",
                                   timeout=_WEB_FETCH_TIMEOUT, allow_redirects=True,
                                   headers=_BING_HEADERS)
    if err is not None:
        return [], err

    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED

    html = resp.text
    if _detect_captcha(html, "bing"):
        return [], SearchErrorType.CAPTCHA

    results = _parse_bing(html, max_results)
    if not results:
        return [], SearchErrorType.EMPTY
    return results, None


# ── laintas_search API ───────────────────────────────────────────────


def _get_laintas_api_key() -> str | None:
    """Get laintas_search API key from /config or env."""
    key = _get_config("search_laintas_api_key", "")
    if key:
        return str(key)
    return os.environ.get("LAINTAS_SEARCH_API_KEY") or None


def _get_laintas_api_url() -> str:
    """Get laintas_search API base URL."""
    url = _get_config("search_laintas_api_url", "")
    if url:
        return str(url).rstrip("/")
    return os.environ.get("LAINTAS_SEARCH_API_URL", "https://search.laintas.com").rstrip("/")


# The API validates its payload against a strict allowlist and rejects the whole
# request (HTTP 400) on any unknown key or out-of-set value. These mirror the
# server's accepted sets so a bad locale degrades to the default instead of
# killing the request. Keep in sync with laintas_search/security.py.
_LAINTAS_LANGUAGES = frozenset({"auto", "zh", "en", "ja", "ko", "de", "fr", "es", "ru"})
_LAINTAS_COUNTRIES = frozenset({"any", "CN", "US", "GB", "JP", "DE", "FR", "CA", "AU"})
_LAINTAS_TIME_RANGES = frozenset({"any", "day", "week", "month", "year"})
_LAINTAS_MAX_QUERY_CHARS = 500

# Same rejection rules the server applies to the query string. Mirrored here so
# the failure is a clear message instead of an opaque 400.
_LAINTAS_CONTROL_RE = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LAINTAS_OVERRIDE_RE = _re.compile(r"(^|\s)[!@:][^\s]+")


def _laintas_locale(region: str | None) -> tuple[str, str]:
    """Map a "cc-ll" region code (e.g. "cn-zh") to (language, country)."""
    value = str(region or "").strip().lower()
    if not value or value == "wt-wt":
        return "auto", "any"
    parts = value.split("-", 1)
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
        language = parts[1] if parts[1] in _LAINTAS_LANGUAGES else "auto"
        country = parts[0].upper()
        if country not in _LAINTAS_COUNTRIES:
            country = "any"
        return language, country
    return "auto", "any"


def _laintas_time_range(timelimit: str | None) -> str:
    return {"d": "day", "w": "week", "m": "month", "y": "year"}.get(
        str(timelimit or "").strip().lower(), "any")


def _laintas_query_problem(query: str) -> str | None:
    """Why the API would reject this query, or None if it would accept it."""
    if len(query) > _LAINTAS_MAX_QUERY_CHARS:
        return (f"query is {len(query)} characters; laintas_search accepts at most "
                f"{_LAINTAS_MAX_QUERY_CHARS}")
    if _LAINTAS_CONTROL_RE.search(query):
        return "query contains control characters, which laintas_search rejects"
    if _LAINTAS_OVERRIDE_RE.search(query):
        return ("query contains a token starting with '!', '@' or ':', which "
                "laintas_search rejects as an engine-override attempt — drop it "
                "and search for the plain terms")
    return None


def _search_laintas(query: str, max_results: int,
                    region: str | None = None,
                    timelimit: str | None = None,
                    ) -> tuple[list[dict], SearchErrorType | None, str]:
    """Search via laintas_search API. Returns (results, error_or_None, message)."""
    api_key = _get_laintas_api_key()
    if not api_key:
        return [], SearchErrorType.API_ERROR, "No laintas_search API key configured (set /config search_laintas_api_key or LAINTAS_SEARCH_API_KEY env)"

    problem = _laintas_query_problem(query)
    if problem:
        return [], SearchErrorType.API_ERROR, f"laintas_search would reject this query: {problem}"

    api_url = _get_laintas_api_url()
    url = api_url + "/search"

    language, country = _laintas_locale(region)
    time_range = _laintas_time_range(timelimit)

    # The API takes exactly these five keys — any extra key is a 400.
    payload = {
        "query": query,
        "maxResults": max_results,
        "language": language,
        "country": country,
        "timeRange": time_range,
    }

    # laintas_search runs its own server-side proxy pool, so its API is normally
    # reached directly. Users behind a national firewall may not be able to
    # reach it at all without their proxy, so a connection failure is retried
    # through the configured proxy before giving up.
    session = requests.Session()
    session.headers.update({
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
        # Lets the server collapse a retried request instead of billing it twice.
        "Idempotency-Key": uuid.uuid4().hex,
    })

    try:
        resp = session.post(url, json=payload, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        proxy = _get_proxy()
        if not proxy:
            return [], SearchErrorType.NETWORK, f"laintas_search request failed: {e}"
        session.proxies = {"http": proxy, "https": proxy}
        try:
            resp = session.post(url, json=payload, timeout=30, allow_redirects=True)
        except requests.RequestException as e2:
            return [], _classify_request_error(e2), (
                f"laintas_search unreachable directly ({e}) and via the configured "
                f"proxy ({e2})")

    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED, "laintas_search rate limited (429)"
    if resp.status_code == 401:
        return [], SearchErrorType.API_ERROR, "laintas_search API key invalid (401)"
    if resp.status_code == 402:
        return [], SearchErrorType.API_ERROR, "laintas_search quota exceeded (402)"
    if resp.status_code == 403:
        return [], SearchErrorType.API_ERROR, "laintas_search access forbidden (403)"
    if resp.status_code == 503:
        return [], SearchErrorType.API_ERROR, "laintas_search service unavailable (503)"
    if resp.status_code >= 400:
        msg = ""
        try:
            body = resp.json()
            msg = body.get("error", body.get("message", ""))
        except Exception:
            msg = resp.text[:200]
        return [], SearchErrorType.API_ERROR, f"laintas_search error ({resp.status_code}): {msg}"

    try:
        data = resp.json()
    except Exception as e:
        return [], SearchErrorType.API_ERROR, f"laintas_search returned non-JSON response: {e}"

    raw_results = data.get("results", [])
    if not raw_results:
        return [], SearchErrorType.EMPTY, "laintas_search returned no results"

    # Normalize laintas_search results to {title, url, snippet}. The API's own
    # field is "snippet"; "content" is only a fallback for other shapes. "date"
    # and "trust" are carried through — date is the only recency signal a result
    # carries, and trust marks the text as untrusted external content.
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url_val = item.get("url", item.get("link", ""))
        snippet = item.get("snippet", item.get("content", ""))
        if title and url_val:
            entry = {
                "title": str(title).strip(),
                "url": str(url_val).strip(),
                "snippet": str(snippet).strip()[:500] if snippet else "",
            }
            date = item.get("date")
            if isinstance(date, str) and date.strip():
                entry["date"] = date.strip()
            trust = item.get("trust")
            if isinstance(trust, str) and trust.strip():
                entry["trust"] = trust.strip()
            results.append(entry)

    results = _dedupe(results, max_results)
    if not results:
        return [], SearchErrorType.EMPTY, "laintas_search returned no usable results"

    return results, None, ""


# ── Tavily Keyless (no API key, zero-config) ─────────────────────────


def _search_tavily_keyless(query: str, max_results: int,
                           region: str | None = None,
                           timelimit: str | None = None,
                           ) -> tuple[list[dict], SearchErrorType | None, str]:
    """Search via Tavily's keyless API endpoint.

    No account or API key required — the ``X-Tavily-Access-Mode: keyless``
    header enables free, rate-limited access.  Results are structured for
    LLM consumption (ranked, scored snippets) rather than raw HTML, so
    this engine gives the best signal-per-token of all the free tiers.
    """
    payload: dict = {"query": query, "max_results": min(max_results, 10)}
    headers = {
        "Content-Type": "application/json",
        "X-Tavily-Access-Mode": "keyless",
    }

    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json=payload, headers=headers,
            timeout=_WEB_FETCH_TIMEOUT, allow_redirects=True,
        )
    except requests.RequestException as e:
        return [], _classify_request_error(e), f"tavily keyless request failed: {e}"

    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED, "tavily keyless rate limited (429)"
    if resp.status_code >= 400:
        msg = ""
        try:
            msg = resp.json().get("detail", resp.text[:200])
        except Exception:
            msg = resp.text[:200]
        return [], SearchErrorType.API_ERROR, f"tavily keyless error ({resp.status_code}): {msg}"

    try:
        data = resp.json()
    except ValueError:
        return [], SearchErrorType.API_ERROR, "tavily keyless returned non-JSON"

    raw = data.get("results") or []
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url_val = item.get("url", "")
        snippet = item.get("content", item.get("snippet", ""))
        if title and url_val:
            results.append({
                "title": str(title).strip(),
                "url": str(url_val).strip(),
                "snippet": str(snippet).strip()[:500] if snippet else "",
            })

    results = _dedupe(results, max_results)
    if not results:
        return [], SearchErrorType.EMPTY, "tavily keyless returned no usable results"
    return results, None, ""


# ── Engine chain ─────────────────────────────────────────────────────


# ── laintas_search through the signed-in account (no API key) ────────


def _laintas_key_available() -> str:
    """Empty when this engine can run, else why it cannot."""
    if _get_laintas_api_key():
        return ""
    return ("no laintas_search API key configured "
            "(/config search_laintas_api_key, or LAINTAS_SEARCH_API_KEY)")


def _gateway_session() -> tuple[str, dict, dict] | None:
    """(base_url, headers, cookies) for the signed-in Laintas session, or None.

    The gateway verifies the same session cookie the CLI already holds, so
    this tier needs no key of its own — but it only exists while signed in,
    and it must never send those credentials to a backend the user pointed at
    something other than Laintas.
    """
    try:
        import json as _json
        import backend_profiles
        from paths import SESSION_FILE
        if not SESSION_FILE.exists():
            return None
        session = _json.loads(SESSION_FILE.read_text())
    except Exception:
        return None
    if not session:
        return None
    try:
        profile = backend_profiles.resolve(
            os.environ.get("LAINTAS_BACKEND") or "https://laintas.com")
        if not profile.sends_laintas_credentials:
            return None
        headers, cookies = backend_profiles.request_auth(profile, session)
    except Exception:
        return None
    if not headers.get("Authorization") and not cookies:
        return None
    return profile.base_url.rstrip("/"), headers, cookies


def _gateway_available() -> str:
    if _gateway_session() is None:
        return "not signed in to Laintas (run /login) — this tier uses your account, not a key"
    return ""


#: The signed-in Laintas session, for other code that talks to the gateway with
#: the same credential — the bundled `code-map` extension does, and a second
#: copy of this logic would be a second place to get "never send these to a
#: non-Laintas backend" wrong. Extensions reach it through a lazy import, so
#: this stays a one-way dependency: core never imports an extension.
laintas_session = _gateway_session


def _search_laintas_gateway(query: str, max_results: int,
                            region: str | None = None,
                            timelimit: str | None = None,
                            ) -> tuple[list[dict], SearchErrorType | None, str]:
    """Search laintas_search through the gateway, billed to the account balance.

    Last in the default chain because it is the only tier that spends the
    user's balance — not because it is the weakest. It is usually the best
    results of the lot.
    """
    auth = _gateway_session()
    if auth is None:
        return [], SearchErrorType.API_ERROR, "not signed in to Laintas"
    base_url, headers, cookies = auth

    problem = _laintas_query_problem(query)
    if problem:
        return [], SearchErrorType.API_ERROR, f"laintas_gateway would reject this query: {problem}"

    # The gateway takes the region/timelimit vocabulary and maps it to the
    # search service's language/country/timeRange itself, so the mapping lives
    # in exactly one place rather than being done here and undone there.
    payload = {
        "query": query,
        "maxResults": max_results,
        "region": region or "wt-wt",
        "source": "cli",
    }
    if timelimit:
        payload["timelimit"] = timelimit
    request_headers = dict(headers)
    request_headers["Content-Type"] = "application/json"
    request_headers["Idempotency-Key"] = uuid.uuid4().hex

    try:
        resp = requests.post(f"{base_url}/api/agent/search", json=payload,
                             headers=request_headers, cookies=cookies,
                             timeout=30, allow_redirects=False)
    except requests.RequestException as e:
        return [], _classify_request_error(e), f"gateway request failed: {e}"

    if resp.status_code == 401:
        return [], SearchErrorType.API_ERROR, "Laintas session expired — run /login"
    if resp.status_code == 402:
        return [], SearchErrorType.API_ERROR, "insufficient Laintas balance for a search"
    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED, "gateway rate limited (429)"
    if resp.status_code == 404:
        return [], SearchErrorType.API_ERROR, (
            "this Laintas backend has no /api/agent/search endpoint yet")
    if resp.status_code >= 400:
        return [], SearchErrorType.API_ERROR, f"gateway error (HTTP {resp.status_code})"

    try:
        data = resp.json()
    except ValueError:
        return [], SearchErrorType.API_ERROR, "gateway returned a non-JSON response"

    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        link = item.get("url", "")
        if not title or not link:
            continue
        entry = {
            "title": str(title).strip(),
            "url": str(link).strip(),
            "snippet": str(item.get("snippet") or "").strip()[:500],
        }
        for key in ("date", "trust"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        results.append(entry)

    results = _dedupe(results, max_results)
    if not results:
        return [], SearchErrorType.EMPTY, "gateway returned no usable results"
    return results, None, ""


# ── Declarative JSON engines ─────────────────────────────────────────


def _template(value, context: dict):
    """Substitute ${query}, ${env:VAR} etc. through a spec value of any shape."""
    if isinstance(value, str):
        def replace(match):
            key = match.group(1)
            if key.startswith("env:"):
                return os.environ.get(key[4:], "")
            return str(context.get(key, ""))
        rendered = _re.sub(r"\$\{([A-Za-z0-9_:]+)\}", replace, value)
        # A bare "${max_results}" should stay a number for APIs that demand one.
        if (value.strip() == "${max_results}") and rendered.isdigit():
            return int(rendered)
        return rendered
    if isinstance(value, dict):
        return {k: _template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_template(v, context) for v in value]
    return value


def _dig(data, path: str):
    """Follow a dotted path through nested dicts/lists."""
    current = data
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _make_json_driver(spec: dict):
    """Build a driver for a declarative JSON search API."""

    def driver(query, max_results, region, timelimit):
        language, country = _laintas_locale(region)
        context = {
            "query": query,
            "max_results": max_results,
            "language": language,
            "country": country,
            "time_range": _laintas_time_range(timelimit),
            "region": region or "",
            "timelimit": timelimit or "",
        }
        url = str(_template(spec.get("url", ""), context))
        # A user-defined engine is still an outbound URL this process will send
        # every query to, so it goes through the same SSRF rules as any fetch.
        # Reaching an intranet search box is a legitimate use, but it has to be
        # asked for explicitly rather than arrived at by accident.
        try:
            _guard_url(url, via_proxy=bool(_proxy_for_host(_host_of(url))))
        except PrivateAddressRefused as e:
            if not spec.get("allow_private"):
                return [], SearchErrorType.API_ERROR, (
                    f"{spec.get('name')}: refusing to call {url} — {e}. Set "
                    f'"allow_private": true on this engine if that is intended.')
        except ValueError as e:
            # A malformed URL or a name that will not resolve. Not a security
            # decision, so it is left to fail as an ordinary request error
            # rather than being reported as a blocked private address.
            if "://" not in url:
                return [], SearchErrorType.API_ERROR, f"{spec.get('name')}: {e}"

        method = str(spec.get("method") or "GET").upper()
        headers = _template(spec.get("headers") or {}, context)
        kwargs = {"timeout": int(spec.get("timeout") or 20), "allow_redirects": True}
        if headers:
            kwargs["headers"] = {str(k): str(v) for k, v in headers.items()}
        if method == "GET":
            params = _template(spec.get("params") or {}, context)
            if params:
                url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        else:
            body = _template(spec.get("body") or {}, context)
            if body:
                kwargs["json"] = body

        resp, err, _session = _request(method, url, host=_host_of(url), **kwargs)
        if err is not None:
            return [], err, f"{spec.get('name')}: request failed ({err.value})"
        if resp.status_code == 429:
            return [], SearchErrorType.RATE_LIMITED, f"{spec.get('name')}: rate limited (429)"
        if resp.status_code in (401, 403):
            return [], SearchErrorType.API_ERROR, (
                f"{spec.get('name')}: credentials rejected (HTTP {resp.status_code})")
        if resp.status_code == 402:
            return [], SearchErrorType.API_ERROR, f"{spec.get('name')}: quota exhausted (402)"
        if resp.status_code >= 400:
            return [], SearchErrorType.API_ERROR, (
                f"{spec.get('name')}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return [], SearchErrorType.API_ERROR, f"{spec.get('name')}: response was not JSON"

        raw = _dig(data, spec.get("results_path") or "results")
        if not isinstance(raw, list):
            return [], SearchErrorType.EMPTY, (
                f"{spec.get('name')}: no list at results_path "
                f"'{spec.get('results_path') or 'results'}'")

        fields = spec.get("fields") or {}
        title_key = str(fields.get("title") or "title")
        url_key = str(fields.get("url") or "url")
        snippet_key = str(fields.get("snippet") or "snippet")
        date_key = str(fields.get("date") or "date")

        results = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = _dig(item, title_key)
            link = _dig(item, url_key)
            if not title or not link:
                continue
            entry = {
                "title": str(title).strip(),
                "url": str(link).strip(),
                "snippet": str(_dig(item, snippet_key) or "").strip()[:500],
            }
            date = _dig(item, date_key)
            if isinstance(date, str) and date.strip():
                entry["date"] = date.strip()
            results.append(entry)

        results = _dedupe(results, max_results)
        if not results:
            return [], SearchErrorType.EMPTY, f"{spec.get('name')}: returned no usable results"
        return results, None, ""

    return driver


# ── Engine registry ──────────────────────────────────────────────────
#
# Engines are table entries rather than branches in a chain, so a user can add
# one their network likes without touching this file. Built-ins carry a Python
# driver; user entries in ~/.laintas/search_engines.json are declarative JSON
# APIs (serper, tavily, an extra laintas key, a self-hosted instance).
#
# Deliberately NOT extensible with scraped-HTML engines: those need a parser
# per site, and the two we maintain by hand have both had extraction bugs. A
# declarative selector spec would not make them less brittle, it would only
# move the breakage to the user's config.

_ENGINE_ALIASES = {
    "ddg": "duckduckgo",
    "duck": "duckduckgo",
    "bing": "cn-bing",
    "bing-cn": "cn-bing",
    "cn.bing": "cn-bing",
    "cn_bing": "cn-bing",
    "laintas": "laintas_search",
    "laintas-search": "laintas_search",
    "gateway": "laintas_gateway",
    "laintas-gateway": "laintas_gateway",
    "laintas-search-gateway": "laintas_gateway",
}

# Order matters: free scrapers first, then the user's own metered credits,
# then the account balance. This is a cost ordering, not a quality ordering —
# the last entry is usually the *best* results, just the only one that bills.
_DEFAULT_CHAIN = ["tavily", "google", "duckduckgo", "cn-bing",
                  "laintas_search", "laintas_gateway"]


def _builtin_engines() -> dict:
    """The engines that ship with the CLI, each with a Python driver."""
    return {
        "tavily": {
            "name": "tavily", "kind": "builtin", "cost": "free",
            "driver": _search_tavily_keyless,
            "describe": "Tavily Keyless — AI-optimized search with no API key; "
                        "returns clean, scored results built for LLM consumption",
        },
        "google": {
            "name": "google", "kind": "builtin", "cost": "free",
            "driver": lambda q, n, r, t: _search_google(q, n, r, t) + ("",),
            "describe": "Google via HTML scraping; blocked from many server IPs",
        },
        "duckduckgo": {
            "name": "duckduckgo", "kind": "builtin", "cost": "free",
            "driver": lambda q, n, r, t: _search_duckduckgo(q, n, r, t) + ("",),
            "describe": "DuckDuckGo via HTML scraping",
        },
        "cn-bing": {
            "name": "cn-bing", "kind": "builtin", "cost": "free",
            "driver": lambda q, n, r, t: _search_bing(q, n, r, t) + ("",),
            "describe": "cn.bing.com; reachable from inside China where the other two are not",
        },
        "laintas_search": {
            "name": "laintas_search", "kind": "builtin", "cost": "metered",
            "driver": _search_laintas,
            "available": _laintas_key_available,
            "describe": "laintas_search API with your own key; merges many engines server-side",
        },
        "laintas_gateway": {
            "name": "laintas_gateway", "kind": "builtin", "cost": "metered",
            "driver": _search_laintas_gateway,
            "available": _gateway_available,
            "describe": "laintas_search through your signed-in Laintas account; "
                        "needs no key, bills your account balance",
        },
    }


_REGISTRY_CACHE: dict = {"mtime": None, "entries": {}, "errors": []}
_REGISTRY_LOCK = threading.Lock()


def _registry_path():
    try:
        from paths import LAINTAS_HOME
        return LAINTAS_HOME / "search_engines.json"
    except Exception:
        from pathlib import Path
        return Path(os.environ.get(
            "LAINTAS_HOME", str(Path.home() / ".laintas"))) / "search_engines.json"


def load_engine_registry() -> tuple[dict, list[str]]:
    """Built-in engines plus the user's JSON entries. Returns (entries, errors).

    mtime-cached like the other user-editable config files. The file is read,
    never written: an engine definition is a URL that every future query gets
    sent to, so it must come from the user and never from a page the agent
    read, a skill, or the agent itself.
    """
    entries = _builtin_engines()
    path = _registry_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return entries, []

    with _REGISTRY_LOCK:
        cached = _REGISTRY_CACHE
        if cached["mtime"] == mtime:
            entries.update(cached["entries"])
            return entries, list(cached["errors"])

    user_entries, errors = _parse_registry_file(path)
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE["mtime"] = mtime
        _REGISTRY_CACHE["entries"] = user_entries
        _REGISTRY_CACHE["errors"] = errors
    entries.update(user_entries)
    return entries, errors


def _parse_registry_file(path) -> tuple[dict, list[str]]:
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return {}, [f"{path.name}: {e}"]

    if isinstance(data, dict):
        data = data.get("engines", [])
    if not isinstance(data, list):
        return {}, [f"{path.name}: expected a list of engines"]

    entries: dict = {}
    errors: list[str] = []
    builtin = _builtin_engines()
    for raw in data:
        if not isinstance(raw, dict):
            errors.append("skipped a non-object engine entry")
            continue
        name = str(raw.get("name") or "").strip().lower()
        if not name:
            errors.append("skipped an engine with no name")
            continue
        if name in builtin:
            errors.append(f"{name}: cannot redefine a built-in engine")
            continue
        if str(raw.get("kind") or "json").lower() != "json":
            errors.append(f"{name}: only kind 'json' engines can be defined here")
            continue
        if not str(raw.get("url") or "").strip():
            errors.append(f"{name}: missing 'url'")
            continue
        entries[name] = {
            "name": name,
            "kind": "json",
            "cost": "metered" if str(raw.get("cost") or "metered").lower() == "metered" else "free",
            "spec": raw,
            "describe": str(raw.get("describe") or f"user-defined JSON engine {name}"),
            "driver": _make_json_driver(raw),
        }
    return entries, errors


_ENGINE_TEMPLATE = {
    "_comment": [
        "Extra search engines for web.search, tried in the order named by",
        "/config search_engine. Only JSON APIs can be defined here — scraped",
        "HTML engines need a parser per site and are built in, not configured.",
        "",
        "Template values: ${query} ${max_results} ${language} ${country}",
        "${time_range} ${region} ${timelimit}, and ${env:VAR} for secrets.",
        "Keep keys out of this file with ${env:...} where you can; the file is",
        "chmod 600 either way.",
        "",
        "cost: 'metered' engines are never put in a failure cooldown, so a",
        "billing or key problem surfaces on the next call instead of being",
        "silently skipped for five minutes.",
        "",
        "Set allow_private to true only for an engine on your own network:",
        "it waives the check that stops queries being sent to a private address.",
    ],
    "engines": [
        {
            "name": "serper",
            "kind": "json",
            "cost": "metered",
            "describe": "Serper.dev Google API",
            "url": "https://google.serper.dev/search",
            "method": "POST",
            "headers": {"X-API-KEY": "${env:SERPER_API_KEY}"},
            "body": {"q": "${query}", "num": "${max_results}"},
            "results_path": "organic",
            "fields": {"title": "title", "url": "link", "snippet": "snippet", "date": "date"},
        },
        {
            "name": "laintas-key2",
            "kind": "json",
            "cost": "metered",
            "describe": "A second laintas_search key, used when the first runs out",
            "url": "https://search.laintas.com/search",
            "method": "POST",
            "headers": {"X-API-KEY": "${env:LAINTAS_SEARCH_API_KEY_2}"},
            "body": {
                "query": "${query}",
                "maxResults": "${max_results}",
                "language": "${language}",
                "country": "${country}",
                "timeRange": "${time_range}",
            },
            "results_path": "results",
            "fields": {"title": "title", "url": "url", "snippet": "snippet", "date": "date"},
        },
    ],
}


def write_engine_template(overwrite: bool = False) -> tuple[bool, str]:
    """Write a starter ~/.laintas/search_engines.json. Returns (written, path)."""
    import json
    path = _registry_path()
    if path.exists() and not overwrite:
        return False, str(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_ENGINE_TEMPLATE, fh, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
    except OSError as e:
        return False, f"{path}: {e}"
    return True, str(path)


def canonical_engine(name: str) -> str:
    key = str(name or "").strip().lower()
    return _ENGINE_ALIASES.get(key, key)


def _configured_chain() -> list[str]:
    """The chain from /config search_engine, or the default cost ordering."""
    raw = str(_get_config("search_engine", "") or
              os.environ.get("LAINTAS_SEARCH_ENGINE", "") or "auto")
    parts = [canonical_engine(p) for p in raw.replace(",", " ").split() if p.strip()]
    if not parts or "auto" in parts:
        return list(_DEFAULT_CHAIN)
    return parts


def resolve_chain(requested=None) -> tuple[list[str], list[str]]:
    """Turn a caller's engine request into a validated chain of names.

    Returns (chain, warnings). An unknown name is reported rather than
    silently ignored — a caller that asked for a specific engine needs to know
    it did not run.
    """
    entries, _errors = load_engine_registry()
    if requested is None:
        names = _configured_chain()
    elif isinstance(requested, str):
        names = [canonical_engine(p) for p in requested.replace(",", " ").split() if p.strip()]
    else:
        names = [canonical_engine(p) for p in requested if str(p).strip()]

    if names == ["auto"] or not names:
        names = _configured_chain()

    chain, warnings = [], []
    for name in names:
        if name in entries:
            if name not in chain:
                chain.append(name)
        else:
            warnings.append(f"unknown engine '{name}' (known: {', '.join(sorted(entries))})")

    if not chain and requested is None:
        # Only a misconfigured chain falls back to the default. An explicit
        # request for engines that do not exist must fail loudly: quietly
        # searching somewhere else is how a caller ends up believing it used
        # the engine it named.
        chain = [n for n in _DEFAULT_CHAIN if n in entries]
    return chain, warnings


def engine_health() -> list[dict]:
    """What the model needs to pick an engine: what exists, what it costs,
    what is usable right now, and what is in cooldown after failing."""
    entries, _errors = load_engine_registry()
    out = []
    for name in sorted(entries):
        entry = entries[name]
        available = entry.get("available")
        reason = ""
        if callable(available):
            try:
                reason = available() or ""
            except Exception as e:
                reason = f"unavailable ({e})"
        out.append({
            "engine": name,
            "cost": entry.get("cost", "free"),
            "describe": entry.get("describe", ""),
            "usable": not reason and not _is_fast_failed(name),
            "reason": reason or ("in cooldown after a recent failure"
                                 if _is_fast_failed(name) else ""),
        })
    return out


def search(query: str, max_results: int = 10,
           engine: str | None = None,
           region: str | None = None,
           timelimit: str | None = None,
           engines=None, interrupt_event=None) -> dict:
    """Run the search engine chain.

    engines is an ordered list of engine names to try; engine is the older
    single-name form and means the same thing with one element. Omit both to
    use the configured chain.

    region is a "cc-ll" code ("cn-zh", "us-en"; "wt-wt" or None = no
    preference). timelimit is d/w/m/y for the last day/week/month/year.

    Returns a dict with:
      ok: bool
      result: list[dict] (on success)
      query: str
      count: int
      engine: str (which engine succeeded)
      errors: list[dict] (per-engine error info, on failure or fallback)
      error: str (final error message if all engines fail)
    """
    query = query.strip()
    if not query:
        return {"ok": False, "error": "missing 'query'"}

    max_results = min(max(max_results, 1), 20)
    if timelimit is not None:
        timelimit = str(timelimit).strip().lower() or None
        if timelimit not in ("d", "w", "m", "y", None):
            timelimit = None

    entries, registry_errors = load_engine_registry()
    requested = engines if engines is not None else engine
    chain, warnings = resolve_chain(requested)

    errors = [{"engine": "(config)", "error": SearchErrorType.API_ERROR.value,
               "message": message}
              for message in list(registry_errors) + list(warnings)]

    if not chain:
        detail = "; ".join(warnings) if warnings else "no search engines configured"
        return {
            "ok": False,
            "query": query,
            "error": f"no usable search engine: {detail}",
            "errors": errors,
            "engines_available": engine_health(),
        }

    for name in chain:
        entry = entries.get(name)
        if entry is None:
            continue

        # An engine that cannot run at all (no key, not signed in) is not a
        # failure — it is simply not part of this user's chain. Reported, but
        # never put into cooldown, since nothing was tried.
        available = entry.get("available")
        if callable(available):
            try:
                reason = available()
            except Exception as e:
                reason = f"availability check failed: {e}"
            if reason:
                errors.append({"engine": name, "error": "unavailable", "message": reason})
                continue

        if _is_fast_failed(name):
            errors.append({
                "engine": name,
                "error": SearchErrorType.NETWORK.value,
                "message": f"{name} skipped (recently failed, in cooldown)",
            })
            continue

        if interrupt_event is not None and interrupt_event.is_set():
            raise InterruptedError("interrupted by user")
        try:
            results, err, msg = entry["driver"](query, max_results, region, timelimit)
        except Exception as e:
            results, err, msg = [], SearchErrorType.API_ERROR, f"{type(e).__name__}: {e}"

        if err is None:
            return {
                "ok": True,
                "result": results,
                "query": query,
                "count": len(results),
                "engine": name,
                "cost": entry.get("cost", "free"),
                "errors": errors if errors else None,
            }

        errors.append({
            "engine": name,
            "error": err.value,
            "message": msg or _error_message(name, err),
        })
        # Metered engines are not cooled down on failure: their failures are
        # usually about the account (no credit, bad key), and silently skipping
        # them for five minutes would hide that from the next call.
        if entry.get("cost") != "metered":
            _mark_fast_fail(name)

    error_parts = [f"{e['engine']}: {e['error']}" for e in errors]
    return {
        "ok": False,
        "error": "All search engines failed; " + " | ".join(error_parts),
        "query": query,
        "errors": errors,
        "engines_available": engine_health(),
    }


def _error_message(engine: str, err: SearchErrorType) -> str:
    """Human-readable error message for an engine failure."""
    messages = {
        SearchErrorType.CAPTCHA: f"{engine} returned a CAPTCHA challenge page",
        SearchErrorType.RATE_LIMITED: f"{engine} rate limited (HTTP 429)",
        SearchErrorType.NETWORK: f"{engine} network error (connection failed or timed out)",
        SearchErrorType.PROXY_ERROR: f"{engine} proxy connection failed",
        SearchErrorType.CONSENT_PAGE: f"{engine} returned a consent/interstitial page (no results)",
        SearchErrorType.EMPTY: f"{engine} returned no parseable results",
        SearchErrorType.DEGRADED: f"{engine} results degraded (low relevance)",
        SearchErrorType.API_ERROR: f"{engine} API error",
    }
    return messages.get(err, f"{engine} failed with {err.value}")


# ── URL safety ───────────────────────────────────────────────────────

_MAX_REDIRECTS = 6

# Host names that always denote the local machine or a private namespace, and
# so must never be fetched no matter what DNS says.
_PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


class PrivateAddressRefused(ValueError):
    """The URL points at this machine's own network position.

    Distinct from every other reason a URL is rejected, because it is the only
    one a user can legitimately override (an intranet search box), and the only
    one where proceeding would be a security problem rather than just a failed
    request. A name that merely does not resolve is not this.
    """


def _ip_is_blocked(ip_str: str) -> bool:
    """True for loopback / private / link-local / reserved addresses.

    Mirrors browser_session._ip_is_blocked. web.fetch runs on the *user's own
    machine*, so an unguarded fetch is a way to reach their LAN, their router
    and cloud metadata at 169.254.169.254 — reachable from here but not from
    the internet.
    """
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # un-parseable → refuse
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _guard_url(raw: str, *, via_proxy: bool) -> str:
    """Security-check a URL before fetching it. Returns it, or raises ValueError.

    Every redirect hop goes through here too: checking only the URL the caller
    supplied would let any site bounce us to http://192.168.1.1/ with a 302.

    When the request egresses through a proxy, the name is resolved by the
    proxy, not here, so DNS-based checks are advisory: a local lookup that
    *succeeds* and yields a private address still refuses, but a lookup that
    fails is not treated as fatal (the proxy may resolve names this host
    cannot). Literal-IP and private-suffix checks always apply.
    """
    import ipaddress
    from urllib.parse import urlparse

    s = (raw or "").strip()
    if not s:
        raise ValueError("a URL is required")
    parts = urlparse(s)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs may be fetched (got '{parts.scheme or 'no scheme'}')")
    if parts.username or parts.password:
        raise ValueError("URLs containing embedded credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError(f"not a valid URL: {raw!r}")
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        raise PrivateAddressRefused(f"refusing to fetch {host!r}: private host name")

    # A literal IP needs no DNS and is checked the same way with or without a proxy.
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(str(literal)):
            raise PrivateAddressRefused(
                f"refusing to fetch {host}: non-public address "
                f"(loopback/private/link-local/metadata are blocked)")
        return s

    import socket
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        if via_proxy:
            return s  # the proxy resolves it; we cannot check further
        raise ValueError(f"cannot resolve host {host!r}: {e}")
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            raise PrivateAddressRefused(
                f"refusing to fetch {host!r}: resolves to non-public address {ip} "
                f"(loopback/private/link-local/metadata are blocked)")
    return s


# ── Blocked-page detection ───────────────────────────────────────────

# Body markers for interstitials that return a normal status code. Kept
# specific: a page *about* CAPTCHAs must not read as a CAPTCHA wall.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser before accessing",
    "cf-browser-verification",
    "cf_chl_opt",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "verify you are human",
    "are you a robot",
    "unusual traffic from your computer network",
    "px-captcha",
    "/_incapsula_resource",
)


def _detect_block(status: int, headers: dict, body: str) -> str | None:
    """Name the wall in front of this page, or None if it looks like content."""
    lower = body[:20000].lower()
    mitigated = ""
    try:
        mitigated = (headers.get("cf-mitigated") or "").lower()
    except AttributeError:
        mitigated = ""
    if mitigated == "challenge":
        return "cloudflare_challenge"
    if status == 429:
        return "rate_limited"
    if status in (401, 403) and any(m in lower for m in _CHALLENGE_MARKERS):
        return "bot_challenge"
    if status == 503 and any(m in lower for m in _CHALLENGE_MARKERS):
        return "cloudflare_challenge"
    if status == 200 and any(m in lower for m in _CHALLENGE_MARKERS):
        return "bot_challenge"
    if status in (401, 403):
        return "forbidden"
    return None


_THIN_TEXT_CHARS = 200


def _looks_like_empty_shell(body: str, text: str) -> bool:
    """True when a 200 carried almost no text but plenty of JavaScript.

    That combination is a client-rendered page whose content never arrives
    without a browser. A short page with no scripts is just a short page —
    escalating those to a browser render would burn seconds for nothing.
    """
    if len(text.strip()) >= _THIN_TEXT_CHARS:
        return False
    lower = body.lower()
    if "<script" not in lower:
        return False
    return len(body) > 500


# ── web.fetch ────────────────────────────────────────────────────────


def _read_capped(resp, max_bytes: int, interrupt_event=None) -> tuple[bytes, bool]:
    """Read a streamed response body, stopping just past the cap."""
    raw = b""
    for chunk in resp.iter_content(chunk_size=8192):
        if interrupt_event is not None and interrupt_event.is_set():
            resp.close()
            raise InterruptedError("interrupted by user")
        if not chunk:
            continue
        if len(raw) + len(chunk) > max_bytes + 1:
            raw += chunk[:max_bytes + 1 - len(raw)]
            break
        raw += chunk
    return raw[:max_bytes], len(raw) > max_bytes


def _decode_body(raw: bytes, content_type: str, apparent) -> str:
    charset = ""
    if "charset=" in content_type:
        try:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        except (IndexError, ValueError):
            charset = ""
    # requests falls back to ISO-8859-1 whenever the server omits a charset,
    # which mangles every CJK page. Prefer the sniffed encoding in that case.
    if not charset or charset.lower() in ("iso-8859-1", "latin-1", "ascii"):
        charset = apparent or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _download_cap(max_bytes: int) -> int:
    """How many raw bytes to download for a text budget of max_bytes.

    These are two different limits and conflating them is a trap: markup is
    many times larger than the text it carries, so capping the *download* at
    the text budget hands the extractor a document truncated inside <head>.
    It then yields no text, and the page gets misread as an empty
    client-rendered shell.
    """
    return max(min(max_bytes * 16, _MAX_RESPONSE_BYTES), 200_000)


def _apply_identity_cookies(session, cookies) -> None:
    """Attach one identity's cookies to a session, scoped to their own domains."""
    for cookie in cookies or []:
        try:
            session.cookies.set(
                str(cookie.get("name")), str(cookie.get("value") or ""),
                domain=str(cookie.get("domain") or ""),
                path=str(cookie.get("path") or "/"))
        except Exception:
            continue


def _http_get(url: str, max_bytes: int, timeout: int,
              identity_cookies=None, interrupt_event=None) -> dict:
    """One guarded HTTP GET, following redirects by hand.

    Returns {ok, status, headers, body, final_url, content_type, truncated} or
    {ok: False, error, error_type}.
    """
    current = url
    host = _host_of(url)
    session = None
    for hop in range(_MAX_REDIRECTS + 1):
        if interrupt_event is not None and interrupt_event.is_set():
            raise InterruptedError("interrupted by user")
        try:
            current = _guard_url(current, via_proxy=bool(_proxy_for_host(_host_of(current))))
        except ValueError as e:
            return {"ok": False, "error_type": "blocked_url", "error": str(e)}

        # A redirect can cross to a host with a different proxy decision — a
        # reachable site handing off to a blocked one is exactly the case a
        # proxy exists for. Rebuild rather than carry the first host's routing.
        hop_host = _host_of(current)
        if (session is not None and hop_host != host
                and bool(_proxy_for_host(hop_host)) != bool(session.proxies)):
            session = None
            host = hop_host

        if session is None:
            # The identity's cookies go onto the jar, which is domain-scoped:
            # a redirect off the identity's own site cannot carry them along.
            resp, err, session = _request(
                "GET", current, host=hop_host or host, timeout=timeout,
                allow_redirects=False, stream=True,
                on_session=(lambda s: _apply_identity_cookies(s, identity_cookies))
                if identity_cookies else None)
            if err is not None:
                return {"ok": False, "error_type": err.value,
                        "error": f"request failed ({err.value})"}
        else:
            try:
                resp = session.get(current, timeout=timeout,
                                   allow_redirects=False, stream=True)
            except requests.RequestException as e:
                return {"ok": False, "error_type": _classify_request_error(e).value,
                        "error": f"request failed: {e}"}

        if 300 <= resp.status_code < 400:
            location = (resp.headers.get("Location") or "").strip()
            resp.close()
            if not location:
                return {"ok": False, "error_type": "http_error",
                        "error": f"HTTP {resp.status_code} with no Location header"}
            current = urllib.parse.urljoin(current, location)
            continue

        content_type = resp.headers.get("Content-Type", "")
        raw, body_truncated = _read_capped(
            resp, _download_cap(max_bytes), interrupt_event)
        apparent = None
        try:
            apparent = resp.apparent_encoding
        except Exception:
            apparent = None
        headers = dict(resp.headers)
        status = resp.status_code
        resp.close()
        return {
            "ok": True,
            "status": status,
            "headers": headers,
            "body": _decode_body(raw, content_type, apparent),
            "final_url": current,
            "content_type": content_type,
            "body_truncated": body_truncated,
        }

    return {"ok": False, "error_type": "http_error",
            "error": f"too many redirects (more than {_MAX_REDIRECTS})"}


def fetch(url: str, max_bytes: int = 65536, timeout: int = 15,
          identity: str | None = None, interrupt_event=None) -> dict:
    """Fetch a URL and extract text content.

    Uses the same proxy and cookie jar as web.search. Every hop is checked
    against the SSRF rules in _guard_url.

    identity names a saved login (see identity_store) to read the page as.
    It is never applied implicitly: a signed-in session must be asked for by
    name, and only reaches URLs inside that identity's own domains. An agent
    that reads untrusted pages should not be one injected sentence away from
    spending the user's Google session on somebody else's URL.

    Returns dict with:
      ok: bool
      result: str (extracted text, on success)
      url / final_url: str
      content_type: str
      size: int
      truncated: bool
      transport: str (how the content was obtained)
      error: str (on failure, with structured error type prefix)
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    max_bytes = max(int(max_bytes), 1000)
    timeout = min(max(int(timeout), 3), 60)

    identity_cookies = None
    if identity:
        try:
            import identity_store
            identity_cookies, reason = identity_store.cookies_for_requests(identity, url)
            if reason:
                return {"ok": False, "url": url, "error": f"[identity] {reason}"}
            identity_store.touch(identity)
        except ImportError:
            return {"ok": False, "url": url,
                    "error": "[identity] identity_store is unavailable in this build"}

    got = _http_get(url, max_bytes, timeout, identity_cookies=identity_cookies,
                    interrupt_event=interrupt_event)
    if not got["ok"]:
        return {"ok": False, "url": url,
                "error": f"[{got['error_type']}] {got['error']}"}

    body = got["body"]
    status = got["status"]
    blocked = _detect_block(status, got["headers"], body)

    if blocked is None and status >= 400:
        return {"ok": False, "url": url, "final_url": got["final_url"],
                "error": f"[http_error] HTTP {status}"}

    text = ""
    if blocked is None:
        text = _extract_readable(body, got["content_type"], got["final_url"])
        # A body cut off at the download cap is expected to look thin near the
        # end; only an intact document can be judged an empty shell.
        if not got["body_truncated"] and _looks_like_empty_shell(body, text):
            blocked = "client_rendered"

    if blocked is not None:
        return _blocked_result(url, got, blocked, max_bytes, timeout,
                               interrupt_event=interrupt_event)

    if _render_mode() == "always" and _render_unavailable_reason() is None:
        # "always" means render even when the plain fetch looked fine — for
        # sites that return real markup but fill in the parts that matter from
        # script. The HTTP text stays as the fallback, so forcing renders can
        # only add content, never lose it.
        rendered = _render_page(got["final_url"], timeout)
        if rendered.get("ok"):
            value = rendered.get("value") or {}
            adopt_cookies(value.get("cookies") or [])
            rendered_text = _extract_readable(
                value.get("html") or "", "text/html",
                value.get("url") or got["final_url"]).strip()
            if len(rendered_text) > len(text.strip()):
                truncated = False
                if len(rendered_text) > max_bytes:
                    rendered_text, truncated = rendered_text[:max_bytes], True
                return {
                    "ok": True,
                    "result": rendered_text,
                    "url": url,
                    "final_url": value.get("url") or got["final_url"],
                    "content_type": "text/html",
                    "size": len(rendered_text),
                    "truncated": truncated,
                    "transport": "browser",
                }

    text = text.strip()
    truncated = got["body_truncated"]
    if len(text) > max_bytes:
        text = text[:max_bytes]
        truncated = True

    return {
        "ok": True,
        "result": text,
        "title": extract_title(body, got["content_type"]),
        "url": url,
        "final_url": got["final_url"],
        "content_type": got["content_type"],
        "size": len(text),
        "truncated": truncated,
        "transport": "http",
    }


# ── Browser render tier ──────────────────────────────────────────────
#
# Playwright's sync API is thread-affine: every call must come from the thread
# that started it. The agent loop, webtest runs and delegated requests all call
# tools from different threads, so a render tier that borrowed whatever browser
# session happened to exist would break in ways that only show up under
# concurrency. Instead one worker thread owns one session and everything is
# handed to it as a job.

import queue as _queue


class _RenderWorker:
    """A single browser session, driven only from its own thread."""

    IDLE_TIMEOUT = 300  # close the browser after this long with nothing to do

    def __init__(self):
        self._jobs: _queue.Queue = _queue.Queue()
        self._thread: threading.Thread | None = None
        self._session = None
        self._lock = threading.Lock()

    # -- worker thread ------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=self.IDLE_TIMEOUT)
            except _queue.Empty:
                self._close_session()
                with self._lock:
                    if self._jobs.empty():
                        self._thread = None
                        return
                continue
            fn, box, done = job
            try:
                session = self._ensure_session()
                box["value"] = fn(session)
            except Exception as e:
                box["error"] = f"{type(e).__name__}: {e}"
            finally:
                # The session outlives the job on purpose: it holds the cookies
                # from a challenge the user solved, and it is the browser they
                # are looking at in the live view. Only the idle timeout and an
                # explicit shutdown close it.
                done.set()

    def _ensure_session(self):
        if self._session is not None and self._session.is_alive():
            return self._session
        self._session = None
        import browser_session as _bs
        egress = _bs.egress_from_env()
        session = _bs.BrowserSession(
            backend_url=os.environ.get("LAINTAS_BACKEND", "http://localhost:8000"),
            agent_id="web-fetch",
            session_id=f"web-fetch-{int(time.time() * 1000)}",
            url="about:blank", **egress)
        try:
            session.start()
        except Exception:
            session.close()
            raise
        # Registered so the user's live-view button can find it — that is the
        # whole point of the unlock path. Marked so the browser.* tools skip it:
        # driving it from the agent's thread would violate Playwright's thread
        # affinity and corrupt this worker's connection.
        session._owned_by_web_fetch = True
        try:
            _bs.register_browser_session(session, name="web-fetch")
        except Exception:
            pass
        self._session = session
        return session

    def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        unregistered = False
        try:
            import browser_session as _bs
            with _bs._browser_lock:
                names = [n for n, s in _bs._browser_sessions.items() if s is session]
            for name in names:
                # unregister_browser_session closes the session itself.
                unregistered = _bs.unregister_browser_session(name) or unregistered
        except Exception:
            pass
        if not unregistered:
            try:
                session.close()
            except Exception:
                pass

    # -- caller side --------------------------------------------------

    def submit(self, fn, timeout: float) -> dict:
        """Run fn(session) on the worker thread. Returns {ok, value|error}."""
        box: dict = {}
        done = threading.Event()
        # Queue the job and check the thread under one lock. Doing the check
        # first and the put afterwards leaves a window where the worker times
        # out and exits in between: the job then lands on a queue nobody is
        # reading and the caller waits out its whole timeout for nothing. The
        # worker's exit path re-checks the queue under this same lock, so a job
        # enqueued here is always either picked up or served by a new thread.
        with self._lock:
            self._jobs.put((fn, box, done))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="web-fetch-render", daemon=True)
                self._thread.start()
        if not done.wait(timeout):
            return {"ok": False, "error": f"browser render timed out after {timeout:.0f}s"}
        if "error" in box:
            return {"ok": False, "error": box["error"]}
        return {"ok": True, "value": box.get("value")}

    def has_live_session(self) -> bool:
        """True when a browser is already up — checked before asking it for
        anything, so a cookie read never boots a browser by itself."""
        session = self._session
        try:
            return session is not None and session.is_alive()
        except Exception:
            return False

    def shutdown(self) -> None:
        self.submit(lambda _session: None, timeout=5)
        self._close_session()


_RENDER_WORKER = _RenderWorker()


def _render_mode() -> str:
    """off | auto | always — whether a blocked page may escalate to a browser."""
    mode = str(_get_config("fetch_render", "") or
               os.environ.get("LAINTAS_FETCH_RENDER", "") or "auto").lower()
    return mode if mode in ("off", "auto", "always") else "auto"


def _render_unavailable_reason() -> str | None:
    """Why the browser tier cannot run here, or None if it can."""
    try:
        import browser_session as _bs
    except Exception as e:
        return f"browser_session unavailable ({e})"
    try:
        import playwright  # noqa: F401
    except Exception:
        return ("Playwright is not installed (pip install playwright && "
                "playwright install chromium)")
    try:
        missing = _bs._check_host_deps()
    except Exception:
        missing = None
    return missing or None


def _seed_browser_cookies(session, page) -> int:
    """Give a fresh browser the clearance this machine has already earned.

    Without this the flow is one-way: cookies go browser → store and never
    back. The render session closes after five idle minutes, so the next
    blocked page opens a brand-new browser that has never passed the wall —
    and the user is asked to solve a challenge they already solved. The HTTP
    path had the clearance the whole time; the browser just could not see it.

    Runs once per session, on the worker thread that owns it.
    """
    if getattr(session, "_cookies_seeded", False):
        return 0
    session._cookies_seeded = True
    if not _cookie_enabled():
        return 0
    try:
        import cookie_store
        stored = cookie_store.load()
    except Exception:
        return 0

    payload = []
    for cookie in stored:
        try:
            entry = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": cookie["domain"],
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure")),
                "httpOnly": bool(cookie.get("httpOnly")),
            }
            if cookie.get("expires"):
                entry["expires"] = float(cookie["expires"])
            payload.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    if not payload:
        return 0
    try:
        page.context.add_cookies(payload)
    except Exception:
        # One malformed record must not cost the whole render.
        return 0
    return len(payload)


def _render_page(url: str, timeout: int, settle_ms: int = 1500) -> dict:
    """Load a URL in the browser and return its rendered HTML and cookies."""

    def job(session):
        def _job(page):
            _seed_browser_cookies(session, page)
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=settle_ms * 2)
            except Exception:
                page.wait_for_timeout(settle_ms)
            html = page.content()
            try:
                cookies = page.context.cookies()
            except Exception:
                cookies = []
            return {"html": html, "url": page.url, "cookies": cookies}

        return session.run(_job)
    return _RENDER_WORKER.submit(job, timeout=timeout + 30)


def _collect_browser_cookies() -> list:
    """Cookies from the live render session (after the user unlocked a site)."""
    def job(session):
        def _job(page):
            try:
                return page.context.cookies()
            except Exception:
                return []
        return session.run(_job)
    out = _RENDER_WORKER.submit(job, timeout=30)
    if not out.get("ok"):
        return []
    return out.get("value") or []


def capture_identity(name: str, domains=None, probe: dict | None = None) -> dict:
    """Save the live render browser's session as a named identity.

    This is the hand-off after a human has signed in: whatever the browser is
    holding right now becomes an identity that later tasks can run as. Called
    once, by the user, at a moment they choose — never automatically.
    """
    try:
        import identity_store
    except ImportError:
        return {"ok": False, "error": "identity_store is unavailable in this build"}
    if not _RENDER_WORKER.has_live_session():
        return {"ok": False, "error": "no browser session is open to capture"}

    def job(session):
        def _job(page):
            context = page.context
            state = context.storage_state()
            try:
                agent = page.evaluate("navigator.userAgent")
            except Exception:
                agent = ""
            return {"state": state, "user_agent": agent}

        return session.run(_job)
    out = _RENDER_WORKER.submit(job, timeout=60)
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error", "capture failed")}
    value = out.get("value") or {}
    state = value.get("state") or {}
    if not (state.get("cookies") or state.get("origins")):
        return {"ok": False, "error": "the browser holds no session to save — "
                                      "sign in first, then capture"}
    record = identity_store.save(
        name, state, domains=domains,
        user_agent=str(value.get("user_agent") or ""), probe=probe)
    return {"ok": True, "identity": identity_store.describe(record["name"])}


def open_for_login(url: str, timeout: int = 30) -> dict:
    """Put the live-view browser on a page so a human can sign in there."""
    reason = _render_unavailable_reason()
    if reason:
        return {"ok": False, "error": reason}
    try:
        safe_url = _guard_url(url, via_proxy=bool(_proxy_for_host(_host_of(url))))
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    def job(session):
        def _job(page):
            page.goto(safe_url, timeout=timeout * 1000, wait_until="domcontentloaded")
            return page.url

        return session.run(_job)
    out = _RENDER_WORKER.submit(job, timeout=timeout + 30)
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error", "navigation failed")}
    return {"ok": True, "url": out.get("value"), "session": "web-fetch"}


def probe_identity(name: str) -> dict:
    """Check whether a saved identity is still signed in.

    Sessions expire, and finding that out halfway through an automated run is
    the expensive way to learn it. The probe fetches the identity's own check
    URL and looks for a marker that only appears when signed in.
    """
    try:
        import identity_store
    except ImportError:
        return {"ok": False, "error": "identity_store is unavailable in this build"}
    record = identity_store.load(name)
    if record is None:
        return {"ok": False, "error": f"no identity named '{name}'"}
    probe = record.get("probe") or {}
    url = str(probe.get("url") or "")
    if not url:
        return {"ok": False, "error": (
            f"identity '{name}' has no probe configured — set one so its "
            f"freshness can be checked before a task depends on it")}

    result = fetch(url, max_bytes=20000, timeout=20, identity=name)
    expect = str(probe.get("expect") or "")
    if not result.get("ok"):
        identity_store.record_probe(name, False, str(result.get("error"))[:200])
        return {"ok": False, "signed_in": False, "error": result.get("error")}
    signed_in = (expect in result.get("result", "")) if expect else True
    detail = "" if signed_in else f"expected marker {expect!r} was not on the page"
    identity_store.record_probe(name, signed_in, detail)
    return {"ok": True, "signed_in": signed_in, "detail": detail,
            "identity": identity_store.describe(name)}


def _set_needs_attention(flag: bool) -> None:
    """Flag the render session as waiting on a human, for the live view."""
    session = _RENDER_WORKER._session
    if session is not None:
        try:
            session._needs_attention = bool(flag)
        except Exception:
            pass


def refresh_cookies_from_browser() -> int:
    """Pull whatever the render browser has now into the shared jar and store.

    Used after a user has solved a challenge by hand, so the cheap HTTP path
    inherits the result. Reads only from a browser that is already running.
    """
    if not _RENDER_WORKER.has_live_session():
        return 0
    # The count, not the report: callers use this as "did anything change",
    # and a report dict is always truthy even when nothing was kept.
    return adopt_cookies(_collect_browser_cookies())["kept"]


# ── Escalation ladder ────────────────────────────────────────────────


def _unlock_enabled() -> bool:
    value = _get_config("fetch_unlock", True)
    return bool(value) if value is not None else True


def _wayback_enabled() -> bool:
    value = _get_config("fetch_wayback", True)
    return bool(value) if value is not None else True


def _blocked_result(url: str, got: dict, blocked: str,
                    max_bytes: int, timeout: int,
                    interrupt_event=None) -> dict:
    """Escalate past a wall: browser render → hand to the user → Wayback.

    Ordered by what it costs: a render is seconds, a manual unlock is the
    user's attention, and an archive snapshot is not the live page.
    """
    final_url = got.get("final_url", url)
    attempts: list[str] = [f"http: {blocked}"]

    def _check():
        if interrupt_event is not None and interrupt_event.is_set():
            raise InterruptedError("interrupted by user")

    _check()

    # A browser may already be open on this site from an earlier unlock, and
    # the user may have just cleared the challenge in it. Inheriting those
    # cookies and retrying the cheap path first is the difference between the
    # unlock costing one render and costing one on every subsequent read.
    # A retry that succeeds means the wall is behind us; the live view should
    # stop pointing here.
    if _cookie_enabled() and refresh_cookies_from_browser():
        _check()
        retried = _http_get(final_url, max_bytes, timeout,
                            interrupt_event=interrupt_event)
        if retried.get("ok"):
            retry_block = _detect_block(retried["status"], retried["headers"],
                                        retried["body"])
            if retry_block is None and retried["status"] < 400:
                text = _extract_readable(retried["body"], retried["content_type"],
                                         retried["final_url"]).strip()
                if len(text) >= _THIN_TEXT_CHARS:
                    truncated = retried["body_truncated"]
                    if len(text) > max_bytes:
                        text, truncated = text[:max_bytes], True
                    return {
                        "ok": True,
                        "result": text,
                        "url": url,
                        "final_url": retried["final_url"],
                        "content_type": retried["content_type"],
                        "size": len(text),
                        "truncated": truncated,
                        "transport": "http",
                        "note": "read using cookies from the browser session the "
                                "user unlocked",
                    }
            attempts.append(f"retry with unlocked cookies: {retry_block or 'no readable text'}")

    if blocked != "client_rendered":
        _check()
        impersonated = _impersonate_get(final_url, max_bytes, timeout)
        if impersonated is None:
            attempts.append("impersonate: curl_cffi not installed")
        elif impersonated.get("ok"):
            return impersonated
        else:
            attempts.append(f"impersonate: {impersonated.get('reason')}")

    if _render_mode() != "off":
        _check()
        reason = _render_unavailable_reason()
        if reason:
            attempts.append(f"browser: unavailable — {reason}")
        else:
            rendered = _render_page(final_url, timeout)
            if not rendered.get("ok"):
                attempts.append(f"browser: {rendered.get('error')}")
            else:
                value = rendered["value"] or {}
                html = value.get("html") or ""
                adopted = adopt_cookies(value.get("cookies") or [])
                still_blocked = _detect_block(200, {}, html)
                text = _extract_readable(html, "text/html", value.get("url") or final_url)
                if not still_blocked and len(text.strip()) >= _THIN_TEXT_CHARS:
                    _set_needs_attention(False)
                    text = text.strip()
                    truncated = False
                    if len(text) > max_bytes:
                        text, truncated = text[:max_bytes], True
                    return {
                        "ok": True,
                        "result": text,
                        "url": url,
                        "final_url": value.get("url") or final_url,
                        "content_type": "text/html",
                        "size": len(text),
                        "truncated": truncated,
                        "transport": "browser",
                        "note": f"the plain HTTP fetch was blocked ({blocked}); "
                                f"this content came from a rendered browser page",
                    }
                attempts.append(f"browser: {still_blocked or 'no readable text'}")
                if _unlock_enabled() and still_blocked:
                    # Mark the session so the live view opens on *this* browser.
                    # The viewer's button asks for "default", so without the
                    # flag the page the user is being asked to unblock is
                    # exactly the one they cannot see.
                    _set_needs_attention(True)
                    return _unlock_result(url, final_url, blocked, attempts, adopted)

    if _wayback_enabled():
        _check()
        snapshot, reason = _wayback_fetch(final_url, max_bytes, timeout,
                                          interrupt_event=interrupt_event)
        if snapshot is not None:
            return snapshot
        attempts.append(f"wayback: {reason}")

    return {
        "ok": False,
        "url": url,
        "final_url": final_url,
        "blocked": blocked,
        "attempts": attempts,
        "error": f"[{blocked}] could not get readable content from {final_url}. "
                 f"Tried — " + "; ".join(attempts),
    }


def _unlock_result(url: str, final_url: str, blocked: str,
                   attempts: list[str], adopted: dict | None = None) -> dict:
    """Leave the browser sitting on the wall and ask the user to clear it."""
    message = (
        f"[{blocked}] {final_url} is behind a challenge that neither a plain "
        f"request nor a headless render got past.\n"
        f"A browser is now open on that page in session 'web-fetch'. Ask the "
        f"user to open the live view, solve the challenge or sign in, and say "
        f"when they are done — then call web.fetch on this URL again. Clearance "
        f"they earn is reused for this site from then on."
    )
    if not _cookie_enabled():
        message += ("\nNOTE: /config search_cookie_enabled is off, so the clearance "
                    "they earn will not be kept. Turn it on first or the wall comes "
                    "straight back.")
    # A sign-in is not kept automatically — say so, rather than letting the user
    # believe an account session is now available to later fetches.
    if adopted and adopted.get("skipped"):
        where = ", ".join(adopted.get("skipped_domains") or []) or "that site"
        message += (
            f"\nNOTE: the browser also holds {adopted['skipped']} non-clearance "
            f"cookie(s) for {where} — a sign-in, most likely. Those are NOT kept "
            f"automatically. If the user wants later fetches to run as that "
            f"login, they must save it deliberately with /identity capture <name>, "
            f"and you must then pass identity=<name> to web.fetch."
        )
    return {
        "ok": False,
        "url": url,
        "final_url": final_url,
        "blocked": blocked,
        "attempts": attempts,
        "unlock_pending": True,
        "error": message,
    }


# ── Browser TLS fingerprint (optional) ───────────────────────────────


def _impersonate_get(url: str, max_bytes: int, timeout: int) -> dict | None:
    """Retry a blocked page with a real browser's TLS fingerprint.

    Bot walls key on the TLS/HTTP2 handshake, not the User-Agent: requests
    announces itself as Chrome while shaking hands like Python, which is a
    louder signal than any header. curl_cffi mimics the handshake, and a good
    share of "blocked" pages simply open — without paying for a browser.

    Returns None when curl_cffi is not installed, {ok: True, ...} on success,
    or {ok: False, reason} when it still did not work.
    """
    try:
        from curl_cffi import requests as _cffi
    except Exception:
        return None

    try:
        safe_url = _guard_url(url, via_proxy=bool(_proxy_for_host(_host_of(url))))
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    proxy = _proxy_for_host(_host_of(url))
    kwargs: dict = {"timeout": timeout, "impersonate": "chrome",
                    "allow_redirects": True}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    try:
        resp = _cffi.get(safe_url, **kwargs)
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    # No streaming cap here, so refuse anything that declares itself huge and
    # skip the attempt rather than pull an unbounded body into memory.
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_RESPONSE_BYTES:
        return {"ok": False, "reason": "response too large to retry"}

    content = resp.content or b""
    if len(content) > _MAX_RESPONSE_BYTES:
        content = content[:_MAX_RESPONSE_BYTES]
    content_type = resp.headers.get("content-type", "")
    body = _decode_body(content, content_type, "utf-8")
    still_blocked = _detect_block(resp.status_code, dict(resp.headers), body)
    if still_blocked or resp.status_code >= 400:
        return {"ok": False, "reason": still_blocked or f"HTTP {resp.status_code}"}

    text = _extract_readable(body, content_type, safe_url).strip()
    if len(text) < _THIN_TEXT_CHARS:
        return {"ok": False, "reason": "no readable text"}
    truncated = False
    if len(text) > max_bytes:
        text, truncated = text[:max_bytes], True
    return {
        "ok": True,
        "result": text,
        "url": url,
        "final_url": str(getattr(resp, "url", safe_url)),
        "content_type": content_type,
        "size": len(text),
        "truncated": truncated,
        "transport": "http-impersonate",
        "note": "the default HTTP client was blocked; this used a browser TLS "
                "fingerprint",
    }


# ── Wayback Machine ──────────────────────────────────────────────────


# The CDX index regularly takes 10-15s and intermittently answers 503, so it
# gets its own floor rather than inheriting a caller's short timeout — this
# rung runs only after everything else already failed, and timing it out at 3s
# would mean it never works for the callers most likely to need it.
_WAYBACK_MIN_TIMEOUT = 25


def _wayback_fetch(url: str, max_bytes: int, timeout: int,
                   interrupt_event=None) -> tuple[dict | None, str]:
    """The closest archived snapshot, plus why there isn't one.

    Not the live page, and labelled as such — but for a page that is dead,
    geo-blocked or permanently hostile it is the difference between some
    content and none.

    "the archive did not answer" and "the archive has no capture" are reported
    separately: the first is worth retrying later, the second never is.
    """
    timeout = max(int(timeout), _WAYBACK_MIN_TIMEOUT)
    # The CDX index, not /wayback/available: the latter answers with an empty
    # archived_snapshots for pages the archive demonstrably holds, so it cannot
    # be relied on to tell "no snapshot" from "not today".
    api = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode({
        "url": url,
        "output": "json",
        "limit": "-1",            # most recent capture
        "filter": "statuscode:200",
        "fl": "timestamp,original",
    })
    resp, err, _session = _request("GET", api, host="web.archive.org", timeout=timeout)
    if err is not None or resp is None:
        return None, "the archive did not answer"
    if resp.status_code != 200:
        return None, f"the archive answered HTTP {resp.status_code}"
    try:
        rows = resp.json()
    except ValueError:
        return None, "the archive sent an unreadable index"
    # Row 0 is the header; anything less means no capture.
    if not isinstance(rows, list) or len(rows) < 2 or not isinstance(rows[-1], list):
        return None, "no capture of this page"
    timestamp, original = (rows[-1] + ["", ""])[:2]
    if not timestamp or not original:
        return None, "no capture of this page"
    # The "id_" modifier serves the capture as it was archived. Without it the
    # archive injects its own navigation banner, which then lands in the
    # extracted text as a header of month names and capture metadata.
    snapshot_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    stamp = str(timestamp)
    captured = (f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
                if len(stamp) >= 8 and stamp[:8].isdigit() else stamp)
    got = _http_get(snapshot_url, max_bytes, timeout,
                    interrupt_event=interrupt_event)
    if not got.get("ok") or got.get("status", 0) >= 400:
        return None, "the snapshot itself could not be read"
    text = _extract_readable(got["body"], got["content_type"], snapshot_url).strip()
    if len(text) < _THIN_TEXT_CHARS:
        return None, "the snapshot had no readable text"
    truncated = got["body_truncated"]
    if len(text) > max_bytes:
        text, truncated = text[:max_bytes], True
    return {
        "ok": True,
        "result": text,
        "url": url,
        "final_url": snapshot_url,
        "content_type": got["content_type"],
        "size": len(text),
        "truncated": truncated,
        "transport": "wayback",
        "note": (f"the live page was unreachable; this is the Wayback Machine "
                 f"snapshot captured {captured} and may be out of date"),
    }, ""


# Lazy-load html2text to avoid module-level import cost
_HTML2TEXT = None


def _get_html2text():
    global _HTML2TEXT
    if _HTML2TEXT is not None:
        return _HTML2TEXT
    try:
        import html2text as _h2t
        _HTML2TEXT = _h2t.HTML2Text()
        _HTML2TEXT.ignore_links = False
        _HTML2TEXT.ignore_images = True
        _HTML2TEXT.body_width = 0
    except ImportError:
        _HTML2TEXT = False  # mark as tried-and-unavailable
    return _HTML2TEXT if _HTML2TEXT is not False else None


_TRAFILATURA = None


def _get_trafilatura():
    """trafilatura if installed, else None. Optional dependency."""
    global _TRAFILATURA
    if _TRAFILATURA is not None:
        return _TRAFILATURA or None
    try:
        import trafilatura as _t
        _TRAFILATURA = _t
    except Exception:
        _TRAFILATURA = False
    return _TRAFILATURA or None


# Whole regions that are never the article: site chrome, menus, promos.
_BOILERPLATE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe",
                     "nav", "header", "footer", "aside", "form", "figure")


def _strip_boilerplate(html: str) -> str:
    for tag in _BOILERPLATE_TAGS:
        html = _re.sub(rf'<{tag}\b[^>]*>.*?</{tag}>', ' ', html,
                       flags=_re.DOTALL | _re.IGNORECASE)
        # Unclosed/self-closing leftovers.
        html = _re.sub(rf'<{tag}\b[^>]*/?>', ' ', html, flags=_re.IGNORECASE)
    return html


def _main_region(html: str) -> str:
    """The <article>/<main> region when the page marks one, else the whole doc."""
    for tag in ("article", "main"):
        matches = _re.findall(rf'<{tag}\b[^>]*>(.*?)</{tag}>', html,
                              flags=_re.DOTALL | _re.IGNORECASE)
        if matches:
            best = max(matches, key=len)
            if len(best) > 500:
                return best
    m = _re.search(r'<body\b[^>]*>(.*?)</body>', html,
                   flags=_re.DOTALL | _re.IGNORECASE)
    return m.group(1) if m else html


def _tags_to_text(html: str) -> str:
    """Strip tags, keeping block structure as line breaks."""
    html = _re.sub(r'<(?:br|hr)\b[^>]*/?>', '\n', html, flags=_re.IGNORECASE)
    html = _re.sub(r'<li\b[^>]*>', '\n- ', html, flags=_re.IGNORECASE)
    html = _re.sub(r'</(?:p|div|li|tr|section|h[1-6]|blockquote|pre|td)\s*>', '\n',
                   html, flags=_re.IGNORECASE)
    html = _re.sub(r'<(?:p|div|tr|section|h[1-6]|blockquote|pre)\b[^>]*>', '\n',
                   html, flags=_re.IGNORECASE)
    html = _re.sub(r'<[^>]+>', '', html)
    text = _html.unescape(html)
    text = text.replace('\xa0', ' ')  # nbsp
    text = _re.sub(r'[^\S\n]+', ' ', text)
    text = _re.sub(r' *\n *', '\n', text)
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


_TITLE_RE = _re.compile(r'<title[^>]*>(.*?)</title>', _re.DOTALL | _re.IGNORECASE)


def extract_title(body: str, content_type: str = "") -> str:
    """The document title, for callers that show the page rather than read it."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    head = body[:1000].lower()
    if mime and "html" not in mime and "<html" not in head:
        return ""
    match = _TITLE_RE.search(body)
    if not match:
        return ""
    return _clean_text(match.group(1))[:300]


def _extract_readable(body: str, content_type: str, url: str = "") -> str:
    """Readable text for a fetched document.

    trafilatura → html2text → a boilerplate-stripping fallback. The first is a
    real article extractor; html2text is a *format converter* that faithfully
    keeps the nav bars, cookie banners and footers, so it is only a second
    choice, and the fallback below at least drops the obvious chrome.
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    head = body[:1000].lower()
    is_html = ("html" in mime or "<html" in head or "<!doctype html" in head)
    if not is_html:
        return body

    extractor = _get_trafilatura()
    if extractor is not None:
        try:
            out = extractor.extract(
                body, url=url or None, favor_precision=True,
                include_comments=False, include_tables=True)
            if out and len(out.strip()) >= 80:
                return out.strip()
        except Exception:
            pass

    stripped = _strip_boilerplate(body)
    region = _main_region(stripped)
    text = _tags_to_text(region)
    if len(text) >= 80:
        return text

    h2t = _get_html2text()
    if h2t is not None:
        try:
            return h2t.handle(body).strip()
        except Exception:
            pass
    return _tags_to_text(_strip_boilerplate(body))


def _extract_text(body: str, is_html: bool) -> str:
    """Backwards-compatible wrapper for the previous signature."""
    return _extract_readable(body, "text/html" if is_html else "text/plain")
