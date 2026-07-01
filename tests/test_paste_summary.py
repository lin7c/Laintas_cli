import unittest

import agent_loop
import laintas_cli


class PasteSummaryTests(unittest.TestCase):
    def setUp(self):
        laintas_cli._reset_paste_registry()
        self._orig = {
            "paste_summary": agent_loop.get_runtime_config("paste_summary"),
            "paste_summary_min_lines": agent_loop.get_runtime_config("paste_summary_min_lines"),
            "paste_summary_min_chars": agent_loop.get_runtime_config("paste_summary_min_chars"),
        }
        agent_loop.set_runtime_config("paste_summary", True)
        agent_loop.set_runtime_config("paste_summary_min_lines", 3)
        agent_loop.set_runtime_config("paste_summary_min_chars", 150)

    def tearDown(self):
        for k, v in self._orig.items():
            agent_loop.set_runtime_config(k, v)
        laintas_cli._reset_paste_registry()

    def test_short_paste_not_summarized(self):
        placeholder, lc = laintas_cli._maybe_summarize_paste("hi there\nsecond line")
        self.assertIsNone(placeholder)
        self.assertEqual(lc, 2)
        self.assertEqual(laintas_cli._paste_registry, {})

    def test_three_line_paste_summarized(self):
        data = "a\nb\nc"
        placeholder, lc = laintas_cli._maybe_summarize_paste(data)
        self.assertIsNotNone(placeholder)
        self.assertEqual(lc, 3)
        self.assertIn("~3 lines", placeholder)
        self.assertEqual(laintas_cli._expand_pastes(placeholder), data)

    def test_long_single_line_summarized(self):
        data = "x" * 200
        placeholder, lc = laintas_cli._maybe_summarize_paste(data)
        self.assertIsNotNone(placeholder)
        self.assertEqual(lc, 1)
        self.assertEqual(laintas_cli._expand_pastes(placeholder), data)

    def test_crlf_normalized(self):
        data = "a\r\nb\r\nc"
        placeholder, lc = laintas_cli._maybe_summarize_paste(data)
        self.assertEqual(lc, 3)
        self.assertEqual(laintas_cli._expand_pastes(placeholder), "a\nb\nc")

    def test_expand_within_surrounding_text(self):
        placeholder, _ = laintas_cli._maybe_summarize_paste("a\nb\nc\nd")
        line = f"please review {placeholder} thanks"
        self.assertEqual(laintas_cli._expand_pastes(line), "please review a\nb\nc\nd thanks")

    def test_two_pastes_both_expand(self):
        p1, _ = laintas_cli._maybe_summarize_paste("a\nb\nc")
        p2, _ = laintas_cli._maybe_summarize_paste("d\ne\nf")
        self.assertNotEqual(p1, p2)
        line = f"{p1} and {p2}"
        self.assertEqual(laintas_cli._expand_pastes(line), "a\nb\nc and d\ne\nf")

    def test_toggle_off_never_summarizes(self):
        agent_loop.set_runtime_config("paste_summary", False)
        placeholder, lc = laintas_cli._maybe_summarize_paste("a\nb\nc\nd\ne")
        self.assertIsNone(placeholder)
        self.assertEqual(lc, 5)
        self.assertEqual(laintas_cli._paste_registry, {})

    def test_reset_clears_registry(self):
        laintas_cli._maybe_summarize_paste("a\nb\nc")
        self.assertTrue(laintas_cli._paste_registry)
        laintas_cli._reset_paste_registry()
        self.assertEqual(laintas_cli._paste_registry, {})
        self.assertEqual(laintas_cli._paste_counter, 0)

    def test_expand_noop_without_placeholder(self):
        self.assertEqual(laintas_cli._expand_pastes("plain text"), "plain text")

    def test_custom_thresholds(self):
        agent_loop.set_runtime_config("paste_summary_min_lines", 10)
        agent_loop.set_runtime_config("paste_summary_min_chars", 10000)
        placeholder, _ = laintas_cli._maybe_summarize_paste("a\nb\nc\nd")
        self.assertIsNone(placeholder)


class PasteSpanTests(unittest.TestCase):
    def setUp(self):
        laintas_cli._reset_paste_registry()

    def _make(self):
        p, _ = laintas_cli._maybe_summarize_paste("a\nb\nc\nd")
        return p

    def test_span_none_without_placeholder(self):
        self.assertIsNone(laintas_cli._paste_span_at("hello world", 3))

    def test_span_covers_placeholder(self):
        p = self._make()
        text = f"pre {p} post"
        start = text.index(p)
        end = start + len(p)
        self.assertEqual(laintas_cli._paste_span_at(text, start), (start, end))
        self.assertEqual(laintas_cli._paste_span_at(text, end), (start, end))
        self.assertEqual(laintas_cli._paste_span_at(text, start + 2), (start, end))

    def test_span_outside_placeholder(self):
        p = self._make()
        text = f"pre {p} post"
        self.assertIsNone(laintas_cli._paste_span_at(text, 1))

    def test_backspace_deletes_whole_segment(self):
        p = self._make()
        text = f"pre {p} post"
        end = text.index(p) + len(p)
        span = laintas_cli._paste_span_at(text, end)
        self.assertIsNotNone(span)
        s, e = span
        result = text[:s] + text[e:]
        self.assertEqual(result, "pre  post")


class PasteLexerTests(unittest.TestCase):
    def setUp(self):
        laintas_cli._reset_paste_registry()

    def test_lexer_marks_placeholder(self):
        from prompt_toolkit.document import Document

        p, _ = laintas_cli._maybe_summarize_paste("a\nb\nc")
        line = f"hi {p} bye"
        lexer = laintas_cli._PastePlaceholderLexer()
        frags = lexer.lex_document(Document(line))(0)
        styles = [style for style, _text in frags]
        self.assertIn("class:paste-placeholder", styles)
        rebuilt = "".join(text for _style, text in frags)
        self.assertEqual(rebuilt, line)

    def test_lexer_plain_line(self):
        from prompt_toolkit.document import Document

        lexer = laintas_cli._PastePlaceholderLexer()
        frags = lexer.lex_document(Document("no placeholder here"))(0)
        self.assertTrue(all(style == "" for style, _t in frags))


if __name__ == "__main__":
    unittest.main()
