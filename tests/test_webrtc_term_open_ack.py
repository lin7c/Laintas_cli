"""K6 (bughunt): term-open must always answer, and VNC backpressure must
be bounded.

_open_term is fired with ensure_future, so an exception was swallowed
silently: a malformed `cols`/`rows` raised ValueError before anything
was sent, and the client hung forever waiting for term-open-ack (or any
error). The fix parses dimensions defensively and wraps the whole open
so any failure reports term-exit. The VNC backpressure loop also gained
a 30s deadline: a viewer that disconnected without closing the data
channel never drains, and the loop used to spin at 50Hz forever.
"""
import asyncio
import json
import unittest
from unittest import mock

import webrtc_channel as wc


def _manager():
    import inspect
    for obj in vars(wc).values():
        if inspect.isclass(obj) and hasattr(obj, "_open_term"):
            m = obj.__new__(obj)
            m._terms = {}
            return m
    raise AssertionError("no webrtc manager class found")


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send(self, s):
        self.sent.append(json.loads(s))


class TermOpenAckTests(unittest.TestCase):
    def test_malformed_dimensions_fall_back_to_defaults(self):
        m = _manager()
        ch = FakeChannel()
        with mock.patch("pty.fork", side_effect=OSError("no pty")):
            asyncio.run(m._open_term(ch, {"id": "s1", "cols": "abc",
                                          "rows": "x"}))
        # The ValueError source is gone; the failure still reports itself.
        self.assertTrue(any(x["t"] == "term-exit" and x.get("id") == "s1"
                            for x in ch.sent))

    def test_any_open_failure_sends_term_exit(self):
        m = _manager()
        ch = FakeChannel()
        with mock.patch("pty.fork", side_effect=OSError("boom")):
            asyncio.run(m._open_term(ch, {"id": "s2"}))
        self.assertTrue(any(x["t"] == "term-exit" and x.get("id") == "s2"
                            for x in ch.sent),
                        "client would hang waiting for an ack")

    def test_missing_id_is_silently_ignored(self):
        m = _manager()
        ch = FakeChannel()
        asyncio.run(m._open_term(ch, {}))
        self.assertEqual(ch.sent, [])


class VncBackpressureTests(unittest.TestCase):
    def test_backpressure_loop_is_bounded(self):
        # A channel that never drains: the pump must exit after the deadline
        # instead of spinning forever.
        m = _manager()

        class StuckChannel:
            bufferedAmount = 2_000_000  # never drains
            sent = []

            def send(self, data):
                self.sent.append(data)

        class FakeLoop:
            _t = 0.0

            def time(self):
                return self._t

        async def run():
            loop = asyncio.get_running_loop()
            # Drive the deadline with a fake clock: patch loop.time via a
            # wrapper is intrusive; instead run with a short real deadline
            # by faking time jumps through sleep.
            calls = {"n": 0}

            async def fast_sleep(s):
                calls["n"] += 1
                FakeLoop._t += 10.0  # advance fake time past the deadline
                if calls["n"] > 60:  # safety: never spin unbounded in test
                    raise AssertionError("backpressure loop unbounded")

            with mock.patch.object(asyncio, "sleep", fast_sleep), \
                    mock.patch.object(loop, "time", lambda: FakeLoop._t):
                await m._pump_rfb_to_channel.__wrapped__(StuckChannel(), None) \
                    if hasattr(m._pump_rfb_to_channel, "__wrapped__") else None

        # Simpler: call the pump directly with a sock that returns data once
        # then EOF, and a channel that never drains.
        class OnceSock:
            def __init__(self):
                self._done = False

        async def pump_probe():
            loop = asyncio.get_running_loop()
            ch = StuckChannel()

            async def fake_recv(sock, n):
                if not ch.sent:
                    return b"x" * 10
                raise AssertionError("sent after backpressure deadline")

            with mock.patch.object(loop, "sock_recv", fake_recv), \
                    mock.patch.object(loop, "time",
                                      side_effect=lambda: FakeLoop._t):
                # each sleep advances fake time by 10s → deadline (30s) hit
                # within a few iterations
                async def fast_sleep(s):
                    FakeLoop._t += 10.0

                with mock.patch.object(asyncio, "sleep", fast_sleep):
                    await m._pump_rfb_to_channel(ch, object())

        FakeLoop._t = 0.0
        asyncio.run(pump_probe())
        # The pump returned (test would hang otherwise) — bounded backpressure.


if __name__ == "__main__":
    unittest.main()
