"""Tests for the ACP (Agent Client Protocol) adapter.

Three layers, cheapest first:

1. Pure translation: laintas event dicts → ACP session/update models,
   tool-name → ToolKind mapping. No SDK transport, no agent loop.
2. Approval round trip: the LoopDeps callbacks against a fake ACP client
   connection — allow, deny, editor-gone fail-closed.
3. Live handshake over a real `laintas-cli --acp` subprocess: initialize,
   new_session, and the no-SDK error path. No model calls — the prompt
   path needs a backend and is covered by the unit layers instead.

The SDK is optional at runtime; every test skips cleanly without it.
"""

import asyncio
import importlib
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

try:
    import acp_agent  # noqa: F401
    from acp.schema import (AgentMessageChunk, AllowedOutcome, DeniedOutcome,
                            PermissionOption, RequestPermissionResponse,
                            TextContentBlock, ToolCallProgress,
                            ToolCallStart, ToolCallUpdate)
    _HAVE_SDK = True
except Exception:  # pragma: no cover - environment without the SDK
    _HAVE_SDK = False


@unittest.skipUnless(_HAVE_SDK, "agent-client-protocol not installed")
class ToolKindMappingTests(unittest.TestCase):
    def test_known_prefixes_map_to_acp_kinds(self):
        self.assertEqual(acp_agent._tool_kind("read"), "read")
        self.assertEqual(acp_agent._tool_kind("grep"), "search")
        self.assertEqual(acp_agent._tool_kind("edit"), "edit")
        self.assertEqual(acp_agent._tool_kind("shell"), "execute")
        self.assertEqual(acp_agent._tool_kind("web_fetch"), "fetch")

    def test_unknown_tools_are_other(self):
        self.assertEqual(acp_agent._tool_kind("totally_new_tool"), "other")
        self.assertEqual(acp_agent._tool_kind(""), "other")


@unittest.skipUnless(_HAVE_SDK, "agent-client-protocol not installed")
class EventTranslationTests(unittest.TestCase):
    """Event dicts from run_agent_loop's events_cb → ACP updates."""

    def _agent(self):
        agent = acp_agent.LaintasAcpAgent({}, depth=0)
        agent._loop = None  # _notify becomes a no-op; we capture directly
        agent._sent = []
        agent._notify = lambda sid, upd: agent._sent.append((sid, upd))
        agent._sessions["s1"] = acp_agent._AcpSession("s1", os.getcwd())
        return agent

    def test_ai_stream_becomes_agent_message_chunk(self):
        agent = self._agent()
        agent._forward_event(agent._sessions["s1"],
                             {"type": "ai_stream", "content": "hello"})
        sid, upd = agent._sent[-1]
        self.assertEqual(sid, "s1")
        self.assertIsInstance(upd, AgentMessageChunk)
        self.assertEqual(upd.content.text, "hello")
        self.assertTrue(agent._sessions["s1"].saw_ai_event)

    def test_tool_started_then_output_completes_the_call(self):
        agent = self._agent()
        s = agent._sessions["s1"]
        agent._forward_event(s, {"type": "tool_started",
                                 "toolCallId": "c1", "name": "read",
                                 "command": "main.py"})
        start = [u for _sid, u in agent._sent
                 if isinstance(u, ToolCallStart)][-1]
        self.assertEqual(start.tool_call_id, "c1")
        self.assertEqual(start.kind, "read")
        self.assertEqual(start.status, "in_progress")

        agent._forward_event(s, {"type": "system", "kind": "tool",
                                 "content": "read",
                                 "meta": {"ok": True, "call_id": "c1"}})
        agent._forward_event(s, {"type": "system", "kind": "output",
                                 "content": "file body"})
        progress = [u for _sid, u in agent._sent
                    if isinstance(u, ToolCallProgress)][-1]
        self.assertEqual(progress.tool_call_id, "c1")
        self.assertEqual(progress.status, "completed")
        self.assertEqual(progress.content[0].content.text, "file body")
        self.assertNotIn("c1", s.open_tool_calls)

    def test_failed_tool_marks_the_call_failed(self):
        agent = self._agent()
        s = agent._sessions["s1"]
        agent._forward_event(s, {"type": "tool_started",
                                 "toolCallId": "c2", "name": "shell",
                                 "command": "false"})
        agent._forward_event(s, {"type": "system", "kind": "tool",
                                 "content": "shell",
                                 "meta": {"ok": False, "call_id": "c2"}})
        agent._forward_event(s, {"type": "system", "kind": "output",
                                 "content": ""})
        progress = [u for _sid, u in agent._sent
                    if isinstance(u, ToolCallProgress)][-1]
        self.assertEqual(progress.status, "failed")

    def test_tool_output_without_started_opens_the_call(self):
        """Older loop paths never emitted tool_started; the output alone
        must still surface a completed tool call to the editor."""
        agent = self._agent()
        s = agent._sessions["s1"]
        agent._forward_event(s, {"type": "system", "kind": "tool",
                                 "content": "read",
                                 "meta": {"ok": True, "call_id": "c3"}})
        agent._forward_event(s, {"type": "system", "kind": "output",
                                 "content": "x"})
        self.assertTrue(any(isinstance(u, ToolCallStart)
                            for _sid, u in agent._sent))

    def test_unknown_events_are_dropped(self):
        agent = self._agent()
        before = len(agent._sent)
        agent._forward_event(agent._sessions["s1"], {"type": "billing"})
        agent._forward_event(agent._sessions["s1"], {"type": "ai_end"})
        self.assertEqual(len(agent._sent), before)


@unittest.skipUnless(_HAVE_SDK, "agent-client-protocol not installed")
class ApprovalRoundTripTests(unittest.TestCase):
    """The three LoopDeps approval callbacks against a fake client conn."""

    def _agent(self, outcome):
        agent = acp_agent.LaintasAcpAgent({}, depth=0)
        agent._sessions["s1"] = acp_agent._AcpSession("s1", os.getcwd())
        agent._sent = []
        agent._notify = lambda sid, upd: agent._sent.append((sid, upd))

        class FakeConn:
            async def request_permission(self, session_id, tool_call,
                                         options, **kwargs):
                self.seen_options = options
                self.seen_tool_call = tool_call
                if isinstance(outcome, Exception):
                    raise outcome
                return RequestPermissionResponse(outcome=outcome)

        fake = FakeConn()
        agent._conn = fake

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        try:
            agent._loop = loop
            thread.start()

            def run(callback):
                return callback("ls -la", "policy: needs approval")

            command_cb = agent._approval_cb(
                agent._sessions["s1"], kind="execute", allow_always=False)
            result = run(command_cb)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()
        return agent, fake, result

    def test_allowed_outcome_maps_to_true(self):
        _agent, fake, result = self._agent(
            AllowedOutcome(option_id="allow", outcome="selected"))
        self.assertTrue(result)
        self.assertEqual(
            [o.kind for o in fake.seen_options],
            ["allow_once", "reject_once"])

    def test_denied_outcome_maps_to_false(self):
        _agent, _fake, result = self._agent(DeniedOutcome(outcome="cancelled"))
        self.assertFalse(result)

    def test_editor_error_fails_closed(self):
        _agent, _fake, result = self._agent(RuntimeError("editor gone"))
        self.assertFalse(result)


@unittest.skipUnless(_HAVE_SDK, "agent-client-protocol not installed")
class StdoutGuardTests(unittest.TestCase):
    def test_official_credentials_cannot_change_the_selected_account(self):
        import io
        import types
        import laintas_cli as cli
        with mock.patch.object(cli, "get_backend_profile", return_value=types.SimpleNamespace(sends_laintas_credentials=True)), \
             mock.patch.object(cli, "load_session", return_value={"userId": "A", "cookies": {"session": "token"}}), \
             mock.patch.object(cli, "check_session", return_value=("ok", {"id": "B"})), \
             mock.patch.object(cli.paths, "ACCOUNT_USER_ID", "A"), \
             mock.patch.object(acp_agent, "LaintasAcpAgent") as agent, \
             mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(acp_agent.serve(), 1)
            agent.assert_not_called()
            self.assertIn("sign-in required", err.getvalue())

    def test_stray_writes_go_to_stderr(self):
        import io
        real = io.StringIO()
        err = io.StringIO()
        guard = acp_agent._StrayStdout(real)
        with mock.patch("sys.stderr", err):
            guard.write("noise\n")
            guard.flush()
            guard.buffer  # property must exist and not touch real stdout
        self.assertEqual(real.getvalue(), "")
        self.assertEqual(err.getvalue(), "noise\n")
        self.assertFalse(guard.isatty())


@unittest.skipUnless(_HAVE_SDK, "agent-client-protocol not installed")
class HandshakeProcessTests(unittest.TestCase):
    """End-to-end against a real `laintas-cli --acp` subprocess.

    initialize + new_session only — a prompt needs a model backend and
    belongs to integration runs, not the unit suite. What this proves:
    the process starts, speaks valid ACP JSON-RPC on stdio, and keeps
    the RPC stream clean of startup banner noise.
    """

    def test_official_without_credentials_exits_without_interactive_login(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, LAINTAS_HOME=home,
                       LAINTAS_ACCOUNT_ID="", LAINTAS_BACKEND="")
            result = subprocess.run(
                [sys.executable, os.path.join(REPO, "laintas_cli.py"), "--acp"],
                env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("sign-in required", result.stderr)

    def test_initialize_and_new_session(self):
        from acp import PROTOCOL_VERSION
        from acp.interfaces import Client
        from acp.stdio import spawn_agent_process

        class MinimalClient(Client):
            async def request_permission(self, *args, **kwargs):
                raise RuntimeError("not expected during handshake")

        async def drive():
            env = dict(os.environ)
            async with spawn_agent_process(
                MinimalClient(),
                sys.executable,
                os.path.join(REPO, "laintas_cli.py"),
                "--acp",
                "--backend", "http://127.0.0.1:1",
                env=env,
            ) as (conn, _process):
                init = await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=None)
                self.assertEqual(init.agent_info.name, "laintas-cli")
                session = await conn.new_session(
                    cwd=REPO, mcp_servers=[])
                self.assertTrue(session.session_id)
                return session

        session = asyncio.run(asyncio.wait_for(drive(), timeout=90))
        self.assertTrue(session.session_id)


if __name__ == "__main__":
    unittest.main()
