"""Locks for system bash-completion subcommand suggestions.

The probe runs the machine's own completion definition to suggest subcommands
(git che<TAB>, docker compose <TAB>). Four things must hold:

1. It never probes for a chat sentence — only when the first word is a real
   command — and never touches term0 (a subprocess in the cwd instead).
2. Sourcing a definition can execute cwd code (git core.fsmonitor, make
   $(shell ...)). While-typing auto-probe is gated on a trusted workspace; an
   explicit Tab always probes, matching bash.
3. The probe script passes the completion function its args in bash's order
   ($cmd, $cur, $prev), so git checkout offers refs, not git's verbs.
4. Results (including failures) are cached with a TTL so a slow definition is
   not re-run on every keystroke.
"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import laintas_cli
from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent

_PROBE = Path(laintas_cli.__file__).parent / "shell_probe" / "complete_probe.sh"
_HAS_BC = Path("/usr/share/bash-completion/bash_completion").is_file()


def _run_probe(*words):
    out = subprocess.run(["bash", "--norc", "--noprofile", str(_PROBE), *words, ""],
                         capture_output=True, text=True, timeout=5)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


class ProbeScriptTests(unittest.TestCase):
    def test_script_is_packaged(self):
        self.assertTrue(_PROBE.is_file(), "probe script missing next to laintas_cli")
        import json
        pm = json.load(open(Path(laintas_cli.__file__).parent / "package_manifest.json"))
        self.assertIn("shell_probe", pm["data_dirs"])

    @unittest.skipUnless(_HAS_BC, "system bash-completion not installed")
    def test_git_subcommands(self):
        subs = _run_probe("git")
        self.assertIn("add", subs)
        self.assertIn("checkout", subs)

    @unittest.skipUnless(_HAS_BC, "system bash-completion not installed")
    def test_git_checkout_offers_refs_not_verbs(self):
        # Correct arg order: the wrapper must see the checkout context, so it
        # offers refs (HEAD, branches), never git's top-level verbs.
        refs = _run_probe("git", "checkout")
        self.assertIn("HEAD", refs)
        self.assertNotIn("archive", refs)

    def test_unknown_command_is_silent(self):
        self.assertEqual(_run_probe("no_such_cmd_xyz"), [])


class ProbeGatingTests(unittest.TestCase):
    def setUp(self):
        self.completer = laintas_cli.MetaCompleter()
        laintas_cli._subcmd_cache.clear()

    def _complete(self, text, *, tab, probe):
        with mock.patch.object(laintas_cli, "_probe_shell_subcommands",
                               side_effect=lambda w: probe.append(w) or []), \
             mock.patch.object(laintas_cli, "get_runtime_config",
                               lambda k: True if k == "shell_command_completion" else None):
            list(self.completer.get_completions(
                Document(text),
                CompleteEvent(completion_requested=tab, text_inserted=not tab)))

    def test_chat_sentence_never_probes(self):
        probe = []
        self._complete("please fix the login ", tab=False, probe=probe)
        self.assertEqual(probe, [])

    def test_real_command_probes_on_tab(self):
        # Tab always probes a real command (trust gate applies only to
        # while-typing); the context excludes the trailing empty fragment.
        probe = []
        with mock.patch.object(laintas_cli.shutil, "which", lambda c: "/usr/bin/git"):
            self._complete("git ", tab=True, probe=probe)
        self.assertEqual(probe, [["git"]])


class TrustGateTests(unittest.TestCase):
    """Only a trusted workspace auto-probes cwd-code commands while typing."""

    def setUp(self):
        self.completer = laintas_cli.MetaCompleter()
        laintas_cli._subcmd_cache.clear()

    def _ran(self, *, tab, trusted):
        ran = []
        with mock.patch.object(laintas_cli.shutil, "which", lambda c: "/usr/bin/git"), \
             mock.patch.object(laintas_cli.trust_store, "project_status",
                               lambda *a, **k: {"trusted": trusted}), \
             mock.patch.object(laintas_cli, "get_runtime_config",
                               lambda k: True if k == "shell_command_completion" else None), \
             mock.patch.object(laintas_cli, "_probe_shell_subcommands",
                               side_effect=lambda w: ran.append(w) or []):
            laintas_cli._subcmd_cache.clear()
            list(self.completer.get_completions(
                Document("git add "),
                CompleteEvent(completion_requested=tab, text_inserted=not tab)))
        return bool(ran)

    def test_typing_untrusted_does_not_probe(self):
        self.assertFalse(self._ran(tab=False, trusted=False))

    def test_typing_trusted_probes(self):
        self.assertTrue(self._ran(tab=False, trusted=True))

    def test_tab_always_probes(self):
        self.assertTrue(self._ran(tab=True, trusted=False))


class CacheTests(unittest.TestCase):
    def setUp(self):
        laintas_cli._subcmd_cache.clear()

    def test_failure_is_cached_no_reprobe(self):
        calls = []

        def slow_bash(*a, **k):
            calls.append(1)
            raise subprocess.TimeoutExpired(cmd="bash", timeout=0.8)

        with mock.patch.object(laintas_cli.subprocess, "run", side_effect=slow_bash), \
             mock.patch.object(laintas_cli.Path, "is_file", lambda self: True):
            first = laintas_cli._probe_shell_subcommands(["systemctl", "restart"])
            second = laintas_cli._probe_shell_subcommands(["systemctl", "restart"])
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(len(calls), 1, "timed-out probe was re-run instead of cached")


if __name__ == "__main__":
    unittest.main()
