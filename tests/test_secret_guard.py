"""Credential gate behavior, persistence and network boundaries. Synthetic data only."""
import io
import json
from pathlib import Path
from unittest import mock

import pytest
import secret_guard as guard

CASES = json.loads((Path(__file__).parent / 'fixtures/secret-inputs.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: str(CASES.index(case)))
def test_offline_corpus(case):
    with mock.patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden')):
        assert bool(guard.input_error(case['text'])) == case['blocked']


def test_fail_closed_without_exposing_input_or_exception():
    with mock.patch.object(guard, '_detectors', side_effect=RuntimeError('private diagnostic')):
        assert guard.input_error('Ordinary text') == guard.UNAVAILABLE
    assert guard.input_error('a' * (guard.MAX_TEXT + 1)) == guard.UNAVAILABLE


def test_nested_tool_values_and_media():
    with pytest.raises(guard.SecretInputError, match='Message not sent'):
        guard.require_safe_payload([{'role': 'tool', 'content': {'password': 'DemoPass42'}}])
    guard.require_safe_payload({'image_url': {'url': 'data:image/png;base64,' + 'x' * 1000100}})


def test_history_never_persists_blocked_input(tmp_path):
    import laintas_cli as cli
    path = tmp_path / 'history'
    history = cli._PrivateCommandHistory(str(path))
    for case in CASES:
        history.append_string(case['text'])
    history.append_string('/password NeverStoreThis42')
    history.append_string('/model add openai sk-FAKEONLY0123456789')
    persisted = path.read_text()
    for case in CASES:
        if case['blocked']:
            assert case['text'] not in persisted
    assert 'NeverStoreThis42' not in persisted
    assert 'sk-FAKEONLY0123456789' not in persisted
    assert 'Please explain how password managers work.' in persisted


def test_backend_gate_runs_before_capture_or_transport():
    import laintas_cli as cli
    with mock.patch.object(cli, 'get_backend_profile') as transport:
        response = cli._call_backend_stream_impl({}, 'hello', 'safe system', '.',
            messages=[{'role': 'tool', 'content': 'password=DemoPass42'}])
    transport.assert_not_called()
    assert response['error'] is True
    assert response['code'] == 'sensitive_input'
    assert 'DemoPass42' not in json.dumps(response)


def test_execute_and_supplementary_inputs_do_not_start_work():
    import queue
    import laintas_cli as cli
    from rich.console import Console
    report = {}
    target = queue.Queue()
    output = io.StringIO()
    with mock.patch.object(cli, 'console', Console(file=output)), mock.patch.object(cli, '_sync_session_approval_from_mode') as start:
        assert cli._run_execute_mode('password=DemoPass42', {}, 0, report=report) == 1
        assert cli._queue_supplementary(target, 'password=DemoPass42') is False
    start.assert_not_called()
    assert target.empty()
    assert 'DemoPass42' not in output.getvalue()
    assert report['success'] is False


def test_agent_admission_and_remote_chat_stop_before_events():
    import agent_loop
    import laintas_cli as cli
    result = agent_loop.run_agent_loop(None, 'password=DemoPass42', {}, {}, [])
    assert result['success'] is False
    assert result['exit_reason'] == 'sensitive_input'
    assert agent_loop.queue_primary_message('unused', 'password=DemoPass42')[0] is False
    remote = mock.Mock()
    cli.AgentRegistry._handle_chat(remote, 'req', {'message': 'password=DemoPass42'}, None, None)
    remote._push_final.assert_called_once_with('req', 'fail', guard.BLOCKED)


def test_agents_mode_rejects_before_dispatch():
    import agents_mode
    controller = mock.Mock()
    agents_mode.AgentsModeController.dispatch(controller, 'password=DemoPass42')
    assert controller.notice == guard.BLOCKED
    controller.invalidate.assert_called_once_with()
    controller.resolve_agent.assert_not_called()


def test_acp_rejects_before_history_or_worker():
    import asyncio
    import acp_agent
    if acp_agent._ACP_IMPORT_ERROR is not None:
        pytest.skip('optional ACP SDK unavailable')
    from types import SimpleNamespace
    agent = acp_agent.LaintasAcpAgent({}, depth=0)
    state = acp_agent._AcpSession('security-test', '.')
    agent._sessions['security-test'] = state
    with pytest.raises(acp_agent.RequestError):
        asyncio.run(agent.prompt('security-test', [SimpleNamespace(text='password=DemoPass42')]))
    assert state.chat_history == []
    assert state.worker is None
