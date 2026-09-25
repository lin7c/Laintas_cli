"""Paths that raised NameError, each hidden behind an ``except`` that turned it
into something else.

ruff's F821 (undefined name) found all of them. None of them failed a test,
because the code under test either was mocked out or never reached the broken
line. Each case here drives the real line.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import branch
import laintas_cli
import tools

from tests.extension_packages import bind_context, extension_package


class FakeTerm0:
    """A persistent shell that answers a marker-wrapped command."""

    def __init__(self, body: str):
        self.body = body
        self.raw_output = ""
        self.sent = []

    def send_keys(self, text: str):
        self.sent.append(text)
        start = text.split("echo ", 1)[1].split(";", 1)[0]
        end = text.rsplit("echo ", 1)[1].split(":", 1)[0]
        self.raw_output += f"{start}\r\n{self.body}\r\n{end}:0\r\n"

    def read_output(self, timeout=0.1):
        return ""

    def is_alive(self):
        return True


class ParentCommandRunsOnce(unittest.TestCase):
    def test_marker_poll_returns_the_commands_output(self):
        term = FakeTerm0("hello from term0")
        with mock.patch.object(agent_loop, "_sync_cwd_from_session"):
            out = agent_loop._marker_poll_simple(term, "echo hello", timeout=5)
        self.assertEqual("hello from term0", out)

    def test_a_read_failure_after_sending_does_not_run_it_again(self):
        term = FakeTerm0("x")
        info = mock.Mock(session=term)
        with mock.patch.object(agent_loop, "get_terminal", return_value=info), \
                mock.patch.object(agent_loop, "_marker_poll_collect",
                                  side_effect=RuntimeError("boom")), \
                mock.patch("subprocess.run") as run:
            out = agent_loop._execute_parent_command("touch /tmp/should-run-once")
        self.assertEqual(1, len(term.sent))
        run.assert_not_called()
        self.assertIn("boom", out)


class OfflineStartKeepsTheSignIn(unittest.TestCase):
    SESSION = {"token": "t", "cookies": {"__Secure-laintas-v2.session_token": "v"},
               "headers": {}}

    def _response(self, status, payload):
        resp = mock.Mock(status_code=status)
        resp.json.return_value = payload
        return resp

    def test_statuses(self):
        cases = [
            (laintas_cli.requests.ConnectionError("down"), "unreachable"),
            (self._response(502, {}), "unreachable"),
            (self._response(429, {}), "unreachable"),
            (self._response(401, {}), "invalid"),
            (self._response(200, None), "invalid"),
            (self._response(200, {"user": {"id": "u1"}}), "ok"),
        ]
        for outcome, expected in cases:
            kwargs = ({"side_effect": outcome} if isinstance(outcome, Exception)
                      else {"return_value": outcome})
            with self.subTest(expected=expected), \
                    mock.patch.object(laintas_cli.requests, "get", **kwargs):
                self.assertEqual(expected, laintas_cli.check_session(self.SESSION)[0])

    def test_ensure_auth_offline_does_not_delete_the_session(self):
        with mock.patch.object(laintas_cli, "load_session",
                               return_value=dict(self.SESSION)), \
                mock.patch.object(laintas_cli.requests, "get",
                                  side_effect=laintas_cli.requests.Timeout()), \
                mock.patch.object(laintas_cli, "clear_session") as clear:
            session = laintas_cli.ensure_auth()
        clear.assert_not_called()
        self.assertEqual("t", session["token"])

    def test_ensure_auth_on_a_real_rejection_still_signs_out(self):
        with mock.patch.object(laintas_cli, "load_session",
                               return_value=dict(self.SESSION)), \
                mock.patch.object(laintas_cli.requests, "get",
                                  return_value=self._response(401, {})), \
                mock.patch.object(laintas_cli, "clear_session") as clear:
            self.assertIsNone(laintas_cli.ensure_auth())
        clear.assert_called_once()


class FilePushReachesTheNetwork(unittest.TestCase):
    def test_upload_is_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_text("x")
            presign = mock.Mock(status_code=500, text="nope")
            presign.json.side_effect = ValueError
            with mock.patch("requests.post", return_value=presign) as post:
                result = tools._bi_file_push(
                    {"paths": [str(path)], "target_agent_id": "ag1"},
                    tools.ToolCtx(session={"cookies": {}, "headers": {}}))
        post.assert_called()
        self.assertNotIn("is not defined", json.dumps(result))


class SpawnParallelWithoutAnAnswer(unittest.TestCase):
    def setUp(self):
        agent_loop.close_all_agents()
        self.addCleanup(agent_loop.close_all_agents)
        self.addCleanup(branch._BRANCHES.clear)
        self.parent = agent_loop.register_agent(name="nf-parent", role="primary")

    def test_an_empty_final_reply_is_reported_not_raised(self):
        from rich.console import Console
        from rich.markdown import Markdown
        deps = agent_loop.LoopDeps(
            read_file=lambda p: None, append_file=lambda p, c: None,
            write_file=lambda p, c: None, strip_ansi=lambda t: t,
            generate_prompt=lambda: "x",
            call_backend=lambda **k: {"reply": "", "tool_calls": [],
                                      "done": True, "error": False},
            SubTerminalSession=mock.Mock,
            display_command_output=lambda *a, **k: None,
            display_sub_terminal_preview=lambda *a, **k: None,
            display_file_diff=lambda *a, **k: None,
            console=Console(file=io.StringIO(), force_terminal=False),
            Markdown=Markdown)
        ctx = tools.ToolCtx(deps=deps, agent_id=self.parent.id,
                            session={}, events_cb=None)
        with mock.patch("worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(agent_loop, "run_agent_loop",
                                  return_value={"state": {"lastReply": ""}}):
            result = tools._bi_spawn_parallel(
                {"tasks": [{"goal": "a"}], "wait": True}, ctx)
        self.assertIn("returned no final answer", json.dumps(result))


class CanvasStartsHelpwoWhenNothingRuns(unittest.TestCase):
    def test_launch_path_prints_the_url(self):
        main = extension_package("canvas")
        ctx = bind_context(main)
        fake_server = mock.Mock(is_running=mock.Mock(return_value=False))
        with mock.patch.dict(sys.modules, {"helpwo_server": fake_server}), \
                mock.patch.object(laintas_cli, "_hosts_helpwo_here", return_value=False), \
                mock.patch.object(laintas_cli, "_launch_app_subterminal",
                                  return_value={"open_url": "http://127.0.0.1:1/x"}):
            main._canvas_open("", mock.Mock(CANVAS_EXTENSION=".excalidraw"))
        self.assertNotIn("NameError", ctx.console.text)
        self.assertIn("http://127.0.0.1:1/x", ctx.console.text)


class ScriptRunIsOneModule(unittest.TestCase):
    def test_main_registers_itself_under_its_name(self):
        source = (Path(laintas_cli.__file__).read_text(encoding="utf-8"))
        head = source[:source.index("\ndef _run_pow_command")]
        self.assertIn('sys.modules.setdefault("laintas_cli", sys.modules["__main__"])',
                      head)


if __name__ == "__main__":
    unittest.main()
