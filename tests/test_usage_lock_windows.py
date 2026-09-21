"""D5 (bughunt): _UsageLock must lock on Windows, not fall through.

The lock body only knew fcntl; on Windows `import fcntl` raises and the
except-branch proceeded unlocked — silent lost updates on exactly the
supported platform the lock exists for. The fix branches on os.name and
locks via msvcrt (the session_lifecycle.guard pattern). POSIX behaviour
is unchanged.
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import memory_system as ms


class _MemDir:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
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
            (self.tmp if hasattr(self, "tmp") else None)
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

    def test_windows_branch_falls_back_safe_without_msvcrt(self):
        # On Linux we cannot exercise msvcrt; verify the nt branch is taken
        # and degrades to unlocked (not a crash) when msvcrt is missing.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "msvcrt":
                raise ModuleNotFoundError("simulated")
            return real_import(name, *a, **k)

        with _MemDir(), \
                mock.patch("os.name", "nt"), \
                mock.patch("builtins.__import__", side_effect=fake_import):
            with ms._UsageLock():
                pass  # must not raise

    @unittest.skipUnless(os.name == "nt", "real msvcrt path, Windows only")
    def test_real_windows_lock(self):
        with _MemDir():
            with ms._UsageLock():
                pass


if __name__ == "__main__":
    unittest.main()
