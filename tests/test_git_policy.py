"""Destructive-git approval tier.

The point of these tests is the AUDIT-mode assertions. Ordinary git rules live
in ``needs_approval``, which is advisory in audit mode (the default), so before
``is_destructive_git_command`` existed a ``git clean -fdx`` ran with no prompt
at all. Anything asserted at ``mode="audit"`` below is guarding that hole.
"""
import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import policy


@contextmanager
def _isolated_policy(root: str, mode: str = "audit"):
    root_path = Path(root)
    config_path = root_path / "policy.json"
    audit_path = root_path / "audit.log"
    cfg = copy.deepcopy(policy._DEFAULT_CONFIG)
    cfg["mode"] = mode
    cfg["allowedRoots"] = [root]
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with mock.patch.object(policy, "CONFIG_PATH", config_path), \
            mock.patch.object(policy, "AUDIT_PATH", audit_path):
        policy._config = None
        policy._config_mtime = 0.0
        try:
            yield
        finally:
            policy._config = None
            policy._config_mtime = 0.0


DESTRUCTIVE = (
    "git clean -fd",
    "git clean -fdx",
    "git -C /repo clean -fd",              # global option before the subcommand
    "git --git-dir=/r/.git clean -f",
    "git reset --hard",
    "git reset --hard HEAD~2",
    "git checkout -- .",
    "git checkout .",
    "git checkout -f main",
    "git restore src/app.py",
    "git restore --worktree --staged src/app.py",
    "git push --force",
    "git push -f origin main",
    "git push --force-with-lease",
    "git push origin :stale-branch",       # refspec branch deletion
    "git branch -D feature",
    "git tag -d v1.0.0",
    "git stash drop",
    "git stash clear",
    "git filter-branch --all",
    "git reflog expire --expire=now",
    "git update-ref -d refs/heads/x",
    "git worktree remove -f ../wt",
    "echo starting && git clean -fd",      # hidden behind a chain
    "parent(git reset --hard)",            # laintas parent() wrapper
    "sudo git clean -fdx",
    "/usr/bin/git push --force",
)

NOT_DESTRUCTIVE = (
    "git clean -n",
    "git clean --dry-run",
    "git reset HEAD~1",
    "git reset --soft HEAD~1",
    "git checkout main",
    "git checkout -b feature",
    "git restore --staged src/app.py",     # unstage only; recoverable
    "git push origin main",
    "git branch -d merged",                # refuses on unmerged work
    "git branch feature",
    "git stash",
    "git stash list",
    "git tag v1.0.0",
    "git reflog",
    "git worktree remove ../wt",           # refuses when the tree is dirty
    "git commit -m 'fix'",
    "git revert abc123",                   # adds history, does not destroy it
    "git merge main",
    "git rebase main",
    "git status",
    "git diff --staged",
    "echo git clean -fd is dangerous",     # merely mentions it
)


class DestructiveGitDetectionTests(unittest.TestCase):
    def test_destructive_forms_are_detected(self):
        for command in DESTRUCTIVE:
            with self.subTest(command=command):
                self.assertTrue(policy.is_destructive_git_command(command))

    def test_safe_and_read_only_forms_are_not_flagged(self):
        for command in NOT_DESTRUCTIVE:
            with self.subTest(command=command):
                self.assertFalse(policy.is_destructive_git_command(command))


class DestructiveGitPolicyTests(unittest.TestCase):
    def test_destructive_git_requires_approval_in_audit_mode(self):
        """The regression this tier exists for: audit is the DEFAULT mode."""
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp, "audit"):
            for command in DESTRUCTIVE:
                with self.subTest(command=command):
                    self.assertEqual(
                        policy.evaluate(command, tmp).action, "needs_approval")

    def test_destructive_git_requires_approval_in_enforce_mode(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp, "enforce"):
            for command in DESTRUCTIVE:
                with self.subTest(command=command):
                    self.assertEqual(
                        policy.evaluate(command, tmp).action, "needs_approval")

    def test_read_only_git_still_runs_without_a_prompt_in_audit_mode(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp, "audit"):
            for command in ("git status", "git diff --staged",
                            "git log --oneline -10", "git stash list",
                            "git branch -a", "git clean -n"):
                with self.subTest(command=command):
                    self.assertEqual(policy.evaluate(command, tmp).action, "allow")

    def test_ordinary_git_mutations_prompt_in_enforce_only(self):
        """`git commit`/`switch`/`cherry-pick` stay in the advisory tier."""
        for command in ("git commit -m x", "git switch main",
                        "git cherry-pick abc123", "git stash"):
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as tmp, \
                        _isolated_policy(tmp, "enforce"):
                    self.assertEqual(
                        policy.evaluate(command, tmp).action, "needs_approval")
                with tempfile.TemporaryDirectory() as tmp, \
                        _isolated_policy(tmp, "audit"):
                    self.assertEqual(policy.evaluate(command, tmp).action, "allow")

    def test_disabled_mode_still_bypasses_everything(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp, "disabled"):
            self.assertEqual(
                policy.evaluate("git clean -fdx", tmp).action, "allow")


if __name__ == "__main__":
    unittest.main()
