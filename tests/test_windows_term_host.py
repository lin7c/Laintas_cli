"""The terminal host that connects the Windows terminal window to the CLI."""
import importlib.util
import os
import struct
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(ROOT, "build", "windows", "term_host.py")
_spec = importlib.util.spec_from_file_location("term_host", HOST)
term_host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(term_host)


class _Session:
    def __init__(self, script: str, rows: int = 24, cols: int = 80):
        self.proc = subprocess.Popen(
            [sys.executable, HOST, "--rows", str(rows), "--cols", str(cols),
             "--", "bash", "--norc", "--noprofile", "-c", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self.reader = term_host.FrameReader()
        self.output = b""
        self.hello = None
        self.exit_status = None

    def send(self, kind: bytes, payload: bytes = b"") -> None:
        self.proc.stdin.write(term_host.encode(kind, payload))
        self.proc.stdin.flush()

    def pump(self, until, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and not until(self):
            data = os.read(self.proc.stdout.fileno(), 65536)
            if not data:
                break
            for kind, payload in self.reader.feed(data):
                if kind == b"O":
                    self.output += payload
                elif kind == b"H":
                    self.hello = payload
                elif kind == b"E":
                    self.exit_status = struct.unpack(">i", payload)[0]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


class FrameCodecTest(unittest.TestCase):
    def test_frames_survive_arbitrary_chunking(self):
        stream = term_host.encode(b"I", b"abc") + term_host.encode(b"X") + term_host.encode(b"O", b"\x00" * 300)
        reader = term_host.FrameReader()
        frames = []
        for i in range(len(stream)):
            frames += reader.feed(stream[i:i + 1])
        self.assertEqual(frames, [(b"I", b"abc"), (b"X", b""), (b"O", b"\x00" * 300)])

    def test_oversized_frame_is_refused(self):
        reader = term_host.FrameReader()
        with self.assertRaises(ValueError):
            reader.feed(term_host.HEADER.pack(b"I", term_host.MAX_PAYLOAD + 1))


class TermHostTest(unittest.TestCase):
    def _session(self, script, **kw):
        session = _Session(script, **kw)
        self.addCleanup(session.close)
        return session

    def test_child_gets_a_terminal_of_the_requested_size(self):
        s = self._session("test -t 0 && test -t 1 && stty size", rows=31, cols=97)
        s.pump(lambda s: s.exit_status is not None)
        self.assertIn(b"31 97", s.output)
        self.assertEqual(s.exit_status, 0)
        self.assertIn(b'"protocol": 1', s.hello)

    def test_input_and_exit_status_are_relayed(self):
        s = self._session("read line; echo got-$line; exit 7")
        s.send(b"I", b"hello\n")
        s.pump(lambda s: s.exit_status is not None)
        self.assertIn(b"got-hello", s.output)
        self.assertEqual(s.exit_status, 7)

    def test_resize_reaches_the_child(self):
        s = self._session("echo ready; read line; stty size")
        s.pump(lambda s: b"ready" in s.output)
        s.send(b"R", struct.pack(">HH", 40, 120))
        s.send(b"I", b"\n")
        s.pump(lambda s: s.exit_status is not None)
        self.assertIn(b"40 120", s.output)

    def test_escape_sequences_pass_through_unchanged(self):
        s = self._session(r"printf '\033[38;2;1;2;3mX\033[0m\033[?2004h'")
        s.pump(lambda s: s.exit_status is not None)
        self.assertIn(b"\x1b[38;2;1;2;3mX\x1b[0m\x1b[?2004h", s.output)

    def test_closing_the_window_ends_the_child(self):
        s = self._session("echo $$; echo ready; sleep 300")
        s.pump(lambda s: b"ready" in s.output)
        child = int(s.output.split()[0])
        s.proc.stdin.close()
        s.pump(lambda s: s.exit_status is not None)
        self.assertEqual(s.proc.wait(timeout=10), s.exit_status)
        self.assertNotEqual(s.exit_status, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_stopping_the_host_ends_the_child(self):
        s = self._session("echo $$; echo ready; sleep 300")
        s.pump(lambda s: b"ready" in s.output)
        child = int(s.output.split()[0])
        s.proc.terminate()
        s.proc.wait(timeout=15)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_close_frame_ends_the_child(self):
        s = self._session("echo ready; sleep 300")
        s.pump(lambda s: b"ready" in s.output)
        s.send(b"X")
        s.pump(lambda s: s.exit_status is not None)
        self.assertIsNotNone(s.exit_status)

    def test_missing_program_reports_127(self):
        proc = subprocess.Popen(
            [sys.executable, HOST, "--", "/nonexistent/laintas-program"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stdin.close)
        self.assertEqual(proc.wait(timeout=15), 127)

if __name__ == "__main__":
    unittest.main()
