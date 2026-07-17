import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console
from rich.theme import Theme

import agent_loop
import backend_profiles
import hwo_ui
import laintas_cli
import mode_manager
import plan_mode
import policy
import prompt_opt
import task_manager
import terminal_preferences
import workflow_engine
import updater


class _Registry:
    agent_id = None

    def unregister(self):
        pass

    def register(self, *args, **kwargs):
        pass

    def start_heartbeat(self):
        pass


class SlashRegistryTests(unittest.TestCase):
    def setUp(self):
        self._terminal_preferences_dir = tempfile.TemporaryDirectory()
        self._sessions_patch = mock.patch.object(
            laintas_cli.paths, "SESSIONS_DIR",
            Path(self._terminal_preferences_dir.name))
        self._terminal_patch = mock.patch.object(
            laintas_cli.paths, "TERMINAL_ID", "slash-tests")
        self._sessions_patch.start()
        self._terminal_patch.start()
        terminal_preferences.reset_cache()

    def tearDown(self):
        terminal_preferences.reset_cache()
        self._terminal_patch.stop()
        self._sessions_patch.stop()
        self._terminal_preferences_dir.cleanup()

    @staticmethod
    def _complete(text):
        return list(laintas_cli.MetaCompleter().get_completions(
            laintas_cli.Document(text, len(text)),
            mock.Mock(completion_requested=True),
        ))

    def test_registry_is_unique_and_drives_palette_and_completion(self):
        names = [name for spec in laintas_cli.COMMAND_SPECS for name in spec.all_names]
        palette = [name for name, _ in laintas_cli._COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(palette), len(set(palette)))
        self.assertEqual(set(names), set(laintas_cli.MetaCompleter.META_COMMANDS))
        self.assertIn("/resume", names)
        self.assertIn("/compact", names)
        self.assertIn("/clear", names)
        self.assertIn("/clear", laintas_cli._NEW_SESSION_COMMANDS)
        palette_descriptions = dict(laintas_cli._COMMANDS)
        self.assertIn("/station <agent-id>", palette_descriptions["/station"])

    def test_exact_slash_command_keeps_a_visible_completion(self):
        completions = self._complete("/task")

        self.assertEqual([item.display_text for item in completions], ["/task"])
        # A trailing-space insertion avoids prompt_toolkit dropping its sole
        # exact/no-op completion while preserving the displayed command.
        self.assertEqual(completions[0].text, "/task ")
        self.assertEqual(completions[0].display_meta_text, "Track project tasks")

    def test_backspace_from_exact_command_restarts_prefix_completion(self):
        buffer = laintas_cli.Buffer()
        buffer.text = "/task"
        buffer.cursor_position = len(buffer.text)
        buffer.start_completion = mock.Mock()
        backspace = next(
            binding for binding in laintas_cli._build_keybindings().bindings
            if binding.keys == (laintas_cli.Keys.ControlH,)
        )

        backspace.handler(mock.Mock(current_buffer=buffer, arg=1))

        self.assertEqual(buffer.text, "/tas")
        buffer.start_completion.assert_called_once_with()
        self.assertIn(
            "/task", [item.display_text for item in self._complete(buffer.text)])

    def test_forward_delete_restarts_slash_completion(self):
        buffer = laintas_cli.Buffer()
        buffer.text = "/taskx"
        buffer.cursor_position = len("/task")
        buffer.start_completion = mock.Mock()
        delete = next(
            binding for binding in laintas_cli._build_keybindings().bindings
            if binding.keys == (laintas_cli.Keys.Delete,)
        )

        delete.handler(mock.Mock(current_buffer=buffer, arg=1))

        self.assertEqual(buffer.text, "/task")
        buffer.start_completion.assert_called_once_with()

    def test_all_static_subcommands_have_contextual_descriptions(self):
        for spec in laintas_cli.COMMAND_SPECS:
            for entry in spec.contextual_completions:
                self.assertTrue(entry.description.strip(), (spec.name, entry.value))
        completions = self._complete("/task pro")

        self.assertEqual([item.display_text for item in completions], ["progress"])
        self.assertEqual(
            completions[0].display_meta_text, "Update completion progress")

    def test_invalid_slash_text_has_no_completions(self):
        self.assertEqual(self._complete("/taskx"), [])
        self.assertEqual(self._complete("/task unrelated"), [])

    def test_alias_subcommand_completion_has_specific_description(self):
        completions = self._complete("/v updat")

        self.assertEqual([item.display_text for item in completions], ["update"])
        self.assertIn("install", completions[0].display_meta_text)
        self.assertIn("restart", completions[0].display_meta_text)

    def test_employee_help_and_completion_are_synchronized(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        agent_loop.close_all_agents()
        try:
            employee = agent_loop.register_agent(
                name="alice", depth=1, role="pool")
            completer = laintas_cli.MetaCompleter()
            completions = list(completer.get_completions(
                laintas_cli.Document("/station al", len("/station al")),
                mock.Mock(completion_requested=True)))
            profile_completions = list(completer.get_completions(
                laintas_cli.Document(
                    "/hire bob --profile rev",
                    len("/hire bob --profile rev")),
                mock.Mock(completion_requested=True)))
            laintas_cli.show_help("/hire")
            laintas_cli.show_help("/station")
        finally:
            laintas_cli.console = old_console
            agent_loop.close_all_agents()

        self.assertIn(employee.id, [item.text for item in completions])
        self.assertIn("reviewer", [item.text for item in profile_completions])
        text = output.getvalue()
        self.assertIn("does not start an assignment", text)
        self.assertIn("It is not auto-deployed", text)
        self.assertIn("private temporary terminal", text)
        self.assertIn("background Assignment", text)
        self.assertIn("isolated state/history", text)

    def test_clear_dispatches_as_new_session_command(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            self.assertFalse(laintas_cli.handle_meta_command(
                "/clear", _Registry(), {}))
        finally:
            laintas_cli.console = old_console
        self.assertIn("handled by the main REPL", output.getvalue())

    def test_usage_model_tier_mapping_and_rendering(self):
        balance = {
            "balanceFormatted": "$1.00",
            "pricing": {
                "defaultTier": "T3",
                "tiers": [{
                    "tier": "T1",
                    "models": ["deepseek-v4-pro"],
                }],
            },
        }
        self.assertEqual(
            laintas_cli._usage_model_tiers(balance),
            {"deepseek-v4-pro": "T1"},
        )

        totals = {
            "calls": 1, "in": 100, "out": 20,
            "costCents": 1, "estimated": False,
        }
        summary = {
            "session": {"totals": totals, "models": {}},
            "today": {"totals": totals, "models": {}},
            "range": {
                "totals": totals,
                "models": {"deepseek-v4-pro": totals},
            },
            "days": 30,
        }
        usage_response = mock.Mock(status_code=200)
        usage_response.json.return_value = {
            "overview": {}, "daily": [],
        }
        balance_response = mock.Mock(status_code=200)
        balance_response.json.return_value = balance
        profile = backend_profiles.BackendProfile(
            "official", "official", "https://laintas.com")
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(
            file=output, force_terminal=False, width=160,
            theme=Theme({
                "rule": "dim", "accent": "cyan", "agent": "magenta",
                "warning": "yellow", "muted": "dim", "success": "green",
            }))
        try:
            with mock.patch.object(
                    laintas_cli.usage_tracker, "summarize",
                    return_value=summary), \
                    mock.patch.object(
                        laintas_cli, "get_backend_profile",
                        return_value=profile), \
                    mock.patch.object(
                        laintas_cli.backend_profiles, "request_auth",
                        return_value=({}, {})), \
                    mock.patch.object(
                        laintas_cli.requests, "get",
                        side_effect=[usage_response, balance_response]):
                laintas_cli._show_usage_command([], {"userId": "u1"})
        finally:
            laintas_cli.console = old_console
        rendered = output.getvalue()
        self.assertIn("tier", rendered)
        self.assertIn("deepseek-v4-pro", rendered)
        self.assertIn("T1", rendered)
        header = next(line for line in rendered.splitlines()
                      if "model" in line and "tier" in line)
        self.assertLess(header.index("cost"), header.index("tier"))

    def test_compact_command_syncs_live_and_resume_state(self):
        state = {"_session_id": "s1", "_thread_messages": [{}] * 8}
        chat = [{"role": "user", "content": "task"}]
        live = {"session_id": "s1", "cwd": "/work"}
        attrs = {
            "_last_agent_state": state,
            "_last_chat_history": chat,
            "_last_deps": object(),
            "_last_session": {"userId": "u1"},
            "_current_live_session": live,
        }
        previous = {
            name: getattr(laintas_cli.handle_meta_command, name, None)
            for name in attrs
        }
        for name, value in attrs.items():
            setattr(laintas_cli.handle_meta_command, name, value)
        result = {
            "ok": True, "changed": True,
            "tokens": 12000, "after_tokens": 4000,
            "messages": 8, "after_messages": 4,
        }
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(
            file=output, force_terminal=False,
            theme=Theme({"rule": "dim", "accent": "cyan", "muted": "dim"}))
        try:
            with mock.patch.object(
                    laintas_cli, "compact_session_context",
                    return_value=result) as compact_mock, \
                    mock.patch.object(
                        laintas_cli.session_store, "sync_runtime",
                        return_value=live) as sync_mock, \
                    mock.patch.object(
                        laintas_cli, "save_resume_state") as save_mock, \
                    mock.patch.object(
                        laintas_cli.task_manager, "export_active_tasks",
                        return_value=[]), \
                    mock.patch.object(laintas_cli.event_log, "append"):
                self.assertFalse(laintas_cli.handle_meta_command(
                    "/compact", _Registry(), {}))
        finally:
            laintas_cli.console = old_console
            for name, value in previous.items():
                setattr(laintas_cli.handle_meta_command, name, value)
        compact_mock.assert_called_once()
        sync_mock.assert_called_once()
        save_mock.assert_called_once_with(state, chat, "/work")
        self.assertIn("12.0k → 4.0k tokens", output.getvalue())

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

    def test_mode_commands_create_switch_and_list(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            output = io.StringIO()
            old_console = laintas_cli.console
            laintas_cli.console = Console(file=output, force_terminal=False)
            try:
                commands = (
                    '/mode create strict --read-only "Only report confirmed defects"',
                    "/mode strict", "/mode list", "/mode act",
                )
                for command in commands:
                    self.assertFalse(laintas_cli.handle_meta_command(
                        command, _Registry(), {}))
            finally:
                laintas_cli.console = old_console
            text = output.getvalue()
            self.assertNotIn("failed:", text)
            self.assertIn("Created mode strict", text)
            self.assertIn("Switched to STRICT mode", text)
            self.assertEqual(mode_manager.get_active_mode()["name"], "act")

    def test_mode_without_args_uses_picker(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(
                    laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(
                    laintas_cli, "choose_record",
                    side_effect=lambda records, **_kwargs: next(
                        item for item in records if item["name"] == "review")):
            self.assertFalse(laintas_cli.handle_meta_command(
                "/mode", _Registry(), {}))
            self.assertEqual(mode_manager.get_active_mode()["name"], "review")

    def test_backend_use_without_name_uses_picker(self):
        selected = backend_profiles.BackendProfile(
            "test", "custom", "https://example.test")
        with mock.patch.object(
                laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(
                    backend_profiles, "list_profiles", return_value=[selected]), \
                mock.patch.object(
                    laintas_cli, "choose_record", return_value=selected), \
                mock.patch.object(
                    backend_profiles, "set_active",
                    return_value=(True, "selected")) as set_active:
            self.assertFalse(laintas_cli.handle_meta_command(
                "/backend use", _Registry(), {}))
        set_active.assert_called_once_with(selected.name)

    def test_policy_without_mode_uses_picker(self):
        with mock.patch.object(
                laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(
                    laintas_cli, "choose_record",
                    return_value={"name": "enforce"}), \
                mock.patch.object(
                    policy, "set_mode", return_value=(True, "enforced")) as set_mode:
            self.assertFalse(laintas_cli.handle_meta_command(
                "/policy", _Registry(), {}))
        set_mode.assert_called_once_with("enforce")

    def test_model_selector_preserves_provider_metadata(self):
        models = [
            {"id": "model-a", "provider": "provider-a"},
            {"id": "model-b", "provider": "provider-b"},
        ]

        def choose_second(items, **_kwargs):
            return items[1]

        with mock.patch.object(
                laintas_cli, "select_dialog", side_effect=choose_second):
            selected = laintas_cli.show_model_selector(models, "model-a")
        self.assertEqual(selected, models[1])

    def test_model_command_persists_selected_provider(self):
        models = [{
            "id": "model-x", "name": "Model X",
            "provider": "provider-a", "description": "Provider A",
        }]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    laintas_cli.paths, "SESSIONS_DIR", Path(tmp) / "sessions"), \
                mock.patch.object(
                    laintas_cli.paths, "TERMINAL_ID", "model-test"), \
                mock.patch.object(
                    laintas_cli, "CONFIG_FILE", Path(tmp) / "config.json"), \
                mock.patch.object(
                    laintas_cli, "fetch_available_models",
                    return_value=(models, "/api/models")), \
                mock.patch.object(
                    laintas_cli, "show_model_selector", return_value=models[0]), \
                mock.patch.object(
                    laintas_cli.sys.stdin, "isatty", return_value=True):
            agent_loop.close_all_terminals()
            agent_loop.close_all_agents()
            terminal_session = mock.Mock()
            terminal_session.is_alive.return_value = True
            agent_loop.register_terminal(
                terminal_session, "/bin/sh", 0, name="term0")
            primary = agent_loop.register_agent(name="primary", role="primary")
            agent_loop.set_current_agent_id(primary.id)
            terminal_preferences.reset_cache()
            output = io.StringIO()
            old_console = laintas_cli.console
            laintas_cli.console = Console(file=output, force_terminal=False)
            try:
                self.assertFalse(laintas_cli.handle_meta_command(
                    "/model", _Registry(), {}))
            finally:
                laintas_cli.console = old_console
            self.assertEqual(laintas_cli.get_selected_model(), "model-x")
            self.assertEqual(
                laintas_cli.get_selected_provider(), "provider-a")
            agent_loop.close_all_terminals()
            agent_loop.close_all_agents()

    def test_explicit_model_clears_stale_provider(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    laintas_cli.paths, "SESSIONS_DIR", Path(tmp)), \
                mock.patch.object(
                    laintas_cli.paths, "TERMINAL_ID", "model-direct"):
            agent_loop.close_all_terminals()
            agent_loop.close_all_agents()
            terminal_session = mock.Mock()
            terminal_session.is_alive.return_value = True
            agent_loop.register_terminal(
                terminal_session, "/bin/sh", 0, name="term0")
            primary = agent_loop.register_agent(name="primary", role="primary")
            agent_loop.set_current_agent_id(primary.id)
            terminal_preferences.reset_cache()
            laintas_cli.set_model_selection("old-model", "old-provider")
            output = io.StringIO()
            old_console = laintas_cli.console
            laintas_cli.console = Console(file=output, force_terminal=False)
            try:
                self.assertFalse(laintas_cli.handle_meta_command(
                    "/model new-model", _Registry(), {}))
            finally:
                laintas_cli.console = old_console
            self.assertEqual(laintas_cli.get_selected_model(), "new-model")
            self.assertEqual(laintas_cli.get_selected_provider(), "")
            agent_loop.close_all_terminals()
            agent_loop.close_all_agents()

    def test_model_can_target_terminal_without_mutating_agent_base(self):
        agent_loop.close_all_terminals()
        agent_loop.close_all_agents()
        root_session = mock.Mock()
        root_session.is_alive.return_value = True
        work_session = mock.Mock()
        work_session.is_alive.return_value = True
        agent_loop.register_terminal(root_session, "/bin/sh", 0, name="term0")
        primary = agent_loop.register_agent(name="primary", role="primary")
        agent_loop.set_current_agent_id(primary.id)
        agent_loop.register_terminal(
            work_session, "/bin/sh", 0, name="work", parent_terminal="term0")
        employee = agent_loop.register_agent(name="alice", role="pool")
        employee.base_model = "base-model"
        self.assertTrue(agent_loop.station_agent(employee.id, "work"))

        try:
            self.assertFalse(laintas_cli.handle_meta_command(
                "/model work terminal-model", _Registry(), {}))
            terminal = agent_loop.get_terminal("work")
            self.assertEqual(terminal.model_override, "terminal-model")
            self.assertEqual(employee.base_model, "base-model")
            self.assertFalse(laintas_cli.handle_meta_command(
                "/model work reset", _Registry(), {}))
            self.assertIsNone(terminal.model_override)
            self.assertEqual(employee.base_model, "base-model")
        finally:
            agent_loop.close_all_terminals()
            agent_loop.close_all_agents()

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

    def test_binary_update_restarts_from_replaced_executable(self):
        installed_path = "/usr/local/bin/laintas-cli"
        with mock.patch.object(updater, "is_frozen", return_value=True), \
                mock.patch.object(updater, "fetch_manifest", return_value={
                    "version": "999.0.0",
                }), \
                mock.patch.object(updater, "is_newer", return_value=True), \
                mock.patch.object(
                    updater, "apply_frozen_update", return_value=installed_path), \
                mock.patch.object(laintas_cli, "stop_trigger_scanner"), \
                mock.patch.object(laintas_cli, "close_all_terminals"), \
                mock.patch.object(
                    laintas_cli.browser_mod, "close_all_browser_sessions"), \
                mock.patch.object(
                    laintas_cli.sys, "argv", ["laintas-cli", "--resume"]), \
                mock.patch.object(
                    laintas_cli, "_restart_process") as restart_mock:
            laintas_cli.handle_version_command(["/v", "update"])

        restart_mock.assert_called_once_with(installed_path)

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

    def test_declared_slash_leaves_reject_ignored_arguments(self):
        cases = (
            ("/help task extra", "/help [command]"),
            ("/connect worker extra", "/connect [folder]"),
            ("/terminate term1 extra", "/terminate <name>"),
            ("/abort agent1 extra", "/abort <agent-id>"),
            ("/task done 1 extra", "/task done <id>"),
            ("/skill load demo extra", "/skill load <name>"),
            ("/bash add vim extra", "/bash add <command>"),
            ("/plan approve extra", "/plan approve"),
            ("/version check extra", "/version check"),
            ("/update --force extra", "/update [--force]"),
        )
        for command, usage in cases:
            action, _, parts = laintas_cli._parse_slash_command(command)
            with self.subTest(command=command), self.assertRaisesRegex(
                    laintas_cli.SlashCommandUsageError, usage.replace("[", r"\[").replace("]", r"\]")):
                laintas_cli._validate_slash_args(action, parts[1:])

    def test_approval_flags_reject_unknown_trailing_arguments(self):
        cases = (
            "/policy disabled typo",
            "/trust allow typo",
            "/hooks trust typo",
            "/skill trust demo typo",
            "/mcp trust demo typo",
        )
        for command in cases:
            action, _, parts = laintas_cli._parse_slash_command(command)
            with self.subTest(command=command), self.assertRaisesRegex(
                    laintas_cli.SlashCommandUsageError, "Unexpected argument"):
                laintas_cli._validate_slash_args(action, parts[1:])

    def test_uncontracted_and_free_form_slash_commands_remain_open(self):
        # Unknown commands may belong to extensions; free-form built-ins consume
        # their raw tail.  Neither is subject to the opt-in leaf contracts.
        for command in (
            "/project-extension one two three",
            "/spawn worker: investigate one two three",
            "/tell agent1 this is a multi word message",
            "/snapshot release candidate one",
        ):
            action, _, parts = laintas_cli._parse_slash_command(command)
            with self.subTest(command=command):
                laintas_cli._validate_slash_args(action, parts[1:])

    def test_hire_defines_employee_profile_without_starting_work(self):
        agent_loop.close_all_terminals()
        agent_loop.close_all_agents()
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False)
        try:
            primary = agent_loop.register_agent(name="primary", role="primary")
            primary.home_terminal = "term0"
            agent_loop.set_current_agent_id(primary.id)
            terminal_session = mock.Mock()
            terminal_session.is_alive.return_value = True
            agent_loop.register_terminal(
                terminal_session, "/bin/sh", 0, name="term0")
            with mock.patch("agent_persistence.save_agent_state", return_value=True), \
                    mock.patch("agent_persistence.delete_agent_state", return_value=True):
                laintas_cli.handle_meta_command(
                    "/hire alice --profile reviewer", _Registry(), {})
                employee = agent_loop.get_agent("alice")
                self.assertIsNotNone(employee)
                self.assertEqual(employee.profile.specialist_role, "reviewer")
                self.assertEqual(employee.status, "idle")
                self.assertEqual(employee.role, "pool")
                self.assertIsNone(employee.stationed_terminal)
                self.assertEqual(employee.home_terminal, "term0")
                self.assertNotIn("alice", agent_loop.get_terminal("term0").stationed_agent_ids)
                self.assertIsNone(employee.active_assignment)
                self.assertNotIn(
                    "shell.exec", employee.profile.tool_policy.allowed_tools)
                agent_loop.close_all_terminals()
        finally:
            laintas_cli.console = old_console
            agent_loop.close_all_terminals()
            agent_loop.close_all_agents()
        self.assertIn("Hired employee: alice", output.getvalue())

    def test_station_task_starts_assignment_without_switching_manager(self):
        agent_loop.close_all_agents()
        manager = agent_loop.register_agent(name="primary", role="primary")
        employee = agent_loop.register_agent(name="alice", role="pool")
        agent_loop.set_current_agent_id(manager.id)
        terminal = mock.Mock()
        terminal.session.is_alive.return_value = True
        assignment = mock.Mock(task="fix login race")
        registry = _Registry()
        try:
            with mock.patch.object(
                    laintas_cli, "get_terminal", return_value=terminal), \
                    mock.patch.object(laintas_cli, "station_agent") as station, \
                    mock.patch.object(
                        laintas_cli, "start_agent_assignment",
                        return_value=(True, "started", assignment)) as start:
                laintas_cli.handle_meta_command(
                    "/station alice work-a --task fix login race",
                    registry, {})
            station.assert_called_once_with(employee.id, "work-a")
            self.assertEqual(start.call_args.args[:2],
                             (employee.id, "fix login race"))
            self.assertEqual(agent_loop.get_current_agent().id, manager.id)
        finally:
            agent_loop.close_all_agents()

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
                    mock.patch.object(laintas_cli, "authorize_direct_command",
                                      return_value=(True, "")), \
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
                    return_value=mock.Mock(session=session)), \
                    mock.patch.object(laintas_cli, "authorize_direct_command",
                                      return_value=(True, "")):
                laintas_cli.handle_meta_command(
                    "/send term1 --wait 0.01 echo hi", _Registry(), {})
        finally:
            laintas_cli.console = old_console
        self.assertIn("NEW OUTPUT", output.getvalue())
        self.assertNotIn("STALE OUTPUT", output.getvalue())

    def test_bash_denial_does_not_execute(self):
        session = mock.Mock()
        session.is_alive.return_value = True
        terminal = mock.Mock(session=session)
        with mock.patch.object(laintas_cli, "get_terminal", return_value=terminal), \
                mock.patch.object(laintas_cli, "authorize_direct_command",
                                  return_value=(False, "denied")), \
                mock.patch.object(laintas_cli, "_marker_poll_exec") as execute:
            laintas_cli.handle_meta_command(
                "/bash rm file.txt", _Registry(), {})
        execute.assert_not_called()

    def test_send_denial_does_not_send_keys(self):
        session = mock.Mock(full_output="")
        session.is_alive.return_value = True
        with mock.patch.object(
                laintas_cli, "get_terminal",
                return_value=mock.Mock(session=session)), \
                mock.patch.object(laintas_cli, "authorize_direct_command",
                                  return_value=(False, "denied")):
            laintas_cli.handle_meta_command(
                "/send term1 rm file.txt", _Registry(), {})
        session.send_keys.assert_not_called()


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

    def test_export_install_round_trip_preserves_one_patch(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            root = Path(tmp)
            prompts = root / "prompts"
            candidates = prompts / "candidates"
            (root / ".laintas").mkdir()
            (root / ".laintas/cli.prop").write_text("BASE\n", encoding="utf-8")
            with mock.patch.multiple(
                    prompt_opt,
                    CANDIDATES_DIR=candidates,
                    FEEDBACK_LOG=prompts / "feedback.jsonl",
                    STATE_PATH=prompts / "_state.json"), \
                    mock.patch.object(prompt_opt.paths, "PROMPTS_DIR", prompts):
                prompt_opt._current_opt = None
                prompt_opt._optimizations = {}
                original = prompt_opt.draft_candidate(
                    "feedback-1", "<rule>ask first</rule>", "reason")
                ok, pack_path = prompt_opt.export_pack(
                    original["id"], str(root / "pack.md"))
                self.assertTrue(ok)
                ok, _, imported_id = prompt_opt.install_pack(pack_path)
                self.assertTrue(ok)
                imported = prompt_opt.read_candidate(imported_id)
                self.assertEqual(imported["patch"], "<rule>ask first</rule>")
                self.assertNotIn("<prompt_opt_patch>", imported["patch"])
                self.assertEqual(
                    prompt_opt.read_candidate(original["id"])["feedback"],
                    "feedback-1",
                )


class PlanAndWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._terminal_preferences_dir = tempfile.TemporaryDirectory()
        self._sessions_patch = mock.patch.object(
            laintas_cli.paths, "SESSIONS_DIR",
            Path(self._terminal_preferences_dir.name))
        self._terminal_patch = mock.patch.object(
            laintas_cli.paths, "TERMINAL_ID", "plan-tests")
        self._sessions_patch.start()
        self._terminal_patch.start()
        terminal_preferences.reset_cache()

    def tearDown(self):
        terminal_preferences.reset_cache()
        self._terminal_patch.stop()
        self._sessions_patch.stop()
        self._terminal_preferences_dir.cleanup()

    def test_auto_mode_has_autonomous_prompt_and_timed_confirmations(self):
        auto = mode_manager.get_mode("auto")
        self.assertIsNotNone(auto)
        with mock.patch.object(mode_manager, "get_active_mode", return_value=auto):
            section = mode_manager.render_prompt_section()
            self.assertEqual(mode_manager.get_auto_confirm_timeout(), 3.0)
            self.assertEqual(
                mode_manager.get_auto_confirm_timeout(destructive=True), 60.0)

        self.assertIn("persistent autonomous engineering agent", section)
        self.assertIn("complete, verified outcome", section)
        self.assertIn("Use Git intelligently", section)
        self.assertIn("Deletion is a last resort", section)
        self.assertIn("approved after 3 seconds", section)
        self.assertIn("approved after 60 seconds", section)

    def test_act_mode_prompt_is_action_oriented_and_deletion_safe(self):
        act = mode_manager.get_mode("act")
        with mock.patch.object(mode_manager, "get_active_mode", return_value=act):
            section = mode_manager.render_prompt_section()

        self.assertIn("[AGENT MODE: ACT]", section)
        self.assertIn("ordinary reversible work already authorized", section)
        self.assertIn("comprehensive, systematic analysis", section)
        self.assertIn("exactly what the target is and contains", section)
        self.assertIn("policy BLOCKED result forbids the underlying operation", section)
        self.assertIn("never retry it through find", section)

    def test_custom_agent_mode_persists_and_restricts_tools(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            ok, _ = mode_manager.create_mode(
                "audit-review", "Find concrete defects.", read_only=True)
            self.assertTrue(ok)
            self.assertTrue(mode_manager.activate("audit-review")[0])
            self.assertEqual(
                mode_manager.get_active_mode()["name"], "audit-review")
            self.assertTrue(mode_manager.is_tool_allowed("fs.read"))
            self.assertFalse(mode_manager.is_tool_allowed("fs.write"))
            self.assertIn("Find concrete defects", mode_manager.render_prompt_section())
            self.assertTrue(mode_manager.delete_mode("audit-review")[0])
            self.assertEqual(mode_manager.get_active_mode()["name"], "act")

    def test_builtin_review_mode_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            self.assertTrue(mode_manager.activate("review")[0])
            self.assertTrue(mode_manager.is_tool_allowed("fs.grep"))
            self.assertFalse(mode_manager.is_tool_allowed("shell.exec"))
            self.assertFalse(mode_manager.delete_mode("review")[0])

    def test_plan_can_wait_for_next_task_message(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            root = Path(tmp)
            with mock.patch.object(plan_mode, "PLANS_DIR", root / "plans"), \
                    mock.patch.object(plan_mode, "_STATE_PATH", root / "plans/_state.json"):
                plan_mode._loaded_cwd = None
                plan_mode._current_plan = None
                plan_mode._plan_mode = False
                plan_mode._pending_task = False
                plan_mode.arm_plan_mode()
                self.assertTrue(plan_mode.is_plan_mode())
                self.assertTrue(plan_mode.is_pending_task())
                plan_mode._plan_mode = False
                plan_mode._pending_task = False
                plan_mode._restore_state()
                self.assertTrue(plan_mode.is_pending_task())
                plan = plan_mode.enter_plan_mode("describe it later")
                self.assertEqual(plan["task"], "describe it later")
                self.assertFalse(plan_mode.is_pending_task())
                plan_mode.exit_plan_mode()

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

    def test_resume_payload_title_and_turns_ignore_shell_input(self):
        history = [
            {"role": "user", "content": "修复恢复逻辑", "input_kind": "prompt"},
            {"role": "assistant", "content": "处理中"},
            {"role": "user", "content": "clear", "input_kind": "shell"},
            {"role": "user", "content": "ls", "input_kind": "shell"},
            {"role": "shell", "content": "a.py\nb.py", "returncode": 0},
        ]

        payload = agent_loop._build_resume_payload({}, history, "/fake/project", "checkpoint")

        self.assertEqual(payload["title"], "修复恢复逻辑")
        self.assertEqual(payload["turn_count"], 1)

    def test_identical_quit_autosave_and_checkpoint_are_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cwd = "/fake/project"
            key = agent_loop._session_key(cwd)
            base = {
                "session_id": "sid-quit", "cwd": cwd,
                "title": "meaningful task", "turn_count": 1,
                "chat_history": [{
                    "role": "user", "content": "meaningful task",
                    "input_kind": "prompt",
                }],
                "state": {}, "tasks": [],
            }
            checkpoint = {
                **base, "id": "chk-quit", "kind": "checkpoint",
                "timestamp": time.time() - 0.1,
            }
            autosave = {
                **base, "id": "sid-quit", "kind": "autosave",
                "timestamp": time.time(),
            }
            (tmp_path / f"{key}_resume_chk-quit.json").write_text(
                json.dumps(checkpoint), encoding="utf-8")
            (tmp_path / f"{key}_session_sid-quit.json").write_text(
                json.dumps(autosave), encoding="utf-8")

            with mock.patch.object(agent_loop.paths, "SESSIONS_DIR", tmp_path):
                states = agent_loop.list_resume_states(cwd)

            self.assertEqual(len(states), 1)
            self.assertEqual(states[0]["kind"], "checkpoint")

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
        self.assertIn("20/30 event(s)", text)
        self.assertIn("most recent", text)
        self.assertIn("message 29", text)
        self.assertNotIn("message 9", text)

    def test_all_shows_every_message(self):
        text = self._render(self._blob(30), None)
        self.assertIn("30/30 event(s)", text)
        self.assertIn("(all)", text)
        self.assertIn("message 0", text)
        self.assertIn("message 29", text)
        self.assertIn("earlier goals digest", text)

    def test_custom_n_shows_last_n(self):
        text = self._render(self._blob(30), 5)
        self.assertIn("5/30 event(s)", text)
        self.assertIn("message 29", text)
        self.assertNotIn("message 24", text)
        self.assertIn("message 25", text)

    def test_zero_prints_nothing(self):
        text = self._render(self._blob(10), 0)
        self.assertEqual(text.strip(), "")

    def test_older_summary_only_when_window_reaches_start(self):
        text = self._render(self._blob(30), 5)
        self.assertNotIn("earlier goals digest", text)

    def test_structured_events_use_prompt_shell_and_tool_styles(self):
        blob = {
            "chat_history": [
                {"role": "user", "content": "检查项目", "input_kind": "prompt"},
                {"role": "assistant", "content": "我先检查。"},
                {"role": "tool", "tool_name": "terminal.create",
                 "display_name": "terminal.create", "summary": "worker",
                 "content": "Created worker", "ok": True},
                {"role": "user", "content": "ls", "input_kind": "shell"},
                {"role": "shell", "content": "a.py", "returncode": 0},
            ],
            "older_summary": "", "turn_count": 1,
        }

        text = self._render(blob, None)

        self.assertIn("❯ 检查项目", text)
        self.assertIn("我先检查。", text)
        self.assertIn("terminal.create", text)
        self.assertIn("worker", text)
        self.assertIn("Created worker", text)
        self.assertIn("$ ls", text)
        self.assertIn("a.py", text)
        self.assertNotIn("\nknowledge\n", text)


class TerminalOutputStyleTests(unittest.TestCase):
    def test_markdown_and_code_inherit_terminal_background(self):
        output = io.StringIO()
        test_console = Console(
            file=output,
            force_terminal=True,
            color_system="truecolor",
            width=100,
            theme=laintas_cli.LAINTAS_THEME,
            style="white",
        )

        test_console.print(laintas_cli.Markdown(
            "ordinary `inline`\n\n```python\nprint('white')\n```"))

        rendered = output.getvalue()
        self.assertIn("ordinary", rendered)
        self.assertIn("print", rendered)
        self.assertNotIn("\x1b[48;", rendered)
        self.assertNotIn("\x1b[44m", rendered)

    def test_prompt_toolkit_chrome_preserves_slash_menu_only(self):
        style = laintas_cli._build_prompt_style()
        for style_name in ("bottom-toolbar", "paste-placeholder"):
            attrs = style.get_attrs_for_style_str(f"class:{style_name}")
            self.assertFalse(attrs.bgcolor, style_name)

        for style_name in (
                "completion-menu",
                "completion-menu.completion.current"):
            attrs = style.get_attrs_for_style_str(f"class:{style_name}")
            self.assertTrue(attrs.bgcolor, style_name)

        for style_name in ("task.text", "task.run", "task.done", "task.err"):
            attrs = hwo_ui._STYLE.get_attrs_for_style_str(
                f"class:{style_name}")
            self.assertFalse(attrs.bgcolor, style_name)

    def test_todolist_distinguishes_todo_hwo_and_hwg_without_background(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="truecolor",
            width=140,
            theme=laintas_cli.LAINTAS_THEME,
            style="white",
        )
        tasks = [
            {"id": "1", "subject": "plain item", "status": "pending",
             "progress": 0, "metadata": {}},
            {"id": "2", "subject": "workflow step", "status": "in_progress",
             "progress": 40, "metadata": {"workflowRunId": "run-1"}},
            {"id": "3", "subject": "graph node", "status": "completed",
             "progress": 100,
             "metadata": {"workflowRunId": "run-2", "nodeId": "n1",
                          "kind": "hwg-node"}},
        ]
        try:
            laintas_cli._render_task_todolist(tasks, "/workspace")
        finally:
            laintas_cli.console = old_console

        rendered = output.getvalue()
        for expected in ("TODO", "HWO", "HWG", "plain item",
                         "workflow step", "graph node"):
            self.assertIn(expected, rendered)
        self.assertNotIn("\x1b[48;", rendered)

    def test_task_command_shows_current_agent_tree_for_current_session(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(
            file=output, force_terminal=False, width=160,
            theme=laintas_cli.LAINTAS_THEME)
        agent_loop.close_all_agents()
        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                root = agent_loop.register_agent(name="root-task", role="primary")
                root.state["_session_id"] = "session-visible"
                child = agent_loop.register_agent(
                    name="child-task", depth=1, parent_id=root.id,
                    role="subagent")
                agent_loop.set_current_agent_id(root.id)
                task_manager.create_task(
                    "root visible", cwd=tmp, session_id="session-visible",
                    owner_agent_id=root.id)
                task_manager.create_task(
                    "child visible", cwd=tmp, session_id="session-visible",
                    owner_agent_id=child.id, parent_agent_id=root.id)
                task_manager.create_task(
                    "other session hidden", cwd=tmp,
                    session_id="session-hidden", owner_agent_id=root.id)

                laintas_cli._cmd_task("", ["/task"])
        finally:
            os.chdir(old_cwd)
            agent_loop.close_all_agents()
            laintas_cli.console = old_console

        rendered = output.getvalue()
        self.assertIn("root visible", rendered)
        self.assertIn("child visible", rendered)
        self.assertNotIn("other session hidden", rendered)

    def test_terminal_style_instruction_is_laintas_prompt_local_and_small(self):
        prompt = laintas_cli.generate_cli_prop_template()
        start = prompt.index("<terminal_output_style>")
        end = prompt.index("</terminal_output_style>")
        section = prompt[start:end]

        self.assertIn("user's terminal background", section)
        self.assertIn("ANSI 24-bit SGR", section)
        self.assertIn("foreground and background", section)
        self.assertLess(len(section), 900)


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
