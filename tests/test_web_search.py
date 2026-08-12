"""Tests for web.search / web.fetch.

Every case here corresponds to a way this code has actually been wrong, so the
assertions are about behaviour that broke rather than about implementation
detail. Nothing reaches the network: HTTP is faked at the session boundary.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cookie_store
import web_search as ws


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", url=""):
        self.status_code = status
        self.reason = "OK"
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Type", "text/html; charset=utf-8")
        self._body = body
        self.url = url
        self.apparent_encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def json(self):
        import json
        return json.loads(self._body.decode())

    def close(self):
        self.closed = True


class FakeSession:
    """Stands in for requests.Session; records every URL it was asked for."""

    def __init__(self, handler):
        self.handler = handler
        self.urls: list[str] = []
        self.headers: dict = {}
        self.proxies: dict = {}
        self.cookies = None

    def request(self, method, url, **kwargs):
        self.urls.append(url)
        return self.handler(url)

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.handler(url)

    def post(self, url, **kwargs):
        self.urls.append(url)
        return self.handler(url)


class _CookieRecorder:
    """Captures whatever an on_session hook tries to put on a session's jar."""

    def __init__(self):
        self.set_cookies: list = []
        self.cookies = self

    def set(self, name, value, **kwargs):
        self.set_cookies.append({"name": name, "value": value, **kwargs})


class _PatchedTransport:
    """Route web_search's HTTP through a handler for the duration of a test."""

    def __init__(self, test, handler):
        self.session = FakeSession(handler)
        self.seeded: list = []          # cookies handed to on_session hooks
        self.on_session_calls = 0
        self._orig_request = ws._request
        self._orig_build = ws._build_session

        def fake_request(method, url, host="", on_session=None, **kw):
            if on_session is not None:
                self.on_session_calls += 1
                recorder = _CookieRecorder()
                on_session(recorder)
                self.seeded.extend(recorder.set_cookies)
            return self.session.request(method, url, **kw), None, self.session

        ws._request = fake_request
        ws._build_session = lambda host="", force_proxy=False: self.session
        test.addCleanup(self._restore)

    def _restore(self):
        ws._request = self._orig_request
        ws._build_session = self._orig_build


class LaintasContractTests(unittest.TestCase):
    """The API validates against a strict key allowlist and 400s on anything
    else, so the exact payload shape is load-bearing."""

    def _capture(self):
        sent = {}

        class Session:
            headers: dict = {}
            proxies: dict = {}

            def __init__(self):
                self.headers = {}

            def post(self, url, json=None, timeout=None, allow_redirects=None):
                sent["url"] = url
                sent["payload"] = json
                sent["headers"] = dict(self.headers)
                return FakeResponse(200, body=(
                    b'{"requestId":"r","results":[{"title":"T",'
                    b'"url":"https://e.com/a","snippet":"S",'
                    b'"trust":"untrusted_external","date":"2026-08-01"}]}'))

        self.addCleanup(setattr, ws.requests, "Session", ws.requests.Session)
        ws.requests.Session = Session
        self.addCleanup(setattr, ws, "_get_laintas_api_key", ws._get_laintas_api_key)
        ws._get_laintas_api_key = lambda: "k" * 40
        return sent

    def test_payload_uses_only_keys_the_api_accepts(self):
        sent = self._capture()
        ws._search_laintas("hello", 7, region="cn-zh", timelimit="w")
        self.assertEqual(set(sent["payload"]),
                         {"query", "maxResults", "language", "country", "timeRange"})
        self.assertEqual(sent["payload"]["maxResults"], 7)
        self.assertEqual(sent["payload"]["language"], "zh")
        self.assertEqual(sent["payload"]["country"], "CN")
        self.assertEqual(sent["payload"]["timeRange"], "week")

    def test_retry_key_is_sent_so_a_retry_is_not_billed_twice(self):
        sent = self._capture()
        ws._search_laintas("hello", 5)
        key = sent["headers"].get("Idempotency-Key", "")
        self.assertRegex(key, r"^[A-Za-z0-9._:-]{8,128}$")

    def test_recency_and_trust_survive_normalization(self):
        self._capture()
        results, err, _msg = ws._search_laintas("hello", 5)
        self.assertIsNone(err)
        self.assertEqual(results[0]["date"], "2026-08-01")
        self.assertEqual(results[0]["trust"], "untrusted_external")

    def test_unusable_locale_degrades_instead_of_failing_the_request(self):
        sent = self._capture()
        ws._search_laintas("hello", 5, region="zz-qq", timelimit="decade")
        self.assertEqual(sent["payload"]["language"], "auto")
        self.assertEqual(sent["payload"]["country"], "any")
        self.assertEqual(sent["payload"]["timeRange"], "any")

    def test_query_the_server_would_reject_fails_with_a_usable_message(self):
        self._capture()
        _results, err, msg = ws._search_laintas("!ddg python", 5)
        self.assertIsNotNone(err)
        self.assertIn("!", msg)


class UrlGuardTests(unittest.TestCase):
    """web.fetch runs on the user's own machine, where an unguarded URL is a
    route into their LAN and their cloud metadata endpoint."""

    def test_private_and_local_destinations_are_refused(self):
        for url in ("http://localhost/x", "http://127.0.0.1/x",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://192.168.1.1/", "http://10.0.0.5/",
                    "http://box.internal/", "http://[::1]/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    ws._guard_url(url, via_proxy=False)

    def test_non_http_schemes_and_embedded_credentials_are_refused(self):
        for url in ("ftp://a.com/x", "file:///etc/passwd",
                    "http://user:pw@example.com/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    ws._guard_url(url, via_proxy=False)

    def test_literal_private_address_is_refused_even_behind_a_proxy(self):
        # A proxy resolves names for us, but a literal address needs no DNS and
        # must never be waved through just because a proxy is configured.
        with self.assertRaises(ValueError):
            ws._guard_url("http://192.168.0.1/", via_proxy=True)

    def test_unresolvable_name_is_allowed_only_when_a_proxy_will_resolve_it(self):
        # Patched rather than using a real bad name: an ISP that hijacks
        # NXDOMAIN would otherwise resolve it and make this pass for the wrong
        # reason.
        import socket
        original = socket.getaddrinfo
        self.addCleanup(setattr, socket, "getaddrinfo", original)

        def refuse(*args, **kwargs):
            raise OSError("Name or service not known")

        socket.getaddrinfo = refuse
        url = "http://nonexistent.example/page"
        self.assertEqual(ws._guard_url(url, via_proxy=True), url)
        with self.assertRaises(ValueError):
            ws._guard_url(url, via_proxy=False)

    def test_public_name_resolving_to_a_private_address_is_refused(self):
        # DNS rebinding: the name is public, the address is not.
        import socket
        original = socket.getaddrinfo
        self.addCleanup(setattr, socket, "getaddrinfo", original)
        socket.getaddrinfo = lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))]
        for via_proxy in (False, True):
            with self.subTest(via_proxy=via_proxy):
                with self.assertRaises(ValueError):
                    ws._guard_url("http://public-name.example/", via_proxy=via_proxy)


class RedirectTests(unittest.TestCase):
    def test_redirect_into_a_private_address_is_refused_at_the_hop(self):
        def handler(url):
            if "start" in url:
                return FakeResponse(302, {"Location": "http://192.168.1.1/admin"})
            return FakeResponse(200, body=b"<html><body>secret</body></html>")

        transport = _PatchedTransport(self, handler)
        out = ws._http_get("https://example.com/start", 65536, 5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "blocked_url")
        # The internal address must never have been requested.
        self.assertEqual(transport.session.urls, ["https://example.com/start"])

    def test_public_redirect_is_followed_and_reported(self):
        def handler(url):
            if "start" in url:
                return FakeResponse(302, {"Location": "https://example.org/final"})
            return FakeResponse(200, body=b"<html><body>" + b"content " * 40 + b"</body></html>")

        _PatchedTransport(self, handler)
        out = ws._http_get("https://example.com/start", 65536, 5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["final_url"], "https://example.org/final")

    def test_redirect_loop_is_bounded(self):
        def handler(url):
            return FakeResponse(302, {"Location": "https://example.com/?n=%d" % len(url)})

        transport = _PatchedTransport(self, handler)
        out = ws._http_get("https://example.com/", 65536, 5)
        self.assertFalse(out["ok"])
        self.assertLessEqual(len(transport.session.urls), ws._MAX_REDIRECTS + 1)


class DownloadCapTests(unittest.TestCase):
    """The text budget and the download cap are different limits. Capping the
    download at the text budget truncates the HTML inside <head>, leaving no
    text to extract — which then reads as an empty client-rendered shell."""

    def test_small_text_budget_still_downloads_enough_markup(self):
        article = ("<p>" + "Sentence about the subject. " * 30 + "</p>") * 6
        page = ("<html><head>" + "<meta name='x' content='y'>" * 400 +
                "</head><body><article>" + article + "</article></body></html>")

        _PatchedTransport(self, lambda url: FakeResponse(200, body=page.encode()))
        out = ws.fetch("https://example.com/", max_bytes=1000, timeout=5)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["transport"], "http")
        self.assertIn("Sentence about the subject", out["result"])

    def test_download_cap_is_larger_than_the_text_budget(self):
        self.assertGreater(ws._download_cap(1000), 1000)
        self.assertLessEqual(ws._download_cap(10 ** 9), ws._MAX_RESPONSE_BYTES)


class BlockDetectionTests(unittest.TestCase):
    def test_challenge_signals(self):
        self.assertEqual(
            ws._detect_block(403, {"cf-mitigated": "challenge"}, "<html></html>"),
            "cloudflare_challenge")
        self.assertEqual(
            ws._detect_block(503, {}, "<title>Just a moment...</title>"),
            "cloudflare_challenge")
        self.assertEqual(ws._detect_block(429, {}, ""), "rate_limited")
        self.assertEqual(ws._detect_block(403, {}, "no"), "forbidden")

    def test_ordinary_content_is_not_a_block(self):
        self.assertIsNone(ws._detect_block(200, {}, "<html><body>an article</body></html>"))

    def test_short_page_without_scripts_is_not_treated_as_a_shell(self):
        # Escalating these to a browser render would cost seconds for nothing.
        self.assertFalse(ws._looks_like_empty_shell("<html><body>hi</body></html>", "hi"))

    def test_script_heavy_page_with_no_text_is_a_shell(self):
        body = ('<html><body><div id="root"></div>' +
                '<script src="app.js"></script>' * 20 + 'x' * 600 + '</body></html>')
        self.assertTrue(ws._looks_like_empty_shell(body, ""))


class ParserTests(unittest.TestCase):
    def test_google_blocks_split_on_whole_class_tokens(self):
        # Matching the class as a substring also split on "logo" and "heading",
        # fragmenting each result away from its snippet.
        html = ('<div class="logo"><img></div>'
                '<div class="g"><h3><a href="https://a.com/1">Alpha</a></h3>'
                '<span class="aCOpRe">Alpha body.</span></div>'
                '<div class="heading"><span>noise</span></div>'
                '<div class="tF2Cxc"><h3><a href="https://b.com/2">Beta</a></h3>'
                '<span class="IsZvec">Beta body.</span></div>')
        results = ws._parse_google(html, 10)
        self.assertEqual([r["title"] for r in results], ["Alpha", "Beta"])
        self.assertEqual([r["snippet"] for r in results], ["Alpha body.", "Beta body."])

    def test_duckduckgo_keeps_each_snippet_with_its_title(self):
        # The snippet sits outside the title's container, with other
        # "result"-classed divs in between.
        html = ''.join(
            f'<div class="result results_links">'
            f'<a class="result__a" href="https://s{i}.com/">Title {i}</a>'
            f'<div class="result__extras"><div class="result__url">s{i}.com</div></div>'
            f'<a class="result__snippet" href="https://s{i}.com/">Snippet {i}.</a>'
            f'</div>'
            for i in range(3))
        results = ws._parse_duckduckgo(html, 10)
        self.assertEqual(len(results), 3)
        for i, item in enumerate(results):
            self.assertEqual(item["title"], f"Title {i}")
            self.assertEqual(item["snippet"], f"Snippet {i}.")

    def test_duckduckgo_unwraps_redirect_links(self):
        html = ('<div class="result"><a class="result__a" '
                'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.example%2Fpage">T</a>'
                '<a class="result__snippet">S</a></div>')
        results = ws._parse_duckduckgo(html, 10)
        self.assertEqual(results[0]["url"], "https://real.example/page")


class ExtractionTests(unittest.TestCase):
    def test_site_chrome_is_dropped(self):
        html = ("<html><body><nav>Home About Contact</nav>"
                "<header>Site name</header>"
                "<article><p>" + "The actual article text. " * 20 + "</p></article>"
                "<footer>Copyright notice</footer>"
                "<script>var x = 1;</script></body></html>")
        text = ws._extract_readable(html, "text/html")
        self.assertIn("The actual article text.", text)
        self.assertNotIn("Copyright notice", text)
        self.assertNotIn("var x", text)

    def test_non_html_is_returned_unchanged(self):
        payload = '{"key": "value"}'
        self.assertEqual(ws._extract_readable(payload, "application/json"), payload)

    def test_missing_charset_does_not_mangle_cjk(self):
        body = "<html><body><article>中文正文内容测试</article></body></html>".encode("utf-8")
        decoded = ws._decode_body(body, "text/html", "utf-8")
        self.assertIn("中文正文内容测试", decoded)


class ProxyRoutingTests(unittest.TestCase):
    def setUp(self):
        ws._PROXY_HOSTS.clear()
        self.addCleanup(ws._PROXY_HOSTS.clear)
        self._orig = ws._get_config
        self.addCleanup(setattr, ws, "_get_config", self._orig)

    def _config(self, **values):
        ws._get_config = lambda key, default=None: values.get(key, default)

    def test_auto_mode_stays_direct_until_a_host_proves_it_needs_the_proxy(self):
        self._config(search_proxy="socks5://127.0.0.1:1080", search_proxy_mode="auto")
        self.assertIsNone(ws._proxy_for_host("example.com"))
        ws._mark_host_needs_proxy("example.com")
        self.assertEqual(ws._proxy_for_host("example.com"), "socks5://127.0.0.1:1080")
        # Learned per host, not globally.
        self.assertIsNone(ws._proxy_for_host("other.com"))

    def test_off_mode_never_proxies_and_never_leaks_to_the_browser(self):
        self._config(search_proxy="socks5://127.0.0.1:1080", search_proxy_mode="off")
        ws._mark_host_needs_proxy("example.com")
        self.assertIsNone(ws._proxy_for_host("example.com"))
        self.assertEqual(ws.browser_egress_overrides(), {})

    def test_browser_shares_the_one_proxy_setting(self):
        self._config(search_proxy="http://127.0.0.1:8080", search_proxy_mode="auto")
        self.assertEqual(ws.browser_egress_overrides(),
                         {"proxy": "http://127.0.0.1:8080"})


class CookieStoreTests(unittest.TestCase):
    def setUp(self):
        cookie_store.COOKIE_FILE = Path(tempfile.mkdtemp()) / "cookies.json"
        self._orig = cookie_store._get_config
        self.addCleanup(setattr, cookie_store, "_get_config", self._orig)
        self._config()

    def _config(self, **values):
        values.setdefault("search_cookie_enabled", True)
        cookie_store._get_config = lambda key, default=None: values.get(key, default)

    def test_round_trip_and_upsert(self):
        cookie_store.save([{"name": "a", "value": "1", "domain": ".example.com"}])
        self.assertEqual(cookie_store.load()[0]["value"], "1")
        cookie_store.merge([{"name": "a", "value": "2", "domain": ".example.com"}])
        stored = cookie_store.load()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["value"], "2")

    def test_expired_cookies_are_dropped(self):
        cookie_store.save([{"name": "old", "value": "1", "domain": "e.com", "expires": 1},
                           {"name": "new", "value": "2", "domain": "e.com"}])
        self.assertEqual([c["name"] for c in cookie_store.load()], ["new"])

    def test_allowlist_limits_what_is_kept(self):
        self._config(search_cookie_domains="example.com")
        kept = cookie_store.save([
            {"name": "a", "value": "1", "domain": "example.com"},
            {"name": "b", "value": "2", "domain": "login.example.com"},
            {"name": "c", "value": "3", "domain": "notexample.com"},
            {"name": "d", "value": "4", "domain": "evil.com"},
        ])
        self.assertEqual(kept, 2)
        self.assertEqual([d for d, _ in cookie_store.summary()],
                         ["example.com", "login.example.com"])

    def test_clear_targets_one_domain_and_its_subdomains(self):
        cookie_store.save([{"name": "a", "value": "1", "domain": "example.com"},
                           {"name": "b", "value": "2", "domain": "sub.example.com"},
                           {"name": "c", "value": "3", "domain": "other.com"}])
        self.assertEqual(cookie_store.clear("example.com"), 2)
        self.assertEqual([d for d, _ in cookie_store.summary()], ["other.com"])

    def test_file_is_not_world_readable(self):
        import os
        cookie_store.save([{"name": "a", "value": "1", "domain": "e.com"}])
        self.assertEqual(os.stat(cookie_store.COOKIE_FILE).st_mode & 0o077, 0)


class EngineRegistryTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._orig_path = ws._registry_path
        self.addCleanup(setattr, ws, "_registry_path", self._orig_path)
        ws._registry_path = lambda: self.home / "search_engines.json"
        self._reset_cache()
        self.addCleanup(self._reset_cache)
        ws._FAST_FAIL.clear()
        self.addCleanup(ws._FAST_FAIL.clear)

    def _reset_cache(self):
        ws._REGISTRY_CACHE.update({"mtime": None, "entries": {}, "errors": []})

    def _write(self, engines):
        import json
        (self.home / "search_engines.json").write_text(json.dumps({"engines": engines}))
        self._reset_cache()

    def test_builtins_include_cn_bing_after_duckduckgo(self):
        chain, _warnings = ws.resolve_chain(None)
        # tavily keyless comes first (best free results, no key needed),
        # followed by the HTML scraping engines.
        self.assertEqual(chain[:4], ["tavily", "google", "duckduckgo", "cn-bing"])
        # Metered tiers come last: this is a cost ordering, not a quality one.
        self.assertEqual(chain[-1], "laintas_gateway")

    def test_aliases_resolve(self):
        for alias, expected in (("ddg", "duckduckgo"), ("bing", "cn-bing"),
                                ("cn.bing", "cn-bing"), ("laintas", "laintas_search"),
                                ("gateway", "laintas_gateway")):
            with self.subTest(alias=alias):
                self.assertEqual(ws.canonical_engine(alias), expected)

    def test_explicit_unknown_engine_fails_instead_of_searching_elsewhere(self):
        chain, warnings = ws.resolve_chain(["nosuchengine"])
        self.assertEqual(chain, [])
        self.assertTrue(warnings)
        out = ws.search("q", engines=["nosuchengine"])
        self.assertFalse(out["ok"])

    def test_partially_unknown_request_uses_the_known_ones(self):
        chain, warnings = ws.resolve_chain(["nosuchengine", "cn-bing"])
        self.assertEqual(chain, ["cn-bing"])
        self.assertTrue(warnings)

    def test_user_engine_is_registered_and_callable(self):
        self._write([{
            "name": "fake", "kind": "json", "cost": "free",
            "url": "https://api.example/search", "method": "POST",
            "headers": {"X-API-KEY": "${env:FAKE_KEY}"},
            "body": {"q": "${query}", "num": "${max_results}", "cc": "${country}"},
            "results_path": "data.items",
            "fields": {"title": "t", "url": "u", "snippet": "s"},
        }])
        import os
        os.environ["FAKE_KEY"] = "sekret"
        self.addCleanup(os.environ.pop, "FAKE_KEY", None)

        captured = {}

        def handler(url):
            captured["url"] = url
            return FakeResponse(200, {"Content-Type": "application/json"}, body=(
                b'{"data":{"items":[{"t":"T","u":"https://x.example/1","s":"S"}]}}'))

        transport = _PatchedTransport(self, handler)
        out = ws.search("hello", max_results=4, engines=["fake"], region="cn-zh")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["engine"], "fake")
        self.assertEqual(out["result"][0]["url"], "https://x.example/1")
        self.assertEqual(transport.session.urls, ["https://api.example/search"])

    def test_user_engine_cannot_redefine_a_builtin(self):
        self._write([{"name": "google", "kind": "json", "url": "https://evil.example/s"}])
        entries, errors = ws.load_engine_registry()
        self.assertEqual(entries["google"]["kind"], "builtin")
        self.assertTrue(any("cannot redefine" in e for e in errors))

    def test_user_engine_may_not_reach_a_private_address_by_default(self):
        self._write([{"name": "leaky", "kind": "json", "url": "http://127.0.0.1:9/x"}])
        out = ws.search("hello", engines=["leaky"])
        self.assertFalse(out["ok"])
        self.assertTrue(any("allow_private" in e["message"] for e in out["errors"]))

    def test_html_engines_cannot_be_defined_by_users(self):
        # Scraped engines need a parser per site; allowing them here would push
        # that breakage onto the user's config.
        self._write([{"name": "scrape", "kind": "html", "url": "https://example.com/s"}])
        entries, errors = ws.load_engine_registry()
        self.assertNotIn("scrape", entries)
        self.assertTrue(any("kind 'json'" in e for e in errors))

    def test_metered_engines_are_not_put_into_cooldown(self):
        # A key or billing failure must surface next call, not be hidden for
        # five minutes behind a cooldown meant for flaky scrapers.
        self.addCleanup(setattr, ws, "_search_laintas", ws._search_laintas)
        ws._search_laintas = lambda *a, **k: ([], ws.SearchErrorType.API_ERROR, "quota exhausted")
        self.addCleanup(setattr, ws, "_laintas_key_available", ws._laintas_key_available)
        ws._laintas_key_available = lambda: ""
        ws.search("hello", engines=["laintas_search"])
        self.assertFalse(ws._is_fast_failed("laintas_search"))

    def test_unavailable_engine_is_reported_but_not_cooled_down(self):
        self.addCleanup(setattr, ws, "_gateway_session", ws._gateway_session)
        ws._gateway_session = lambda: None
        out = ws.search("hello", engines=["laintas_gateway"])
        self.assertFalse(out["ok"])
        self.assertTrue(any(e["error"] == "unavailable" for e in out["errors"]))
        self.assertFalse(ws._is_fast_failed("laintas_gateway"))

    def test_health_report_lists_cost_and_usability(self):
        health = {h["engine"]: h for h in ws.engine_health()}
        self.assertEqual(health["cn-bing"]["cost"], "free")
        self.assertEqual(health["laintas_gateway"]["cost"], "metered")


class BingParserTests(unittest.TestCase):
    def test_blocks_split_on_whole_class_token(self):
        html = ('<li class="b_pag"><span>nav</span></li>'
                '<li class="b_algo"><h2><a href="https://a.example/1">Alpha</a></h2>'
                '<div class="b_caption"><p>Alpha body.</p></div></li>'
                '<li class="b_algo b_algoBorder"><h2><a href="https://b.example/2">Beta</a></h2>'
                '<div class="b_caption"><p>Beta body.</p></div></li>')
        results = ws._parse_bing(html, 10)
        self.assertEqual([r["title"] for r in results], ["Alpha", "Beta"])
        self.assertEqual([r["snippet"] for r in results], ["Alpha body.", "Beta body."])

    def test_navigation_headers_are_sent(self):
        # Bing serves an empty result frame to requests without fetch metadata.
        for header in ("Sec-Fetch-Mode", "Sec-Fetch-Dest", "Upgrade-Insecure-Requests"):
            self.assertIn(header, ws._BING_HEADERS)


class ChallengeDetectionTests(unittest.TestCase):
    def test_inline_script_config_is_not_a_captcha(self):
        # Bing ships "captchaSuccessPostMessage" in the config blob of every
        # ordinary search page.
        page = ('<html><head><script>var cfg={"captchaSuccessPostMessage":"done",'
                '"verifyEndpoint":"https://www.bing.com/challenge/verify"};</script></head>'
                '<body><li class="b_algo"><h2><a href="https://a.example/">R</a></h2></li>'
                '</body></html>')
        self.assertFalse(ws._detect_captcha(page, "bing"))

    def test_visible_challenge_text_is_still_detected(self):
        page = "<html><body><h1>Please verify you are human</h1></body></html>"
        self.assertTrue(ws._detect_captcha(page, "bing"))


class FakeBrowserSession:
    def __init__(self):
        self.closed = False
        self.thread_ids = []

    def is_alive(self):
        return not self.closed

    def close(self):
        self.closed = True


class RenderWorkerTests(unittest.TestCase):
    """The render tier owns its browser on one thread because Playwright's sync
    API is thread-affine. These assert the queueing rather than the browser."""

    def setUp(self):
        self.worker = ws._RenderWorker()
        self.session = FakeBrowserSession()

        def ensure():
            # Mirror the real _ensure_session, which records the session on the
            # worker so _close_session can find it.
            self.worker._session = self.session
            return self.session

        self.worker._ensure_session = ensure
        self.addCleanup(self.worker._close_session)

    def test_job_runs_and_returns_its_value(self):
        out = self.worker.submit(lambda session: "done", timeout=5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["value"], "done")

    def test_failing_job_reports_instead_of_raising(self):
        def boom(_session):
            raise RuntimeError("navigation failed")

        out = self.worker.submit(boom, timeout=5)
        self.assertFalse(out["ok"])
        self.assertIn("navigation failed", out["error"])

    def test_every_job_runs_on_the_same_thread(self):
        import threading

        seen: list[int] = []
        results: list[dict] = []
        lock = threading.Lock()

        def record(_session):
            with lock:
                seen.append(threading.get_ident())
            return len(seen)

        def caller():
            results.append(self.worker.submit(record, timeout=10))

        threads = [threading.Thread(target=caller) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        self.assertEqual(len(results), 8)
        self.assertTrue(all(r["ok"] for r in results), results)
        # One owning thread, no matter how many callers.
        self.assertEqual(len(set(seen)), 1)
        # And never the caller's own thread.
        self.assertNotIn(threading.get_ident(), seen)

    def test_a_hung_job_times_out_rather_than_blocking_the_caller(self):
        import threading
        release = threading.Event()
        self.addCleanup(release.set)

        out = self.worker.submit(lambda _session: release.wait(30), timeout=0.5)
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])

    def test_idle_worker_closes_its_browser(self):
        self.worker.IDLE_TIMEOUT = 0.2
        self.worker.submit(lambda _session: None, timeout=5)
        deadline = __import__("time").time() + 5
        while __import__("time").time() < deadline and not self.session.closed:
            __import__("time").sleep(0.05)
        self.assertTrue(self.session.closed)


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self._orig_config = ws._get_config
        self.addCleanup(setattr, ws, "_get_config", self._orig_config)
        ws._get_config = lambda key, default=None: {
            "fetch_render": "auto", "fetch_unlock": True, "fetch_wayback": False,
        }.get(key, default)
        self._orig_reason = ws._render_unavailable_reason
        self.addCleanup(setattr, ws, "_render_unavailable_reason", self._orig_reason)
        ws._render_unavailable_reason = lambda: None
        self._orig_render = ws._render_page
        self.addCleanup(setattr, ws, "_render_page", self._orig_render)

    def _blocked_page(self):
        return {"final_url": "https://example.com/", "body_truncated": False}

    def test_render_recovers_a_blocked_page_and_says_so(self):
        article = "<html><body><article><p>" + "Real content here. " * 30 + "</p></article></body></html>"
        ws._render_page = lambda url, timeout, settle_ms=1500: {
            "ok": True, "value": {"html": article, "url": url, "cookies": []}}
        out = ws._blocked_result("https://example.com/", self._blocked_page(),
                                 "cloudflare_challenge", 5000, 10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["transport"], "browser")
        self.assertIn("Real content here.", out["result"])

    def test_challenge_surviving_the_render_is_handed_to_the_user(self):
        ws._render_page = lambda url, timeout, settle_ms=1500: {
            "ok": True, "value": {"html": "<html><title>Just a moment...</title></html>",
                                  "url": url, "cookies": []}}
        out = ws._blocked_result("https://example.com/", self._blocked_page(),
                                 "cloudflare_challenge", 5000, 10)
        self.assertFalse(out["ok"])
        self.assertTrue(out["unlock_pending"])
        self.assertIn("live view", out["error"])

    def test_failure_reports_every_rung_it_tried(self):
        ws._render_page = lambda url, timeout, settle_ms=1500: {
            "ok": False, "error": "browser render timed out after 40s"}
        out = ws._blocked_result("https://example.com/", self._blocked_page(),
                                 "bot_challenge", 5000, 10)
        self.assertFalse(out["ok"])
        joined = " ".join(out["attempts"])
        self.assertIn("http: bot_challenge", joined)
        self.assertIn("timed out", joined)

    def test_render_is_skipped_when_disabled(self):
        ws._get_config = lambda key, default=None: {
            "fetch_render": "off", "fetch_unlock": True, "fetch_wayback": False,
        }.get(key, default)

        def fail(*_args, **_kwargs):
            raise AssertionError("render must not run when fetch_render is off")

        ws._render_page = fail
        out = ws._blocked_result("https://example.com/", self._blocked_page(),
                                 "forbidden", 5000, 10)
        self.assertFalse(out["ok"])


class IdentityStoreTests(unittest.TestCase):
    """Identities hold live logins to the user's own accounts, so these are
    about what must NOT happen as much as what must."""

    def setUp(self):
        import identity_store
        self.store = identity_store
        self.dir = Path(tempfile.mkdtemp()) / "identities"
        self._orig_dir = identity_store.IDENTITY_DIR
        self.addCleanup(setattr, identity_store, "IDENTITY_DIR", self._orig_dir)
        identity_store.IDENTITY_DIR = self.dir
        self._orig_config = identity_store._get_config
        self.addCleanup(setattr, identity_store, "_get_config", self._orig_config)
        identity_store._get_config = lambda key, default=None: (
            True if key == "identity_enabled" else default)
        self._orig_egress = identity_store.current_egress
        self.addCleanup(setattr, identity_store, "current_egress", self._orig_egress)
        identity_store.current_egress = lambda: "direct"

    def _state(self, domain="example.com"):
        return {
            "cookies": [{"name": "sid", "value": "SECRET-TOKEN",
                         "domain": domain, "path": "/"}],
            "origins": [{"origin": f"https://{domain}",
                         "localStorage": [{"name": "tok", "value": "SECRET-LS"}]}],
        }

    def test_localstorage_is_kept_not_just_cookies(self):
        # A cookie-only export logs you out of every site that keeps its
        # session in localStorage.
        self.store.save("acct", self._state())
        record = self.store.load("acct")
        self.assertTrue(record["storage_state"]["origins"])
        self.assertEqual(
            record["storage_state"]["origins"][0]["localStorage"][0]["value"],
            "SECRET-LS")

    def test_describe_never_exposes_secret_values(self):
        self.store.save("acct", self._state())
        summary = self.store.describe("acct")
        blob = json.dumps(summary)
        self.assertNotIn("SECRET-TOKEN", blob)
        self.assertNotIn("SECRET-LS", blob)
        self.assertEqual(summary["cookies"], 1)
        self.assertEqual(summary["domains"], ["example.com"])

    def test_identity_refuses_urls_outside_its_domains(self):
        # The prompt-injection case: a page tells the agent to fetch somewhere
        # else "with your login".
        self.store.save("acct", self._state(), domains=["example.com"])
        record, reason = self.store.authorize("acct", "https://evil.example/steal")
        self.assertIsNone(record)
        self.assertIn("limited to", reason)

    def test_identity_allows_its_own_subdomains(self):
        self.store.save("acct", self._state(), domains=["example.com"])
        record, reason = self.store.authorize("acct", "https://mail.example.com/inbox")
        self.assertIsNotNone(record, reason)

    def test_identity_refuses_a_changed_exit(self):
        self.store.save("acct", self._state(), egress="socks5://127.0.0.1:1080")
        self.store.current_egress = lambda: "direct"
        record, reason = self.store.authorize("acct", "https://example.com/x")
        self.assertIsNone(record)
        self.assertIn("exit", reason)

    def test_cookies_are_scoped_to_the_target_host(self):
        state = {
            "cookies": [
                {"name": "a", "value": "1", "domain": "example.com", "path": "/"},
                {"name": "b", "value": "2", "domain": "other.example", "path": "/"},
            ],
            "origins": [],
        }
        self.store.save("multi", state, domains=["example.com", "other.example"])
        cookies, reason = self.store.cookies_for_requests("multi", "https://example.com/x")
        self.assertEqual(reason, "")
        self.assertEqual([c["name"] for c in cookies], ["a"])

    def test_disabled_store_hands_out_nothing(self):
        self.store.save("acct", self._state())
        self.store._get_config = lambda key, default=None: (
            False if key == "identity_enabled" else default)
        record, reason = self.store.authorize("acct", "https://example.com/x")
        self.assertIsNone(record)
        # The message must name the switch that actually governs logins, not
        # the cookie-jar one they used to share.
        self.assertIn("identity_enabled", reason)

    def test_files_are_not_readable_by_other_users(self):
        import os
        self.store.save("acct", self._state())
        self.assertEqual(os.stat(self.dir / "acct.json").st_mode & 0o077, 0)

    def test_invalid_names_are_rejected(self):
        for bad in ("../escape", "has space", "", "A" * 100, "/etc/passwd"):
            with self.subTest(name=bad):
                with self.assertRaises(self.store.IdentityError):
                    self.store.validate_name(bad)

    def test_delete_revokes(self):
        self.store.save("acct", self._state())
        self.assertTrue(self.store.delete("acct"))
        self.assertIsNone(self.store.load("acct"))


class IdentityFetchTests(unittest.TestCase):
    def setUp(self):
        import identity_store
        self.store = identity_store
        self.dir = Path(tempfile.mkdtemp()) / "identities"
        self.addCleanup(setattr, identity_store, "IDENTITY_DIR", identity_store.IDENTITY_DIR)
        identity_store.IDENTITY_DIR = self.dir
        self.addCleanup(setattr, identity_store, "_get_config", identity_store._get_config)
        identity_store._get_config = lambda key, default=None: (
            True if key == "identity_enabled" else default)
        self.addCleanup(setattr, identity_store, "current_egress", identity_store.current_egress)
        identity_store.current_egress = lambda: "direct"
        identity_store.save("acct", {
            "cookies": [{"name": "sid", "value": "SECRET", "domain": "example.com", "path": "/"}],
            "origins": [],
        }, domains=["example.com"])

    def _page(self, url):
        return FakeResponse(200, body=b"<html><body><article><p>" +
                            b"Body text here. " * 20 + b"</p></article></body></html>")

    def test_fetch_without_identity_sends_no_login_cookies(self):
        # Credentials must not be ambient: an ordinary fetch of a site the user
        # happens to be signed in to carries nothing from that session.
        transport = _PatchedTransport(self, self._page)
        out = ws.fetch("https://example.com/page", max_bytes=5000, timeout=5)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(transport.on_session_calls, 0)
        self.assertEqual(transport.seeded, [])

    def test_fetch_with_identity_sends_exactly_that_identity_cookies(self):
        transport = _PatchedTransport(self, self._page)
        out = ws.fetch("https://example.com/page", max_bytes=5000, timeout=5,
                       identity="acct")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual([c["name"] for c in transport.seeded], ["sid"])
        self.assertEqual(transport.seeded[0]["domain"], "example.com")

    def test_fetch_with_identity_outside_its_domains_is_refused(self):
        out = ws.fetch("https://evil.example/x", max_bytes=5000, timeout=5,
                       identity="acct")
        self.assertFalse(out["ok"])
        self.assertIn("[identity]", out["error"])

    def test_fetch_with_unknown_identity_is_refused(self):
        out = ws.fetch("https://example.com/x", max_bytes=5000, timeout=5,
                       identity="nosuch")
        self.assertFalse(out["ok"])
        self.assertIn("no identity", out["error"])


class ClearanceOnlyAdoptionTests(unittest.TestCase):
    """The unlock prompt tells the user to "solve the challenge or sign in".
    Whatever they do, only the clearance may become ambient — a login picked up
    in the same browser must not attach itself to every later fetch."""

    def setUp(self):
        cookie_store.COOKIE_FILE = Path(tempfile.mkdtemp()) / "cookies.json"
        self.addCleanup(setattr, cookie_store, "_get_config", cookie_store._get_config)
        cookie_store._get_config = lambda key, default=None: (
            True if key == "search_cookie_enabled" else default)
        self.addCleanup(setattr, ws, "_get_config", ws._get_config)
        ws._get_config = lambda key, default=None: (
            True if key == "search_cookie_enabled" else default)
        ws.clear_cookie_jar(persistent=False)
        self.addCleanup(ws.clear_cookie_jar, False)

    def test_clearance_cookies_are_recognised(self):
        for name in ("cf_clearance", "__cf_bm", "GOOGLE_ABUSE_EXEMPTION",
                     "datadome", "incap_ses_123_456", "CONSENT"):
            with self.subTest(name=name):
                self.assertTrue(cookie_store.is_clearance(name))

    def test_session_cookies_are_not_clearance(self):
        for name in ("sid", "SID", "session", "auth_token", "__Secure-1PSID",
                     "csrftoken"):
            with self.subTest(name=name):
                self.assertFalse(cookie_store.is_clearance(name))

    def test_a_login_picked_up_during_an_unlock_is_not_kept(self):
        report = ws.adopt_cookies([
            {"name": "cf_clearance", "value": "CLEAR", "domain": "shop.example", "path": "/"},
            {"name": "__Secure-1PSID", "value": "LOGIN", "domain": "google.com", "path": "/"},
            {"name": "sid", "value": "LOGIN2", "domain": "shop.example", "path": "/"},
        ])
        self.assertEqual(report["kept"], 1)
        self.assertEqual(report["skipped"], 2)
        self.assertEqual(report["skipped_domains"], ["google.com", "shop.example"])

        stored = {c["name"] for c in cookie_store.load(all_egress=True)}
        self.assertEqual(stored, {"cf_clearance"})
        jar = ws._get_cookie_jar()
        self.assertNotIn("__Secure-1PSID", {c.name for c in jar})

    def test_user_can_name_an_unknown_clearance_cookie(self):
        cookie_store._get_config = lambda key, default=None: {
            "search_cookie_enabled": True,
            "search_cookie_names": "wall_pass",
        }.get(key, default)
        self.assertTrue(cookie_store.is_clearance("wall_pass"))

    def test_the_unlock_message_says_a_login_was_not_kept(self):
        out = ws._unlock_result(
            "https://x.example/", "https://x.example/", "bot_challenge", [],
            {"kept": 1, "skipped": 2, "skipped_domains": ["x.example"]})
        self.assertIn("/identity capture", out["error"])
        self.assertIn("NOT kept", out["error"])

    def test_refresh_returns_a_count_not_a_truthy_report(self):
        # It is used as "did anything change"; a report dict is always truthy.
        self.addCleanup(setattr, ws._RENDER_WORKER, "has_live_session",
                        ws._RENDER_WORKER.has_live_session)
        ws._RENDER_WORKER.has_live_session = lambda: True
        self.addCleanup(setattr, ws, "_collect_browser_cookies",
                        ws._collect_browser_cookies)
        ws._collect_browser_cookies = lambda: [
            {"name": "sid", "value": "L", "domain": "x.example", "path": "/"}]
        self.assertEqual(ws.refresh_cookies_from_browser(), 0)


class CookieEgressTests(unittest.TestCase):
    """Clearance cookies name the exit they were issued to — Google's abuse
    exemption literally embeds the address — so replaying one through a
    different exit is useless at best."""

    def setUp(self):
        cookie_store.COOKIE_FILE = Path(tempfile.mkdtemp()) / "cookies.json"
        self.addCleanup(setattr, cookie_store, "_get_config", cookie_store._get_config)
        cookie_store._get_config = lambda key, default=None: (
            True if key == "search_cookie_enabled" else default)
        self.addCleanup(setattr, cookie_store, "current_egress",
                        cookie_store.current_egress)

    def _at(self, exit_label):
        cookie_store.current_egress = lambda: exit_label

    def test_cookies_are_stamped_with_the_exit_that_earned_them(self):
        self._at("socks5://127.0.0.1:1080")
        cookie_store.save([{"name": "cf_clearance", "value": "C", "domain": "e.example"}])
        self.assertEqual(cookie_store.load()[0]["egress"], "socks5://127.0.0.1:1080")

    def test_a_different_exit_does_not_get_them(self):
        self._at("socks5://127.0.0.1:1080")
        cookie_store.save([{"name": "cf_clearance", "value": "C", "domain": "e.example"}])
        self._at("direct")
        self.assertEqual(cookie_store.load(), [])
        # Still visible for listing and deletion.
        self.assertEqual(len(cookie_store.load(all_egress=True)), 1)

    def test_merging_from_one_exit_does_not_delete_the_others(self):
        # save() rewrites the whole file; merging while filtered would silently
        # drop everything earned through the proxy.
        self._at("socks5://127.0.0.1:1080")
        cookie_store.save([{"name": "cf_clearance", "value": "P", "domain": "proxied.example"}])
        self._at("direct")
        cookie_store.merge([{"name": "cf_clearance", "value": "D", "domain": "direct.example"}])
        domains = {c["domain"] for c in cookie_store.load(all_egress=True)}
        self.assertEqual(domains, {"proxied.example", "direct.example"})

    def test_stats_separate_stored_from_usable(self):
        self._at("socks5://127.0.0.1:1080")
        cookie_store.save([{"name": "cf_clearance", "value": "P", "domain": "a.example"}])
        self._at("direct")
        cookie_store.merge([{"name": "cf_clearance", "value": "D", "domain": "b.example"}])
        stats = cookie_store.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["usable"], 1)
        self.assertEqual(stats["egress"], "direct")

    def test_records_without_an_exit_are_still_usable(self):
        # Written before exits were recorded; discarding them would silently
        # throw away a working clearance on upgrade.
        cookie_store.COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cookie_store.COOKIE_FILE.write_text(json.dumps([
            {"name": "cf_clearance", "value": "C", "domain": "old.example",
             "path": "/", "expires": 0}]))
        self._at("direct")
        loaded = cookie_store.load()
        self.assertEqual(len(loaded), 1)


class BrowserSeedingTests(unittest.TestCase):
    """Cookies used to flow browser -> store only, so a fresh render session
    faced a wall this machine had already cleared."""

    class FakeContext:
        def __init__(self):
            self.added = []

        def add_cookies(self, cookies):
            self.added.extend(cookies)

    class FakePage:
        def __init__(self, context):
            self.context = context

    class FakeSession:
        pass

    def setUp(self):
        cookie_store.COOKIE_FILE = Path(tempfile.mkdtemp()) / "cookies.json"
        self.addCleanup(setattr, cookie_store, "_get_config", cookie_store._get_config)
        cookie_store._get_config = lambda key, default=None: (
            True if key == "search_cookie_enabled" else default)
        self.addCleanup(setattr, ws, "_get_config", ws._get_config)
        ws._get_config = lambda key, default=None: (
            True if key == "search_cookie_enabled" else default)
        cookie_store.save([
            {"name": "cf_clearance", "value": "C", "domain": "wall.example",
             "path": "/", "secure": True, "expires": 4102444800},
        ])

    def test_a_new_session_receives_stored_clearance(self):
        context, session = self.FakeContext(), self.FakeSession()
        added = ws._seed_browser_cookies(session, self.FakePage(context))
        self.assertEqual(added, 1)
        self.assertEqual(context.added[0]["name"], "cf_clearance")
        self.assertEqual(context.added[0]["domain"], "wall.example")

    def test_seeding_happens_once_per_session(self):
        context, session = self.FakeContext(), self.FakeSession()
        page = self.FakePage(context)
        ws._seed_browser_cookies(session, page)
        ws._seed_browser_cookies(session, page)
        self.assertEqual(len(context.added), 1)

    def test_nothing_is_injected_when_the_jar_is_off(self):
        ws._get_config = lambda key, default=None: (
            False if key == "search_cookie_enabled" else default)
        context, session = self.FakeContext(), self.FakeSession()
        ws._seed_browser_cookies(session, self.FakePage(context))
        self.assertEqual(context.added, [])


class ToolSurfaceTests(unittest.TestCase):
    def test_fetch_output_is_labelled_untrusted(self):
        import tools
        page = "<html><body><article><p>" + "Readable body text. " * 20 + "</p></article></body></html>"
        _PatchedTransport(self, lambda url: FakeResponse(200, body=page.encode()))
        out = tools._bi_web_fetch({"url": "https://example.com/"}, tools.ToolCtx())
        self.assertTrue(out["ok"], out.get("error"))
        self.assertTrue(out["result"].startswith(tools._UNTRUSTED_WEB_NOTICE))
        self.assertIn("Readable body text.", out["result"])

    def test_search_output_carries_the_notice_alongside_results(self):
        import tools
        self.addCleanup(setattr, ws, "search", ws.search)
        ws.search = lambda **kw: {"ok": True, "result": [{"title": "T", "url": "u", "snippet": "s"}],
                                  "count": 1, "engine": "duckduckgo"}
        out = tools._bi_web_search({"query": "x"}, tools.ToolCtx())
        self.assertEqual(out["result"]["notice"], tools._UNTRUSTED_WEB_NOTICE)
        self.assertEqual(len(out["result"]["results"]), 1)


if __name__ == "__main__":
    unittest.main()
