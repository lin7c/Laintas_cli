"""L4 (bughunt): a frontmatter rewrite is a round-trip, not a projection.

_format_frontmatter emitted only whitelisted keys, so any key it did not
know (written by a newer version, another product, or a manual edit)
vanished on every rewrite — silent data loss for anything this build
cannot interpret yet. The fix re-emits unknown keys verbatim, sanitized
like every other header value.
"""
import unittest

import memory_system as ms


_CONTENT = """---
name: probe-mem
description: probe
metadata:
  type: project
custom_key: important_value
another_unknown: 42
---

Body here.
"""


class UnknownKeyRoundTripTests(unittest.TestCase):
    def test_unknown_keys_survive_rewrite(self):
        meta, body = ms._parse_frontmatter(_CONTENT)
        rebuilt = ms._format_frontmatter(meta, body)
        meta2, body2 = ms._parse_frontmatter(rebuilt)
        self.assertEqual(meta2.get("custom_key"), "important_value")
        self.assertEqual(meta2.get("another_unknown"), "42")
        self.assertEqual(body2, "Body here.")

    def test_known_keys_unchanged(self):
        meta, body = ms._parse_frontmatter(_CONTENT)
        meta2, _ = ms._parse_frontmatter(ms._format_frontmatter(meta, body))
        for key in ("name", "description", "type"):
            self.assertEqual(meta2.get(key), meta.get(key))

    def test_unknown_key_values_are_sanitized(self):
        meta = {"name": "n", "description": "d", "type": "user",
                "custom": "line1\ninjected: x"}
        rebuilt = ms._format_frontmatter(meta, "b")
        meta2, _ = ms._parse_frontmatter(rebuilt)
        self.assertEqual(meta2.get("custom"), "line1 injected: x")
        self.assertNotIn("injected", meta2)

    def test_no_duplicate_emission_for_known_keys(self):
        # A whitelisted key must not be re-emitted by the unknown-key loop.
        meta = {"name": "n", "description": "d", "type": "user",
                "status": "stale", "importance": "0.5"}
        rebuilt = ms._format_frontmatter(meta, "b")
        self.assertEqual(rebuilt.count("name:"), 1)
        self.assertEqual(rebuilt.count("status:"), 1)

    def test_metadata_nested_dict_not_duplicated(self):
        meta, body = ms._parse_frontmatter(_CONTENT)
        # 'metadata' is normalized into 'type' by the parser; the rebuilt
        # header must carry the type exactly once.
        rebuilt = ms._format_frontmatter(meta, body)
        self.assertEqual(rebuilt.count("type:"), 1)


if __name__ == "__main__":
    unittest.main()
