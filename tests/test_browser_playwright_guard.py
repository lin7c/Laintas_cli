"""Playwright's sync API must never be able to spin forever.

The bug these tests pin cost 47 minutes of a wedged CLI: `browser.close()` on a
session whose driver connection had already gone away. Playwright's sync wait
is

    while not task.done():
        self._dispatcher_fiber.switch()

and switching to a *dead* greenlet returns immediately, so the loop becomes an
uninterruptible spin at 100% CPU — no timeout, no exit, GIL held. Nothing about
it is specific to close(); every sync call had the same hole.

So the properties here are about the hole, not about close():
  * a dead dispatcher fiber raises instead of spinning,
  * a fiber that makes no progress is abandoned at the cap,
  * a connection that cannot be used is recognised locally, before a call,
  * and teardown frees Chrome even when the Playwright half fails.
"""
import asyncio
import os
import signal
import subprocess
import threading
import time
import unittest
from unittest import mock

import browser_session


def _live_fiber():
    """A dispatcher fiber that is alive but never advances the loop.

    This is what upstream's loop assumes cannot happen: control comes straight
    back with the task still pending.
    """
    import greenlet
    parent = greenlet.getcurrent()

    def body():
        while True:
            parent.switch()

    fiber = greenlet.greenlet(body)
    fiber.switch()          # start it; it parks on the first switch back
    return fiber


def _dead_fiber():
    import greenlet
    fiber = greenlet.greenlet(lambda: None)
    fiber.switch()          # runs to completion → dead
    return fiber


class SyncGuard(unittest.TestCase):
    """The patched SyncBase._sync, exercised against real Playwright internals."""

    def setUp(self):
        try:
            import greenlet  # noqa: F401
            from playwright._impl._sync_base import SyncBase
            from playwright._impl._errors import Error as PWError
        except ImportError as e:                       # pragma: no cover
            self.skipTest(f"playwright not installed: {e}")
        browser_session._install_pw_sync_guard()
        self.SyncBase = SyncBase
        self.PWError = PWError
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _call(self, fiber):
        """Invoke the guarded _sync with a task that can never complete."""
        async def never():                             # pragma: no cover
            await asyncio.sleep(3600)

        obj = mock.Mock(_loop=self.loop, _dispatcher_fiber=fiber)
        # In production the loop is dead when this happens, so the cancelled
        # task is simply garbage; here the loop is fine, and letting the
        # cancellation settle keeps the test output clean.
        self.addCleanup(self._settle)
        return self.SyncBase._sync(obj, never())

    def _settle(self):
        for task in asyncio.all_tasks(self.loop):
            task.cancel()
            try:
                self.loop.run_until_complete(task)
            except (asyncio.CancelledError, RuntimeError):
                pass

    def test_guard_is_installed_once(self):
        first = self.SyncBase._sync
        browser_session._install_pw_sync_guard()
        self.assertIs(self.SyncBase._sync, first)
        self.assertTrue(getattr(first, "_laintas_guarded", False))

    def test_dead_dispatcher_raises_instead_of_spinning(self):
        started = time.monotonic()
        with self.assertRaises(self.PWError) as caught:
            self._call(_dead_fiber())
        # The point is not just that it raises but that it raises *at once*:
        # the old loop would still be running when this assert would have run.
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("dispatcher fiber dead", str(caught.exception))

    def test_no_progress_is_abandoned_at_the_cap(self):
        started = time.monotonic()
        with browser_session._pw_sync_cap(0.2):
            with self.assertRaises(self.PWError) as caught:
                self._call(_live_fiber())
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("no progress", str(caught.exception))

    def test_cap_is_per_thread_and_restored(self):
        seen = {}

        def worker():
            seen["inside"] = getattr(browser_session._PW_CAP_TLS, "cap", None)

        with browser_session._pw_sync_cap(1.5):
            self.assertEqual(browser_session._PW_CAP_TLS.cap, 1.5)
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        self.assertIsNone(seen["inside"])
        self.assertIsNone(getattr(browser_session._PW_CAP_TLS, "cap", None))

    def test_closed_loop_still_reports_the_upstream_error(self):
        loop = asyncio.new_event_loop()
        loop.close()

        async def never():                             # pragma: no cover
            await asyncio.sleep(3600)

        obj = mock.Mock(_loop=loop, _dispatcher_fiber=_live_fiber())
        with self.assertRaises(self.PWError) as caught:
            self.SyncBase._sync(obj, never())
        self.assertIn("Event loop is closed", str(caught.exception))


def _session(**kw):
    """A BrowserSession that was never started — no Chrome, no Playwright."""
    return browser_session.BrowserSession(
        backend_url="http://localhost:8000", agent_id="test",
        session_id="guard-test", **kw)


class ConnectionState(unittest.TestCase):
    """_pw_alive / _pw_owned_here answer locally, without touching the wire."""

    def test_dead_fiber_is_not_alive(self):
        sess = _session()
        sess._pw_browser = mock.Mock(
            _loop=mock.Mock(is_closed=lambda: False),
            _dispatcher_fiber=_dead_fiber(), is_connected=True)
        self.assertFalse(sess._pw_alive())

    def test_disconnected_browser_is_not_alive(self):
        sess = _session()
        sess._pw_browser = mock.Mock(
            _loop=mock.Mock(is_closed=lambda: False),
            _dispatcher_fiber=_live_fiber(), is_connected=False)
        self.assertFalse(sess._pw_alive())

    def test_healthy_connection_is_alive(self):
        sess = _session()
        sess._pw_browser = mock.Mock(
            _loop=mock.Mock(is_closed=lambda: False),
            _dispatcher_fiber=_live_fiber(), is_connected=True)
        self.assertTrue(sess._pw_alive())

    def test_ownership_is_the_connecting_thread(self):
        sess = _session()
        self.assertTrue(sess._pw_owned_here())          # nobody owns it yet
        sess._pw_tid = threading.get_ident()
        self.assertTrue(sess._pw_owned_here())
        sess._pw_tid = threading.get_ident() + 1
        self.assertFalse(sess._pw_owned_here())

    def test_get_page_refuses_a_foreign_thread(self):
        sess = _session()
        sess._pw = mock.Mock()
        sess._pw_tid = threading.get_ident() + 1
        with self.assertRaises(RuntimeError) as caught:
            sess.get_page()
        self.assertIn("another thread", str(caught.exception))

    def test_get_page_reconnects_over_a_dead_connection(self):
        sess = _session()
        sess._pw = mock.Mock()
        sess._pw_tid = threading.get_ident()
        page = mock.Mock()
        sess._pw_browser = mock.Mock(
            _loop=mock.Mock(is_closed=lambda: False),
            _dispatcher_fiber=_dead_fiber(), is_connected=True,
            contexts=[mock.Mock(pages=[page])])
        dropped = []
        # _drop_pw is stubbed out, so _pw stays non-None and no real
        # reconnect is attempted; what is asserted is that the dead
        # connection was discarded rather than called into.
        with mock.patch.object(type(sess), "_drop_pw",
                               lambda self: dropped.append(True)):
            self.assertIs(sess.get_page(), page)
        self.assertEqual(dropped, [True])


class DropAndTeardown(unittest.TestCase):
    """Dropping a connection must not leak the driver; close must free Chrome."""

    def _sleeper(self, node=False):
        if node:
            # A real node process: _drop_pw refuses to signal a pid that is
            # not one, so a `sleep` would not exercise the kill at all.
            node_bin = None
            try:
                import playwright
                candidate = os.path.join(
                    os.path.dirname(playwright.__file__), "driver", "node")
                node_bin = candidate if os.path.exists(candidate) else None
            except ImportError:
                pass
            node_bin = node_bin or browser_session._which("node")
            if not node_bin:
                self.skipTest("no node binary to stand in for the driver")
            proc = subprocess.Popen([node_bin, "-e", "setTimeout(()=>{}, 60000)"])
        else:
            proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self._reap, proc)
        return proc

    def _reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def test_drop_pw_kills_the_driver_process(self):
        proc = self._sleeper(node=True)
        sess = _session()
        sess._pw = mock.Mock()
        sess._pw._impl_obj._connection._transport._proc = mock.Mock(
            pid=proc.pid, returncode=None)
        sess._pw_browser = mock.Mock()
        sess._pw_tid = threading.get_ident()

        sess._drop_pw()

        self.assertIsNone(sess._pw)
        self.assertIsNone(sess._pw_browser)
        self.assertIsNone(sess._pw_tid)
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(proc.poll(), -signal.SIGKILL)

    def test_drop_pw_spares_a_pid_that_is_no_longer_the_driver(self):
        """returncode is read from a loop that has stopped running, so it can
        say "alive" about a pid that exited and was reused. Killing on that
        word alone would shoot an innocent process."""
        victim = self._sleeper()          # not node → not our driver
        sess = _session()
        sess._pw = mock.Mock()
        sess._pw._impl_obj._connection._transport._proc = mock.Mock(
            pid=victim.pid, returncode=None)

        sess._drop_pw()

        time.sleep(0.2)
        self.assertIsNone(victim.poll())

    def test_drop_pw_survives_unexpected_internals(self):
        sess = _session()
        sess._pw = object()          # no _impl_obj at all
        sess._drop_pw()              # must not raise
        self.assertIsNone(sess._pw)

    def test_foreign_thread_teardown_drops_instead_of_calling(self):
        sess = _session()
        sess._pw = mock.Mock()
        sess._pw_browser = mock.Mock()
        sess._pw_tid = threading.get_ident() + 1
        with mock.patch.object(type(sess), "_drop_pw",
                               autospec=True) as drop:
            sess._close_playwright()
        drop.assert_called_once()
        sess._pw_browser.close.assert_not_called()

    def test_close_frees_chrome_even_when_playwright_teardown_raises(self):
        chrome = self._sleeper()
        xvfb = self._sleeper()
        sess = _session()
        sess._chrome = chrome
        sess._xvfb = xvfb
        sess.display_n = 4242        # not a real display; only lock paths
        with mock.patch.object(type(sess), "_close_playwright",
                               side_effect=RuntimeError("driver is wedged")):
            with self.assertRaises(RuntimeError):
                sess.close()
        self.assertIsNotNone(chrome.poll())
        self.assertIsNotNone(xvfb.poll())

    def test_close_is_idempotent(self):
        sess = _session()
        calls = []
        with mock.patch.object(type(sess), "_close_playwright",
                               lambda self: calls.append(True)):
            sess.close()
            sess.close()
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
