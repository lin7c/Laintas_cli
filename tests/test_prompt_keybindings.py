"""Input-line keybindings: undo/redo and accept-suggestion fallbacks.

Covers the bindings _build_keybindings() adds on top of prompt_toolkit's
defaults. The tests drive the real prompt_toolkit Buffer/Document objects
so they exercise the same code paths a live keypress does.
"""

import unittest
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.keys import Keys


def _press(buffer, handler, undo_stacks_before=True):
    """Simulate what prompt_toolkit's key processor does around a handler.

    By default every keypress saves the buffer to the undo stack first
    (undo_stacks_before=True); handlers registered with
    ``save_before=lambda e: False`` run without that pre-save, which is what
    keeps Ctrl+Y's redo stack intact.
    """
    if undo_stacks_before:
        buffer.save_to_undo_stack()
    handler()


class UndoRedoKeybindingTests(unittest.TestCase):
    def test_undo_reverts_keystrokes_and_redo_restores(self):
        buf = Buffer()
        insert = lambda ch: (buf.save_to_undo_stack(),
                            buf.insert_text(ch))
        for ch in "abc":
            insert(ch)
        self.assertEqual(buf.text, "abc")

        buf.undo()
        self.assertEqual(buf.text, "ab")
        buf.undo()
        self.assertEqual(buf.text, "a")

        buf.redo()
        self.assertEqual(buf.text, "ab")

    def test_undo_and_redo_on_empty_stacks_are_no_ops(self):
        buf = Buffer()
        buf.undo()   # must not raise
        buf.redo()   # must not raise
        self.assertEqual(buf.text, "")

    def test_undo_restores_cursor_position(self):
        buf = Buffer()
        for ch in "hello":
            buf.save_to_undo_stack()
            buf.insert_text(ch)
        buf.undo()
        buf.undo()
        self.assertEqual(buf.text, "hel")
        self.assertEqual(buf.cursor_position, 3)


class AcceptSuggestionFallbackTests(unittest.TestCase):
    def test_c_e_without_suggestion_moves_to_end_of_line(self):
        buf = Buffer()
        buf.insert_text("line1\nlin2")
        buf.cursor_position = 0
        # Exactly what the c-e fallback handler does.
        buf.cursor_position += buf.document.get_end_of_line_position()
        self.assertEqual(buf.cursor_position, 5)   # end of "line1"
        buf.cursor_position = 6
        buf.cursor_position += buf.document.get_end_of_line_position()
        self.assertEqual(buf.cursor_position, len("line1\nlin2"))

    def test_c_f_without_suggestion_moves_forward_one_char(self):
        buf = Buffer()
        buf.insert_text("abc")
        buf.cursor_position = 0
        # event.arg is None when no numeric prefix was typed; the handler
        # passes it through as (arg or 1).
        buf.cursor_right(count=(None or 1))
        self.assertEqual(buf.cursor_position, 1)
        # Overshoot clamps at the end of the buffer, like forward-char.
        buf.cursor_right(count=99)
        self.assertEqual(buf.cursor_position, 3)

    def test_c_f_with_suggestion_accepts_it(self):
        buf = Buffer()
        buf.insert_text("hel")
        buf.suggestion = SimpleNamespace(text="lo")
        # Exactly what the c-f handler does when a suggestion exists.
        if buf.suggestion:
            buf.insert_text(buf.suggestion.text)
        self.assertEqual(buf.text, "hello")


class _KbLookupMixin:
    """Shared access to the CLI's own keybinding registry."""

    kb = None

    @classmethod
    def setUpClass(cls):
        import laintas_cli
        cls.kb = laintas_cli._build_keybindings()

    def _find(self, *keys):
        wanted = tuple(keys)
        for binding in self.kb.bindings:
            if binding.keys == wanted:
                return binding
        self.fail(f"no binding for {wanted!r}; have "
                  f"{[b.keys for b in self.kb.bindings]}")


class KeybindingRegistrationTests(_KbLookupMixin, unittest.TestCase):
    """The bindings the CLI adds must exist with the right properties."""

    def test_ctrl_e_handler_moves_to_end_of_line_without_suggestion(self):
        binding = self._find(Keys.ControlE)
        buf = Buffer()
        buf.insert_text("line1\nline2")
        buf.cursor_position = 0
        binding.handler(SimpleNamespace(current_buffer=buf, arg=1))
        self.assertEqual(buf.cursor_position, 5)

    def test_ctrl_f_handler_moves_forward_without_suggestion(self):
        binding = self._find(Keys.ControlF)
        buf = Buffer()
        buf.insert_text("abc")
        buf.cursor_position = 0
        binding.handler(SimpleNamespace(current_buffer=buf, arg=1))
        self.assertEqual(buf.cursor_position, 1)

    def test_ctrl_f_handler_accepts_a_suggestion(self):
        binding = self._find(Keys.ControlF)
        buf = Buffer()
        buf.insert_text("hel")
        buf.suggestion = SimpleNamespace(text="lo")
        binding.handler(SimpleNamespace(current_buffer=buf, arg=1))
        self.assertEqual(buf.text, "hello")

    def test_ctrl_z_is_undo_and_skips_pre_save(self):
        binding = self._find(Keys.ControlZ)
        self.assertFalse(binding.save_before(None))
        # No literal Ctrl-Z character may be inserted by our handler: calling
        # it on a buffer must only ever mutate via undo().
        buf = Buffer()
        buf.insert_text("ab")
        buf.save_to_undo_stack()
        buf.insert_text("c")
        event = SimpleNamespace(current_buffer=buf)
        binding.handler(event)
        self.assertEqual(buf.text, "ab")
        self.assertNotIn("\x1a", buf.text)

    def test_ctrl_y_is_redo_and_skips_pre_save(self):
        binding = self._find(Keys.ControlY)
        self.assertFalse(binding.save_before(None))
        buf = Buffer()
        buf.insert_text("a")
        buf.save_to_undo_stack()
        buf.insert_text("b")
        buf.undo()                     # text: "a", redo stack: ["ab"]
        event = SimpleNamespace(current_buffer=buf)
        binding.handler(event)
        self.assertEqual(buf.text, "ab")

    def test_ctrl_z_pre_save_would_break_redo(self):
        """Why save_before=False is load-bearing for Ctrl+Z: every default
        pre-save clears the redo stack, so a SECOND undo destroys the redo
        history the first one created - "ab" becomes unreachable."""
        buf = Buffer()
        for ch in "ab":
            buf.save_to_undo_stack()
            buf.insert_text(ch)
        # First undo, WITH the default pre-save (as a plain keypress gets).
        buf.save_to_undo_stack()
        buf.undo()
        self.assertEqual(buf.text, "a")
        # Second undo, again with the default pre-save: it clears the redo
        # stack before undo() can preserve "ab" in it.
        buf.save_to_undo_stack()
        buf.undo()
        self.assertEqual(buf.text, "")
        buf.redo()
        buf.redo()
        # Only one step came back - "ab" was lost by the second pre-save.
        self.assertEqual(buf.text, "a")

    def test_double_undo_double_redo_round_trips_without_pre_save(self):
        """With save_before=False (what the real bindings use), undoing
        twice and redoing twice returns to the original text."""
        buf = Buffer()
        for ch in "ab":
            buf.save_to_undo_stack()
            buf.insert_text(ch)
        buf.undo()
        buf.undo()
        self.assertEqual(buf.text, "")
        buf.redo()
        self.assertEqual(buf.text, "a")
        buf.redo()
        self.assertEqual(buf.text, "ab")


class CollapseExpandKeybindingTests(_KbLookupMixin, unittest.TestCase):
    """Ctrl+X Ctrl+S / Ctrl+X Ctrl+O: selection <-> content segment."""

    def setUp(self):
        import laintas_cli
        self.lc = laintas_cli
        laintas_cli._reset_paste_registry()
        self.addCleanup(laintas_cli._reset_paste_registry)

    def _select(self, buf, start, end):
        from prompt_toolkit.selection import SelectionType
        buf.cursor_position = start
        buf.start_selection(selection_type=SelectionType.CHARACTERS)
        buf.cursor_position = end

    def test_collapse_and_expand_are_registered(self):
        self._find(Keys.ControlX, Keys.ControlS)
        self._find(Keys.ControlX, Keys.ControlO)

    def test_collapse_replaces_selection_with_snippet_token(self):
        buf = Buffer()
        buf.insert_text("before\nline A\nline B\nline C\nafter")
        self._select(buf, 7, 27)
        binding = self._find(Keys.ControlX, Keys.ControlS)
        binding.handler(SimpleNamespace(current_buffer=buf))
        token = list(self.lc._paste_registry)[0]
        self.assertEqual(buf.text, f"before\n{token}\nafter")
        self.assertEqual(self.lc._paste_registry[token],
                         "line A\nline B\nline C")
        self.assertIn("~3 lines", token)
        self.assertIsNone(buf.selection_state)
        self.assertEqual(buf.cursor_position,
                         len("before\n" + token))

    def test_collapse_without_selection_is_a_noop(self):
        buf = Buffer()
        buf.insert_text("nothing")
        self.assertFalse(
            self.lc._collapse_selection_to_placeholder(buf))
        self.assertEqual(self.lc._paste_registry, {})
        self.assertEqual(buf.text, "nothing")

    def test_collapse_of_short_single_line_selection_is_a_noop(self):
        buf = Buffer()
        buf.insert_text("nothing selected")
        self._select(buf, 2, 4)       # "th" - shorter than the token
        self.assertFalse(
            self.lc._collapse_selection_to_placeholder(buf))
        self.assertEqual(self.lc._paste_registry, {})
        self.assertEqual(buf.text, "nothing selected")

    def test_collapse_of_whitespace_only_selection_is_a_noop(self):
        buf = Buffer()
        buf.insert_text("a   b")
        self._select(buf, 1, 4)
        self.assertFalse(
            self.lc._collapse_selection_to_placeholder(buf))
        self.assertEqual(self.lc._paste_registry, {})

    def test_collapse_flattens_inner_placeholder(self):
        # Selecting "text + [Pasted #N] + text" and collapsing must flatten
        # the inner token: nested tokens would survive submit as literals.
        inner, _ = self.lc._maybe_summarize_paste("x\ny\nz\nw")
        buf = Buffer()
        buf.insert_text(f"intro {inner} outro")
        self._select(buf, 0, len(buf.text))
        self.assertTrue(
            self.lc._collapse_selection_to_placeholder(buf))
        token = [t for t in self.lc._paste_registry if "Snippet" in t][0]
        self.assertEqual(self.lc._paste_registry[token],
                         "intro x\ny\nz\nw outro")
        expanded = self.lc._expand_pastes(buf.text)
        self.assertEqual(expanded, "intro x\ny\nz\nw outro")
        self.assertNotIn("[Pasted", expanded)

    def test_expand_at_cursor_restores_content(self):
        buf = Buffer()
        buf.insert_text("before\nline A\nline B\nline C\nafter")
        self._select(buf, 7, 27)
        self.assertTrue(
            self.lc._collapse_selection_to_placeholder(buf))
        token = [t for t in self.lc._paste_registry if "Snippet" in t][0]
        buf.cursor_position = buf.text.index(token) + 1
        self.assertTrue(self.lc._expand_placeholder_at_cursor(buf))
        self.assertEqual(buf.text,
                         "before\nline A\nline B\nline C\nafter")
        self.assertEqual(buf.cursor_position, 27)

    def test_expand_without_token_at_cursor_is_a_noop(self):
        buf = Buffer()
        buf.insert_text("plain text")
        buf.cursor_position = 3
        self.assertFalse(self.lc._expand_placeholder_at_cursor(buf))
        self.assertEqual(buf.text, "plain text")

    def test_hand_typed_lookalike_stays_literal(self):
        # Someone typing "[Pasted #9 ~3 lines]" by hand must not have it
        # expand: only registered tokens are content segments.
        buf = Buffer()
        buf.insert_text("[Pasted #9 ~3 lines]")
        buf.cursor_position = 5
        self.assertFalse(self.lc._expand_placeholder_at_cursor(buf))
        self.assertEqual(buf.text, "[Pasted #9 ~3 lines]")

    def test_generated_token_does_not_collide_with_literal_user_text(self):
        literal = "[Snippet #1 ~1 lines]"
        content = "x" * 40
        buf = Buffer()
        buf.insert_text(f"{literal} {content}")
        self._select(buf, len(literal) + 1, len(buf.text))

        self.assertTrue(
            self.lc._collapse_selection_to_placeholder(buf))
        self.assertIn(literal, buf.text)
        self.assertEqual(
            self.lc._expand_pastes(buf.text),
            f"{literal} {content}",
        )

    def test_token_survives_undo_of_collapse(self):
        # A real Ctrl+X Ctrl+S keypress snapshots the buffer into the undo
        # stack before the handler runs (default save_before=True), so
        # Ctrl+Z afterwards restores the pre-collapse text. _press models
        # that pre-save; the collapse itself is the handler.
        buf = Buffer()
        buf.insert_text("before\nline A\nline B\nafter")
        self._select(buf, 7, 21)
        _press(buf, lambda: self.lc._collapse_selection_to_placeholder(buf))
        self.assertTrue("[Snippet" in buf.text)
        buf.undo()          # what the Ctrl+Z binding runs
        self.assertEqual(buf.text,
                         "before\nline A\nline B\nafter")
        # and collapsing the same range again still works
        self._select(buf, 7, 21)
        self.assertTrue(
            self.lc._collapse_selection_to_placeholder(buf))
        self.assertEqual(
            self.lc._expand_pastes(buf.text),
            "before\nline A\nline B\nafter")

    def test_pasted_and_snippet_share_one_counter(self):
        p, _ = self.lc._register_placeholder("a\nb", kind="Pasted")
        s, _ = self.lc._register_placeholder("c\nd", kind="Snippet")
        self.assertIn("#1", p)
        self.assertIn("#2", s)
        self.assertEqual(self.lc._paste_counter, 2)

    def test_long_single_line_selection_collapses(self):
        # One 40-char line still beats a 19-char token on one line.
        buf = Buffer()
        buf.insert_text("w" * 40)
        self._select(buf, 0, 40)
        self.assertTrue(
            self.lc._collapse_selection_to_placeholder(buf))
        token = list(self.lc._paste_registry)[0]
        self.assertIn("~1 lines", token)


if __name__ == "__main__":
    unittest.main()
