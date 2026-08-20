"""Tests for the co-author trailer laintas-cli adds to git commits.

The interesting half is not that the trailer gets added — it is that the
rewrite keeps its hands off every command it cannot read with certainty.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import git_attribution


IDENTITY = "laintas <318547197+laintas@users.noreply.github.com>"
TRAILER = f"--trailer 'Co-Authored-By: {IDENTITY}'"


def apply(command):
    return git_attribution.apply(command, co_author=IDENTITY)


class TrailerInsertion(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(git_attribution, "_git_version",
                                    return_value=(2, 43))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_simple_commit(self):
        self.assertEqual(
            apply('git commit -m "fix parser"'),
            f'git commit {TRAILER} -m "fix parser"')

    def test_global_options_before_subcommand(self):
        self.assertEqual(
            apply("git -C /repo --no-pager commit -m x"),
            f"git -C /repo --no-pager commit {TRAILER} -m x")
        self.assertEqual(
            apply("git --git-dir=/r/.git commit --amend --no-edit"),
            f"git --git-dir=/r/.git commit {TRAILER} --amend --no-edit")

    def test_chained_commands_each_get_one(self):
        out = apply("git add a.py && git commit -m one && git commit -m two")
        self.assertEqual(out.count("--trailer"), 2)
        self.assertTrue(out.startswith("git add a.py && git commit --trailer"))

    def test_absolute_git_path(self):
        self.assertEqual(apply("/usr/bin/git commit -m x"),
                         f"/usr/bin/git commit {TRAILER} -m x")

    def test_message_body_is_not_command_syntax(self):
        """`commit` inside the message must not be mistaken for a subcommand."""
        out = apply('git commit -m "explain how to git commit properly"')
        self.assertEqual(out.count("--trailer"), 1)
        self.assertTrue(out.startswith(f"git commit {TRAILER} -m"))


class LeavesAlone(unittest.TestCase):
    """Commands the rewrite must return byte-identical."""

    def setUp(self):
        patcher = mock.patch.object(git_attribution, "_git_version",
                                    return_value=(2, 43))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _unchanged(self, command):
        self.assertEqual(apply(command), command)

    def test_non_commit_git_commands(self):
        for command in ("git status", "git log --oneline -5",
                        "git add -- src/parser.py", "git push origin main",
                        "git commit-tree abc123"):
            self._unchanged(command)

    def test_not_git_at_all(self):
        self._unchanged("npm run commit")
        self._unchanged("echo git commit")   # `git` is an argument, not a program

    def test_caller_already_attributed(self):
        self._unchanged("git commit -m 'x\n\nCo-authored-by: Someone <a@b.c>'")
        self._unchanged("git commit --trailer 'Reviewed-by: X <x@y.z>' -m x")

    def test_heredoc_is_data_not_syntax(self):
        self._unchanged('git commit -F - <<EOF\nsubject\nEOF')

    def test_unbalanced_quotes(self):
        self._unchanged('git commit -m "unterminated')

    def test_disabled_by_config(self):
        self.assertEqual(git_attribution.apply("git commit -m x", co_author=""),
                         "git commit -m x")

    def test_old_git_without_trailer_support(self):
        with mock.patch.object(git_attribution, "_git_version",
                               return_value=(2, 25)):
            self._unchanged("git commit -m x")


class ConfigResolution(unittest.TestCase):
    def _with_config(self, payload):
        path = mock.MagicMock()
        path.read_text.return_value = json.dumps(payload)
        return mock.patch.object(git_attribution.paths, "CONFIG_FILE", path)

    def test_default_when_key_absent(self):
        with self._with_config({"agentName": "box"}):
            self.assertEqual(git_attribution.configured_co_author(),
                             git_attribution.DEFAULT_CO_AUTHOR)

    def test_default_when_config_unreadable(self):
        path = mock.MagicMock()
        path.read_text.side_effect = OSError
        with mock.patch.object(git_attribution.paths, "CONFIG_FILE", path):
            self.assertEqual(git_attribution.configured_co_author(),
                             git_attribution.DEFAULT_CO_AUTHOR)

    def test_custom_identity(self):
        with self._with_config({"gitCoAuthor": "bot <b@example.com>"}):
            self.assertEqual(git_attribution.configured_co_author(),
                             "bot <b@example.com>")

    def test_empty_string_disables(self):
        with self._with_config({"gitCoAuthor": ""}):
            self.assertEqual(git_attribution.configured_co_author(), "")

    def test_false_disables(self):
        with self._with_config({"gitCoAuthor": False}):
            self.assertEqual(git_attribution.configured_co_author(), "")


if __name__ == "__main__":
    unittest.main()
