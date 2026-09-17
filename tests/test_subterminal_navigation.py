import contextlib
import io
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

import laintas_cli as cli
from terminal_preview import TerminalPreview


class PreviewTests(unittest.TestCase):
    def test_repaints_replace_old_screen_and_preserve_literal_brackets(self):
        preview = TerminalPreview()
        text = "old\r\nprogress 10%\r\x1b[2Kprogress 100% [ok]"
        self.assertEqual(preview.render(text), "old\nprogress 100% [ok]")
        text += "\x1b[2J\x1b[H新界面\x1b]777;LAINTAS_DETACH\x07"
        self.assertEqual(preview.render(text), "新界面")

    def test_partial_escape_and_cursor_updates_are_incremental(self):
        preview = TerminalPreview()
        preview.render("before\x1b[")
        self.assertEqual(preview.render("before\x1b[2K\rafter"), "after")
        self.assertEqual(preview.render("fresh"), "fresh")

    def test_live_screen_is_bounded(self):
        preview = TerminalPreview()
        result = preview.render("\r\n".join(str(i) for i in range(500)), rows=10)
        self.assertEqual(result.splitlines(), [str(i) for i in range(490, 500)])


class NavigationTests(unittest.TestCase):
    def test_create_action_is_available_without_any_terminal(self):
        with mock.patch.object(cli.resource_ui, "ResourceBrowser") as browser, \
                mock.patch.object(cli, "get_all_terminals", return_value=[]):
            browser.return_value.run.return_value = cli.resource_ui.UIOutcome("cancel")
            cli.show_terminal_manager()
        action = next(a for a in browser.call_args.kwargs["actions"] if a.name == "new")
        self.assertEqual(action.key, "n")
        self.assertTrue(action.allow_empty)

    def test_new_terminal_returns_to_browser_and_avoids_existing_names(self):
        with mock.patch.object(cli, "_show_terminal_manager_once", side_effect=["new", None]), \
                mock.patch.object(cli, "get_terminal", side_effect=lambda n: object() if n == "term1" else None), \
                mock.patch.object(cli, "_cmd_term") as create:
            cli.show_terminal_manager(None, "registry")
        create.assert_called_once_with(["/term", "term2"], "registry", None)

    def test_nested_creation_keeps_parent_and_depth(self):
        registry = SimpleNamespace(terminal_meta={"name": "outer"}, depth=2, agent_id=None)
        with mock.patch.object(cli, "_REPL_PROCESS_DEPTH", 2), \
                mock.patch.object(cli, "get_terminal", return_value=None), \
                mock.patch.object(cli, "SubTerminalSession") as session, \
                mock.patch.object(cli, "register_terminal") as register, \
                mock.patch.object(cli.time, "sleep"), \
                mock.patch.object(cli, "console", Console(file=io.StringIO())):
            cli._cmd_term(["/term", "inner"], registry, None)
        command = shlex.split(session.call_args.args[0])
        self.assertEqual(command[command.index("--depth") + 1], "3")
        self.assertEqual(command[command.index("--parent-terminal") + 1], "outer")
        self.assertFalse(session.call_args.kwargs["use_tmux"])
        self.assertEqual(register.call_args.kwargs["parent_terminal"], "term0")

    def test_status_displays_child_identity_without_changing_shell_routing(self):
        agent = SimpleNamespace(name="primary", id="primary", base_model="")
        with mock.patch.object(cli, "_LOCAL_TERMINAL_NAME", "child"), \
                mock.patch.object(cli, "get_current_agent", return_value=agent), \
                mock.patch.object(cli, "_terminal_agents", SimpleNamespace(configured=False)), \
                mock.patch.object(cli, "agent_deployment_terminal", return_value="term0"), \
                mock.patch.object(cli, "get_terminal", return_value=None) as lookup, \
                mock.patch.object(cli, "get_selected_model", return_value="model"), \
                mock.patch.object(cli, "get_runtime_config", return_value=False), \
                mock.patch.object(cli, "_update_status_cache") as update:
            cli._sync_status_context()
        lookup.assert_called_once_with("term0")
        self.assertEqual(update.call_args.kwargs["terminal"], "child")


class PTYOwnershipTests(unittest.TestCase):
    def test_back_detaches_twice_while_background_scanner_is_running(self):
        # A real child PTY, including a marker split across separate reads.
        program = """import os, tty, time
tty.setraw(0)
os.write(1,b'READY')
while True:
    b=os.read(0,1)
    if b == b'B':
        os.write(1,b'\\x1b]777;LAINTAS_')
        time.sleep(.04)
        os.write(1,b'DETACH\\x07')
"""
        session = cli.InteractiveSession(
            f"{shlex.quote(sys.executable)} -u -c {shlex.quote(program)}")
        session.start()
        self.addCleanup(session.close)
        deadline = time.monotonic() + 3
        while "READY" not in session.full_output and time.monotonic() < deadline:
            session.read_output(.02)
        self.assertIn("READY", session.full_output)
        stop = threading.Event()

        def scan():
            while not stop.wait(.001):
                session.read_output(0)

        scanner = threading.Thread(target=scan)
        scanner.start()
        try:
            for _ in range(2):
                sent = False
                deadline = time.monotonic() + 3

                def read_bytes(timeout):
                    nonlocal sent
                    if not sent:
                        sent = True
                        return b"B"
                    if time.monotonic() >= deadline:
                        self.fail("detach marker was lost")
                    time.sleep(.01)
                    return None

                output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
                with mock.patch.object(cli.terminal_arbiter, "hold", return_value=
                                       contextlib.nullcontext(SimpleNamespace(read_bytes=read_bytes))), \
                        mock.patch.object(cli.sys, "stdout", output), \
                        mock.patch.object(cli, "console", Console(file=output)):
                    cli.enter_session(session, "child")
                    output.flush()
                    captured = output.buffer.getvalue().decode()
                self.assertIn("LAINTAS_DETACH", session.raw_output, captured)
                self.assertTrue(session.is_alive())
            self.assertEqual(session.raw_output.count("LAINTAS_DETACH"), 2)
        finally:
            stop.set()
            scanner.join(timeout=2)
            self.assertFalse(scanner.is_alive())
            session.close()
            self.assertFalse(session.is_alive())


class CLINavigationTests(unittest.TestCase):
    def test_create_enter_back_and_reenter_real_cli(self):
        import pyte
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="laintas-navigation-") as directory:
            master, slave = pty.openpty()
            termios.tcsetwinsize(slave, (42, 160))
            env = {**os.environ, "LAINTAS_HOME": directory + "/home",
                   "LAINTAS_BACKEND": "http://127.0.0.1:1", "TERM": "xterm-256color",
                   "LAINTAS_TERMINAL_ID": "navigation-test", "PYTHONDONTWRITEBYTECODE": "1"}
            env.pop("TMUX", None)
            process = subprocess.Popen(
                [sys.executable, str(repo / "laintas_cli.py"), "--depth", "1",
                 "--terminal-name", "outer"], cwd=directory, env=env,
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            screen = pyte.Screen(160, 42)
            stream = pyte.ByteStream(screen)
            transcript = bytearray()

            def wait_for(text, timeout=20):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], .1)
                    if ready:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        stream.feed(data)
                        transcript.extend(data)
                        if b"\x1b[6n" in data:
                            os.write(master, b"\x1b[1;1R")
                    if text in "\n".join(screen.display):
                        return
                    if process.poll() is not None:
                        break
                self.fail(f"Missing {text!r}:\n" + "\n".join(screen.display)
                          + "\nRecent raw output:\n" + repr(bytes(transcript[-16000:])))

            try:
                wait_for("primary@outer")
                os.write(master, b"/t\r")
                wait_for("New terminal")
                os.write(master, b"n")
                wait_for("term1")
                wait_for("New terminal")
                # The reopened list starts at the current terminal.
                os.write(master, b"\x1b[Be")
                wait_for("primary@term1")
                os.write(master, b"/back\r")
                wait_for("Returned to outer")
                wait_for("primary@outer")
                os.write(master, b"/t\r")
                wait_for("New terminal")
                os.write(master, b"\x1b[Be")
                wait_for("primary@term1")
                os.write(master, b"/back\r")
                wait_for("Returned to outer")
                wait_for("primary@outer")
            finally:
                process.send_signal(signal.SIGTERM) if process.poll() is None else None
                deadline = time.monotonic() + 10
                while process.poll() is None and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], .1)
                    if ready:
                        try:
                            os.read(master, 65536)
                        except OSError:
                            break
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
