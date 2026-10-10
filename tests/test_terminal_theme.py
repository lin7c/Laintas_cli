"""Text colors follow the terminal background and never repaint it."""
import io

import pytest
from rich.color import ColorType
from rich.console import Console
from rich.style import Style
from rich.theme import Theme

import agent_loop
import append_input
import terminal_theme


def _luminance(hexcode: str) -> float:
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(int(hexcode[i:i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


TEXT_ROLES = ("muted", "subtle", "success", "warning", "error", "info", "agent", "accent")
# Backgrounds people actually run: black, common editor/terminal defaults,
# Solarized, Ubuntu aubergine; white, light grey, Solarized light.
BACKGROUNDS = {
    "dark": ("#000000", "#0c0c0c", "#1e1e1e", "#282c34", "#002b36", "#300a24"),
    "light": ("#ffffff", "#f5f5f5", "#fdf6e3", "#eeeeee"),
}


@pytest.mark.parametrize("mode", ("dark", "light"))
def test_text_roles_stay_readable_on_real_terminal_backgrounds(mode):
    for role in TEXT_ROLES:
        for background in BACKGROUNDS[mode]:
            ratio = _contrast(terminal_theme.color(role, mode), background)
            assert ratio >= 4.5, (mode, role, background, round(ratio, 2))


@pytest.mark.parametrize("mode", ("dark", "light"))
def test_painted_backgrounds_carry_their_own_readable_text(mode):
    palette = terminal_theme.PALETTES[mode]
    assert _contrast(palette["plate_fg"], palette["plate_bg"]) >= 7
    assert _contrast(palette["selected_fg"], palette["selected_bg"]) >= 7


def _console(mode: str) -> append_input.AppendConsole:
    theme = Theme(terminal_theme.rich_styles(mode))
    console = append_input.AppendConsole(
        file=io.StringIO(), force_terminal=True, color_system="truecolor",
        theme=theme, width=120, highlight=False)
    console.style_resolver = terminal_theme.StyleResolver(theme.styles, mode)
    return console


def _render(mode: str, markup: str) -> str:
    console = _console(mode)
    console.print(markup)
    return console.file.getvalue()


@pytest.mark.parametrize("mode", ("dark", "light"))
def test_legacy_markup_uses_the_palette_instead_of_ansi_colors(mode):
    output = _render(
        mode, "[dim]a[/dim][yellow]b[/yellow][red]c[/red][green]d[/green][cyan]e[/cyan]"
              "[dim yellow]f[/dim yellow][bold cyan]g[/bold cyan][blue]h[/blue]")
    # No faint attribute and no 16-color foreground survives.
    assert "\x1b[2m" not in output and ";2;38" not in output
    for code in range(30, 38):
        assert f"\x1b[{code}m" not in output and f"\x1b[1;{code}m" not in output
    muted = terminal_theme.color("muted", mode)
    red, green, blue = (int(muted[i:i + 2], 16) for i in (1, 3, 5))
    assert f"38;2;{red};{green};{blue}" in output


@pytest.mark.parametrize("mode", ("dark", "light"))
def test_body_text_and_white_keep_the_terminal_foreground(mode):
    assert _render(mode, "plain [white]white[/white] [foreground]body[/foreground]") == (
        "plain white body\n")
    assert _render(mode, "[bold white]title[/bold white]") == "\x1b[1mtitle\x1b[0m\n"


def test_ordinary_output_never_paints_a_background():
    for mode in ("dark", "light", "mono"):
        output = _render(mode, "[dim]a[/dim] [warning]b[/warning] [muted]c[/muted] plain")
        assert "48;2" not in output and "\x1b[4" not in output


def test_compound_styles_may_mix_semantic_names():
    console = _console("dark")
    style = console.get_style("bold accent")
    assert style.bold and style.color.get_truecolor().hex == terminal_theme.color("accent", "dark")


def test_explicit_colors_and_painted_backgrounds_pass_through():
    exact = Style.parse("#123456")
    assert terminal_theme.adapt(exact, "dark") is exact
    badge = Style.parse("black on #bb9af7")
    assert terminal_theme.adapt(badge, "light") is badge


def test_mono_carries_no_color():
    output = _render("mono", "[yellow]a[/yellow][red]b[/red][error]c[/error][accent]d[/accent]")
    assert "38;" not in output and "\x1b[3" not in output
    for style in terminal_theme.rich_styles("mono").values():
        style = style if isinstance(style, Style) else Style.parse(style)
        assert style.color is None or style.color.type != ColorType.STANDARD


def test_rich_default_styles_are_moved_onto_the_palette():
    styles = Theme(terminal_theme.rich_styles("light")).styles
    for name, style in styles.items():
        if style.bgcolor is None and not style.reverse:
            assert not style.dim, name
            assert style.color is None or style.color.type != ColorType.STANDARD, name


# ---------- terminal background ----------

def test_background_reply_parsing_and_classification():
    parse = terminal_theme.parse_background_reply
    assert terminal_theme.classify(parse(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07\x1b[?62;c")) == "dark"
    assert terminal_theme.classify(parse(b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")) == "light"
    assert terminal_theme.classify(parse(b"\x1b]11;rgb:fd/f6/e3\x07")) == "light"
    assert terminal_theme.classify(parse(b"\x1b]11;rgb:30/0a/24\x07")) == "dark"
    assert parse(b"\x1b[?1;2c") is None and parse(b"") is None


def test_colorfgbg_fallback():
    assert terminal_theme.from_colorfgbg("15;0") == "dark"
    assert terminal_theme.from_colorfgbg("0;default;15") == "light"
    assert terminal_theme.from_colorfgbg("0;7") == "light"
    assert terminal_theme.from_colorfgbg("") is None
    assert terminal_theme.from_colorfgbg("15;default") is None


@pytest.fixture
def fresh_detection(monkeypatch):
    monkeypatch.setattr(terminal_theme, "_probed", False)
    monkeypatch.setattr(terminal_theme, "_detected", None)
    monkeypatch.delenv("COLORFGBG", raising=False)


def test_auto_follows_the_terminal_and_asks_once(fresh_detection):
    calls = []

    def query():
        calls.append(1)
        return b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?1;2c"

    assert terminal_theme.detect_background(query=query) == "light"
    assert terminal_theme.detect_background(query=query) == "light"
    assert calls == [1]
    assert terminal_theme.resolve("auto") == "light"
    assert terminal_theme.resolve_quietly("auto") == "light"


def test_auto_falls_back_to_dark_when_the_terminal_is_silent(fresh_detection):
    assert terminal_theme.detect_background(query=lambda: b"") is None
    assert terminal_theme.resolve("auto") == "dark"


def test_explicit_themes_never_query_the_terminal(fresh_detection, monkeypatch):
    def fail():
        raise AssertionError("queried the terminal")

    monkeypatch.setattr(terminal_theme, "_query_terminal", fail)
    for name in ("dark", "light", "mono"):
        assert terminal_theme.resolve(name) == name
    with pytest.raises(ValueError):
        terminal_theme.resolve("neon")


def test_theme_setting_accepts_auto_and_defaults_to_it():
    assert agent_loop._DEFAULT_CONFIG["theme"] == "auto"
    for value in terminal_theme.THEMES:
        assert agent_loop._coerce_runtime_config_value("theme", value) == value
    with pytest.raises(ValueError):
        agent_loop._coerce_runtime_config_value("theme", "neon")


def test_hand_picked_theme_that_fights_the_terminal_is_called_out(fresh_detection, monkeypatch):
    import laintas_cli

    printed = []
    monkeypatch.setattr(laintas_cli.console, "print", lambda *a, **k: printed.append(str(a[0])))
    terminal_theme.detect_background(query=lambda: b"\x1b]11;rgb:0000/0000/0000\x07")
    laintas_cli._warn_theme_mismatch("dark")
    laintas_cli._warn_theme_mismatch("auto")
    laintas_cli._warn_theme_mismatch("mono")
    assert printed == []
    laintas_cli._warn_theme_mismatch("light")
    assert len(printed) == 1 and "/theme auto" in printed[0]


def test_slash_menu_keeps_its_established_dark_colors(monkeypatch):
    import laintas_cli

    monkeypatch.setattr(laintas_cli, "_ui_theme_mode", lambda: "dark")
    style = laintas_cli._build_prompt_style()
    row = style.get_attrs_for_style_str("class:completion-menu.completion")
    meta = style.get_attrs_for_style_str("class:completion-menu.meta.completion")
    current = style.get_attrs_for_style_str("class:completion-menu.completion.current")
    assert (row.color, row.bgcolor) == ("c9d1d9", "161b22")
    assert (meta.color, meta.bgcolor) == ("8b949e", "161b22")
    assert (current.color, current.bgcolor, current.bold) == ("c9d1d9", "21262d", True)


def test_startup_notice_is_shown_once(fresh_detection, monkeypatch, tmp_path):
    import laintas_cli

    printed = []
    monkeypatch.setattr(laintas_cli.console, "print", lambda *a, **k: printed.append(str(a[0])))
    monkeypatch.setattr(laintas_cli.paths, "LAINTAS_HOME", tmp_path)
    monkeypatch.setattr(laintas_cli.sys.stdout, "isatty", lambda: True, raising=False)
    terminal_theme.detect_background(query=lambda: b"\x1b]11;rgb:0000/0000/0000\x07")

    # A saved choice that suits the terminal says nothing and stays armed.
    laintas_cli._theme_startup_notice("dark", saved=True)
    assert printed == []
    laintas_cli._theme_startup_notice("light", saved=True)
    assert len(printed) == 1 and "/theme auto" in printed[0]
    laintas_cli._theme_startup_notice("light", saved=True)
    laintas_cli._theme_startup_notice("auto", saved=False)
    assert len(printed) == 1


def test_new_user_is_told_once_that_colors_follow_the_terminal(
        fresh_detection, monkeypatch, tmp_path):
    import laintas_cli

    printed = []
    monkeypatch.setattr(laintas_cli.console, "print", lambda *a, **k: printed.append(str(a[0])))
    monkeypatch.setattr(laintas_cli.paths, "LAINTAS_HOME", tmp_path)
    monkeypatch.setattr(laintas_cli.sys.stdout, "isatty", lambda: True, raising=False)
    terminal_theme.detect_background(query=lambda: b"\x1b]11;rgb:ffff/ffff/ffff\x07")

    laintas_cli._theme_startup_notice("auto", saved=False)
    laintas_cli._theme_startup_notice("auto", saved=False)
    assert len(printed) == 1 and "light terminal background" in printed[0]


def test_keys_typed_during_the_probe_are_kept(fresh_detection, monkeypatch):
    monkeypatch.setattr(terminal_theme, "_typeahead", "")
    reply = b"ls -la\rgit st\x1b[A\x1b]11;rgb:0000/0000/0000\x07atus\x1b[?62;c"
    assert terminal_theme.detect_background(query=lambda: reply) == "dark"
    assert terminal_theme.take_typeahead() == "ls -la\ngit status"
    assert terminal_theme.take_typeahead() == ""


def test_typeahead_is_resubmitted_and_the_unfinished_line_returns_to_the_prompt(monkeypatch):
    import laintas_cli

    submitted = []
    monkeypatch.setattr(laintas_cli, "_enqueue_user_input", submitted.append)
    monkeypatch.setattr(laintas_cli, "_pending_prompt_default", None, raising=False)
    monkeypatch.setattr(terminal_theme, "_typeahead", "ls -la\n\ngit st")
    laintas_cli._restore_typeahead()
    assert submitted == ["ls -la"]
    assert laintas_cli._pending_prompt_default == "git st"
