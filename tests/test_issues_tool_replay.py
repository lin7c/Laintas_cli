import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console
from rich.markdown import Markdown

import agent_loop
import laintas_cli
import model_route
import paths
import task_manager
from issues_tool_replay import Journal, ReplayPending
from test_session_runtime import _chdir


class JournalTests(unittest.TestCase):
    def test_restart_replays_result_and_never_reexecutes_unknown_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'journal.sqlite3'
            first = Journal(path)
            self.assertIsNone(first.claim('ic_run_1_0','write',{'path':'a'}))
            with self.assertRaises(ReplayPending):
                Journal(path).claim('ic_run_1_0','write',{'path':'a'})
            row = {'output':'written','returncode':0,'tool':'write'}
            first.complete('ic_run_1_0',row)
            self.assertEqual(Journal(path).claim('ic_run_1_0','write',{'path':'a'}),row)
            with self.assertRaises(ReplayPending):
                first.claim('ic_run_1_0','write',{'path':'different'})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_provider_identity_survives_native_parser_and_dispatch_mapping(self):
        calls = laintas_cli._native_to_tool_calls({0:{'id':'ic_run_1_0','name':'time_now','arguments':'{}'}},
                                                  {'time_now':'time.now'})
        self.assertEqual(calls[0]['_source_call_id'],'ic_run_1_0')
        normalized = agent_loop._prepare_tool_call(calls[0],0,0)
        self.assertEqual(normalized['source_call_id'],'ic_run_1_0')
        self.assertEqual(normalized['call_id'],'call_01_01')


class ReplayLoopTests(unittest.TestCase):
    def setUp(self):
        task_manager.clear_session_tasks()
        agent_loop.reset_runtime_config()
        for key,value in [('auto_snapshot',False),('loop_delay',0),('use_message_thread',True),('mem_extract_on_compact',False)]:
            agent_loop.set_runtime_config(key,value)
        patcher = mock.patch.object(model_route,'active_model',return_value='')
        patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self):
        agent_loop.reset_runtime_config(); task_manager.clear_session_tasks()

    def test_replayed_gateway_call_runs_once_and_reports_original_identity(self):
        requests = []
        def backend(**kw):
            requests.append(kw)
            if len(requests) <= 2:
                return {'reply':'checking','tool_calls':[{'name':'time.now','arguments':{},'_source_call_id':'ic_test_run_2_0'}],
                        'finish_reason':'tool_calls','done':False,'error':False}
            return {'reply':'finished','tool_calls':[],'finish_reason':'stop','done':False,'error':False}
        deps = agent_loop.LoopDeps(read_file=lambda _:None,append_file=lambda *_:None,write_file=lambda *_:None,
            strip_ansi=lambda x:x,generate_prompt=lambda:'You are a test agent.',call_backend=backend,
            SubTerminalSession=mock.Mock,display_command_output=lambda *a,**k:None,
            display_sub_terminal_preview=lambda *a,**k:None,display_file_diff=lambda *a,**k:None,
            console=Console(file=io.StringIO(),force_terminal=False),Markdown=Markdown)
        registry = agent_loop.tools_mod.get_registry()
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp), \
                mock.patch.object(paths,'SESSIONS_DIR',Path(tmp)/'sessions'), \
                mock.patch.object(registry,'invoke',wraps=registry.invoke) as invoke:
            Path('.laintas').mkdir()
            agent_loop.run_agent_loop(deps,'what time is it',{}, {'_model_override':'glm-5.3'},
                                      [{'role':'user','content':'what time is it'}],max_loops_override=4)
            self.assertEqual(sum(c.args[0]=='time.now' for c in invoke.call_args_list),1)
            self.assertGreaterEqual(len(requests),3)
            for request in requests[1:3]:
                tool_results = [m for m in request['messages'] if m.get('role')=='tool']
                self.assertTrue(tool_results)
                self.assertTrue(all(m['tool_call_id']=='ic_test_run_2_0' for m in tool_results))
