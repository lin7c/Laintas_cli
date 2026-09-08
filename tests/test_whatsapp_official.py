import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path

import extension_manager
import extension_runtime
from scripts import build_official_extensions


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "whatsapp"
BRIDGE = EXTENSION / "bridge" / "bridge.mjs"


def _load_main():
    spec = importlib.util.spec_from_file_location(
        "whatsapp_under_test", EXTENSION / "main.py",
        submodule_search_locations=[str(EXTENSION)])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _have_node() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class ManifestTests(unittest.TestCase):
    def test_manifest_is_valid_and_official(self):
        manifest = extension_manager.read_manifest(EXTENSION)
        self.assertEqual(extension_manager.validate_manifest(manifest, "whatsapp"), [])
        self.assertIn("whatsapp", build_official_extensions.OFFICIAL_NAMES)

    def test_sources_are_english(self):
        for path in EXTENSION.rglob("*"):
            if (not path.is_file()
                    or extension_runtime._is_unmanaged(path.relative_to(EXTENSION))
                    or path.suffix not in {".py", ".json", ".md", ".mjs"}):
                continue
            self.assertNotRegex(path.read_text(encoding="utf-8"),
                                r"[一-鿿]", str(path))


class TaskRunnerContractTests(unittest.TestCase):
    """An extension needs the agent, not just the model.

    `BackendGateway.chat` generates text. A channel extension that is a remote
    control -- "check the disk", "fix this file" -- needs the agent loop, and
    one built on `chat` can hold a conversation while being unable to do a
    single thing it is asked."""

    def test_the_context_offers_task_execution(self):
        self.assertIn("tasks", extension_runtime.ExtensionContext.__dataclass_fields__)
        self.assertTrue(hasattr(extension_runtime, "TaskRunner"))

    def test_an_unconfigured_runner_fails_rather_than_pretending(self):
        result = extension_runtime.TaskRunner().run("do a thing")
        self.assertFalse(result["ok"])
        self.assertIn("not available", result["error"])

    def test_the_runner_passes_the_conversation_through(self):
        seen = {}

        def callback(text, conversation="", on_progress=None):
            seen.update(text=text, conversation=conversation)
            return {"ok": True, "reply": "done", "error": ""}

        result = extension_runtime.TaskRunner(callback).run(
            "check disk", conversation="whatsapp:1@s.whatsapp.net")
        self.assertTrue(result["ok"])
        self.assertEqual(seen["conversation"], "whatsapp:1@s.whatsapp.net")

    def test_the_extension_executes_rather_than_chats(self):
        source = (EXTENSION / "main.py").read_text()
        # The inbound path must reach the agent. `_backend.chat` survives only
        # for summarising the agent's own output.
        self.assertIn("_tasks.run(", source)
        run_one = source.split("def _run_one(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_tasks.run(", run_one)
        self.assertNotIn("_backend.chat", run_one)


class AccessControlTests(unittest.TestCase):
    """One rule: only the account's own chat is acted on."""

    def setUp(self):
        self.module = _load_main()
        self.module._log = lambda *_: None
        while not self.module._task_queue.empty():
            self.module._task_queue.get_nowait()

    def test_the_self_chat_is_accepted(self):
        self.module._on_message({"text": "check disk", "selfChat": True,
                                 "remoteJid": "me@s.whatsapp.net", "key": {}})
        self.assertEqual(self.module._task_queue.qsize(), 1)

    def test_a_stranger_is_ignored_entirely(self):
        # Not answered, not executed. "run this" from someone else's phone is
        # not a capability this extension has.
        self.module._on_message({"text": "rm -rf /", "selfChat": False,
                                 "remoteJid": "8613800138000@s.whatsapp.net",
                                 "name": "Someone", "key": {}})
        self.assertEqual(self.module._task_queue.qsize(), 0)

    def test_a_group_is_ignored_entirely(self):
        self.module._on_message({"text": "deploy", "selfChat": False,
                                 "isGroup": True, "remoteJid": "123@g.us",
                                 "key": {}})
        self.assertEqual(self.module._task_queue.qsize(), 0)

    def test_empty_text_is_not_a_task(self):
        self.module._on_message({"text": "   ", "selfChat": True,
                                 "remoteJid": "me@s.whatsapp.net", "key": {}})
        self.assertEqual(self.module._task_queue.qsize(), 0)


class ExecutionFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_main()
        self.module._log = lambda *_: None
        self.sent: list[tuple] = []
        self.reacted: list[str] = []
        self.module._send = lambda jid, text: (self.sent.append((jid, text)), (True, ""))[1]
        self.module._react = lambda jid, key, emoji: self.reacted.append(emoji)

        class Tasks:
            def __init__(self, outer):
                self.outer = outer
                self.calls = []

            def run(self, text, conversation="", on_progress=None):
                self.calls.append({"text": text, "conversation": conversation})
                return self.outer.result

        self.tasks = Tasks(self)
        self.module._tasks = self.tasks
        self.result = {"ok": True, "reply": "disk is at 57%", "error": ""}

    def _job(self, text="check disk"):
        return {"text": text, "jid": "me@s.whatsapp.net", "key": {"id": "x"}}

    def test_a_task_is_acknowledged_then_answered(self):
        self.module._run_one(self._job())
        # Acknowledged before running: a long task must not look like nothing
        # happened.
        self.assertEqual(self.reacted[0], "⏳")
        self.assertEqual(self.reacted[-1], "✅")
        self.assertIn("disk is at 57%", self.sent[0][1])

    def test_the_conversation_is_scoped_to_the_chat(self):
        self.module._run_one(self._job())
        self.assertEqual(self.tasks.calls[0]["conversation"],
                         "whatsapp:me@s.whatsapp.net")

    def test_a_failed_task_says_so(self):
        self.result = {"ok": False, "reply": "", "error": "tool exploded"}
        self.module._run_one(self._job())
        self.assertEqual(self.reacted[-1], "❌")
        self.assertIn("tool exploded", self.sent[0][1])

    def test_a_task_that_produced_nothing_is_not_reported_as_an_answer(self):
        self.result = {"ok": True, "reply": "", "error": ""}
        self.module._run_one(self._job())
        self.assertIn("no output", self.sent[0][1])

    def test_tasks_run_one_at_a_time(self):
        # Each is a full agent loop that may hold a PTY and run commands.
        source = (EXTENSION / "main.py").read_text()
        self.assertIn("_task_queue", source)
        self.assertIn("def _task_worker", source)
        self.assertIn("_task_queue.get()", source)


class SummaryAndChunkingTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_main()
        self.module._log = lambda *_: None

    def test_a_short_answer_is_sent_verbatim(self):
        self.assertEqual(self.module._summarise("q", "42"), "42")

    def test_a_long_answer_is_summarised_for_a_phone(self):
        class Backend:
            def chat(_s, message, system_prompt="", **kw):
                return {"reply": "short version"}

        self.module._backend = Backend()
        self.assertEqual(self.module._summarise("q", "x" * 2000), "short version")

    def test_summarising_is_a_nicety_not_a_gate(self):
        class Backend:
            def chat(_s, *a, **k):
                raise RuntimeError("model down")

        self.module._backend = Backend()
        out = self.module._summarise("q", "y" * 2000)
        self.assertTrue(out.startswith("y"))

    def test_long_text_is_chunked_under_the_limit(self):
        parts = self.module._chunks("para\n\n" * 3000)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), self.module.CHUNK_LIMIT)

    def test_short_text_is_one_chunk(self):
        self.assertEqual(self.module._chunks("hello"), ["hello"])


class CredentialLocationTests(unittest.TestCase):
    """Credentials outlive any one version of the code that uses them."""

    def test_credentials_live_outside_the_extension(self):
        module = _load_main()
        self.assertNotIn(str(EXTENSION), str(module.AUTH_DIR))
        self.assertIn("credentials", str(module.AUTH_DIR))

    def test_the_sidecar_clamps_the_umask_before_writing(self):
        source = BRIDGE.read_text()
        self.assertIn("process.umask(0o077)", source)
        self.assertLess(source.index("process.umask"), source.index("const AUTH_DIR"))

    def test_nothing_secret_is_packaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "whatsapp.lext"
            extension_manager.create_publication_archive(EXTENSION, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                unpacked = sum(i.file_size for i in archive.infolist())
        for required in ("main.py", "extension.json", "README.md",
                         "package.json", "bridge/bridge.mjs"):
            self.assertIn(required, names)
        for name in names:
            self.assertNotIn("node_modules", name)
            self.assertNotIn(".auth", name)
        self.assertLessEqual(len(names), extension_manager.MAX_ARCHIVE_FILES)
        self.assertLessEqual(unpacked, extension_manager.MAX_UNPACKED_BYTES)


class ConsoleCaptureTests(unittest.TestCase):
    """A dependency writing to stdout is both corruption and a key leak.

    libsignal calls `console.info("Closing session:", session)` on every
    session close. `console.info` goes to stdout -- the IPC channel -- and the
    object it passes is a live Signal session including its private key."""

    def test_console_is_rebound_away_from_stdout(self):
        source = BRIDGE.read_text()
        self.assertIn("for (const level of ['log', 'info', 'debug', 'warn', "
                      "'error', 'trace', 'dir'])", source)
        block = source.split("console capture", 1)[1][:1400]
        self.assertIn("note(", block)          # stderr, never stdout

    def test_object_arguments_are_dropped_not_truncated(self):
        # Truncating would still leak whatever sorted first.
        block = BRIDGE.read_text().split("console capture", 1)[1][:1400]
        self.assertIn("typeof args[0] === 'string'", block)
        self.assertNotIn("JSON.stringify(args", block)

    def test_a_harmless_startup_timeout_does_not_shout(self):
        module = _load_main()
        source = (EXTENSION / "main.py").read_text()
        self.assertIn("benign", source)
        self.assertIn("init queries", source)


class HardWonBridgeBehaviourTests(unittest.TestCase):
    """Regressions that each cost a real debugging session. Keep them pinned."""

    def test_an_unfinished_pairing_is_only_reset_when_starting_one(self):
        # Resetting on every connect deleted the pairing the user was in the
        # middle of: `requestPairingCode` writes `creds.me` when the code is
        # issued, so a pairing in progress legitimately looks unfinished.
        source = BRIDGE.read_text()
        self.assertIn("async function connect({ allowReset = false } = {})", source)
        self.assertIn("if (allowReset && isAbandonedPairing(state.creds))", source)
        sched = source.split("function scheduleReconnect()", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("allowReset", sched)

    def test_a_completed_pairing_is_never_treated_as_abandoned(self):
        # `registered` is set only on the link-code path; testing it would
        # delete a working QR pairing.
        source = BRIDGE.read_text()
        condition = source.split("function isAbandonedPairing", 1)[1].split("}", 1)[0]
        self.assertIn("!creds.account", condition)
        self.assertNotIn("registered", condition)

    def test_reconnects_are_bounded(self):
        source = BRIDGE.read_text()
        self.assertIn("failedConnects > MAX_FAILED_CONNECTS", source)

    def test_the_device_identity_is_the_one_that_pairs(self):
        source = BRIDGE.read_text()
        self.assertIn("WA_DEVICE_NAME || 'Ubuntu'", source)
        self.assertIn("WA_PLATFORM || 'Chrome'", source)

    def test_the_orphan_guard_only_watches_a_real_parent_pipe(self):
        # Exiting on bare stdin EOF killed the sidecar instantly under
        # /dev/null, racing the connection.
        self.assertIn("isFIFO()", BRIDGE.read_text())

    def test_replayed_history_is_not_run_as_a_backlog_of_tasks(self):
        self.assertIn("ts < startedAt - 60", BRIDGE.read_text())

    def test_our_own_replies_do_not_become_tasks(self):
        source = BRIDGE.read_text()
        self.assertIn("rememberOwnMessage(sent?.key?.id)", source)
        self.assertIn("ownMessageIds.has(id)", source)


@unittest.skipUnless(_have_node(), "node is not installed")
class BridgeProtocolTests(unittest.TestCase):
    def _run_bridge(self, script: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "node_modules" / "@whiskeysockets" / "baileys"
            stub.mkdir(parents=True)
            (stub / "package.json").write_text(json.dumps(
                {"name": "@whiskeysockets/baileys", "version": "0.0.0",
                 "type": "module", "main": "index.mjs"}))
            (stub / "index.mjs").write_text(textwrap.dedent("""
                export const DisconnectReason = { loggedOut: 401 };
                export function useMultiFileAuthState() {
                  return Promise.resolve({ state: {}, saveCreds: () => {} });
                }
                export default function makeWASocket({ logger }) {
                  logger.error('noise that must not reach stdout');
                  const handlers = {};
                  return {
                    ev: { on: (n, f) => { handlers[n] = f; },
                          removeAllListeners: () => {} },
                    end: () => {},
                    sendMessage: () => Promise.reject(new Error('offline')),
                  };
                }
            """))
            qrcode = root / "node_modules" / "qrcode"
            qrcode.mkdir(parents=True)
            (qrcode / "package.json").write_text(json.dumps(
                {"name": "qrcode", "version": "0.0.0", "type": "module",
                 "main": "index.mjs"}))
            (qrcode / "index.mjs").write_text(
                "export default { toDataURL: () => Promise.resolve('data:,x') };\n")

            copy = root / "bridge.mjs"
            copy.write_text(BRIDGE.read_text())
            proc = subprocess.Popen(
                ["node", str(copy)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(root),
                env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                     "WA_AUTH_DIR": str(root / ".auth"), "WA_HTTP_PORT": "0"})
            out: list[str] = []
            reader = threading.Thread(target=lambda: out.extend(proc.stdout),
                                      daemon=True)
            reader.start()
            time.sleep(1.0)
            proc.stdin.write(script)
            proc.stdin.flush()
            time.sleep(1.0)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            reader.join(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            return "".join(out)

    def test_stdout_carries_only_ipc_frames(self):
        # Baileys logs through pino, which writes to stdout by default.
        output = self._run_bridge(json.dumps({"type": "ping"}) + "\n")
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            json.loads(line)
        self.assertNotIn("noise that must not reach stdout", output)

    def test_a_frame_split_across_writes_is_reassembled(self):
        frame = json.dumps({"type": "send", "reqId": "big",
                            "to": "1@s.whatsapp.net", "text": "x" * 200000}) + "\n"
        acks = [json.loads(l) for l in self._run_bridge(frame).splitlines()
                if l.strip() and json.loads(l).get("type") == "send_result"]
        self.assertEqual([a["reqId"] for a in acks], ["big"])

    def test_every_send_is_acknowledged(self):
        output = self._run_bridge(json.dumps(
            {"type": "send", "reqId": "r1", "to": "1@s.whatsapp.net",
             "text": "hi"}) + "\n")
        acks = [json.loads(l) for l in output.splitlines()
                if l.strip() and json.loads(l).get("type") == "send_result"]
        self.assertEqual(len(acks), 1)
        self.assertFalse(acks[0]["ok"])


if __name__ == "__main__":
    unittest.main()
