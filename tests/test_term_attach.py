"""term_attach's stream rules and the guards that keep others off a busy term0."""
import threading
import unittest
from unittest import mock

import laintas_cli
import repl_mirror
import term_attach
import tools

INTEG = term_attach._Integration(
    nonce="n0nce", cmdfile="/dev/null",
    start=term_attach._seq("n0nce", "start"),
    prefix=b"\x1b]777;laintas;n0nce;")


def done(token, rc, cwd):
    return INTEG.done_for(token) + f"{rc};{cwd}".encode() + b"\x07"


def scan(chunks, token="tok"):
    sc = term_attach._Scanner(INTEG, token)
    shown = b"".join(sc.feed(c) for c in chunks)
    return sc, shown


STREAM = (b" __LAINTAS_TOK=tok; eval ...\r\n" + INTEG.start
          + b"hello\r\n\x1b[1mbold\x1b[0m\r\n" + done("tok", 3, "/a;b c")
          + b"PS1-prompt$ ")


class ScannerTests(unittest.TestCase):
    def test_echo_is_dropped_output_kept_prompt_dropped(self):
        sc, shown = scan([STREAM])
        self.assertEqual(b"hello\r\n\x1b[1mbold\x1b[0m\r\n", shown)
        self.assertTrue(sc.finished)
        self.assertEqual(3, sc.returncode)
        self.assertEqual("/a;b c", sc.cwd, "a ; in the directory survives")
        self.assertEqual("hello\nbold", sc.text())

    def test_every_split_point_gives_the_same_screen(self):
        _, whole = scan([STREAM])
        for cut in range(1, len(STREAM)):
            for second in (cut, min(len(STREAM), cut + 7)):
                parts = [STREAM[:cut], STREAM[cut:second], STREAM[second:]]
                sc, shown = scan(parts)
                self.assertEqual(whole, shown, f"split at {cut}/{second}")
                self.assertTrue(sc.finished)

    def test_one_byte_at_a_time(self):
        sc, shown = scan([bytes([b]) for b in STREAM])
        self.assertEqual(scan([STREAM])[1], shown)
        self.assertEqual(3, sc.returncode)

    def test_another_commands_done_is_removed_not_obeyed(self):
        stream = (INTEG.start + b"a\r\n" + done("", 0, "/old")
                  + b"b\r\n" + done("tok", 0, "/new"))
        sc, shown = scan([stream])
        self.assertEqual(b"a\r\nb\r\n", shown)
        self.assertEqual("/new", sc.cwd)

    def test_a_done_from_another_shell_is_just_output(self):
        foreign = b"\x1b]777;laintas;other;done;tok;0;/x\x07"
        sc, shown = scan([INTEG.start + foreign + b"still running"])
        self.assertFalse(sc.finished)
        self.assertIn(b"still running", shown)

    def test_our_done_without_a_start_still_finishes(self):
        sc, shown = scan([b"echo\r\n" + done("tok", 1, "/x")])
        self.assertTrue(sc.finished)
        self.assertEqual(b"", shown)

    def test_a_partial_marker_is_held_back(self):
        sc = term_attach._Scanner(INTEG, "tok", started=True)
        self.assertEqual(b"out", sc.feed(b"out\x1b]777;lain"))
        self.assertEqual(b"", sc.feed(b"tas;n0nce;done;tok;0;/"))
        self.assertEqual(b"", sc.feed(b"x\x07"))
        self.assertTrue(sc.finished)

    def test_an_unrelated_escape_is_not_held(self):
        sc = term_attach._Scanner(INTEG, "tok", started=True)
        self.assertEqual(b"\x1b]0;title\x07x", sc.feed(b"\x1b]0;title\x07x"))

    def test_fullscreen_is_noticed_and_summarised(self):
        sc, _ = scan([INTEG.start + b"\x1b[?1049hscreen\x1b[?1049l" + done("tok", 0, "/")])
        self.assertTrue(sc.fullscreen)
        res = term_attach.AttachResult("done", 0, "/", sc.text(), sc.fullscreen)
        self.assertEqual("(ran full-screen program `vim x`; exit 0)",
                         term_attach.summary_for_model(res, "vim x"))

    def test_modes_left_on_are_switched_off(self):
        sc = term_attach._Scanner(INTEG, "tok", started=True)
        sc.feed(b"\x1b[?1049h\x1b[?1000;1006hscreen")
        self.assertEqual(b"\x1b[?1000l\x1b[?1006l\x1b[?1049l", sc.modes_off())
        self.assertEqual(b"\x1b[?1000h\x1b[?1006h\x1b[?1049h", sc.modes_back_on())
        sc.feed(b"\x1b[?1006l\x1b[?1000l\x1b[?1049l")
        self.assertEqual(b"", sc.modes_off())

    def test_progress_bars_keep_their_last_state(self):
        self.assertEqual("100%\ndone", term_attach.clean_output(b"10%\r50%\r100%\r\ndone"))


class IntegrationLineTests(unittest.TestCase):
    def test_only_bash_and_zsh(self):
        self.assertEqual("bash", term_attach._shell_kind("/usr/bin/bash"))
        self.assertEqual("zsh", term_attach._shell_kind("-zsh"))
        self.assertEqual("", term_attach._shell_kind("/usr/bin/fish"))
        self.assertEqual("", term_attach.integration_line("", "n", "/f"))

    def test_an_unintegrable_shell_is_not_attached(self):
        session = mock.Mock(command="/usr/bin/fish", master_fd=5,
                            command_lock=threading.RLock(),
                            output_lock=threading.RLock(), _laintas_fg_job=None)
        session.is_alive.return_value = True
        self.assertIsNone(term_attach.integration(session))
        self.assertIsNone(term_attach.run(session, "ls", hold=None))

    def test_the_line_chains_the_users_hooks(self):
        line = term_attach.integration_line("bash", "n", "/tmp/x y")
        self.assertIn("${PROMPT_COMMAND:+;$PROMPT_COMMAND}", line)
        self.assertIn('"${PROMPT_COMMAND[@]}"', line)
        self.assertIn("'/tmp/x y'", line)
        zsh = term_attach.integration_line("zsh", "n", "/f")
        self.assertIn("precmd_functions=(__laintas_done $precmd_functions)", zsh)

    def test_the_trigger_reports_completion_itself(self):
        line = term_attach.trigger("T")
        self.assertTrue(line.startswith(" __LAINTAS_PREV=$?;"), "$? must be saved first")
        self.assertTrue(line.endswith('eval "$__LAINTAS_CMD" && :; __laintas_done && :'))
        self.assertIn('__laintas_ret "$__LAINTAS_PREV" && :;', line)


class BusyTerm0Guards(unittest.TestCase):
    """While a detached command runs, nobody else types into term0."""

    def setUp(self):
        self.session = mock.Mock()
        self.session.is_alive.return_value = True
        self.session._laintas_fg_job = term_attach.DetachedJob(
            command="make", scanner=mock.Mock(), offset=0)
        self.session.command_lock = threading.RLock()

    def test_marker_poll_refuses(self):
        res = laintas_cli._marker_poll_exec(self.session, "cd /tmp")
        self.assertEqual(-1, res["returncode"])
        self.assertIn("make", res["stderr"])
        self.session.send_keys.assert_not_called()

    def test_stuck_shell_recovery_never_signals_it(self):
        with mock.patch("os.killpg") as killpg, mock.patch("os.kill") as kill:
            self.assertFalse(tools.recover_stuck_shell(self.session))
        killpg.assert_not_called()
        kill.assert_not_called()

    def test_agent_shell_exec_refuses(self):
        res = tools._exec_in_deployed_shell("ls", self.session, 10)
        self.assertFalse(res["ok"])
        self.assertIn("make", res["error"])
        self.session.send_keys.assert_not_called()


class _FinishedSession:
    """term0 whose detached command has already printed its done mark."""

    def __init__(self, command="sleep 5"):
        self.output_lock = threading.RLock()
        self.command_lock = threading.RLock()
        self._text = STREAM.decode()
        self.output_total = len(self._text)
        self.sent = []
        self._laintas_fg_job = term_attach.DetachedJob(
            command=command, scanner=term_attach._Scanner(INTEG, "tok"), offset=0)

    def is_alive(self):
        return True

    def output_from(self, offset):
        return self._text[offset:]

    def send_keys(self, text):
        self.sent.append(text)


class FinishedDetachedJob(unittest.TestCase):
    """A detached command that ended frees term0 at once, not at the next prompt."""

    def test_is_busy_settles_a_finished_job(self):
        session = _FinishedSession()
        self.assertFalse(term_attach.is_busy(session))
        self.assertIsNone(term_attach.detached_job(session))

    def test_result_is_kept_for_the_main_loop(self):
        session = _FinishedSession()
        term_attach.is_busy(session)          # settled by, say, an agent turn
        command, res = term_attach.take_finished(session)
        self.assertEqual("sleep 5", command)
        self.assertEqual(("done", 3, "/a;b c"), (res.status, res.returncode, res.cwd))
        self.assertIsNone(term_attach.take_finished(session))   # reported once

    def test_agent_shell_exec_is_not_refused(self):
        session = _FinishedSession()
        res = tools._exec_in_deployed_shell("ls", session, 0.3)
        self.assertNotIn("detached", str(res.get("error", "")))
        self.assertTrue(session.sent)

    def test_main_loop_reports_what_another_check_settled(self):
        session = _FinishedSession()
        term_attach.is_busy(session)
        info = mock.Mock(session=session)
        with mock.patch.object(laintas_cli, "get_terminal", return_value=info), \
                mock.patch.object(laintas_cli.console, "print") as printed, \
                mock.patch("os.chdir"):
            laintas_cli._report_detached_finish()
        self.assertIn("sleep 5", str(printed.call_args))
        self.assertIn("exit 3", str(printed.call_args))


class ParentCommandGuard(unittest.TestCase):
    """parent() types into term0 too, and must not while a detached program runs."""

    def test_parent_command_is_not_typed_into_a_busy_term0(self):
        import agent_loop
        session = mock.Mock()
        session.is_alive.return_value = True
        session._laintas_fg_job = term_attach.DetachedJob(
            command="vim notes.txt", scanner=mock.Mock(), offset=0)
        out = agent_loop._marker_poll_simple(session, "rm -rf build")
        self.assertIn("vim notes.txt", out)
        session.send_keys.assert_not_called()
        agent_loop._sync_cwd_from_session(session)
        session.send_keys.assert_not_called()


class MirrorOnly(unittest.TestCase):
    def test_records_only_while_recording(self):
        hub = repl_mirror.MirrorHub()
        hub.mirror_only("before\n", "a")
        self.assertEqual([], hub.read_lines("a"))
        hub.start_recording()
        hub.mirror_only("during\n", "a")
        self.assertEqual(["during"], hub.read_lines("a"))


if __name__ == "__main__":
    unittest.main()
