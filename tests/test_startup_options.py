"""Launch flags apply to one run and never write the user's preferences."""
import argparse
import io
import json
import os

import pytest

import agent_loop
import laintas_cli
import mode_manager
import terminal_preferences


def _args(**overrides):
    values = dict(
        task=None, execute=None, output_format="text", cwd=None, route=None,
        mcp_config=None, append_system_prompt=None, append_system_prompt_file=None,
        config=[], effort=None, max_loops=None, verbose=False, theme=None,
        no_color=False, model=None, mode=None, plan=False, yes=False,
        auto_approve=None, tools=None, disallow_tools=None)
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture(autouse=True)
def _restore_process_state(monkeypatch):
    saved_config = dict(agent_loop._runtime_config)
    saved_approval = dict(laintas_cli._session_approval_state)
    monkeypatch.setattr(laintas_cli, "_startup_model_override", "")
    monkeypatch.setattr(laintas_cli, "_startup_auto_approve", None)
    monkeypatch.setattr(laintas_cli, "_startup_first_message", None)
    monkeypatch.setattr(mode_manager, "_session_mode", None)
    monkeypatch.setattr(laintas_cli, "_apply_ui_theme", lambda *a, **k: "dark")
    yield
    agent_loop._runtime_config.clear()
    agent_loop._runtime_config.update(saved_config)
    laintas_cli._session_approval_state.update(saved_approval)
    agent_loop.set_startup_tool_scope(None, None)
    agent_loop.set_startup_prompt_append("")
    laintas_cli.console.no_color = False


def _tty(monkeypatch, stdin=True, stdout=True):
    monkeypatch.setattr(laintas_cli.sys.stdin, "isatty", lambda: stdin, raising=False)
    monkeypatch.setattr(laintas_cli.sys.stdout, "isatty", lambda: stdout, raising=False)


# ---------- the task ----------

def test_positional_task_opens_an_interactive_session_at_a_terminal(monkeypatch):
    _tty(monkeypatch)
    args = _args(task="fix the build")
    laintas_cli._resolve_startup_task(args)
    assert args.execute is None
    assert laintas_cli._startup_first_message == "fix the build"


def test_positional_task_runs_once_when_output_is_piped(monkeypatch):
    _tty(monkeypatch, stdout=False)
    args = _args(task="fix the build")
    laintas_cli._resolve_startup_task(args)
    assert args.execute == "fix the build"
    assert laintas_cli._startup_first_message is None


@pytest.mark.parametrize("field", ("task", "execute"))
def test_dash_reads_the_task_from_standard_input(monkeypatch, field):
    monkeypatch.setattr(laintas_cli.sys, "stdin", io.StringIO("  summarize this\n"))
    args = _args(**{field: "-"})
    laintas_cli._resolve_startup_task(args)
    assert args.execute == "summarize this"


def test_standard_input_is_only_read_when_asked_for(monkeypatch):
    class Unreadable(io.StringIO):
        def read(self, *a):
            raise AssertionError("read standard input without being asked")

    monkeypatch.setattr(laintas_cli.sys, "stdin", Unreadable())
    args = _args(execute="say hi")
    laintas_cli._resolve_startup_task(args)
    assert args.execute == "say hi"


def test_task_errors_are_reported_not_guessed(monkeypatch):
    with pytest.raises(ValueError, match="once"):
        laintas_cli._resolve_startup_task(_args(task="a", execute="b"))
    monkeypatch.setattr(laintas_cli.sys, "stdin", io.StringIO("   "))
    with pytest.raises(ValueError, match="empty"):
        laintas_cli._resolve_startup_task(_args(execute="-"))
    _tty(monkeypatch)
    with pytest.raises(ValueError, match="--output-format"):
        laintas_cli._resolve_startup_task(_args(output_format="json"))


# ---------- overrides ----------

def test_model_flag_is_used_but_never_saved(monkeypatch):
    saved = []
    monkeypatch.setattr(terminal_preferences, "set_value", lambda *a: saved.append(a))
    monkeypatch.setattr(terminal_preferences, "get",
                        lambda key, default="": "saved-model" if key == "model" else default)
    laintas_cli._apply_startup_overrides(_args(model="run-model"))
    assert laintas_cli.get_selected_model() == "run-model"
    assert saved == []
    # An explicit /model choice replaces the launch flag.
    laintas_cli.set_selected_model("picked")
    assert laintas_cli.get_selected_model() == "saved-model"


def test_mode_flag_is_session_only_and_validated(monkeypatch):
    saved = []
    monkeypatch.setattr(mode_manager, "_save_instance_active_mode", saved.append)
    laintas_cli._apply_startup_overrides(_args(mode="review"))
    assert mode_manager.get_active_mode()["name"] == "review"
    assert saved == []
    with pytest.raises(ValueError, match="Unknown mode"):
        laintas_cli._apply_startup_overrides(_args(mode="nosuch"))
    # Switching modes by hand ends the launch override.
    mode_manager.activate("act")
    assert mode_manager._session_mode is None


def test_plan_flag_arms_plan_mode(monkeypatch):
    import plan_mode
    armed = []
    monkeypatch.setattr(plan_mode, "arm_plan_mode", lambda: armed.append(True))
    laintas_cli._apply_startup_overrides(_args(plan=True))
    assert armed == [True]
    with pytest.raises(ValueError):
        laintas_cli._apply_startup_overrides(_args(plan=True, mode="review"))


@pytest.mark.parametrize("flags,writes,commands", [
    (dict(auto_approve="writes"), True, False),
    (dict(auto_approve="commands"), False, True),
    (dict(yes=True), True, True),
    (dict(auto_approve="none"), False, False),
])
def test_auto_approve_flag_outlives_the_mode_posture(flags, writes, commands):
    laintas_cli._apply_startup_overrides(_args(**flags))
    # --execute re-syncs from the mode before it runs; the flag must survive.
    laintas_cli._sync_session_approval_from_mode()
    assert laintas_cli._session_approval_state["all_writes"] is writes
    assert laintas_cli._session_approval_state["all_commands"] is commands


def test_yes_and_a_different_posture_is_refused():
    with pytest.raises(ValueError):
        laintas_cli._apply_startup_overrides(_args(yes=True, auto_approve="writes"))


def test_config_flags_set_runtime_values_and_named_flags_win():
    laintas_cli._apply_startup_overrides(_args(
        config=["max_loops=5", "reasoning_effort=low"], max_loops=9, effort="high",
        verbose=True))
    assert agent_loop.get_runtime_config("max_loops") == 9
    assert agent_loop.get_runtime_config("reasoning_effort") == "high"
    assert agent_loop.get_runtime_config("detail") is True


@pytest.mark.parametrize("config,match", [
    (["bad"], "KEY=VALUE"), (["nope=1"], "unknown setting"), (["max_loops=0"], "max_loops"),
])
def test_bad_config_flags_are_rejected(config, match):
    with pytest.raises(ValueError, match=match):
        laintas_cli._apply_startup_overrides(_args(config=config))


def test_no_color_turns_color_off():
    laintas_cli._apply_startup_overrides(_args(no_color=True))
    assert laintas_cli.console.no_color is True
    assert agent_loop.get_runtime_config("theme") == "mono"


def test_tool_flags_only_narrow():
    laintas_cli._apply_startup_overrides(_args(tools="file.*, web.search",
                                               disallow_tools="file.write"))
    allowed = agent_loop._tool_allowed_at_startup
    assert allowed("file.read") and allowed("web.search")
    assert not allowed("file.write") and not allowed("shell.exec")
    for protocol_tool in list(agent_loop._PROTOCOL_TOOLS)[:3]:
        assert allowed(protocol_tool)


def test_early_options_change_directory_route_and_mcp_file(monkeypatch, tmp_path):
    import mcp_client
    import model_route

    monkeypatch.setattr(mcp_client, "CONFIG_PATH", mcp_client.CONFIG_PATH)
    # setenv registers the variable for restoration; the option overwrites it.
    monkeypatch.setenv(model_route.ENV_OVERRIDE, "")
    monkeypatch.chdir(os.getcwd())
    servers = tmp_path / "servers.json"
    servers.write_text("{}", encoding="utf-8")
    extra = tmp_path / "extra.txt"
    extra.write_text("Answer in one line.\n", encoding="utf-8")
    args = _args(cwd=str(tmp_path), route="my-route", mcp_config=str(servers),
                 append_system_prompt="Be terse.", append_system_prompt_file=str(extra))
    laintas_cli._apply_early_startup_options(args)
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)
    assert os.environ[model_route.ENV_OVERRIDE] == "my-route"
    assert mcp_client.CONFIG_PATH == servers
    assert args.append_system_prompt == "Be terse.\n\nAnswer in one line."
    for bad in (dict(cwd=str(tmp_path / "missing")), dict(mcp_config=str(tmp_path / "missing")),
                dict(append_system_prompt_file=str(tmp_path / "missing"))):
        with pytest.raises(ValueError):
            laintas_cli._apply_early_startup_options(_args(**bad))


# ---------- --resume ----------

def test_resume_selector_accepts_id_prefix_number_and_latest(monkeypatch):
    first = {"session_id": "abc123", "chat_history": [1]}
    second = {"state": {"_session_id": "def456"}, "chat_history": [2]}
    monkeypatch.setattr(laintas_cli, "list_resume_states", lambda cwd, agent_id: [first, second])
    monkeypatch.setattr(
        laintas_cli, "load_resume_state",
        lambda cwd, session_id=None, agent_id="primary":
            {"logical": True} if session_id == "nightly" else (None if session_id else first))
    pick = lambda selector: laintas_cli._startup_resume_blob("/w", selector, "primary")
    assert pick(None) is first
    assert pick("nightly") == {"logical": True}
    assert pick("def") is second
    assert pick("2") is second
    assert pick("latest") is first
    assert pick("zzz") is None and pick("9") is None


# ---------- --output-format json ----------

def _fake_loop(response):
    def run(deps, **kwargs):
        print("progress line that must not reach standard output")
        return response
    return run


def test_json_output_is_one_object_and_progress_goes_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(laintas_cli, "get_loop_deps", lambda: None)
    monkeypatch.setattr(laintas_cli, "run_agent_loop", _fake_loop(
        {"success": True, "msg": "done", "state": {"lastOutput": "42"}}))
    code = laintas_cli.run_execute_mode("task", {}, 0, None, output_format="json")
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0 and report["success"] is True and report["exit_code"] == 0
    assert report["result"] == "done\n42" and report["error"] == ""
    assert {"session_id", "model", "mode", "cwd", "duration_ms"} <= set(report)
    assert "progress line" in captured.err and "progress line" not in captured.out


def test_json_output_reports_failure_and_crashes(monkeypatch, capsys):
    monkeypatch.setattr(laintas_cli, "get_loop_deps", lambda: None)
    monkeypatch.setattr(laintas_cli, "run_agent_loop", _fake_loop(
        {"success": False, "msg": "", "state": {"_last_error": "quota exhausted"}}))
    assert laintas_cli.run_execute_mode("task", {}, 0, None, output_format="json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["success"] is False and report["error"] == "quota exhausted"

    def boom(deps, **kwargs):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr(laintas_cli, "run_agent_loop", boom)
    assert laintas_cli.run_execute_mode("task", {}, 0, None, output_format="json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["success"] is False and "backend unreachable" in report["error"]


def test_text_output_is_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(laintas_cli, "get_loop_deps", lambda: None)
    monkeypatch.setattr(laintas_cli, "run_agent_loop", lambda deps, **kw: (
        {"success": False, "msg": "partial", "state": {"_last_error": "stopped"}}))
    assert laintas_cli.run_execute_mode("task", {}, 0, None) == 1
    captured = capsys.readouterr()
    assert captured.out == "partial\n" and captured.err == "stopped\n"


# ---------- sign-in ----------

def test_unattended_run_never_starts_a_sign_in(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("started an interactive sign-in")

    monkeypatch.setattr(laintas_cli, "load_session", lambda: None)
    monkeypatch.setattr(laintas_cli, "choose_login_method", fail)
    monkeypatch.setattr(laintas_cli, "login_via_browser", fail)
    assert laintas_cli.ensure_auth(interactive=False) is None
