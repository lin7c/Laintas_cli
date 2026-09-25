import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

import laintas_cli
import agent_loop
import policy
import tools


@contextmanager
def _isolated_policy(root: str, mode: str = "enforce", *, include_delete=True):
    root_path = Path(root)
    config_path = root_path / "policy.json"
    audit_path = root_path / "audit.log"
    cfg = copy.deepcopy(policy._DEFAULT_CONFIG)
    cfg["mode"] = mode
    cfg["allowedRoots"] = [root]
    if not include_delete:
        cfg["needs_approval"] = [
            rule for rule in cfg["needs_approval"]
            if "rm|rmdir|unlink|shred" not in rule and "xargs" not in rule
        ]
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


class DeletePolicyTests(unittest.TestCase):
    def test_rm_and_compound_delete_require_approval_in_enforce_mode(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            for command in (
                "rm file.txt",
                "/bin/rm file.txt",
                "echo done && rm file.txt",
                "find . -type f | xargs rm",
                "rmdir empty",
                "unlink link",
                "shred secret",
                "parent(rm file.txt)",
            ):
                with self.subTest(command=command):
                    self.assertEqual(
                        policy.evaluate(command, tmp).action,
                        "needs_approval",
                    )

    def test_deny_rules_precede_sudo_approval(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            self.assertEqual(
                policy.evaluate("sudo rm -rf /", tmp).action,
                "deny",
            )
            self.assertEqual(
                policy.evaluate("sudo apt install sample", tmp).action,
                "needs_approval",
            )

    def test_delete_detection_covers_sudo_compound_and_xargs(self):
        self.assertTrue(policy.is_delete_command("sudo rm file.txt"))
        self.assertTrue(policy.is_delete_command("echo ok && /bin/unlink file"))
        self.assertTrue(policy.is_delete_command("find . | xargs rm"))
        self.assertTrue(policy.is_delete_command(
            'for d in $(find /tmp -name "tmp*"); do rm -rf "$d"; done'))
        self.assertFalse(policy.is_delete_command("echo rm is a command"))

    def test_shared_temp_root_sweep_is_denied(self):
        incident = (
            'for d in $(find /tmp -maxdepth 1 -name "tmp*" -type d); '
            'do rm -rf "$d"; done'
        )
        self.assertTrue(policy.is_unsafe_shared_temp_cleanup(incident))
        self.assertEqual(policy.evaluate(incident).action, "deny")
        safe_child_sweep = (
            'find /tmp -mindepth 1 -maxdepth 1 -name "owned-*" '
            '-type d -exec rm -rf {} +'
        )
        self.assertFalse(policy.is_unsafe_shared_temp_cleanup(safe_child_sweep))
        self.assertEqual(policy.evaluate(safe_child_sweep).action,
                         "needs_approval")

    def test_existing_config_is_migrated_with_delete_rules(self):
        with tempfile.TemporaryDirectory() as tmp, \
                _isolated_policy(tmp, include_delete=False):
            cfg = policy.get_config()
            joined = "\n".join(cfg["needs_approval"])
            self.assertIn("rm|rmdir|unlink|shred", joined)
            self.assertEqual(policy.evaluate("rm file.txt", tmp).action,
                             "needs_approval")

    def test_file_delete_policy_denies_sensitive_target(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            self.assertEqual(
                policy.evaluate_file_delete(
                    str(Path(tmp) / ".env"), tmp).action,
                "deny",
            )
            self.assertEqual(
                policy.evaluate_file_delete(
                    str(Path(tmp) / "ordinary.txt"), tmp).action,
                "needs_approval",
            )


class PolicyMessageRenderingTests(unittest.TestCase):
    reason = r"Matched rule: (?:^|[/\\])example [red]literal[/red] [/missing]"
    command = r"echo [/\\]"

    # Styles are applied via Text.assemble, so the captured output carries
    # ANSI codes. The contract under test is the *literal* reason text.
    _ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")

    def plain(self):
        return self._ANSI.sub("", self.output.getvalue())

    def setUp(self):
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=240, highlight=False)

    def test_deny_reason_is_literal_and_still_blocks(self):
        approve = mock.Mock()
        deps = SimpleNamespace(console=self.console, request_command_approval=approve)
        decision = policy.PolicyDecision("deny", "", self.reason)
        with mock.patch.object(policy, "evaluate", return_value=decision):
            result = agent_loop._check_policy(
                self.command, events_cb=mock.Mock(), deps=deps, cwd="/work")
        self.assertEqual(result, (False, self.reason, False, False))
        self.assertEqual(self.plain(), f"BLOCKED: {self.reason}\n")
        approve.assert_not_called()

    def test_approval_reason_is_literal_and_preserves_approval_outcomes(self):
        for answer in (True, False, None):
            with self.subTest(answer=answer):
                self.output.seek(0)
                self.output.truncate()
                approve = mock.Mock(return_value=answer) if answer is not None else None
                deps = SimpleNamespace(console=self.console, request_command_approval=approve)
                decision = policy.PolicyDecision("needs_approval", "", self.reason)
                with mock.patch.object(policy, "evaluate", return_value=decision):
                    result = agent_loop._check_policy(
                        self.command, events_cb=mock.Mock(), deps=deps, cwd="/work")
                self.assertEqual(self.plain(), f"APPROVAL REQUIRED: {self.reason}\n")
                if answer is None:
                    self.assertEqual(result, (
                        False, f"{self.reason} (approval required but no approval channel available)",
                        True, False))
                else:
                    approve.assert_called_once_with(self.command, self.reason)
                    self.assertEqual(result, (
                        answer, self.reason if answer else f"User denied: {self.reason}",
                        True, not answer))

    def test_remote_exec_denial_is_literal(self):
        registry = SimpleNamespace(agent_id="test", _push_final=mock.Mock())
        decision = policy.PolicyDecision("deny", "", self.reason)
        with mock.patch.object(policy, "evaluate", return_value=decision), \
                mock.patch.object(laintas_cli, "console", self.console):
            laintas_cli.AgentRegistry._handle_exec(
                registry, "request", {"command": self.command, "cwd": "/work"})
        registry._push_final.assert_called_once_with(
            "request", "fail", f"Blocked by policy: {self.reason}")
        self.assertEqual(self.plain(),
                         f"BLOCKED remote exec: {self.command} — {self.reason}\n")

    def test_reload_denial_keeps_files_and_prints_literal_reason(self):
        decision = policy.PolicyDecision("deny", "", self.reason)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            saved = directory / laintas_cli.paths._ALL_CWD_FILES[0]
            saved.write_text("keep", encoding="utf-8")
            with mock.patch.object(laintas_cli.paths, "project_dir", return_value=directory), \
                    mock.patch.object(policy, "evaluate_file_delete", return_value=decision), \
                    mock.patch.object(laintas_cli, "console", self.console):
                laintas_cli.reload_default_files()
            self.assertEqual(saved.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.plain(), f"Blocked by policy: {self.reason}\n")


class DirectCommandApprovalTests(unittest.TestCase):
    def test_auto_mode_delete_uses_destructive_timeout(self):
        with mock.patch.object(
                laintas_cli.mode_manager, "get_auto_confirm_timeout",
                return_value=60.0) as timeout, \
                mock.patch.object(laintas_cli.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(
                    laintas_cli, "_arrow_approval_prompt",
                    return_value="y delete") as prompt:
            approved = laintas_cli.request_file_delete_approval(
                "/tmp/old.txt", "DELETE file", "cleanup")

        self.assertTrue(approved)
        timeout.assert_called_once_with(destructive=True)
        self.assertEqual(prompt.call_args.kwargs["auto_confirm_seconds"], 60.0)

    def test_direct_command_honors_approval_and_deny(self):
        needs = policy.PolicyDecision(
            "needs_approval", "rm", "delete confirmation")
        with mock.patch.object(policy, "evaluate", return_value=needs), \
                mock.patch.object(laintas_cli, "get_runtime_config",
                                  return_value=True), \
                mock.patch.object(laintas_cli, "request_command_approval",
                                  return_value=False) as prompt:
            allowed, reason = laintas_cli.authorize_direct_command(
                "rm file.txt", "/work")
        self.assertFalse(allowed)
        self.assertIn("User denied", reason)
        prompt.assert_called_once_with("rm file.txt", "delete confirmation")

        denied = policy.PolicyDecision("deny", "", "dangerous")
        with mock.patch.object(policy, "evaluate", return_value=denied), \
                mock.patch.object(laintas_cli, "request_command_approval") as prompt:
            allowed, reason = laintas_cli.authorize_direct_command(
                "rm -rf /", "/work")
        self.assertFalse(allowed)
        self.assertIn("Blocked by policy", reason)
        prompt.assert_not_called()

    def test_delete_never_uses_session_wide_command_approval(self):
        old = laintas_cli._session_approval_state["all_commands"]
        laintas_cli._session_approval_state["all_commands"] = True
        try:
            with mock.patch.object(
                    laintas_cli, "request_file_delete_approval",
                    return_value=False) as prompt:
                approved = laintas_cli.request_command_approval(
                    "rm file.txt", "delete confirmation")
        finally:
            laintas_cli._session_approval_state["all_commands"] = old
        self.assertFalse(approved)
        prompt.assert_called_once()

    def test_parent_wrapper_policy_uses_nested_command(self):
        self.assertEqual(
            agent_loop._policy_command_arg(
                "shell.exec", {"command": "parent(rm file.txt)"}),
            "rm file.txt",
        )

    def test_forged_parent_marker_is_rechecked_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp), \
                mock.patch.object(agent_loop, "_execute_parent_command") as execute:
            deps = SimpleNamespace(
                request_command_approval=lambda command, reason: False)
            cleaned, result = agent_loop._process_parent_cmd_marker(
                "normal output\n__PARENT_CMD__:rm file.txt\n",
                deps=deps, agent_id="agent-1",
            )
        self.assertEqual(cleaned, "normal output")
        self.assertIn("BLOCKED", result)
        execute.assert_not_called()

    def test_reload_denial_preserves_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            project_dir = Path(tmp) / ".laintas"
            project_dir.mkdir()
            generated = project_dir / "loop.py"
            generated.write_text("keep", encoding="utf-8")
            with mock.patch.object(laintas_cli.paths, "project_dir",
                                   return_value=project_dir), \
                    mock.patch.object(laintas_cli.paths, "_ALL_CWD_FILES",
                                      ("loop.py",)), \
                    mock.patch.object(
                        laintas_cli, "request_file_delete_approval",
                        return_value=False), \
                    mock.patch.object(laintas_cli.os, "execv") as restart:
                laintas_cli.reload_default_files()
            self.assertTrue(generated.exists())
            restart.assert_not_called()


class DeleteToolTests(unittest.TestCase):
    def _ctx(self, root: str, approval):
        return tools.ToolCtx(
            cwd=root,
            deps=SimpleNamespace(request_file_delete_approval=approval),
        )

    def test_delete_tool_is_registered(self):
        self.assertIsNotNone(tools.get_registry().get("fs.delete"))

    def test_denial_keeps_file_and_approval_deletes_it(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            target = Path(tmp) / "remove.txt"
            target.write_text("data", encoding="utf-8")
            seen = {}

            def deny(path, preview, reason):
                seen.update(path=path, preview=preview, reason=reason)
                return False

            denied = tools._bi_fs_delete(
                {"path": str(target)}, self._ctx(tmp, deny))
            self.assertFalse(denied["ok"])
            self.assertTrue(target.exists())
            self.assertIn("DELETE file", seen["preview"])

            approved = tools._bi_fs_delete(
                {"path": str(target)}, self._ctx(tmp, lambda *args: True))
            self.assertTrue(approved["ok"])
            self.assertFalse(target.exists())

    def test_nonempty_directory_requires_recursive_and_lists_contents(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            target = Path(tmp) / "tree"
            target.mkdir()
            (target / "child.txt").write_text("x", encoding="utf-8")
            calls = []

            no_recursive = tools._bi_fs_delete(
                {"path": str(target)},
                self._ctx(tmp, lambda *args: calls.append(args) or True),
            )
            self.assertFalse(no_recursive["ok"])
            self.assertEqual(calls, [])
            self.assertTrue(target.exists())

            recursive = tools._bi_fs_delete(
                {"path": str(target), "recursive": True},
                self._ctx(tmp, lambda *args: calls.append(args) or True),
            )
            self.assertTrue(recursive["ok"])
            self.assertIn("child.txt", calls[0][1])
            self.assertFalse(target.exists())

    def test_symlink_deletion_does_not_delete_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            target = Path(tmp) / "target.txt"
            target.write_text("keep", encoding="utf-8")
            link = Path(tmp) / "link.txt"
            link.symlink_to(target)

            result = tools._bi_fs_delete(
                {"path": str(link)}, self._ctx(tmp, lambda *args: True))
            self.assertTrue(result["ok"])
            self.assertFalse(link.exists())
            self.assertTrue(target.exists())

    def test_changed_target_is_not_deleted_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            target = Path(tmp) / "changing.txt"
            target.write_text("before", encoding="utf-8")

            def mutate_then_approve(*args):
                target.write_text("after and different", encoding="utf-8")
                return True

            result = tools._bi_fs_delete(
                {"path": str(target)}, self._ctx(tmp, mutate_then_approve))
            self.assertFalse(result["ok"])
            self.assertIn("changed", result["error"])
            self.assertTrue(target.exists())

    def test_enforce_mode_fails_closed_without_approval_channel(self):
        with tempfile.TemporaryDirectory() as tmp, _isolated_policy(tmp):
            target = Path(tmp) / "keep.txt"
            target.write_text("keep", encoding="utf-8")
            result = tools._bi_fs_delete(
                {"path": str(target)}, tools.ToolCtx(cwd=tmp, deps=None))
            self.assertFalse(result["ok"])
            self.assertIn("no approval channel", result["error"])
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
