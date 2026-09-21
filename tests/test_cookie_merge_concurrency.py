"""D6 (bughunt): concurrent cookie merges must not drop each other.

merge() did an unlocked load→modify→save of the whole jar (save/clear
took the thread lock, but merge's own read was outside it): two writers
each rewrote the file from their own stale read and silently dropped the
other's cookies. The fix runs merge under the thread RLock plus a
cross-process flock sidecar (the workflow_state._store_lock pattern).
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import cookie_store as cs


class _Store:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p = mock.patch.object(cs, "COOKIE_FILE",
                                    self.tmp / "cookies.json")
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


def _cookie(name):
    return {"name": name, "value": "v", "domain": "example.com", "path": "/"}


class MergeConcurrencyTests(unittest.TestCase):
    def test_concurrent_merges_lose_nothing(self):
        with _Store():
            def worker(i):
                for k in range(10):
                    cs.merge([_cookie(f"c{i}-{k}")])

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            names = {c["name"] for c in cs.load(all_egress=True)}
            expected = {f"c{i}-{k}" for i in range(8) for k in range(10)}
            self.assertEqual(names, expected,
                             f"lost {len(expected - names)} cookies")

    def test_merge_upserts_existing(self):
        with _Store():
            self.assertEqual(cs.merge([_cookie("a")]), 1)
            # Same cookie again: no change counted, still one entry.
            self.assertEqual(cs.merge([_cookie("a")]), 0)
            # Changed value: counted, still one entry.
            changed = dict(_cookie("a"), value="v2")
            self.assertEqual(cs.merge([changed]), 1)
            entries = [c for c in cs.load(all_egress=True)
                       if c["name"] == "a"]
            self.assertEqual(len(entries), 1)

    def test_merge_keeps_cookies_from_other_egress(self):
        # The whole-jar read is the documented point of merge(); make sure
        # locking did not change it.
        with _Store():
            cs.merge([_cookie("via-a")])
            cs.merge([_cookie("via-b")])
            names = {c["name"] for c in cs.load(all_egress=True)}
            self.assertEqual(names, {"via-a", "via-b"})

    def test_expired_cookies_dropped(self):
        import time
        with _Store():
            stale = dict(_cookie("old"),
                         expires=time.time() - 1000)
            self.assertEqual(cs.merge([stale]), 0)


if __name__ == "__main__":
    unittest.main()
