"""Tests for the /config markdown_theme presets, palette merging, and
custom-file fallback logic."""
import json
from unittest import mock

import pytest

import agent_loop
import laintas_cli


# ---------- config validation ----------

def test_markdown_theme_accepts_valid_presets():
    for value in ("default", "green-red", "custom"):
        assert agent_loop._coerce_runtime_config_value("markdown_theme", value) == value


def test_markdown_theme_rejects_unknown_values():
    for value in ("green_red", "neon", ""):
        with pytest.raises(ValueError):
            agent_loop._coerce_runtime_config_value("markdown_theme", value)


def test_markdown_theme_normalizes_case_and_whitespace():
    assert agent_loop._coerce_runtime_config_value("markdown_theme", " Green-Red ") == "green-red"
    assert agent_loop._coerce_runtime_config_value("markdown_theme", "DEFAULT") == "default"


# ---------- preset integrity & palette ----------

def test_bundled_presets_are_complete_and_nonempty():
    required = laintas_cli._MARKDOWN_REQUIRED_KEYS
    for name, preset in laintas_cli._MARKDOWN_THEMES.items():
        assert required.issubset(preset), f"{name} missing keys"
        assert set(laintas_cli._MARKDOWN_THEMES) == {"default", "green-red"}


def test_load_palette_builtin_presets():
    default = laintas_cli._load_markdown_palette("default")
    green_red = laintas_cli._load_markdown_palette("green-red")
    assert default["h1"] == ""
    assert "#4ade80" in green_red["h1"]      # green titles
    assert "#f85149" in green_red["h3"]      # red sub-titles


def test_load_palette_unknown_name_falls_back_to_default():
    palette = laintas_cli._load_markdown_palette("does-not-exist")
    assert palette == laintas_cli._load_markdown_palette("default")


def test_theme_styles_maps_to_rich_keys_and_skips_empty():
    styles = laintas_cli._markdown_theme_styles(
        laintas_cli._load_markdown_palette("green-red"))
    assert styles["markdown.h1"] == "bold #4ade80"
    assert "markdown.strong" not in styles      # empty values omitted
    assert "markdown.block_quote" not in styles


# ---------- custom JSON loading & fallback ----------

def _patched_home(tmp_path, monkeypatch):
    monkeypatch.setattr(laintas_cli.paths, "LAINTAS_HOME", tmp_path)
    return tmp_path / "markdown_theme.json"


def test_custom_merges_valid_json(tmp_path, monkeypatch):
    theme_file = _patched_home(tmp_path, monkeypatch)
    theme_file.write_text(json.dumps({
        "h1": "bold #ff0000",
        "h2": "#00ff00",
        "unknown_key": "ignored",          # forward-compat
    }))
    palette = laintas_cli._load_markdown_palette("custom")
    assert palette["h1"] == "bold #ff0000"
    assert palette["h2"] == "#00ff00"
    assert palette["h3"] == ""             # untouched keys keep default


def test_custom_skips_invalid_style_values(tmp_path, monkeypatch):
    theme_file = _patched_home(tmp_path, monkeypatch)
    theme_file.write_text(json.dumps({
        "h1": "bold #ff0000",     # valid
        "h2": "not-a-style(((" ,  # invalid -> skipped per-key
    }))
    palette = laintas_cli._load_markdown_palette("custom")
    assert palette["h1"] == "bold #ff0000"  # valid key survives
    assert palette["h2"] == ""              # invalid key falls back


def test_custom_missing_file_falls_back_silently(tmp_path, monkeypatch):
    _patched_home(tmp_path, monkeypatch)  # no file created
    palette = laintas_cli._load_markdown_palette("custom")
    assert palette == laintas_cli._load_markdown_palette("default")


def test_custom_broken_json_falls_back(tmp_path, monkeypatch):
    theme_file = _patched_home(tmp_path, monkeypatch)
    theme_file.write_text("{ not json")
    palette = laintas_cli._load_markdown_palette("custom")
    assert palette == laintas_cli._load_markdown_palette("default")


def test_custom_non_object_json_falls_back(tmp_path, monkeypatch):
    theme_file = _patched_home(tmp_path, monkeypatch)
    theme_file.write_text(json.dumps(["h1", "red"]))
    palette = laintas_cli._load_markdown_palette("custom")
    assert palette == laintas_cli._load_markdown_palette("default")
