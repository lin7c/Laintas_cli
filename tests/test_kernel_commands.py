import io
from types import SimpleNamespace
from unittest import mock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import laintas_cli as cli
import resource_ui
import windows_kernel as kernel


@pytest.fixture
def ui(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=160))
    monkeypatch.setattr(cli.winbridge, "in_wsl", lambda: True)
    state = dict(wsl=True, installed=True, version="1.0.0", path=r"C:\Tools [test]\kernel.exe",
                 connected=False, processRunning=False, tiers={}, tools=[])
    monkeypatch.setattr(kernel, "status", lambda: state)
    return SimpleNamespace(output=output, state=state)


@pytest.mark.parametrize("command", ["/kernel", "/windows"])
def test_alias_has_all_actions_and_contextual_completion(command):
    spec = cli._find_command_spec(command)
    assert {"update", "uninstall", "restart", "start", "check", "status"} <= set(spec.subcommands)
    def complete(text):
        return {c.text for c in cli.MetaCompleter().get_completions(
            cli.Document(text, len(text)), mock.Mock(completion_requested=True))}
    assert {"update", "uninstall"} <= complete(command + " ")
    assert "--force" in complete(command + " update ")
    assert {"workspace", "read", "write"} <= complete(command + " restart ")
    with pytest.raises(cli.SlashCommandUsageError):
        cli._validate_slash_args(command, ["uninstall", "extra"])


def test_status_is_offline_and_keeps_literal_path(ui, monkeypatch):
    check = mock.Mock(side_effect=AssertionError("status must not go online"))
    monkeypatch.setattr(kernel, "latest", check)
    cli._cmd_windows(["/kernel", "status"])
    assert ui.state["path"] in ui.output.getvalue()
    assert "Stopped" in ui.output.getvalue()
    check.assert_not_called()


def test_no_argument_falls_back_to_status_without_tty(ui, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    manager = mock.Mock()
    monkeypatch.setattr(cli, "_kernel_manager", manager)
    cli._cmd_windows(["/kernel"])
    manager.assert_not_called()
    assert "Stopped" in ui.output.getvalue()


@pytest.mark.parametrize("command", [["update", "--typo"], ["uninstall", "extra"], ["start", "wrong"], ["restart", "read", "extra"]])
def test_bad_arguments_do_not_mutate(ui, monkeypatch, command):
    action = mock.Mock()
    monkeypatch.setattr(cli, "_kernel_action", action)
    cli._cmd_windows(["/kernel", *command])
    action.assert_not_called()
    assert "/windows update" in ui.output.getvalue()


def test_update_feedback_handles_kept_and_restarted(ui, monkeypatch):
    update = mock.Mock(return_value={"action": "kept", "version": "2.0.0"})
    monkeypatch.setattr(kernel, "update", update)
    cli._cmd_windows(["/kernel", "update"])
    assert "Nothing changed" in ui.output.getvalue()
    assert update.call_args.kwargs["force"] is False
    update.return_value = {"action": "upgraded", "version": "2.0.0", "restarted": True, "tier": "read"}
    cli._cmd_windows(["/kernel", "update", "--force"])
    assert update.call_args.kwargs["force"] is True
    assert "same read access" in ui.output.getvalue()


def test_start_passes_the_explicit_tier(ui, monkeypatch):
    start = mock.Mock(return_value={"action": "started", "tier": "write"})
    monkeypatch.setattr(kernel, "ensure_started", start)
    cli._cmd_windows(["/kernel", "start", "write"])
    assert start.call_args.args == ("write",)
    assert start.call_args.kwargs["restart"] is False
    assert "Started with write" in ui.output.getvalue()


def test_restart_reuses_connected_tier(ui, monkeypatch):
    monkeypatch.setattr(kernel, "_connected_tier", lambda: "read")
    start = mock.Mock(return_value={"action": "started", "tier": "read"})
    monkeypatch.setattr(kernel, "ensure_started", start)
    cli._cmd_windows(["/kernel", "restart"])
    assert start.call_args.args == ("read",)
    assert start.call_args.kwargs["restart"] is True


def test_uninstall_is_one_command_and_errors_are_literal(ui, monkeypatch):
    uninstall = mock.Mock(side_effect=kernel.KernelInstallError(r"blocked [/\\]"))
    monkeypatch.setattr(kernel, "uninstall", uninstall)
    cli._cmd_windows(["/kernel", "uninstall"])
    uninstall.assert_called_once_with()
    assert r"blocked [/\\]" in ui.output.getvalue()


def test_manager_keyboard_action_returns_to_screen_with_feedback(ui, monkeypatch):
    # Use the real full-screen application, including its keyboard bindings.
    real_browser = cli._KernelBrowser
    actions = mock.Mock(return_value="Already up to date (v1.0.0). Nothing changed.")
    monkeypatch.setattr(cli, "_kernel_action", actions)
    screens = []
    with create_pipe_input() as pipe:
        def browser(**kwargs):
            app = real_browser(**kwargs, input=pipe, output=DummyOutput())
            screens.append(app)
            if len(screens) == 1:
                # Start, read, write, update — select update and run it.
                pipe.send_text("\x1b[B\x1b[B\x1b[B\r")
            else:
                pipe.send_text("q")
            return app
        monkeypatch.setattr(cli, "_KernelBrowser", browser)
        cli._kernel_manager()
    actions.assert_called_once_with("update", [])
    assert len(screens) == 2
    assert screens[1]._selected_item().key == "update"
    assert "Already up to date" in screens[1].status


def test_manager_quit_never_runs_an_operation(ui, monkeypatch):
    real_browser = cli._KernelBrowser
    action = mock.Mock()
    monkeypatch.setattr(cli, "_kernel_action", action)
    with create_pipe_input() as pipe:
        monkeypatch.setattr(cli, "_KernelBrowser", lambda **kw: real_browser(
            **kw, input=pipe, output=DummyOutput()))
        pipe.send_text("q")
        cli._kernel_manager()
    action.assert_not_called()
