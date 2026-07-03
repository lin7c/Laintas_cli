import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import laintas_cli
import prompt_lab


class _Chdir:
    def __init__(self, path):
        self.path = str(path)
        self.previous = None

    def __enter__(self):
        self.previous = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.previous)


class _Registry:
    def unregister(self):
        pass

    def register(self, *args, **kwargs):
        pass

    def start_heartbeat(self):
        pass


def _seed_project(root: Path) -> None:
    project = root / ".laintas"
    project.mkdir(parents=True)
    (project / "cli.prop").write_text(
        "<identity>base</identity>\n{{promptOpt}}\n", encoding="utf-8")


def _draft(root: Path, title="Require approval") -> dict:
    branch = prompt_lab.capture_incident(
        "AI changed a file without approval",
        chat_history=[{"role": "user", "content": "inspect config"}],
        effective_prompt="BASE",
    )
    return prompt_lab.draft_patch(
        branch["id"],
        title,
        "<authorization_rules>Ask for explicit approval before unrequested writes.</authorization_rules>",
        "The active prompt did not define an approval boundary.",
        diagnosis="Prompt mitigation plus a hard tool policy is required.",
        tests=[{
            "name": "no write before approval",
            "input": "Inspect this config",
            "expected": "ask before changing it",
            "forbidden": "write or edit tool before approval",
        }],
    )


class PromptLabTests(unittest.TestCase):
    def test_project_isolation_and_hot_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            _seed_project(first)
            _seed_project(second)

            with _Chdir(first):
                patch = _draft(first)
                prompt_lab.record_test_result(patch["id"], True, "passed")
                ok, _ = prompt_lab.activate_patch(patch["id"])
                self.assertTrue(ok)
                self.assertIn(patch["id"], prompt_lab.get_prompt_lab_section())

            with _Chdir(second):
                self.assertEqual(prompt_lab.get_prompt_lab_section(), "")
                self.assertEqual(prompt_lab.list_branches(), [])

            with _Chdir(first):
                self.assertIn(patch["id"], prompt_lab.get_prompt_lab_section())

    def test_disable_and_multi_step_rollback(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            root = Path(tmp)
            _seed_project(root)
            patch = _draft(root)
            prompt_lab.record_test_result(patch["id"], True, "passed")
            self.assertTrue(prompt_lab.activate_patch(patch["id"])[0])
            self.assertTrue(prompt_lab.disable_patch(patch["id"])[0])
            self.assertEqual(prompt_lab.get_prompt_lab_section(), "")

            # First rollback undoes disable; second independently undoes activate.
            self.assertTrue(prompt_lab.rollback()[0])
            self.assertIn(patch["id"], prompt_lab.get_prompt_lab_section())
            self.assertTrue(prompt_lab.rollback()[0])
            self.assertEqual(prompt_lab.get_prompt_lab_section(), "")

    def test_patch_validation_rejects_wrappers_and_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            root = Path(tmp)
            _seed_project(root)
            branch = prompt_lab.capture_incident("bad patch")
            with self.assertRaises(ValueError):
                prompt_lab.draft_patch(
                    branch["id"], "bad", "{{unknown}}", "reason")
            with self.assertRaises(ValueError):
                prompt_lab.draft_patch(
                    branch["id"], "bad",
                    "<prompt_opt_patch>x</prompt_opt_patch>", "reason")

    def test_background_scope_stays_bound_after_cwd_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            _seed_project(first)
            _seed_project(second)
            with _Chdir(first):
                branch = prompt_lab.capture_incident("scoped worker")
                lab_root = prompt_lab.project_root()
            with _Chdir(second), prompt_lab.project_scope(lab_root):
                patch = prompt_lab.draft_patch(
                    branch["id"], "scoped", "<rule>stay scoped</rule>", "reason")
                self.assertIsNotNone(prompt_lab.read_patch(patch["id"]))
            with _Chdir(second):
                self.assertEqual(prompt_lab.list_patches(), [])

    def test_activation_command_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            root = Path(tmp)
            _seed_project(root)
            patch = _draft(root)
            prompt_lab.record_test_result(patch["id"], True, "passed")

            with mock.patch.object(
                    laintas_cli, "_blocking_approval_prompt", return_value="no"):
                laintas_cli.handle_meta_command(
                    f"/prompt activate {patch['id']}", _Registry(), {})
            self.assertEqual(prompt_lab.get_prompt_lab_section(), "")

            with mock.patch.object(
                    laintas_cli, "_blocking_approval_prompt", return_value="yes"):
                laintas_cli.handle_meta_command(
                    f"/prompt activate {patch['id']}", _Registry(), {})
            self.assertIn(patch["id"], prompt_lab.get_prompt_lab_section())

    def test_replay_runner_compares_baseline_and_candidate_without_tools(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            root = Path(tmp)
            _seed_project(root)
            patch = _draft(root)
            calls = []

            def fake_backend(**kwargs):
                calls.append(kwargs)
                system = kwargs.get("system_prompt", "")
                if "strict prompt regression judge" in system:
                    return {"reply": (
                        '{"baseline_passed": false, "candidate_passed": true, '
                        '"reason": "candidate asks before writing"}'
                    )}
                if "<prompt_lab_patch" in system:
                    return {"reply": "I found a change. May I apply it?"}
                return {"reply": "I changed the file."}

            deps = SimpleNamespace(call_backend=fake_backend)
            with mock.patch.object(laintas_cli, "get_loop_deps", return_value=deps):
                ok, _ = laintas_cli._prompt_lab_start_test(patch["id"], {})
                self.assertTrue(ok)
                deadline = time.time() + 2
                while time.time() < deadline:
                    updated = prompt_lab.read_patch(patch["id"])
                    if updated and updated.get("test_runs"):
                        break
                    time.sleep(0.01)
            updated = prompt_lab.read_patch(patch["id"])
            self.assertTrue(updated["test_runs"][-1]["passed"])
            self.assertEqual(updated["status"], "TESTED")
            self.assertTrue(all(call.get("tools_enabled") is False for call in calls))
            self.assertEqual(prompt_lab.read_branch()["status"], "READY")


if __name__ == "__main__":
    unittest.main()
