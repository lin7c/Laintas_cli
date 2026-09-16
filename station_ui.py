"""Station presentation; all mutations go through StationService."""
import re
import shlex
import uuid

import resource_ui as ui
import agent_ui_events
from agent_router import RouteRequest


def show_station(service, *, owner_id, deps, session, create_terminal,
                 events_cb=None, initial_key=""):
    mode = ["terminals" if initial_key.startswith("terminal:") else "agents"]
    close_armed = [None]

    def load_items():
        agents, terminals = service.snapshot()
        rows = []
        by_id = {a.id: a for a in agents}
        if mode[0] == "agents":
            seen = set()

            def emit(a, depth):
                if a.id in seen:
                    return
                seen.add(a.id)
                rows.append(ui.UIItem(
                    key=f"agent:{a.id}", title="  " * min(depth, 8) + a.name,
                    subtitle=f"{a.role} · {a.deployment or 'not stationed'} · {a.task[:100]}",
                    status=a.status, badge="AGENT", payload=a,
                    search_text=f"{a.id} {a.parent_id} {a.home} {a.task}",
                    status_style="class:error" if a.status in {"error", "aborted"} else "class:success"))
                for child in agents:
                    if child.parent_id == a.id:
                        emit(child, depth + 1)

            for a in agents:
                if a.parent_id not in by_id:
                    emit(a, 0)
            for a in agents:  # malformed legacy cycles remain inspectable
                emit(a, 0)
        else:
            for t in terminals:
                rows.append(ui.UIItem(
                    key=f"terminal:{t.name}", title=t.name,
                    subtitle=f"Parent: {t.parent or 'root'} · Owner: {t.owner or 'none'}",
                    status="alive" if t.alive else "ended", badge="TERM", payload=t))
            for a in agents:
                if not a.deployment:
                    rows.append(ui.UIItem(
                        key=f"agent:{a.id}", title=a.name,
                        subtitle=f"Not stationed · task terminal: {a.task_terminal or 'none'}",
                        status=a.status, badge="AGENT", payload=a))
        return rows

    def load_detail(item):
        obj = item.payload
        if item.key.startswith("terminal:"):
            return ui.UIDetail.text(obj.name,
                f"Parent: {obj.parent or 'root'}\nOwner: {obj.owner or 'none'}\n"
                f"State: {'alive' if obj.alive else 'ended'}\n"
                "Commands: close (shows affected resources), close confirm\n\n"
                f"{obj.output or '(no output)'}",
                "Persistent terminal · observed output only")
        return ui.UIDetail.text(obj.name, "\n".join([
            f"Agent: {obj.id} · {obj.role} · {obj.status}",
            f"Parent: {obj.parent_id or 'root'}", f"Home: {obj.home or 'none'}",
            f"Deployment: {obj.deployment or 'none'}",
            f"Task terminal: {obj.task_terminal or 'none'}",
            f"Run: {obj.run_id or 'none'}", f"Stage: {obj.stage}",
            f"Queue position: {obj.queue_position or 'not queued'}",
            f"Model: {obj.model}", f"Tools: {obj.tools}",
            "", "Task", obj.task or "(no active task)", "", "Routing",
            obj.reason or "Explicit assignment / existing runtime", "", "Latest result",
            obj.result or "(no result yet)", "", "Commands (press a)",
            "task <work>          Assign selected employee",
            "auto <work>          Create an isolated child automatically",
            "suggest <work>       Preview automatic routing",
            "bind <terminal>      Station selected employee",
            "cancel               Cancel selected task",
            "unstation            Release selected deployment",
            "", "Recent activity",
            *[f"{event.event_type}: {event.summary}" for event in
              agent_ui_events.hub.agent_events(obj.id)[-12:]],
        ]), "Delegation and terminal ownership are separate")

    def refresh_result(result):
        return ui.UIActionResult(message=result.message, refresh=True,
                                 message_style="class:success" if result.ok else "class:error")

    def toggle(_item):
        mode[0] = "terminals" if mode[0] == "agents" else "agents"
        return ui.UIActionResult(message=f"View: {mode[0]}", refresh=True)

    def _deploy_key_terminal(item):
        # One-keystroke deploy targets a terminal named after the agent;
        # "bind <terminal>" remains the way to pick a specific terminal.
        agent = item.payload
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", agent.name or agent.id).strip("-")
        return (name or agent.id)[:64]

    def deploy_selected(item):
        if item is None or not item.key.startswith("agent:"):
            return ui.UIActionResult(message="Select an agent to deploy.",
                                     message_style="class:warning")
        return refresh_result(service.deploy(item.payload.id, _deploy_key_terminal(item),
                                             owner_id=owner_id, create_terminal=create_terminal))

    def undeploy_selected(item):
        if item is None or not item.key.startswith("agent:"):
            return ui.UIActionResult(message="Select an agent to unstation.",
                                     message_style="class:warning")
        return refresh_result(service.undeploy(item.payload.id, owner_id))

    def command(item, detail, text, interrupt):
        if interrupt.is_set():
            return ui.UIActionResult(message="Cancelled.")
        verb, _, tail = text.strip().partition(" ")
        verb = verb.lower()
        selected = item.payload if item and item.key.startswith("agent:") else None
        if verb == "close" and item and item.key.startswith("terminal:"):
            name = item.payload.name
            if name == "term0":
                return ui.UIActionResult(message="The primary terminal belongs to the CLI; use /exit.",
                                         message_style="class:warning")
            impact = service.terminal_impact(name)
            identity = (name, item.payload.created_at, impact)
            if tail != "confirm" or close_armed[0] != identity:
                close_armed[0] = identity
                affected = [t[0] for t in impact[0]]
                people = [a[0] for a in impact[1]]
                return ui.UIActionResult(message=f"Closes terminals {', '.join(affected)}; "
                    f"affected agents: {', '.join(people) or 'none'}. Enter close confirm to proceed.",
                    message_style="class:warning")
            close_armed[0] = None
            return refresh_result(service.close_terminal(name, owner_id,
                expected_created_at=item.payload.created_at, expected_impact=impact))
        if verb in {"auto", "suggest", "task"}:
            if not tail.strip():
                return ui.UIActionResult(message="Enter a task after the command.", message_style="class:warning")
            if verb == "task" and selected is None:
                return ui.UIActionResult(message="Select an employee first.", message_style="class:warning")
            request = RouteRequest(owner_id, tail.strip(), uuid.uuid4().hex,
                                   target_id=selected.id if verb == "task" else "")
            if verb == "suggest":
                decision = service.preview(request)
                return ui.UIActionResult(message=f"{decision.action}: {decision.reason}")
            return refresh_result(service.assign(request, deps, session=session, events_cb=events_cb))
        if selected is None:
            return ui.UIActionResult(message="Select an agent first.", message_style="class:warning")
        if verb == "bind":
            args = shlex.split(tail)
            if len(args) != 1:
                return ui.UIActionResult(message="Usage: bind <terminal>", message_style="class:warning")
            return refresh_result(service.deploy(selected.id, args[0], owner_id=owner_id,
                                                  create_terminal=create_terminal))
        if verb == "cancel" and not tail:
            return refresh_result(service.cancel(selected.id, selected.run_id, owner_id))
        if verb == "unstation" and not tail:
            return refresh_result(service.undeploy(selected.id, owner_id))
        return ui.UIActionResult(message="Use task, auto, suggest, bind, cancel or unstation.",
                                 message_style="class:warning")

    return ui.ResourceBrowser(
        title="Station", load_items=load_items, load_detail=load_detail,
        actions=[ui.UIAction("v", "view_mode", "Agents / terminals", toggle, allow_empty=True),
                 ui.UIAction("e", "open", "Open terminal / dialogue"),
                 ui.UIAction("s", "deploy", "Station to own terminal", deploy_selected),
                 ui.UIAction("u", "undeploy", "Release deployment", undeploy_selected)],
        primary_action="view", refresh_interval=.5, presentation="operations",
        pane_labels=("RELATIONSHIPS", "TASK & ACTIVITY"),
        empty_message="No resources. Use /hire or /term to create one.",
        assistant_handler=command,
        assistant_placeholder="task / auto / suggest / bind / cancel / unstation  ·  s deploy / u unstation",
        assistant_label="Command",
        initial_key=initial_key,
    ).run()
