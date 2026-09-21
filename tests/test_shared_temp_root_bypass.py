"""S2 (bughunt): path-equivalent spellings of the shared temp root.

`/tmp/.`, `/tmp/./` and `/tmp//` are the same starting point as `/tmp` to
find(1), but the shared-root regex only accepted an optional single
trailing slash — every one of them walked past the unsafe-sweep check
(`find /tmp/. -name 'tmp*' -exec rm -rf {} +` could select and delete the
shared root itself). The fix matches the dot-and-slash spellings that
normalise to the root; real subdirectories and the -mindepth guard are
unchanged.
"""
import unittest

import policy


class SharedTempRootTests(unittest.TestCase):
    def test_dot_and_slash_spellings_are_caught(self):
        for cmd in (
            "find /tmp/. -name 'tmp*' -exec rm -rf {} +",
            "find /tmp/./ -name 'tmp*' -exec rm -rf {} +",
            "find /tmp// -name 'tmp*' -exec rm -rf {} +",
            "find /var/tmp/. -name 'x' -exec rm -rf {} +",
        ):
            self.assertTrue(policy.is_unsafe_shared_temp_cleanup(cmd),
                            f"bypassed: {cmd!r}")

    def test_plain_spellings_still_caught(self):
        for cmd in ("find /tmp -name 'tmp*' -exec rm -rf {} +",
                    "find /tmp/ -name 'tmp*' -exec rm -rf {} +"):
            self.assertTrue(policy.is_unsafe_shared_temp_cleanup(cmd))

    def test_mindepth_guard_preserved(self):
        self.assertFalse(
            policy.is_unsafe_shared_temp_cleanup(
                "find /tmp -mindepth 1 -name 'x' -delete"))

    def test_real_subdirectory_not_flagged(self):
        self.assertFalse(
            policy.is_unsafe_shared_temp_cleanup(
                "find /tmp/mysubdir -name 'x' -delete"))

    def test_non_temp_roots_not_flagged(self):
        self.assertFalse(
            policy.is_unsafe_shared_temp_cleanup(
                "find /home/user -name 'x' -delete"))



class Python310CompatTests(unittest.TestCase):
    def test_no_311_only_regex_syntax(self):
        # setup.py supports 3.10; possessive quantifiers and atomic groups are
        # 3.11+ and made every policy.evaluate() raise re.error there.
        import inspect
        import io
        import re
        import tokenize
        import policy
        src = inspect.getsource(policy.is_unsafe_shared_temp_cleanup)
        literals = [tok.string for tok in tokenize.generate_tokens(
            io.StringIO(src).readline) if tok.type == tokenize.STRING]
        for lit in literals:            # patterns only, not the comments
            self.assertIsNone(re.search(r"[*+?}]\+|\(\?>", lit), lit)

if __name__ == "__main__":
    unittest.main()
