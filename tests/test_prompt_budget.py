"""The layered budget: floors without ceilings, levels that stay dormant until
their parent overflows, level-2 tags kept and deeper markers removed."""
import pytest

import prompt_budget as pb


def count(text):
    return len(text) // 4


def tree(**system_children):
    raw = {"name": "system", "share": 0.1,
           "children": system_children or {
               "keep": {"share": 0.2, "shrink": "none"},
               "big": {"share": 0.5},
               "small": {"share": 0.3},
           }}
    return pb._build("system", "system", raw, "truncate")


def block(name, size, inner=""):
    return f"<{name}>\n" + (inner or ("x" * size * 4)) + f"\n</{name}>\n"


def test_nothing_changes_while_the_parent_fits():
    text = block("keep", 50) + block("big", 400) + block("small", 30)
    rows = []
    out = pb.fit(text, tree(), 10_000, count, depth=1, state={}, rows=rows)
    assert out == text
    assert not rows[0].triggered


def test_over_budget_compresses_in_proportion_and_keeps_fixed_blocks():
    text = block("keep", 50) + block("big", 400) + block("small", 30)
    out = pb.fit(text, tree(), 300, count, depth=1, state={})
    segments = {s.node.name: s for s in pb.split(out, tree()) if s.node}
    assert "x" * 200 in segments["keep"].inner          # fixed block untouched
    assert segments["small"].inner.count("x") == 30 * 4  # under its share: untouched
    assert count(segments["big"].inner) <= 300 - 50 - 30 + 2
    assert "compressed to fit its budget" in segments["big"].inner


def test_level_two_tags_stay_and_level_three_markers_are_removed():
    children = {"capabilities": {"share": 0.5, "children": {"tools": {"share": 1.0}}}}
    text = "<capabilities>\nuse <tools>tool one</tools> wisely\n</capabilities>\n"
    out = pb.fit(text, tree(**children), 10_000, count, depth=1, state={})
    assert out == "<capabilities>\nuse tool one wisely\n</capabilities>\n"


def test_unconfigured_and_unclosed_tags_are_plain_text():
    text = "<terminal_output_style>\nplain\n</terminal_output_style>\n<big>\nhalf"
    out = pb.fit(text, tree(), 10_000, count, depth=1, state={})
    assert out == text


def test_a_hidden_extra_child_is_ceded_first():
    children = {"body": {"share": 1.0}, "gateway": {"share": 0.0, "hidden": True}}
    text = block("body", 90)
    rows = []
    out = pb.fit(text, tree(**children), 100, count, depth=1, state={},
                 extra={"gateway": 30}, rows=rows)
    assert pb.ceded(rows, "system.gateway")
    assert out == text                                   # body alone fits


def test_floors_hold_even_past_the_budget():
    children = {"a": {"share": 0.1, "min_tokens": 80}, "b": {"share": 0.9}}
    text = block("a", 100) + block("b", 100)
    out = pb.fit(text, tree(**children), 100, count, depth=1, state={})
    segs = {s.node.name: s for s in pb.split(out, tree(**children)) if s.node}
    assert count(segs["a"].inner) >= 78


def test_hysteresis_keeps_a_block_compressed_just_below_the_line():
    state = {}
    assert pb.triggered(state, "p", 110, 100, 0.9)
    assert pb.triggered(state, "p", 95, 100, 0.9)
    assert not pb.triggered(state, "p", 80, 100, 0.9)


def test_compression_is_deterministic():
    text = block("keep", 50) + block("big", 400) + block("small", 30)
    first = pb.fit(text, tree(), 300, count, depth=1, state={})
    second = pb.fit(text, tree(), 300, count, depth=1, state={})
    assert first == second


def test_output_reserve_is_share_with_floor_and_never_above_the_model_ceiling():
    trees = pb.load()
    assert pb.split_window(trees, 1_000_000, 65_536) == (65_536, 1_000_000 - 65_536)
    assert pb.split_window(trees, 131_072, 65_536)[0] == 32_768
    assert pb.split_window(trees, 64_000, 8_192)[0] == 8_192


def test_every_config_key_round_trips_and_the_gateway_is_not_configurable():
    defaults = pb.config_defaults()
    assert "budget.system.share" in defaults
    assert "budget.system.capabilities.tools.share" in defaults
    assert "budget.thread.compact_background" in defaults
    assert not any(".gateway" in key for key in defaults)
    overrides = dict(defaults, **{"budget.system.share": 0.3})
    trees = pb.load(overrides.get)
    assert trees["system"].share == 0.3


@pytest.mark.parametrize("key,value", [
    ("budget.system.share", 1.5), ("budget.system.min", -1),
    ("budget.system.shrink", "squash")])
def test_invalid_values_are_rejected(key, value):
    with pytest.raises(ValueError):
        pb.validate(key, value)


def test_fit_items_drops_from_the_end_but_never_the_core():
    items = [("a", 10), ("b", 10), ("c", 10), ("d", 10)]
    assert pb.fit_items(items, 25, keep={"d"}) == ["a", "d"]


# ── Wired into the loop ─────────────────────────────────────────────────────

from unittest import mock

import agent_loop as loop


@pytest.fixture
def input_budget(monkeypatch):
    def set_reserved(tokens):
        monkeypatch.setattr(loop, "compaction_budget", lambda state=None: {
            "window": tokens * 2, "output": tokens, "reserved": tokens,
            "overhead": 0, "usable": tokens})
    loop.reset_runtime_config()
    loop._gateway_injectable.clear()
    yield set_reserved
    loop._gateway_injectable.clear()
    loop.reset_runtime_config()


SYSTEM = ("<platform_safety_policy>\nsafe\n</platform_safety_policy>\n<user_customization>\n"
          "<context>\nMemory: " + loop._budget_slot_marker("persistentMemory", "m " * 3000)
          + "\nRules: " + loop._budget_slot_marker("durableRules", "- [r1] never deploy")
          + "\n</context>\n<execution>\n" + "work carefully\n" * 400 + "</execution>\n"
          "</user_customization>")


def test_a_prompt_that_fits_is_sent_as_is_minus_the_markers(input_budget):
    input_budget(1_000_000)
    state = {}
    out = loop.fit_system_prompt(SYSTEM, state)
    assert "<persistentMemory>" not in out and "<durableRules>" not in out
    assert out == SYSTEM.replace("<persistentMemory>", "").replace("</persistentMemory>", "") \
        .replace("<durableRules>", "").replace("</durableRules>", "")
    assert loop.wire_prompt_budget(state) == {}


def test_an_oversized_prompt_cedes_the_gateway_first_then_compresses(input_budget):
    input_budget(10_000)                      # system share 15% -> 1,500 tokens
    loop._gateway_injectable[loop._provider_window_key()] = 400
    state = {}
    out = loop.fit_system_prompt(SYSTEM, state)
    assert loop.wire_prompt_budget(state) == {"gateway": 0}
    assert "- [r1] never deploy" in out                       # durable rules never cut
    assert "<execution>" in out and "</execution>" in out     # level-2 tags kept
    assert "compressed to fit its budget" in out
    assert loop._count_prompt_tokens(out) < loop._count_prompt_tokens(SYSTEM)
    rows = state["_budget_rows"]["system"]
    assert not any("gateway" in row["path"] for row in rows)  # never shown


def test_the_gateway_alone_can_tip_the_prompt_over(input_budget):
    input_budget(10_000)
    loop.set_runtime_config("budget.system.share", 0.9)
    size = loop._count_prompt_tokens(loop.fit_system_prompt(SYSTEM, {}))
    loop.set_runtime_config("budget.system.share", (size + 50) / 10_000)
    loop._gateway_injectable[loop._provider_window_key()] = 200
    state = {}
    out = loop.fit_system_prompt(SYSTEM, state)
    assert loop.wire_prompt_budget(state) == {"gateway": 0}
    assert "compressed to fit its budget" not in out          # ceding was enough


def test_tool_narrowing_keeps_the_core_surface(input_budget):
    input_budget(2_000)                       # tools share 10% -> 200 tokens
    names = {"fs.read", "shell.exec", "browser.open", "browser.click", "hwo"}
    state = {}
    kept = loop.fit_tool_names(names, state)
    assert {"fs.read", "shell.exec"} <= kept
    assert kept < names
    input_budget(10_000_000)
    assert loop.fit_tool_names(names, {}) == names


def test_live_tail_compresses_bulky_blocks_and_keeps_the_task(input_budget):
    input_budget(4_000)                       # live share 5% -> 200 tokens
    text = ("<task>\nfix the build\n</task>\n<session_memory>\n" + "note\n" * 900
            + "</session_memory>\n<now>\n2026-09-19\n</now>\n")
    out = loop.fit_live_state(text, {})
    assert "fix the build" in out and "2026-09-19" in out
    assert out.count("note") < 900


# ── Seeing it: /prop budget and the agent's report ──────────────────────────

import prop_ui


ROWS = {"system": [
    {"path": "system", "depth": 1, "natural": 900, "allotted": 500, "delivered": 480,
     "triggered": True, "shrink": "truncate", "original": "O" * 50, "compressed": "C" * 20},
    {"path": "system.execution", "depth": 2, "natural": 600, "allotted": 200,
     "delivered": 190, "triggered": True, "shrink": "truncate",
     "original": "long original", "compressed": "short sent"},
    {"path": "system.gateway", "depth": 2, "natural": 30, "allotted": 0,
     "delivered": 0, "triggered": True, "shrink": "drop"},
]}


def test_budget_view_lists_blocks_sent_first_and_never_the_gateway():
    items = prop_ui.budget_items(ROWS)
    assert [i.title.strip() for i in items] == ["system", "system.execution"]
    detail = items[1].payload["content"]
    assert detail.index("short sent") < detail.index("long original")
    assert items[1].badge == "compressed"


@pytest.mark.parametrize("raw,expected", [("budget", 1), ("budget 3", 3), ("sys", None)])
def test_prop_budget_arguments(raw, expected):
    assert prop_ui.parse_budget_target(raw) == expected


def test_the_agent_report_is_written_once_and_announced_once(tmp_path, monkeypatch):
    monkeypatch.setattr(loop.paths, "project_dir", lambda: tmp_path)
    printed = []
    deps = mock.Mock()
    deps.console.print = printed.append
    state = {"_budget_rows": ROWS, "_agent_id": "primary"}
    loop.publish_budget_report(state, deps)
    report = (tmp_path / "budget" / "primary.md").read_text()
    assert "system.execution" in report and "short sent" in report
    assert "gateway" not in report
    loop.publish_budget_report(state, deps)             # unchanged: silent
    assert len(printed) == 1
    state["_budget_rows"] = {}
    loop.publish_budget_report(state, deps)             # released: file removed
    assert not (tmp_path / "budget" / "primary.md").exists()


# ── /config budget: one word per level ───────────────────────────────────────

import laintas_cli


@pytest.mark.parametrize("words,expected", [
    (["budget", "system", "share"], ("budget.system.share", None, None)),
    (["budget", "system", "share", "0.3"], ("budget.system.share", "0.3", None)),
    (["budget", "system"], (None, None, "budget.system")),
    (["budget.system.share"], ("budget.system.share", None, None)),
])
def test_budget_config_words_resolve_level_by_level(words, expected):
    assert laintas_cli._resolve_budget_words(words, loop.describe_runtime_config()) == expected


def test_budget_keys_are_shown_with_spaces_and_others_untouched():
    assert laintas_cli._config_display_key("budget.tools.share") == "budget tools share"
    assert laintas_cli._config_display_key("compact_background") == "compact_background"


def test_setting_a_budget_level_with_spaces(monkeypatch):
    monkeypatch.setattr(laintas_cli.terminal_preferences, "set_ui_preference", lambda k, v: None)
    try:
        laintas_cli._cmd_config(["/config", "budget", "tools", "share", "0.2"])
        assert loop.get_runtime_config("budget.tools.share") == 0.2
    finally:
        loop.reset_runtime_config()


def test_config_completion_walks_the_budget_one_level_at_a_time():
    completer = laintas_cli.MetaCompleter()
    from prompt_toolkit.document import Document
    words = [c.text for c in completer.get_completions(Document("/config budget "), None)]
    assert "system" in words and "tools" in words and "share" not in words
    words = [c.text for c in completer.get_completions(Document("/config budget system capabilities "), None)]
    assert "share" in words and "tools" in words
    words = [c.text for c in completer.get_completions(Document("/config budget tools shrink "), None)]
    assert "narrow" in words


# ── The tuning page and its import ──────────────────────────────────────────

import budget_page
import config_file


@pytest.fixture
def quiet_prefs(monkeypatch):
    saved = {}
    monkeypatch.setattr(laintas_cli.terminal_preferences, "get_ui_preferences", lambda: dict(saved))
    monkeypatch.setattr(laintas_cli.terminal_preferences, "update", lambda values, **k: saved.update(values.get("ui", {})))
    monkeypatch.setattr(laintas_cli.terminal_preferences, "set_ui_preference", lambda k, v: saved.__setitem__(k, v))
    loop.reset_runtime_config()
    yield saved
    loop.reset_runtime_config()


def test_a_config_file_holds_any_setting_one_line_each(tmp_path, quiet_prefs):
    path = tmp_path / "b.config"
    path.write_text(
        "# from the budget page\n"
        "budget system share 0.3\n"
        "/config budget thread compact_background 0.45   # a pasted command line\n"
        "budget thread compact_target 0.4\n"
        "compact_background false\n"
        "search_engine 'cn-bing duckduckgo'\n")
    laintas_cli._cmd_config(["/config", "import", str(path)])
    assert loop.get_runtime_config("budget.system.share") == 0.3
    assert loop.get_runtime_config("budget.thread.compact_background") == 0.45
    assert loop.get_runtime_config("compact_background") is False
    assert loop.get_runtime_config("search_engine") == "cn-bing duckduckgo"
    assert quiet_prefs["budget.system.share"] == 0.3              # persisted like /config


def test_one_bad_line_changes_nothing_and_is_named(tmp_path, quiet_prefs, capsys):
    path = tmp_path / "b.config"
    path.write_text("budget system share 0.3\ncompact_background false\nbudget tools share 7\n")
    printed = []
    laintas_cli.console.print = lambda *a, **k: printed.append(" ".join(map(str, a)))
    try:
        laintas_cli._cmd_config(["/config", "import", str(path)])
    finally:
        del laintas_cli.console.print
    assert loop.get_runtime_config("budget.system.share") == 0.15
    assert loop.get_runtime_config("compact_background") is True
    assert quiet_prefs == {}
    assert any("line 3" in line and "budget tools share" in line for line in printed)


@pytest.mark.parametrize("text", ["budget system gateway share 0.5\n", "no_such_setting 1\n",
                                  "budget system share\n", "# only a comment\n"])
def test_unknown_or_incomplete_lines_are_refused(text):
    with pytest.raises(ValueError):
        config_file.parse(text, loop.describe_runtime_config())


def test_export_then_import_round_trips(tmp_path, quiet_prefs):
    laintas_cli._cmd_config(["/config", "budget", "tools", "share", "0.2"])
    loop.set_runtime_config("search_engine", "cn-bing duckduckgo")
    path = tmp_path / "all.config"
    laintas_cli._cmd_config(["/config", "export", str(path)])
    text = path.read_text()
    assert "budget tools share 0.2" in text and "search_engine 'cn-bing duckduckgo'" in text
    loop.reset_runtime_config()
    laintas_cli._cmd_config(["/config", "import", str(path)])
    assert loop.get_runtime_config("budget.tools.share") == 0.2
    assert loop.get_runtime_config("search_engine") == "cn-bing duckduckgo"


def test_budget_reset_restores_the_shipped_tree(quiet_prefs):
    laintas_cli._cmd_config(["/config", "budget", "system", "share", "0.3"])
    laintas_cli._cmd_config(["/config", "budget", "reset"])
    assert loop.get_runtime_config("budget.system.share") == 0.15


def test_prop_budget_output_writes_a_page_without_the_gateway(tmp_path, monkeypatch):
    rows = dict(ROWS, tools=[{"path": "tools", "depth": 1, "natural": 20, "allotted": 10,
                              "delivered": 10, "triggered": True, "shrink": "narrow",
                              "original": "a\nb", "compressed": "a",
                              "items": [{"name": "a", "tokens": 10, "core": True, "kept": True}]}])
    monkeypatch.setattr(laintas_cli.handle_meta_command, "_last_agent_state",
                        {"_budget_rows": rows, "_budget_window": {"model": "m", "window": 1000}},
                        raising=False)
    monkeypatch.setattr(laintas_cli.context_snapshot if hasattr(laintas_cli, "context_snapshot") else
                        __import__("context_snapshot"), "load_conversation",
                        lambda *a, **k: (_ for _ in ()).throw(__import__("context_snapshot").ContextSnapshotNotFound()))
    target = tmp_path / "page.html"
    laintas_cli._cmd_prop(f"budget output {target}", {})
    page = target.read_text()
    assert "system.execution" in page and '"model": "m"' in page and '"mode": "cli"' in page
    assert "gateway" not in page.split('<script id="data"')[1].split("</script>")[0]


@pytest.mark.parametrize("raw,index,output", [
    ("budget", 1, None), ("budget 2 output", 2, ""), ("budget output /tmp/x.html", 1, "/tmp/x.html")])
def test_prop_budget_output_arguments(raw, index, output):
    assert prop_ui.parse_budget_target(raw) == index
    assert prop_ui.budget_output_path(raw) == output


# ── Every changed value is a /config setting ────────────────────────────────

@pytest.mark.parametrize("words,key,value", [
    (["budget", "assumed_window", "80000"], "budget.assumed_window", 80000),
    (["budget", "assumed_summary_window", "40000"], "budget.assumed_summary_window", 40000),
    (["budget", "chars_per_token", "3"], "budget.chars_per_token", 3.0),
    (["budget", "thread", "grep_line", "share", "0.001"], "budget.thread.grep_line.share", 0.001),
])
def test_former_constants_are_settable_level_by_level(words, key, value, quiet_prefs):
    laintas_cli._cmd_config(["/config", *words])
    assert loop.get_runtime_config(key) == value


def test_the_assumed_window_and_chars_per_token_take_effect(quiet_prefs, monkeypatch):
    monkeypatch.setattr(loop, "_provider_context_window", 0)
    monkeypatch.setattr(loop, "_load_remembered_provider_window", lambda: None)
    loop.set_runtime_config("budget.assumed_window", 80000)
    assert loop._effective_context_window() == 80000
    before = loop.thread_chars("tool_result", {})
    loop.set_runtime_config("budget.chars_per_token", 7)
    assert loop.thread_chars("tool_result", {}) == 2 * before


@pytest.mark.parametrize("key,value", [("budget.assumed_window", 0), ("budget.chars_per_token", -1)])
def test_scalar_values_are_validated(key, value):
    with pytest.raises(ValueError):
        pb.validate(key, value)


def test_scalars_round_trip_through_a_config_file(tmp_path, quiet_prefs):
    path = tmp_path / "s.config"
    path.write_text("budget assumed_window 100000\nbudget chars_per_token 4\n")
    laintas_cli._cmd_config(["/config", "import", str(path)])
    assert loop.budget_scalar("assumed_window") == 100000
    assert loop.budget_scalar("chars_per_token") == 4.0


def test_the_page_carries_scalars_without_breaking():
    data = budget_page.page_data(ROWS, {"model": "m", "window": 1000},
                                 loop.describe_runtime_config(), newest_index=1)
    assert {"assumed_window", "assumed_summary_window", "chars_per_token"} <= set(data["scalars"])
    assert "thread.grep_line" in data["nodes"]
