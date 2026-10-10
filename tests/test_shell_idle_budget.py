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


class BufferedPipelineBudgetTests(unittest.TestCase):
    """A pipeline ending in `tail`/`sort`/`wc` is silent BY CONSTRUCTION.

    Measured failure (seq 30209): `pytest | tail -20` was killed at 60s of
    silence. The silence was not a symptom — the last stage cannot print one
    byte until the stage before it exits, so the idle budget's whole premise
    ("silence means wedged") does not apply to it.
    """

    def test_a_buffering_tail_stage_is_recognised(self):
        for command in ("pytest tests/ | tail -20", "make | sort",
                        "grep -r x . | wc -l", "cat log | uniq",
                        "FOO=bar cmd | awk '{print}'", "a; b | sort"):
            self.assertTrue(tools._pipeline_buffers_output(command), command)

    def test_a_streaming_command_is_not_treated_as_buffering(self):
        for command in ("pytest tests/", "tail -f app.log", "sort big.txt",
                        "a || b", "echo hi", "git log | head", "cmd > out.log"):
            self.assertFalse(tools._pipeline_buffers_output(command), command)

    def test_a_buffering_pipeline_gets_a_longer_idle_budget(self):
        plain = tools._shell_idle_budget({"command": "pytest tests/"})
        piped = tools._shell_idle_budget({"command": "pytest tests/ | tail -20"})
        self.assertEqual(plain * tools.BUFFERED_PIPELINE_IDLE_FACTOR, piped)
        self.assertGreater(piped, plain)

    def test_an_explicit_timeout_is_still_scaled_not_ignored(self):
        """The model's own number stays authoritative in proportion: a caller
        that asks for 10s of silence gets 10s, buffered or not, times the
        factor — never a hardcoded floor that overrides what it asked for."""
        self.assertEqual(
            10.0 * tools.BUFFERED_PIPELINE_IDLE_FACTOR,
            tools._shell_idle_budget({"command": "pytest | tail", "timeout": 10}))
        self.assertEqual(10.0, tools._shell_idle_budget(
            {"command": "pytest", "timeout": 10}))

    def test_the_budget_comes_from_config_not_a_hardcoded_60(self):
        """The PTY path used `params.get("timeout", 60)` while the schema and
        `/config shell_idle_timeout` both say 120 — so the configured value was
        ignored exactly where long commands run."""
        import agent_loop
        with mock.patch.object(agent_loop, "get_runtime_config",
                               return_value=77.0):
            self.assertEqual(77.0, tools.shell_idle_timeout())
            self.assertEqual(
                77.0, tools._shell_idle_budget({"command": "pytest"}))

    def test_a_timeout_on_a_buffered_pipeline_says_how_to_fix_it(self):
        hint = tools.buffered_pipeline_hint("pytest | tail -20")
        self.assertIn("withholds all output", hint)
        self.assertIn("> /tmp/out.log", hint)
        self.assertIn("terminal.exec", hint)
        self.assertEqual("", tools.buffered_pipeline_hint("pytest"))

    def test_the_subprocess_timeout_error_carries_the_hint(self):
        result = ShellIdleBudgetTests._run(
            self, "sleep 30 | tail -5", timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("no output", result["error"])
        self.assertIn("withholds all output", result["error"])

    def test_the_factor_never_exceeds_the_configured_ceiling(self):
        """The multiplier is a convenience, not a way past what the
        administrator allowed — 4x of a 24h ceiling would be four days of
        silence. But max() must keep an explicit request authoritative, so a
        caller that already asked for more than the ceiling is never reduced.

        Base and ceiling are patched together: set_runtime_config validates a
        new value against _RUNTIME_LIMITS, so base > ceiling is not a state a
        real deployment can reach and asserting on it would test nothing.
        """
        import agent_loop
        with mock.patch.dict(agent_loop._RUNTIME_LIMITS,
                             {"shell_idle_timeout": (5, 100)}), \
                mock.patch.object(agent_loop, "get_runtime_config",
                                  return_value=60.0):
            self.assertEqual(100.0, tools._shell_idle_ceiling())
            self.assertEqual(60.0, tools._shell_idle_budget(
                {"command": "pytest"}))
            # 60 * 4 = 240, clamped to the ceiling.
            self.assertEqual(100.0, tools._shell_idle_budget(
                {"command": "pytest | tail"}))
            # 90 * 4 = 360 -> clamped to 100, and max() keeps it at the request
            # rather than dropping below it.
            self.assertEqual(100.0, tools._shell_idle_budget(
                {"command": "pytest | tail", "timeout": 90}))
            # An explicit 500 is above the ceiling and stays exactly that.
            self.assertEqual(500.0, tools._shell_idle_budget(
                {"command": "pytest | tail", "timeout": 500}))

    def test_the_bridge_path_applies_the_same_budget(self):
        """local_runtime.py:63 promises a command does not behave differently
        depending on which transport carried it. The HTTP path resolves its own
        timeout, so it has to scale the same way — otherwise `pytest | tail`
        dies at 120s over the bridge and survives in-process."""
        import local_runtime as lr
        self.assertEqual(120, lr._buffered_exec_timeout("pytest", 120))
        self.assertEqual(
            int(120 * tools.BUFFERED_PIPELINE_IDLE_FACTOR),
            lr._buffered_exec_timeout("pytest | tail -20", 120))
        # MAX_EXEC_TIMEOUT still caps the result.
        self.assertEqual(lr.MAX_EXEC_TIMEOUT,
                         lr._buffered_exec_timeout("pytest | tail",
                                                   lr.MAX_EXEC_TIMEOUT))
        self.assertIn("withholds all output",
                      lr._buffered_pipeline_hint("pytest | tail"))
        self.assertEqual("", lr._buffered_pipeline_hint("pytest"))


if __name__ == "__main__":
    unittest.main()
