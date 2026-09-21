"""D1 (bughunt): the current-session pointer must not race.

save_session's pointer update (write for a live session, read-compare-
unlink for a closed one) ran unguarded. A concurrent close+save could
interleave — close reads current=X, save writes current=Y, close unlinks
current — losing the new session's pointer or resurrecting a closed
session. The fix wraps the pointer segment in session_lifecycle.guard,
which is per-thread reentrant (nested saves share the outer lock).
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import paths
import session_store


class _SessionsDir:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = paths.SESSIONS_DIR
        paths.SESSIONS_DIR = self.tmp / "sessions"
        paths.SESSIONS_DIR.mkdir(parents=True)
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        return self

    def __exit__(self, *exc):
        os.chdir(self._cwd)
        paths.SESSIONS_DIR = self._old

    def current(self):
        # The pointer file is <key>_current_<terminal_id>.json (and a legacy
        # <key>_current.json); ask session_store for the real path instead of
        # guessing filenames.
        cwd = str(self.tmp)
        for p in (session_store._current_path(cwd),
                  session_store._legacy_current_path(cwd)):
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        return None


def _mk(sid):
    return {"session_id": sid, "state": {"x": sid}, "terminal_id": "t1"}


class PointerRaceTests(unittest.TestCase):
    def test_concurrent_close_and_save_keeps_consistent_pointer(self):
        with _SessionsDir() as ctx:
            session_store.save_session(_mk("s2"))  # s2 is current
            barrier = threading.Barrier(2)
            errors = []

            def save_new():
                barrier.wait()
                try:
                    session_store.save_session(_mk("s1"))
                except Exception as e:
                    errors.append(e)

            def close_current():
                barrier.wait()
                try:
                    session_store.close_session(_mk("s2"))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=save_new),
                       threading.Thread(target=close_current)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            cur = ctx.current()
            # Never a resurrected closed session; a live session's pointer
            # is either the new one or absent (close won the race cleanly).
            self.assertTrue(cur is None or cur.get("session_id") != "s2")

    def test_close_then_save_live_pointer_is_new_session(self):
        with _SessionsDir() as ctx:
            session_store.save_session(_mk("old"))
            session_store.close_session(_mk("old"))
            session_store.save_session(_mk("new"))
            cur = ctx.current()
            self.assertIsNotNone(cur)
            self.assertEqual(cur.get("session_id"), "new")

    def test_save_closed_session_removes_its_pointer(self):
        with _SessionsDir() as ctx:
            session_store.save_session(_mk("doomed"))
            session_store.close_session(_mk("doomed"))
            self.assertIsNone(ctx.current())

    def test_many_concurrent_saves_pointer_survives(self):
        with _SessionsDir() as ctx:
            errors = []
            barrier = threading.Barrier(8)

            def worker(i):
                barrier.wait()
                try:
                    session_store.save_session(_mk(f"s{i}"))
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            cur = ctx.current()
            self.assertIsNotNone(cur, "pointer lost under concurrent saves")
            self.assertIn(cur.get("session_id"),
                          {f"s{i}" for i in range(8)})


if __name__ == "__main__":
    unittest.main()
