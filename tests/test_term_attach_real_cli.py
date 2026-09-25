"""A command typed at the real CLI behaves like it would in a terminal.

Drives laintas_cli.py on a pty and reads the screen through pyte, so what is
asserted is what a user sees: live output, the keyboard reaching the program,
Ctrl+C stopping the command and not the CLI, cd persisting, `exec`/`exit`
ending the shell without hanging the CLI, Ctrl+] detaching and /fg returning.
"""
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

try:
    import pyte
except ImportError:          # pragma: no cover - dev dependency
    pyte = None


@unittest.skipIf(pyte is None, "pyte not installed")
@unittest.skipUnless(Path("/bin/bash").exists(), "needs bash")
class AttachedCommandsInTheRealCli(unittest.TestCase):
    COLS, ROWS = 140, 50

    def setUp(self):
        repo = Path(__file__).resolve().parents[1]
        self.dir = tempfile.TemporaryDirectory(prefix="laintas-attach-")
        self.addCleanup(self.dir.cleanup)
        self.work = Path(self.dir.name) / "work"
        self.work.mkdir()
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (self.ROWS, self.COLS))
        env = {**os.environ, "LAINTAS_HOME": self.dir.name + "/home",
               "LAINTAS_BACKEND": "http://127.0.0.1:1", "TERM": "xterm-256color",
               "LAINTAS_TERMINAL_ID": "attach-test", "PYTHONDONTWRITEBYTECODE": "1",
               "SHELL": "/bin/bash", "HOME": self.dir.name}
        # The pty's size is the terminal's size; an inherited COLUMNS/LINES
        # would override it.
        for name in ("TMUX", "COLUMNS", "LINES"):
            env.pop(name, None)
        self.process = subprocess.Popen(
            [sys.executable, str(repo / "laintas_cli.py"), "--depth", "1",
             "--terminal-name", "outer"], cwd=str(self.work), env=env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.master = master
        self.screen = pyte.Screen(self.COLS, self.ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.transcript = bytearray()
        self.addCleanup(self._stop)
        self.wait_for("primary@outer")

    def _stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
        os.close(self.master)

    def pump(self, seconds=0.1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                self.stream.feed(data)
                self.transcript.extend(data)
                if b"\x1b[6n" in data:
                    os.write(self.master, b"\x1b[1;1R")

    def text(self):
        return "\n".join(line.rstrip() for line in self.screen.display)

    def wait_for(self, needle, timeout=20, count=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if self.text().count(needle) >= count:
                return
            if self.process.poll() is not None:
                break
        self.fail(f"Missing {needle!r} (x{count}):\n{self.text()}\nRecent raw output:\n"
                  + repr(bytes(self.transcript[-6000:])))

    def type(self, data: bytes):
        os.write(self.master, data)

    def wait_raw(self, needle: bytes, since: int, timeout=20) -> int:
        """Wait for *needle* in the raw stream after offset *since*."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            at = self.transcript.find(needle, since)
            if at >= 0:
                return at + len(needle)
            self.pump(0.1)
            if self.process.poll() is not None:
                break
        self.fail(f"Missing raw {needle!r}:\n{self.text()}\nRecent raw output:\n"
                  + repr(bytes(self.transcript[-6000:])))

    def run_cmd(self, cmd: str):
        """Type a line and wait until the prompt has handed it over."""
        mark = len(self.transcript)
        self.type(cmd.encode() + b"\r")
        # prompt_toolkit turns bracketed paste off when the line is accepted.
        self.accepted = self.wait_raw(b"\x1b[?2004l", mark)

    def prompt_back(self, _count=None):
        """The CLI prompt is up again after the last command."""
        self.wait_raw(b"\x1b[?2004h", self.accepted)

    # ── the cases ──────────────────────────────────────────────────────

    def test_output_is_live_and_state_persists(self):
        self.run_cmd("for i in 1 2 3; do echo tick$i; sleep 1; done")
        self.wait_for("tick1", timeout=10)
        self.assertNotIn("tick3", self.text(), "output only appeared at the end")
        self.wait_for("tick3", timeout=10)
        self.prompt_back(2)
        self.run_cmd("cd /usr && export LAINTAS_T=kept")
        self.prompt_back(3)
        self.run_cmd("echo \"$PWD:$LAINTAS_T\"")
        self.wait_for("/usr:kept")

    def test_keyboard_reaches_the_program(self):
        self.run_cmd("read -p 'Your name: ' n; echo hello-$n")
        self.wait_raw(b"\r\nYour name: ", self.accepted)
        self.type(b"bob\r")
        self.wait_for("hello-bob")
        self.prompt_back(2)

    def test_ctrl_c_stops_the_command_not_the_cli(self):
        self.run_cmd("sleep 60")
        self.pump(1.0)
        self.type(b"\x03")
        self.prompt_back(2)
        self.assertIsNone(self.process.poll(), "Ctrl+C killed the CLI")
        self.run_cmd("echo still-here")
        self.wait_for("still-here")

    def test_exec_and_exit_do_not_hang(self):
        self.run_cmd("exec sh -c 'echo bye-from-exec'")
        self.wait_raw(b"\r\nbye-from-exec\r\n", self.accepted)
        self.wait_for("The shell exited")
        self.prompt_back(2)
        self.run_cmd("echo after-exec")
        self.wait_for("after-exec")
        self.prompt_back(3)
        self.run_cmd("exit")
        # Earlier lines scroll off the emulated screen; read the stream.
        self.wait_raw(b"The shell exited", self.accepted)
        self.prompt_back()
        self.run_cmd("echo after-exit")
        self.wait_for("after-exit")

    def test_interactive_program_in_the_same_shell(self):
        self.run_cmd("export LAINTAS_SEEN=inherited")
        self.prompt_back(2)
        self.run_cmd("python3 -q")
        self.wait_for(">>>")
        self.type(b"import os; print(os.environ.get('LAINTAS_SEEN'), 6*7)\r")
        self.wait_for("inherited 42")
        self.type(b"exit()\r")
        self.prompt_back(3)

    def test_detach_and_fg(self):
        self.run_cmd("sleep 3; echo LATE-OUTPUT")
        self.pump(0.8)
        self.type(b"\x1d")
        self.wait_for("keeps running in term0")
        self.prompt_back(2)
        self.run_cmd("echo while-busy")
        self.wait_for("still running")
        self.run_cmd("/fg")
        self.wait_for("LATE-OUTPUT", timeout=10)
        self.prompt_back(4)
        self.run_cmd("echo free-again")
        self.wait_for("free-again")

    def test_fullscreen_program_and_back(self):
        (self.work / "notes.txt").write_text("".join(f"line {i}\n" for i in range(200)))
        self.run_cmd("less notes.txt")
        self.wait_for("line 10")
        self.type(b"q")
        self.prompt_back()
        self.run_cmd("echo after-less")
        self.wait_for("after-less")

    def test_killed_fullscreen_program_leaves_a_normal_screen(self):
        self.run_cmd("printf '\\033[?1049h\\033[?1000hIN-ALT-SCREEN'; sleep 30")
        # The command line echo also contains the text: wait for the output.
        self.wait_raw(b"\x1b[?1000hIN-ALT-SCREEN", self.accepted)
        self.pump(0.5)
        self.type(b"\x03")
        self.prompt_back()
        tail = bytes(self.transcript[self.accepted:])
        self.assertIn(b"\x1b[?1049l", tail)
        self.assertIn(b"\x1b[?1000l", tail)
        self.run_cmd("echo back-on-main-screen")
        self.wait_for("back-on-main-screen")

    def test_esc_and_ctrl_d_reach_the_program(self):
        self.run_cmd("read -rsn1 k; printf 'key=%q\\n' \"$k\"")
        self.pump(0.5)
        self.type(b"\x1b")
        self.wait_for("key=$'\\E'")
        self.prompt_back()
        self.run_cmd("cat")
        self.pump(0.5)
        self.type(b"typed-into-cat\r")
        self.wait_for("typed-into-cat", count=2)   # the tty's echo, then cat's
        self.type(b"\x04")
        self.prompt_back()
        self.assertIsNone(self.process.poll())

    def test_a_clobbered_prompt_hook_does_not_hang(self):
        self.run_cmd("export PROMPT_COMMAND=true; PS0=''")
        self.prompt_back()
        self.run_cmd("echo survived-clobber")
        self.wait_for("survived-clobber")
        self.prompt_back()
        self.run_cmd("sleep 30")
        self.pump(0.8)
        self.type(b"\x03")      # the fallback hook was put back
        self.prompt_back()

    def test_exit_status_and_errexit_behave_as_in_a_terminal(self):
        self.run_cmd("false")
        self.prompt_back()
        self.run_cmd("echo status=$?")
        self.wait_for("status=1")
        self.prompt_back()
        # set -e never takes term0 (and the user's cd/venv/exports) down.
        self.run_cmd("set -e")
        self.prompt_back()
        self.run_cmd("false && true")
        self.prompt_back()
        self.run_cmd("false")
        self.prompt_back()
        self.run_cmd("echo alive-under-errexit")
        self.wait_raw(b"\r\nalive-under-errexit\r\n", self.accepted)
        self.prompt_back()
        self.assertNotIn(b"The shell exited", bytes(self.transcript))

    def test_history_gets_the_command_not_the_machinery(self):
        self.run_cmd("echo from-history-test")
        self.prompt_back()
        self.run_cmd("history 5")
        self.prompt_back()
        listing = bytes(self.transcript[self.accepted:])
        self.assertRegex(listing, rb"\d+  echo from-history-test")
        self.assertNotIn(b"__LAINTAS", listing)

    def test_background_job_returns_at_once(self):
        self.run_cmd("sleep 30 &")
        self.wait_raw(b"[1]", self.accepted, timeout=5)
        self.prompt_back()
        self.run_cmd("jobs")
        self.wait_for("Running")


if __name__ == "__main__":
    unittest.main()
