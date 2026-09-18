"""Manifest resources use real registry ownership and Station deployment."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop as runtime
import agent_persistence
import app_host
import laintas_cli


class TopologyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, attr, value in (
            (runtime, "_agent_registry", {}),
            (runtime, "_terminal_registry", {}),
            (agent_persistence, "AGENTS_DIR", Path(directory.name)),
        ):
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.root_shell = mock.Mock(command="bash", full_output="")
        self.root_shell.is_alive.return_value = True
        runtime.register_terminal(self.root_shell, "bash", 0, name="term0")
        self.primary = runtime.register_agent(name="primary", role="primary")
        self.primary.home_terminal = "term0"
        self.data = {
            "name": "demo", "auto_approve": True,
            "agent": {"tools": ["*"], "prompt": "Coordinate work."},
            "terminals": [{"name": "worker-shell", "cwd": "."}],
            "agents": [
                {"name": "worker", "parent": "primary", "terminal": "worker-shell",
                 "model": "selected-model", "provider": "selected-provider",
                 "prompt": "Run tests.", "capability_tags": ["testing"],
                 "tools": ["fs.read", "shell.exec"], "denied_tools": ["fs.write"]},
                {"name": "reviewer", "parent": "worker", "tools": ["fs.read"]},
            ],
        }
        self.shell = mock.Mock(command="bash", full_output="")
        self.shell.is_alive.return_value = True

    def manifest(self, data=None):
        manifest, error = app_host.parse_manifest(data or self.data, "/demo.json")
        self.assertEqual(error, "")
        return manifest

    def test_resources_have_real_profiles_parent_edges_and_deployment(self):
        rollback = app_host.configure_runtime(self.manifest(), runtime, lambda spec: self.shell)
        self.addCleanup(rollback)
        worker = runtime.get_agent("worker")
        self.assertEqual(worker.parent_id, "primary")
        self.assertIn("worker", self.primary.child_ids)
        self.assertIn("reviewer", worker.child_ids)
        self.assertEqual(runtime.get_agent("reviewer").parent_id, "worker")
        self.assertEqual(worker.profile.prompt, "Run tests.")
        self.assertEqual(worker.profile.capability_tags, ["testing"])
        self.assertEqual(worker.profile.tool_policy.allowed_tools, ["fs.read", "shell.exec"])
        self.assertEqual(worker.profile.tool_policy.denied_tools, ["fs.write"])
        self.assertEqual((worker.base_model, worker.base_provider), ("selected-model", "selected-provider"))
        self.assertEqual(runtime.agent_deployment_terminal(worker), "worker-shell")
        self.assertEqual(runtime.get_terminal("worker-shell").stationed_agent_id, "worker")
        self.assertIsNone(worker.active_assignment)
        self.assertIsNone(self.primary.profile.tool_policy.allowed_tools)

    def test_failed_deployment_rolls_back_agents_terminals_and_primary(self):
        self.primary.profile.prompt = "original"
        with mock.patch.object(runtime, "station_agent", return_value=False):
            with self.assertRaises(RuntimeError):
                app_host.configure_runtime(self.manifest(), runtime, lambda spec: self.shell)
        self.assertEqual([a.id for a in runtime.get_all_agents()], ["primary"])
        self.assertEqual([t.name for t in runtime.get_all_terminals()], ["term0"])
        self.assertEqual(self.primary.child_ids, [])
        self.assertEqual(self.primary.profile.prompt, "original")
        self.shell.close.assert_called_once()
        self.root_shell.close.assert_not_called()

    def test_existing_terminal_is_preserved_on_conflict(self):
        runtime.register_terminal(self.shell, "bash", 0, name="worker-shell")
        with self.assertRaises(ValueError):
            app_host.configure_runtime(self.manifest(), runtime, lambda spec: self.shell)
        self.shell.close.assert_not_called()
        self.assertIs(runtime.get_terminal("worker-shell").session, self.shell)

    def test_session_inherits_full_catalogue_or_explicit_tools(self):
        for tools in (["*"], ["fs.read", "agent.spawn", "terminal.create"]):
            with self.subTest(tools=tools):
                manifest = self.manifest({"name": "demo", "session_tools": tools})
                rollback = app_host.configure_runtime(manifest, runtime, lambda spec: self.shell, session=True)
                try:
                    allowed = self.primary.profile.tool_policy.allowed_tools
                    if tools == ["*"]:
                        self.assertIsNone(allowed)
                    else:
                        self.assertEqual(set(allowed), set(tools) | app_host.SESSION_BASE_TOOLS)
                finally:
                    rollback()

    def test_invalid_topology_fails_before_starting_resources(self):
        cases = [
            {"agents": [{"name": "a", "parent": "a"}]},
            {"agents": [{"name": "a", "parent": []}]},
            {"agents": [{"name": "a", "terminal": "missing"}]},
            {"agents": [{"name": "a"}, {"name": "a"}]},
            {"agent": {"tools": ["made.up"]}},
            {"agent": {"profile": "made-up-role"}},
            {"terminals": [{"name": "term0"}]},
            {"terminals": [{"name": "x", "cwd": 4}]},
            {"terminals": [{"name": "x"}], "agents": [
                {"name": "a", "terminal": "x"}, {"name": "b", "terminal": "x"}]},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                manifest, error = app_host.parse_manifest({"name": "demo", **patch}, "/demo.json")
                self.assertIsNone(manifest)
                self.assertTrue(error)

    def test_session_launch_applies_topology_and_interactive_approval(self):
        registry = mock.Mock()
        data = dict(self.data, auto_approve=False)
        with mock.patch.object(laintas_cli, "_app_terminal_factory", return_value=self.shell), \
                mock.patch("helpwo_server.start_server", return_value=(True, "ready")), \
                mock.patch("helpwo_server.get_url", return_value="http://127.0.0.1:1234"), \
                mock.patch.object(app_host, "_active", None):
            result = laintas_cli._app_session_start_in_process("demo", {
                "session_user": "alice", "manifest": data, "idle_minutes": 0,
            }, registry, {}, {"token": "tok", "local_agent_id": "session-agent"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(registry.app_mode, {"approval": "ask"})
        self.assertEqual(runtime.agent_deployment_terminal(runtime.get_agent("worker")), "worker-shell")
        runtime.unregister_agent("worker")
        runtime.unregister_terminal("worker-shell")
        self.shell.close.assert_called_once()

    def test_bridge_start_failure_rolls_back_session_resources(self):
        with mock.patch.object(laintas_cli, "_app_terminal_factory", return_value=self.shell), \
                mock.patch("helpwo_server.start_server", return_value=(False, "port busy")), \
                mock.patch.object(app_host, "_active", None):
            result = laintas_cli._app_session_start_in_process("demo", {
                "session_user": "alice", "manifest": self.data, "idle_minutes": 0,
            }, mock.Mock(), {}, {})
        self.assertEqual(result, {"status": "error", "message": "port busy"})
        self.assertIsNone(runtime.get_agent("worker"))
        self.assertIsNone(runtime.get_terminal("worker-shell"))
        self.shell.close.assert_called_once()

    def test_terminal_cwd_is_shell_quoted_and_start_failure_closes_it(self):
        with mock.patch.object(laintas_cli, "SubTerminalSession") as terminal:
            terminal.return_value.start.side_effect = RuntimeError("cannot start")
            with self.assertRaises(RuntimeError):
                laintas_cli._app_terminal_factory({"name": "x", "cwd": "/tmp/path with spaces", "command": "bash"})
        terminal.assert_called_once_with("cd '/tmp/path with spaces' && bash", timeout=0, use_tmux=False)
        terminal.return_value.close.assert_called_once()
