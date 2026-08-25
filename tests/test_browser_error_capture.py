"""The capture window must have no blind spot, and "nothing" must not read as "clean".

The bug these pin cost an investigation rather than a crash. Chrome was handed
the target URL on its own command line, so the first page load happened while
the browser was still booting — before Playwright had connected and before a
single listener existed. A React app that threw on mount therefore produced:

    browser.get_errors -> "No runtime errors captured
                           (no JS exceptions, console errors, or failed requests)."

which is a true statement about the buffers and a false one about the page. The
agent read the second meaning, concluded the tooling was at fault, and stopped —
while the app was in fact broken on every load in every browser.

Two properties close it, and neither is a check that can be forgotten:
  * the opening navigation happens through the instrumented page, so there is
    no window in which a load is unwatched;
  * an uninstrumented session reports that it captured nothing BECAUSE it was
    not watching, in words that cannot be read as a clean bill of health.
"""
import threading
import unittest
from unittest import mock

import browser_session
import tools


def _session(url="https://example.test/app"):
    """A session that was never started. The URL is set after construction so
    the test needs no DNS — validate_browse_url is a separate concern with its
    own tests."""
    sess = browser_session.BrowserSession(
        backend_url="http://localhost:8000", agent_id="t", session_id="test")
    sess.url = url
    return sess


class ChromeNeverNavigatesFirst(unittest.TestCase):
    def test_the_target_url_is_not_on_chromes_command_line(self):
        import tempfile
        sess = _session()
        captured = {}

        def fake_popen(args, **kwargs):
            captured["args"] = args
            return mock.Mock(pid=1234, poll=lambda: None)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(browser_session, "find_chrome",
                                  lambda: "/usr/bin/chrome"), \
                mock.patch.object(browser_session.subprocess, "Popen", fake_popen), \
                mock.patch.object(browser_session, "_unpriv_user", lambda: None):
            sess.user_data_dir = tmp
            sess._log_dir = tmp
            sess.display_n = 99
            sess.cdp_port = 9222
            sess._start_chrome()

        args = captured["args"]
        self.assertIn("about:blank", args)
        self.assertNotIn(sess.url, args)
        # It is the LAST argument that Chrome treats as the page to open.
        self.assertEqual(args[-1], "about:blank")

    def test_the_opening_navigation_goes_through_the_instrumented_page(self):
        """start() must reach the page only after listeners are attached."""
        sess = _session()
        order = []
        page = mock.Mock()
        page.goto.side_effect = lambda *a, **k: order.append("goto")

        def fake_get_page(self):
            if not self._monitoring:
                order.append("instrument")
                self._monitoring = True
            return page

        with mock.patch.object(type(sess), "_get_page", fake_get_page):
            sess.run(lambda p: p.goto(sess.url))
        self.assertEqual(order, ["instrument", "goto"])
        sess.close()

    def test_a_failed_opening_navigation_is_recorded_not_swallowed(self):
        sess = _session()
        self.assertIsNone(sess.initial_nav_error)
        page = mock.Mock()
        page.goto.side_effect = RuntimeError("net::ERR_CONNECTION_REFUSED")
        with mock.patch.object(type(sess), "_get_page", lambda self: page):
            try:
                sess.run(lambda p: p.goto(sess.url))
            except RuntimeError as e:
                sess.initial_nav_error = f"{type(e).__name__}: {e}"
        self.assertIn("ERR_CONNECTION_REFUSED", sess.initial_nav_error)
        sess.close()


class NothingIsNotClean(unittest.TestCase):
    """`clean` must mean "watched, and nothing happened"."""

    def _errors_for(self, sess):
        with mock.patch.object(tools, "_browser_resolve_session",
                               lambda params: (sess, None)):
            return tools._bi_browser_get_errors({}, mock.Mock())

    def test_an_uninstrumented_session_does_not_report_clean(self):
        sess = _session()
        self.assertFalse(sess.is_monitoring())
        out = self._errors_for(sess)
        self.assertFalse(out["clean"])
        self.assertFalse(out["monitored"])
        self.assertIn("NOT a clean result", out["result"])

    def test_an_instrumented_session_with_no_errors_is_clean(self):
        sess = _session()
        sess._monitoring = True
        out = self._errors_for(sess)
        self.assertTrue(out["clean"])
        self.assertTrue(out["monitored"])
        self.assertIn("No runtime errors captured", out["result"])

    def test_instrumenting_a_page_flips_the_flag(self):
        sess = _session()
        page = mock.Mock()
        sess._instrument(page)
        self.assertTrue(sess.is_monitoring())

    def test_console_makes_the_same_distinction(self):
        sess = _session()
        with mock.patch.object(tools, "_browser_resolve_session",
                               lambda params: (sess, None)):
            unwatched = tools._bi_browser_get_console({}, mock.Mock())
            sess._monitoring = True
            watched = tools._bi_browser_get_console({}, mock.Mock())
        self.assertFalse(unwatched["monitored"])
        self.assertIn("nothing captured", unwatched["result"])
        self.assertTrue(watched["monitored"])


if __name__ == "__main__":
    unittest.main()
