"""The live display and the /resume replay must be the same code.

These tests pin the property that made them drift apart: a turn is rendered
from the transcript event, and the event carries everything the row needs, so
replaying a saved session reproduces the live rows exactly.
"""

import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import agent_persistence
import laintas_cli
import transcript_view


@contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _console():
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def _tool_rows(text):
    """The tool rows of a rendering, stripped of padding."""
    return [line.rstrip() for line in text.splitlines()
            if transcript_view.symbols.DOT in line]


class LiveReplayParityTests(unittest.TestCase):

    def _run_turn(self, tool_calls):
        """Run one loop turn that makes `tool_calls`, return (live text, history)."""
        live_console = _console()
        deps = agent_loop.LoopDeps(
            read_file=lambda path: None,
            append_file=lambda path, content: None,
            write_file=lambda path, content: None,
            strip_ansi=lambda text: text,
            generate_prompt=lambda: "You are a test agent.",
            call_backend=lambda **kwargs: {
                "reply": "working", "finish_reason": "tool_calls",
                "tool_calls": tool_calls, "done": False, "error": False},
            SubTerminalSession=mock.Mock,
            display_command_output=lambda *a, **k: None,
            display_sub_terminal_preview=lambda *a, **k: None,
            display_file_diff=lambda *a, **k: None,
            console=live_console,
            Markdown=Markdown,
        )
        history = []
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(agent_persistence, "AGENTS_DIR",
                                  Path(tmp) / "agents"):
            Path(".laintas").mkdir()
            Path("a.txt").write_text("hello\nworld\n", encoding="utf-8")
            Path("b.txt").write_text("second\n", encoding="utf-8")
            agent_loop.run_agent_loop(
                deps, "read the files", {}, {}, history,
                events_cb=lambda events: None, max_loops_override=1)
        return live_console.file.getvalue(), history

    def _replay(self, history):
        replay_console = _console()
        old = laintas_cli.console
        laintas_cli.console = replay_console
        try:
            laintas_cli._print_resume_events(history)
        finally:
            laintas_cli.console = old
        return replay_console.file.getvalue()

    def test_replayed_rows_are_identical_to_the_live_rows(self):
        live, history = self._run_turn([
            {"name": "fs.read", "arguments": {"path": "a.txt"}},
            {"name": "fs.read", "arguments": {"path": "b.txt"}},
            {"name": "fs.read", "arguments": {"path": "missing.txt"}},
        ])
        tool_events = [m for m in history if m.get("role") == "tool"]
        self.assertEqual(len(tool_events), 3)

        live_rows = _tool_rows(live)
        replay_rows = _tool_rows(self._replay(history))

        self.assertTrue(live_rows, f"no tool rows rendered live: {live!r}")
        self.assertEqual(live_rows, replay_rows)
        # The two successful reads folded into one grouped row, the failure
        # kept its own — the live shape, reproduced from the saved events.
        self.assertEqual(len(live_rows), 2)
        self.assertIn("2 sources", live_rows[0])

    def test_events_carry_what_the_row_needs(self):
        _live, history = self._run_turn(
            [{"name": "fs.read", "arguments": {"path": "a.txt"}}])
        event = [m for m in history if m.get("role") == "tool"][0]
        for key in ("tool_name", "display_name", "summary", "ok",
                    "returncode", "elapsed"):
            self.assertIn(key, event)


class ToolMetaTests(unittest.TestCase):
    """The status tail is derived from the event alone — never from a result."""

    def test_extra_facts_are_persisted_for_replay(self):
        event = transcript_view.build_tool_event(
            "web.search", "Search web", "resume style", "...",
            {"ok": True, "count": 3})
        self.assertEqual(event["extra"]["count"], 3)
        self.assertEqual(transcript_view.tool_meta(event), "3 results")

    def test_failed_shell_keeps_exit_code_cause_and_why(self):
        event = transcript_view.build_tool_event(
            "shell.exec", "Bash", "pytest -q", "boom",
            {"ok": False, "error": "no such file"}, elapsed=2.5, returncode=1)
        meta = transcript_view.tool_meta(event)
        self.assertIn("exit 1", meta)
        self.assertIn("no such file", meta)
        self.assertIn("/why", meta)
        self.assertIn("2.5s", meta)

    def test_old_events_without_extra_still_render(self):
        legacy = {"role": "tool", "tool_name": "shell.exec",
                  "display_name": "Bash", "summary": "ls", "content": "a\nb",
                  "ok": True, "returncode": 0}
        self.assertEqual(transcript_view.tool_meta(legacy),
                         f"2L {transcript_view.symbols.BULLET} exit 0")
        self.assertIn("Bash", transcript_view.tool_row(legacy, 100))

    def test_silent_tools_stay_silent_in_replay(self):
        console = _console()
        view = transcript_view.TranscriptRenderer(console)
        view.event(transcript_view.build_tool_event(
            "task.create", "task.create", "plan", "", {"ok": True}))
        view.flush()
        self.assertEqual(console.file.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main()
