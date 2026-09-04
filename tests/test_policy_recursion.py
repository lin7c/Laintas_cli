"""An unbounded walk of a tree nobody sized has to ask first.

`fs.glob`/`fs.grep` now bound themselves, but a shell command has nobody to
stop it: one `find /mnt/f -name laintas-cli` killed a session outright.

The rule is deliberately about the SHAPE of the command, not about where the
files are. A list of slow filesystems (9p, sshfs, NFS, SMB, a network drive)
or of big directories has no end and cannot be known from inside this process.
"No depth bound, and rooted outside the directory the user is working in" is
knowable, and it is the same boundary `_check_paths` already draws for writes.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import policy


class UnboundedRecursionTests(unittest.TestCase):
    CWD = "/work/project"

    def _asks(self, command):
        return policy.is_unbounded_recursion(command, self.CWD)

    def test_work_inside_the_working_directory_is_frictionless(self):
        """Their own tree, chosen by them, whose scale is least surprising."""
        for command in ("find . -name '*.py'", "find ./tests -name x",
                        "grep -r needle .", "grep -rn needle src/",
                        "du -sh ./tests", "cp -r ./a ./b", "rg needle"):
            self.assertFalse(self._asks(command), command)

    def test_an_unbounded_walk_outside_it_asks(self):
        for command in ("find / -name x", "find ~ -name x",
                        "find /mnt/f -name laintas-cli",
                        "grep -r needle /home/other", "ls -R /mnt/c",
                        "rg needle /mnt/f", "cp -r /etc/x ."):
            self.assertTrue(self._asks(command), command)

    def test_a_real_ceiling_clears_it(self):
        for command in ("find /mnt/f -maxdepth 2 -name x",
                        "rg --max-depth 2 needle /mnt/f",
                        "tree -L 2 /mnt/f"):
            self.assertFalse(self._asks(command), command)

    def test_du_is_not_bounded_by_an_option_that_shortens_its_output(self):
        """`-s` and `-d N` change what du PRINTS, never what it visits.

        This is the command from the incident report: `du -sh` on five
        directories of a mounted drive sat for a minute and was killed.
        """
        self.assertTrue(self._asks("du -sh /data"))
        self.assertTrue(self._asks("du -d 1 /data"))

    def test_the_pattern_is_not_mistaken_for_the_path(self):
        """`grep -r needle /somewhere` reads its path second, not first."""
        self.assertTrue(self._asks("grep -r needle /etc"))
        self.assertTrue(self._asks("grep -r --include=*.py needle /etc"))
        self.assertTrue(self._asks("rg -g '*.py' needle /var"))
        # ...and a search with no path at all defaults to the working
        # directory, which is inside.
        self.assertFalse(self._asks("grep -r needle"))

    def test_a_reader_that_does_not_recurse_is_left_alone(self):
        for command in ("ls /mnt/f", "cat /mnt/f/a.txt", "head -n 5 /etc/hosts",
                        "stat /mnt/c/Users"):
            self.assertFalse(self._asks(command), command)

    def test_it_names_no_filesystem(self):
        """Behaviour: an ordinary directory outside cwd asks just the same."""
        self.assertTrue(self._asks("find /srv/data -name x"))
        self.assertTrue(self._asks("find /Volumes/backup -name x"))
        self.assertTrue(self._asks("find /net/share -name x"))

    def test_a_wrapper_does_not_hide_it(self):
        self.assertTrue(self._asks("sudo find / -name x"))
        self.assertTrue(self._asks("ls -la && find /etc -name x"))


class PolicyDecisionTests(unittest.TestCase):
    def test_it_asks_in_audit_mode_too(self):
        """Audit mode makes ordinary approval rules advisory. This one is not
        advisory: the cost is paid during the command, not after it."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(policy, "_load_config",
                                   return_value={**policy._DEFAULT_CONFIG,
                                                 "mode": "audit"}), \
                    mock.patch.object(policy, "_write_audit"):
                decision = policy.evaluate("find / -name x", cwd=tmp)
        self.assertEqual("needs_approval", decision.action)
        self.assertIn("depth bound", decision.reason)

    def test_the_reason_says_what_would_clear_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(policy, "_write_audit"):
                decision = policy.evaluate("du -sh /", cwd=tmp)
        self.assertEqual("needs_approval", decision.action)
        self.assertIn("-maxdepth", decision.reason)


if __name__ == "__main__":
    unittest.main()
