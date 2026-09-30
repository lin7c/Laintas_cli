import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import terminal_link as link


class FakeAdapter:
    def __init__(self):
        self.terminals = set()
        self.starts = 0
        self.cancelled = []
        self.messages = []
        self.busy = False
    def manager_exists(self, _aid): return True
    def has_terminal(self, name): return name in self.terminals
    def has_child_terminals(self): return bool(self.terminals)
    def idle(self): return not self.busy
    def inventory(self): return [{"id": "primary", "primary": True, "tools": ["fs.read"]}]
    def import_child(self, invite, _rows): self.terminals.add(invite["alias"])
    def remove_child(self, child): self.terminals.discard(child["alias"])
    def enter_child(self, _invite): pass
    def leave_child(self): pass
    def notice(self, value): self.messages.append(value)
    def start_job(self, _relation, job_id, _payload):
        if self.busy: raise link.LinkError("Busy")
        self.starts += 1
        return {"ok": True, "job_id": job_id}
    def jobs(self, _relation): return []
    def cancel_job(self, relation, job): self.cancelled.append((relation, job))
    def cancel_owned(self, relation): self.cancelled.append(relation)
    def sync(self, _child, _result): pass
    def offline(self, _child): pass
    def admitted(self, *_args): pass
    def deploy(self, _aid): return {}
    def close(self): pass


class DirectTransport:
    endpoints = {}
    def __init__(self, ref, uid="u"):
        self.ref, self.uid = ref, uid
    def source(self, _target): return self.ref
    def call(self, target, op, payload):
        result = self.endpoints[target].dispatch(self.ref, self.uid, op, payload)
        if not result["ok"]: raise link.LinkError(result["error"])
        return result["result"]
    def close(self): pass


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        DirectTransport.endpoints = {}
        self.services = []
        self.a = self.make("local:A")
        self.b = self.make("local:B")
        self.c = self.make("local:C")
    def tearDown(self):
        for service in self.services:
            service.close()
    def make(self, ref, uid="u"):
        service = link.Service(FakeAdapter(), DirectTransport(ref, uid), uid)
        DirectTransport.endpoints[ref] = service
        self.services.append(service)
        return service
    def attach(self):
        invitation = self.b.adopt("local:A", "A", "primary")
        self.a.accept(invitation)
        return invitation, self.b.children["A"]
    def test_acceptance_and_two_levels(self):
        invite = self.b.adopt("local:A", "A", "primary")
        self.assertFalse(self.b.children)
        self.assertIsNone(self.a.parent)
        self.a.accept(invite)
        self.a.accept(invite)
        self.assertEqual(list(self.b.children), ["A"])
        with self.assertRaises(link.LinkError): self.a.adopt("local:C", "C", "primary")
        with self.assertRaises(link.LinkError): self.c.adopt("local:B", "B", "primary")
    def test_inverse_offers_cannot_form_cycle(self):
        first = self.b.adopt("local:A", "A", "primary")
        with self.assertRaises(link.LinkError): self.a.adopt("local:B", "B", "primary")
        with self.assertRaises(link.LinkError): self.a.accept(first)
        self.a.release("B")
        self.a.accept(first)
    def test_authentication_deduplication_release(self):
        invite, child = self.attach()
        request = {"id": invite, "token": child["token"], "job_id": "a" * 32,
                   "agent": "primary", "task": "read", "tools": ["fs.read"]}
        for _ in range(2):
            self.assertTrue(self.a.dispatch("local:B", "u", "assign", request)["ok"])
        self.assertEqual(self.a.adapter.starts, 1)
        self.assertFalse(self.a.dispatch("local:C", "u", "assign", request)["ok"])
        self.assertFalse(self.a.dispatch("local:B", "wrong", "assign", request)["ok"])
        self.assertFalse(self.a.dispatch("local:B", "u", "assign", dict(request, task="different"))["ok"])
        self.b.release("A")
        self.assertIsNone(self.a.parent)
        self.assertFalse(self.a.dispatch("local:B", "u", "assign", request)["ok"])
    def test_busy_and_lease_expiration(self):
        invite = self.b.adopt("local:A", "A", "primary")
        self.a.adapter.busy = True
        with self.assertRaises(link.LinkError): self.a.accept(invite)
        self.a.adapter.busy = False
        self.a.accept(invite)
        self.a.parent["contact"] = time.time() - link.LEASE_SECONDS - 1
        self.a.tick()
        self.assertIn(invite, self.a.adapter.cancelled)
        self.assertTrue(self.a.parent["offline"])
    def test_remote_account_boundary(self):
        remote = self.make("remote:other", "other")
        self.assertFalse(remote.dispatch("remote:B", "u", "inspect", {})["ok"])
        self.assertTrue(remote.dispatch("remote:B", "other", "inspect", {})["ok"])
    def test_status_with_many_large_results_keeps_all_job_identities(self):
        invite, child = self.attach()
        jobs = [{"job_id": f"{n:032x}", "agent": "primary", "task": "task" * 10000,
                 "status": "completed", "result": "\x00中文" * 50000, "error": ""}
                for n in range(128)]
        with mock.patch.object(self.a.adapter, "jobs", return_value=jobs):
            response = self.a.dispatch("local:B", "u", "status", {"id": invite, "token": child["token"]})
        self.assertTrue(response["ok"], response)
        self.assertLessEqual(len(link._json(response)), link.MAX_BYTES)
        self.assertEqual([j["job_id"] for j in response["result"]["jobs"]], [j["job_id"] for j in jobs])
        self.assertTrue(all(j["result_truncated"] for j in response["result"]["jobs"]))
    def test_local_accounts_require_explicit_acceptance(self):
        self.a.user_id = "different"
        invitation = self.b.adopt("local:A", "A", "primary")
        self.assertIsNone(self.a.parent)
        self.a.accept(invitation)
        self.assertEqual(self.a.parent["user_id"], "u")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        import agent_loop as r
        from terminal_link_runtime import Adapter
        self.r = r
        r.close_all_agents()
        r.close_all_terminals()
        shell = mock.Mock(command="bash", full_output="")
        shell.is_alive.return_value = True
        r.register_terminal(shell, "bash", 0, name="term0")
        self.primary = r.register_agent("primary", role="primary")
        self.scout = r.register_agent("scout", parent_id="primary")
        self.adapter = Adapter(r, {"userId": "source-account"}, lambda: None, lambda _v: None)
    def tearDown(self):
        self.adapter.leave_child()
        self.r.close_all_agents()
        self.r.close_all_terminals()
    def test_flat_proxy_registration_and_no_local_execution(self):
        import agent_persistence
        invite = {"id": "i", "alias": "A", "owner": "primary"}
        self.adapter.import_child(invite, [{"id": "primary", "primary": True, "tools": ["fs.read"]},
                                         {"id": "scout", "tools": ["fs.read"]}])
        for aid in ["A/primary", "A/scout"]:
            a = self.r.get_agent(aid)
            self.assertEqual(a.parent_id, "primary")
            self.assertEqual(a.remote_terminal, "A")
            self.assertFalse(self.r.start_agent_assignment(aid, "test", None)[0])
            with mock.patch.object(agent_persistence, "_ensure_dir", side_effect=AssertionError("proxy persisted")):
                self.assertTrue(agent_persistence.save_agent_state(a))
        self.assertFalse(self.r.can_agents_communicate("A/primary", "A/scout"))
        self.adapter.remove_child(invite)
        self.assertIsNone(self.r.get_agent("A/primary"))
        self.assertIsNotNone(self.r.get_agent("primary"))
    def test_source_flattening_restores_original_parent(self):
        self.adapter.enter_child({"parent": "local:B", "owner": "primary"})
        self.assertEqual(self.scout.parent_id, "local:B/primary")
        self.assertNotIn("scout", self.primary.child_ids)
        self.adapter.leave_child()
        self.assertEqual(self.scout.parent_id, "primary")
        self.assertIn("scout", self.primary.child_ids)
    def test_proxy_is_not_visible_until_remote_identity_is_initialized(self):
        registered, release, reading, observed = (threading.Event() for _ in range(4))
        original = self.r.register_agent
        seen, errors = [], []
        def register(*args, **kwargs):
            result = original(*args, **kwargs)
            registered.set()
            if not release.wait(2): raise AssertionError("registration was not released")
            return result
        def import_child():
            try:
                self.adapter.import_child({"id": "i", "alias": "A", "owner": "primary"},
                    [{"id": "primary", "primary": True, "tools": ["fs.read"]}])
            except Exception as exc: errors.append(exc)
        def read():
            reading.set()
            seen.append(self.r.get_agent("A/primary").remote_terminal)
            observed.set()
        writer, reader = threading.Thread(target=import_child), threading.Thread(target=read)
        with mock.patch.object(self.r, "register_agent", side_effect=register):
            try:
                writer.start()
                self.assertTrue(registered.wait(2))
                reader.start()
                self.assertTrue(reading.wait(2))
                self.assertFalse(observed.wait(.05))
            finally:
                release.set()
                for worker in (writer, reader):
                    if worker.ident is not None:
                        worker.join(3)
                        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(seen, ["A"])
    def test_linked_terminal_commands_preserve_offline_relation_and_reject_raw_input(self):
        import laintas_cli as cli
        invite = {"id": "i", "alias": "A", "owner": "primary"}
        self.adapter.import_child(invite, [{"id": "primary", "primary": True, "tools": ["fs.read"]}])
        terminal = self.r.get_terminal("A")
        with mock.patch.object(cli, "console"), mock.patch.object(cli, "authorize_direct_command") as approve:
            cli._cmd_send("A echo hi")
            approve.assert_not_called()
            terminal.session.online = False
            cli._cmd_term(["/term", "A"], None, None)
        self.assertIs(self.r.get_terminal("A"), terminal)
        self.assertIsNotNone(self.r.get_agent("A/primary"))
    def test_employee_scope_restored_without_controller_polling(self):
        self.adapter.enter_child({"parent": "local:B", "owner": "primary"})
        old_scope = ["fs.read", "fs.list"]
        self.scout.state["_tool_allowlist"] = old_scope
        with mock.patch.object(self.r, "schedule_agent", side_effect=lambda _aid, runner: runner(True)), \
             mock.patch.object(self.r, "run_agent_loop", return_value={"success": True, "state": {"lastReply": "done"}}), \
             mock.patch("agent_persistence.save_agent_state"):
            self.adapter.start_job("i", "a" * 32, {"agent": "scout", "task": "read", "tools": ["fs.read"]})
            self.scout.thread.join(3)
            self.assertFalse(self.scout.thread.is_alive())
            self.assertEqual(self.scout.state["_tool_allowlist"], old_scope)
            self.assertIsNone(self.scout.active_assignment)
            # A new controller must not inherit a stale "running" cache when
            # the old controller disappeared without polling the final result.
            self.adapter.start_job("next", "b" * 32, {"agent": "scout", "task": "again", "tools": ["fs.read"]})
            self.scout.thread.join(3)
            self.assertFalse(self.scout.thread.is_alive())
            self.assertEqual(self.scout.state["_tool_allowlist"], old_scope)
    def test_late_completion_and_old_running_job_stay_visible(self):
        self.adapter.work = {str(n): {"job_id": str(n), "agent": "primary", "relation": "i",
                                     "status": "completed", "completed_at": n, "native": None}
                             for n in range(140)}
        self.adapter.work["0"].update(status="running", completed_at=None)
        self.assertIn("0", [j["job_id"] for j in self.adapter.jobs("i")])
        self.adapter.work["0"].update(status="completed", completed_at=1000)
        self.assertIn("0", [j["job_id"] for j in self.adapter.jobs("i")])
    def test_execution_scope_busy_and_stale_cancel(self):
        started, finish = threading.Event(), threading.Event()
        self.adapter.enter_child({"parent": "local:B", "owner": "primary"})
        captured = {}
        def execute(_deps, task, session, state, _history, **_kwargs):
            captured.update(account=session["userId"], task=task, tools=state["_tool_allowlist"])
            started.set()
            finish.wait(3)
            return {"success": True, "state": {"lastReply": "done"}}
        try:
            with mock.patch.object(self.r, "run_agent_loop", side_effect=execute), mock.patch("agent_persistence.save_agent_state"):
                self.adapter.start_job("i", "a" * 32, {"agent": "primary", "task": "read", "tools": ["fs.read", "FAKE"]})
                self.assertTrue(started.wait(2))
                with self.assertRaises(link.LinkError):
                    self.adapter.start_job("i", "b" * 32, {"agent": "primary", "task": "read", "tools": []})
                self.adapter.cancel_job("other", "a" * 32)
                self.assertFalse(self.primary.abort_event.is_set())
                finish.set()
                self.primary.thread.join(3)
                self.assertFalse(self.primary.thread.is_alive())
                self.adapter.cancel_job("i", "a" * 32)
                self.assertFalse(self.primary.abort_event.is_set())
                self.assertEqual(captured["account"], "source-account")
                self.assertNotIn("FAKE", captured["tools"])
                self.assertNotIn("_tool_allowlist", self.primary.state)
        finally:
            finish.set()
            if self.primary.thread: self.primary.thread.join(3)


class ProcessTests(unittest.TestCase):
    def test_real_process_execution_and_cleanup(self):
        # Worker is a different Python runtime, with its own registry, auth and cwd.
        with tempfile.TemporaryDirectory(prefix="term-link-process-") as temporary:
            root = Path(temporary)
            workspace = root / "worker-workspace"
            workspace.mkdir()
            env = dict(os.environ, LAINTAS_HOME=str(root), PYTHONDONTWRITEBYTECODE="1")
            script = Path(__file__).resolve()
            worker = subprocess.Popen([sys.executable, "-B", str(script), "--worker", str(root)],
                cwd=workspace, env=dict(env, PYTHONPATH=str(script.parents[1])),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            service = None
            try:
                ref = worker.stdout.readline().strip()
                self.assertEqual(ref, "local:worker", worker.stderr.read() if not ref else "")
                transport = link.LocalTransport(root, "controller", "controller-account", lambda *args: service.dispatch(*args))
                service = link.Service(FakeAdapter(), transport, "controller-account")
                # Do not start the watcher; this test controls status polling.
                transport.start()
                invitation = service.adopt(ref, "A", "primary")
                worker.stdin.write(json.dumps({"accept": invitation}) + "\n")
                worker.stdin.flush()
                self.assertEqual(json.loads(worker.stdout.readline()), {"accepted": True})
                child = service.children["A"]
                payload = {"id": child["id"], "token": child["token"], "job_id": "a" * 32,
                           "agent": "primary", "task": "inspect workspace", "tools": ["fs.read"]}
                first = transport.call(ref, "assign", payload)
                self.assertEqual(transport.call(ref, "assign", payload), first)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    jobs = transport.call(ref, "status", {"id": child["id"], "token": child["token"]})["jobs"]
                    if jobs and jobs[0]["status"] == "completed": break
                    time.sleep(.02)
                result = json.loads(jobs[0]["result"])
                self.assertEqual(result["cwd"], str(workspace))
                self.assertEqual(result["account"], "worker-account")
                self.assertEqual(result["pid"], worker.pid)
                self.assertEqual(result["calls"], 1)
                service.release("A")
                with self.assertRaises(link.LinkError): transport.call(ref, "assign", payload)
                self.assertIsNone(worker.poll())
            finally:
                if service: service.close()
                if worker.poll() is None:
                    try:
                        worker.stdin.write('{"stop": true}\n')
                        worker.stdin.flush()
                    except BrokenPipeError:
                        pass
                try: worker.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.communicate(timeout=3)
                self.assertIsNotNone(worker.returncode)
                self.assertFalse(list((root / "terminal-endpoints").glob("*.json")))


def _worker(root):
    import agent_loop as r
    from terminal_link_runtime import Adapter
    from contextlib import redirect_stdout
    shell = mock.Mock(command="bash", full_output="")
    shell.is_alive.return_value = True
    r.register_terminal(shell, "bash", 0, name="term0")
    r.register_agent("primary", role="primary")
    calls = 0
    def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"success": True, "state": {"lastReply": json.dumps({"cwd": os.getcwd(), "pid": os.getpid(),
            "account": args[2]["userId"], "calls": calls})}}
    adapter = Adapter(r, {"userId": "worker-account"}, lambda: None, lambda _v: None)
    transport = link.LocalTransport(root, "worker", "worker-account", lambda *args: service.dispatch(*args))
    service = link.Service(adapter, transport, "worker-account")
    link._service = service
    with mock.patch.object(r, "run_agent_loop", side_effect=execute), mock.patch("agent_persistence.save_agent_state"):
        service.start()
        print(transport.ref, flush=True)
        try:
            for line in sys.stdin:
                data = json.loads(line)
                if data.get("stop"): break
                with redirect_stdout(sys.stderr):
                    service.accept(data["accept"])
                print(json.dumps({"accepted": True}), flush=True)
        finally:
            link.stop_service()
            r.close_all_agents()
            r.close_all_terminals()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _worker(sys.argv[-1])
    else:
        unittest.main()
