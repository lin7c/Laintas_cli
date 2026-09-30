import io
import os
import queue
import threading
import unittest
from contextlib import nullcontext
from unittest import mock
from types import SimpleNamespace

import pyte
from rich.text import Text

import agent_loop
import append_input
import laintas_cli
import repl_mirror
import terminal_arbiter


class AppendInputTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.console = append_input.AppendConsole(
            file=self.output, force_terminal=True, width=90, height=24,
            _environ={'TERM': 'xterm-256color'},
            theme=laintas_cli.LAINTAS_THEME)
        self.ui = append_input.AppendInput(
            self.console, lambda: ('glm-5.3-flash', 'AUTO'))
        self.assertTrue(self.ui.start())
        self.addCleanup(self.ui.close)

    def screen(self):
        self.ui._live.refresh()
        screen = pyte.Screen(90, 24)
        # A real tty's ONLCR maps LF to CRLF; StringIO does not.
        pyte.Stream(screen).feed(self.output.getvalue().replace('\n', '\r\n'))
        return [line.rstrip() for line in screen.display if line.strip()]

    def test_connected_rows_hide_preview_execution_and_logs(self):
        with self.ui.status(lambda: Text('reply preview\nexecuting shell command')):
            self.ui.set_draft('只分析，不要修改')
            self.console.print('tool output must wait')
            rows = self.screen()
        self.assertEqual(len(rows), 2, rows)
        self.assertRegex(rows[0], r'L[·›»] ─╮ Thinking… .*glm-5.3-flash · AUTO')
        self.assertEqual(rows[1], '    ╰─ append 只分析，不要修改')
        self.assertEqual(rows[0].index('╮'), rows[1].index('╰'))
        self.assertNotIn('tool output must wait', self.output.getvalue())
        self.ui.set_draft('')
        self.assertIn('tool output must wait', self.output.getvalue())

    def test_consumed_append_keeps_branch_and_preserves_next_draft(self):
        target = queue.Queue()
        self.ui.set_draft('追加第一条')
        self.ui.submit(target, '追加第一条')
        self.ui.set_draft('第二条草稿')
        self.console.print('deferred execution')
        self.ui.consume([target.get_nowait()])
        self.assertEqual(self.ui.draft_text(), '第二条草稿')
        rows = self.screen()
        self.assertEqual(rows[1:], ['    ├─ append 追加第一条',
                                   '    ╰─ append 第二条草稿'])
        self.assertEqual(self.ui.pending, [])
        self.assertNotIn('deferred execution', self.output.getvalue())
        self.ui.set_draft('')
        self.assertNotIn('deferred execution', self.output.getvalue())
        self.assertEqual(self.ui.close(), '')
        self.assertIn('deferred execution', self.output.getvalue())

    def test_each_submission_adds_branch_and_last_branch_changes_to_junction(self):
        target = queue.Queue()
        for text in ['内容1', '内容2', '内容3']:
            self.ui.set_draft(text)
            self.ui.submit(target, text)
        self.assertEqual(self.screen()[1:], [
            '    ├─ append 内容1', '    ├─ append 内容2', '    ╰─ append 内容3'])
        self.ui.set_draft('内容4')
        self.console.print('execution must stay hidden')
        rows = self.screen()
        self.assertEqual(rows[1:], [
            '    ├─ append 内容1', '    ├─ append 内容2',
            '    ├─ append 内容3', '    ╰─ append 内容4'])
        for row in rows[1:]:
            self.assertEqual(row.index('─'), rows[0].index('╮') + 1)
        self.assertNotIn('execution must stay hidden', self.output.getvalue())
        self.assertEqual([target.get_nowait() for _ in range(3)],
                         ['内容1', '内容2', '内容3'])
        self.ui.consume(['内容1', '内容2', '内容3'])
        self.assertEqual(self.ui.close(), '内容4')

    def test_tall_tree_keeps_newest_input_and_bottom_connector_visible(self):
        target = queue.Queue()
        for number in range(30):
            self.ui.submit(target, f'内容{number}')
        self.ui.set_draft('最新草稿')
        rows = self.screen()
        self.assertLessEqual(len(rows), 23)
        self.assertIn('earlier append', rows[1])
        self.assertEqual(rows[-1], '    ╰─ append 最新草稿')
        self.assertEqual(rows[-2], '    ├─ append 内容29')

    def test_full_queue_keeps_draft(self):
        target = queue.Queue(maxsize=1)
        target.put('existing')
        self.ui.set_draft('keep me')
        with self.assertRaises(queue.Full):
            self.ui.submit(target, 'keep me')
        self.assertEqual(self.ui.draft_text(), 'keep me')
        self.assertEqual(self.ui.pending, [])

    def test_tool_status_cannot_break_append_connector(self):
        self.ui.set_draft('继续检查')
        with agent_loop.activity_status(self.console, 'shell.run', 'npm test', delay=0):
            rows = self.screen()
        self.assertEqual(len(rows), 2)
        self.assertIn('Thinking…', rows[0])
        self.assertNotIn('npm test', '\n'.join(rows))
        self.assertNotIn('Running', '\n'.join(rows))

    def test_tool_preview_still_displays_without_append(self):
        with agent_loop.activity_status(self.console, 'shell.run', 'npm test', delay=0):
            rows = self.screen()
        self.assertIn('Running…', rows[0])
        self.assertEqual(rows[1], 'npm test')

    def test_pause_for_approval_preserves_draft_and_close_stops_refresh_thread(self):
        self.ui.set_draft('approval draft')
        live = self.ui._live
        worker = live._refresh_thread
        self.ui.pause()
        self.assertFalse(worker.is_alive())
        self.console.print('approval question')
        self.ui.resume()
        self.assertIn('append approval draft', self.screen()[-1])
        worker = self.ui._live._refresh_thread
        self.assertEqual(self.ui.close(), 'approval draft')
        self.assertFalse(worker.is_alive())
        self.assertIsNone(append_input.current())

    def test_paste_is_two_rows_and_crop_respects_terminal_width(self):
        self.console.width = 38
        self.ui.set_draft('第一行\n第二行\t' + '中文' * 40)
        rows = self.screen()
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(rows[1].startswith('    ╰─ append '))
        self.assertLessEqual(agent_loop._cell_len(rows[1]), 37)

    def test_live_draft_never_enters_agent_history(self):
        hub = repl_mirror.MirrorHub()
        hub.start_recording()
        self.console.file = repl_mirror.TeeFile(lambda: 'primary', hub)
        with mock.patch('sys.stdout', self.output):
            self.ui.set_draft('private unfinished draft')
            self.ui._live.refresh()
            self.assertEqual(hub.read_lines('primary'), [])
            self.console.print('tool result')
            self.ui.set_draft('')
            self.assertEqual(hub.read_lines('primary'), ['tool result'])

    def test_reader_resumes_draft_and_submits_paste_without_raw_echo(self):
        stop = threading.Event()
        target = queue.Queue()
        self.ui.set_draft('保留')
        feed = [terminal_arbiter.Key('paste', '追加\n内容'),
                terminal_arbiter.Key('enter')]

        class Term:
            interactive = True

            def read_key(self, timeout=None):
                if feed:
                    return feed.pop(0)
                stop.set()
                return None

        with mock.patch.object(laintas_cli.terminal_arbiter, 'hold',
                               return_value=nullcontext(Term())), \
                mock.patch.object(laintas_cli, 'console', self.console), \
                mock.patch.object(laintas_cli.sys.stdout, 'write') as raw:
            laintas_cli._bg_reader_cbreak_mode(target, threading.Event(), stop)
        self.assertEqual(target.get_nowait(), '保留追加\n内容')
        self.assertEqual(self.ui.pending, ['保留追加\n内容'])
        self.assertEqual(self.ui.draft_text(), '')
        raw.assert_not_called()

    def test_prompt_toolkit_submission_is_queued(self):
        stop = threading.Event()
        target = queue.Queue()

        def prompt(*args, **kwargs):
            if target.empty():
                return 'prompt submission'
            stop.set()
            return ''

        with mock.patch.object(laintas_cli, 'PromptSession') as session, \
                mock.patch.object(laintas_cli.terminal_arbiter, 'hold',
                                  return_value=nullcontext()), \
                mock.patch.object(laintas_cli, 'patch_stdout',
                                  return_value=nullcontext()), \
                mock.patch.object(laintas_cli, 'console', self.console):
            session.return_value.prompt.side_effect = prompt
            laintas_cli._bg_reader_prompt_mode(target, None, stop)
        self.assertEqual(target.get_nowait(), 'prompt submission')

    @unittest.skipUnless(hasattr(os, 'openpty'), 'requires PTY support')
    def test_real_terminal_keys_submit_without_interrupt_and_release_reader(self):
        master, slave = os.openpty()
        arbiter = terminal_arbiter.TerminalArbiter(slave)
        stop, interrupt = threading.Event(), threading.Event()
        target = queue.Queue()
        reader = threading.Thread(target=laintas_cli._bg_reader_cbreak_mode,
                                  args=(target, interrupt, stop))
        try:
            with mock.patch.object(laintas_cli.terminal_arbiter, 'hold', arbiter.hold), \
                    mock.patch.object(laintas_cli, 'console', self.console):
                reader.start()
                os.write(master, '中文 append\r'.encode())
                self.assertEqual(target.get(timeout=3), '中文 append')
                self.assertFalse(interrupt.is_set())
                stop.set()
                reader.join(timeout=2)
                self.assertFalse(reader.is_alive())
                self.assertEqual(arbiter.current_owner(), '')
        finally:
            stop.set()
            arbiter.shutdown()
            if reader.ident is not None:
                reader.join(timeout=2)
            if arbiter._reader_thread is not None:
                arbiter._reader_thread.join(timeout=2)
                self.assertFalse(arbiter._reader_thread.is_alive())
            os.close(master)
            os.close(slave)

    def test_foreground_run_restores_unconsumed_input_in_prompt_and_closes_live(self):
        self.ui.close()
        target = queue.Queue()
        seen = {}

        def loop(*args, **kwargs):
            ui = append_input.current()
            self.assertIsNotNone(ui)
            seen['worker'] = ui._live._refresh_thread
            ui.submit(kwargs['message_queue'], '未消费的追加')
            ui.set_draft('未提交的草稿')
            return {'success': True, 'msg': '', 'state': {}}

        with mock.patch.object(laintas_cli, 'console', self.console), \
                mock.patch.object(laintas_cli, 'get_current_agent', return_value=None), \
                mock.patch.object(laintas_cli, 'get_user_message_queue', return_value=target), \
                mock.patch.object(laintas_cli, '_start_bg_input_reader'), \
                mock.patch.object(laintas_cli, 'get_runtime_config', return_value=False), \
                mock.patch.object(laintas_cli, '_repl_process_depth', return_value=0), \
                mock.patch.object(laintas_cli.sys.stdin, 'isatty', return_value=True), \
                mock.patch.object(laintas_cli, '_pending_prompt_default', ''), \
                mock.patch.object(laintas_cli, 'run_agent_loop', side_effect=loop):
            laintas_cli._run_agent_loop_with_interrupt(
                SimpleNamespace(console=self.console), 'task', {}, {}, [],
                events_cb=lambda events: None)
            self.assertEqual(laintas_cli._pending_prompt_default,
                             '未消费的追加\n未提交的草稿')
        self.assertFalse(seen['worker'].is_alive())
        self.assertIsNone(append_input.current())


if __name__ == '__main__':
    unittest.main()
