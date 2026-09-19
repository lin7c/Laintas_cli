"""Slash-command argument contracts, handler edge cases and argument completion.

Each case here was a shipped bug: an extra word silently ignored, a documented
form rejected, a mistyped subcommand that did something else, or an argument
the completer knew nothing about.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

from rich.console import Console

import agent_loop
import laintas_cli


def _validate(command):
    action, _raw, parts = laintas_cli._parse_slash_command(command)
    laintas_cli._validate_slash_args(action, parts[1:])


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


class ArgumentContractTests(unittest.TestCase):
    def test_documented_forms_are_accepted(self):
        for command in (
                "/prop", "/prop sys", "/prop 2", "/prop sys 2",
                "/extensions create demo --desc 'a demo'",
                "/extensions pack demo --output demo.lext",
                "/usage 7d local", "/usage buy calls",
                "/helpwo --port 8080 --host ::1 --dist ./dist --remote",
                "/shared rm old --yes", "/windows install --force",
                "/web cookies clear example.com", "/identity capture work a.com,b.com",
                "/evolve activate cand-1 --force", "/prompt status"):
            with self.subTest(command=command):
                _validate(command)

    def test_extra_words_are_rejected(self):
        for command in (
                "/prop sys 2 3", "/usage buy calls now", "/img list extra",
                "/retask help me",
                "/handoff list mine", "/shared ls a b", "/shared delete a b",
                "/windows stop now", "/windows start write now",
                "/web status now", "/web test google bing",
                "/identity delete a b", "/why 1 2", "/hwo status run1 run2", "/evolve status now",
                "/prompt branches all", "/task mine now", "/task agent a b",
                "/mode auto now", "/mode delete a b", "/policy status now"):
            with self.subTest(command=command):
                with self.assertRaises(laintas_cli.SlashCommandUsageError):
                    _validate(command)


class HandlerEdgeCaseTests(unittest.TestCase):
    def test_usage_rejects_a_repeated_range(self):
        with self.assertRaises(laintas_cli.SlashCommandUsageError):
            laintas_cli._show_usage_command(["7d", "90d"], {})

    def test_img_text_flag_is_matched_as_a_word(self):
        with mock.patch("vision.image_to_text",
                        return_value={"text": "ok"}) as ocr, \
                mock.patch.object(laintas_cli, "load_session", return_value={}), \
                mock.patch.object(laintas_cli, "_gateway_post_json_for_cli"), \
                _Capture():
            laintas_cli._cmd_img("shot--text.png --text --ocr")
        self.assertEqual(ocr.call_args.args[0], "shot--text.png")

    def test_mode_act_only_takes_always(self):
        with _Capture() as out, \
                mock.patch.object(laintas_cli.mode_manager, "activate") as activate:
            laintas_cli._cmd_mode("act alwyas", ["/mode", "act", "alwyas"])
        activate.assert_not_called()
        self.assertIn("Usage: /mode act", out.text)

    def test_policy_typo_is_not_reported_as_status(self):
        with _Capture() as out, mock.patch("sys.stdin.isatty", return_value=False):
            laintas_cli._cmd_policy(["/policy", "enfroce"])
        self.assertIn("Usage: /policy", out.text)
        self.assertNotIn("Allow rules", out.text)

    def test_hwo_verb_without_file_does_not_open_the_editor(self):
        for verb in ("run", "compile", "view"):
            with self.subTest(verb=verb), _Capture() as out, \
                    mock.patch.object(laintas_cli.hwo_ui_mod, "run_hwo_ui") as editor:
                laintas_cli._cmd_hwo(["/hwo", verb], {})
            editor.assert_not_called()
            self.assertIn(f"Usage: /hwo {verb}", out.text)

    def test_helpwo_refuses_an_unknown_word(self):
        registry = mock.Mock(workspace_path=None, agent_id=None)
        with _Capture() as out, \
                mock.patch("helpwo_server.is_running", return_value=False), \
                mock.patch("helpwo_server.start_server") as start:
            laintas_cli._cmd_helpwo("stpo", ["/helpwo", "stpo"], registry, {})
        start.assert_not_called()
        self.assertIn("Unexpected argument", out.text)

    def test_web_engines_refuses_anything_but_init(self):
        with _Capture() as out:
            laintas_cli._web_engines(["/web", "engines", "list"])
        self.assertIn("Usage: /web engines", out.text)

    def test_told_rejects_extra_and_non_numeric_counts(self):
        for parts in (["/told", "3", "x"], ["/told", "reply", "many"],
                      ["/told", "all", "now"]):
            with self.subTest(parts=parts), _Capture() as out:
                laintas_cli._cmd_told(parts)
            self.assertIn("Usage: /told", out.text)

    def test_enterprise_takes_one_target(self):
        with _Capture() as out, \
                mock.patch("enterprise_installer.install_extension") as install:
            laintas_cli._handle_enterprise(["on", "off"])
        install.assert_not_called()
        self.assertIn("Usage: /v enterprise", out.text)

    def test_extensions_create_checks_its_flag(self):
        with _Capture() as out, \
                mock.patch("extension_manager.ExtensionManager") as manager:
            laintas_cli._cmd_extensions(
                ["/extensions", "create", "demo", "--description", "x"], {})
        manager.return_value.create.assert_not_called()
        self.assertIn("Usage: /extensions create", out.text)

    def test_debug_export_rejects_unknown_words(self):
        with _Capture() as out:
            laintas_cli._cmd_debug(["/debug", "3", "out.txt", "junk"])
        self.assertIn("unexpected argument", out.text)


class ArgumentCompletionTests(unittest.TestCase):
    def setUp(self):
        laintas_cli._ARG_COMPLETION_CACHE.clear()

    @staticmethod
    def _complete(text):
        return [item.text.rstrip() for item in laintas_cli.MetaCompleter().get_completions(
            laintas_cli.Document(text, len(text)),
            mock.Mock(completion_requested=True))]

    def test_static_second_level_values(self):
        self.assertEqual(self._complete("/usage buy "), ["calls", "storage"])
        self.assertEqual(self._complete("/windows start "), ["read", "write"])
        self.assertEqual(self._complete("/v enterprise "), ["on", "off", "gateway"])
        self.assertEqual(self._complete("/policy disabled "), ["--yes"])
        self.assertIn("--plain", self._complete("/agents "))
        self.assertEqual(self._complete("/prop "), ["sys", "budget"])

    def test_help_completes_command_names(self):
        self.assertIn("model", self._complete("/help mod"))
        self.assertIn("/model", self._complete("/help /mod"))

    def test_terminal_and_agent_names_come_from_the_registries(self):
        agent_loop.close_all_agents()
        try:
            agent = agent_loop.register_agent(name="alice", depth=1, role="pool")
            terminal = mock.Mock(stationed_agent_id=None)
            terminal.name = "build"
            terminal.session.is_alive.return_value = True
            with mock.patch.object(laintas_cli, "get_all_terminals",
                                   return_value=[terminal]):
                self.assertEqual(self._complete("/terminate "), ["build"])
                self.assertEqual(self._complete("/term rename b"), ["build"])
            self.assertIn(agent.id, self._complete("/abort "))
        finally:
            agent_loop.close_all_agents()

    def test_hire_flags_and_their_values(self):
        self.assertEqual(
            self._complete("/hire bob --"),
            ["--profile", "--prompt", "--tools", "--model", "--terminal"])
        self.assertNotIn("--tools", self._complete("/hire bob --tools inherit --"))
        self.assertIn("reviewer", self._complete("/hire bob --profile rev"))

    def test_file_arguments_filter_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("flow.hwo", "graph.hwg", "notes.txt"):
                open(os.path.join(tmp, name), "w").close()
            os.mkdir(os.path.join(tmp, "nested"))
            old = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(self._complete("/hwo run "), ["flow.hwo", "nested/"])
                self.assertEqual(self._complete("/hwg compile "), ["graph.hwg", "nested/"])
            finally:
                os.chdir(old)

    def test_model_ids_never_fetch_outside_the_live_prompt(self):
        with mock.patch.object(laintas_cli, "_prompt_session", None), \
                mock.patch.object(laintas_cli, "_rprompt_model_cache", []), \
                mock.patch.object(laintas_cli, "_rprompt_kick_model_fetch") as kick:
            self.assertEqual(self._complete("/model aux "), ["reset"])
        kick.assert_not_called()

    def test_unrelated_words_still_complete_to_nothing(self):
        self.assertEqual(self._complete("/task unrelated"), [])
        self.assertEqual(self._complete("/debug clear "), [])


class ExtensionDynamicSubcommandTests(unittest.TestCase):
    def test_a_callable_child_list_is_resolved_when_completing(self):
        import extension_runtime
        runtime = extension_runtime.ExtensionRuntime()
        runtime.register_command("demo", "/demo", lambda *args: None, subcommands=[
            ("pick", "choose one", lambda: [("alpha", "first")]),
            ("broken", "provider fails", lambda: 1 / 0),
            ("nested", "static", [("leaf", "fixed")]),
        ])
        self.assertEqual(runtime.command_subcommands("/demo"),
                         [("pick", "choose one"), ("broken", "provider fails"),
                          ("nested", "static")])
        self.assertEqual(runtime.command_subcommands_at("/demo", "pick"),
                         [("alpha", "first")])
        self.assertEqual(runtime.command_subcommands_at("/demo", "broken"), [])
        self.assertEqual(runtime.command_subcommands_at("/demo", "nested"),
                         [("leaf", "fixed")])


if __name__ == "__main__":
    unittest.main()
