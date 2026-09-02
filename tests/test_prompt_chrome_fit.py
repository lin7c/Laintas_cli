"""The rprompt must never be physically wider than the terminal.

Background: the prompt used to stack copies of itself down the screen. The
mechanism was a width miscount — prompt_toolkit measures East-Asian AMBIGUOUS
characters as one column, while a CJK-configured terminal may draw them as
two. A right-aligned prompt containing such characters can run past the edge,
and every resize repaint then lands on a fresh row.

These tests pin both halves of the fix: worst-case fitting, and the reserved
final column.
"""
import unittest
from unittest import mock

import laintas_cli
import symbols


def _text(fragments) -> str:
    return "".join(fragment[1] for fragment in fragments)


class PessimisticWidthTests(unittest.TestCase):
    def test_ambiguous_width_characters_count_as_two(self):
        # The exact glyphs our chrome is built from.
        self.assertEqual(laintas_cli._pessimistic_width(symbols.BULLET), 2)
        self.assertEqual(laintas_cli._pessimistic_width(symbols.ARROW_U), 2)
        self.assertEqual(laintas_cli._pessimistic_width("ACT"), 3)
        # A separator-joined segment: 3 + (1+2+1) + 3
        self.assertEqual(
            laintas_cli._pessimistic_width(f"ACT {symbols.BULLET} act"), 10)

    def test_wide_and_narrow_characters(self):
        self.assertEqual(laintas_cli._pessimistic_width("中文"), 4)
        self.assertEqual(laintas_cli._pessimistic_width("abc"), 3)

    def test_never_underestimates_prompt_toolkit(self):
        from prompt_toolkit.utils import get_cwidth
        for sample in ("primary · ACT · glm-5.2", "ACT", "中文 · ACT",
                       f"a{symbols.BULLET}b", "primary@term0"):
            self.assertGreaterEqual(laintas_cli._pessimistic_width(sample),
                                    get_cwidth(sample), sample)


class RpromptFitTests(unittest.TestCase):
    @staticmethod
    def _render(width, path, model="glm-5.2", agent="primary",
                terminal="term0", multi=False):
        laintas_cli._status_cache.update(
            model=model, agent=agent, terminal=terminal,
            multi_agent=multi, prompt_path=path)
        with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                  return_value={"name": "act"}), \
                mock.patch.object(laintas_cli, "_terminal_width",
                                  return_value=width):
            return _text(laintas_cli._render_rprompt())

    def test_fits_inside_the_terminal_on_a_cjk_terminal(self):
        """Worst case: every ambiguous glyph drawn double, path row included."""
        cases = [
            (100, "~"), (120, "~/laintas_cli"), (80, "~"), (78, "~"),
            (70, "~"), (62, "~"), (55, "~"), (48, "~"),
            (110, "~/some/deep/project/path"),
            (100, "~/a-very-long-working-directory-name-goes-here"),
            # A wide-character path: two cells per glyph, so a
            # character-counting shortener would leave the row too long.
            (90, "~/中文目录/项目"),
        ]
        for width, path in cases:
            with self.subTest(width=width, path=path):
                rendered = self._render(width, path)
                used = laintas_cli._pessimistic_width("  " + path)
                total = used + laintas_cli._pessimistic_width(rendered)
                self.assertLessEqual(
                    total, width,
                    f"rprompt overflows: {total} > {width} for {rendered!r}")

    def test_right_alignment_uses_only_unambiguous_width_chrome(self):
        """Physical and prompt_toolkit widths must agree after right alignment.

        Merely fitting ``path + pessimistic(rprompt)`` is insufficient. The
        toolkit chooses the rprompt's starting column using its own wcwidth;
        any wider CJK rendering then extends beyond the terminal edge.
        Exercise a resize sequence because each overflow used to leave one
        more copy of the row in scrollback.
        """
        from prompt_toolkit.utils import get_cwidth

        for width in range(120, 39, -1):
            with self.subTest(width=width):
                rendered = self._render(width, "~")
                self.assertEqual(
                    laintas_cli._pessimistic_width(rendered),
                    get_cwidth(rendered),
                    f"right-aligned chrome has ambiguous width: {rendered!r}")

    def test_last_column_is_always_reserved(self):
        for width in range(40, 130, 7):
            with self.subTest(width=width):
                self.assertTrue(self._render(width, "~").endswith(" "))

    def test_never_ends_on_a_dangling_separator(self):
        for width in range(40, 130):
            rendered = self._render(width, "~").rstrip()
            with self.subTest(width=width):
                self.assertFalse(
                    rendered.endswith(laintas_cli._RPROMPT_SEPARATOR.strip()),
                    rendered)

    def test_still_discloses_progressively_when_there_is_room(self):
        self.assertNotIn("glm-5.2", self._render(62, "~"))
        self.assertIn("glm-5.2", self._render(100, "~"))
        self.assertIn("primary@term0", self._render(120, "~"))

    def test_long_path_sheds_rprompt_segments_rather_than_overflowing(self):
        """When the path eats the row, segments are dropped — not overflowed.

        `_shorten_path` caps the displayed path at 60 columns, so a wide
        terminal always has room for the full rprompt; the squeeze only bites
        on a narrower one.
        """
        roomy = self._render(80, "~")
        cramped = self._render(80, "~/" + "d" * 58)
        self.assertLess(laintas_cli._pessimistic_width(cramped),
                        laintas_cli._pessimistic_width(roomy))
        # And the cramped render still fits.
        used = laintas_cli._pessimistic_width("  ~/" + "d" * 58)
        self.assertLessEqual(
            used + laintas_cli._pessimistic_width(cramped), 80)


class RenderCallbackPurityTests(unittest.TestCase):
    def test_rprompt_never_writes_to_the_console(self):
        """A render callback that prints corrupts prompt_toolkit's screen diff."""
        laintas_cli._status_cache.update(prompt_path="~", model="glm-5.2")
        laintas_cli._session_approval_state["all_commands"] = True
        laintas_cli._approval_star_announced = False
        laintas_cli._approval_star_pending = False
        try:
            with mock.patch("plan_mode.is_plan_mode", return_value=False), \
                    mock.patch.object(laintas_cli.mode_manager, "get_active_mode",
                                      return_value={"name": "act"}), \
                    mock.patch.object(laintas_cli, "_terminal_width",
                                      return_value=100), \
                    mock.patch.object(laintas_cli.console, "print") as printed:
                rendered = _text(laintas_cli._render_rprompt())
            printed.assert_not_called()
            self.assertIn("ACT*", rendered)
            self.assertTrue(laintas_cli._approval_star_pending)

            # The notice is not lost — it is emitted between prompts instead.
            with mock.patch.object(laintas_cli.console, "print") as printed:
                laintas_cli._flush_deferred_notices()
            printed.assert_called_once()
            self.assertFalse(laintas_cli._approval_star_pending)
        finally:
            laintas_cli._reset_session_approvals()


if __name__ == "__main__":
    unittest.main()
