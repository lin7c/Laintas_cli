import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import evolution_lab
import extension_runtime
import laintas_cli
import paths


class _Chdir:
    def __init__(self, value):
        self.value = str(value)

    def __enter__(self):
        self.previous = os.getcwd()
        os.chdir(self.value)

    def __exit__(self, *exc):
        os.chdir(self.previous)


def _extension_files(name="hello", reply="hello"):
    return [
        {
            "path": "extension.json",
            "content": json.dumps({
                "schemaVersion": 1, "name": name, "version": "0.1.0",
                "entrypoint": "main.py", "enabled": True,
            }),
        },
        {
            "path": "main.py",
            "content": (
                "def setup(ctx):\n"
                f"    ctx.register_command('/{name}', lambda parts, raw='': '{reply}')\n"
            ),
        },
    ]


class EvolutionLabTests(unittest.TestCase):
    def test_create_test_activate_invoke_and_rollback_extension(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp), \
                mock.patch.object(paths, "TRUST_FILE", Path(tmp) / "trust.json"):
            runtime = extension_runtime.ExtensionRuntime()
            runtime.configure(backend_callback=lambda **kwargs: {"reply": "gateway"})
            branch = evolution_lab.create_branch("create a hello command")
            candidate = evolution_lab.draft_candidate(
                branch["id"], "Hello", "extension", "hello",
                _extension_files(), description="A hello command")

            ok, _, run = evolution_lab.test_candidate(candidate["id"])
            self.assertTrue(ok, run)
            ok, message = evolution_lab.activate_candidate(
                candidate["id"], runtime=runtime)
            self.assertTrue(ok, message)
            handled, result = runtime.invoke_command("/hello", ["/hello"])
            self.assertTrue(handled)
            self.assertEqual(result, "hello")
            self.assertEqual(runtime.list()[0]["name"], "hello")

            ok, message = evolution_lab.rollback(runtime)
            self.assertTrue(ok, message)
            self.assertEqual(runtime.list(), [])
            self.assertFalse((paths.extensions_dir() / "hello").exists())

    def test_test_receipt_is_bound_to_candidate_file_content(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            runtime = extension_runtime.ExtensionRuntime()
            branch = evolution_lab.create_branch("create a hello command")
            candidate = evolution_lab.draft_candidate(
                branch["id"], "Hello", "extension", "hello", _extension_files())
            self.assertTrue(evolution_lab.test_candidate(candidate["id"])[0])

            path = evolution_lab._item_path("candidates", candidate["id"])
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["files"][1]["content"] = changed["files"][1]["content"].replace(
                "'hello'", "'changed'")
            path.write_text(json.dumps(changed), encoding="utf-8")

            ok, message = evolution_lab.activate_candidate(
                candidate["id"], runtime=runtime)
            self.assertFalse(ok)
            self.assertIn("passing test", message)

    def test_backend_gateway_exposes_results_not_raw_session(self):
        captured = {}

        def callback(**kwargs):
            captured.update(kwargs)
            return {"reply": "metered-result", "_billing": {"receipt": "r1"}}

        gateway = extension_runtime.BackendGateway(callback)
        result = gateway.chat("hello", system_prompt="system", token="forbidden")
        self.assertEqual(result["reply"], "metered-result")
        self.assertEqual(captured["message"], "hello")
        self.assertNotIn("token", captured)

    def test_commands_candidate_gets_exact_file_trust(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp), \
                mock.patch.object(paths, "TRUST_FILE", Path(tmp) / "trust.json"):
            paths.ensure_project_dir()
            target = paths.project_file(paths.CWD_COMMANDS)
            target.write_text("def handle_extra_command(action, parts, ctx): return False\n",
                              encoding="utf-8")
            branch = evolution_lab.create_branch("improve commands", "IMPROVE")
            candidate = evolution_lab.draft_candidate(
                branch["id"], "Commands", "commands", "commands",
                [{"path": "commands.py", "content": (
                    "def handle_extra_command(action, parts, ctx):\n"
                    "    return action == '/creative'\n"
                )}])
            self.assertTrue(evolution_lab.test_candidate(candidate["id"])[0])
            self.assertTrue(evolution_lab.activate_candidate(candidate["id"])[0])
            import trust_store
            self.assertTrue(trust_store.is_execution_allowed(target)[0])

    def test_project_scoping_matches_prompt_lab_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            first.mkdir()
            second.mkdir()
            with _Chdir(first):
                evolution_lab.create_branch("first feature")
                self.assertEqual(len(evolution_lab.list_branches()), 1)
            with _Chdir(second):
                self.assertEqual(evolution_lab.list_branches(), [])

    def test_candidate_tests_file_is_part_of_activation_gate(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            files = _extension_files()
            files.append({
                "path": "tests.py",
                "content": "raise SystemExit('candidate behavior failed')\n",
            })
            branch = evolution_lab.create_branch("create tested extension")
            candidate = evolution_lab.draft_candidate(
                branch["id"], "Tested", "extension", "hello", files)
            ok, _, run = evolution_lab.test_candidate(candidate["id"])
            self.assertFalse(ok)
            self.assertFalse(run["report"]["candidate"]["tests"]["passed"])
            activated, message = evolution_lab.activate_candidate(
                candidate["id"], runtime=extension_runtime.ExtensionRuntime())
            self.assertFalse(activated)
            self.assertIn("passing test", message)

    def test_reserved_builtin_command_rejects_extension_load(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            runtime = extension_runtime.ExtensionRuntime()
            runtime.configure(reserved_commands=["/reload"])
            directory = paths.extensions_dir() / "bad"
            directory.mkdir(parents=True)
            (directory / "extension.json").write_text(json.dumps({
                "schemaVersion": 1, "name": "bad", "version": "1.0",
                "entrypoint": "main.py",
            }), encoding="utf-8")
            (directory / "main.py").write_text(
                "def setup(ctx):\n"
                "    ctx.register_command('/reload', lambda parts: None)\n",
                encoding="utf-8")
            ok, message = runtime.load("bad")
            self.assertFalse(ok)
            self.assertIn("reserved", message)

    def test_multifile_extension_supports_relative_imports(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            files = _extension_files()
            files[1]["content"] = (
                "from .helper import message\n"
                "def setup(ctx):\n"
                "    ctx.commands.register('/hello', lambda parts: message())\n"
            )
            files.append({
                "path": "helper.py",
                "content": "def message(): return 'from-helper'\n",
            })
            branch = evolution_lab.create_branch("create multifile extension")
            candidate = evolution_lab.draft_candidate(
                branch["id"], "Multi", "extension", "hello", files)
            self.assertTrue(evolution_lab.test_candidate(candidate["id"])[0])
            runtime = extension_runtime.ExtensionRuntime()
            self.assertTrue(evolution_lab.activate_candidate(
                candidate["id"], runtime=runtime)[0])
            self.assertEqual(runtime.invoke_command("/hello", ["/hello"])[1],
                             "from-helper")

    def test_reload_reconciliation_marks_compat_candidate_reset(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp), \
                mock.patch.object(paths, "TRUST_FILE", Path(tmp) / "trust.json"):
            paths.ensure_project_dir()
            target = paths.project_file(paths.CWD_LOOP)
            target.write_text("def handle_loop_command(command, ctx): return None\n",
                              encoding="utf-8")
            branch = evolution_lab.create_branch("improve loop")
            candidate = evolution_lab.draft_candidate(
                branch["id"], "Loop", "loop", "loop", [{
                    "path": "loop.py",
                    "content": "def handle_loop_command(command, ctx): return 'new'\n",
                }])
            self.assertTrue(evolution_lab.test_candidate(candidate["id"])[0])
            self.assertTrue(evolution_lab.activate_candidate(candidate["id"])[0])
            target.write_text("def handle_loop_command(command, ctx): return None\n",
                              encoding="utf-8")
            evolution_lab.reconcile_workspace()
            self.assertEqual(
                evolution_lab.read_candidate(candidate["id"])["status"],
                "RESET_BY_RELOAD")

    def test_evolve_command_captures_idea_without_mutating_active_code(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp), \
                mock.patch.object(laintas_cli, "_evolution_lab_start_worker",
                                  return_value=None):
            laintas_cli.handle_meta_command(
                "/evolve create a release-notes command", mock.Mock(), {})
            branch = evolution_lab.read_branch()
            self.assertIsNotNone(branch)
            self.assertEqual(branch["intent"], "CREATE")
            self.assertIn("release-notes", branch["description"])
            self.assertEqual(extension_runtime.get_runtime().list(), [])


if __name__ == "__main__":
    unittest.main()
