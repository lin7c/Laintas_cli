"""AI-PoW as an extension: the event contract, the routing, the commands.

AI-PoW used to be four top-level modules with calls into them from six places
in the core. It is an official extension now, fed by `ctx.on` lifecycle
events, and the property that motivated the move is tested here directly: a
session started outside a repository records the work into the repository the
agent actually worked in -- before, it recorded nothing at all.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import extension_manager
import extension_runtime
import usage_tracker
from scripts import build_official_extensions

from tests.extension_packages import RecordingConsole

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "ai-pow"
VENDORED = ("ai_pow.py", "ai_pow_scoring.py", "ai_pow_report.py")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    # Commits in these tests must not run a real post-commit hook.
    git(path, "config", "core.hooksPath", "/dev/null")
    return os.path.realpath(str(path))


def events(root: str) -> list:
    database = Path(root) / ".git" / "ai-pow" / "events.sqlite3"
    connection = sqlite3.connect(database)
    try:
        return [json.loads(row[0]) for row in
                connection.execute("SELECT body FROM events ORDER BY seq")]
    finally:
        connection.close()


def kinds(root: str) -> list:
    return [event["type"] for event in events(root)]


class ManifestTests(unittest.TestCase):
    def test_manifest_is_valid_official_and_declares_observe(self):
        manifest = extension_manager.read_manifest(EXTENSION)
        self.assertEqual(extension_manager.validate_manifest(manifest, "ai-pow"), [])
        self.assertIn("ai-pow", build_official_extensions.OFFICIAL_NAMES)
        self.assertIn("observe", manifest["capabilities"])

    def test_sources_are_english(self):
        for path in EXTENSION.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json", ".md"} \
                    and "__pycache__" not in path.parts:
                self.assertNotRegex(path.read_text(encoding="utf-8"),
                                    r"[一-鿿]", str(path))

    def test_vendor_matches_independent_source_when_available(self):
        source = ROOT.parent / "ai-pow"
        if not (source / "ai_pow.py").is_file():
            self.skipTest("no ai-pow checkout alongside this repository")
        for name in VENDORED:
            self.assertEqual((source / name).read_bytes(),
                             (EXTENSION / name).read_bytes(), name)

    def test_publication_archive_ships_the_vendored_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ai-pow.lext"
            extension_manager.create_publication_archive(EXTENSION, archive)
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
        for name in ("main.py", "router.py", *VENDORED):
            self.assertIn(name, names)

    def test_core_no_longer_knows_ai_pow(self):
        manifest = json.loads((ROOT / "package_manifest.json").read_text())
        for module in ("ai_pow", "ai_pow_scoring", "ai_pow_report", "aipow_bridge"):
            self.assertNotIn(module, manifest["modules"])
            self.assertFalse((ROOT / f"{module}.py").exists(), module)
        for module in manifest["modules"]:
            text = (ROOT / f"{module}.py").read_text(encoding="utf-8")
            self.assertNotIn("aipow_bridge", text, module)
            self.assertNotIn("import ai_pow", text, module)


class ObserveContractTests(unittest.TestCase):
    """`ctx.on` itself, independent of AI-PoW."""

    def _load(self, capabilities, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name) / "watcher"
        directory.mkdir()
        (directory / "extension.json").write_text(json.dumps({
            "schemaVersion": 2, "name": "watcher", "version": "1.0.0",
            "entrypoint": "main.py", "capabilities": capabilities}))
        (directory / "main.py").write_text(body)
        runtime = extension_runtime.ExtensionRuntime()
        patcher = mock.patch.object(extension_runtime, "_extension_roots",
                                    return_value=[Path(tmp.name)])
        patcher.start()
        self.addCleanup(patcher.stop)
        return runtime, runtime.load("watcher")

    def test_observing_requires_the_capability(self):
        _runtime, (loaded, message) = self._load(
            ["fs.read"], "def setup(ctx):\n    ctx.on('tool.call', print)\n")
        self.assertFalse(loaded)
        self.assertIn("observe", message)

    def test_unknown_events_are_refused(self):
        _runtime, (loaded, message) = self._load(
            ["observe"], "def setup(ctx):\n    ctx.on('tool.maybe', print)\n")
        self.assertFalse(loaded)
        self.assertIn("unknown event", message)

    def test_handlers_see_a_copy_cannot_break_the_host_and_leave_on_unload(self):
        runtime, (loaded, message) = self._load(["observe"], (
            "seen = []\n"
            "def setup(ctx):\n"
            "    ctx.on('tool.call', lambda p: (seen.append(p), p.clear()))\n"
            "    ctx.on('tool.call', lambda p: 1 / 0)\n"))
        self.assertTrue(loaded, message)
        module = runtime.loaded_module("watcher")
        payload = {"name": "fs.read"}
        runtime.emit("tool.call", payload)  # the raising handler is contained
        self.assertEqual(payload, {"name": "fs.read"})
        self.assertEqual(len(module.seen), 1)
        runtime.unload("watcher")
        runtime.emit("tool.call", payload)
        self.assertEqual(len(module.seen), 1)
        self.assertEqual(runtime._observers, {})

    def test_turn_end_is_raised_however_the_loop_leaves(self):
        import agent_loop
        seen = []
        runtime = extension_runtime.ExtensionRuntime()
        runtime._capabilities["t"] = frozenset({"observe"})
        runtime.subscribe("t", "turn.end", seen.append)

        @agent_loop._observed_turn
        def loop(fail, inner=None):
            if inner:
                inner()
            if fail:
                raise RuntimeError("boom")
            return agent_loop._current_turn_depth()

        with mock.patch.object(extension_runtime, "_runtime", runtime):
            self.assertEqual(loop(False, inner=lambda: loop(False)), 1)
            with self.assertRaises(RuntimeError):
                loop(True)
        self.assertEqual([p["depth"] for p in seen], [2, 1, 1])
        self.assertEqual(agent_loop._current_turn_depth(), 0)


class TargetTests(unittest.TestCase):
    def setUp(self):
        runtime = extension_runtime.ExtensionRuntime()
        with mock.patch.object(extension_runtime, "_extension_roots",
                               return_value=[ROOT / "extensions"]):
            loaded, message = runtime.load("ai-pow")
        self.assertTrue(loaded, message)
        self.addCleanup(runtime.unload, "ai-pow")
        self.router = sys.modules[runtime.loaded_module("ai-pow").__name__ + ".router"]

    def test_file_tools_resolve_relative_paths_and_patches(self):
        targets = self.router.tool_targets
        self.assertEqual(targets("fs.edit", {"path": "src/a.py"}, "/w"),
                         [os.path.realpath("/w/src/a.py")])
        patch = "*** Begin Patch\n*** Update File: x/y.py\n*** Move to: z.py\n"
        self.assertEqual(targets("apply_patch", {"input": patch}, "/w"),
                         [os.path.realpath("/w/x/y.py"), os.path.realpath("/w/z.py")])
        self.assertEqual(targets("web.search", {"path": "/etc"}, "/w"), [])

    def test_shell_follows_cd_and_git_dash_c(self):
        found = self.router.tool_targets(
            "shell.exec", {"command": "cd laintas_cli && git -C ../ai-pow status; cd $HOME"},
            "/root")
        self.assertEqual(found, ["/root", os.path.realpath("/root/laintas_cli"),
                                 os.path.realpath("/root/ai-pow")])
        self.assertEqual(self.router.tool_targets(
            "shell.exec", {"command": "ls", "cwd": "sub"}, "/w"), [os.path.realpath("/w/sub")])
        # Unbalanced quotes must not raise.
        self.router.shell_directories("cd 'broken", "/w")

    def test_new_file_belongs_to_the_repository_of_its_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            self.assertEqual(self.router.find_repo_root(
                os.path.join(root, "not", "yet", "there.py")), root)
            self.assertIsNone(self.router.find_repo_root(tmp))


class RoutingTests(unittest.TestCase):
    """The extension loaded through the real runtime, fed real events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="laintas-aipow-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        self.app = make_repo(self.home / "app")
        self.lib = make_repo(self.home / "lib")
        self.runtime = extension_runtime.ExtensionRuntime()
        self.console = RecordingConsole()
        self.runtime.configure(console=self.console)
        patcher = mock.patch.object(extension_runtime, "_extension_roots",
                                    return_value=[ROOT / "extensions"])
        patcher.start()
        self.addCleanup(patcher.stop)
        # Core call sites raise events through the module-level runtime.
        runtime_patch = mock.patch.object(extension_runtime, "_runtime", self.runtime)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        loaded, message = self.runtime.load("ai-pow")
        self.assertTrue(loaded, message)
        self.addCleanup(self.runtime.unload, "ai-pow")
        self.module = self.runtime.loaded_module("ai-pow")
        self.pow = self.module.ai_pow
        self.router = self.module._router
        self.run_counter = 0

    def init(self, root):
        self.pow.Recorder(root).init()
        self.router.forget(root)

    def start(self, text="Please change it", cwd=None, foreground=True, depth=1):
        self.run_counter += 1
        extension_runtime.emit(
            "turn.start", session_id="s", run_id=f"r{self.run_counter}",
            agent_id="main", cwd=str(cwd or self.home), depth=depth,
            foreground=foreground, human_text=text)

    def end(self, depth=1):
        extension_runtime.emit("turn.end", depth=depth)

    def call(self, name, arguments, call_id="c1", cwd=None):
        extension_runtime.emit("tool.call", name=name, call_id=call_id,
                               arguments=arguments, cwd=str(cwd or self.home),
                               source="builtin", session_id="s", run_id="r")

    def result(self, call_id="c1", name="fs.write", ok=True):
        extension_runtime.emit("tool.result", name=name, call_id=call_id, ok=ok,
                               session_id="s", run_id="r")
        extension_runtime.emit("tool.batch_end", session_id="s", run_id="r")

    def test_started_outside_a_repository_records_where_the_work_is(self):
        self.init(self.app)
        target = Path(self.app) / "main.py"
        self.start("SECRET PROMPT")
        extension_runtime.emit("assistant.reply", text="Editing now", final=False,
                               visible=True, session_id="s", run_id="r", agent_id="main")
        self.call("fs.write", {"path": "app/main.py", "content": "DO NOT STORE"})
        target.write_text("value = 1\n")
        self.result()
        extension_runtime.emit("assistant.reply", text="Done", final=True,
                               visible=True, session_id="s", run_id="r", agent_id="main")
        self.end()

        order = kinds(self.app)
        self.assertEqual(order.count("human.message"), 1)
        self.assertEqual(order.count("assistant.visible"), 2)
        # message -> (baseline sample) -> call -> observed change
        self.assertLess(order.index("human.message"), order.index("tool.call"))
        self.assertLess(order.index("tool.call"), order.index("file.observed"))
        self.assertEqual(self.router.focus, self.app)
        data = (Path(self.app) / ".git" / "ai-pow" / "events.sqlite3").read_bytes()
        self.assertNotIn(b"SECRET PROMPT", data)
        self.assertNotIn(b"DO NOT STORE", data)

    def test_a_turn_across_two_repositories_splits_its_tool_calls(self):
        self.init(self.app)
        self.init(self.lib)
        self.start()
        self.call("fs.edit", {"path": f"{self.app}/a.py"}, call_id="a")
        self.result("a")
        self.call("shell.exec", {"command": "cd lib && make"}, call_id="b")
        self.result("b", name="shell.exec")
        self.end()
        for root, name in ((self.app, "fs.edit"), (self.lib, "shell.exec")):
            with self.subTest(root=root):
                recorded = events(root)
                self.assertEqual([e["data"]["name"] for e in recorded
                                  if e["type"] == "tool.call"], [name])
                self.assertEqual(kinds(root).count("human.message"), 1)
                self.assertEqual(kinds(root).count("tool.result"), 1)

    def test_a_chat_only_turn_counts_toward_the_focus(self):
        self.init(self.app)
        self.start()
        self.call("fs.read", {"path": f"{self.app}/README"})
        self.result(name="fs.read")
        self.end()
        self.start("What did you just change?")
        with mock.patch.object(usage_tracker, "_usage_dir", return_value=self.home), \
                mock.patch.object(usage_tracker, "_SESSION", []):
            usage_tracker.record(model="example", prompt_tokens=100,
                                 completion_tokens=20, official=True,
                                 cost_cents=2, cached_prompt_tokens=10)
        self.assertEqual(kinds(self.app).count("human.message"), 1)  # not yet
        self.end()
        self.assertEqual(kinds(self.app).count("human.message"), 2)
        usage = [e["data"] for e in events(self.app) if e["type"] == "model.usage"]
        self.assertEqual(usage[-1]["input_tokens"], 100)
        self.assertEqual(usage[-1]["actual_usd"], "0.02")

    def test_repositories_that_are_not_recording_get_nothing(self):
        self.start()
        self.call("fs.write", {"path": f"{self.lib}/x.py"})
        self.result()
        self.end()
        self.assertFalse((Path(self.lib) / ".git" / "ai-pow").exists())
        self.assertEqual(self.router.unrecorded, [self.lib])
        self.module.handle(["/pow"])
        self.assertIn("worked in, not recording", self.console.text)

    def test_a_nested_loop_ending_does_not_end_its_caller(self):
        self.init(self.app)
        self.start(depth=1)
        self.start(text=None, foreground=False, depth=2)
        self.call("fs.read", {"path": f"{self.lib}/x"}, call_id="inner")
        self.end(depth=2)
        self.call("fs.write", {"path": f"{self.app}/x.py"}, call_id="outer")
        self.end(depth=1)
        self.assertEqual(kinds(self.app).count("human.message"), 1)

    def test_threads_keep_their_own_turns(self):
        self.init(self.app)
        self.init(self.lib)
        errors = []

        def worker():
            try:
                extension_runtime.emit("turn.start", session_id="s", run_id="child",
                                       agent_id="child", cwd=self.lib, depth=1,
                                       foreground=False, human_text=None)
                self.call("fs.write", {"path": f"{self.lib}/c.py"}, call_id="child")
                self.end()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        self.start()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.call("fs.write", {"path": f"{self.app}/p.py"}, call_id="parent")
        self.end()
        self.assertFalse(errors, errors)
        self.assertEqual(kinds(self.lib).count("human.message"), 0)
        self.assertEqual(kinds(self.app).count("human.message"), 1)

    def test_pause_writes_a_gap_and_records_nothing(self):
        self.init(self.app)
        self.start()
        self.call("fs.read", {"path": f"{self.app}/x"})
        self.end()
        before = len(events(self.app))
        self.module.handle(["/pow", "off"])
        self.start()
        self.call("fs.write", {"path": f"{self.app}/x"}, call_id="c2")
        self.end()
        recorded = events(self.app)
        self.assertEqual(len(recorded), before + 1)
        self.assertEqual(recorded[-1]["data"], {"reason": "paused_by_user"})
        self.module.handle(["/pow", "on"])
        self.assertFalse(self.router.paused)

    def test_a_stale_hook_of_ours_is_repointed_on_first_use(self):
        rec = self.pow.Recorder(self.app)
        rec.init()
        git(self.app, "config", "--unset", "core.hooksPath")
        with mock.patch.object(self.pow, "HOST_INVOCATION", ["/gone/ai_pow.py"]):
            self.assertTrue(self.pow.install_hook(rec)["installed"])
        self.assertEqual(self.pow.hook_status(rec)["state"], "stale")
        self.router.forget(self.app)
        self.start()
        self.call("fs.read", {"path": f"{self.app}/x"})
        self.end()
        self.assertEqual(self.pow.hook_status(rec)["state"], "installed")

    def test_commands_init_status_report(self):
        with mock.patch.object(self.module, "_cwd", return_value=self.app):
            self.module.handle(["/pow", "init"])
            self.assertIn("AI-PoW recording", self.console.text)
            self.assertTrue((Path(self.app) / ".git" / "ai-pow" / "events.sqlite3").is_file())
            self.start(cwd=self.app)
            self.call("fs.write", {"path": "a.py"}, cwd=self.app)
            (Path(self.app) / "a.py").write_text("x = 1\n")
            self.result()
            self.end()
            git(self.app, "add", "a.py")
            git(self.app, "commit", "-qm", "first")
            self.pow.Recorder(self.app).seal()
            self.module.handle(["/pow"])
            self.assertIn("hook", self.console.text)
            self.assertRegex(self.console.text, r"HEAD [0-9a-f]{7}: \d+\.\d \(")
            self.module.handle(["/pow", "report"])
            self.assertIn("reports", self.console.text)
            self.module.handle(["/pow", "verify"])
            self.assertIn("verified", self.console.text)

    def test_init_outside_a_repository_names_the_candidates(self):
        self.start()
        self.call("fs.read", {"path": f"{self.lib}/x"})
        self.end()
        with mock.patch.object(self.module, "_cwd", return_value=str(self.home)):
            self.module.handle(["/pow", "init"])
        self.assertIn("not inside a Git repository", self.console.text)
        self.assertIn(self.lib, self.console.text)
        self.assertFalse((Path(self.lib) / ".git" / "ai-pow").exists())

    def test_the_command_declares_its_argument_contract(self):
        import laintas_cli
        self.assertIn("/pow", self.runtime.command_names())
        self.assertEqual(self.runtime.command_arg_rule("/pow", "export")[0], 3)
        self.assertEqual(self.runtime.command_arg_rule("/pow", "history")[0], 1)
        def validate(command):
            # Exactly what the REPL does with a typed line.
            action, _raw, parts = laintas_cli._parse_slash_command(command)
            laintas_cli._validate_slash_args(action, parts[1:])

        for command in ("/pow history extra", "/pow init a b"):
            with self.subTest(command=command):
                with self.assertRaises(laintas_cli.SlashCommandUsageError):
                    validate(command)
        validate("/pow export out.jsonl HEAD~1")
        validate("/pow")


class LauncherTests(unittest.TestCase):
    def test_laintas_pow_serves_the_standalone_cli(self):
        import laintas_cli
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            runtime = extension_runtime.ExtensionRuntime()
            with mock.patch.object(extension_runtime, "_runtime", runtime), \
                    mock.patch.object(extension_runtime, "_extension_roots",
                                      return_value=[ROOT / "extensions"]), \
                    mock.patch("sys.stdout") as out:
                code = laintas_cli._run_pow_command(["--cwd", root, "init", "--no-hook"])
                self.assertEqual(code, 0)
                module = runtime.loaded_module("ai-pow")
                # The hook would name the launcher, never the extension's files.
                self.assertIn("laintas_cli.py", " ".join(module.ai_pow.invocation()))
                self.assertNotIn("extensions", " ".join(module.ai_pow.invocation()))
                runtime.unload("ai-pow")
            self.assertTrue((Path(root) / ".git" / "ai-pow" / "events.sqlite3").is_file())
            self.assertTrue(out.write.called)

    def test_a_missing_extension_is_a_clear_error(self):
        import laintas_cli
        with tempfile.TemporaryDirectory() as tmp:
            runtime = extension_runtime.ExtensionRuntime()
            with mock.patch.object(extension_runtime, "_runtime", runtime), \
                    mock.patch.object(extension_runtime, "_extension_roots",
                                      return_value=[Path(tmp)]), \
                    mock.patch("sys.stderr") as err:
                self.assertEqual(laintas_cli._run_pow_command(["status"]), 1)
            written = "".join(call.args[0] for call in err.write.call_args_list)
            self.assertIn("/extensions install ai-pow", written)


if __name__ == "__main__":
    unittest.main()
