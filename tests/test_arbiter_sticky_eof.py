"""K2 (bughunt): EOF must be sticky for terminal_arbiter readers.

The reader loop dispatched one eof key to the current holder and stopped
reading. A later read_key(timeout=None) from the same holder — empty
inbox, no reader behind it — blocked forever: the key loop could never
notice the stream had died. The fix remembers EOF (set on both the EOF
and the dead-fd OSError path) and read_key/read_bytes return it
immediately from an empty inbox instead of parking.
"""
import queue
import unittest
from unittest import mock

import terminal_arbiter as ta


def _session_with_holder(raw_bytes: bool = False):
    """A TerminalSession over a hand-built holder/arbiter pair (no real tty).

    __new__ skips __init__, so the attributes the sticky-EOF path reads
    (_eof, and nothing else in these tests) are set by hand.
    """
    import threading
    arb = ta.TerminalArbiter.__new__(ta.TerminalArbiter)
    arb._eof = threading.Event()
    holder = ta._Holder(owner="test", mode=ta.Mode.CBREAK,
                        thread_id=0, raw_bytes=raw_bytes)
    sess = ta.TerminalSession(arb, holder, interactive=True)
    return arb, sess, holder


class StickyEofTests(unittest.TestCase):
    def test_read_key_returns_eof_immediately_after_eof(self):
        arb, sess, holder = _session_with_holder()
        arb._eof.set()
        # Empty inbox + sticky EOF: returns the eof key at once (would have
        # blocked forever pre-fix).
        key = sess.read_key(timeout=None)
        self.assertIsNotNone(key)
        self.assertEqual(key.name, "eof")

    def test_read_bytes_returns_empty_after_eof(self):
        arb, sess, holder = _session_with_holder(raw_bytes=True)
        arb._eof.set()
        self.assertEqual(sess.read_bytes(timeout=None), b"")

    def test_without_eof_empty_inbox_returns_none_on_timeout(self):
        arb, sess, holder = _session_with_holder()
        # _eof starts clear; timeout returns None (unchanged behaviour).
        self.assertIsNone(sess.read_key(timeout=0.05))

    def test_queued_keys_precede_sticky_eof(self):
        arb, sess, holder = _session_with_holder()
        holder.inbox.put(ta.Key("a"))
        arb._eof.set()
        self.assertEqual(sess.read_key(timeout=0.05).name, "a")

    def test_no_holder_returns_none(self):
        arb = ta.TerminalArbiter.__new__(ta.TerminalArbiter)
        arb._eof = __import__("threading").Event()
        sess = ta.TerminalSession(arb, None, interactive=True)
        self.assertIsNone(sess.read_key(timeout=0.05))


if __name__ == "__main__":
    unittest.main()
