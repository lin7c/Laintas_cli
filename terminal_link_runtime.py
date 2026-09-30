"""CLI runtime adapter for terminal adoption; execution never leaves its owner."""
import atexit
import json
import os
import re
import threading
import time
import uuid

import account_store
import paths
import terminal_link as link


class LinkedTerminal:
    command = "linked CLI"
    def __init__(self, underlying=None):
        self.underlying = underlying
        self.online = True
        self.closed = False
        self.full_output = ""
    def is_alive(self):
        return self.online and not self.closed
    def close(self):
        # Called under the runtime registry lock: never do I/O here.
        self.closed = True
    def read_output(self, **_kwargs):
        return self.underlying.read_output(**_kwargs) if self.underlying else self.full_output
    def __getattr__(self, name):
        if self.underlying is not None:
            return getattr(self.underlying, name)
        raise AttributeError(name)


class Adapter:
    def __init__(self, runtime, session, deps, notice):
        self.r = runtime
        self.session = account_store.frozen_auth(session or {})
        self.deps = deps
        self.notice = notice
        self.work = {}
        self.work_lock = threading.RLock()
        self.imported = {}
        self.controlled = set()
        self.parents = {}

    def manager_exists(self, aid):
        a = self.r.get_agent(aid)
        return bool(a and not a.lifecycle_terminated and not a.remote_terminal)

    def has_terminal(self, name):
        return self.r.get_terminal(name) is not None

    def has_child_terminals(self):
        return any(t.name != "term0" for t in self.r.get_all_terminals())

    def idle(self):
        return all(not a.active_assignment and not a.deployment_pending and
                   a.status not in {"queued", "running", "thinking", "waiting"}
                   and not (a.thread and a.thread.is_alive())
                   for a in self.r.get_all_agents())

    def inventory(self):
        return [{"id": a.id, "name": a.name, "primary": a.role == "primary",
                 "status": a.status, "title": a.profile.title,
                 "role": a.profile.specialist_role or "",
                 "tags": list(a.profile.capability_tags),
                 "tools": sorted(self.r._allowed_tool_names_for_state(a.state, a.id)),
                 "model": a.base_model, "deployed": bool(self.r.agent_deployment_terminal(a))}
                for a in self.r.get_all_agents()
                if a.role in {"primary", "pool", "deployed"} and not a.lifecycle_terminated
                and not a.remote_terminal]

    def enter_child(self, invite):
        self.controlled = {row["id"] for row in self.inventory()}
        with self.r._registry_lock:
            for aid in self.controlled:
                a = self.r.get_agent(aid)
                self.parents[aid] = (a, a.parent_id)
                a._term_original_parent = a.parent_id
                a.parent_id = invite["parent"] + "/" + invite["owner"]
                a.child_ids[:] = [cid for cid in a.child_ids if cid not in self.controlled]

    def leave_child(self):
        with self.r._registry_lock:
            for aid, (a, original) in self.parents.items():
                if self.r.get_agent(aid) is a:
                    a.parent_id = original
                    del a._term_original_parent
            for aid, (a, original) in self.parents.items():
                parent = self.r.get_agent(original) if original else None
                if self.r.get_agent(aid) is a and parent and aid not in parent.child_ids:
                    parent.child_ids.append(aid)
        self.parents.clear()
        self.controlled.clear()

    def import_child(self, invite, inventory):
        # Routing readers must not see a just-registered employee before its
        # remote identity is set, or they could execute it in the controller.
        with self.r._registry_lock:
            return self._import_child_locked(invite, inventory)

    def _import_child_locked(self, invite, inventory):
        r, alias = self.r, invite["alias"]
        if sum(bool(row.get("primary")) for row in inventory) != 1:
            raise link.LinkError("Execution terminal must export exactly one primary")
        ids = set()
        for row in inventory:
            aid = row.get("id")
            if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", aid) or aid in ids:
                raise link.LinkError("Invalid or duplicate exported Agent ID")
            ids.add(aid)
            if r.get_agent(alias + "/" + aid):
                raise link.LinkError("Agent proxy name is already in use")
            if not isinstance(row.get("tools"), list) or not all(isinstance(t, str) for t in row["tools"]):
                raise link.LinkError("Invalid exported tool policy")
        existing = r.get_terminal(alias)
        if existing and (not invite.get("created") or getattr(existing, "link_creation_id", None) != invite["id"]):
            raise link.LinkError("Terminal name is already in use")
        session = LinkedTerminal(existing.session if existing else None)
        made = []
        try:
            if existing:
                existing.session = session
            else:
                r.register_terminal(session, session.command, 0, name=alias, parent_terminal="term0")
            for row in inventory:
                profile = r.EmployeeProfile(title=str(row.get("title") or "Remote Agent"),
                    specialist_role=None, capability_tags=list(row.get("tags") or []),
                    tool_policy=r.AgentToolPolicy(allowed_tools=row["tools"]))
                a = r.register_agent(alias + "/" + row["id"], depth=1,
                    parent_id=invite["owner"], role="deployed" if row.get("primary") else "pool",
                    profile=profile, replace_existing=False)
                made.append(a.id)
                a.remote_terminal, a.remote_agent, a.remote_relation = alias, row["id"], invite["id"]
                a.home_terminal = alias
                a.base_model = str(row.get("model") or "")
                a.status = str(row.get("status") or "idle")
                if row.get("primary"):
                    a.deployment_terminal = a.stationed_terminal = alias
                    t = r.get_terminal(alias)
                    t.stationed_agent_id = t.dialog_agent_id = a.id
            self.imported[invite["id"]] = (alias, made)
        except BaseException:
            for aid in made:
                r.unregister_agent(aid)
            if existing:
                existing.session = session.underlying
            else:
                r.unregister_terminal(alias)
            raise

    def remove_child(self, child):
        entry = self.imported.pop(child["id"], None)
        if entry:
            alias, ids = entry
            for aid in ids:
                self.r.unregister_agent(aid)
            t = self.r.get_terminal(alias)
            if t and isinstance(t.session, LinkedTerminal) and t.session.underlying:
                t.session = t.session.underlying
                t.stationed_agent_id = t.dialog_agent_id = None
            else:
                self.r.unregister_terminal(alias)

    def offline(self, child):
        t = self.r.get_terminal(child["alias"])
        if t and isinstance(t.session, LinkedTerminal):
            t.session.online = False

    def admitted(self, a, result, task):
        with a.assignment_lock:
            # A status poll may have completed this job before the RPC returned.
            if any(j["id"] == result["job_id"] for j in a.assignment_history):
                return
            a.active_assignment = self.r.AgentAssignment(result["job_id"], task, a.remote_terminal,
                status="running", created_at=time.time())
            a.status = "running"

    def sync(self, child, result):
        r = self.r
        t = r.get_terminal(child["alias"])
        if not t or not isinstance(t.session, LinkedTerminal) or t.session.closed:
            return
        t.session.online = True
        for row in result.get("inventory", []):
            a = r.get_agent(child["alias"] + "/" + str(row.get("id")))
            if a and a.remote_relation == child["id"]:
                a.profile.tool_policy.allowed_tools = list(row.get("tools") or [])
                if not a.active_assignment:
                    a.status = str(row.get("status") or "idle")
        for job in result.get("jobs", []):
            a = r.get_agent(child["alias"] + "/" + str(job.get("agent")))
            if not a or a.remote_relation != child["id"]:
                continue
            with a.assignment_lock:
                if any(j["id"] == job["job_id"] for j in a.assignment_history):
                    continue
                if job["status"] not in {"completed", "error", "aborted"}:
                    if a.active_assignment is None:
                        self.admitted_unlocked(a, job)
                    continue
                if a.active_assignment and a.active_assignment.id != job["job_id"]:
                    continue
                a.active_assignment = None
                a.last_reply = a.result = str(job.get("result") or "")
                a.error = str(job.get("error") or "")
                a.status = "idle" if job["status"] == "completed" else job["status"]
                a.assignment_history.append({"id": job["job_id"], "status": job["status"],
                    "task": job.get("task", ""), "result": a.result, "error": a.error})
                t.session.full_output = (t.session.full_output + "\n" + a.id + ": " +
                                         (a.error or a.result))[-12000:]
                r.send_to_agent(child["owner"], {"kind": "child-done" if job["status"] == "completed" else "child-error",
                    "from": a.id, "run_id": job["job_id"], "summary": a.result,
                    "error": a.error, "status": job["status"]})
                try:
                    import agent_ui_events
                    agent_ui_events.hub.emit("agent_done" if job["status"] == "completed" else "agent_error",
                        agent_id=a.id, terminal_name=child["alias"], run_id=job["job_id"],
                        summary=a.error or a.result, status=job["status"])
                except Exception:
                    pass

    def admitted_unlocked(self, a, job):
        a.active_assignment = self.r.AgentAssignment(job["job_id"], job.get("task", ""),
            a.remote_terminal, status=job["status"], created_at=time.time())
        a.status = "running"

    def start_job(self, relation, job_id, payload):
        r = self.r
        aid, task, tools = payload.get("agent"), payload.get("task"), payload.get("tools")
        if aid not in self.controlled or not isinstance(task, str) or not task.strip():
            raise link.LinkError("Unknown exported Agent or empty task")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise link.LinkError("Invalid task tool scope")
        a = r.get_agent(aid)
        if not a or a.remote_terminal:
            raise link.LinkError("Agent is unavailable")
        scope = sorted(set(tools) & r._allowed_tool_names_for_state(a.state, aid)) or ["__term_no_tools__"]
        with self.work_lock, link.delegated():
            if any(j["agent"] == aid and
                   (j["native"].status if j.get("native") is not None else j["status"]) in {"queued", "running"}
                   for j in self.work.values()):
                raise link.LinkError("Agent already has a delegated task")
            job = {"job_id": job_id, "agent": aid, "task": task, "relation": relation,
                   "status": "running", "result": "", "error": "", "native": None}
            old_scope = a.state.get("_tool_allowlist")
            if a.role == "primary":
                ok, message = r.begin_primary_run(aid)
                if not ok:
                    raise link.LinkError(message)
                a.state["_tool_allowlist"] = scope
                a.chat_history.append({"role": "user", "content": task, "input_kind": "prompt"})
                self.work[job_id] = job
                def execute():
                    reply = error = ""
                    try:
                        with r.thread_agent(aid):
                            result = r.run_agent_loop(self.deps(), task, self.session, a.state, a.chat_history,
                                depth=0, agent_id=aid, interrupt_event=a.abort_event,
                                message_queue=a.message_queue)
                        reply = r.harvest_agent_reply(result, a.chat_history)
                        if isinstance(result, dict) and result.get("success", True) is False and not a.abort_event.is_set():
                            error = r.describe_exit_reason(result)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    finally:
                        with self.work_lock:
                            self._restore_scope(a, old_scope)
                            job.update(status="aborted" if a.abort_event.is_set() else "error" if error else "completed",
                                       result=reply[-50000:], error=error, completed_at=time.time())
                            r.finish_primary_run(aid, reply=reply, error=error, aborted=a.abort_event.is_set())
                        if not a.lifecycle_terminated:
                            import agent_persistence
                            agent_persistence.save_agent_state(a)
                a.thread = threading.Thread(target=execute, name="term-job-" + job_id, daemon=True)
                try:
                    a.thread.start()
                except BaseException:
                    self._restore_scope(a, old_scope)
                    self.work.pop(job_id, None)
                    r.finish_primary_run(aid, error="Could not start delegated task")
                    raise
            else:
                if a.active_assignment or a.status in {"running", "queued", "thinking", "waiting"}:
                    raise link.LinkError("Agent already has active work")
                a.state["_tool_allowlist"] = scope
                def finished(native):
                    # Restoration belongs to execution teardown. The controller
                    # can disappear before the next status poll. This callback
                    # runs under the employee lock: do not acquire work_lock.
                    self._restore_scope(a, old_scope)
                ok, message, native = r.start_agent_assignment(aid, task, self.deps(),
                    session=self.session, on_finished=finished)
                if not ok:
                    self._restore_scope(a, old_scope)
                    raise link.LinkError(message)
                job["native"] = native
                self.work[job_id] = job
        return {"ok": True, "job_id": job_id}

    def _restore_scope(self, a, old):
        if old is None:
            a.state.pop("_tool_allowlist", None)
        else:
            a.state["_tool_allowlist"] = old

    def jobs(self, relation):
        with self.work_lock:
            result = []
            for job in self.work.values():
                if job["relation"] != relation:
                    continue
                native = job.get("native")
                if native is not None:
                    job.update(status=native.status, result=str(native.result or "")[-50000:],
                               error=native.error, completed_at=native.completed_at)
                result.append({k: v for k, v in job.items() if k not in {"native", "old_scope", "relation"}})
            active = [j for j in result if j["status"] not in {"completed", "error", "aborted"}]
            completed = sorted((j for j in result if j not in active),
                               key=lambda j: j.get("completed_at") or 0)
            return active + completed[-128:]

    def cancel_job(self, relation, job_id):
        with self.work_lock:
            job = self.work.get(job_id)
            if not job or job["relation"] != relation or job["status"] not in {"queued", "running"}:
                return
            a = self.r.get_agent(job["agent"])
            if a and (job["native"] is None or a.active_assignment is job["native"]):
                self.r.abort_agent(a.id)

    def cancel_owned(self, relation):
        for row in self.jobs(relation):
            self.cancel_job(relation, row["job_id"])

    def deploy(self, aid):
        a = self.r.get_agent(aid)
        if not a or aid not in self.controlled:
            raise link.LinkError("Unknown exported Agent")
        # One persistent shell has one owner. Pool employees keep their private PTY.
        return {"agent": aid, "terminal": "term0" if a.role == "primary" else "private PTY"}

    def close(self):
        for relation in {j["relation"] for j in self.work.values()}:
            self.cancel_owned(relation)


def start_service(runtime, session, deps, notice, registry=None):
    if link._service is not None:
        return link._service
    adapter = Adapter(runtime, session, deps, notice)
    transport = link.LocalTransport(paths.ROOT_HOME, paths.PROCESS_INSTANCE_ID, paths.ACCOUNT_USER_ID,
                                    lambda *args: service.dispatch(*args))
    service = link.Service(adapter, transport, paths.ACCOUNT_USER_ID)
    if registry is not None:
        def source():
            if not registry.agent_id or not registry._kernel_hooks_host:
                raise link.LinkError("Connect this CLI with /helpwo first")
            return "remote:" + registry.agent_id
        def remote_call(target, op, payload):
            source()
            result = registry._kernel_hooks_host.request({"t": "cli-term-request", "agentId": registry.agent_id,
                "targetId": target[7:], "operation": op, "payload": payload,
                "rpcId": uuid.uuid4().hex}, timeout=25)
            if not result.get("ok"):
                raise link.LinkError(result.get("error") or "Kernel refused terminal RPC")
            return result.get("result", {})
        transport.remote_call, transport.remote_source = remote_call, source
    link._service = service
    try:
        service.start()
    except BaseException:
        link._service = None
        transport.close()
        raise
    atexit.register(link.stop_service)
    raw = os.environ.pop("LAINTAS_TERM_INVITE", "")
    if raw:
        invite = json.loads(raw)
        if invite.get("target") != transport.ref:
            raise link.LinkError("Created-terminal identity mismatch")
        row = transport.record(invite["source"])
        service.invitations[invite["id"]] = {k: invite[k] for k in ("id", "token", "owner", "alias", "expires")}
        service.invitations[invite["id"]].update(parent=invite["source"], user_id=row.get("user_id", ""))
        def accept_created():
            deadline = time.monotonic() + 30
            while not service.stop.is_set() and time.monotonic() < deadline:
                try:
                    service.accept(invite["id"])
                    return
                except link.LinkError:
                    if service.stop.wait(.25):
                        return
            notice("Created terminal could not attach; inspect /term and retry acceptance or /term release.")
        threading.Thread(target=accept_created, name="term-created-accept", daemon=True).start()
    return service
