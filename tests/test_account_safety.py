"""Profile binding and account-switch failures must preserve ownership."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import account_store
import agent_loop
import json_store
import laintas_cli as cli
import paths
import peer_coordination
import terminal_link
import windows_host


def isolated_python(tmp_path, code, *, account='', mode=''):
    env = dict(os.environ, LAINTAS_HOME=str(tmp_path / 'home'), HOME=str(tmp_path),
               LAINTAS_ACCOUNT_ID=account, LAINTAS_ACCOUNT_MODE=mode,
               PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', '-c', code], env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.parametrize('operation', [
    'paths.ensure_home()',
    'import cookie_store',
    'import child_registry',
    'import identity_store',
    'import official_messages',
    'import usage_tracker; usage_tracker._usage_dir()',
    'json_store.save_json_atomic(paths.CONFIG_FILE,{"x":1})',
])
def test_unselected_runtime_cannot_write_or_cache_user_paths(tmp_path, operation):
    isolated_python(tmp_path, f'''
import paths,json_store
try:
    {operation}
except paths.AccountSelectionError: pass
else: raise AssertionError('uninitialized profile admitted')
assert not paths.ROOT_HOME.exists()
''')


def test_profile_binding_refuses_late_hot_swap(tmp_path):
    isolated_python(tmp_path, '''
import paths,account_store,json_store
paths.configure_account('A')
import cookie_store,child_registry,identity_store,official_messages
home=account_store.profile_dir(paths.ROOT_HOME,'A')
assert cookie_store.COOKIE_FILE == home/'cookies.json'
assert child_registry.RUN_DIR == home/'run'
assert identity_store.IDENTITY_DIR == home/'identities'
assert official_messages.CACHE_FILE == home/'messages.json'
paths.configure_account('A')
try: paths.configure_account('B')
except paths.AccountSelectionError: pass
else: raise AssertionError('cached profile was changed')
assert paths.LAINTAS_HOME == home and paths.ACCOUNT_USER_ID == 'A'
json_store.save_json_atomic(paths.CONFIG_FILE,{'owner':'A'})
assert paths.CONFIG_FILE.exists()
for target in [paths.ROOT_HOME/'config.json',account_store.profile_dir(paths.ROOT_HOME,'B')/'config.json',
               home/'..'/account_store.profile_dir(paths.ROOT_HOME,'B').name/'config.json']:
    try: json_store.save_json_atomic(target,{'owner':'wrong'})
    except paths.AccountSelectionError: pass
    else: raise AssertionError('foreign store write admitted')
    assert not target.exists()
foreign=account_store.profile_dir(paths.ROOT_HOME,'B')
foreign.mkdir(parents=True)
(home/'escape').symlink_to(foreign,target_is_directory=True)
try: json_store.save_json_atomic(home/'escape'/'config.json',{'owner':'wrong'})
except paths.AccountSelectionError: pass
else: raise AssertionError('profile symlink escape admitted')
assert not (foreign/'config.json').exists()
''')


def test_explicit_anonymous_backend_can_bind_stores(tmp_path):
    isolated_python(tmp_path, '''
import paths
paths.configure_account('')
import cookie_store
paths.ensure_home()
assert cookie_store.COOKIE_FILE == paths.ROOT_HOME/'cookies.json'
''')


def test_embedded_environment_selects_verified_profile(tmp_path):
    isolated_python(tmp_path, '''
import os,account_store
from pathlib import Path
root=Path(os.environ['LAINTAS_HOME'])
account_store.remember(root,{'userId':'A','token':'token'})
os.environ['LAINTAS_ACCOUNT_ID']='A'
import paths,cookie_store
assert paths.ACCOUNT_USER_ID == 'A'
assert cookie_store.COOKIE_FILE == account_store.profile_dir(root,'A')/'cookies.json'
''')


def test_cli_explicit_account_takes_precedence_over_stale_environment(tmp_path):
    isolated_python(tmp_path, '''
import os,sys,account_store
from pathlib import Path
root=Path(os.environ['LAINTAS_HOME'])
account_store.remember(root,{'userId':'B','token':'token'})
os.environ['LAINTAS_ACCOUNT_ID']='missing-account'
sys.argv=['laintas-cli','--account','B']
import laintas_cli,paths,cookie_store
assert paths.ACCOUNT_USER_ID == 'B'
assert cookie_store.COOKIE_FILE == account_store.profile_dir(root,'B')/'cookies.json'
''')


@pytest.mark.parametrize('flag', ['--help', '--version'])
def test_help_and_version_do_not_require_a_valid_inherited_account(tmp_path, flag):
    env = dict(os.environ, LAINTAS_HOME=str(tmp_path / 'home'), HOME=str(tmp_path),
               LAINTAS_ACCOUNT_ID='missing-account', LAINTAS_ACCOUNT_MODE='',
               PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', str(Path(cli.__file__)), flag],
                            env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout
    assert not (tmp_path / 'home').exists()


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    for uid in ('A', 'B', 'C'):
        account_store.remember(tmp_path, {'userId': uid, 'token': 'test-' + uid})
    monkeypatch.setattr(paths, 'ROOT_HOME', tmp_path)
    monkeypatch.setattr(paths, 'TERMINAL_ID', 'test-terminal')
    monkeypatch.setenv('LAINTAS_ACCOUNT_ID', 'A')
    account_store.select(tmp_path, paths.TERMINAL_ID, 'A')
    yield tmp_path
    account_store.cancel_transition()


def test_failed_second_selection_write_restores_exact_prior_selection(accounts):
    first = account_store.select(accounts, paths.TERMINAL_ID, 'A')
    before = {p: p.read_bytes() for p in first['previous']}
    real = json_store.save_json_atomic

    def fail_default(path, data, **kwargs):
        if Path(path).name == 'default.json':
            raise OSError('default unwritable')
        return real(path, data, **kwargs)

    with mock.patch.object(json_store, 'save_json_atomic', side_effect=fail_default):
        with pytest.raises(OSError):
            account_store.select(accounts, paths.TERMINAL_ID, 'B')
    assert {p: p.read_bytes() for p in before} == before
    assert not list(accounts.rglob('*.rollback'))
    assert not list(accounts.rglob('*.tmp'))


def test_selection_rollback_preserves_other_terminal_progress(accounts):
    before = account_store.select(accounts, paths.TERMINAL_ID, 'A')
    old = {p: p.read_bytes() for p in before['previous']}
    change = account_store.select(accounts, paths.TERMINAL_ID, 'B')
    other = account_store.select(accounts, 'other-terminal', 'C')
    default = accounts / 'accounts' / 'default.json'
    latest = default.read_bytes()
    account_store.rollback_selection(change)
    terminal = next(p for p in old if p != default)
    assert terminal.read_bytes() == old[terminal]
    assert default.read_bytes() == latest
    assert json.loads(default.read_bytes())['selectionId'] == other['selection_id']


def test_missing_restart_target_never_saves_selects_or_tears_down(accounts):
    with mock.patch.object(cli, '_restart_command', side_effect=FileNotFoundError('missing')), \
            mock.patch.object(cli, '_save_before_account_switch') as save, \
            mock.patch.object(account_store, 'select') as select, \
            mock.patch.object(cli, 'close_all_agents') as close:
        with pytest.raises(FileNotFoundError):
            cli._restart_into_account('B')
    save.assert_not_called()
    select.assert_not_called()
    close.assert_not_called()
    assert os.environ['LAINTAS_ACCOUNT_ID'] == 'A'


def test_failed_preparation_keeps_current_runtime_alive(accounts):
    with mock.patch.object(cli, '_save_account_tasks_before_switch') as save, \
            mock.patch.object(cli, '_account_idle', return_value=True), \
            mock.patch.object(account_store, 'select', side_effect=OSError('unwritable')), \
            mock.patch.object(cli, 'close_all_agents') as close:
        with pytest.raises(OSError):
            cli._restart_into_account('B')
    save.assert_called_once()
    close.assert_not_called()
    assert not account_store._transitioning
    assert os.environ['LAINTAS_ACCOUNT_ID'] == 'A'


def test_initial_login_exec_failure_restores_previous_selection(accounts):
    prior = account_store.select(accounts, paths.TERMINAL_ID, 'A')
    before = {p: p.read_bytes() for p in prior['previous']}
    with mock.patch.object(cli, '_restart_process', side_effect=OSError('exec failed')):
        with pytest.raises(OSError):
            cli._restart_after_login('B')
    assert {p: p.read_bytes() for p in before} == before
    assert os.environ['LAINTAS_ACCOUNT_ID'] == 'A'


def test_exec_failure_rolls_back_selection_and_exits_after_saving_once(accounts):
    prior = account_store.select(accounts, paths.TERMINAL_ID, 'A')
    before = {p: p.read_bytes() for p in prior['previous']}
    interactive = mock.Mock()
    registry = mock.Mock()
    with ExitStack() as stack:
        for module, attribute in [(cli, 'stop_trigger_scanner'), (cli, 'close_all_agents'),
                                  (cli, 'close_all_terminals'), (cli, '_get_mcp_mod'),
                                  (cli.browser_mod, 'close_all_browser_sessions'),
                                  (cli._terminal_agents, 'close'), (windows_host, 'stop_host'),
                                  (peer_coordination, 'release_all_leases'),
                                  (peer_coordination, 'get_coord'), (cli.child_registry, 'kill_all'),
                                  (cli.terminal_arbiter, 'reset_to_pristine')]:
            stack.enter_context(mock.patch.object(module, attribute))
        stack.enter_context(mock.patch.object(cli, '_account_idle', return_value=True))
        save = stack.enter_context(mock.patch.object(cli, '_save_account_tasks_before_switch'))
        stack.enter_context(mock.patch.object(cli, '_restart_process', side_effect=OSError('exec failed')))
        printed = stack.enter_context(mock.patch.object(cli.console, 'print'))
        with pytest.raises(SystemExit) as exc:
            cli._restart_into_account('B', registry, interactive_session=interactive)
    assert exc.value.code == 1
    save.assert_called_once()
    interactive.close.assert_called_once()
    registry.unregister.assert_called_once()
    assert {p: p.read_bytes() for p in before} == before
    assert os.environ['LAINTAS_ACCOUNT_ID'] == 'A'
    assert not account_store._transitioning
    assert '--resume' in str(printed.call_args)


@pytest.mark.parametrize('argv', [
    ['--execute', 'old task', '--app', 'old-app', '--app-state', 'old-state', '--session-id', 'old-session'],
    ['--execute=old task', '--app=old-app', '--app-options={}', '--agent-name=old'],
    ['-eold task', '--agent-id', 'old', '--app-launch-id', 'old', '--continue', '--monitor-only'],
])
def test_interactive_switch_removes_launch_tasks_but_login_retains_them(argv):
    argv += ['--backend', 'http://localhost:9', '--account=A', '--resume']
    with mock.patch.object(sys, 'argv', ['cli', *argv]):
        interactive = cli._account_restart_args('B')
        login = cli._account_restart_args('B', returning=False)
    assert interactive == ['--backend', 'http://localhost:9', '--account', 'B', '--resume', '--account-return']
    assert login == [v for v in argv if v != '--account=A'] + ['--account', 'B']


def test_snapshot_owner_conflict_records_diagnostic_without_task_content(monkeypatch):
    monkeypatch.setattr(paths, 'ACCOUNT_USER_ID', 'B')
    monkeypatch.setattr(agent_loop, '_debug_log', [])
    with mock.patch.object(agent_loop, '_atomic_write_json') as write:
        agent_loop.save_session_snapshot({'owner_user_id': 'A', 'shortTermMemory': 'secret'},
                                        [{'role': 'user', 'content': 'secret'}] * 2, '/work')
    write.assert_not_called()
    assert agent_loop._debug_log[-1]['msg'] == 'session_snapshot_owner_conflict'
    assert 'secret' not in str(agent_loop._debug_log)


def test_local_term_works_when_pairing_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(terminal_link, '_service', None)
    sub = mock.Mock()
    with mock.patch.object(cli, 'get_terminal', return_value=None), \
            mock.patch.object(cli, 'SubTerminalSession', return_value=sub) as create, \
            mock.patch.object(cli, 'register_terminal') as register, \
            mock.patch.object(cli, '_build_connected_subterminal_cmd') as connected, \
            mock.patch.object(cli.console, 'print') as printed:
        cli._cmd_term(['/term', 'local'], None, None)
    create.assert_called_once_with(cli.DEFAULT_SHELL, use_tmux=False)
    sub.start.assert_called_once()
    register.assert_called_once_with(sub, cli.DEFAULT_SHELL, 0, name='local', parent_terminal='term0')
    connected.assert_not_called()
    assert 'Created local shell' in str(printed.call_args)


def test_adopted_terminal_cannot_use_local_fallback_to_create_a_third_level(monkeypatch):
    monkeypatch.setattr(terminal_link, '_service', SimpleNamespace(parent={'id': 'controller'}))
    with mock.patch.object(cli, 'get_terminal', return_value=None), \
            mock.patch.object(cli, 'SubTerminalSession') as create, \
            mock.patch.object(cli.console, 'print') as printed:
        cli._cmd_term(['/term', 'child'], None, None)
    create.assert_not_called()
    assert 'third terminal level' in str(printed.call_args)


def test_term_start_failure_closes_pty_and_releases_pending_pairing(monkeypatch):
    service = mock.Mock()
    service.parent = None
    service.prepare_created.return_value = ('instance', {'id': 'invite'})
    monkeypatch.setattr(terminal_link, '_service', service)
    sub = mock.Mock()
    sub.start.side_effect = OSError('PTY unavailable')
    with mock.patch.object(cli, 'get_terminal', return_value=None), \
            mock.patch.object(cli, 'get_current_agent', return_value=None), \
            mock.patch.object(cli, 'SubTerminalSession', return_value=sub), \
            mock.patch.object(cli, '_build_connected_subterminal_cmd', return_value='command'), \
            mock.patch.object(cli, 'register_terminal') as register, \
            mock.patch.object(cli.console, 'print'):
        cli._cmd_term(['/term', 'child'], None, None)
    sub.close.assert_called_once()
    service.release.assert_called_once_with('child')
    register.assert_not_called()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX CLI process integration')
def test_real_account_switch_round_trip_saves_one_checkpoint_without_extra_autosave(tmp_path):
    isolated_python(tmp_path, '''
import paths,account_store
for uid in ('A','B'):
    account_store.remember(paths.ROOT_HOME,{'userId':uid,'token':'test-token'})
paths.configure_account('A')
import agent_loop
cwd=paths.ROOT_HOME.parent/'work'
cwd.mkdir()
agent_loop.save_resume_state({'_session_id':'task-a','owner_user_id':'A','objective':'saved task'},
                            [{'role':'user','content':'saved task'}],str(cwd),agent_id='primary')
''')
    home = tmp_path / 'home'
    workspace = tmp_path / 'work'
    env = dict(os.environ, HOME=str(tmp_path), LAINTAS_HOME=str(home),
               LAINTAS_ACCOUNT_ID='', LAINTAS_ACCOUNT_MODE='anonymous',
               LAINTAS_TERMINAL_ID='switch-round-trip', LAINTAS_BACKEND='http://127.0.0.1:1',
               PYTHONDONTWRITEBYTECODE='1')
    process = subprocess.Popen([sys.executable, '-B', str(Path(cli.__file__)),
                               '--account', 'A', '--resume', '--simple-prompt'],
                               cwd=workspace, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)

    def prompt():
        output = bytearray()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], .1)[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if output.endswith(b'\n$ '):
                    return output.decode('utf-8', errors='replace')
            if process.poll() is not None:
                break
        pytest.fail('CLI did not reach a prompt: ' + output.decode('utf-8', errors='replace'))

    def send(command):
        process.stdin.write((command + '\n').encode())
        process.stdin.flush()

    try:
        assert 'Resumed saved session.' in prompt()
        directory = account_store.profile_dir(home, 'A') / 'sessions'
        autosaves = {p: p.read_bytes() for p in directory.glob('*.json')
                     if json.loads(p.read_bytes()).get('kind') == 'autosave'}
        assert autosaves
        send('/account switch B')
        prompt()
        after_autosaves = {p: p.read_bytes() for p in directory.glob('*.json')
                          if json.loads(p.read_bytes()).get('kind') == 'autosave'}
        assert after_autosaves == autosaves
        checkpoints = [json.loads(p.read_bytes()) for p in directory.glob('*.json')
                       if json.loads(p.read_bytes()).get('kind') == 'checkpoint']
        # The checkpoint, latest pointer and per-session pointer deliberately
        # store three copies of one logical checkpoint.
        assert len({row['id'] for row in checkpoints}) == 1
        assert checkpoints[0]['state']['owner_user_id'] == 'A'
        assert checkpoints[0]['chat_history'][-1]['content'] == 'saved task'
        send('/account list')
        assert '* B  B' in prompt()
        send('/account switch A')
        assert 'Resumed saved session.' in prompt()
        send('/account list')
        assert '* A  A' in prompt()
        send('/q')
        process.communicate(timeout=15)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
