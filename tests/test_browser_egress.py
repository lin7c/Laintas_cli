"""Tests for the browser egress path: ProxyAuthRelay and egress_from_env.

The relay is what keeps proxy credentials out of Chrome's argv, so its
behaviour is asserted against a fake upstream rather than a real proxy — these
run without network access.
"""
from __future__ import annotations

import os
import socket
import threading
import unittest

import browser_session as bs


class FakeProxy:
    """Minimal upstream that records the request head it was sent."""

    def __init__(self, answer: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.answer = answer
        self.heads: list[bytes] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    head += chunk
                self.heads.append(head)
                conn.sendall(self.answer)
                conn.recv(1024)          # let the client speak, then hang up
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class ProxyAuthRelayTests(unittest.TestCase):

    def _relay(self, upstream_port: int, creds: str | None) -> bs.ProxyAuthRelay:
        relay = bs.ProxyAuthRelay(f"127.0.0.1:{upstream_port}", creds)
        relay.start()
        self.addCleanup(relay.close)
        return relay

    @staticmethod
    def _connect(relay: bs.ProxyAuthRelay, head: bytes) -> bytes:
        sock = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
        try:
            sock.sendall(head)
            sock.settimeout(5)
            return sock.recv(4096)
        finally:
            sock.close()

    def test_rejects_upstream_without_port(self):
        with self.assertRaises(ValueError):
            bs.ProxyAuthRelay("198.51.100.7")

    def test_adds_authorization_header_upstream(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        relay = self._relay(upstream.port, "user:secret")
        self._connect(relay, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        self.assertTrue(upstream.heads, "upstream saw no request")
        # base64("user:secret")
        self.assertIn(b"Proxy-Authorization: Basic dXNlcjpzZWNyZXQ=", upstream.heads[0])

    def test_replaces_client_supplied_authorization(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        relay = self._relay(upstream.port, "user:secret")
        self._connect(relay, b"CONNECT example.com:443 HTTP/1.1\r\n"
                             b"Proxy-Authorization: Basic Zm9yZ2Vk\r\n\r\n")
        head = upstream.heads[0]
        self.assertNotIn(b"Zm9yZ2Vk", head, "client's header must not reach upstream")
        self.assertEqual(head.count(b"Proxy-Authorization:"), 1)

    def test_sends_no_header_when_no_credentials(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        relay = self._relay(upstream.port, None)
        self._connect(relay, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        self.assertNotIn(b"Proxy-Authorization", upstream.heads[0])

    def test_relays_upstream_refusal_to_the_client(self):
        upstream = FakeProxy(answer=b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        self.addCleanup(upstream.close)
        relay = self._relay(upstream.port, "user:secret")
        answer = self._connect(relay, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b"407", answer)

    def test_listens_on_loopback_only(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        relay = self._relay(upstream.port, None)
        host, _ = relay._sock.getsockname()          # type: ignore[union-attr]
        self.assertEqual(host, "127.0.0.1")

    def test_close_frees_the_port(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        relay = bs.ProxyAuthRelay(f"127.0.0.1:{upstream.port}", None)
        port = relay.start()
        relay.close()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_close_is_idempotent(self):
        upstream = FakeProxy()
        self.addCleanup(upstream.close)
        relay = bs.ProxyAuthRelay(f"127.0.0.1:{upstream.port}", None)
        relay.start()
        relay.close()
        relay.close()


class EgressFromEnvTests(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "LAINTAS_BROWSER_PROXY", "LAINTAS_BROWSER_PROXY_CREDENTIALS",
            "LAINTAS_BROWSER_USER_AGENT")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_empty_environment_changes_nothing(self):
        self.assertEqual(bs.egress_from_env(), {})

    def test_credentials_only_ride_along_with_a_proxy(self):
        os.environ["LAINTAS_BROWSER_PROXY_CREDENTIALS"] = "user:secret"
        self.assertEqual(bs.egress_from_env(), {})

    def test_reads_all_three(self):
        os.environ["LAINTAS_BROWSER_PROXY"] = "198.51.100.7:8080"
        os.environ["LAINTAS_BROWSER_PROXY_CREDENTIALS"] = "user:secret"
        os.environ["LAINTAS_BROWSER_USER_AGENT"] = "UA/1.0"
        self.assertEqual(bs.egress_from_env(), {
            "proxy": "198.51.100.7:8080",
            "proxy_credentials": "user:secret",
            "user_agent": "UA/1.0",
        })


if __name__ == "__main__":
    unittest.main()
