"""Shell commands are bounded by SILENCE, never by how long they run.

The wall-clock budget these replace signalled the foreground process group of
commands that had been streaming output the whole time — it killed the long
build, which is the one thing you least want killed, and could not tell it from
a wedged process, which is the one thing you do.
"""

import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tools


class _Ctx:
    """Minimal ToolCtx stand-in for the subprocess path of shell.exec."""

    def __init__(self, cwd="/tmp"):
        self.deps = None
        self.agent_id = None
        self.session = None
        self.events_cb = None
        self.cwd = cwd
        self.task_cwd = cwd
        self.state = {}
        self.stationed_terminal = None
        self.interactive_session = None
        self.get_agent = None
        self.get_terminal = None
        self.depth = 1

    def __getattr__(self, name):      # any other ToolCtx attribute is absent
        return None


class ShellIdleBudgetTests(unittest.TestCase):
    def _run(self, command, **params):
        ctx = _Ctx()
        with mock.patch.object(tools, "_deployed_shell_session", return_value=None):
            return tools._bi_shell_exec({"command": command, **params}, ctx)

    def test_a_slow_but_talking_command_is_not_killed(self):
        """Runs for ~2.4s — well past its 1s idle budget — but never goes quiet
        for a whole second, so it must be allowed to finish."""
        started = time.monotonic()
        result = self._run(
            "for i in 1 2 3 4 5 6; do echo tick $i; sleep 0.4; done",
            timeout=1)
        elapsed = time.monotonic() - started

        self.assertTrue(result["ok"], result)
        self.assertGreater(elapsed, 2.0)          # it really did outlive the budget
        self.assertIn("tick 6", result["result"])

    def test_a_silent_command_is_stopped_and_keeps_its_output(self):
        result = self._run("echo before going quiet; sleep 30", timeout=1)

        self.assertFalse(result["ok"])
        self.assertIn("no output", result["error"])
        self.assertIn("before going quiet", result["result"])

    def test_a_normal_command_still_returns_its_output(self):
        result = self._run("echo hello world")
        self.assertTrue(result["ok"], result)
        self.assertIn("hello world", result["result"])

    def test_failure_exit_code_survives_the_rewrite(self):
        result = self._run("echo oops >&2; exit 3")
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 3)
        self.assertIn("oops", result["result"])

    def test_large_output_does_not_deadlock_the_pipe(self):
        """The old code used communicate() specifically to avoid a full-pipe
        deadlock; reading both pipes with select must keep that property."""
        result = self._run("seq 1 50000", timeout=10)
        self.assertTrue(result["ok"], result)
        self.assertIn("50000", result["result"])

    @staticmethod
    def _tree_command(pid_path):
        script = (
            "import os,signal,time; "
            "child=os.fork(); "
            "open(" + repr(str(pid_path)) + ", 'w').write(" 
            "f'{os.getpid()} {child}\\n') if child else None; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print(f'pids {os.getpid()} {child}', flush=True) if child else None; "
            "time.sleep(30)"
        )
        return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    @staticmethod
    def _read_pids(path, deadline=3.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                values = [int(value) for value in path.read_text().split()]
                if len(values) == 2 and all(values):
                    return values
            except (FileNotFoundError, OSError, ValueError):
                pass
            time.sleep(0.01)
        raise AssertionError(f"child pid file was not populated: {path}")

    @staticmethod
    def _assert_processes_stopped(pids, deadline=3.0):
        end = time.monotonic() + deadline
        remaining = set(pids)
        while remaining and time.monotonic() < end:
            for pid in list(remaining):
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().split()
                    if len(fields) > 2 and fields[2] == "Z":
                        remaining.remove(pid)
                except FileNotFoundError:
                    remaining.remove(pid)
                except OSError:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        remaining.remove(pid)
            if remaining:
                time.sleep(0.01)
        if remaining:
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        assert not remaining, f"processes survived shell.exec cleanup: {remaining}"

    def test_idle_timeout_stops_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as raw:
            pid_path = Path(raw) / "pids"
            result = self._run(self._tree_command(pid_path), timeout=1)
            pids = self._read_pids(pid_path)

            self.assertFalse(result["ok"])
            self.assertIn("no output", result["error"])
            self._assert_processes_stopped(pids)

    def test_pipe_read_error_stops_child_and_grandchild(self):
        with tempfile.TemporaryDirectory() as raw:
            pid_path = Path(raw) / "pids"

            def fail_after_spawn(_stream):
                self._read_pids(pid_path)
                raise OSError("synthetic pipe failure")

            with mock.patch.object(
                    tools, "_read_shell_pipe", side_effect=fail_after_spawn):
                result = self._run(self._tree_command(pid_path), timeout=10)
            pids = self._read_pids(pid_path)

            self.assertFalse(result["ok"])
            self.assertIn("synthetic pipe failure", result["error"])
            self._assert_processes_stopped(pids)

    def test_closed_pipes_with_live_process_stops_whole_group(self):
        with tempfile.TemporaryDirectory() as raw:
            pid_path = Path(raw) / "pids"

            def closed_after_spawn(_stream):
                self._read_pids(pid_path)
                return b""

            def both_ready(readers, _writes, _errors, _timeout):
                return list(readers), [], []

            with mock.patch("select.select", side_effect=both_ready), \
                    mock.patch.object(
                        tools, "_read_shell_pipe", side_effect=closed_after_spawn), \
                    mock.patch.object(
                        tools, "SHELL_PROCESS_EXIT_WAIT_SECONDS", 0.1):
                result = self._run(self._tree_command(pid_path), timeout=10)
            pids = self._read_pids(pid_path)

            self.assertFalse(result["ok"])
            self.assertIsNone(result["returncode"])
            self._assert_processes_stopped(pids)


if __name__ == "__main__":
    unittest.main()
