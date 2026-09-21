"""S1 (bughunt): approval rules must match command variants, not just the
string as typed.

A user rule written with a literal single space (`rm -rf\\s`) was walked
past by `rm  -rf` (double space) — the deny loop already matched
per-variant, but the needs_approval loop only searched the raw string.
The fix aligns the approval loop with the deny loop. The original string
is always variants[0], so pre-existing matches keep firing; this can
only add approvals, never remove one.
"""
import unittest

import policy


def _approval_hit(command: str, pattern: str) -> bool:
    cfg = {"needs_approval": [pattern]}
    rules = policy._get_compiled_rules("needs_approval", cfg)
    stripped = policy._unwrap_parent(command)
    variants, _ = policy._command_variants(stripped)
    return any(r.search(v) for r in rules for v in variants)


class ApprovalVariantTests(unittest.TestCase):
    def test_literal_space_rule_matches_double_space(self):
        self.assertTrue(_approval_hit("rm  -rf /", r"rm -rf\s"))
        self.assertTrue(_approval_hit("rm  -rf  /tmp", r"rm -rf\s"))

    def test_single_space_still_matches(self):
        self.assertTrue(_approval_hit("rm -rf /", r"rm -rf\s"))

    def test_non_matching_command_still_allowed(self):
        self.assertFalse(_approval_hit("ls -la", r"rm -rf\s"))

    def test_default_rules_are_unaffected(self):
        # The shipped rules use \s classes and matched either way; the fix
        # must not change their verdicts.
        for cmd in ("rm -rf /", "rm  -rf /", "git clean -fdx",
                    "git  clean -fdx"):
            # _compile_rules, not _get_compiled_rules: the latter caches by
            # (key, mtime, org version) and ignores which dict it was handed,
            # so any earlier test's config would answer for the defaults.
            rules = policy._compile_rules(
                policy._DEFAULT_CONFIG.get("needs_approval", []))
            stripped = policy._unwrap_parent(cmd)
            variants, _ = policy._command_variants(stripped)
            hit = any(r.search(v) for r in rules for v in variants)
            self.assertTrue(hit, f"default rules lost a match on {cmd!r}")


if __name__ == "__main__":
    unittest.main()
