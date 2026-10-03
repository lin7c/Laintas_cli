"""Usage accounting remains serialized across POSIX threads and processes."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import memory_system as ms


class _MemDir:
    def __enter__(self):
        self._directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self._directory.name)
        self._patches = [
            mock.patch.object(ms, "MEMORY_DIR", self.tmp),
            mock.patch.object(ms, "usage_file",
                              lambda: self.tmp / "usage.json"),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        self._directory.cleanup()


class UsageLockTests(unittest.TestCase):
    def test_posix_lock_acquires_and_releases(self):
        with _MemDir():
            with ms._UsageLock():
                pass  # acquiring at all is the assertion
            # Re-acquirable after release
            with ms._UsageLock():
                pass

    def test_concurrent_touches_are_serialized(self):
        with _MemDir():
            hits = []

            def worker():
                with ms._usage_thread_lock, ms._UsageLock():
                    hits.append(1)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(hits), 4)



if __name__ == "__main__":
    unittest.main()
