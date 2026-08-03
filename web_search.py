"""Web search and fetch with engine chain, proxy, cookie, and structured errors.

Engine chain (auto): Google -> DuckDuckGo -> laintas_search
  - Google/DDG: free HTML scraping, best-effort with proxy+cookie
  - laintas_search: paid API fallback, always reliable

Proxy: LAINTAS_HTTP_PROXY env or /config search_proxy
  - http://, https://, socks5://, socks5h://
  - Only affects web.search (Google/DDG) and web.fetch, NOT laintas_search API

Cookie: shared process-level CookieJar, opt-in via /config search_cookie_enabled
  - web.search and web.fetch share the same jar
  - Google consent cookie auto-injected when enabled

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
    """Return proxy URL from /config or LAINTAS_HTTP_PROXY env."""
    proxy = _get_config("search_proxy", "")
    if proxy:
        return str(proxy)
    return os.environ.get("LAINTAS_HTTP_PROXY") or None


def _cookie_enabled() -> bool:
    return bool(_get_config("search_cookie_enabled", False))


def _get_cookie_jar() -> requests.cookies.RequestsCookieJar | None:
    """Return the shared cookie jar if enabled, else None."""
    global _COOKIE_JAR
    if not _cookie_enabled():
        return None
    with _COOKIE_LOCK:
        if _COOKIE_JAR is None:
            _COOKIE_JAR = requests.cookies.RequestsCookieJar()
            # Inject Google consent cookie to bypass consent wall
            _COOKIE_JAR.set("CONSENT", "YES+", domain=".google.com", path="/")
        return _COOKIE_JAR


def clear_cookie_jar() -> int:
    """Clear all cookies. Returns the number of cookies removed."""
    global _COOKIE_JAR
    with _COOKIE_LOCK:
        if _COOKIE_JAR is None:
            return 0
        count = len(_COOKIE_JAR)
        _COOKIE_JAR = None
        return count


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


def _build_session() -> requests.Session:
    """Build a requests.Session with proxy and cookie jar configured."""
    session = requests.Session()
    session.headers.update(_DEFAULT_HEADERS)

    proxy = _get_proxy()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    jar = _get_cookie_jar()
    if jar is not None:
        session.cookies = jar

    return session


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
    lower = html.lower()
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


def _detect_captcha(html: str, engine: str) -> bool:
    """Check if the page is a CAPTCHA challenge."""
    lower = html.lower()
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

    # Google results are in <div class="g"> or <div class="tF2Cxc"> blocks
    # Try multiple selectors for robustness
    blocks = _re.split(r'<div[^>]*class="[^"]*(?:g|tF2Cxc)[^"]*"[^>]*>', html)

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


def _search_google(query: str, max_results: int) -> tuple[list[dict], SearchErrorType | None]:
    """Search Google via HTML scraping. Returns (results, error_or_None)."""
    session = _build_session()
    url = "https://www.google.com/search?" + urllib.parse.urlencode({
        "q": query,
        "num": str(min(max_results + 2, 20)),
        "hl": "en",
    })

    try:
        resp = session.get(url, timeout=_WEB_FETCH_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return [], _classify_request_error(e)

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


def _parse_duckduckgo(html: str, max_results: int) -> list[dict]:
    """Parse DuckDuckGo HTML search results."""
    results = []
    blocks = _re.split(r'<div[^>]*class="[^"]*result[^"]*"[^>]*>', html)
    for block in blocks:
        title_m = _re.search(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            block, _re.DOTALL)
        if not title_m:
            continue
        href = _html.unescape(title_m.group(1))
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            href = qs["uddg"][0]
        snippet_m = _re.search(
            r'<[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
            block, _re.DOTALL)
        results.append({
            "title": _clean_text(title_m.group(2)),
            "url": href,
            "snippet": _clean_text(snippet_m.group(1))[:500] if snippet_m else "",
        })
    return _dedupe(results, max_results)


def _search_duckduckgo(query: str, max_results: int) -> tuple[list[dict], SearchErrorType | None]:
    """Search DuckDuckGo via HTML scraping."""
    session = _build_session()
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})

    try:
        resp = session.post(url, timeout=_WEB_FETCH_TIMEOUT, allow_redirects=True,
                            data={"q": query})
    except requests.RequestException as e:
        return [], _classify_request_error(e)

    if resp.status_code == 429:
        return [], SearchErrorType.RATE_LIMITED

    html = resp.text

    if _detect_captcha(html, "duckduckgo"):
        return [], SearchErrorType.CAPTCHA

    results = _parse_duckduckgo(html, max_results)
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


def _search_laintas(query: str, max_results: int) -> tuple[list[dict], SearchErrorType | None, str]:
    """Search via laintas_search API. Returns (results, error_or_None, message)."""
    api_key = _get_laintas_api_key()
    if not api_key:
        return [], SearchErrorType.API_ERROR, "No laintas_search API key configured (set /config search_laintas_api_key or LAINTAS_SEARCH_API_KEY env)"

    api_url = _get_laintas_api_url()
    url = api_url + "/search"

    # laintas_search API does NOT go through the user's proxy (it has its own
    # proxy pool server-side). Use a clean session without proxy.
    session = requests.Session()
    session.headers.update({
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    })

    try:
        resp = session.post(url, json={"query": query, "num": max_results},
                            timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        return [], SearchErrorType.NETWORK, f"laintas_search request failed: {e}"

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

    # Normalize laintas_search results to {title, url, snippet}
    results = []
    for item in raw_results:
        title = item.get("title", "")
        url_val = item.get("url", item.get("link", ""))
        snippet = item.get("content", item.get("snippet", ""))
        if title and url_val:
            results.append({
                "title": str(title).strip(),
                "url": str(url_val).strip(),
                "snippet": str(snippet).strip()[:500] if snippet else "",
            })

    results = _dedupe(results, max_results)
    if not results:
        return [], SearchErrorType.EMPTY, "laintas_search returned no usable results"

    return results, None, ""


# ── Engine chain ─────────────────────────────────────────────────────


def _get_engine_chain() -> list[str]:
    """Determine the engine chain from /config or env."""
    engine = str(_get_config("search_engine", "") or
                 os.environ.get("LAINTAS_SEARCH_ENGINE", "") or
                 "auto").lower()

    if engine in ("google",):
        return ["google"]
    if engine in ("ddg", "duckduckgo"):
        return ["duckduckgo"]
    if engine in ("laintas", "laintas_search"):
        return ["laintas"]
    # auto (default): Google -> DDG -> laintas_search
    return ["google", "duckduckgo", "laintas"]


def search(query: str, max_results: int = 10,
           engine: str | None = None) -> dict:
    """Run the search engine chain.

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

    # Allow per-call engine override
    if engine:
        engine = engine.lower()
        if engine in ("google",):
            chain = ["google"]
        elif engine in ("ddg", "duckduckgo"):
            chain = ["duckduckgo"]
        elif engine in ("laintas", "laintas_search"):
            chain = ["laintas"]
        else:
            chain = _get_engine_chain()
    else:
        chain = _get_engine_chain()

    errors = []

    for eng in chain:
        # Skip fast-failed engines (but always try laintas_search)
        if eng != "laintas" and _is_fast_failed(eng):
            errors.append({
                "engine": eng,
                "error": SearchErrorType.NETWORK.value,
                "message": f"{eng} skipped (recently failed, in cooldown)",
            })
            continue

        if eng == "google":
            results, err = _search_google(query, max_results)
            if err is None:
                return {
                    "ok": True,
                    "result": results,
                    "query": query,
                    "count": len(results),
                    "engine": "google",
                    "errors": errors if errors else None,
                }
            errors.append({
                "engine": "google",
                "error": err.value,
                "message": _error_message("google", err),
            })
            _mark_fast_fail("google")

        elif eng == "duckduckgo":
            results, err = _search_duckduckgo(query, max_results)
            if err is None:
                return {
                    "ok": True,
                    "result": results,
                    "query": query,
                    "count": len(results),
                    "engine": "duckduckgo",
                    "errors": errors if errors else None,
                }
            errors.append({
                "engine": "duckduckgo",
                "error": err.value,
                "message": _error_message("duckduckgo", err),
            })
            _mark_fast_fail("duckduckgo")

        elif eng == "laintas":
            results, err, msg = _search_laintas(query, max_results)
            if err is None:
                return {
                    "ok": True,
                    "result": results,
                    "query": query,
                    "count": len(results),
                    "engine": "laintas_search",
                    "errors": errors if errors else None,
                }
            errors.append({
                "engine": "laintas_search",
                "error": err.value,
                "message": msg or _error_message("laintas_search", err),
            })

    # All engines failed
    error_parts = [f"{e['engine']}: {e['error']}" for e in errors]
    return {
        "ok": False,
        "error": "All search engines failed; " + " | ".join(error_parts),
        "query": query,
        "errors": errors,
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


# ── web.fetch ────────────────────────────────────────────────────────


def fetch(url: str, max_bytes: int = 65536, timeout: int = 15) -> dict:
    """Fetch a URL and extract text content.

    Uses the same proxy and cookie jar as web.search.
    Returns dict with:
      ok: bool
      result: str (extracted text, on success)
      url: str
      content_type: str
      size: int
      truncated: bool
      error: str (on failure, with structured error type prefix)
    """
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    session = _build_session()
    session.headers["User-Agent"] = _UA_BROWSER

    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
    except requests.RequestException as e:
        err_type = _classify_request_error(e)
        return {
            "ok": False,
            "error": f"[{err_type.value}] Request failed: {e}",
            "url": url,
        }

    if resp.status_code == 429:
        return {"ok": False, "error": "[rate_limited] HTTP 429: Too Many Requests", "url": url}
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"[http_error] HTTP {resp.status_code}: {resp.reason}",
            "url": url,
        }

    content_type = resp.headers.get("Content-Type", "")

    # Read up to max_bytes + 1 to detect truncation
    raw = b""
    for chunk in resp.iter_content(chunk_size=8192):
        if len(raw) + len(chunk) > max_bytes + 1:
            raw += chunk[:max_bytes + 1 - len(raw)]
            break
        raw += chunk
    resp.close()

    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]

    # Decode body
    charset = "utf-8"
    if "charset=" in content_type:
        try:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        except (IndexError, ValueError):
            pass

    try:
        body = raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        body = raw.decode("utf-8", errors="replace")

    # Extract text from HTML
    is_html = ("text/html" in content_type or
               "<html" in body[:1000].lower() or
               "<!doctype html" in body[:1000].lower())

    text = _extract_text(body, is_html)

    # Trim
    text = text.strip()
    if len(text) > max_bytes:
        text = text[:max_bytes]
        truncated = True

    return {
        "ok": True,
        "result": text,
        "url": url,
        "content_type": content_type,
        "size": len(text),
        "truncated": truncated,
    }


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


def _extract_text(body: str, is_html: bool) -> str:
    """Extract readable text from HTML or return raw text."""
    if not is_html:
        return body

    h2t = _get_html2text()
    if h2t is not None:
        return h2t.handle(body)

    # Fallback: strip HTML tags manually
    body = _re.sub(r'<script[^>]*>.*?</script>', '', body,
                   flags=_re.DOTALL | _re.IGNORECASE)
    body = _re.sub(r'<style[^>]*>.*?</style>', '', body,
                   flags=_re.DOTALL | _re.IGNORECASE)
    body = _re.sub(r'<(?:br|p|div|li|tr|h[1-6])[^>]*>', '\n', body,
                   flags=_re.IGNORECASE)
    body = _re.sub(r'<[^>]+>', '', body)
    body = _re.sub(r'\n{3,}', '\n\n', body)
    for ent, ch in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                     ('&quot;', '"'), ('&#x27;', "'"), ('&nbsp;', ' ')]:
        body = body.replace(ent, ch)
    return body
