"""Private file rules for model/tool access, with owner-only local management.

Rules combine monotonically across ancestors and resolve both lexical and real
paths. This is an application boundary, not an OS sandbox. Native execution is
disabled while custom rules exist rather than relying on shell-string matching.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from json_store import save_json_atomic


class PrivateFileError(PermissionError):
    pass


def store_path() -> Path:
    return Path.home() / '.laintas' / 'private-files.json'


def canonical(path, cwd=None):
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.path.join(cwd or os.getcwd(), str(path)))))


def _under(path, parent):
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def rules():
    try:
        data = json.loads(store_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise PrivateFileError('Private file settings unavailable; access denied.') from None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('rules'), list):
        raise PrivateFileError('Invalid private file settings; access denied.')
    for r in data['rules']:
        if (not isinstance(r, dict) or not isinstance(r.get('path'), str)
                or not os.path.isabs(r['path']) or canonical(r['path']) != r['path']
                or any(type(r.get(k)) is not bool for k in ('hidden', 'noRead', 'noWrite'))):
            raise PrivateFileError('Invalid private file rule; access denied.')
    return data['rules']


def set_rule(path, *, hidden=False, noRead=False, noWrite=False):
    path = canonical(path)
    current = [r for r in rules() if r['path'] != path]
    if hidden or noRead or noWrite:
        current.append({'path': path, 'hidden': bool(hidden), 'noRead': bool(noRead), 'noWrite': bool(noWrite)})
    save_json_atomic(store_path(), {'version': 1, 'rules': current}, mode=0o600, account_independent=True)


def flags(path, cwd=None):
    lexical = canonical(path, cwd)
    physical = os.path.normcase(os.path.realpath(lexical))
    out = dict(hidden=False, noRead=False, noWrite=False)
    # Policy and credential storage are never exposed to model file tools.
    protected = [str(store_path()), str(Path.home() / '.laintas' / 'password-vault')]
    for p in protected:
        if any(_under(target, canonical(p)) or _under(target, canonical(os.path.realpath(p))) for target in (lexical, physical)):
            out = dict(hidden=True, noRead=True, noWrite=True)
    for r in rules():
        if any(_under(target, parent) for target in (lexical, physical)
               for parent in (r['path'], canonical(os.path.realpath(r['path'])))):
            for k in out:
                out[k] = out[k] or r[k]
    return out


def allowed(path, operation='visible', cwd=None):
    f = flags(path, cwd)
    return not (f['hidden'] or operation == 'read' and f['noRead'] or operation == 'write' and f['noWrite'])


def check(path, operation='read', cwd=None, *, recursive=False):
    if not allowed(path, operation, cwd):
        raise PrivateFileError('Access denied by private file permissions.')
    if recursive:
        base = canonical(path, cwd)
        # Include non-existent protected targets to prevent renaming/deleting
        # their parent into a state that bypasses protection.
        for r in rules():
            if _under(r['path'], base) and (r['hidden'] or operation == 'write' and r['noWrite'] or operation == 'read' and r['noRead']):
                raise PrivateFileError('Recursive operation contains private files; access denied.')
        if os.path.isdir(base) and not os.path.islink(base):
            for directory, dirs, files in os.walk(base, followlinks=False):
                for name in dirs + files:
                    check(os.path.join(directory, name), operation)


def execution_error():
    try:
        if not rules():
            return None
    except PrivateFileError:
        pass
    return 'Private files are enabled. Native execution needs an isolated runtime; use protected file tools.'


def public_path(path):
    """Suppress hidden locations in automatically generated model context."""
    try:
        return str(path) if allowed(path, 'visible') else '[unavailable]'
    except PrivateFileError:
        return '[unavailable]'


def check_tool(name, params, cwd=None, *, external=False):
    if external or name.startswith(('shell.', 'terminal.', 'session.', 'git.', 'snapshot.', 'browser.', 'app.')):
        error = execution_error()
        if error:
            raise PrivateFileError(error)
    if name == 'fs.diff' and params.get('path') and not params.get('a'):
        error = execution_error()
        if error:
            raise PrivateFileError(error)
    write = name in ('fs.write', 'fs.edit', 'fs.multi_edit', 'fs.delete')
    if name.startswith('storage.'):
        # Storage paths are remote names, not host filesystem paths.
        if name == 'storage.put' and params.get('local_path'):
            check(params['local_path'], 'read', cwd)
        return
    if name == 'file_push':
        paths = params.get('paths') or []
        if isinstance(paths, str):
            paths = [paths]
        for path in paths:
            if isinstance(path, str):
                check(path, 'read', cwd, recursive=True)
    for key in ('path', 'a', 'b', 'file', 'file_path', 'output_path', 'image_path'):
        path = params.get(key)
        if isinstance(path, str) and path:
            operation = 'write' if write or key == 'output_path' else 'read'
            check(path, operation, cwd, recursive=name == 'fs.delete')
            # Existing write/edit tools return old-content diffs. Prevent a
            # noRead file from leaking through that write response.
            if write and name != 'fs.delete' and os.path.exists(canonical(path, cwd)):
                check(path, 'read', cwd)


def guard(name):
    """Also enforce when in-process callers invoke builtins directly."""
    def decorate(fn):
        @functools.wraps(fn)
        def run(params, ctx):
            try:
                check_tool(name, params or {}, getattr(ctx, 'cwd', None))
            except PrivateFileError as error:
                return {'ok': False, 'error': str(error), 'status': 'private_file_denied'}
            return fn(params, ctx)
        return run
    return decorate


def handle_command(parts):
    """Local owner command. Never registered as a model-facing mutation tool."""
    import shlex
    usage = '/private [list | set <path> hidden|no-read|no-write [...] | remove <path>]'
    try:
        action = parts[0] if parts else 'list'
        if action == 'list' and len(parts) <= 1:
            for r in rules():
                kinds = [label for k, label in [('hidden', 'hidden'), ('noRead', 'no-read'), ('noWrite', 'no-write')] if r[k]]
                print(f"{shlex.quote(r['path'])}  {', '.join(kinds)}")
            if not rules():
                print('No private file rules. ' + usage)
        elif action == 'remove' and len(parts) == 2:
            set_rule(parts[1])
            print('Private rule removed.')
        elif action == 'set' and len(parts) >= 3 and set(parts[2:]) <= {'hidden', 'no-read', 'no-write'}:
            set_rule(parts[1], hidden='hidden' in parts[2:], noRead='no-read' in parts[2:], noWrite='no-write' in parts[2:])
            print('Private rule saved. Native AI execution is disabled while rules exist.')
        else:
            print(usage)
    except (OSError, ValueError) as error:
        print(str(error))


def read_text(path, *, encoding='utf-8'):
    check(path, 'read')
    return Path(path).read_text(encoding=encoding)
