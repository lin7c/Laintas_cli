"""E2 (bughunt): frontmatter values must not be able to inject keys.

_format_frontmatter interpolated name/description (and other string
fields) into the line-oriented YAML header without sanitizing. An
LLM-generated description containing a newline wrote a new key into the
header; the next parse read it back as a real field. The fix collapses
all whitespace in header values to single spaces — a value can never
break out of its line.
"""
import unittest

import memory_system as ms


class FrontmatterSanitizeTests(unittest.TestCase):
    def test_newline_injection_is_folded_to_text(self):
        meta = {"name": "test-mem",
                "description": "line one\nfake_key: injected",
                "type": "project"}
        text = ms._format_frontmatter(meta, "body")
        parsed, body = ms._parse_frontmatter(text)
        self.assertEqual(parsed.get("name"), "test-mem")
        self.assertEqual(parsed.get("description"),
                         "line one fake_key: injected")
        self.assertNotIn("fake_key", parsed)
        self.assertEqual(body, "body")

    def test_carriage_return_and_tabs_also_folded(self):
        meta = {"name": "n", "description": "a\r\nb\tc", "type": "user"}
        parsed, _ = ms._parse_frontmatter(ms._format_frontmatter(meta, "b"))
        self.assertEqual(parsed.get("description"), "a b c")

    def test_normal_values_round_trip(self):
        meta = {"name": "n", "description": "正常描述 with spaces",
                "type": "user"}
        parsed, _ = ms._parse_frontmatter(ms._format_frontmatter(meta, "b"))
        self.assertEqual(parsed.get("description"), "正常描述 with spaces")

    def test_multiline_description_is_preserved_as_text(self):
        # Legitimate multi-line content stays readable, just single-line.
        meta = {"name": "n", "description": "first line\nsecond line",
                "type": "user"}
        parsed, _ = ms._parse_frontmatter(ms._format_frontmatter(meta, "b"))
        self.assertEqual(parsed.get("description"),
                         "first line second line")

    def test_other_string_fields_are_sanitized(self):
        meta = {"name": "n", "description": "d", "type": "user",
                "product": "cli\ninjected: 1",
                "stale_reason": "r\nbad: 2"}
        text = ms._format_frontmatter(meta, "b")
        parsed, _ = ms._parse_frontmatter(text)
        self.assertNotIn("injected", parsed)
        self.assertNotIn("bad", parsed)


if __name__ == "__main__":
    unittest.main()
