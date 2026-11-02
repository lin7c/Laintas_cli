"""Bootstrap identity, cheap flags, and nonblocking update notices."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

import cli_entry
import laintas_cli as cli
import updater
from cli_args import build_parser
from version import __version__


@pytest.mark.parametrize("flag", ["--version", "-V", "--help", "-h"])
def test_cheap_flags_skip_runtime_and_invalid_account(tmp_path, flag):
    env = dict(os.environ, LAINTAS_HOME=str(tmp_path / "unused"),
               LAINTAS_ACCOUNT_ID="missing-account", LAINTAS_ACCOUNT_MODE="",
               PYTHONDONTWRITEBYTECODE="1")
    code = """
import sys
import cli_entry
sys.argv = ['laintas-cli', sys.argv[1]]
try:
    cli_entry.main()
except SystemExit as exc:
    assert exc.code == 0
assert 'laintas_cli' not in sys.modules
assert 'prompt_toolkit' not in sys.modules
assert 'rich' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-B", "-c", code, flag],
                            cwd=Path(cli_entry.__file__).parent, env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout
    if flag in ("--version", "-V"):
        assert result.stdout.strip() == f"laintas-cli {__version__}"
    assert not (tmp_path / "unused").exists()


def test_bootstrap_preserves_main_module_and_account_initialization(tmp_path):
    # ACP exits before interactive startup. A fake adapter exercises the real
    # runtime bootstrap without connecting a model or starting REPL workers.
    env = dict(os.environ, LAINTAS_HOME=str(tmp_path / "state"),
               LAINTAS_ACCOUNT_MODE="anonymous", LAINTAS_ACCOUNT_ID="",
               PYTHONDONTWRITEBYTECODE="1")
    code = """
import sys, types
adapter = types.ModuleType('acp_agent')
def serve(depth):
    import laintas_cli, paths
    assert laintas_cli is sys.modules['__main__']
    assert paths._ACCOUNT_CONFIGURED
    assert depth == 3
    return 0
adapter.serve = serve
sys.modules['acp_agent'] = adapter
sys.argv = ['laintas-cli', '--acp', '--depth', '3']
import cli_entry
cli_entry.main()
"""
    result = subprocess.run([sys.executable, "-B", "-c", code],
                            cwd=Path(cli_entry.__file__).parent, env=env,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_shared_parser_preserves_launch_options():
    args = build_parser().parse_args([
        "--execute", "task", "--resume", "session", "--theme", "light",
        "--config", "loop_delay=0", "--model", "test-model", "--plan"])
    assert args.execute == "task"
    assert args.resume == "session"
    assert args.theme == "light"
    assert args.config == ["loop_delay=0"]
    assert args.model == "test-model"
    assert args.plan


def test_entry_modules_are_in_release_manifest():
    root = Path(cli_entry.__file__).parent
    manifest = json.loads((root / "package_manifest.json").read_text())
    assert {"cli_entry", "cli_args", "laintas_cli"} <= set(manifest["modules"])


def test_update_notice_does_not_wait_for_the_network():
    entered, release = threading.Event(), threading.Event()

    def check():
        entered.set()
        assert release.wait(3)
        return "next-version"

    with mock.patch.object(updater, "check_update_available", side_effect=check), \
            mock.patch.object(updater, "is_source_checkout", return_value=True), \
            mock.patch.object(cli.startup_mail, "post") as post:
        worker = cli._start_update_check()
        try:
            assert worker is not None
            assert entered.wait(2)
            assert worker.is_alive()
            post.assert_not_called()
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive()
        assert post.call_args.kwargs["action"] == ""
        assert "git pull" in post.call_args.args[2]
