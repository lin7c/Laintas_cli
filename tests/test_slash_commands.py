import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

import agent_loop
import laintas_cli
import plan_mode
import prompt_opt
import task_manager
import workflow_engine


class _Registry:
    agent_id = None

    def unregister(self):
        pass

    def register(self, *args, **kwargs):
        pass

    def start_heartbeat(self):
        pass


class SlashRegistryTests(unittest.TestCase):
    def test_registry_is_unique_and_drives_palette_and_completion(self):
        names = [name for spec in laintas_cli.COMMAND_SPECS for name in spec.all_names]
        palette = [name for name, _ in laintas_cli._COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(palette), len(set(palette)))
        self.assertEqual(set(names), set(laintas_cli.MetaCompleter.META_COMMANDS))
        self.assertIn("/resume", names)

    def test_raw_parser_preserves_quotes_json_and_spacing(self):
        _, raw, parts = laintas_cli._parse_slash_command(
            "/bash printf '%s\\n' 'a  b'")
        self.assertEqual(raw, "printf '%s\\n' 'a  b'")
        self.assertEqual(parts[-1], "a  b")
        _, raw, _ = laintas_cli._parse_slash_command(
            '/tool x {"text":"a  b"}')
        self.assertEqual(raw, 'x {"text":"a  b"}')

    def test_redaction_and_prop_validation(self):
        redacted = laintas_cli._redact_sensitive_text(
            "Authorization: Bearer abc123 password=hunter2")
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("hunter2", redacted)
        _, errors, _ = laintas_cli._validate_prop_template(
            "<role>{{bad-name}}</role>")
        self.assertTrue(errors)

    def test_resume_dispatcher_is_registered_and_not_unknown(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            laintas_cli.handle_meta_command("/resume", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        text = output.getvalue()
        self.assertIn("main REPL", text)
        self.assertNotIn("Unknown command", text)

    def test_dispatcher_contains_unexpected_errors(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(
                    laintas_cli, "_handle_meta_command_impl",
                    side_effect=RuntimeError("boom")):
                should_exit = laintas_cli.handle_meta_command(
                    "/prop", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertFalse(should_exit)
        self.assertIn("RuntimeError: boom", output.getvalue())

    def test_dangerous_commands_reject_extra_args(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(laintas_cli, "reload_default_files") as reload_mock, \
                    mock.patch.object(laintas_cli, "close_all_terminals") as close_mock, \
                    mock.patch.object(laintas_cli, "clear_session") as clear_mock:
                self.assertFalse(laintas_cli.handle_meta_command("/reload now", _Registry(), {}))
                self.assertFalse(laintas_cli.handle_meta_command("/exit now", _Registry(), {}))
                self.assertFalse(laintas_cli.handle_meta_command("/quit now", _Registry(), {}))
        finally:
            laintas_cli.console = old_console
        reload_mock.assert_not_called()
        close_mock.assert_not_called()
        clear_mock.assert_not_called()
        text = output.getvalue()
        self.assertIn("Usage: /reload", text)
        self.assertIn("Usage: /exit", text)
        self.assertIn("Usage: /quit", text)

    def test_update_check_does_not_apply_update(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(laintas_cli, "handle_version_command") as version_mock:
                laintas_cli.handle_meta_command("/update check", _Registry(), {})
                laintas_cli.handle_meta_command("/update --force", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertEqual(version_mock.call_args_list, [
            mock.call(["/v", "check"]),
            mock.call(["/v", "update", "--force"]),
        ])
        self.assertNotIn("Usage: /update", output.getvalue())

    def test_term_rejects_extra_args(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(laintas_cli, "SubTerminalSession") as sub_mock:
                laintas_cli.handle_meta_command("/term worker extra", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        sub_mock.assert_not_called()
        self.assertIn("Usage: /term", output.getvalue())

    def test_json_args_preserve_quotes(self):
        sent = {}
        invoked = {}

        def capture_send(target_id, body):
            sent["target_id"] = target_id
            sent["body"] = body
            return True

        def capture_invoke(name, params, ctx):
            invoked["name"] = name
            invoked["params"] = params
            return {"ok": True}

        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(laintas_cli, "send_to_agent", side_effect=capture_send), \
                    mock.patch.object(laintas_cli.tools_mod.get_registry(), "invoke", side_effect=capture_invoke):
                laintas_cli.handle_meta_command('/tell agent1 {"kind":"note","text":"a  b"}', _Registry(), {})
                laintas_cli.handle_meta_command('/tool sample {"text":"a  b"}', _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertEqual(sent["target_id"], "agent1")
        self.assertEqual(sent["body"]["text"], "a  b")
        self.assertEqual(invoked["name"], "sample")
        self.assertEqual(invoked["params"], {"text": "a  b"})

    def test_back_is_subterminal_only(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(laintas_cli.sys.stdout, "write") as write_mock:
                laintas_cli.handle_meta_command("/back", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        write_mock.assert_not_called()
        self.assertIn("only detaches", output.getvalue())

    def test_prompt_fail_json_preserves_quotes(self):
        captured = {}

        def capture_failure(fields):
            captured["fields"] = fields
            return {"id": "f1"}

        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(prompt_opt, "capture_structured_failure", side_effect=capture_failure), \
                    mock.patch.object(prompt_opt, "spawn_optimizer", return_value=None), \
                    mock.patch.object(laintas_cli, "get_current_agent", return_value=None):
                laintas_cli.handle_meta_command('/prompt fail {"task":"a  b","actual":"c  d"}', _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertEqual(captured["fields"], {"task": "a  b", "actual": "c  d"})

    def test_case_insensitive_skill_and_mcp_subcommands(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            with mock.patch.object(laintas_cli.skills_mod, "SKILLS_DIR", "/tmp/skills"), \
                    mock.patch.object(laintas_cli.skills_mod, "get_all_metadata", return_value={}), \
                    mock.patch.object(laintas_cli, "_get_mcp_mod") as mcp_mod:
                mcp_mod.return_value.MCP_AVAILABLE = False
                mcp_mod.return_value.MCP_IMPORT_ERROR = "missing"
                laintas_cli.handle_meta_command("/skill LIST", _Registry(), {})
                laintas_cli.handle_meta_command("/mcp LIST", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        text = output.getvalue()
        self.assertIn("No skills", text)
        self.assertIn("mcp SDK not installed", text)

    def test_bash_receives_exact_raw_command(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        session = mock.Mock()
        session.is_alive.return_value = True
        terminal = mock.Mock(session=session)
        captured = {}

        def execute(_session, command, **kwargs):
            captured["command"] = command
            return {"stdout": "", "returncode": 0}

        try:
            with mock.patch.object(laintas_cli, "get_terminal", return_value=terminal), \
                    mock.patch.object(laintas_cli, "_ensure_term0_alive"), \
                    mock.patch.object(laintas_cli, "_sync_cwd_from_term0"), \
                    mock.patch.object(laintas_cli, "_marker_poll_exec", side_effect=execute):
                laintas_cli.handle_meta_command(
                    "/bash printf '%s\\n' 'a  b'", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertEqual(captured["command"], "printf '%s\\n' 'a  b'")

    def test_send_displays_only_new_terminal_output(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)

        class Session:
            full_output = "STALE OUTPUT\n"

            def is_alive(self):
                return True

            def send_keys(self, value):
                self.sent = value

            def read_output(self, timeout=0):
                if "NEW OUTPUT" not in self.full_output:
                    self.full_output += "NEW OUTPUT\n"

        session = Session()
        try:
            with mock.patch.object(
                    laintas_cli, "get_terminal",
                    return_value=mock.Mock(session=session)):
                laintas_cli.handle_meta_command(
                    "/send term1 --wait 0.01 echo hi", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertIn("NEW OUTPUT", output.getvalue())
        self.assertNotIn("STALE OUTPUT", output.getvalue())


class ConfigAndMemoryTests(unittest.TestCase):
    def tearDown(self):
        agent_loop.reset_runtime_config()

    def test_runtime_config_is_typed_and_rejects_bad_values(self):
        self.assertTrue(agent_loop.set_runtime_config(
            "allow_remote_exec_without_approval", "false"))
        self.assertIs(agent_loop.get_runtime_config(
            "allow_remote_exec_without_approval"), False)
        with self.assertRaises(ValueError):
            agent_loop.set_runtime_config("max_loops", "not-a-number")
        self.assertFalse(agent_loop.set_runtime_config("missing", "1"))

    def test_malformed_project_memory_returns_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            Path(".laintas").mkdir()
            Path(".laintas/memory.json").write_text(
                '["bad entry"]', encoding="utf-8")
            entries, errors, _ = laintas_cli._load_project_memory_entries()
        self.assertEqual(entries, [])
        self.assertTrue(errors)


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        task_manager.clear_session_tasks()

    def test_subtask_is_saved_in_same_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = task_manager.create_task("parent", cwd=tmp)
            ok, _, updated = task_manager.update_task(
                parent["id"], cwd=tmp, addSubtask="child")
            tasks = task_manager.list_tasks(cwd=tmp, include_session=False)
        self.assertTrue(ok)
        self.assertEqual([task["id"] for task in tasks], ["1", "2"])
        self.assertEqual(updated["blocks"], ["2"])

    def test_blocked_task_cannot_start_and_ids_sort_numerically(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = task_manager.create_task("blocker", cwd=tmp)
            blocked = task_manager.create_task("blocked", cwd=tmp)
            task_manager.update_task(
                blocked["id"], cwd=tmp, addBlockedBy=[blocker["id"]])
            ok, message, _ = task_manager.update_task(
                blocked["id"], cwd=tmp, status="in_progress")
            for index in range(8):
                task_manager.create_task(str(index), cwd=tmp)
            ids = [task["id"] for task in task_manager.list_tasks(
                cwd=tmp, include_session=False)]
        self.assertFalse(ok)
        self.assertIn("blocked by", message)
        self.assertEqual(ids, [str(index) for index in range(1, 11)])


class PromptOptimizationTests(unittest.TestCase):
    def test_ids_import_apply_and_skill_containment(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            root = Path(tmp)
            prompts = root / "prompts"
            candidates = prompts / "candidates"
            skills = root / "skills"
            (root / ".laintas").mkdir()
            (root / ".laintas/cli.prop").write_text("BASE\n", encoding="utf-8")
            pack = root / "pack.md"
            pack.write_text(
                "---\nkind: laintas-prompt-pack\nversion: 1\nname: test\n---\n"
                "<prompt_opt_patch>\nPATCH\n</prompt_opt_patch>\n",
                encoding="utf-8",
            )
            with mock.patch.multiple(
                    prompt_opt,
                    CANDIDATES_DIR=candidates,
                    FEEDBACK_LOG=prompts / "feedback.jsonl",
                    STATE_PATH=prompts / "_state.json"), \
                    mock.patch.object(prompt_opt.paths, "PROMPTS_DIR", prompts), \
                    mock.patch.object(prompt_opt.paths, "SKILLS_DIR", skills):
                prompt_opt._current_opt = None
                prompt_opt._optimizations = {}
                first = prompt_opt.draft_candidate("f1", "one", "r")
                second = prompt_opt.draft_candidate("f2", "two", "r")
                self.assertNotEqual(first["id"], second["id"])
                ok, _, candidate_id = prompt_opt.install_pack(str(pack))
                self.assertTrue(ok)
                self.assertTrue(prompt_opt.apply_candidate(candidate_id)[0])
                self.assertTrue(prompt_opt.discard_candidate(candidate_id)[0])
                self.assertEqual(
                    (root / ".laintas/cli.prop").read_text(encoding="utf-8"),
                    "BASE\n",
                )
                self.assertIsNone(prompt_opt._resolve_skill_file(
                    "missing", "/etc/hosts"))
                self.assertIsNone(prompt_opt._resolve_skill_file(
                    "missing", "../../outside"))
                limitation = prompt_opt.draft_candidate(
                    "f3", "", "model limitation")
                self.assertEqual(limitation["type"], "model_limitation")


class PlanAndWorkflowTests(unittest.TestCase):
    def test_plan_state_restores_for_same_project(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            root = Path(tmp)
            with mock.patch.object(plan_mode, "PLANS_DIR", root / "plans"), \
                    mock.patch.object(plan_mode, "_STATE_PATH", root / "plans/_state.json"):
                plan_mode._current_plan = None
                plan_mode._plan_mode = False
                plan_mode.enter_plan_mode("persist")
                plan_mode._current_plan = None
                plan_mode._plan_mode = False
                plan_mode._restore_state()
                self.assertTrue(plan_mode.is_plan_mode())
                self.assertIsNotNone(plan_mode.get_current_plan())

    def test_plan_active_state_is_scoped_by_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_a = root / "a"
            project_b = root / "b"
            project_a.mkdir()
            project_b.mkdir()
            state_path = root / "plans/_state.json"
            with mock.patch.object(plan_mode, "PLANS_DIR", root / "plans"), \
                    mock.patch.object(plan_mode, "_STATE_PATH", state_path):
                with _chdir(project_a):
                    plan_mode._loaded_cwd = None
                    plan_mode.enter_plan_mode("project a")
                    self.assertTrue(plan_mode.is_plan_mode())
                with _chdir(project_b):
                    self.assertFalse(plan_mode.is_plan_mode())
                    plan_mode.enter_plan_mode("project b")
                    self.assertEqual(
                        plan_mode.get_current_plan()["task"], "project b")
                with _chdir(project_a):
                    self.assertTrue(plan_mode.is_plan_mode())
                    self.assertEqual(
                        plan_mode.get_current_plan()["task"], "project a")

    def test_workflow_persists_and_confirmation_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            workflow_engine._active_workflow = None
            workflow_engine._active_workflow_cwd = None
            workflow_engine.start_workflow("feature-dev", "test")
            workflow_engine.advance_phase("discover", force=True)
            workflow_engine.advance_phase("explore", force=True)
            with self.assertRaises(workflow_engine.WorkflowTransitionError):
                workflow_engine.advance_phase("bypass", force=True)
            workflow_engine._active_workflow = None
            workflow_engine._active_workflow_cwd = None
            restored = workflow_engine.get_active_workflow()
            self.assertEqual(restored.current.name, "clarify")
            self.assertEqual(
                workflow_engine.advance_phase(
                    "approved", user_confirmed=True).name,
                "architect",
            )


class ResumeStateTests(unittest.TestCase):
    """Regression tests for /resume state management."""

    def test_resume_choices_returns_both_checkpoints_and_autosaves(self):
        """_resume_choices must not hide newer autosaves when checkpoints exist."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cwd = "/fake/project"
            key = agent_loop._session_key(cwd)
            checkpoint_blob = {
                "id": "chk123abc", "session_id": "sid001",
                "kind": "checkpoint", "cwd": cwd,
                "timestamp": time.time() - 3600,
                "title": "Old checkpoint",
                "turn_count": 3,
                "chat_history": [{"role": "user", "content": "hi"}],
                "state": {},
            }
            (tmp_path / f"{key}_resume_chk123abc.json").write_text(
                json.dumps(checkpoint_blob), encoding="utf-8")
            autosave_blob = {
                "id": "sid001", "session_id": "sid001",
                "kind": "autosave", "cwd": cwd,
                "timestamp": time.time(),
                "title": "Latest autosave",
                "turn_count": 5,
                "chat_history": [{"role": "user", "content": "hi"}],
                "state": {},
            }
            (tmp_path / f"{key}_session_sid001.json").write_text(
                json.dumps(autosave_blob), encoding="utf-8")
            with mock.patch.object(agent_loop.paths, "SESSIONS_DIR", tmp_path):
                choices = laintas_cli._resume_choices(cwd)
            self.assertEqual(len(choices), 2)
            self.assertEqual(choices[0]["kind"], "autosave")
            self.assertEqual(choices[1]["kind"], "checkpoint")

    def test_delete_resume_state_removes_all_related_files(self):
        """Deleting a checkpoint must remove checkpoint + session + latest files."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cwd = "/fake/project"
            key = agent_loop._session_key(cwd)
            blob_id = "chk999"
            session_id = "sid999"
            blob = {
                "id": blob_id, "session_id": session_id,
                "kind": "checkpoint", "cwd": cwd,
                "timestamp": time.time(),
                "title": "Test checkpoint",
                "turn_count": 1,
                "chat_history": [{"role": "user", "content": "hi"}],
                "state": {},
            }
            (tmp_path / f"{key}_resume_{blob_id}.json").write_text(
                json.dumps(blob), encoding="utf-8")
            (tmp_path / f"{key}_session_{session_id}.json").write_text(
                json.dumps(blob), encoding="utf-8")
            (tmp_path / f"{key}_resume.json").write_text(
                json.dumps(blob), encoding="utf-8")
            with mock.patch.object(agent_loop.paths, "SESSIONS_DIR", tmp_path):
                agent_loop.delete_resume_state(cwd, blob)
                self.assertFalse((tmp_path / f"{key}_resume_{blob_id}.json").exists())
                self.assertFalse((tmp_path / f"{key}_session_{session_id}.json").exists())
                self.assertFalse((tmp_path / f"{key}_resume.json").exists())
                states = agent_loop.list_resume_states(cwd)
                self.assertEqual(len(states), 0)

    def test_delete_checkpoint_preserves_newer_autosave(self):
        """Deleting an old checkpoint must not destroy a newer autosave."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cwd = "/fake/project"
            key = agent_loop._session_key(cwd)
            session_id = "sid555"
            checkpoint_blob = {
                "id": "chk_old", "session_id": session_id,
                "kind": "checkpoint", "cwd": cwd,
                "timestamp": time.time() - 3600,
                "title": "Old checkpoint",
                "turn_count": 2,
                "chat_history": [{"role": "user", "content": "hi"}],
                "state": {},
            }
            autosave_blob = {
                "id": session_id, "session_id": session_id,
                "kind": "autosave", "cwd": cwd,
                "timestamp": time.time(),
                "title": "Newer autosave",
                "turn_count": 5,
                "chat_history": [{"role": "user", "content": "hi"}],
                "state": {},
            }
            (tmp_path / f"{key}_resume_chk_old.json").write_text(
                json.dumps(checkpoint_blob), encoding="utf-8")
            (tmp_path / f"{key}_session_{session_id}.json").write_text(
                json.dumps(autosave_blob), encoding="utf-8")
            (tmp_path / f"{key}_resume.json").write_text(
                json.dumps(autosave_blob), encoding="utf-8")
            with mock.patch.object(agent_loop.paths, "SESSIONS_DIR", tmp_path):
                agent_loop.delete_resume_state(cwd, checkpoint_blob)
                self.assertFalse((tmp_path / f"{key}_resume_chk_old.json").exists())
                self.assertTrue((tmp_path / f"{key}_session_{session_id}.json").exists())
                self.assertTrue((tmp_path / f"{key}_resume.json").exists())
                states = agent_loop.list_resume_states(cwd)
                self.assertEqual(len(states), 1)
                self.assertEqual(states[0]["kind"], "autosave")


class ResumeTranscriptTests(unittest.TestCase):
    """Regression tests for /resume conversation echo (_print_resume_transcript)."""

    def _blob(self, n_messages):
        history = []
        for i in range(n_messages):
            role = "user" if i % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"message {i}"})
        return {
            "chat_history": history,
            "older_summary": "earlier goals digest",
            "timestamp": time.time(),
            "turn_count": n_messages,
            "title": "Test session",
        }

    def _render(self, blob, limit):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False, width=200)
        history_before = list(blob.get("chat_history") or [])
        try:
            laintas_cli._print_resume_transcript(blob, limit)
        finally:
            laintas_cli.console = old_console
        self.assertEqual(blob.get("chat_history"), history_before)
        return output.getvalue()

    def test_default_limit_shows_last_20(self):
        text = self._render(self._blob(30), 20)
        self.assertIn("20/30 message(s)", text)
        self.assertIn("most recent", text)
        self.assertIn("message 29", text)
        self.assertNotIn("message 9", text)

    def test_all_shows_every_message(self):
        text = self._render(self._blob(30), None)
        self.assertIn("30/30 message(s)", text)
        self.assertIn("(all)", text)
        self.assertIn("message 0", text)
        self.assertIn("message 29", text)
        self.assertIn("earlier goals digest", text)

    def test_custom_n_shows_last_n(self):
        text = self._render(self._blob(30), 5)
        self.assertIn("5/30 message(s)", text)
        self.assertIn("message 29", text)
        self.assertNotIn("message 24", text)
        self.assertIn("message 25", text)

    def test_zero_prints_nothing(self):
        text = self._render(self._blob(10), 0)
        self.assertEqual(text.strip(), "")

    def test_older_summary_only_when_window_reaches_start(self):
        text = self._render(self._blob(30), 5)
        self.assertNotIn("earlier goals digest", text)


class _chdir:
    def __init__(self, path):
        self.path = path
        self.old = None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, exc_type, exc, tb):
        os.chdir(self.old)


if __name__ == "__main__":
    unittest.main()
