"""Regression coverage for the November 2026 consistency audit."""
import concurrent.futures
import gzip
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import auto_pilot
import contract_store as contracts
import helpwo_server
import laintas_cli
import local_runtime
import paths
import peer_coordination as peers
import task_manager
import tools
import workgraph


def _edit_in_child(filename, lock_root, ready, done, results):
    paths.INSTANCES_DIR = Path(lock_root)
    coord = peers._PeerCoordinator()
    coord._enabled = True
    peers._coord = coord
    coord.note_read(filename)
    ready.set()
    with mock.patch.object(tools, "_check_file_write_policy", return_value=None), \
            mock.patch.object(tools, "_attach_diagnostics", side_effect=lambda value, *_: value):
        result = tools._bi_fs_edit(
            {"path": filename, "old_string": "beta=0", "new_string": "beta=1"},
            tools.ToolCtx(cwd=str(Path(filename).parent)))
    results.put(result)
    done.set()


def _propose_in_child(cwd, actor, start):
    start.wait(10)
    for index in range(8):
        contracts.propose(f"GET /{actor}/{index}",
                          {"responses": {"200": {"description": "ok"}}}, actor, cwd=cwd)


class _TemporaryWorkspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(paths, "INSTANCES_DIR", self.root / "instances")
        patch.start()
        self.addCleanup(patch.stop)

    def stop_process(self, process):
        if process.pid is None:
            return
        if process.is_alive():
            process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)


class FileMutationTests(_TemporaryWorkspace):
    def setUp(self):
        super().setUp()
        self.target = self.root / "example.txt"
        self.target.write_text("alpha=0\nbeta=0\n")
        for patch in [
                mock.patch.object(tools, "_check_file_write_policy", return_value=None),
                mock.patch.object(tools, "_attach_diagnostics", side_effect=lambda value, *_: value),
                mock.patch.object(tools, "_run_formatter", return_value=False)]:
            patch.start()
            self.addCleanup(patch.stop)

    def edit(self, field, actor=""):
        return tools._bi_fs_edit(
            {"path": str(self.target), "old_string": field + "=0", "new_string": field + "=1"},
            tools.ToolCtx(cwd=str(self.root), agent_id=actor))

    def test_concurrent_edit_reads_the_latest_committed_bytes(self):
        first_inside = threading.Event()
        second_inside = threading.Event()
        release = threading.Event()

        def approval(_path, ctx, _diff):
            if ctx.agent_id == "first":
                first_inside.set()
                self.assertTrue(release.wait(5))
            else:
                second_inside.set()

        with mock.patch.object(peers, "get_coord", return_value=peers._PeerCoordinator()), \
                mock.patch.object(tools, "_check_file_write_policy", side_effect=approval), \
                concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.edit, "alpha", "first")
            try:
                self.assertTrue(first_inside.wait(5))
                second = pool.submit(self.edit, "beta", "second")
                self.assertFalse(second_inside.wait(0.1))
            finally:
                release.set()
            self.assertTrue(first.result(5)["ok"])
            self.assertTrue(second.result(5)["ok"])
        self.assertEqual(self.target.read_text(), "alpha=1\nbeta=1\n")

    def test_other_process_cannot_commit_a_stale_edit(self):
        ctx = multiprocessing.get_context("spawn")
        ready, done, results = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_edit_in_child, args=(
            str(self.target), str(paths.INSTANCES_DIR), ready, done, results))
        self.addCleanup(results.close)
        coord = peers._PeerCoordinator()
        with mock.patch.object(peers, "get_coord", return_value=coord):
            try:
                with peers.write_guard(self.target):
                    process.start()
                    self.assertTrue(ready.wait(10))
                    self.assertFalse(done.wait(0.1))
                    self.assertTrue(self.edit("alpha")["ok"])
                result = results.get(timeout=10)
                process.join(10)
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(result["ok"])
                self.assertIn("changed since", result["error"])
            finally:
                self.stop_process(process)
        self.assertEqual(self.target.read_text(), "alpha=1\nbeta=0\n")

    def test_read_and_recorded_fingerprint_cannot_straddle_a_write(self):
        entered, release, writer_inside = threading.Event(), threading.Event(), threading.Event()
        coord = peers._PeerCoordinator()
        coord._enabled = True
        note_read = coord.note_read

        def record(path):
            entered.set()
            self.assertTrue(release.wait(5))
            return note_read(path)

        with mock.patch.object(peers, "get_coord", return_value=coord), \
                mock.patch.object(coord, "note_read", side_effect=record), \
                mock.patch.object(tools, "_check_file_write_policy", side_effect=lambda *_: writer_inside.set()), \
                concurrent.futures.ThreadPoolExecutor(2) as pool:
            read = pool.submit(tools._bi_fs_read, {"path": str(self.target), "offset": 1, "limit": 2},
                               tools.ToolCtx(cwd=str(self.root)))
            try:
                self.assertTrue(entered.wait(5))
                write = pool.submit(self.edit, "alpha")
                self.assertFalse(writer_inside.wait(0.1))
            finally:
                release.set()
            result = read.result(5)
            self.assertTrue(result["ok"])
            self.assertIn("alpha=0", result["result"])
            self.assertTrue(write.result(5)["ok"])

    def test_formatter_result_is_the_version_recorded_for_the_writer(self):
        coord = peers._PeerCoordinator()
        coord._enabled = True
        coord.note_read(str(self.target))

        def formatter(path):
            Path(path).write_text("formatted\n")
            return True

        with mock.patch.object(peers, "get_coord", return_value=coord), \
                mock.patch.object(tools, "_run_formatter", side_effect=formatter):
            result = tools._bi_fs_write({"path": str(self.target), "content": "before format"},
                                        tools.ToolCtx(cwd=str(self.root)))
        self.assertTrue(result["ok"])
        self.assertIsNone(coord.assert_unchanged(str(self.target)))
        self.assertEqual(self.target.read_text(), "formatted\n")

    def test_bridge_and_cli_use_the_same_write_lock(self):
        handler = object.__new__(helpwo_server._HelpwoHandler)
        handler._json = mock.Mock()
        handler._private_access = mock.Mock(return_value=True)
        coord = peers._PeerCoordinator()
        coord.attach_actor(peers.HELPWO_BRIDGE_ACTOR)
        coord.note_read(str(self.target), actor=peers.HELPWO_BRIDGE_ACTOR)
        entered = threading.Event()
        original = helpwo_server._peer_guard_write

        def checked(path):
            entered.set()
            return original(path)

        with mock.patch.object(peers, "get_coord", return_value=coord), \
                mock.patch.object(helpwo_server, "_local_root_ref", self.root), \
                mock.patch.object(helpwo_server, "_peer_guard_write", side_effect=checked), \
                concurrent.futures.ThreadPoolExecutor(1) as pool:
            with peers.write_guard(self.target):
                future = pool.submit(handler._handle_local_fs_write,
                                     {"path": str(self.target), "content": "stale bridge write"})
                self.assertFalse(entered.wait(0.1))
                self.assertTrue(self.edit("alpha")["ok"])
            future.result(5)
        self.assertEqual(handler._json.call_args.args[0], 409)
        self.assertEqual(self.target.read_text(), "alpha=1\nbeta=0\n")

    def test_failed_replace_preserves_contents_and_removes_temporary_file(self):
        with mock.patch.object(peers.os, "replace", side_effect=OSError("disk failure")):
            result = self.edit("alpha")
        self.assertFalse(result["ok"])
        self.assertEqual(self.target.read_text(), "alpha=0\nbeta=0\n")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_atomic_write_preserves_permissions_and_symlink(self):
        self.target.chmod(0o751)
        alias = self.root / "alias.txt"
        alias.symlink_to(self.target.name)
        peers.write_bytes_atomically(alias, b"replacement")
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.target.read_bytes(), b"replacement")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o751)

    def test_atomic_write_accepts_a_long_valid_basename(self):
        target = self.root / ("x" * 240 + ".txt")
        target.write_bytes(b"original")
        peers.write_bytes_atomically(target, b"updated")
        self.assertEqual(target.read_bytes(), b"updated")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_lock_metadata_stays_bounded_when_many_files_are_accessed(self):
        for index in range(1500):
            with peers.write_guard(self.root / f"file-{index}.txt"):
                pass
        sidecars = list((paths.INSTANCES_DIR / "file-locks").glob("*.lock"))
        self.assertGreater(len(sidecars), 0)
        self.assertLessEqual(len(sidecars), 1024)

    def test_coordination_failure_does_not_allow_a_write(self):
        coord = mock.Mock(spec=peers._PeerCoordinator)
        coord.assert_unchanged.side_effect = RuntimeError("broken coordinator")
        with mock.patch.object(peers, "get_coord", return_value=coord):
            result = self.edit("alpha")
        self.assertFalse(result["ok"])
        self.assertIn("coordination unavailable", result["error"])
        self.assertEqual(self.target.read_text(), "alpha=0\nbeta=0\n")


class ContractTransactionTests(_TemporaryWorkspace):
    DEFINITION = {"responses": {"200": {"description": "ok"}}}

    def propose(self, name):
        return contracts.propose("GET /" + name, self.DEFINITION, name, cwd=str(self.root))

    def test_threads_preserve_both_proposals(self):
        entered, release, second_inside = threading.Event(), threading.Event(), threading.Event()
        original = contracts._record

        def record(entry, actor, *args, **kwargs):
            if actor == "first":
                entered.set()
                self.assertTrue(release.wait(5))
            else:
                second_inside.set()
            return original(entry, actor, *args, **kwargs)

        with mock.patch.object(contracts, "_record", side_effect=record), \
                concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.propose, "first")
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.propose, "second")
                self.assertFalse(second_inside.wait(0.1))
            finally:
                release.set()
            self.assertTrue(first.result(5)["ok"])
            self.assertTrue(second.result(5)["ok"])
        self.assertEqual(set(contracts.load(str(self.root))[0]["paths"]), {"/first", "/second"})

    def test_processes_preserve_all_proposals(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        processes = [ctx.Process(target=_propose_in_child, args=(str(self.root), actor, start))
                     for actor in ("first", "second")]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                self.stop_process(process)
        self.assertEqual(len(contracts.load(str(self.root))[0]["paths"]), 16)

    def test_interrupted_pair_commit_is_recovered_before_reading(self):
        self.propose("first")
        original = contracts._write_json

        def fail_lock_file(path, value):
            if path == contracts.lock_path(str(self.root)):
                raise OSError("interrupted commit")
            original(path, value)

        with mock.patch.object(contracts, "_write_json", side_effect=fail_lock_file):
            with self.assertRaises(OSError):
                self.propose("second")
        spec, state = contracts.load(str(self.root))
        self.assertEqual(set(spec["paths"]), {"/first", "/second"})
        self.assertEqual(set(state["operations"]), {"GET /first", "GET /second"})
        self.assertFalse(contracts._journal_path(str(self.root)).exists())

    def test_verification_merges_unrelated_changes_and_rejects_a_changed_operation(self):
        for change_same_operation in (False, True):
            with self.subTest(change_same_operation=change_same_operation):
                self.propose("checked")
                contracts.agree("GET /checked", "provider", cwd=str(self.root))
                implementation = self.root / "server.py"
                implementation.write_text("# implementation")
                contracts.implement("GET /checked", "provider", [implementation.name], cwd=str(self.root))
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.status = 200
                response.read.return_value = b"{}"

                def probe(*args, **kwargs):
                    self.propose("checked" if change_same_operation else "unrelated")
                    return response

                with mock.patch("urllib.request.urlopen", side_effect=probe):
                    result = contracts.verify("GET /checked", base_url="http://localhost", cwd=str(self.root))
                self.assertEqual(result["ok"], not change_same_operation)
                state = contracts.load(str(self.root))[1]["operations"]
                self.assertIn("GET /unrelated", state)
                self.assertEqual(state["GET /checked"]["state"],
                                 "proposed" if change_same_operation else "verified")


class WorkGraphConsistencyTests(_TemporaryWorkspace):
    def test_failed_task_update_rolls_back_children_and_dependencies(self):
        cwd = str(self.root)
        task = task_manager.create_task("parent", cwd=cwd, session_id="session")
        blocker = task_manager.create_task("blocker", cwd=cwd, session_id="session")
        result = task_manager.update_task(task["id"], cwd=cwd, session_id="session",
            addSubtask={"subject": "must roll back"}, addBlockedBy=[blocker["id"]], status="invalid")
        self.assertFalse(result[0])
        self.assertEqual(len(task_manager.list_tasks(cwd=cwd, session_id="session")), 2)
        self.assertEqual(task_manager.get_task(task["id"], cwd=cwd, session_id="session")["blockedBy"], [])

    def test_implicit_completion_obeys_the_same_transition_rules(self):
        cwd = str(self.root)
        work = workgraph.create_work("state test", cwd=cwd)
        for status in ("deleted", "blocked", "skipped"):
            with self.subTest(status=status):
                step = workgraph.create_step(work["id"], status, cwd=cwd)
                workgraph.update_step(work["id"], step["id"], cwd=cwd, status=status)
                with self.assertRaises(workgraph.WorkGraphConflict):
                    workgraph.update_step(work["id"], step["id"], cwd=cwd, progress=100)
                self.assertEqual(workgraph.get_step(work["id"], step["id"], cwd=cwd)["status"], status)

    def test_concurrent_initialization_returns_one_active_work(self):
        barrier = threading.Barrier(4)

        def ensure(_):
            barrier.wait(5)
            return workgraph.ensure_active_work(cwd=str(self.root), session_id="shared")["id"]

        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            identities = list(pool.map(ensure, range(4)))
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual(len(workgraph.list_work(cwd=str(self.root))), 1)

    def test_database_recreated_at_the_same_path_is_initialized(self):
        cwd = str(self.root)
        workgraph.create_work("old", cwd=cwd)
        db = workgraph.db_path(cwd)
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        work = workgraph.create_work("new", cwd=cwd)
        self.assertEqual(workgraph.get_work(work["id"], cwd=cwd)["objective"], "new")

    def test_outer_rollback_includes_nested_transactions_and_reads(self):
        cwd = str(self.root)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with workgraph.transaction(cwd):
                work = workgraph.create_work("uncommitted", cwd=cwd)
                self.assertIsNotNone(workgraph.get_work(work["id"], cwd=cwd))
                raise RuntimeError("abort")
        self.assertEqual(workgraph.list_work(cwd=cwd), [])


class RuntimeIdentityTests(unittest.TestCase):
    def test_duplicate_exec_never_starts_a_second_process(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(local_runtime, "_exec_requests", {}), \
                mock.patch.object(local_runtime, "_buffered_exec_timeout", side_effect=lambda cmd, timeout: timeout), \
                mock.patch.object(local_runtime.subprocess, "Popen") as popen:
            request = local_runtime._register_exec("same")
            sse = mock.Mock()
            local_runtime.run_exec({"reqId": "same", "cmd": "echo duplicate"}, sse,
                                   lambda _: Path(tmp))
            self.assertIn("already running", sse.event.call_args.args[0]["error"])
            popen.assert_not_called()
            self.assertIs(local_runtime._exec_requests["same"], request)
            local_runtime._release_exec(request)

    def test_duplicate_id_is_rejected_and_stale_release_cannot_remove_a_new_request(self):
        with mock.patch.object(local_runtime, "_exec_requests", {}):
            first = local_runtime._register_exec("same")
            with self.assertRaisesRegex(ValueError, "already running"):
                local_runtime._register_exec("same")
            self.assertTrue(local_runtime.abort_exec("same"))
            self.assertTrue(first.abort.is_set())
            local_runtime._release_exec(first)
            second = local_runtime._register_exec("same")
            local_runtime._release_exec(first)
            self.assertTrue(local_runtime.abort_exec("same"))
            self.assertTrue(second.abort.is_set())
            local_runtime._release_exec(second)

    def test_busy_detached_terminal_still_expires(self):
        term = object.__new__(local_runtime.LocalTerminal)
        term.closed = threading.Event()
        term.lock = threading.RLock()
        term.sock = None
        term.master_fd = 12345
        term._detached_at = time.monotonic() - local_runtime.TERM_GRACE_SECONDS - 1
        with mock.patch.object(term, "terminate", side_effect=term.closed.set) as terminate, \
                mock.patch.object(term, "_finish"), \
                mock.patch.object(local_runtime.select, "select", return_value=([12345], [], [])), \
                mock.patch.object(local_runtime.os, "read", side_effect=[b"busy", b""]) as read:
            term._pump()
        terminate.assert_called_once()
        read.assert_not_called()

    def test_proxy_preserves_compressed_payload_and_encoding(self):
        compressed = gzip.compress(b"<html>hello</html>")
        response = mock.Mock(status=200)
        response.read.return_value = compressed
        response.getheaders.return_value = [("Content-Encoding", "gzip"), ("Content-Type", "text/html")]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("http.client.HTTPConnection", return_value=connection):
            result = local_runtime.proxy_request(12345, "POST", "/", {"Content-Encoding": "gzip"}, compressed)
        self.assertTrue(result["ok"])
        self.assertEqual(result["headers"]["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(result["body"]), b"<html>hello</html>")
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Content-Encoding"], "gzip")


class DecompositionDeadlineTests(unittest.TestCase):
    def test_timeout_returns_before_a_late_callback_and_ignores_its_result(self):
        release, done = threading.Event(), threading.Event()

        def callback(*args):
            try:
                release.wait(5)
                return ["late-a", "late-b"]
            finally:
                done.set()

        with mock.patch.object(auto_pilot, "_decompose_cb", callback):
            started = time.monotonic()
            try:
                result = auto_pilot.decompose_task("update foo.py and also fix bar.py",
                                                 auto_pilot.PARALLEL_HINT, timeout=0.05)
                self.assertLess(time.monotonic() - started, 1)
                self.assertNotEqual(result, ["late-a", "late-b"])
                self.assertNotEqual(auto_pilot.get_last_decompose_source(), "llm")
            finally:
                release.set()
                self.assertTrue(done.wait(5))

    def test_backend_decomposition_cancels_inference_at_the_deadline(self):
        observed = {}

        def backend(*args, **kwargs):
            observed.update(kwargs)
            self.assertTrue(kwargs["interrupt_event"].wait(1))
            return {"reply": '["late-a", "late-b"]'}

        with mock.patch.object(laintas_cli, "aux_calls_enabled", return_value=True), \
                mock.patch.object(laintas_cli, "load_session", return_value={}), \
                mock.patch.object(laintas_cli, "call_backend_stream", side_effect=backend):
            result = laintas_cli._decompose_task_via_backend("task", auto_pilot.PARALLEL_HINT, 0.05)
        self.assertIsNone(result)
        self.assertEqual(observed["request_timeout_override"], 0.05)
        self.assertTrue(observed["interrupt_event"].is_set())
