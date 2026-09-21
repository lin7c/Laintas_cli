"""Locks for interactive-input detection and direct-command routing.

1. Prompt detection only counts explicit prompts. "Line ends with a colon"
   also matched `Running migrations:` / `Building wheels for collected
   packages:`, so a quiet-but-working command was killed mid-migration.
2. Detection in _marker_poll_exec is opt-in (answer_fn). Callers without one
   (/bash, the post-turn `cd` sync) used to get an early return that left the
   prompt holding term0 — the next command was typed into it as the answer.
3. A credential answer is never echoed and never goes through
   _queue_supplementary ("Queued instruction: <password>").
4. is_system_command: shell-shaped arguments make a line a command even
   when it opens with an everyday word ("kill -9 1234 5678").
5. Interpreter passthrough: only eval/print flags leave the tty; a script may
   read the keyboard ("python manage.py createsuperuser").
"""
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock

import laintas_cli
import tools


class PromptDetectorTests(unittest.TestCase):
    def test_progress_headers_are_not_prompts(self):
        for line in ("Running migrations:",
                     "Building wheels for collected packages:",
                     "Downloading model weights:",
                     "Traceback (most recent call last):",
                     "Compiling crates:"):
            self.assertEqual(tools.question_shaped_tail(line), "", line)

    def test_real_prompts_are_detected(self):
        for line in ("[sudo] password for bob:",
                     "Enter passphrase for key /root/.ssh/id_ed25519:",
                     "Do you want to continue? [Y/n]",
                     "New password:", "Full Name []:",
                     "Country Name (2 letter code) [AU]:",
                     "Username for 'https://github.com':",
                     "Are you sure you want to continue connecting "
                     "(yes/no/[fingerprint])?"):
            self.assertTrue(tools.question_shaped_tail(line), line)

    def test_only_the_last_line_counts(self):
        self.assertEqual(tools.question_shaped_tail(
            "Password:\nauthenticated, continuing"), "")

    def test_secret_prompts(self):
        self.assertTrue(tools.is_secret_prompt("[sudo] password for bob:"))
        self.assertTrue(tools.is_secret_prompt("Enter passphrase for key k:"))
        self.assertFalse(tools.is_secret_prompt("Full Name []:"))


class MarkerPollAnswerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.session = laintas_cli.InteractiveSession(
            laintas_cli.DEFAULT_SHELL, timeout=0, stream_output=False,
            persistent=True, cwd=self.tmp)
        self.session.start()
        time.sleep(0.1)
        self.session.read_output(timeout=0.1)
        self.addCleanup(self.session.close)
        patcher = mock.patch.object(tools, "_PROMPT_WAIT_SECONDS", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_answer_fn_feeds_the_prompt(self):
        asked = []
        result = laintas_cli._marker_poll_exec(
            self.session, 'read -r -p "Enter name: " n; echo "got-$n"',
            timeout=10, answer_fn=lambda p: asked.append(p) or "bob")
        self.assertEqual(asked, ["Enter name:"])
        self.assertIn("got-bob", result["stdout"])
        self.assertEqual(result["returncode"], 0)

    def test_no_answer_fn_keeps_idle_recovery(self):
        # Without answer_fn nothing returns early: the idle budget runs out
        # and the shell is reclaimed, so the next command runs normally
        # instead of being typed into the waiting `read`.
        result = laintas_cli._marker_poll_exec(
            self.session, 'read -r -p "Enter name: " n; echo "got-$n"',
            timeout=2)
        self.assertNotIn("waiting_for_input", result)
        self.assertIn("terminal recovered", result["stderr"])
        after = laintas_cli._marker_poll_exec(
            self.session, "echo __AFTER''__", timeout=5)
        self.assertIn("__AFTER__", after["stdout"])
        self.assertNotIn("got-", after["stdout"])

    def test_answer_none_interrupts_and_recovers(self):
        result = laintas_cli._marker_poll_exec(
            self.session, 'read -r -p "Enter name: " n; echo "got-$n"',
            timeout=10, answer_fn=lambda p: None)
        self.assertIn("interrupted", result["stderr"])
        after = laintas_cli._marker_poll_exec(
            self.session, "echo __AFTER''__", timeout=5)
        self.assertIn("__AFTER__", after["stdout"])


class SecretAnswerReaderTests(unittest.TestCase):
    """Drive the cbreak reader loop with scripted keys."""

    def _run_reader(self, mode, keys):
        Key = laintas_cli.terminal_arbiter.Key
        feed = [Key("text", text=c) for c in keys] + [Key("enter")]

        class _Term:
            interactive = True

            def read_key(self_inner, timeout=None):
                if feed:
                    return feed.pop(0)
                stop.set()
                return None

        class _Hold:
            def __enter__(self_inner):
                return _Term()

            def __exit__(self_inner, *a):
                return False

        stop = threading.Event()
        q = queue.Queue()
        writes = []
        with mock.patch.object(laintas_cli.terminal_arbiter, "hold",
                               lambda *a, **k: _Hold()), \
                mock.patch.object(laintas_cli, "_bg_answer_mode", mode), \
                mock.patch.object(laintas_cli.sys.stdout, "write",
                                  side_effect=writes.append), \
                mock.patch.object(laintas_cli, "_queue_supplementary") as supp:
            laintas_cli._bg_reader_cbreak_mode(q, threading.Event(), stop)
        return q, "".join(writes), supp

    def test_secret_answer_is_not_echoed_or_queued_as_instruction(self):
        q, written, supp = self._run_reader("secret", "hunter2")
        self.assertEqual(q.get_nowait(), "hunter2")
        self.assertNotIn("hunter2", written)
        supp.assert_not_called()

    def test_plain_answer_goes_to_the_command_not_the_agent(self):
        q, written, supp = self._run_reader("plain", "bob")
        self.assertEqual(q.get_nowait(), "bob")
        self.assertIn("bob", written)
        supp.assert_not_called()


class CommandRoutingTests(unittest.TestCase):
    def test_everyday_commands_stay_commands(self):
        for cmd in ("kill -9 1234 5678", "history | grep ssh",
                    "source venv/bin/activate && pytest",
                    "time python train.py --epochs 3", "set -euo pipefail",
                    "sort data.txt > sorted.txt", "sleep 5 && ls",
                    "which python3 node npm", "find . -name '*.py'",
                    "kill %1", "help cd"):
            self.assertTrue(laintas_cli.is_system_command(cmd), cmd)

    def test_sentences_go_to_the_agent(self):
        for text in ("help me fix this bug", "time flies like an arrow",
                     "find a better way", "kill the old server process",
                     "which one is faster", "set up a new project"):
            self.assertFalse(laintas_cli.is_system_command(text), text)

    def test_interpreter_passthrough(self):
        cases = {"python": True, "python manage.py createsuperuser": True,
                 "python3 -i x.py": True, "python -u game.py": True,
                 "python -c 'print(1)'": False, "node -e 'x'": False,
                 "sudo vim /etc/hosts": True, "env A=1 B=2 vim": True,
                 "timeout 10s vim": True, "ls -la": False}
        for cmd, want in cases.items():
            self.assertEqual(laintas_cli._needs_tty_passthrough(cmd), want, cmd)


if __name__ == "__main__":
    unittest.main()
