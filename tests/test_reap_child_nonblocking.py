"""K1 (bughunt): _reap_child must not block while holding output_lock.

_reap_child ran inside _read_output_unlocked (caller holds output_lock)
with a blocking waitpid(pid, 0). A child that closed its stdio but stayed
alive (daemonized/detached process) then hung every thread that wanted
the lock. The fix polls WNOHANG; close() passes wait=True with a bounded
retry after SIGKILL.
"""
import sys
import time
import unittest

sys.argv = [sys.argv[0]]

from laintas_cli import InteractiveSession


class ReapChildTests(unittest.TestCase):
    def test_reap_child_does_not_block_on_live_child(self):
        # Child closes its stdio but stays alive: the exact K1 shape.
        cmd = ("python3 -c 'import os,time; os.close(0); os.close(1); "
               "os.close(2); time.sleep(3)'")
        s = InteractiveSession(cmd, timeout=5)
        self.addCleanup(s.close)
        s.start()

        t0 = time.monotonic()
        for _ in range(10):
            s.read_output(timeout=0.05)
        elapsed = time.monotonic() - t0
        # 10 polls at 50ms must stay far below the child's 3s lifetime:
        # a blocking reap here would have taken >= 3s.
        self.assertLess(elapsed, 2.0,
                        f"read_output blocked for {elapsed:.2f}s — "
                        "reap is holding the output lock")
        self.assertEqual(s._returncode, -1)  # child still alive, not hung

    def test_reap_child_wait_true_reaps_after_kill(self):
        s = InteractiveSession("python3 -c 'import time; time.sleep(30)'",
                               timeout=5)
        s.start()
        s.close()  # SIGTERM -> SIGKILL -> bounded wait
        self.assertIsNotNone(s._returncode)
        self.assertNotEqual(s._returncode, -1)

    def test_reap_child_normal_exit_records_code(self):
        s = InteractiveSession("python3 -c 'print(1); pass'", timeout=5)
        s.start()
        deadline = time.monotonic() + 5
        while s._returncode == -1 and time.monotonic() < deadline:
            s.read_output(timeout=0.1)
        self.assertEqual(s._returncode, 0)


if __name__ == "__main__":
    unittest.main()
