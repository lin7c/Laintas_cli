"""The official `issues` extension, and the `account` primitive it stands on.

`ctx.laintas` lets an extension call Laintas as the signed-in person without
ever holding the session: only for extensions that declare `account`, only
/api/ paths, and a change (POST) only after the person approves it.
"""
import importlib.util
import io
import json
import tempfile
import unittest
import threading
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from rich.console import Console

import extension_manager
import extension_runtime
from scripts import build_official_extensions

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "issues"
ISSUE = "4cd0cbcb-1f5c-46f5-9ea5-ff1a7ebabeba"


def view(**extra):
    return {"id": ISSUE, "url": f"https://issues.laintas.com/#/issue/{ISSUE}", "title": "Why?", "status": "open",
            "open": True, "peopleOnly": False, "canSelect": True, "bountyCents": 100, "closedReason": None,
            "answers": [{"id": 1, "by": "ds", "solver": "ds", "trust": "provider", "body": "Because.", "version": 1.0,
                         "selected": False, "paidMicro": 0, "costMicro": 0, "folded": False},
                        {"id": 2, "by": "px", "solver": "px", "trust": "proxy", "body": "**Other** reason.",
                         "version": 2.0, "selected": False, "paidMicro": 0, "costMicro": 0, "folded": False}], **extra}


class ManifestTests(unittest.TestCase):
    def test_manifest_is_valid_official_and_declares_account(self):
        manifest = extension_manager.read_manifest(EXTENSION)
        self.assertEqual(extension_manager.validate_manifest(manifest, "issues"), [])
        self.assertIn("issues", build_official_extensions.OFFICIAL_NAMES)
        self.assertEqual(manifest["capabilities"], ["account"])

    def test_sources_are_english(self):
        for path in EXTENSION.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json", ".md"} and "__pycache__" not in path.parts:
                self.assertNotRegex(path.read_text(encoding="utf-8"), '[一-鿿]', str(path))


class AccountPrimitiveTests(unittest.TestCase):
    def _load(self, capabilities, callback):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name) / "acct"
        directory.mkdir()
        (directory / "extension.json").write_text(json.dumps({
            "schemaVersion": 2, "name": "acct", "version": "1.0.0", "entrypoint": "main.py",
            "capabilities": capabilities}))
        (directory / "main.py").write_text("CTX = None\ndef setup(ctx):\n    global CTX\n    CTX = ctx\n")
        runtime = extension_runtime.ExtensionRuntime()
        runtime.configure(account_callback=callback)
        patcher = mock.patch.object(extension_runtime, "_extension_roots", return_value=[Path(tmp.name)])
        patcher.start()
        self.addCleanup(patcher.stop)
        loaded, message = runtime.load("acct")
        self.assertTrue(loaded, message)
        return runtime.loaded_module("acct").CTX

    def test_without_the_capability_nothing_is_called(self):
        callback = mock.Mock()
        ctx = self._load(["fs.read"], callback)
        out = ctx.laintas.request("GET", "/api/chat/issues")
        self.assertFalse(out["ok"])
        self.assertIn("account", out["error"])
        callback.assert_not_called()

    def test_only_api_paths_and_a_post_says_what_it_does(self):
        callback = mock.Mock(return_value={"ok": True, "status": 200, "data": {}, "error": ""})
        ctx = self._load(["account"], callback)
        for path in ("https://evil.example/api/x", "/api/../admin", "/v1/models", "api/x"):
            self.assertFalse(ctx.laintas.request("GET", path)["ok"], path)
        self.assertFalse(ctx.laintas.request("DELETE", "/api/x")["ok"])
        self.assertIn("purpose", ctx.laintas.request("POST", "/api/x", {})["error"])
        callback.assert_not_called()
        self.assertTrue(ctx.laintas.request("POST", "/api/x", {"a": 1}, purpose="Do a thing")["ok"])
        callback.assert_called_once_with(owner="acct", method="POST", path="/api/x", body={"a": 1},
                                         purpose="Do a thing")


class BundledTests(unittest.TestCase):
    """`issues` ships with the CLI and is installed by default, but stays an extension."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "extensions"
        for target, value in ((extension_manager.paths, "global_extensions_dir"),):
            patcher = mock.patch.object(target, value, return_value=self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        roots = mock.patch.object(extension_runtime, "_extension_roots", return_value=[self.home])
        roots.start()
        self.addCleanup(roots.stop)

    def load(self):
        runtime = extension_runtime.ExtensionRuntime()
        return runtime, runtime.load("issues")

    def test_seeded_copy_loads_without_a_trust_prompt(self):
        self.assertEqual(extension_manager.seed_bundled(), ["issues"])
        self.assertEqual(json.loads((self.home / "issues" / "extension.json").read_text())["install"]["trustedBy"],
                         "bundled")
        _runtime, (loaded, message) = self.load()
        self.assertTrue(loaded, message)
        self.assertEqual(extension_manager.seed_bundled(), [])          # same version: left alone

    def test_an_edited_copy_is_not_trusted_as_bundled(self):
        extension_manager.seed_bundled()
        with open(self.home / "issues" / "main.py", "a", encoding="utf-8") as f:
            f.write("\n# edited\n")
        _runtime, (loaded, message) = self.load()
        self.assertFalse(loaded)
        self.assertIn("not trusted", message)

    def test_removal_is_remembered_until_installed_again(self):
        extension_manager.seed_bundled()
        manager = extension_manager.ExtensionManager(runtime=None, console=mock.Mock())
        self.assertTrue(manager.uninstall("issues"))
        self.assertEqual(extension_manager.seed_bundled(), [])
        self.assertFalse((self.home / "issues").exists())
        extension_manager._set_bundled_declined("issues", False)
        self.assertEqual(extension_manager.seed_bundled(), ["issues"])

    def test_an_older_seeded_copy_is_refreshed_and_a_users_own_is_not(self):
        extension_manager.seed_bundled()
        manifest = self.home / "issues" / "extension.json"
        old = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**old, "version": "0.0.1"}))
        self.assertEqual(extension_manager.seed_bundled(), ["issues"])
        mine = {**old, "version": "0.0.1", "install": {"trustedBy": "ed25519"}}
        manifest.write_text(json.dumps(mine))
        self.assertEqual(extension_manager.seed_bundled(), [])
        self.assertEqual(json.loads(manifest.read_text())["version"], "0.0.1")


class IssuesExtensionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("issues_ext_under_test", EXTENSION / "main.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.calls, self.replies = [], []
        self.out = io.StringIO()

        def request(method, path, body=None, *, purpose=""):
            self.calls.append((method, path, body, purpose))
            return self.replies.pop(0)

        ctx = mock.Mock()
        ctx.console = Console(file=self.out, force_terminal=False, width=120)
        ctx.laintas.request.side_effect = request
        self.ctx = ctx
        self.mod.setup(ctx)

    def ok(self, data):
        return {"ok": True, "status": 200, "data": data, "error": ""}

    def test_registers_two_tools_and_a_command(self):
        self.assertEqual([c.args[0] for c in self.ctx.register_tool.call_args_list], ["answers", "pick"])
        self.assertEqual(self.ctx.register_command.call_args.args[0], "issues")

    def test_answers_default_to_the_latest_question(self):
        self.replies = [self.ok({"issues": [{"id": ISSUE}]}), self.ok(view())]
        out = self.mod.tool_answers()
        self.assertEqual([c[:2] for c in self.calls], [("GET", "/api/chat/issues"), ("GET", f"/api/chat/issues/{ISSUE}")])
        self.assertEqual([(a["n"], a["answer_id"], a["unverified_upstream"]) for a in out["answers"]],
                         [(1, 1, False), (2, 2, True)])
        self.assertTrue(out["can_pick"])

    def test_pick_asks_with_a_purpose_and_returns_the_final_answer(self):
        self.replies = [self.ok(view()), self.ok(view(status="solved", open=False, canSelect=False))]
        out = self.mod.tool_pick(ISSUE, 2)
        method, path, body, purpose = self.calls[1]
        self.assertEqual((method, path, body), ("POST", f"/api/chat/issues/{ISSUE}/accept", {"answer_id": 2}))
        self.assertIn("paid its Solver", purpose)
        self.assertEqual(out["final_answer"], "**Other** reason.")

    def test_pick_refused_by_the_person_is_an_error_not_a_pick(self):
        self.replies = [self.ok(view()), {"ok": False, "status": 0, "data": {}, "error": "the person declined"}]
        with self.assertRaisesRegex(RuntimeError, "declined"):
            self.mod.tool_pick(ISSUE, 1)

    def test_metered_budget_is_explained_in_english(self):
        metered = view(costMicro=250000, remainingMicro=750000)
        self.replies = [self.ok(metered)]
        self.mod.handle([ISSUE])
        text = self.out.getvalue()
        self.assertIn("Answer costs: $0.250000", text)
        self.assertIn("Remaining budget: $0.750000", text)
        self.assertIn("paid its Solver's quote", text)
        self.assertNotRegex(text, r"[\u3400-\u9fff]")
        self.replies = [self.ok(metered), self.ok(view(status="solved", open=False, canSelect=False))]
        self.mod.tool_pick(ISSUE, 2)
        self.assertIn("paid its Solver's quote", self.calls[-1][3])

    def test_command_lists_answers_and_points_to_the_conversation(self):
        self.replies = [self.ok({"issues": [{"id": ISSUE}]}), self.ok(view())]
        self.mod.handle([])
        text = self.out.getvalue()
        self.assertIn("[1] ds", text)
        self.assertIn("unverified upstream", text)
        self.assertIn("pick answer 2 as final", text)

    def task_view(self, awaiting=False):
        offer = {'id': 7, 'applicant': {'id': 'worker', 'name': 'Worker'}, 'status': 'applied',
                 'billing': 'postpaid', 'human_fee_usd': 1, 'note': 'I can help', 'deadline_hours': 24,
                 'credit': {'done': 3}, 'squad': [{'name': 'worker-ai', 'model': 'example-model'}]}
        run = {'id': ISSUE, 'state': 'declared', 'cost': {'verified_usd': .2},
               'checklist': [{'index': 1, 'text': 'Works at 360px'}],
               'declaration': {'status': 'awaiting', 'items': [{'index': 1, 'evidence': 'Viewport checked'}]}}
        return view(peopleOnly=True, canSelect=False, answers=[], task={
            'escrow_usd': 5, 'applications_list': [offer], 'execution': run if awaiting else None})

    def _long_view(self):
        body = "start " + "filler " * 80 + "THEEND"
        return view(answers=[{"id": 1, "by": "ds", "solver": "ds", "trust": "provider", "body": body,
                              "version": 1.0, "selected": True, "paidMicro": 0, "costMicro": 0, "folded": False}])

    def test_command_previews_long_answers_by_default(self):
        self.replies = [self.ok(self._long_view())]
        self.mod.handle([ISSUE])
        text = self.out.getvalue()
        self.assertNotIn("THEEND", text)
        self.assertIn("PICKED", text)
        self.assertIn("shows one in full", text)

    def test_command_expands_the_answer_named_by_number(self):
        self.replies = [self.ok(self._long_view())]
        self.mod.handle([ISSUE, "1"])
        self.assertIn("THEEND", self.out.getvalue())

    def test_task_display_shows_applicants_quotes_and_chat(self):
        self.replies = [self.ok(self.task_view())]
        self.mod.handle([ISSUE])
        output = self.out.getvalue()
        self.assertIn('Application 7', output)
        self.assertIn('$1.00 human fee', output)
        self.assertIn('/issues chat 7', output)

    def test_choose_is_explicit_approved_and_uses_task_endpoint(self):
        self.replies = [self.ok(self.task_view()), self.ok(self.task_view())]
        self.mod.handle(['choose', ISSUE, '7'])
        method, path, body, purpose = self.calls[-1]
        self.assertEqual((method, path, body), ('POST', f'/api/chat/issues/{ISSUE}/task/pick', {'answer_id': 7}))
        self.assertIn('Share the sealed', purpose)
        self.assertIn('$1.00', purpose)

    def test_accept_shows_evidence_and_binds_verdict_to_run(self):
        self.replies = [self.ok(self.task_view(True)), self.ok(self.task_view(True))]
        self.mod.handle(['accept', ISSUE])
        self.assertIn('Viewport checked', self.out.getvalue())
        self.assertEqual(self.calls[-1][1:3], (f'/api/chat/issues/{ISSUE}/task/accept', {'run_id': ISSUE}))
        self.assertIn('settle', self.calls[-1][3])

    def test_reject_carries_item_and_reason(self):
        self.replies = [self.ok(self.task_view(True)), self.ok(self.task_view(True))]
        self.mod.handle(['reject', ISSUE, '1', 'Still', 'overflows'])
        self.assertEqual(self.calls[-1][2], {'run_id': ISSUE, 'item': 1, 'reason': 'Still overflows'})

    def test_task_model_tool_does_not_treat_applications_as_answers(self):
        self.replies = [self.ok(self.task_view())]
        result = self.mod.tool_answers(ISSUE)
        self.assertFalse(result['can_pick'])
        self.assertIn('/issues choose', result['note'])

    def test_live_chat_send_and_exit_stop_the_worker(self):
        chat = {'id': 7, 'title': 'Task', 'peer': {'name': 'Worker'}, 'messages': []}
        sent = {**chat, 'messages': [{'id': 1, 'text': 'Hello', 'mine': True}]}
        self.replies = [self.ok(chat), self.ok(sent)]
        session = mock.Mock()
        session.prompt.side_effect = ['Hello', '/exit']
        with mock.patch('prompt_toolkit.PromptSession', return_value=session), \
                mock.patch('prompt_toolkit.patch_stdout.patch_stdout', return_value=nullcontext()):
            self.mod._chat(7)
        self.assertEqual(self.calls[-1][0:2], ('POST', '/api/issues/conversations/7'))
        self.assertEqual(self.calls[-1][2]['text'], 'Hello')
        self.assertTrue(self.calls[-1][2]['client_id'])
        self.assertFalse(any(t.name == 'issues-private-chat' for t in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
