"""Esc must take effect while the backend call is blocked.

The bug these cover: `requests` blocks inside a socket read, and the only
interrupt checkpoint was "between two SSE lines". A reasoning model whose
provider buffers its thinking sends no lines at all during that phase, so
pressing Esc set the interrupt event and then nothing happened until the
model finished thinking on its own — up to the 120s read timeout.
"""

import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import laintas_cli


class _StalledResponse:
    """A response whose iter_lines() blocks like a real socket read does."""

    def __init__(self, lines=(), stall=30.0):
        self._lines = list(lines)
        self._stall = stall
        self.closed = threading.Event()
        self.entered = threading.Event()

    def iter_lines(self, decode_unicode=False):
        self.entered.set()
        for line in self._lines:
            yield line
        # Now behave like a provider that is thinking: emit nothing at all.
        self.closed.wait(self._stall)

    def close(self):
        self.closed.set()


class IterLinesInterruptibleTests(unittest.TestCase):

    def test_yields_lines_normally(self):
        resp = _StalledResponse(["a", "b", "c"], stall=0.05)
        event = threading.Event()
        got = list(laintas_cli._iter_lines_interruptible(resp, event))
        self.assertEqual(["a", "b", "c"], got)

    def test_no_interrupt_event_passes_straight_through(self):
        resp = _StalledResponse(["a"], stall=0.05)
        self.assertEqual(["a"], list(
            laintas_cli._iter_lines_interruptible(resp, None)))

    def test_interrupt_during_a_silent_stream_returns_promptly(self):
        # The regression: no lines are arriving, so the old code had no
        # checkpoint and sat here for the full read timeout.
        resp = _StalledResponse([], stall=30.0)
        event = threading.Event()
        threading.Timer(0.2, event.set).start()

        started = time.monotonic()
        got = list(laintas_cli._iter_lines_interruptible(resp, event))
        elapsed = time.monotonic() - started

        self.assertEqual([], got)
        self.assertLess(elapsed, 5.0,
                        "interrupt did not take effect while the stream was "
                        f"silent (took {elapsed:.1f}s)")

    def test_interrupt_closes_the_response(self):
        resp = _StalledResponse([], stall=30.0)
        event = threading.Event()
        threading.Timer(0.2, event.set).start()
        list(laintas_cli._iter_lines_interruptible(resp, event))
        self.assertTrue(resp.closed.is_set(),
                        "the reader thread was left parked on the socket")

    def test_partial_output_before_the_interrupt_is_preserved(self):
        resp = _StalledResponse(["first", "second"], stall=30.0)
        event = threading.Event()
        got = []
        for line in laintas_cli._iter_lines_interruptible(resp, event):
            got.append(line)
            if len(got) == 2:
                event.set()
        self.assertEqual(["first", "second"], got)

    def test_transport_errors_reach_the_consumer(self):
        class _Boom:
            def iter_lines(self, decode_unicode=False):
                raise OSError("connection reset")
                yield  # pragma: no cover

            def close(self):
                pass

        with self.assertRaises(OSError):
            list(laintas_cli._iter_lines_interruptible(_Boom(), threading.Event()))


class PostWithInterruptTests(unittest.TestCase):

    def _patch_post(self, fn):
        original = laintas_cli.requests.post
        laintas_cli.requests.post = fn
        self.addCleanup(setattr, laintas_cli.requests, "post", original)

    def test_returns_the_response(self):
        self._patch_post(lambda **kw: "response")
        self.assertEqual("response", laintas_cli._post_with_interrupt(
            threading.Event(), url="http://example.invalid"))

    def test_no_interrupt_event_calls_post_directly(self):
        calls = []
        self._patch_post(lambda **kw: calls.append(kw) or "r")
        self.assertEqual("r", laintas_cli._post_with_interrupt(
            None, url="http://example.invalid"))
        self.assertEqual(1, len(calls))

    def test_interrupt_while_waiting_for_headers_raises(self):
        # This is the other half of the thinking window: time-to-first-byte,
        # before iter_lines is even reached.
        release = threading.Event()
        self._patch_post(lambda **kw: (release.wait(30), "late")[1])
        self.addCleanup(release.set)

        event = threading.Event()
        threading.Timer(0.2, event.set).start()

        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            laintas_cli._post_with_interrupt(
                event, url="http://example.invalid")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_request_errors_propagate(self):
        def _boom(**kw):
            raise laintas_cli.requests.ConnectionError("nope")

        self._patch_post(_boom)
        with self.assertRaises(laintas_cli.requests.ConnectionError):
            laintas_cli._post_with_interrupt(
                threading.Event(), url="http://example.invalid")


if __name__ == "__main__":
    unittest.main()
