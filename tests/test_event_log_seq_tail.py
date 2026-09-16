"""Recovering the last sequence number must not read the whole log.

The log is append-only and never truncated: on a working machine it reached
41 MB and 57k events, and finding the last `seq` by reading all of it took
1.8 seconds. That is paid once per process, at the first event of a session,
and it grows without bound — so the number is read from the tail instead.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import event_log


def _write(path, seqs, pad=0):
    with open(path, "w", encoding="utf-8") as fh:
        for s in seqs:
            fh.write(json.dumps({"seq": s, "type": "t", "pad": "x" * pad}) + "\n")
    return path


class LastSeqFromTail(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"

    def test_reads_the_last_sequence(self):
        _write(self.path, [1, 2, 3])
        self.assertEqual(event_log._last_seq_in(self.path), 3)

    def test_a_file_smaller_than_the_window_still_works(self):
        _write(self.path, [7])
        self.assertEqual(event_log._last_seq_in(self.path), 7)

    def test_a_missing_file_is_zero(self):
        self.assertEqual(event_log._last_seq_in(self.path), 0)

    def test_an_empty_file_is_zero(self):
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(event_log._last_seq_in(self.path), 0)

    def test_the_partial_line_a_seek_lands_in_is_discarded(self):
        """Seeking into the middle of a line yields invalid JSON; it must be
        skipped rather than read as the newest record."""
        _write(self.path, range(1, 400), pad=500)   # comfortably over 64 KB
        self.assertEqual(event_log._last_seq_in(self.path), 399)

    def test_it_widens_when_the_tail_holds_no_sequence(self):
        """A run of seq-less trailing lines must not read as 'no events'."""
        _write(self.path, [11])
        with open(self.path, "a", encoding="utf-8") as fh:
            for _ in range(2000):
                fh.write(json.dumps({"type": "noseq", "pad": "y" * 200}) + "\n")
        self.assertEqual(event_log._last_seq_in(self.path), 11)

    def test_trailing_junk_does_not_break_it(self):
        _write(self.path, [5])
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        self.assertEqual(event_log._last_seq_in(self.path), 5)

    def test_a_large_log_is_read_quickly(self):
        """The property the change exists for, pinned loosely enough to be
        stable on a loaded machine: a big log costs about what a small one
        does, not proportionally more."""
        _write(self.path, range(1, 20001), pad=600)   # ~12 MB
        self.assertGreater(self.path.stat().st_size, 8_000_000)
        start = time.perf_counter()
        self.assertEqual(event_log._last_seq_in(self.path), 20000)
        self.assertLess(time.perf_counter() - start, 0.25)

    def test_next_seq_continues_from_what_was_recovered(self):
        _write(self.path, [41])
        event_log._SEQ_BY_PATH.pop(str(self.path.resolve()), None)
        self.assertEqual(event_log._next_seq(self.path), 42)
        self.assertEqual(event_log._next_seq(self.path), 43)


if __name__ == "__main__":
    unittest.main()
