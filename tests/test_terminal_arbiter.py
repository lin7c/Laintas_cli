"""Tests for the single-owner terminal arbiter.

The bugs this module exists to prevent are concurrency bugs, so the important
tests here are the ones that run threads: a parser test that passes says the
grammar is right, but only the stress test says the *ownership* is right.
"""

import os
import pty
import queue
import select
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import terminal_arbiter as ta
from terminal_arbiter import Key, Mode, TerminalArbiter, TerminalBusy


class KeyParserTests(unittest.TestCase):
    """The parser must never drop or tear input.

    Every one of these cases corresponds to a byte sequence the old
    hand-rolled readers either discarded (`os.read(fd, 32)` after an ESC) or
    delivered as literal text (a torn bracketed-paste wrapper).
    """

    def setUp(self):
        self.parser = ta._KeyParser()

    def names(self, data: bytes):
        return [k.name for k in self.parser.feed(data)]

    def test_ctrl_c_is_a_named_key_not_text(self):
        # The original defect: 0x03 fell through to "decode it and echo it",
        # so Ctrl+C during an agent run was consumed as a character.
        keys = self.parser.feed(b"\x03")
        self.assertEqual([Key("ctrl-c")], keys)
        self.assertFalse(keys[0].is_text)

    def test_every_c0_byte_produces_a_key(self):
        for byte in range(0x00, 0x20):
            if byte == 0x1b:
                continue          # ESC is stateful; covered separately
            parser = ta._KeyParser()
            keys = parser.feed(bytes([byte]))
            self.assertEqual(1, len(keys), f"byte {byte:#04x} produced {keys}")
            self.assertFalse(keys[0].is_text, f"byte {byte:#04x} became text")

    def test_arrow_key_is_one_key(self):
        self.assertEqual(["up"], self.names(b"\x1b[A"))

    def test_escape_sequence_split_across_reads_is_not_torn(self):
        # Two threads reading one byte at a time is how this used to break.
        # One reader saw ESC, the other saw "[A" and typed it into the buffer.
        self.assertEqual([], self.names(b"\x1b"))
        self.assertEqual([], self.names(b"["))
        self.assertEqual(["up"], self.names(b"A"))

    def test_bare_escape_needs_an_explicit_flush(self):
        self.assertEqual([], self.names(b"\x1b"))
        self.assertTrue(self.parser.escape_pending)
        self.assertEqual([Key("escape")], self.parser.flush_escape())
        self.assertFalse(self.parser.escape_pending)

    def test_flush_does_not_fire_mid_sequence(self):
        self.parser.feed(b"\x1b[")
        self.assertEqual([], self.parser.flush_escape())

    def test_bracketed_paste_becomes_one_key(self):
        keys = self.parser.feed(b"\x1b[200~hello world\x1b[201~")
        self.assertEqual([Key("paste", "hello world")], keys)

    def test_control_chars_inside_paste_stay_literal(self):
        # A pasted snippet containing 0x03 must not be read as an interrupt.
        keys = self.parser.feed(b"\x1b[200~a\x03b\x1b[201~")
        self.assertEqual([Key("paste", "a\x03b")], keys)

    def test_split_utf8_is_held_until_complete(self):
        self.assertEqual([], self.parser.feed(b"\xe4\xb8"))
        self.assertEqual([Key("text", "中")], self.parser.feed(b"\xad"))

    def test_printable_run_is_batched(self):
        self.assertEqual([Key("text", "abc")], self.parser.feed(b"abc"))

    def test_unknown_sequence_is_reported_not_discarded(self):
        keys = self.parser.feed(b"\x1b[99;99R")
        self.assertEqual(["unknown"], [k.name for k in keys])


class _PtyArbiterTestCase(unittest.TestCase):
    """Base class giving each test a real pty to arbitrate."""

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(self._close_pty)
        self.arbiter = TerminalArbiter(fd=self.slave)
        self.addCleanup(self.arbiter.shutdown)
        self.assertTrue(self.arbiter.interactive)

    def _close_pty(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def send(self, data: bytes):
        os.write(self.master, data)

    def lflag(self):
        return termios.tcgetattr(self.slave)[3]


class ModeRestorationTests(_PtyArbiterTestCase):
    """PRISTINE is the only baseline — modes must never accumulate."""

    def test_modes_are_computed_from_pristine_not_from_current_state(self):
        pristine = termios.tcgetattr(self.slave)
        with self.arbiter.hold("outer", Mode.RAW):
            pass
        self.assertEqual(pristine[3], self.lflag())

    def test_nested_hold_pops_back_to_the_outer_mode(self):
        with self.arbiter.hold("outer", Mode.CBREAK):
            outer = self.lflag()
            with self.arbiter.hold("inner", Mode.RAW):
                self.assertNotEqual(outer, self.lflag())
            self.assertEqual(outer, self.lflag(),
                             "inner hold restored the wrong mode")

    def test_cbreak_keeps_isig_so_ctrl_c_still_signals(self):
        # An approval prompt in raw mode is a prompt you cannot Ctrl+C out of.
        with self.arbiter.hold("prompt", Mode.CBREAK):
            self.assertTrue(self.lflag() & termios.ISIG)

    def test_raw_clears_isig(self):
        with self.arbiter.hold("passthrough", Mode.RAW):
            self.assertFalse(self.lflag() & termios.ISIG)

    def test_reset_to_pristine_recovers_from_foreign_corruption(self):
        # Simulates a child process that died leaving the terminal in raw mode.
        pristine = termios.tcgetattr(self.slave)
        broken = list(pristine)
        broken[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        termios.tcsetattr(self.slave, termios.TCSANOW, broken)
        self.arbiter.reset_to_pristine()
        self.assertEqual(pristine[3], self.lflag())

    def test_external_mode_restores_pristine_on_exit(self):
        pristine = termios.tcgetattr(self.slave)
        with self.arbiter.hold("child", Mode.EXTERNAL):
            broken = list(pristine)
            broken[3] &= ~termios.ICANON
            termios.tcsetattr(self.slave, termios.TCSANOW, broken)
        self.assertEqual(pristine[3], self.lflag())


class OwnershipTests(_PtyArbiterTestCase):

    def test_second_thread_is_refused_with_the_owner_named(self):
        entered = threading.Event()
        release = threading.Event()
        errors: queue.Queue = queue.Queue()

        def holder():
            with self.arbiter.hold("first-owner", Mode.CBREAK):
                entered.set()
                release.wait(5)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(entered.wait(5))

        def contender():
            try:
                with self.arbiter.hold("second-owner", Mode.CBREAK,
                                       timeout=0.2):
                    errors.put(None)
            except TerminalBusy as exc:
                errors.put(exc)

        c = threading.Thread(target=contender, daemon=True)
        c.start()
        c.join(5)
        exc = errors.get(timeout=5)
        self.assertIsInstance(exc, TerminalBusy)
        self.assertEqual("first-owner", exc.owner)
        self.assertEqual("second-owner", exc.requester)

        release.set()
        t.join(5)

    def test_terminal_is_handed_over_after_release(self):
        with self.arbiter.hold("first", Mode.CBREAK):
            self.assertEqual("first", self.arbiter.current_owner())
        self.assertEqual("", self.arbiter.current_owner())
        with self.arbiter.hold("second", Mode.CBREAK):
            self.assertEqual("second", self.arbiter.current_owner())

    def test_same_thread_may_nest(self):
        with self.arbiter.hold("outer", Mode.CBREAK):
            with self.arbiter.hold("outer", Mode.RAW):
                self.assertEqual("outer", self.arbiter.current_owner())
            self.assertEqual(Mode.CBREAK, self.arbiter.current_mode())


class ReaderTests(_PtyArbiterTestCase):

    def test_keys_reach_the_holder(self):
        with self.arbiter.hold("reader", Mode.CBREAK) as term:
            self.send(b"x")
            self.assertEqual(Key("text", "x"), term.read_key(timeout=5))

    def test_ctrl_c_is_a_signal_in_cbreak_not_a_byte(self):
        # Not a quirk to work around — the reason CBREAK is the default for
        # prompts. The line discipline turns 0x03 into SIGINT before it ever
        # reaches a reader, so a prompt cannot swallow the user's interrupt.
        with self.arbiter.hold("reader", Mode.CBREAK) as term:
            self.send(b"\x03")
            self.assertIsNone(term.read_key(timeout=0.4))

    def test_ctrl_c_is_a_byte_in_raw(self):
        # RAW disables ISIG, so passthrough consumers must handle 0x03
        # themselves — and now they get it as a named key, not as text.
        with self.arbiter.hold("passthrough", Mode.RAW) as term:
            self.send(b"\x03")
            self.assertEqual(Key("ctrl-c"), term.read_key(timeout=5))

    def test_bare_escape_is_delivered_after_the_gap(self):
        with self.arbiter.hold("reader", Mode.CBREAK) as term:
            self.send(b"\x1b")
            key = term.read_key(timeout=5)
            self.assertEqual(Key("escape"), key)

    def test_arrow_key_does_not_surface_as_escape(self):
        with self.arbiter.hold("reader", Mode.CBREAK) as term:
            self.send(b"\x1b[A")
            self.assertEqual(Key("up"), term.read_key(timeout=5))

    def test_reader_is_parked_in_external_mode(self):
        # The whole point: a forked child on the inherited terminal must get
        # its own keystrokes. If the arbiter read here, it would eat them.
        with self.arbiter.hold("child", Mode.EXTERNAL) as term:
            self.send(b"hello")
            self.assertIsNone(term.read_key(timeout=0.4))
        # The bytes are still in the tty buffer for whoever reads next.
        with self.arbiter.hold("after", Mode.CBREAK) as term:
            self.assertEqual(Key("text", "hello"), term.read_key(timeout=5))

    def test_leftover_input_does_not_leak_to_the_next_owner(self):
        with self.arbiter.hold("first", Mode.CBREAK) as term:
            self.send(b"\x1b[")          # half an escape sequence
            time.sleep(0.2)
        with self.arbiter.hold("second", Mode.CBREAK) as term:
            self.send(b"A")
            key = term.read_key(timeout=5)
            self.assertEqual(Key("text", "A"), key,
                             "a torn sequence from the previous owner was "
                             "completed against the new owner's input")

    def test_read_line_collects_text_and_stops_at_enter(self):
        with self.arbiter.hold("liner", Mode.CBREAK) as term:
            self.send(b"hi there\r")
            self.assertEqual("hi there", term.read_line(timeout=5))

    def test_read_line_returns_none_on_ctrl_c(self):
        # RAW so the byte reaches us; in CBREAK the same keypress arrives as
        # SIGINT instead (see test_ctrl_c_is_a_signal_in_cbreak_not_a_byte).
        with self.arbiter.hold("liner", Mode.RAW) as term:
            self.send(b"abc\x03")
            self.assertIsNone(term.read_line(timeout=5))

    def test_read_line_returns_none_on_escape(self):
        with self.arbiter.hold("liner", Mode.CBREAK) as term:
            self.send(b"abc\x1b")
            self.assertIsNone(term.read_line(timeout=5))


class StressTests(_PtyArbiterTestCase):
    """The invariant under contention: whatever happens, the terminal ends up
    exactly as it started, and no two holders are ever live at once."""

    def test_concurrent_holders_never_overlap_and_always_restore(self):
        pristine = termios.tcgetattr(self.slave)
        live = []
        live_lock = threading.Lock()
        overlaps: queue.Queue = queue.Queue()
        modes = [Mode.CBREAK, Mode.RAW, Mode.COOKED, Mode.EXTERNAL]

        def worker(n):
            for i in range(25):
                mode = modes[(n + i) % len(modes)]
                try:
                    with self.arbiter.hold(f"w{n}", mode, timeout=10):
                        with live_lock:
                            live.append(n)
                            if len(live) != 1:
                                overlaps.put(list(live))
                        time.sleep(0.001)
                        with live_lock:
                            live.remove(n)
                except TerminalBusy as exc:
                    overlaps.put(exc)

        threads = [threading.Thread(target=worker, args=(n,), daemon=True)
                   for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
            self.assertFalse(t.is_alive(), "worker deadlocked")

        if not overlaps.empty():
            self.fail(f"ownership violated: {overlaps.get_nowait()}")
        self.assertIsNone(self.arbiter.current_mode())
        self.assertEqual(pristine[3], self.lflag(),
                         "terminal was not restored to its pristine state")


class ProcessExitTests(unittest.TestCase):
    """End-to-end: boot the real CLI on a pty and kill it.

    Everything else here tests the arbiter in isolation. This one exists
    because the failure the user actually reports is "my shell is broken
    after the CLI goes away" — ICANON and ECHO left off, typing invisible.
    Only running the whole program proves the restore is wired into the
    signal path, and a dropped SSH session delivers exactly this signal.
    """

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # This test boots the real CLI, which requires an authenticated session
    # (~/.laintas/session.json). Without one the CLI enters the device-login
    # flow and never reaches its prompt — that is an environment limitation,
    # not a regression. Skip instead of failing (e.g. on CI runners).
    @unittest.skipUnless(
        os.environ.get("LAINTAS_E2E_SESSION") == "1"
        or Path.home().joinpath(".laintas/session.json").exists()
        or os.path.exists(os.path.join(os.environ.get("LAINTAS_HOME", ""), "session.json")),
        "no saved login session: CLI cannot reach its prompt (CI runner)",
    )
    def test_terminal_is_pristine_after_sigterm(self):
        import signal
        import subprocess

        master, slave = pty.openpty()
        pristine = termios.tcgetattr(slave)

        proc = subprocess.Popen(
            [sys.executable, "laintas_cli.py"],
            stdin=slave, stdout=slave, stderr=slave, cwd=self.REPO,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        self.addCleanup(self._kill, proc)
        self.addCleanup(os.close, master)

        # Wait for the prompt caret (U+203A) to appear.
        seen = b""
        deadline = time.time() + 40
        while time.time() < deadline and "›".encode() not in seen:
            try:
                r, _, _ = select.select([master], [], [], 0.5)
            except OSError:
                break
            if r:
                try:
                    seen += os.read(master, 65536)
                except OSError:
                    break
        self.assertIn("›".encode(), seen, "CLI never reached its prompt")

        # Keep draining while it shuts down: a full pty buffer would block
        # the CLI's own shutdown output and make this look like a hang.
        draining = threading.Event()

        def _drain():
            while not draining.is_set():
                try:
                    r, _, _ = select.select([master], [], [], 0.2)
                    if r and not os.read(master, 65536):
                        return
                except OSError:
                    return

        drainer = threading.Thread(target=_drain, daemon=True)
        drainer.start()
        self.addCleanup(draining.set)

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except Exception:
            self.fail("CLI did not exit on SIGTERM")
        draining.set()
        drainer.join(timeout=5)

        self.assertEqual(
            pristine[3], termios.tcgetattr(master)[3],
            "terminal left dirty after exit — ICANON/ECHO state differs from "
            "the state the shell handed the CLI")

    @staticmethod
    def _kill(proc):
        import signal
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:
                pass


class NonInteractiveTests(unittest.TestCase):

    def test_hold_is_a_noop_without_a_tty(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        arbiter = TerminalArbiter(fd=r)
        self.assertFalse(arbiter.interactive)
        with arbiter.hold("nobody", Mode.CBREAK) as term:
            self.assertFalse(term.interactive)
            self.assertIsNone(term.read_key(timeout=0.05))

    def test_missing_fd_does_not_raise(self):
        arbiter = TerminalArbiter(fd=-1)
        self.assertFalse(arbiter.interactive)
        self.assertEqual("", arbiter.current_owner())



class TerminalModeRestoreTests(unittest.TestCase):
    """Restoring termios is not restoring the terminal.

    Mouse reporting, bracketed paste and cursor visibility are DEC private
    modes held by the emulator, and they outlive the process that set them.
    Putting only termios back makes the shell echo again, so the terminal
    looks recovered while it keeps sending mouse reports into whatever runs
    next — which then arrive with no one holding the terminal, in canonical
    mode with echo, and get printed as ``^[[<35;46;1M``.
    """

    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        from terminal_arbiter import TerminalArbiter
        self.arbiter = TerminalArbiter(fd=self.slave)

    def _read_available(self):
        chunks = []
        while select.select([self.master], [], [], 0.2)[0]:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)

    def test_construction_clears_modes_a_previous_run_left_behind(self):
        # A process killed with os._exit or a signal never writes the disable
        # sequence, so the next start is the only chance to heal the terminal
        # without the user closing the window.
        written = self._read_available()
        self.assertIn(b"\x1b[?1003l", written)
        self.assertIn(b"\x1b[?1006l", written)

    def test_reset_to_pristine_turns_mouse_reporting_off(self):
        self._read_available()                    # discard the constructor's
        self.arbiter.reset_to_pristine()
        written = self._read_available()
        for mode in (b"1000l", b"1002l", b"1003l", b"1015l", b"1006l"):
            self.assertIn(b"\x1b[?" + mode, written,
                          f"reset must disable mode {mode!r}")
        self.assertIn(b"\x1b[?2004l", written)   # bracketed paste
        self.assertIn(b"\x1b[?25h", written)     # cursor visible

    def test_reset_leaves_the_alternate_screen_alone(self):
        # A full-screen UI owns that buffer and is entitled to be mid-render
        # when a crash handler runs.
        self._read_available()
        self.arbiter.reset_to_pristine()
        self.assertNotIn(b"\x1b[?1049", self._read_available())

    def test_a_non_terminal_fd_writes_nothing(self):
        from terminal_arbiter import TerminalArbiter
        with tempfile.TemporaryFile() as handle:
            quiet = TerminalArbiter(fd=handle.fileno())
            quiet.reset_to_pristine()
            handle.seek(0)
            self.assertEqual(b"", handle.read())


if __name__ == "__main__":
    unittest.main()