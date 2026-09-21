"""D4 (bughunt): MEMORY.md index updates must be atomic and serialized.

_update_index/_remove_from_index did a bare read-modify-write of the
shared index: concurrent writers lost each other's entries (last writer
wins) and a crash mid-write corrupted the whole index. The fix wraps the
RMW in a module RLock and lands via _atomic_write_text (tmp + replace).
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import memory_system as ms


class _IndexDir:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [
            mock.patch.object(ms, "MEMORY_DIR", self.tmp),
            mock.patch.object(ms, "MEMORY_INDEX", self.tmp / "MEMORY.md"),
        ]
        for p in self._patches:
            p.start()
        ms.ensure_memory_dir()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    def content(self) -> str:
        idx = self.tmp / "MEMORY.md"
        return idx.read_text(encoding="utf-8") if idx.exists() else ""


class IndexConcurrencyTests(unittest.TestCase):
    def test_concurrent_updates_lose_no_entries(self):
        with _IndexDir() as ctx:
            def worker(i):
                ms._update_index(f"mem-{i}", f"desc {i}", "project", "project")

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            content = ctx.content()
            for i in range(10):
                self.assertIn(f"mem-{i}", content,
                              f"entry mem-{i} lost to a concurrent writer")

    def test_update_replaces_existing_entry(self):
        with _IndexDir() as ctx:
            ms._update_index("m", "old", "project", "project")
            ms._update_index("m", "new", "project", "project")
            content = ctx.content()
            self.assertIn("new", content)
            self.assertNotIn("old", content)

    def test_remove_deletes_only_the_named_entry(self):
        with _IndexDir() as ctx:
            ms._update_index("keep", "k", "project", "project")
            ms._update_index("drop", "d", "project", "project")
            ms._remove_from_index("drop")
            content = ctx.content()
            self.assertIn("keep", content)
            self.assertNotIn("drop", content)

    def test_index_starts_empty_not_crashing(self):
        with _IndexDir() as ctx:
            ms._update_index("first", "f", "user", "user")
            self.assertIn("first", ctx.content())

    def test_concurrent_mixed_update_and_remove(self):
        with _IndexDir() as ctx:
            for i in range(6):
                ms._update_index(f"m{i}", f"d{i}", "project", "project")
            errors = []

            def updater(i):
                try:
                    ms._update_index(f"m{i}", f"updated {i}", "project", "project")
                except Exception as e:
                    errors.append(e)

            def remover(i):
                try:
                    ms._remove_from_index(f"m{i}")
                except Exception as e:
                    errors.append(e)

            threads = ([threading.Thread(target=updater, args=(i,))
                        for i in range(3)]
                       + [threading.Thread(target=remover, args=(i,))
                          for i in range(3, 6)])
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            content = ctx.content()
            for i in range(3):
                self.assertIn(f"m{i}", content)
            for i in range(3, 6):
                self.assertNotIn(f"m{i}", content)


if __name__ == "__main__":
    unittest.main()
