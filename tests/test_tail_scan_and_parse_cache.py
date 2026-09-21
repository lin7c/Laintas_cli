"""Correctness locks for two hot-path optimisations.

event_log.last_incomplete_task reads the log backwards in growing windows.
A single event longer than the first window (a prompt with a pasted file)
used to be skipped: the window started inside that line, the scan moved its
"consumed" mark past it anyway, and the next window read only a truncated
prefix that failed to parse — crash recovery then missed the pending task.

memory_system._parse_memory_file caches parsed memories by stat. File
timestamps are tick-coarse and os.replace recycles inodes, so a same-length
rewrite could reproduce an older version's stat key and read back stale;
every in-module write now drops the path's cache entries. Hits are deep
copies, so a caller mutating nested meta cannot poison the cache.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import event_log
import memory_system


class LongEventTailScanTests(unittest.TestCase):
    def _scan(self, events):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps(e) + "\n" for e in events),
                            encoding="utf-8")
            with mock.patch.object(event_log, "_log_path", lambda: path):
                return event_log.last_incomplete_task()

    def test_pending_prompt_longer_than_first_window(self):
        pad = [{"type": "tool_result", "out": "x" * 200} for _ in range(600)]
        big = {"type": "prompt_admitted", "run_id": "r1", "id": "BIG",
               "prompt": "y" * (event_log._SEQ_TAIL_WINDOWS[0] + 36_000)}
        found = self._scan(pad + [big])
        self.assertIsNotNone(found, "oversized pending prompt was skipped")
        self.assertEqual(found["id"], "BIG")

    def test_pending_prompt_longer_than_every_window(self):
        huge = {"type": "prompt_admitted", "run_id": "r1", "id": "HUGE",
                "prompt": "z" * (event_log._SEQ_TAIL_WINDOWS[-1] + 10_000)}
        self.assertEqual(self._scan([huge])["id"], "HUGE")

    def test_oversized_prompt_that_ended_is_not_pending(self):
        big = {"type": "prompt_admitted", "run_id": "r1",
               "prompt": "y" * 100_000}
        self.assertIsNone(self._scan([big, {"type": "turn_ended",
                                            "run_id": "r1"}]))


class ParseCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / "x.md"

    def _write(self, desc):
        memory_system._atomic_write_text(
            self.file, f"---\nname: x\ndescription: {desc}\nmetadata:\n"
                       f"  type: user\n---\n\nbody\n")

    def test_same_length_rewrites_never_read_stale(self):
        stale = 0
        for _ in range(500):
            for desc in ("aaaa", "bbbb", "cccc"):   # equal lengths, same tick
                self._write(desc)
                meta, _body = memory_system._parse_memory_file(self.file)
                stale += meta["description"] != desc
        self.assertEqual(stale, 0)

    def test_caller_mutation_does_not_poison_cache(self):
        self._write("hello")
        memory_system._parse_memory_file(self.file)          # miss: fills cache
        meta, _ = memory_system._parse_memory_file(self.file)  # hit
        meta["metadata"]["type"] = "POISONED"
        meta["description"] = "changed"
        again, _ = memory_system._parse_memory_file(self.file)
        self.assertEqual(again["metadata"], {"type": "user"})
        self.assertEqual(again["description"], "hello")

    def test_deleted_file_is_not_served_from_cache(self):
        self._write("hello")
        memory_system._parse_memory_file(self.file)
        self.file.unlink()
        memory_system._forget_parsed(self.file)
        self.assertIsNone(memory_system._parse_memory_file(self.file))


if __name__ == "__main__":
    unittest.main()
