"""Tests for /model image and /model video: per-kind media model choice and off switch.

The off switch must remove the tool from the advertised list and refuse at
invoke time, not merely change prompt wording. The two kinds are separate
axes: switching one off must not touch the other.
"""
import io
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

import agent_loop as loop
import laintas_cli as cli
import terminal_preferences as prefs
import tools


_real_fetch_media_models = cli.fetch_media_models


MEDIA_ROWS = [
    {"id": "img-a", "kind": "image", "description": "First image model",
     "defaultSize": "2048x2048", "default": True},
    {"id": "img-b", "kind": "image", "description": "Local image model",
     "defaultSize": None, "default": False, "device": True, "local": True},
    {"id": "vid-a", "kind": "video", "description": "First video model",
     "defaultSize": None, "default": True},
]


@pytest.fixture
def control(tmp_path, monkeypatch):
    saved = dict(loop._runtime_config)
    loop.reset_runtime_config()
    monkeypatch.setattr(prefs, "preference_path", lambda: tmp_path / "preferences.json")
    prefs.reset_cache()
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=160))
    with mock.patch.object(cli, "fetch_media_models") as fetch:
        yield SimpleNamespace(output=output, fetch=fetch)
    prefs.reset_cache()
    loop._runtime_config.clear()
    loop._runtime_config.update(saved)


def test_axes_are_independent(control):
    cli._cmd_model_media("image", ["img-x"], {})
    cli._cmd_model_media("image", ["off"], {})
    assert not loop.media_generation_enabled("image")
    assert loop.media_model_override("image") == "img-x"
    # Video is a separate axis: untouched by the image switch.
    assert loop.media_generation_enabled("video")
    assert loop.media_model_override("video") == ""
    cli._cmd_model_media("image", ["on"], {})
    assert loop.media_generation_enabled("image")
    assert loop.media_model_override("image") == "img-x"
    control.fetch.assert_not_called()


def test_off_and_model_survive_preference_reload(control):
    cli._cmd_model_media("video", ["saved/vid"], {})
    cli._cmd_model_media("video", ["off"], {})
    loop.reset_runtime_config()
    prefs.reset_cache()
    for key, value in prefs.get_ui_preferences().items():
        loop.set_runtime_config(key, value)
    assert not loop.media_generation_enabled("video")
    assert loop.media_model_override("video") == "saved/vid"


def test_status_is_read_only(control):
    cli._cmd_model_media("image", ["off"], {})
    saved = dict(loop._runtime_config)
    with mock.patch.object(prefs, "update") as update:
        cli._cmd_model_media("image", ["status"], {})
    assert "Image generation: off" in control.output.getvalue()
    assert loop._runtime_config == saved
    update.assert_not_called()
    control.fetch.assert_not_called()


@pytest.mark.parametrize("kind", ["image", "video"])
def test_reset_clears_model_and_reenables(control, kind):
    cli._cmd_model_media(kind, ["some/model"], {})
    cli._cmd_model_media(kind, ["off"], {})
    cli._cmd_model_media(kind, ["reset"], {})
    assert loop.media_generation_enabled(kind)
    assert loop.media_model_override(kind) == ""


def test_picker_filters_by_kind_and_marks_default_and_local(control):
    control.fetch.return_value = MEDIA_ROWS
    seen = {}

    def choose(labels, **kwargs):
        seen["labels"] = labels
        return labels[1]

    with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
            mock.patch.object(cli, "select_dialog", side_effect=choose):
        cli._cmd_model_media("image", [], {})
    ids = [label for label, _desc in seen["labels"]]
    assert any("img-a" in label for label in ids)
    assert any("img-b" in label for label in ids)
    assert not any("vid-a" in label for label in ids)
    descriptions = " ".join(desc for _label, desc in seen["labels"])
    assert "gateway default" in descriptions
    assert "your machine" in descriptions
    assert loop.media_model_override("image") == "img-a"
    assert loop.media_generation_enabled("image")


def test_picker_off_keeps_saved_model(control):
    cli._cmd_model_media("video", ["vid-a"], {})
    control.fetch.return_value = MEDIA_ROWS
    with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
            mock.patch.object(cli, "select_dialog", side_effect=lambda labels, **kw: labels[0]):
        cli._cmd_model_media("video", [], {})
    assert not loop.media_generation_enabled("video")
    assert loop.media_model_override("video") == "vid-a"


def test_fetch_media_models_parses_gateway_rows(control):
    resp = SimpleNamespace(status_code=200, text="",
                           json=lambda: {"mediaModels": MEDIA_ROWS + [{"nope": 1}]})
    with mock.patch.object(cli, "get_backend_profile",
                           return_value=SimpleNamespace(base_url="https://example.test")), \
            mock.patch.object(cli.backend_profiles, "request_auth", return_value=({}, {})), \
            mock.patch.object(cli.requests, "get", return_value=resp) as get:
        rows = _real_fetch_media_models({})
    assert [row["id"] for row in rows] == ["img-a", "img-b", "vid-a"]
    assert get.call_args[0][0].endswith("/api/models")


def _fake_image_response():
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"images": ["https://img.test/1.png"], "model": "img-a",
                      "billing": {"costFormatted": "$0.01"}})


def test_image_tool_refuses_when_off_without_any_request(control):
    loop.set_runtime_config("image_enabled", False)
    tools.register_builtin_tools()
    with mock.patch("requests.post") as post:
        out = tools.get_registry().invoke(
            "media.generate_image", {"prompt": "a cat"}, tools.ToolCtx(session={}))
    assert not out["ok"]
    assert out["error"] == ("Image generation is off. "
                            "Run /model image on to enable it.")
    post.assert_not_called()


def test_image_tool_sends_chosen_model(control):
    loop.set_runtime_config("image_model", "img-a")
    tools.register_builtin_tools()
    with mock.patch("requests.post", return_value=_fake_image_response()) as post:
        out = tools.get_registry().invoke(
            "media.generate_image", {"prompt": "a cat"}, tools.ToolCtx(session={}))
    assert out["ok"]
    assert post.call_args.kwargs["json"]["model"] == "img-a"


def test_image_tool_omits_model_when_unset(control):
    tools.register_builtin_tools()
    with mock.patch("requests.post", return_value=_fake_image_response()) as post:
        out = tools.get_registry().invoke(
            "media.generate_image", {"prompt": "a cat"}, tools.ToolCtx(session={}))
    assert out["ok"]
    assert "model" not in post.call_args.kwargs["json"]


def test_video_tool_refuses_when_off_without_any_request(control):
    loop.set_runtime_config("video_enabled", False)
    tools.register_builtin_tools()
    with mock.patch("requests.post") as post:
        out = tools.get_registry().invoke(
            "media.generate_video", {"prompt": "waves"}, tools.ToolCtx(session={}))
    assert not out["ok"]
    assert out["error"] == ("Video generation is off. "
                            "Run /model video on to enable it.")
    post.assert_not_called()


def test_video_tool_sends_chosen_model(control):
    loop.set_runtime_config("video_model", "vid-a")
    tools.register_builtin_tools()
    created = SimpleNamespace(status_code=200, json=lambda: {"taskId": "t1"})
    done = SimpleNamespace(status_code=200, json=lambda: {
        "status": "succeeded", "videoUrl": "https://vid.test/1.mp4",
        "model": "vid-a", "billing": {"costFormatted": "$0.10"}})
    with mock.patch.object(tools, "_VIDEO_POLL_INTERVAL_S", 0), \
            mock.patch("requests.post", return_value=created) as post, \
            mock.patch("requests.get", return_value=done):
        out = tools.get_registry().invoke(
            "media.generate_video", {"prompt": "waves"}, tools.ToolCtx(session={}))
    assert out["ok"]
    assert post.call_args.kwargs["json"]["model"] == "vid-a"


def test_disabled_media_tools_leave_the_advertised_list(control):
    authorized = {"media.generate_image", "media.generate_video", "fs.read"}
    loop.set_runtime_config("dynamic_context", False)
    loop.set_runtime_config("image_enabled", False)
    visible = loop._visible_tool_names_for_task("anything", {}, set(authorized))
    assert visible == {"media.generate_video", "fs.read"}
    loop.set_runtime_config("image_enabled", True)
    loop.set_runtime_config("video_enabled", False)
    visible = loop._visible_tool_names_for_task("anything", {}, set(authorized))
    assert visible == {"media.generate_image", "fs.read"}
