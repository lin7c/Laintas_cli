import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import account_store
import account_tasks
import agent_loop
import handoff
import laintas_cli as cli
import paths
import peer_coordination
import session_store
import task_handoff


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = str(self.root / "work")
        Path(self.cwd).mkdir()
        for name, value in {"ROOT_HOME": self.root, "LAINTAS_HOME": self.root / "A",
                            "ACCOUNT_USER_ID": "A", "SESSIONS_DIR": self.root / "A" / "sessions",
                            "SESSION_LOCKS_DIR": self.root / "session_locks"}.items():
            patch = mock.patch.object(paths, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(peer_coordination.release_all_leases)
        self.addCleanup(account_store.cancel_transition)
        self.a = {"userId": "A", "userName": "same", "cookies": {"auth": "A-secret"}}
        self.b = {"userId": "B", "userName": "same", "cookies": {"auth": "B-secret"}}
        self.state = {"_session_id": "source-task", "objective": "fix login", "owner_user_id": "A"}
        self.history = [{"role": "user", "content": "fix login", "input_kind": "prompt"}]

    def make_handoff(self):
        checkpoint = task_handoff.capture(self.state, self.history,
            [{"subject": "verify", "status": "pending"}], "A")
        return handoff.create("fix login", "A", checkpoint=checkpoint,
                              target_user_id="B", cwd=self.cwd)

    def test_profiles_are_independent_and_aliases_cannot_collide(self):
        account_store.remember(self.root, self.a, "first")
        account_store.remember(self.root, self.b, "second")
        self.assertEqual(account_store.resolve(self.root, "first")["userId"], "A")
        self.assertNotEqual(account_store.profile_dir(self.root, "A"), account_store.profile_dir(self.root, "B"))
        with self.assertRaises(account_store.AccountError):
            account_store.remember(self.root, self.b, "first")
        saved = json.loads((account_store.profile_dir(self.root, "A") / "session.json").read_text())
        self.assertEqual(saved, self.a)

    def test_terminal_selection_does_not_follow_another_terminal(self):
        for session in (self.a, self.b):
            account_store.remember(self.root, session)
        account_store.select(self.root, "terminal-a", "A")
        account_store.select(self.root, "terminal-b", "B")
        with mock.patch.dict(os.environ, {"LAINTAS_ACCOUNT_ID": ""}):
            self.assertEqual(account_store.launch_account(self.root, "terminal-a", []), "A")
            self.assertEqual(account_store.launch_account(self.root, "terminal-b", []), "B")
            self.assertEqual(account_store.launch_account(self.root, "terminal-a", ["--account", "B"]), "B")

    def test_legacy_credentials_do_not_assign_tasks(self):
        (self.root / "session.json").write_text(json.dumps(self.a))
        old = self.root / "sessions" / "old.json"
        old.parent.mkdir()
        original = json.dumps({"chat_history": self.history})
        old.write_text(original)
        with mock.patch.dict(os.environ, {"LAINTAS_ACCOUNT_ID": ""}):
            self.assertEqual(account_store.launch_account(self.root, "t", []), "A")
        self.assertEqual(old.read_text(), original)
        # Logging out must not resurrect credentials from the old cache.
        (account_store.profile_dir(self.root, "A") / "session.json").unlink()
        account_store.launch_account(self.root, "t", [])
        self.assertFalse((account_store.profile_dir(self.root, "A") / "session.json").exists())

    def test_cross_account_login_does_not_mutate_outgoing_credentials(self):
        account_store.remember(self.root, self.a)
        auth = copy.deepcopy(self.a)
        registry = mock.Mock(agent_id="")
        with mock.patch.object(cli, "choose_login_method", return_value="remote"), \
             mock.patch.object(cli, "run_cancellable_blocking", return_value=self.b), \
             mock.patch.object(cli, "get_all_agents", return_value=[]), \
             mock.patch.object(cli, "_IN_SUB_TERMINAL", False), \
             mock.patch.object(cli, "_ACCOUNT_RESTART", None), \
             mock.patch.object(cli.console, "print"):
            self.assertTrue(cli._cmd_login(auth, registry))
            self.assertEqual(cli._ACCOUNT_RESTART, "B")
        self.assertEqual(auth, self.a)
        self.assertEqual(json.loads((account_store.profile_dir(self.root, "A") / "session.json").read_text()), self.a)

    def test_execution_auth_is_detached_and_mismatches_are_refused(self):
        frozen = account_store.frozen_auth(self.a, "A")
        self.a["userId"] = "B"
        self.a["cookies"]["auth"] = "changed"
        self.assertEqual(frozen["userId"], "A")
        self.assertEqual(frozen["cookies"]["auth"], "A-secret")
        with self.assertRaises(account_store.AccountError):
            account_store.frozen_auth(self.a, "A")

    def test_verification_must_identify_the_account_returned_by_login(self):
        with mock.patch.object(cli, "verify_session", return_value={"id": "B"}):
            self.assertFalse(cli._verify_login_identity(self.a))
        with mock.patch.object(cli, "verify_session", return_value={"id": "A", "name": "verified"}):
            self.assertTrue(cli._verify_login_identity(self.a))
        self.assertEqual(self.a["userName"], "verified")

    def test_switch_freezes_both_primary_and_employee_admission(self):
        account_store.freeze_admissions(lambda: True)
        self.assertIn("switch", agent_loop.begin_primary_run("missing")[1])
        self.assertIn("switch", agent_loop.start_agent_assignment("missing", "task", None)[1])
        account_store.cancel_transition()
        self.assertNotIn("switch", agent_loop.begin_primary_run("missing")[1])

    def test_idle_status_with_a_live_worker_still_blocks_switching(self):
        agent = mock.Mock(status="idle", thread=mock.Mock())
        agent.thread.is_alive.return_value = True
        with mock.patch.object(cli, "get_all_agents", return_value=[agent]), \
             mock.patch.object(cli.console, "print"):
            self.assertFalse(cli._account_idle())

    def test_switch_arguments_replace_old_account_and_preserve_backend(self):
        with mock.patch.object(sys, "argv", ["cli", "--account", "A", "--backend", "http://localhost:9", "--resume"]):
            args = cli._account_restart_args("B")
        self.assertEqual(args.count("--account"), 1)
        self.assertEqual(args[args.index("--account") + 1], "B")
        self.assertIn("--account-return", args)
        self.assertIn("http://localhost:9", args)

    def test_account_filter_rejects_a_foreign_blob_even_if_copied_into_our_directory(self):
        session_store.create_session(self.cwd, self.state, self.history)
        agent_loop.save_resume_state(self.state, self.history, self.cwd)
        self.assertTrue(agent_loop.list_resume_states(self.cwd))
        with mock.patch.object(paths, "ACCOUNT_USER_ID", "B"):
            self.assertEqual(agent_loop.list_resume_states(self.cwd), [])
            self.assertIsNone(agent_loop.load_resume_state(self.cwd, "source-task"))
            self.assertIsNone(session_store.load_current_session(self.cwd))
            with self.assertRaises(account_store.AccountError):
                cli._restore_resume_blob({"owner_user_id": "A"}, [])

    def test_task_provenance_survives_turns_and_autosaves(self):
        self.state.update(created_by="original", handoff_from={"session_id": "ancestor"})
        prepared = agent_loop.prepare_state_for_repl(self.state)
        agent_loop.save_resume_state(prepared, self.history, self.cwd)
        saved = agent_loop.load_resume_state(self.cwd, "source-task")
        self.assertEqual(saved["owner_user_id"], "A")
        self.assertEqual(saved["created_by"], "original")
        self.assertEqual(saved["state"]["handoff_from"], {"session_id": "ancestor"})

    def test_checkpoint_transfers_context_but_not_auth_or_runtime_grants(self):
        self.state.update(token="raw-secret", approval=True, _work_id="source-work",
                          lastOutput="api_key=sk-12345678901234567890",
                          _thread_messages=[{"role": "system", "content": "old tools"},
                                            {"role": "user", "content": "verify"}])
        env = self.make_handoff()
        received = task_handoff.continuation(env, "B", self.cwd)
        self.assertNotEqual(received["session_id"], "source-task")
        self.assertEqual(received["owner_user_id"], "B")
        self.assertEqual(received["created_by"], "A")
        self.assertEqual(received["handoff_from"]["session_id"], "source-task")
        self.assertEqual(received["tasks"][0]["subject"], "verify")
        rendered = json.dumps(env)
        self.assertNotIn("raw-secret", rendered)
        self.assertNotIn("sk-12345678901234567890", rendered)
        self.assertNotIn("source-work", rendered)
        self.assertNotIn("approval", env["checkpoint"]["state"])
        self.assertEqual(self.state["owner_user_id"], "A")

    def test_target_account_and_checkpoint_immutability(self):
        env = self.make_handoff()
        with self.assertRaises(ValueError):
            task_handoff.continuation(env, "C", self.cwd)
        with self.assertRaises(handoff.HandoffError):
            handoff.append(env["id"], "claim", "C", cwd=self.cwd)
        changed = copy.deepcopy(env)
        changed["checkpoint"]["state"]["objective"] = "other"
        with self.assertRaises(handoff.HandoffError):
            handoff.merge(env, changed)
        self.assertEqual(handoff.decode_token(handoff.export_token(env))["checkpoint"], env["checkpoint"])

    def test_repeated_accept_preserves_receiver_progress_and_has_one_session_id(self):
        env = self.make_handoff()
        one = task_handoff.continuation(env, "B", self.cwd)
        two = task_handoff.continuation(env, "B", self.cwd)
        self.assertEqual(one["session_id"], two["session_id"])
        with mock.patch.object(paths, "ACCOUNT_USER_ID", "B"):
            stored = account_tasks.save_received(one)
            stored["state"]["objective"] = "receiver progressed"
            path = agent_loop._resume_session_path(self.cwd, stored["session_id"])
            session_store._atomic_write_json(path, stored)
            again = account_tasks.save_received(two)
        self.assertEqual(again["state"]["objective"], "receiver progressed")

    def test_failed_task_save_does_not_change_account_or_teardown(self):
        with mock.patch.object(cli, "get_all_agents", return_value=[]), \
             mock.patch.object(cli.handle_meta_command, "_last_agent_state", self.state, create=True), \
             mock.patch.object(cli.handle_meta_command, "_last_chat_history", self.history, create=True), \
             mock.patch.object(cli.handle_meta_command, "_current_live_session", None, create=True), \
             mock.patch.object(cli, "save_resume_checkpoint", return_value=None), \
             mock.patch.object(cli, "_restart_process") as restart:
            with self.assertRaises(OSError):
                cli._save_before_account_switch()
            restart.assert_not_called()
        self.assertFalse(account_store._transitioning)

    def test_accept_rebinds_the_repl_and_repeated_accept_does_not_execute_again(self):
        source = session_store.create_session(self.cwd, self.state, self.history)
        source_path = session_store._session_path(self.cwd, source["session_id"])
        original = source_path.read_text()
        env = self.make_handoff()
        state = {"_session_id": "b-current", "owner_user_id": "B"}
        history = [{"role": "user", "content": "B's other task"}]
        with mock.patch.object(paths, "ACCOUNT_USER_ID", "B"), \
             mock.patch.object(paths, "SESSIONS_DIR", self.root / "B" / "sessions"), \
             mock.patch.object(cli, "get_all_agents", return_value=[]), \
             mock.patch.object(cli, "get_current_agent_id", return_value="primary"), \
             mock.patch.object(cli, "get_current_agent", return_value=None), \
             mock.patch.object(cli.handle_meta_command, "_last_agent_state", state, create=True), \
             mock.patch.object(cli.handle_meta_command, "_last_chat_history", history, create=True), \
             mock.patch.object(cli.handle_meta_command, "_last_existing_session", None, create=True), \
             mock.patch.object(cli.handle_meta_command, "_agent_switch_performed", False, create=True), \
             mock.patch.object(cli.console, "print"), \
             mock.patch.object(cli, "_enqueue_user_input", return_value=True) as queued:
            current = session_store.create_session(self.cwd, state, history)
            with mock.patch.object(cli.handle_meta_command, "_current_live_session", current, create=True):
                self.assertTrue(account_tasks.accept(env["id"], self.cwd))
                received = cli.handle_meta_command._last_agent_state
                self.assertEqual(received["owner_user_id"], "B")
                self.assertEqual(received["handoff_from"]["session_id"], "source-task")
                self.assertTrue(cli.handle_meta_command._agent_switch_performed)
                received["objective"] = "new progress"
                agent_loop.save_resume_state(received, cli.handle_meta_command._last_chat_history, self.cwd)
                self.assertFalse(account_tasks.accept(env["id"], self.cwd))
                self.assertEqual(cli.handle_meta_command._last_agent_state["objective"], "new progress")
                queued.assert_called_once()
        cli._release_live_session_lease()
        self.assertEqual(source_path.read_text(), original)
        self.assertEqual(handoff.project(handoff.load(env["id"], self.cwd))["holder"], "B")

    def test_accept_write_failure_leaves_the_source_unclaimed_and_releases_the_lease(self):
        env = self.make_handoff()
        blob = task_handoff.continuation(env, "B", self.cwd)
        with mock.patch.object(paths, "ACCOUNT_USER_ID", "B"), \
             mock.patch.object(cli, "get_all_agents", return_value=[]), \
             mock.patch.object(cli, "get_current_agent_id", return_value="primary"), \
             mock.patch.object(cli.handle_meta_command, "_last_agent_state", {}, create=True), \
             mock.patch.object(account_tasks, "save_received", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                account_tasks.accept(env["id"], self.cwd)
        self.assertEqual(handoff.project(handoff.load(env["id"], self.cwd))["holder"], "")
        lease = peer_coordination.session_lock_path(self.cwd, blob["session_id"], "B")
        self.assertFalse(lease.exists())

    def test_same_explicit_session_id_is_independent_between_accounts_but_locked_within_one_account(self):
        with mock.patch.object(paths, "PROCESS_INSTANCE_ID", "first-a"):
            self.assertTrue(peer_coordination.acquire_session_lease(self.cwd, "named-task")["ok"])
        with mock.patch.object(paths, "ACCOUNT_USER_ID", "B"), \
             mock.patch.object(paths, "PROCESS_INSTANCE_ID", "first-b"):
            self.assertTrue(peer_coordination.acquire_session_lease(self.cwd, "named-task")["ok"])
            peer_coordination.release_session_lease(self.cwd, "named-task")
        with mock.patch.object(paths, "PROCESS_INSTANCE_ID", "second-a"):
            self.assertFalse(peer_coordination.acquire_session_lease(self.cwd, "named-task")["ok"])
        with mock.patch.object(paths, "PROCESS_INSTANCE_ID", "first-a"):
            peer_coordination.release_session_lease(self.cwd, "named-task")

    def test_legacy_adoption_is_explicit_and_cannot_be_claimed_by_two_accounts(self):
        old = {"id": "old-task", "session_id": "old-task", "cwd": self.cwd,
               "timestamp": 1, "state": {"_session_id": "old-task"}, "chat_history": self.history}
        directory = self.root / "sessions"
        directory.mkdir()
        source = directory / "old.json"
        original = json.dumps(old)
        source.write_text(original)
        with mock.patch.object(cli, "get_all_agents", return_value=[]), \
             mock.patch.object(cli, "get_current_agent_id", return_value="primary"), \
             mock.patch.object(account_tasks, "activate", return_value=False), \
             mock.patch.object(cli.console, "print"):
            account_tasks.adopt("old-task", self.cwd)
            self.assertTrue(agent_loop.list_resume_states(self.cwd))
            with mock.patch.object(paths, "ACCOUNT_USER_ID", "B"):
                with self.assertRaises(account_store.AccountError):
                    account_tasks.adopt("old-task", self.cwd)
        self.assertEqual(source.read_text(), original)

    def test_real_cli_import_selects_independent_paths_with_shared_coordination(self):
        account_store.remember(self.root, self.a, "first")
        account_store.remember(self.root, self.b, "second")
        script = ("import sys,json; sys.argv=['laintas-cli','--account',sys.argv[1]]; "
                  "import laintas_cli as c,paths; "
                  "print(json.dumps({'account':paths.ACCOUNT_USER_ID,'home':str(paths.LAINTAS_HOME),"
                  "'sessions':str(paths.SESSIONS_DIR),'locks':str(paths.SESSION_LOCKS_DIR),"
                  "'auth':c.load_session()['userId']}))")
        environment = dict(os.environ, LAINTAS_HOME=str(self.root), LAINTAS_ACCOUNT_ID="", PYTHONDONTWRITEBYTECODE="1")
        rows = []
        for alias in ("first", "second"):
            result = subprocess.run([sys.executable, "-B", "-c", script, alias],
                cwd=str(Path(__file__).resolve().parents[1]), env=environment,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows.append(json.loads(result.stdout.strip().splitlines()[-1]))
        self.assertEqual([row["auth"] for row in rows], ["A", "B"])
        self.assertNotEqual(rows[0]["home"], rows[1]["home"])
        self.assertNotEqual(rows[0]["sessions"], rows[1]["sessions"])
        self.assertEqual(rows[0]["locks"], rows[1]["locks"])

    def test_a_selected_account_can_handoff_when_using_an_external_backend(self):
        with mock.patch.object(cli, "get_all_agents", return_value=[]), \
             mock.patch.object(cli.handle_meta_command, "_last_agent_state", self.state, create=True), \
             mock.patch.object(cli.handle_meta_command, "_last_chat_history", self.history, create=True), \
             mock.patch.object(paths, "live_cwd", return_value=self.cwd), \
             mock.patch.object(cli.console, "print"):
            cli._cmd_handoff(["/handoff", "new", "external task"], {})
        env = handoff.list_all(self.cwd)[0]
        self.assertEqual(env["checkpoint"]["source_user_id"], "A")
        self.assertEqual(env["createdBy"], "A")

    def test_stale_lease_takeover_has_only_one_winner_across_processes(self):
        lock = peer_coordination.session_lock_path(self.cwd, "stale-task")
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": 999999999, "instance_id": "dead"}))
        gate = self.root / "go"
        script = ("import sys,time,json; from pathlib import Path; import paths,peer_coordination as p; "
                  "paths.configure_account('A'); gate=Path(sys.argv[2]); "
                  "\nwhile not gate.exists(): time.sleep(.005)"
                  "\nprint(json.dumps(p.acquire_session_lease(sys.argv[1],'stale-task')),flush=True); input()")
        environment = dict(os.environ, LAINTAS_HOME=str(self.root), LAINTAS_ACCOUNT_ID="", PYTHONDONTWRITEBYTECODE="1")
        children = []
        try:
            for _ in range(4):
                children.append(subprocess.Popen([sys.executable, "-B", "-c", script, self.cwd, str(gate)],
                    cwd=str(Path(__file__).resolve().parents[1]), env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            gate.write_text("go")
            results = [json.loads(child.stdout.readline()) for child in children]
            self.assertEqual(sum(bool(row["ok"]) for row in results), 1)
        finally:
            for child in children:
                try:
                    child.communicate("\n", timeout=10)
                except (subprocess.TimeoutExpired, BrokenPipeError):
                    child.kill()
                    child.communicate()
                child.wait()

    def test_parallel_processes_do_not_lose_handoff_events(self):
        env = handoff.create("shared", "A", cwd=self.cwd)
        code = "import handoff,sys; [handoff.append(sys.argv[1], 'note', sys.argv[2], str(i), cwd=sys.argv[3]) for i in range(12)]"
        children = []
        try:
            for number in range(4):
                children.append(subprocess.Popen([sys.executable, "-B", "-c", code, env["id"], str(number), self.cwd],
                    cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            for child in children:
                _, errors = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, errors.decode())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
        self.assertEqual(len(handoff.load(env["id"], self.cwd)["events"]), 49)


if __name__ == "__main__":
    unittest.main()
