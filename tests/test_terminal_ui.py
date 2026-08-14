import io
import os
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

import agent_loop
import laintas_cli
import tools


def _text(fragments):
    return "".join(value for _style, value in fragments)


class PromptInputLifecycleTests(unittest.TestCase):
    def test_prompt_toolkit_eof_reaches_main_loop(self):
        prompt_session = mock.Mock()
        prompt_session.prompt.side_effect = EOFError
        with mock.patch.object(
                laintas_cli, "get_prompt_session",
                return_value=prompt_session), \
                mock.patch.object(
                    laintas_cli, "_terminal_width", return_value=80), \
                mock.patch("plan_mode.is_plan_mode", return_value=False):
            with self.assertRaises(EOFError):
                laintas_cli.pt_prompt("/tmp")

    def test_prompt_toolkit_unexpected_error_is_not_turned_into_busy_loop(self):
        prompt_session = mock.Mock()
        prompt_session.prompt.side_effect = RuntimeError("prompt failed")
        with mock.patch.object(
                laintas_cli, "get_prompt_session",
                return_value=prompt_session), \
                mock.patch.object(
                    laintas_cli, "_terminal_width", return_value=80), \
                mock.patch("plan_mode.is_plan_mode", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "prompt failed"):
                laintas_cli.pt_prompt("/tmp")

    def test_plain_input_eof_reaches_main_loop(self):
        stdin = SimpleNamespace(buffer=io.BytesIO(b""))
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch("builtins.print"):
            with self.assertRaises(EOFError):
                laintas_cli._simple_prompt("/tmp")

    @unittest.skipUnless(hasattr(os, "openpty"), "requires PTY support")
    def test_disconnected_pty_is_detected_and_read_exits(self):
        master_fd, slave_fd = os.openpty()
        slave = os.fdopen(slave_fd, "rb", buffering=0)
        stdin = SimpleNamespace(buffer=slave, fileno=slave.fileno)
        os.close(master_fd)
        try:
            with mock.patch.object(sys, "stdin", stdin), \
                    mock.patch("builtins.print"):
                self.assertTrue(laintas_cli._stdin_terminal_disconnected())
                with self.assertRaises(EOFError):
                    laintas_cli._simple_prompt("/tmp")
        finally:
            slave.close()


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

        # Every rprompt ends with one reserved space: prompt_toolkit aligns it
        # flush to the terminal edge, and a glyph in the final column arms the
        # terminal's deferred wrap, which desyncs the renderer and stacks the
        # prompt down the screen. Assert the padding, then compare content.
        for rendered in (narrow, medium, wide):
            self.assertTrue(rendered.endswith(" "),
                            f"rprompt must reserve the last column: {rendered!r}")
        self.assertEqual(narrow.rstrip(), "ACT")
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
        self.assertIn("/training on", rendered)

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


class FmtElapsedTests(unittest.TestCase):
    def test_zero_returns_empty(self):
        self.assertEqual(laintas_cli._fmt_elapsed(0), "")

    def test_negative_returns_empty(self):
        self.assertEqual(laintas_cli._fmt_elapsed(-1.5), "")

    def test_milliseconds(self):
        self.assertEqual(laintas_cli._fmt_elapsed(0.5), "500ms")

    def test_sub_second_precision(self):
        self.assertEqual(laintas_cli._fmt_elapsed(0.123), "123ms")

    def test_seconds_with_decimal(self):
        self.assertEqual(laintas_cli._fmt_elapsed(5.4), "5.4s")

    def test_seconds_just_under_minute(self):
        self.assertEqual(laintas_cli._fmt_elapsed(59.9), "59.9s")

    def test_minutes(self):
        self.assertEqual(laintas_cli._fmt_elapsed(125), "2m5s")

    def test_hours(self):
        self.assertEqual(laintas_cli._fmt_elapsed(3700), "1h1m40s")

    def test_multiple_hours(self):
        self.assertEqual(laintas_cli._fmt_elapsed(7384), "2h3m4s")

    def test_exact_hour(self):
        self.assertEqual(laintas_cli._fmt_elapsed(3600), "1h0m0s")


class TruncateWithEllipsisTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(laintas_cli._truncate_with_ellipsis("hello", 80), "hello")

    def test_exact_fit_unchanged(self):
        self.assertEqual(laintas_cli._truncate_with_ellipsis("hello", 5), "hello")

    def test_truncated_adds_ellipsis(self):
        result = laintas_cli._truncate_with_ellipsis("hello world", 8)
        self.assertEqual(len(result), 8)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(result, "hello w…")

    def test_empty_string(self):
        self.assertEqual(laintas_cli._truncate_with_ellipsis("", 80), "")

    def test_one_char_max(self):
        self.assertEqual(laintas_cli._truncate_with_ellipsis("ab", 1), "…")


class ParseSubtaskJsonTests(unittest.TestCase):
    def test_clean_json_array(self):
        result = laintas_cli._parse_subtask_json('["task A", "task B"]')
        self.assertEqual(result, ["task A", "task B"])

    def test_json_in_code_fence(self):
        text = '```json\n["task A", "task B"]\n```'
        result = laintas_cli._parse_subtask_json(text)
        self.assertEqual(result, ["task A", "task B"])

    def test_json_in_plain_code_fence(self):
        text = '```\n["task A", "task B"]\n```'
        result = laintas_cli._parse_subtask_json(text)
        self.assertEqual(result, ["task A", "task B"])

    def test_json_with_surrounding_prose(self):
        text = 'Here are the subtasks:\n["task A", "task B"]\nLet me know.'
        result = laintas_cli._parse_subtask_json(text)
        self.assertEqual(result, ["task A", "task B"])

    def test_single_quotes(self):
        result = laintas_cli._parse_subtask_json("['task A', 'task B']")
        self.assertEqual(result, ["task A", "task B"])

    def test_trailing_comma(self):
        result = laintas_cli._parse_subtask_json('["task A", "task B",]')
        self.assertEqual(result, ["task A", "task B"])

    def test_no_array_returns_none(self):
        self.assertIsNone(laintas_cli._parse_subtask_json("no json here"))

    def test_empty_array_returns_empty_list(self):
        """Empty array is valid JSON - caller filters it out via len() >= 2 check."""
        result = laintas_cli._parse_subtask_json("[]")
        self.assertEqual(result, [])

    def test_non_string_elements_returns_none(self):
        self.assertIsNone(laintas_cli._parse_subtask_json("[1, 2, 3]"))

    def test_last_resort_quoted_strings(self):
        text = 'I think "update the auth module" and "fix the API" work'
        result = laintas_cli._parse_subtask_json(text)
        self.assertEqual(result, ["update the auth module", "fix the API"])

    def test_three_subtasks(self):
        text = '["run tests", "fix bugs", "commit changes"]'
        result = laintas_cli._parse_subtask_json(text)
        self.assertEqual(result, ["run tests", "fix bugs", "commit changes"])


class ShortestUniqueTests(unittest.TestCase):
    def test_single_path_returns_basename(self):
        result = agent_loop._shortest_unique(["src/router.py"])
        self.assertEqual(result, ["router.py"])

    def test_unique_basenames(self):
        result = agent_loop._shortest_unique(["src/router.py", "lib/api.py"])
        self.assertEqual(result, ["router.py", "api.py"])

    def test_same_basename_different_dirs(self):
        result = agent_loop._shortest_unique([
            "agent_gateway/router.py", "gateway/router.py"])
        self.assertEqual(result, ["agent_gateway/router.py", "gateway/router.py"])

    def test_three_same_basename(self):
        result = agent_loop._shortest_unique([
            "a/router.py", "b/router.py", "c/router.py"])
        self.assertEqual(result,
                         ["a/router.py", "b/router.py", "c/router.py"])

    def test_mixed_unique_and_dup(self):
        result = agent_loop._shortest_unique([
            "a/router.py", "b/router.py", "api.py"])
        self.assertEqual(result, ["a/router.py", "b/router.py", "api.py"])

    def test_empty_list(self):
        self.assertEqual(agent_loop._shortest_unique([]), [])

    def test_trailing_slash_stripped(self):
        result = agent_loop._shortest_unique(["src/module/", "lib/module/"])
        self.assertEqual(result, ["src/module", "lib/module"])

    def test_backslash_normalized(self):
        result = agent_loop._shortest_unique(["a\\router.py", "b\\router.py"])
        self.assertEqual(result, ["a/router.py", "b/router.py"])


class SalientArgTests(unittest.TestCase):
    def test_task_complete_returns_summary(self):
        result = agent_loop._salient_arg("task.complete", {
            "summary": "Fixed the memory leak in gateway.py"
        })
        self.assertEqual(result, "Fixed the memory leak in gateway.py")

    def test_task_complete_truncates_long_summary(self):
        long_summary = "A" * 200
        result = agent_loop._salient_arg("task.complete", {
            "summary": long_summary
        })
        self.assertEqual(len(result), 120)

    def test_task_complete_empty_summary(self):
        result = agent_loop._salient_arg("task.complete", {})
        self.assertEqual(result, "")

    def test_shell_exec_returns_command(self):
        result = agent_loop._salient_arg("shell.exec", {
            "command": "python -m pytest"
        })
        self.assertEqual(result, "python -m pytest")


class CompactToolLineTests(unittest.TestCase):
    def test_shell_exec_uses_tail_truncate(self):
        """Shell commands should not be middle-cropped (hint_middle=False)."""
        long_cmd = "python -m pytest tests/test_gateway.py -v --tb=short -x " + "arg " * 30
        name, hint, meta = agent_loop._compact_tool_line(
            "shell.exec", long_cmd, "exit 0", width=60, hint_middle=False)
        # Tail-truncated: starts with "python", ends with ellipsis
        self.assertTrue(hint.startswith("python"))
        self.assertTrue(hint.endswith("…"))
        # Should NOT contain middle-crop marker in the middle of the command
        # (middle crop would put "…" between start and end fragments)

    def test_default_uses_middle_truncate(self):
        """Default behavior middle-crops for non-shell tools."""
        long_hint = "a" * 60 + "MIDDLE" + "b" * 60
        name, hint, meta = agent_loop._compact_tool_line(
            "fs.read", long_hint, "", width=40, hint_middle=True)
        self.assertIn("…", hint)
        # Middle crop keeps start and end
        self.assertTrue(hint.startswith("a"))

    def test_short_hint_unchanged(self):
        name, hint, meta = agent_loop._compact_tool_line(
            "shell.exec", "ls -la", "exit 0", width=80, hint_middle=False)
        self.assertEqual(hint, "ls -la")


class CommandHasCdPrefixTests(unittest.TestCase):
    def test_plain_command_no_prefix(self):
        self.assertFalse(tools._command_has_cd_prefix("ls -la"))

    def test_cd_with_and_ampersand(self):
        self.assertTrue(tools._command_has_cd_prefix("cd /tmp && ls -la"))

    def test_cd_with_semicolon(self):
        self.assertTrue(tools._command_has_cd_prefix("cd /tmp ; ls -la"))

    def test_cd_alone_no_separator(self):
        self.assertFalse(tools._command_has_cd_prefix("cd /tmp"))

    def test_leading_whitespace_stripped(self):
        self.assertTrue(tools._command_has_cd_prefix("  cd /tmp && ls"))

    def test_non_cd_command(self):
        self.assertFalse(tools._command_has_cd_prefix("echo hello"))

    def test_command_starting_with_cd_substring(self):
        self.assertFalse(tools._command_has_cd_prefix("cdrecord -v file.iso"))


class ParseReadRangeTests(unittest.TestCase):
    def test_plain_path_no_at(self):
        path, start, end = agent_loop._parse_read_range("src/main.py")
        self.assertEqual(path, "src/main.py")
        self.assertEqual(start, 1)
        self.assertIsNone(end)

    def test_offset_only(self):
        path, start, end = agent_loop._parse_read_range("src/main.py@50")
        self.assertEqual(path, "src/main.py")
        self.assertEqual(start, 50)
        self.assertIsNone(end)

    def test_offset_and_limit(self):
        path, start, end = agent_loop._parse_read_range("src/main.py@50+100")
        self.assertEqual(path, "src/main.py")
        self.assertEqual(start, 50)
        self.assertEqual(end, 149)

    def test_offset_one_with_limit(self):
        path, start, end = agent_loop._parse_read_range("src/main.py@1+200")
        self.assertEqual(path, "src/main.py")
        self.assertEqual(start, 1)
        self.assertEqual(end, 200)

    def test_path_with_at_symbol(self):
        # rpartition ensures we split on the LAST @
        path, start, end = agent_loop._parse_read_range("weird@path.py@10+5")
        self.assertEqual(path, "weird@path.py")
        self.assertEqual(start, 10)
        self.assertEqual(end, 14)


class RangesOverlapTests(unittest.TestCase):
    def test_exact_same(self):
        self.assertTrue(agent_loop._ranges_overlap(1, 100, 1, 100))

    def test_subset(self):
        self.assertTrue(agent_loop._ranges_overlap(1, 200, 50, 100))

    def test_partial_overlap(self):
        self.assertTrue(agent_loop._ranges_overlap(1, 100, 50, 150))

    def test_no_overlap(self):
        self.assertFalse(agent_loop._ranges_overlap(1, 100, 101, 200))

    def test_adjacent_no_overlap(self):
        self.assertFalse(agent_loop._ranges_overlap(1, 100, 101, 200))

    def test_open_end_overlap(self):
        self.assertTrue(agent_loop._ranges_overlap(1, None, 50, 100))

    def test_open_end_no_overlap(self):
        self.assertFalse(agent_loop._ranges_overlap(101, 200, 1, 100))


if __name__ == "__main__":
    unittest.main()
