"""Shared Station operations. Runtime is injected; this module never imports the CLI."""
import re
import threading
from dataclasses import dataclass, replace
import worktree_manager

from agent_router import Candidate, RouteDecision, RouteRequest, decide, suggest_role


@dataclass(frozen=True)
class StationResult:
    ok: bool
    message: str
    agent_id: str = ""
    run_id: str = ""
    action: str = ""


@dataclass(frozen=True)
class AgentView:
    id: str
    name: str
    parent_id: str
    role: str
    status: str
    home: str
    deployment: str
    task_terminal: str
    run_id: str
    task: str
    reason: str
    result: str
    stage: str
    tools: str
    model: str
    queue_position: str


@dataclass(frozen=True)
class TerminalView:
    name: str
    parent: str
    owner: str
    alive: bool
    output: str
    created_at: float


class StationService:
    def __init__(self, runtime):
        self.runtime = runtime
        self._condition = threading.Condition()
        self._requests = {}
        self._pending = {}

    def candidates(self):
        r = self.runtime
        with r._registry_lock:
            result = []
            for a in r.get_all_agents():
                deployment = r.agent_deployment_terminal(a)
                terminal = r.get_terminal(deployment) if deployment else None
                policy = a.profile.tool_policy
                result.append(Candidate(
                    a.id, a.parent_id or "", a.role, a.profile.specialist_role or "",
                    tuple(a.profile.capability_tags),
                    tuple(r._allowed_tool_names_for_state(
                        {"_role_name": a.profile.specialist_role}, a.id)),
                    tuple(policy.denied_tools),
                    a.active_assignment is not None or a.deployment_pending or a.status in {"running", "waiting", "queued"},
                    a.lifecycle_terminated,
                    not deployment or bool(terminal and terminal.session and terminal.session.is_alive())))
            return tuple(result)

    def snapshot(self):
        r = self.runtime
        with r._registry_lock:
            agents = []
            for a in r.get_all_agents():
                job = a.active_assignment
                deployment = r.agent_deployment_terminal(a) or ""
                agents.append(AgentView(
                    a.id, a.name, a.parent_id or "", a.role, a.status,
                    a.home_terminal or "", deployment,
                    job.terminal_name if job else "private PTY" if a.ephemeral_session else "",
                    job.id if job else a.id if a.role == "subagent" else "",
                    job.task if job else str(a.state.get("objective") or a.state.get("_assignment_task") or ""),
                    a.route_reason,
                    a.error or a.last_reply or a.result or "", a.stage,
                    (", ".join(a.profile.tool_policy.allowed_tools)
                     if a.profile.tool_policy.allowed_tools is not None else "inherit")
                    + " | denied: " + (", ".join(a.profile.tool_policy.denied_tools) or "none"),
                    a.base_model or "inherit",
                    str(r.scheduler_status(a.id).get("queue_position") or "")))
            terminals = tuple(TerminalView(
                t.name, t.parent_terminal or "", t.stationed_agent_id or "",
                bool(t.session and t.session.is_alive()),
                str(t.session.full_output if t.session else "")[-12000:], t.created_at)
                for t in r.get_all_terminals())
        return tuple(agents), terminals

    def preview(self, request):
        r = self.runtime
        owner = r.get_agent(request.owner_id)
        if owner is None or owner.lifecycle_terminated or owner.abort_event.is_set():
            return RouteDecision("reject", reason="Manager has ended or was cancelled.")
        if request.role and r.agent_roles.get_role(request.role) is None:
            return RouteDecision("reject", reason="Unknown specialist role.")
        decision = decide(request, self.candidates(), can_spawn=r.can_spawn(request.owner_id))
        if decision.action != "spawn":
            return decision
        role = request.role or suggest_role(request.task)
        role_info = r.agent_roles.get_role(role) if role else None
        allowed = r._allowed_tool_names_for_state(owner.state, owner.id)
        if role_info and role_info.allowed_tools:
            allowed &= set(role_info.allowed_tools)
        if request.capability_tags or not set(request.required_tools).issubset(allowed):
            return RouteDecision("reject", reason="Child cannot satisfy the declared capabilities and tools.")
        cwd = owner.state.get("cwd") or owner.state.get("_task_cwd") or r.os.getcwd()
        if not self._readonly_role(role) and not worktree_manager.is_git_repo(cwd):
            return RouteDecision("parent", reason="Automatic writing tasks require Git worktree isolation; keep this task with its parent.")
        return replace(decision, reason=f"{decision.reason} Role: {role or 'general'}.")

    def _readonly_role(self, role):
        info = self.runtime.agent_roles.get_role(role) if role else None
        readonly_tools = {"fs.read", "fs.ls", "fs.grep", "fs.glob", "web.search", "web.fetch", "tool.search"}
        return bool(info and info.allowed_tools and set(info.allowed_tools).issubset(readonly_tools))

    def assign(self, request: RouteRequest, deps, *, session=None, events_cb=None):
        """Deduplicate admission, including concurrent retries, without locking during I/O."""
        key = (request.session_id, request.run_id, request.owner_id, request.request_id)
        if not request.request_id:
            return StationResult(False, "A request identity is required.")
        with self._condition:
            while key in self._pending:
                if self._pending[key] != request:
                    return StationResult(False, "Request identity conflicts with another task.")
                self._condition.wait()
            previous = self._requests.get(key)
            if previous:
                return previous[1] if previous[0] == request else StationResult(
                    False, "Request identity conflicts with another task.")
            # Keep successful identities for this runtime, never evict a live
            # deduplication record and accidentally admit the same work again.
            if len(self._requests) + len(self._pending) >= 4096:
                return StationResult(False, "Station request history is full; start a new runtime before admitting more work.")
            self._pending[key] = request
        result = None
        try:
            result = self._assign(request, deps, session, events_cb)
        except Exception as exc:
            result = StationResult(False, f"Admission failed: {type(exc).__name__}: {exc}")
        finally:
            with self._condition:
                self._pending.pop(key, None)
                if result is not None:
                    self._requests[key] = (request, result)
                self._condition.notify_all()
        return result

    def _assign(self, request, deps, session, events_cb):
        r = self.runtime
        decision = self.preview(request)
        if decision.action == "assign":
            ok, message, assignment = r.start_agent_assignment(
                decision.agent_id, request.task, deps, session=session,
                events_cb=events_cb, expected_parent_id=request.owner_id)
            return StationResult(ok, message, decision.agent_id,
                                 assignment.id if assignment else "", "assign")
        if decision.action == "spawn":
            role = request.role or suggest_role(request.task)
            owner = r.get_agent(request.owner_id)
            if owner is None:
                return StationResult(False, "Manager ended before admission.")
            child_id = r.spawn_subagent(
                request.owner_id, request.task, deps, session=session,
                events_cb=events_cb, role=role or None,
                group_id=request.group_id or None,
                concurrency_limit=request.concurrency_limit,
                state_overrides={"_require_worktree": not self._readonly_role(role),
                                 "_tool_allowlist": sorted(r._allowed_tool_names_for_state(
                    owner.state, request.owner_id)) or ["__route_no_tools__"]})
            child = r.get_agent(child_id) if child_id else None
            if child is None or child.status in {"error", "aborted"}:
                return StationResult(False, (child.error if child else "") or "Could not start child.")
            child.route_reason = decision.reason
            if not request.group_id:
                try:
                    r.branch_mod.open_branch(request.owner_id, "single", [(child_id, request.task)])
                except Exception:
                    r.abort_agent(child_id)
                    raise
            return StationResult(True, f"Routed task to {child_id}. {decision.reason}",
                                 child_id, child_id, "spawn")
        return StationResult(False, decision.reason, action=decision.action)

    def route_parallel(self, owner_id, tasks, deps, *, session=None, events_cb=None,
                       run_id="", session_id="", max_parallel=4):
        """Retain every task; execution slots, not list slicing, impose the cap."""
        r = self.runtime
        branch = r.branch_mod.open_branch(owner_id, "parallel", [],
            budget=r.branch_mod.Budget(token_max=int(r.get_runtime_config("auto_pilot_budget_tokens") or 0)))
        results = []
        try:
            for index, task in enumerate(tasks):
                request = RouteRequest(
                    owner_id, str(task), f"auto-{index}", session_id=session_id,
                    run_id=run_id or branch.branch_id, group_id=branch.branch_id,
                    concurrency_limit=max(1, int(max_parallel)))
                result = (self.assign(request, deps, session=session, events_cb=events_cb)
                          if branch.status == r.branch_mod.STATUS_OPEN else
                          StationResult(False, "Batch stopped before task admission.", action="parent"))
                if result.ok:
                    try:
                        r.branch_mod.add_member(branch, result.agent_id, request.task)
                    except RuntimeError:
                        r.abort_agent(result.agent_id)
                        result = StationResult(False, "Batch stopped during task admission.", action="parent")
                results.append(result)
                if not result.ok:
                    r.send_to_agent(owner_id, {"from": "router", "kind": "child-error",
                                              "task": request.task, "error": result.message})
        finally:
            r.branch_mod.seal(branch.branch_id)
            if not branch.members:
                r.branch_mod.close(branch.branch_id, "No tasks admitted.")
        return results

    def deploy(self, agent_id, terminal_name, *, owner_id, create_terminal):
        r = self.runtime
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", terminal_name):
            return StationResult(False, "Invalid terminal name.")
        terminal_name = "term0" if terminal_name.lower() in {"current", "here", "term0"} else terminal_name
        agent = r.get_agent(agent_id)
        if agent is None:
            return StationResult(False, "Agent not found.")
        # Reserve under the assignment lock, but start the PTY outside locks.
        with agent.assignment_lock:
            if (agent.active_assignment is not None or agent.deployment_pending
                    or agent.status in {"queued", "running", "waiting"}):
                return StationResult(False, "Finish or cancel active work before deployment.")
            if agent.id != owner_id and agent.parent_id != owner_id:
                return StationResult(False, "Agent is not a direct child of this manager.")
            original_deployment = r.agent_deployment_terminal(agent)
            agent.deployment_pending = True
        created = None
        committed = False
        try:
            existing = r.get_terminal(terminal_name)
            if existing and (not existing.session or not existing.session.is_alive()):
                return StationResult(False, "Terminal has ended; remove it before reusing its name.")
            if existing is None:
                created = create_terminal(terminal_name)
                if not created.is_alive():
                    raise RuntimeError("Terminal did not start.")
            # Publish and bind together, rechecking facts after terminal startup.
            with r._registry_lock:
                manager = r.get_agent(owner_id)
                if (r.get_agent(agent_id) is not agent or agent.lifecycle_terminated
                        or manager is None or manager.lifecycle_terminated or manager.abort_event.is_set()
                        or r.agent_deployment_terminal(agent) != original_deployment):
                    raise RuntimeError("Agent or manager changed during deployment.")
                if created is not None:
                    r.register_terminal(created, created.command, 0, name=terminal_name,
                                        parent_terminal=r.agent_scope_terminal(manager) or "term0")
                    try:
                        if not r.station_agent(agent_id, terminal_name):
                            raise RuntimeError("Deployment is no longer valid.")
                    except BaseException:
                        r.unregister_terminal(terminal_name)
                        created = None  # unregister already closed it
                        raise
                elif not r.station_agent(agent_id, terminal_name):
                    raise RuntimeError("Terminal is occupied or deployment is no longer valid.")
                committed = True
            return StationResult(True, f"Stationed {agent_id} → {terminal_name}.", agent_id)
        except Exception as exc:
            return StationResult(False, str(exc))
        finally:
            try:
                if created is not None and not committed:
                    created.close()
            finally:
                with agent.assignment_lock:
                    agent.deployment_pending = False

    def undeploy(self, agent_id, owner_id):
        r = self.runtime
        agent = r.get_agent(agent_id)
        if agent is None:
            return StationResult(False, "Agent has ended.")
        with agent.assignment_lock:
            if agent.parent_id != owner_id or agent.role == "primary":
                return StationResult(False, "Only this manager's employees can be unstationed.")
            if agent.active_assignment or agent.deployment_pending or agent.status in {"running", "queued", "waiting"}:
                return StationResult(False, "Finish or cancel active work first.")
            r.unstation_agent(agent.id)
        return StationResult(True, f"Unstationed {agent.id}.")

    def cancel(self, agent_id, run_id, owner_id):
        r = self.runtime
        agent = r.get_agent(agent_id)
        if agent is None:
            return StationResult(False, "Agent has ended.")
        with agent.assignment_lock:
            active = agent.active_assignment
            identity = active.id if active else agent.id if agent.role == "subagent" else ""
            if agent.parent_id != owner_id or not identity or identity != run_id:
                return StationResult(False, "Task changed or is not owned by this manager; refresh first.")
            r.abort_agent(agent_id)
        return StationResult(True, "Cancellation requested; resources release when execution stops.")

    def terminal_impact(self, name):
        r = self.runtime
        with r._registry_lock:
            names = {name}
            terminals = r.get_all_terminals()
            while True:
                expanded = names | {t.name for t in terminals if t.parent_terminal in names}
                if expanded == names:
                    break
                names = expanded
            return (
                tuple(sorted((t.name, t.created_at, t.stationed_agent_id or "")
                             for t in terminals if t.name in names)),
                tuple(sorted((a.id, a.home_terminal or "", r.agent_deployment_terminal(a) or "")
                             for a in r.get_all_agents()
                             if a.home_terminal in names or r.agent_deployment_terminal(a) in names)),
            )

    def close_terminal(self, name, owner_id, *, expected_created_at, expected_impact=None):
        r = self.runtime
        if name == "term0":
            return StationResult(False, "The primary terminal belongs to the CLI; use /exit.")
        with r._registry_lock:
            terminal = r.get_terminal(name)
            manager = r.get_agent(owner_id)
            if terminal is None or manager is None:
                return StationResult(False, "Terminal or manager has ended.")
            if terminal.created_at != expected_created_at:
                return StationResult(False, "Terminal was replaced; inspect it before closing.")
            if expected_impact is not None and self.terminal_impact(name) != expected_impact:
                return StationResult(False, "Affected resources changed; inspect them before closing.")
            if terminal.parent_terminal != (r.agent_scope_terminal(manager) or "term0"):
                return StationResult(False, "Terminal is outside this manager's direct scope.")
            if not r.unregister_terminal(name):
                return StationResult(False, "Terminal has already ended.")
        return StationResult(True, f"Closed {name} and its owned resources.")


_services_lock = threading.Lock()


def service_for(runtime):
    # The registry owns the service lifetime. No reverse import or CLI global.
    with _services_lock:
        service = getattr(runtime, "_station_service", None)
        if service is None:
            service = StationService(runtime)
            runtime._station_service = service
        return service
