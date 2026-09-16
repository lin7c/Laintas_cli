import threading
import asyncio
import unittest
from dataclasses import replace
from unittest import mock
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import agent_loop as runtime
import resource_ui
import station_ui
from agent_router import Candidate, RouteRequest, decide, suggest_role
from station_service import StationService


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.request = RouteRequest("root", "inspect source", "req", reuse_employee=True,
                                    required_tools=("fs.read",), allow_spawn=False)
        self.candidate = Candidate("child", "root", "pool", "explorer", (),
                                   ("fs.read",), (), False, False, True)

    def test_direct_child_with_tools_is_selected(self):
        self.assertEqual(decide(self.request, (self.candidate,), can_spawn=True).agent_id, "child")

    def test_terminal_membership_cannot_override_parent_or_policy(self):
        for invalid in [replace(self.candidate, parent_id="other"),
                        replace(self.candidate, denied_tools=("fs.read",)),
                        replace(self.candidate, busy=True),
                        replace(self.candidate, terminal_alive=False)]:
            self.assertEqual(decide(self.request, (invalid,), can_spawn=False).action, "parent")

    def test_explicit_target_failure_does_not_silently_spawn(self):
        request = replace(self.request, target_id="child", allow_spawn=True)
        decision = decide(request, (replace(self.candidate, busy=True),), can_spawn=True)
        self.assertEqual(decision.action, "reject")

    def test_unstructured_auto_task_prefers_isolated_child(self):
        request = replace(self.request, reuse_employee=False, allow_spawn=True)
        self.assertEqual(decide(request, (self.candidate,), can_spawn=True).action, "spawn")

    def test_role_selection_does_not_confuse_testing_with_readonly_coverage_review(self):
        self.assertEqual(suggest_role("review the API"), "reviewer")
        self.assertEqual(suggest_role("定位登录入口"), "explorer")
        self.assertEqual(suggest_role("run tests"), "")
        self.assertEqual(suggest_role("test the implementation"), "")
        self.assertEqual(suggest_role("review and fix the API"), "")


class StationTests(unittest.TestCase):
    def setUp(self):
        runtime.close_all_agents()
        runtime.close_all_terminals()
        self.addCleanup(runtime.close_all_agents)
        self.addCleanup(runtime.close_all_terminals)
        shell = mock.Mock(command="bash", full_output="")
        shell.is_alive.return_value = True
        runtime.register_terminal(shell, "bash", 0, name="term0")
        self.manager = runtime.register_agent(name="manager", role="primary")
        self.employee = runtime.register_agent(name="employee", role="pool", parent_id=self.manager.id)
        self.service = StationService(runtime)
        self.request = RouteRequest(self.manager.id, "inspect source", "req", target_id=self.employee.id)

    def test_concurrent_retries_only_admit_once(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def admit(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return True, "accepted", mock.Mock(id="job-1")
        with mock.patch.object(runtime, "start_agent_assignment", side_effect=admit) as start:
            workers = [threading.Thread(target=lambda: results.append(
                self.service.assign(self.request, None))) for _ in range(2)]
            try:
                workers[0].start()
                self.assertTrue(entered.wait(2))
                workers[1].start()
            finally:
                release.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(3)
                        self.assertFalse(worker.is_alive())
            self.assertEqual(start.call_count, 1)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            conflict = self.service.assign(replace(self.request, task="different task"), None)
            self.assertFalse(conflict.ok)

    def test_cancel_stale_job_does_not_abort_next_assignment(self):
        self.employee.active_assignment = runtime.AgentAssignment("new-job", "new task", "temporary")
        with mock.patch.object(runtime, "abort_agent") as abort:
            result = self.service.cancel(self.employee.id, "old-job", self.manager.id)
        self.assertFalse(result.ok)
        abort.assert_not_called()

    def test_cancel_current_job_uses_existing_cascade(self):
        self.employee.active_assignment = runtime.AgentAssignment("job", "task", "temporary")
        with mock.patch.object(runtime, "abort_agent") as abort:
            self.assertTrue(self.service.cancel(self.employee.id, "job", self.manager.id).ok)
        abort.assert_called_once_with(self.employee.id)

    def test_cancelled_manager_cannot_admit_work(self):
        self.manager.abort_event.set()
        with mock.patch.object(runtime, "start_agent_assignment") as start:
            self.assertFalse(self.service.assign(self.request, None).ok)
        start.assert_not_called()

    def test_auto_write_without_isolation_is_not_spawned(self):
        request = replace(self.request, target_id="", task="implement a new API")
        with mock.patch("station_service.worktree_manager.is_git_repo", return_value=False), \
                mock.patch.object(runtime, "spawn_subagent") as spawn:
            result = self.service.assign(request, None)
        self.assertFalse(result.ok)
        self.assertIn("isolation", result.message)
        spawn.assert_not_called()

    def test_child_thread_start_failure_removes_unstarted_worktree(self):
        worktree = mock.Mock(path="/unused/worktree", branch="isolated")
        with mock.patch("worktree_manager.is_git_repo", return_value=True), \
                mock.patch("worktree_manager.create_isolated_worktree", return_value=worktree), \
                mock.patch("worktree_manager.remove_worktree") as remove, \
                mock.patch.object(runtime.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("thread limit")
            child_id = runtime.spawn_subagent(self.manager.id, "work", None)
        self.assertEqual(runtime.get_agent(child_id).status, "error")
        self.assertIn("thread limit", runtime.get_agent(child_id).error)
        remove.assert_called_once_with(worktree)

    def test_terminal_replacement_rejects_old_close_request(self):
        shell = mock.Mock(command="bash", full_output="")
        shell.is_alive.return_value = True
        runtime.register_terminal(shell, "bash", 0, name="work")
        result = self.service.close_terminal("work", self.manager.id, expected_created_at=-1)
        self.assertFalse(result.ok)
        shell.close.assert_not_called()

    def test_deployment_failure_closes_only_new_terminal(self):
        shell = mock.Mock(command="bash", full_output="")
        shell.is_alive.return_value = True
        with mock.patch.object(runtime, "station_agent", return_value=False):
            result = self.service.deploy(self.employee.id, "new", owner_id=self.manager.id,
                                         create_terminal=lambda _: shell)
        self.assertFalse(result.ok)
        self.assertIsNone(runtime.get_terminal("new"))
        self.assertTrue(shell.close.called)
        self.assertIsNotNone(runtime.get_agent(self.manager.id))

    def test_deployment_reserves_admission_without_holding_assignment_lock_during_start(self):
        shell = mock.Mock(command="bash", full_output="")
        shell.is_alive.return_value = True
        def create(_name):
            self.assertTrue(self.employee.assignment_lock.acquire(blocking=False))
            self.employee.assignment_lock.release()
            self.assertTrue(self.employee.deployment_pending)
            ok, message, _ = runtime.start_agent_assignment(self.employee.id, "other work", None)
            self.assertFalse(ok)
            self.assertIn("deployment", message)
            return shell
        self.assertTrue(self.service.deploy(self.employee.id, "new", owner_id=self.manager.id,
                                            create_terminal=create).ok)
        self.assertFalse(self.employee.deployment_pending)

    def test_occupied_terminal_is_not_stopped(self):
        shell = mock.Mock(command="bash", full_output="")
        shell.is_alive.return_value = True
        runtime.register_terminal(shell, "bash", 0, name="occupied")
        other = runtime.register_agent(name="other", role="pool", parent_id=self.manager.id)
        runtime.station_agent(other.id, "occupied")
        result = self.service.deploy(self.employee.id, "occupied", owner_id=self.manager.id,
                                     create_terminal=mock.Mock())
        self.assertFalse(result.ok)
        shell.close.assert_not_called()

    def test_snapshot_is_detached_from_mutable_agent_state(self):
        agents, _ = self.service.snapshot()
        self.employee.status = "running"
        saved = next(a for a in agents if a.id == self.employee.id)
        self.assertEqual(saved.status, "idle")

    def test_station_browser_reuses_resource_ui_and_in_place_view_action(self):
        with mock.patch.object(resource_ui, "ResourceBrowser") as browser:
            station_ui.show_station(self.service, owner_id=self.manager.id, deps=None,
                                    session={}, create_terminal=mock.Mock())
        options = browser.call_args.kwargs
        items = options["load_items"]()
        self.assertIn("Parent:", "\n".join(line.text for line in options["load_detail"](items[1]).lines))
        result = options["actions"][0].handler(items[0])
        self.assertTrue(result.refresh)
        self.assertFalse(result.close)
        self.assertEqual(options["presentation"], "operations")
        self.assertEqual(options["assistant_label"], "Command")

    def test_live_station_switches_view_without_closing_or_stopping_agents(self):
        browsers, errors = [], []
        browser_type = resource_ui.ResourceBrowser
        with create_pipe_input() as pipe:
            def create_browser(**kwargs):
                browser = browser_type(**kwargs, input=pipe, output=DummyOutput())
                browsers.append(browser)
                async def exercise():
                    try:
                        await asyncio.sleep(.15)
                        self.assertTrue(any(i.key.startswith("agent:") for i in browser.items))
                        pipe.send_text("v")
                        await asyncio.sleep(.1)
                        self.assertTrue(any(i.key == "terminal:term0" for i in browser.items))
                        self.assertFalse(browser.app.is_done)
                        pipe.send_text("v")
                        await asyncio.sleep(.1)
                        self.assertFalse(any(i.key.startswith("terminal:") for i in browser.items))
                        self.assertFalse(browser.app.is_done)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        pipe.send_text("q")
                browser.app.pre_run_callables.append(lambda: browser.app.create_background_task(exercise()))
                return browser
            with mock.patch.object(resource_ui, "ResourceBrowser", side_effect=create_browser):
                station_ui.show_station(self.service, owner_id=self.manager.id, deps=None,
                                        session={}, create_terminal=mock.Mock())
        if errors:
            raise errors[0]
        self.assertEqual(len(browsers), 1)
        self.assertFalse(self.employee.abort_event.is_set())
        self.assertFalse(browsers[0]._running)

    def test_auto_batch_keeps_every_task_and_registers_supervision(self):
        def spawn(parent, task, deps, **kwargs):
            return runtime.register_agent(role="subagent", parent_id=parent).id
        with mock.patch.object(runtime, "spawn_subagent", side_effect=spawn) as create, \
                mock.patch("station_service.worktree_manager.is_git_repo", return_value=True), \
                mock.patch.object(runtime.branch_mod, "open_branch") as open_branch, \
                mock.patch.object(runtime.branch_mod, "seal"):
            branch = runtime.branch_mod.Branch("batch", self.manager.id, "parallel", sealed=False)
            open_branch.return_value = branch
            results = self.service.route_parallel(self.manager.id, ["a", "b", "c"], None,
                                                   run_id="run", max_parallel=1)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(branch.members), 3)
        self.assertTrue(all(r.ok for r in results))
        self.assertTrue(all(call.kwargs["concurrency_limit"] == 1 for call in create.call_args_list))

    def test_batch_stopped_during_admission_reports_all_unstarted_tasks(self):
        branch = runtime.branch_mod.Branch("stopped", self.manager.id, "parallel",
                                           status=runtime.branch_mod.STATUS_CLOSED, sealed=False)
        with mock.patch.object(runtime.branch_mod, "open_branch", return_value=branch), \
                mock.patch.object(runtime.branch_mod, "seal"), \
                mock.patch.object(runtime.branch_mod, "close"), \
                mock.patch.object(self.service, "assign") as assign:
            results = self.service.route_parallel(self.manager.id, ["a", "b", "c"], None)
        assign.assert_not_called()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(not result.ok for result in results))

    def test_group_cap_queues_without_blocking_other_groups(self):
        old_cap = runtime._max_concurrent
        runtime._max_concurrent = 8
        self.addCleanup(setattr, runtime, "_max_concurrent", old_cap)
        first = runtime.register_agent(role="subagent", parent_id=self.manager.id)
        second = runtime.register_agent(role="subagent", parent_id=self.manager.id)
        third = runtime.register_agent(role="subagent", parent_id=self.manager.id)
        for agent in (first, second):
            agent.group_id, agent.concurrency_limit = "batch", 1
        started = []
        finished = threading.Event()
        runtime.schedule_agent(first.id, lambda ok: started.append(first.id))
        runtime.schedule_agent(second.id, lambda ok: (started.append(second.id), finished.set()))
        runtime.schedule_agent(third.id, lambda ok: started.append(third.id))
        self.assertEqual(started, [first.id, third.id])
        self.assertEqual(second.status, "queued")
        runtime.mark_agent_finished(first.id)
        self.assertTrue(finished.wait(2))
        self.assertIn(second.id, started)

    def test_cancelling_queued_member_does_not_release_running_slot(self):
        first = runtime.register_agent(role="subagent", parent_id=self.manager.id)
        second = runtime.register_agent(role="subagent", parent_id=self.manager.id)
        for a in (first, second):
            a.group_id, a.concurrency_limit = "batch", 1
        cancelled = threading.Event()
        runtime.schedule_agent(first.id, lambda ok: None)
        runtime.schedule_agent(second.id, lambda ok: cancelled.set() if not ok else None)
        self.assertTrue(self.service.cancel(second.id, second.id, self.manager.id).ok)
        self.assertTrue(cancelled.wait(2))
        self.assertTrue(first.slot_held)
        self.assertFalse(second.slot_held)
        runtime.mark_agent_finished(first.id)
        self.assertEqual(runtime._running_count, 0)
