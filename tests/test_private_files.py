import json
from pathlib import Path

import pytest
import private_files as pf
import tools
import policy


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, 'store_path', lambda: tmp_path / 'policy.json')
    root = tmp_path / 'workspace'
    root.mkdir()
    folder = root / 'private'
    folder.mkdir()
    file = folder / 'token.txt'
    file.write_text('dummy-sensitive-token')
    (root / 'public.txt').write_text('public')
    (root / 'alias').symlink_to(file)
    return root, folder, file, tools.ToolCtx(cwd=str(root))


def test_hidden_lists_search_and_symlinks(workspace):
    root, folder, file, ctx = workspace
    pf.set_rule(folder, hidden=True)
    listing = tools._bi_fs_ls({'path': '.'}, ctx)
    assert listing['ok']
    assert [r['name'] for r in listing['result']] == ['public.txt']
    assert not tools._bi_fs_read({'path': 'alias', 'offset': 1}, ctx)['ok']
    assert not pf.allowed(root / 'private' / '..' / 'private' / 'token.txt')
    glob = tools._bi_fs_glob({'path': '.', 'pattern': '**/*'}, ctx)
    assert glob['ok']
    assert 'private' not in json.dumps(glob.get('result'))
    grep = tools._bi_fs_grep({'path': '.', 'pattern': 'dummy-sensitive'}, ctx)
    assert grep['ok']
    assert 'dummy-sensitive-token' not in json.dumps(grep)


def test_no_read_and_no_write_inheritance(workspace):
    root, folder, file, ctx = workspace
    pf.set_rule(folder, noRead=True)
    assert pf.allowed(file, 'visible')
    assert not tools._bi_fs_read({'path': str(file)}, ctx)['ok']
    # Writes exposing a diff cannot disclose unreadable old content.
    assert not tools._bi_fs_write({'path': str(file), 'content': 'new'}, ctx)['ok']
    pf.set_rule(folder, noWrite=True)
    assert tools._bi_fs_read({'path': str(file), 'offset': 1}, ctx)['ok']
    assert not tools._bi_fs_write({'path': str(file), 'content': 'new'}, ctx)['ok']
    assert not tools._bi_fs_edit({'path': str(file), 'old_string': 'dummy', 'new_string': 'new'}, ctx)['ok']
    assert not tools._bi_fs_delete({'path': str(root), 'recursive': True}, ctx)['ok']
    assert file.read_text() == 'dummy-sensitive-token'
    pf.set_rule(file)  # A child cannot loosen its parent's permission.
    assert not pf.allowed(file, 'write')


def test_shell_gate_and_corruption(workspace):
    root, folder, file, ctx = workspace
    pf.set_rule(folder, hidden=True)
    assert policy.evaluate('cat alias', str(root)).action == 'deny'
    assert not tools._bi_shell_exec({'command': 'cat alias'}, ctx)['ok']
    with pytest.raises(pf.PrivateFileError):
        pf.check_tool('browser.snapshot', {}, str(root))
    with pytest.raises(pf.PrivateFileError):
        pf.check_tool('file_push', {'paths': [str(file)]}, str(root))
    pf.set_rule(folder)
    assert pf.execution_error() is None
    pf.store_path().write_text('{damaged')
    assert pf.execution_error()
    assert not tools._bi_fs_read({'path': 'public.txt'}, ctx)['ok']


def test_registry_and_private_store(workspace):
    root, folder, file, ctx = workspace
    assert not pf.allowed(pf.store_path())
    pf.set_rule(file, noRead=True)
    assert not tools.get_registry().invoke('fs.read', {'path': str(file)}, ctx)['ok']
    assert pf.store_path().stat().st_mode & 0o777 == 0o600


def test_bridge_helpers_use_permissions(workspace, monkeypatch):
    import helpwo_server as server
    root, folder, file, ctx = workspace
    monkeypatch.setattr(server, '_local_root_ref', root)
    handler = object.__new__(server._HelpwoHandler)
    responses = []
    handler._json = lambda status, body: responses.append((status, body))
    pf.set_rule(folder, noRead=True, noWrite=True)
    assert not handler._private_access(str(file), 'read')
    assert not handler._private_access(str(root), 'write', recursive=True)
    assert all(status == 403 for status, _ in responses)


def test_cached_memory_and_loaded_skills_obey_new_rules(workspace):
    import memory_system
    import skills
    root, folder, file, ctx = workspace
    memory = folder / 'memory.md'
    memory.write_text('---\nname: private-memory\ntype: project\n---\ndummy-private-body')
    assert memory_system._parse_memory_file(memory) is not None
    meta = skills.SkillMetadata(name='private-skill', dir_path=str(folder))
    state = skills.SkillState(metadata=meta, body='dummy-private-skill')
    state.loaded = True
    original = skills._skill_states
    skills._skill_states = {'private-skill': state}
    try:
        pf.set_rule(folder, noRead=True)
        assert memory_system._parse_memory_file(memory) is None
        assert not skills.skill_is_available(meta)
        assert 'dummy-private-skill' not in skills.get_activated_skills_context()
        with pytest.raises(pf.PrivateFileError):
            pf.read_text(file)
    finally:
        skills._skill_states = original


def test_transfer_evidence_and_memory_mutation_checks(workspace, monkeypatch):
    import memory_system
    import skills
    root, folder, file, ctx = workspace
    pf.set_rule(folder, noRead=True, noWrite=True)
    assert not tools._bi_storage_put({'local_path': str(file), 'path': 'remote.txt'}, ctx)['ok']
    assert tools._resolve_evidence([str(file)], ctx)[1]
    assert tools._check_file_write_policy(str(file), ctx, '')['status'] == 'private_file_denied'
    monkeypatch.setattr(memory_system, 'MEMORY_DIR', root / 'test-memory')
    monkeypatch.setattr(memory_system, 'MEMORY_INDEX', root / 'test-memory' / 'MEMORY.md')
    monkeypatch.setattr(memory_system, '_memory_path', lambda *a, **k: file)
    assert not memory_system.delete_memory('token')[0]
    assert not memory_system.write_memory('token', 'project', 'description', 'new body')[0]
    state = skills.SkillState(metadata=skills.SkillMetadata(name='cached-ref', dir_path=str(root)))
    state.references['private/token.txt'] = 'dummy-cached-sensitive-reference'
    monkeypatch.setattr(skills, '_skill_states', {'cached-ref': state})
    monkeypatch.setattr(skills, '_skill_metadata', {'cached-ref': state.metadata})
    # Real reference directory and cache must both observe newly applied rules.
    reference_dir = root / 'references'
    reference_dir.mkdir()
    reference = reference_dir / 'secret.md'
    reference.write_text('dummy-ref')
    state.references['secret.md'] = 'dummy-ref'
    pf.set_rule(reference, noRead=True)
    assert skills.load_reference('cached-ref', 'secret.md') is None
    assert file.read_text() == 'dummy-sensitive-token'


def test_hidden_context_and_native_hooks(workspace, monkeypatch):
    import agent_loop
    import extension_runtime
    import hooks
    from types import SimpleNamespace
    root, folder, file, ctx = workspace
    pf.set_rule(folder, hidden=True)
    assert pf.public_path(folder) == '[unavailable]'
    assert pf.public_path(root) == str(root)
    reads = []
    deps = SimpleNamespace(read_file=lambda path: reads.append(path) or 'dummy-body')
    assert agent_loop._private_prompt_read(deps, str(file)) is None
    assert not reads
    monkeypatch.setattr(hooks, '_load_config', lambda: pytest.fail('native hooks must not load'))
    assert hooks.trigger('pre_tool') == (True, [])
    runtime = extension_runtime.ExtensionRuntime()
    runtime._observers['event'] = [('dummy', lambda p: pytest.fail('native observer must not run'))]
    runtime.emit('event', {})
    assert not runtime.load('dummy')[0]


def test_automatic_memory_migration_cannot_write_readonly_files(workspace, monkeypatch):
    import memory_system
    root, folder, file, ctx = workspace
    monkeypatch.setattr(memory_system, 'MEMORY_DIR', folder)
    legacy = folder / 'legacy.md'
    legacy.write_text('---\nname: legacy\ntype: project\n---\ndummy-legacy-body')
    original = legacy.read_text()
    pf.set_rule(folder, noWrite=True)
    assert memory_system.migrate_legacy_scopes() == 0
    assert legacy.read_text() == original
    with pytest.raises(pf.PrivateFileError):
        memory_system._atomic_write_text(legacy, 'bypass')
