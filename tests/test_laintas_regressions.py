#!/usr/bin/env python3
"""Regression tests for agent-loop/tool behavior found in debug logs."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_loop
import tools


class _DummyConsole:
    def print(self, *args, **kwargs):
        pass

    def status(self, *args, **kwargs):
        from contextlib import nullcontext
        return nullcontext()


class _DummyMarkdown:
    def __init__(self, text):
        self.text = text


class _DummySession:
    pass


def _deps(response):
    return agent_loop.LoopDeps(
        read_file=lambda path: None,
        append_file=lambda path, content: None,
        write_file=lambda path, content: None,
        strip_ansi=lambda text: text,
        generate_prompt=lambda: "Test prompt",
        call_backend=lambda **kwargs: response,
        SubTerminalSession=_DummySession,
        display_command_output=lambda *args, **kwargs: None,
        display_sub_terminal_preview=lambda *args, **kwargs: None,
        display_file_diff=lambda *args, **kwargs: None,
        console=_DummyConsole(),
        Markdown=_DummyMarkdown,
    )


class LaintasRegressionTests(unittest.TestCase):
    def test_task_complete_updates_debug_done(self):
        agent_loop.clear_debug_logs()
        previous = agent_loop.get_runtime_config("max_loops")
        agent_loop.set_runtime_config("max_loops", 1)
        try:
            result = agent_loop.run_agent_loop(
                _deps({
                    "reply": "Done.",
                    "tool_calls": [
                        {"name": "task.complete", "arguments": {"summary": "finished"}}
                    ],
                    "done": False,
                    "error": False,
                }),
                "finish the task",
                session={},
                state={},
                chat_history=[],
            )
        finally:
            agent_loop.set_runtime_config("max_loops", previous)

        logs = agent_loop.get_debug_logs()
        self.assertIs(result["success"], True)
        self.assertTrue(logs, "expected a debug entry")
        self.assertIs(logs[0].done, True)

    def test_fs_diff_path_alias_returns_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp, check=True)
            file_path = os.path.join(tmp, "sample.txt")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("before\n")
            subprocess.run(["git", "add", "sample.txt"], cwd=tmp, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp, check=True, capture_output=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("after\n")

            ctx = tools.ToolCtx(cwd=tmp)
            result = tools.get_registry().invoke("fs.diff", {"path": "sample.txt"}, ctx)

        self.assertIs(result["ok"], True)
        self.assertIs(result["changed"], True)
        self.assertIn("-before", result["result"])
        self.assertIn("+after", result["result"])


if __name__ == "__main__":
    unittest.main()
