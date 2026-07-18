import io
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

import agent_loop
import laintas_cli


def _text(fragments):
    return "".join(value for _style, value in fragments)


class ResponsiveTerminalChromeTests(unittest.TestCase):
    def setUp(self):
        self._status = dict(laintas_cli._status_cache)
        laintas_cli._status_cache.update({
            "model": "model-long",
            "agent": "primary",
            "terminal": "term0",
            "deployment": "deployed",
            "last_thinking_time": 2.4,
        })

    def tearDown(self):
        laintas_cli._status_cache.clear()
        laintas_cli._status_cache.update(self._status)

    def test_rprompt_progressively_discloses_context(self):
        common = (
            mock.patch("plan_mode.is_plan_mode", return_value=False),
            mock.patch.object(
                laintas_cli.mode_manager, "get_active_mode",
                return_value={"name": "act"}),
        )
        with common[0], common[1], mock.patch.object(
                laintas_cli, "_terminal_width", return_value=48):
            narrow = _text(laintas_cli._render_rprompt())
        with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                  return_value={"name": "act"}), \
                mock.patch.object(laintas_cli, "_terminal_width", return_value=80):
            medium = _text(laintas_cli._render_rprompt())
        with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                  return_value={"name": "act"}), \
                mock.patch.object(laintas_cli, "_terminal_width", return_value=120):
            wide = _text(laintas_cli._render_rprompt())

        self.assertEqual(narrow, "ACT")
        self.assertIn("model-long", medium)
        self.assertNotIn("primary@term0", medium)
        self.assertIn("primary@term0", wide)

    def test_bottom_toolbar_never_forces_wide_context_on_narrow_terminal(self):
        with mock.patch.object(laintas_cli, "_session_token_totals",
                               return_value=(1234, 56)), \
                mock.patch.object(laintas_cli, "_terminal_width", return_value=48):
            narrow = _text(laintas_cli._render_bottom_toolbar())
        with mock.patch.object(laintas_cli, "_session_token_totals",
                               return_value=(1234, 56)), \
                mock.patch.object(laintas_cli, "_terminal_width", return_value=100):
            wide = _text(laintas_cli._render_bottom_toolbar())

        self.assertEqual(narrow, "↑1.2k ↓56")
        self.assertIn("term0 · deployed", wide)
        self.assertIn("last 2.4s", wide)

    def test_context_sync_distinguishes_terminal_model_override(self):
        agent = SimpleNamespace(
            id="a1", name="primary", base_model="base-model",
            deployment_terminal="term0", stationed_terminal="term0",
            home_terminal="term0",
        )
        terminal = SimpleNamespace(model_override="terminal-model")
        with mock.patch.object(laintas_cli, "get_current_agent", return_value=agent), \
                mock.patch.object(laintas_cli, "get_terminal", return_value=terminal), \
                mock.patch.object(laintas_cli, "get_runtime_config", return_value=False):
            laintas_cli._sync_status_context()

        self.assertEqual(laintas_cli._status_cache["model"], "terminal-model")
        self.assertEqual(laintas_cli._status_cache["model_source"], "terminal")
        self.assertEqual(laintas_cli._status_cache["deployment"], "deployed")

    def test_rprompt_places_agent_before_mode_on_medium_width(self):
        laintas_cli._status_cache.update({
            "agent": "agent1", "model": "glm-5.2", "terminal": "term0"})
        with mock.patch.object(laintas_cli, "_terminal_width", return_value=80), \
                mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                  return_value={"name": "act"}), \
                mock.patch("plan_mode.is_plan_mode", return_value=False):
            text = _text(laintas_cli._render_rprompt())
        self.assertTrue(text.startswith("agent1 · ACT"))
        self.assertIn("glm-5.2", text)

    def test_prompt_uses_one_foreground_without_agent_routing(self):
        prompt_session = mock.Mock()
        prompt_session.prompt.return_value = "inspect another directory"
        coordinator = mock.Mock()
        coordinator.configured = True
        with mock.patch.object(laintas_cli, "_terminal_agents", coordinator), \
                mock.patch.object(laintas_cli, "get_prompt_session",
                                  return_value=prompt_session), \
                mock.patch.object(laintas_cli, "_sync_status_context"), \
                mock.patch.object(laintas_cli, "_terminal_width",
                                  return_value=80), \
                mock.patch("plan_mode.is_plan_mode", return_value=False):
            value = laintas_cli.pt_prompt("/tmp")

        self.assertEqual(value, "inspect another directory")
        coordinator.commit_input_target.assert_not_called()
        coordinator.pin_input_target.assert_not_called()
        prompt_kwargs = prompt_session.prompt.call_args.kwargs
        self.assertIsInstance(prompt_session.prompt.call_args.args[0], list)
        self.assertNotIn("refresh_interval", prompt_kwargs)

    def test_sections_and_tables_are_copy_friendly_without_borders(self):
        output = io.StringIO()
        c = Console(file=output, force_terminal=False, width=100)
        c.print(laintas_cli.Panel("https://accounts.laintas.com/login", title="Laintas Auth"))
        table = laintas_cli.Table(title="Models")
        table.add_column("ID")
        table.add_row("glm-5.2")
        c.print(table)
        rendered = output.getvalue()
        self.assertIn("Laintas Auth", rendered)
        self.assertIn("https://accounts.laintas.com/login", rendered)
        self.assertIn("glm-5.2", rendered)
        for border in ("╭", "╮", "╰", "╯", "┏", "┓", "┗", "┛"):
            self.assertNotIn(border, rendered)

    def test_startup_banner_preserves_original_environment_summary(self):
        output = io.StringIO()
        old_console = laintas_cli.console
        laintas_cli.console = Console(file=output, force_terminal=False, width=120)
        try:
            with mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                      return_value={"name": "act"}), \
                    mock.patch("plan_mode.is_plan_mode", return_value=False), \
                    mock.patch("policy.get_config", return_value={"mode": "audit"}), \
                    mock.patch.object(laintas_cli, "get_backend_profile",
                                      return_value=SimpleNamespace(
                                          base_url="https://laintas.com",
                                          kind="official",
                                          billing_label="Laintas")), \
                    mock.patch.object(laintas_cli.task_manager, "list_tasks",
                                      return_value=[]):
                laintas_cli.show_banner(
                    "primary", {"userEmail": "user@example.com"})
        finally:
            laintas_cli.console = old_console

        rendered = output.getvalue()
        self.assertIn("╭─╮", rendered)
        self.assertIn("cli", rendered)
        self.assertIn("user@example.com", rendered)
        self.assertIn("Linux", rendered)
        self.assertIn("https://laintas.com", rendered)
        self.assertIn("mode", rendered)
        self.assertIn("policy", rendered)
        self.assertIn("/mode", rendered)
        self.assertIn("/policy", rendered)

    def test_prompt_uses_thin_unbold_chevron(self):
        captured = {}
        prompt_session = mock.Mock()

        def prompt(message, **_kwargs):
            captured["message"] = message
            return ""

        prompt_session.prompt.side_effect = prompt
        with mock.patch.object(laintas_cli, "get_prompt_session",
                               return_value=prompt_session), \
                mock.patch.object(laintas_cli, "_sync_status_context"), \
                mock.patch.object(laintas_cli, "_terminal_width", return_value=80), \
                mock.patch("plan_mode.is_plan_mode", return_value=False):
            laintas_cli.pt_prompt("/tmp")

        prompt_text = "".join(value for _style, value in captured["message"])
        self.assertIn("› ", prompt_text)
        self.assertNotIn("❯", prompt_text)
        attrs = laintas_cli._build_prompt_style().get_attrs_for_style_str(
            "class:prompt-caret")
        self.assertFalse(attrs.bold)

    def test_no_color_flag_is_presence_based(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(laintas_cli._no_color_requested())
        with mock.patch.dict("os.environ", {"NO_COLOR": ""}, clear=True):
            self.assertTrue(laintas_cli._no_color_requested())

    def test_thinking_shimmer_moves_without_changing_text_width(self):
        first = agent_loop._shimmer_label("Thinking…", 0.0)
        later = agent_loop._shimmer_label("Thinking…", 0.35)

        self.assertEqual(first.plain, "Thinking…")
        self.assertEqual(later.plain, "Thinking…")
        self.assertEqual(len(first), len(later))
        self.assertNotEqual(
            [str(span.style) for span in first.spans],
            [str(span.style) for span in later.spans],
        )

    def test_cell_crop_handles_cjk_and_preserves_budget(self):
        cropped = agent_loop._crop_cells("目录/非常长的文件名.py", 12, middle=True)
        self.assertLessEqual(agent_loop._cell_len(cropped), 12)
        self.assertIn("…", cropped)

    def test_compact_tool_line_preserves_failure_action(self):
        _name, hint, meta = agent_loop._compact_tool_line(
            "shell", "cd /root/a/very/long/path && git branch -a",
            "exit -1 · Command timed out (60s) · /why", 72)
        self.assertIn("/why", meta)
        self.assertIn("…", hint)

    def test_adaptive_delay_is_fast_normally_and_backs_off_on_failure(self):
        self.assertLessEqual(agent_loop._adaptive_loop_delay(1.5, failed=False), 0.25)
        self.assertGreaterEqual(
            agent_loop._adaptive_loop_delay(0.2, failed=True, retry_count=1), 1.5)

    def test_recent_failure_filters_by_scope(self):
        with agent_loop._failure_lock:
            old = list(agent_loop._recent_tool_failures)
            agent_loop._recent_tool_failures.clear()
        try:
            agent_loop._remember_tool_failure({
                "tool": "shell.exec", "agent_id": "a1", "terminal": "term1"})
            agent_loop._remember_tool_failure({
                "tool": "fs.read", "agent_id": "a2", "terminal": "term2"})
            rows = agent_loop.get_recent_tool_failures(agent_id="a1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["terminal"], "term1")
        finally:
            with agent_loop._failure_lock:
                agent_loop._recent_tool_failures[:] = old

    def test_term0_health_replaces_dirty_session_without_replacing_metadata(self):
        old_session = mock.Mock()
        old_session.is_alive.return_value = True
        old_session._laintas_shell_dirty = True
        old_session._laintas_last_cwd = "/tmp"
        terminal = SimpleNamespace(
            session=old_session, command="bash", completed_at=1,
            returncode=9, stationed_agent_id="primary",
        )
        replacement = mock.Mock()
        replacement.is_alive.return_value = True

        with mock.patch.object(laintas_cli, "get_terminal",
                               return_value=terminal), \
                mock.patch.object(laintas_cli, "InteractiveSession",
                                  return_value=replacement):
            laintas_cli._ensure_term0_alive()

        self.assertIs(terminal.session, replacement)
        self.assertEqual(terminal.stationed_agent_id, "primary")
        self.assertIsNone(terminal.completed_at)
        self.assertIsNone(terminal.returncode)
        old_session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
