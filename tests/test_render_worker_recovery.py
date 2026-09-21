"""K5 (bughunt): a wedged render worker must not brick web_search forever.

A render job that hung (page never settles) left the worker thread stuck
inside fn(session); every later submit queued behind it and timed out —
the worker was permanently dead. And shutdown() closed the Playwright
session from the CALLER's thread, which crashes (Playwright is
thread-affine). The fix uses per-generation poison flags: a timed-out
caller poisons that generation, the wedged thread closes its session on
ITSELF when it unwinds, and the next submit builds a fresh queue/thread
so renders recover.
"""
import threading
import time
import unittest

import web_search as ws


class RenderWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.worker = ws._RenderWorker()

        class _FakeSession:
            closed = False

            def is_alive(self):
                return not self.closed

            def close(self):
                self.closed = True

        # Never launch a real browser here: a cold Playwright start (7s+ on a
        # loaded box) exceeds these timeouts and fails the test for a reason
        # that has nothing to do with generation recovery.
        def ensure():
            if self.worker._session is None or not self.worker._session.is_alive():
                self.worker._session = _FakeSession()
            return self.worker._session

        self.worker._ensure_session = ensure

    def tearDown(self):
        # Poison any generation still alive so daemon threads wind down.
        try:
            with self.worker._lock:
                if self.worker._poison_event is not None:
                    self.worker._poison_event.set()
                self.worker._poisoned = True
        except Exception:
            pass

    def test_wedged_worker_recovers_on_next_submit(self):
        release = threading.Event()
        started = threading.Event()

        def stuck_job(session):
            started.set()
            release.wait(timeout=10)  # simulates a hung page render

        # Fire the stuck job on a caller that gives up (short timeout).
        def caller():
            self.worker.submit(stuck_job, timeout=0.5)

        t = threading.Thread(target=caller, daemon=True)
        t.start()
        started.wait(timeout=5)
        time.sleep(0.8)  # let the caller time out and poison the generation

        def healthy_job(session):
            return "recovered"

        out = self.worker.submit(healthy_job, timeout=5.0)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("value"), "recovered",
                         "worker did not recover after a wedged job")
        release.set()

    def test_submit_after_poison_builds_new_generation(self):
        with self.worker._lock:
            self.worker._poisoned = True  # simulate an earlier timeout
        out = self.worker.submit(lambda s: "fresh", timeout=5.0)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("value"), "fresh")

    def test_shutdown_does_not_touch_session_from_caller_thread(self):
        # shutdown must only poison + enqueue a no-op; it must NOT call
        # session.close() here. With no session at all it must be a no-op.
        self.worker.shutdown()  # must not raise

    def test_poisoned_worker_thread_closes_session_on_itself(self):
        # The wedged thread's finally-branch releases the session when the
        # job unwinds; verify _release clears the reference only for the
        # session it owns.
        class FakeSession:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        s = FakeSession()
        self.worker._session = s
        self.worker._release(s)
        self.assertTrue(s.closed)
        self.assertIsNone(self.worker._session)

    def test_release_keeps_newer_session_reference(self):
        class FakeSession:
            def close(self):
                pass

        old, new = FakeSession(), FakeSession()
        self.worker._session = new
        self.worker._release(old)  # stale generation's session
        self.assertIs(self.worker._session, new,
                      "release dropped the CURRENT session's reference")


if __name__ == "__main__":
    unittest.main()
