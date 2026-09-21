"""K7 (bughunt): the opaque proxy relay must not park threads forever.

_serve set settimeout(None) once the connection went opaque, so a
half-dead peer (crashed without a TCP RST, NAT entry still warm) parked
the relay's threads in recv() forever — three threads per connection,
unbounded. The fix caps idle time at _IDLE_TIMEOUT (600s): a silent peer
retires the pipe, and the done event wakes the opposite direction.
"""
import socket
import threading
import time
import unittest

import browser_session as bs


def _half_dead_pair():
    """A socketpair whose far end goes silent without closing (the K7 shape:
    a peer that stopped sending but never sent a FIN/RST we can observe)."""
    a, b = socket.socketpair()
    return a, b


class ProxyRelayIdleTimeoutTests(unittest.TestCase):
    def test_idle_timeout_constant_exists_and_is_sane(self):
        # Long enough that live long-poll/websocket traffic never trips it,
        # short enough that dead relays retire within a session.
        self.assertGreater(bs.ProxyAuthRelay._IDLE_TIMEOUT, 60)
        self.assertLessEqual(bs.ProxyAuthRelay._IDLE_TIMEOUT, 3600)

    def test_pipe_exits_on_idle_timeout_and_wakes_peer(self):
        # Two sockets that never speak: the pipe must exit via the idle
        # timeout (not hang), set done, and unblock the opposite direction.
        relay_cls = bs.ProxyAuthRelay
        # Use a tiny timeout for the test by patching the class constant.
        src, dst = _half_dead_pair()
        # Make recv() time out fast: 0.3s idle cap.
        src.settimeout(0.3)
        done = threading.Event()
        exited = threading.Event()

        def run_pipe():
            relay_cls._pipe(src, dst, done)
            exited.set()

        t = threading.Thread(target=run_pipe, daemon=True)
        t.start()
        # The pipe must return on its own (deadline well below the test
        # runner's patience) — pre-fix this recv() blocked forever.
        self.assertTrue(exited.wait(timeout=5.0),
                        "pipe did not exit on idle timeout (K7)")
        self.assertTrue(done.is_set(),
                        "pipe exited without setting done — the opposite "
                        "direction would stay parked")

    def test_pipe_still_relays_live_data(self):
        # _pipe(src, dst) reads src.recv() and writes dst.sendall(). In a
        # socketpair, data sent on dst arrives at src and vice versa: feed
        # the pipe from dst, read its output on src.
        relay_cls = bs.ProxyAuthRelay
        src, dst = _half_dead_pair()
        done = threading.Event()
        received = []

        def run_pipe():
            relay_cls._pipe(src, dst, done)

        t = threading.Thread(target=run_pipe, daemon=True)
        t.start()
        dst.send(b"payload")  # → arrives at src → pipe relays it to dst
        deadline = time.monotonic() + 3
        src.setblocking(False)
        while not received and time.monotonic() < deadline:
            try:
                received.append(src.recv(65536))
            except BlockingIOError:
                time.sleep(0.02)
        self.assertEqual(received, [b"payload"])
        # Clean shutdown: closing the FEED side (dst) ends the pipe's
        # recv(src) with EOF; closing src itself would not wake a recv
        # already blocked on it.
        dst.close()
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertTrue(done.is_set())


if __name__ == "__main__":
    unittest.main()
