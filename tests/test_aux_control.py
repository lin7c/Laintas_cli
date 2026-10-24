"""The AUX off switch must prevent requests, not merely choose another model."""
import copy
import io
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

import agent_loop as loop
import background_compaction as bg
import laintas_cli as cli
import terminal_preferences as prefs


@pytest.fixture
def control(tmp_path, monkeypatch):
    saved = dict(loop._runtime_config)
    loop.reset_runtime_config()
    monkeypatch.setattr(prefs, "preference_path", lambda: tmp_path / "preferences.json")
    prefs.reset_cache()
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=160))
    with mock.patch.object(cli, "fetch_available_models") as fetch:
        yield SimpleNamespace(output=output, fetch=fetch)
    prefs.reset_cache()
    loop._runtime_config.clear()
    loop._runtime_config.update(saved)


def test_off_on_preserve_model_and_individual_feature_preferences(control):
    cli._cmd_model_aux(["custom/model"], {})
    loop.set_runtime_config("critic_enabled", False)
    loop.set_runtime_config("intent_enabled", True)
    cli._cmd_model_aux(["off"], {})
    assert not loop.aux_calls_enabled()
    assert loop.aux_model_override() == ("custom/model", "")
    assert loop.get_runtime_config("critic_enabled") is False
    assert loop.get_runtime_config("intent_enabled") is True
    cli._cmd_model_aux(["on"], {})
    assert loop.aux_calls_enabled()
    assert loop.aux_model_override() == ("custom/model", "")
    assert loop.get_runtime_config("critic_enabled") is False
    control.fetch.assert_not_called()


def test_off_and_model_survive_preference_reload(control):
    cli._cmd_model_aux(["saved/model"], {})
    cli._cmd_model_aux(["off"], {})
    loop.reset_runtime_config()
    prefs.reset_cache()
    for key, value in prefs.get_ui_preferences().items():
        loop.set_runtime_config(key, value)
    assert not loop.aux_calls_enabled()
    assert loop.aux_model_override() == ("saved/model", "")
    cli._cmd_model_aux(["on"], {})
    assert loop.aux_calls_enabled()
    assert loop.aux_model_override() == ("saved/model", "")


def test_status_is_read_only_and_does_not_fetch_models(control):
    cli._cmd_model_aux(["off"], {})
    saved = dict(loop._runtime_config)
    with mock.patch.object(prefs, "update") as update:
        cli._cmd_model_aux(["status"], {})
    assert "Auxiliary AI: off" in control.output.getvalue()
    assert loop._runtime_config == saved
    update.assert_not_called()
    control.fetch.assert_not_called()


@pytest.mark.parametrize("command", ["reset", "clear", "default", "another/model"])
def test_model_selection_and_reset_reenable_aux(control, command):
    cli._cmd_model_aux(["off"], {})
    cli._cmd_model_aux([command], {})
    assert loop.aux_calls_enabled()
    assert loop.aux_model_override() == ((command, "") if "/" in command else ("", ""))


def test_picker_can_disable_aux_without_changing_saved_model(control):
    control.fetch.return_value = ([{"id": "example/model"}], "test")
    with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
            mock.patch.object(cli, "show_model_selector", return_value={"id": "off"}):
        cli._cmd_model_aux([], {})
    assert not loop.aux_calls_enabled()
    assert loop.get_runtime_config("aux_model") != "off"


@pytest.mark.parametrize("task_kind", sorted(loop.AUX_TASK_KINDS))
def test_transport_blocks_aux_before_any_backend_work(control, task_kind):
    loop.set_runtime_config("aux_enabled", False)
    with mock.patch.object(cli, "get_backend_profile") as backend:
        result = cli._call_backend_stream_impl(
            {}, "source", "system", "/project", task_kind=task_kind,
            model_override="old/model")
    assert result["code"] == "aux_disabled"
    backend.assert_not_called()


def test_aux_helpers_do_not_fall_back_to_main_model(control):
    loop.set_runtime_config("aux_enabled", False)
    deps = SimpleNamespace(call_backend=mock.Mock())
    assert loop._aux_backend_reply(deps, {}, system_prompt="system", messages=[],
                                   task_kind="critic") == ""
    assert loop._llm_summarize(deps, {}, "/project", "source", None, "EN") is None
    assert loop._llm_review_summary(deps, {}, "/project", "source", "draft", None, "EN") == "draft"
    assert loop._summarize_head_in_chunks(deps, {}, [], None, "EN", "test") is None
    deps.call_backend.assert_not_called()


def test_compaction_keeps_user_history_and_only_prunes_locally(control, monkeypatch):
    loop.set_runtime_config("aux_enabled", False)
    messages = [{"role": "user", "content": f"user turn {index}"} for index in range(8)]
    before = copy.deepcopy(messages)
    state = {"_thread_messages": messages}
    monkeypatch.setattr(loop, "compaction_budget", lambda state: {"window": 2000, "usable": 1000})
    with mock.patch.object(loop, "_summarize_head_in_chunks") as summarize:
        loop._compact_thread_messages(messages, mock.Mock(), {}, "EN", state, force=True)
    assert messages == before
    summarize.assert_not_called()


def test_disabled_checkpoint_cancels_speculation_without_starting_another(control):
    loop.set_runtime_config("aux_enabled", False)
    coordinator = mock.Mock()
    token = bg.current.set(coordinator)
    try:
        with mock.patch.object(loop, "_compact_thread_messages", return_value=False):
            loop._coordinate_compaction([], mock.Mock(), {}, "EN", {})
        coordinator.close.assert_called_once()
        coordinator.start.assert_not_called()
    finally:
        bg.current.reset(token)


def test_off_progress_and_compact_explain_how_to_reenable(control, monkeypatch):
    loop.set_runtime_config("aux_enabled", False)
    reporter = mock.Mock()
    monkeypatch.setattr(cli, "_progress_reporter", reporter)
    monkeypatch.setattr(cli.handle_meta_command, "_last_agent_state", {}, raising=False)
    with mock.patch.object(cli.append_input, "current", return_value=None), \
            mock.patch.object(cli, "compact_session_context") as compact:
        cli._request_progress_report()
        cli._cmd_compact(["/compact"], {})
    reporter.request.assert_not_called()
    compact.assert_not_called()
    assert "/model aux on" in control.output.getvalue()


def test_disabled_memory_consolidation_does_not_advance_idle_watermark(control):
    loop.set_runtime_config("aux_enabled", False)
    state = {"_thread_messages": [{"role": "user", "content": "keep this"}],
             "_mem_idle_mark": 0}
    before = copy.deepcopy(state)
    assert not loop._consolidate_memories_when_idle(mock.Mock(), {}, state)
    assert state == before
