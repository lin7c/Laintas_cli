"""HWO runner — parse and execute .hwo workflow files.

Mirrors hwo.ts from the Helpwo project:
  - HwoParser: ports the TypeScript parser verbatim
  - run_hwo_file(): entry point
  - run_sequence(): serial execution with context inheritance
  - run_task(): single step in the parent agent's context
  - run_agent(): spawn a child agent, wait for it (slot released while waiting)
  - run_parallel(): concurrent agents via threading, join all
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


# ── Data model (mirrors hwo.ts types) ────────────────────────────────────

@dataclass
class HwoTask:
    kind: str = "task"
    text: str = ""


@dataclass
class HwoAgent:
    kind: str = "agent"
    name: str = ""
    body: list = field(default_factory=list)   # list[HwoStep]
    prompt_file: Optional[str] = None           # optional (file.md) prefix override


@dataclass
class HwoParallel:
    kind: str = "parallel"
    body: list = field(default_factory=list)   # list[HwoAgent] only


HwoStep = Union[HwoTask, HwoAgent, HwoParallel]


# ── Parser + validation: shared grammar (vendored from agent_gateway/hwo) ──
# The HWO *language* (source -> AST + validation) is the single source of truth
# in agent_gateway/hwo, vendored here as hwo_adapter. We keep the dataclass AST
# above for the executor + hwo_ui, converting from the adapter's JSON AST.
from hwo_adapter import (  # noqa: E402
    HWO_COMM_TOOLS,
    HwoParseError,
    parse as parse_ast,
    validate as validate_ast,
)


def _to_node(d: dict) -> HwoStep:
    """Convert a shared JSON-AST node into the local dataclass model."""
    t = d["type"]
    if t == "task":
        return HwoTask(text=d["text"])
    if t == "agent":
        return HwoAgent(
            name=d["name"],
            prompt_file=d.get("promptFile"),
            body=[_to_node(c) for c in d["body"]],
        )
    if t == "parallel":
        return HwoParallel(body=[_to_node(c) for c in d["body"]])
    raise HwoParseError(f"Unknown HWO node type: {t!r}", 0)


def parse_hwo(source: str) -> list:
    """Parse `source` into the local dataclass AST (executor + hwo_ui)."""
    return [_to_node(d) for d in parse_ast(source)]


def summarize_steps(steps: list, indent: int = 0) -> list:
    pad = '  ' * indent
    lines = []
    for step in steps:
        if step.kind == 'task':
            lines.append(f"{pad}- task: {step.text.replace(chr(10), ' ')[:100]}")
        elif step.kind == 'agent':
            lines.append(f"{pad}- agent: #{step.name}#")
            lines.extend(summarize_steps(step.body, indent + 1))
        elif step.kind == 'parallel':
            lines.append(f"{pad}- parallel:")
            lines.extend(summarize_steps(step.body, indent + 1))
    return lines


# ── Context inheritance (mirrors appendContext in hwo.ts) ─────────────────

MAX_STEP_CONTEXT = 4000
MAX_STEP_OUTPUT_IN_CONTEXT = 1200


def _step_label(step: HwoStep) -> str:
    if step.kind == 'task':
        return step.text.replace('\n', ' ')[:80]
    if step.kind == 'agent':
        return f"#{step.name}#"
    return 'parallel block'


def _append_context(acc: str, label: str, output: str) -> str:
    capped = (output[:MAX_STEP_OUTPUT_IN_CONTEXT] + '…') if len(output) > MAX_STEP_OUTPUT_IN_CONTEXT else output
    joined = f"{acc}\n\n[DONE: {label}]\n{capped}" if acc else f"[DONE: {label}]\n{capped}"
    return ('…' + joined[-MAX_STEP_CONTEXT:]) if len(joined) > MAX_STEP_CONTEXT else joined


# ── Workflow manifest (mirrors buildWorkflowManifest/generateTeamManifest) ──
#
# Pre-scans the parsed AST once per `hwo run` so every spawned agent can be
# told its own identity and how to reach its teammates. Without this, agents
# are registered into the global registry with no idea any siblings exist.

def _collect_agent_names(items: list) -> list:
    names = []
    for item in items:
        if item.kind == 'agent':
            names.append(item.name)
        elif item.kind == 'parallel':
            for child in item.body:
                if child.kind == 'agent':
                    names.append(child.name)
    return names


def build_workflow_manifest(steps: list) -> dict:
    """Map every agent name to {name, prompt_file, parent_name, child_names, sibling_names}."""
    manifest: dict = {}

    def walk(items, parent_name, siblings):
        agents_in_scope = _collect_agent_names(items)
        for item in items:
            if item.kind == 'agent':
                sibling_names = [n for n in agents_in_scope if n != item.name]
                manifest[item.name] = {
                    "name": item.name,
                    "prompt_file": item.prompt_file,
                    "parent_name": parent_name,
                    "child_names": _collect_agent_names(item.body),
                    "sibling_names": sibling_names,
                }
                walk(item.body, item.name, sibling_names)
            elif item.kind == 'parallel':
                # Parallel members are siblings of each other, sharing the same parent
                parallel_names = _collect_agent_names(item.body)
                for child in item.body:
                    if child.kind == 'agent':
                        sib = [n for n in parallel_names if n != child.name]
                        manifest[child.name] = {
                            "name": child.name,
                            "prompt_file": child.prompt_file,
                            "parent_name": parent_name,
                            "child_names": _collect_agent_names(child.body),
                            "sibling_names": sib,
                        }
                        walk(child.body, child.name, sib)

    walk(steps, None, [])
    return manifest


def generate_team_manifest(agent_name: str, manifest: dict) -> str:
    """Per-agent note: who am I, who's my parent/siblings/children, how to message them."""
    node = manifest.get(agent_name)
    if not node:
        return ""

    def _tag(name: str) -> str:
        n = manifest.get(name)
        return f"#{name}# ({n['prompt_file']})" if n and n.get('prompt_file') else f"#{name}#"

    lines = ["[TEAM MANIFEST — members of this workflow]"]
    self_prompt = f" (prompt: {node['prompt_file']})" if node.get('prompt_file') else ""
    lines.append(f"You are #{agent_name}#{self_prompt}")

    if node.get('parent_name'):
        lines.append(f"Parent: {_tag(node['parent_name'])}")
    else:
        lines.append("You are the top-level agent in this workflow (no parent).")

    if node.get('sibling_names'):
        lines.append(f"Siblings: {', '.join(_tag(n) for n in node['sibling_names'])}")

    if node.get('child_names'):
        lines.append(f"Children: {', '.join(_tag(n) for n in node['child_names'])}")

    lines.append("")
    lines.append(HWO_COMM_TOOLS)

    return "\n".join(lines)


def _load_prompt_file(path: str) -> str:
    """Resolve a (prompt.md) prefix: relative to cwd, else treat as given."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / path
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


# ── HWO peer-to-peer mailbox ─────────────────────────────────────────────
#
# Deliberately NOT the general AgentInfo.inbox queue: that inbox is drained
# every iteration by run_agent_loop's own bookkeeping (to build {{inbox}} /
# {{parallelResults}}), so an agent-to-agent message sitting there could be
# silently consumed as generic inbox text before agent_receive ever sees it.
# A dedicated, name-keyed mailbox (mirrors hwo.ts's WorkflowMailbox) avoids
# that race entirely.

_hwo_mailboxes: dict = {}
_hwo_mailbox_lock = threading.Lock()


def _get_hwo_mailbox(name: str) -> "queue.Queue":
    with _hwo_mailbox_lock:
        q = _hwo_mailboxes.get(name)
        if q is None:
            q = queue.Queue(maxsize=200)
            _hwo_mailboxes[name] = q
        return q


def hwo_send(to: str, from_: str, text: str) -> bool:
    try:
        _get_hwo_mailbox(to).put_nowait({"from": from_, "text": text, "ts": time.time()})
        return True
    except queue.Full:
        return False


def hwo_receive(name: str, from_: Optional[str], timeout: float) -> Optional[dict]:
    """Block up to `timeout` seconds for a message from `from_` (or anyone if None)."""
    mailbox = _get_hwo_mailbox(name)
    deadline = time.time() + max(0.0, timeout)
    pending = []
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                msg = mailbox.get(timeout=min(remaining, 2.0))
            except queue.Empty:
                continue
            if from_ is None or msg.get("from") == from_:
                return msg
            pending.append(msg)
    finally:
        for m in pending:
            try:
                mailbox.put_nowait(m)
            except queue.Full:
                pass


# ── Execution context ─────────────────────────────────────────────────────

@dataclass
class HwoCtx:
    deps: object                        # LoopDeps from agent_loop
    session: dict
    parent_id: Optional[str] = None    # the owning agent (for waiting + child registration)
    name_path: list = field(default_factory=list)   # ancestor name path for naming
    inherited_context: str = ""        # parent's accumulated step context
    abort_event: Optional[object] = None
    workflow_manifest: Optional[dict] = None   # name -> {parent_name, sibling_names, child_names, prompt_file}
    prompt_override: Optional[str] = None      # resolved (prompt.md) content, inherited down the tree


# ── Execution ─────────────────────────────────────────────────────────────

def run_sequence(steps: list, ctx: HwoCtx) -> dict:
    """Execute steps serially with context inheritance.  Returns {ok, msg}."""
    import agent_loop as _al

    context = ctx.inherited_context
    outputs = []

    for step in steps:
        if ctx.abort_event and ctx.abort_event.is_set():
            return {"ok": False, "msg": "HWO run cancelled."}

        if step.kind == 'task':
            result = _run_task(step.text, ctx, context)
        elif step.kind == 'agent':
            result = _run_agent(step, ctx, context)
        elif step.kind == 'parallel':
            result = _run_parallel(step, ctx, context)
        else:
            continue

        outputs.append(result.get("msg", ""))
        if not result.get("ok"):
            return result
        context = _append_context(context, _step_label(step), result.get("msg", ""))

    return {
        "ok": True,
        "msg": "\n\n".join(filter(None, outputs)) or "HWO sequence completed.",
    }


def _run_task(text: str, ctx: HwoCtx, inherited: str = "") -> dict:
    """Execute a plain-text step as a sub-agent call in the parent's name context."""
    import agent_loop as _al
    from agent_loop import (
        register_agent, mark_agent_finished, enter_waiting, exit_waiting,
        run_agent_loop, schedule_agent, send_to_agent, can_spawn,
    )

    parent_id = ctx.parent_id
    if parent_id and not can_spawn(parent_id):
        return {"ok": False, "msg": "Cannot spawn task: max depth reached."}

    parent = _al.get_agent(parent_id) if parent_id else None
    depth = (parent.depth + 1) if parent else 0

    child = register_agent(
        name=None, depth=depth, parent_id=parent_id, role="subagent"
    )
    if ctx.prompt_override:
        child.state['_prompt_override'] = ctx.prompt_override

    spawn_ctx = (
        "[HWO] Execute this workflow step exactly. Do not reinterpret the workflow; "
        "the HWO runner controls ordering and parallelism."
    )
    if inherited:
        spawn_ctx += (
            f"\n\n[WORKFLOW CONTEXT — earlier step results]\n{inherited}\n"
            "[END CONTEXT] Your goal is ONLY the step text below."
        )
    # NOTE: spawn_ctx used to be built and then discarded — run_agent_loop was
    # called with bare `text`, so the [HWO] note and inherited context never
    # reached the model. Fixed: fold it into the actual input.
    full_input = f"{spawn_ctx}\n\n[STEP TEXT]\n{text}"

    done_event = threading.Event()
    result_holder = {}

    def _runner(ok: bool):
        if not ok:
            result_holder.update({"ok": False, "msg": "Task cancelled while queued."})
            done_event.set()
            return
        try:
            r = run_agent_loop(
                ctx.deps, full_input, ctx.session, child.state, child.chat_history,
                depth=child.depth, agent_id=child.id,
            )
            reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
            hwo_return = child.state.pop('_hwo_return', None)
            if hwo_return is not None:
                reply = hwo_return
            mark_agent_finished(child.id, result=reply)
            result_holder.update({"ok": True, "msg": reply or "(done)"})
        except Exception as e:
            mark_agent_finished(child.id, error=repr(e))
            result_holder.update({"ok": False, "msg": repr(e)})
        finally:
            done_event.set()

    if parent_id:
        enter_waiting(parent_id)
    try:
        t = threading.Thread(
            target=lambda: schedule_agent(child.id, _runner),
            daemon=True, name=f"hwo-task-{child.id}",
        )
        t.start()
        done_event.wait(timeout=300)
    finally:
        if parent_id:
            exit_waiting(parent_id)

    if not result_holder:
        mark_agent_finished(child.id, error="timeout")
        return {"ok": False, "msg": f"Task timed out: {text[:80]}"}
    return result_holder


def _run_agent(agent_step: HwoAgent, ctx: HwoCtx, inherited: str = "") -> dict:
    """Spawn a child agent, run its body, wait for it.

    Identity matters here: `register_agent(name=agent_step.name, ...)` makes
    this agent's registry id EQUAL to its declared name (e.g. "alice"), which
    is what makes it addressable by agent_send/agent_receive. If the body
    were handed off to the generic run_sequence -> _run_task path, each plain
    task line would spawn its OWN anonymous grandchild ("AI-7") to actually
    run the LLM loop — and THAT anonymous id, not "alice", would be the one
    calling tools, breaking name-based addressing. So when the body is flat
    (task lines only, no nested agents/parallel — the common case), we run
    those steps directly under this agent's own identity instead of
    recursing. Bodies that DO contain nested structure still recurse via
    run_sequence, where each nested #agent# gets its own proper identity.
    """
    import agent_loop as _al
    from agent_loop import (
        register_agent, mark_agent_finished, enter_waiting, exit_waiting,
        run_agent_loop, schedule_agent, can_spawn,
    )

    parent_id = ctx.parent_id
    if parent_id and not can_spawn(parent_id):
        return {"ok": False, "msg": f"Cannot spawn #{agent_step.name}#: max depth reached."}

    parent = _al.get_agent(parent_id) if parent_id else None
    depth = (parent.depth + 1) if parent else 0
    full_name = "/".join([*ctx.name_path, agent_step.name])

    child = register_agent(
        name=agent_step.name, depth=depth, parent_id=parent_id, role="subagent"
    )

    # Resolve this agent's system prompt override: its own (file) prefix wins,
    # else it inherits whatever override (if any) its parent was running under.
    prompt_override = ctx.prompt_override
    if agent_step.prompt_file:
        loaded = _load_prompt_file(agent_step.prompt_file)
        if loaded.strip():
            prompt_override = loaded
    if prompt_override:
        child.state['_prompt_override'] = prompt_override

    team_manifest = generate_team_manifest(agent_step.name, ctx.workflow_manifest) if ctx.workflow_manifest else ""
    body_context = f"{team_manifest}\n\n{inherited}" if (team_manifest and inherited) else (team_manifest or inherited)

    done_event = threading.Event()
    result_holder = {}

    only_tasks = bool(agent_step.body) and all(s.kind == 'task' for s in agent_step.body)

    def _runner(ok: bool):
        if not ok:
            result_holder.update({"ok": False, "msg": f"#{full_name}# cancelled while queued."})
            done_event.set()
            return
        try:
            if only_tasks:
                context = body_context
                reply = ""
                for step in agent_step.body:
                    spawn_ctx = (
                        "[HWO] Execute this workflow step exactly. Do not reinterpret the "
                        "workflow; the HWO runner controls ordering and parallelism."
                    )
                    if context:
                        spawn_ctx += (
                            f"\n\n[WORKFLOW CONTEXT — earlier step results]\n{context}\n"
                            "[END CONTEXT] Your goal is ONLY the step text below."
                        )
                    full_input = f"{spawn_ctx}\n\n[STEP TEXT]\n{step.text}"
                    r = run_agent_loop(
                        ctx.deps, full_input, ctx.session, child.state, child.chat_history,
                        depth=child.depth, agent_id=child.id,
                    )
                    reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
                    hwo_return = child.state.pop('_hwo_return', None)
                    if hwo_return is not None:
                        reply = hwo_return
                        break
                    context = _append_context(context, step.text.replace('\n', ' ')[:80], reply or "")
                msg = reply or "(done)"
                mark_agent_finished(child.id, result=msg)
                result_holder.update({"ok": True, "msg": msg})
            else:
                sub_ctx = HwoCtx(
                    deps=ctx.deps,
                    session=ctx.session,
                    parent_id=child.id,
                    name_path=[*ctx.name_path, agent_step.name],
                    inherited_context=body_context,
                    abort_event=ctx.abort_event,
                    workflow_manifest=ctx.workflow_manifest,
                    prompt_override=prompt_override,
                )
                result = run_sequence(agent_step.body, sub_ctx)
                msg = result.get("msg", "")
                hwo_return = child.state.pop('_hwo_return', None)
                if hwo_return is not None:
                    msg = hwo_return
                if result.get("ok"):
                    mark_agent_finished(child.id, result=msg)
                else:
                    mark_agent_finished(child.id, error=msg)
                result_holder.update({**result, "msg": msg})
        except Exception as e:
            mark_agent_finished(child.id, error=repr(e))
            result_holder.update({"ok": False, "msg": repr(e)})
        finally:
            done_event.set()

    if parent_id:
        enter_waiting(parent_id)
    try:
        t = threading.Thread(
            target=lambda: schedule_agent(child.id, _runner),
            daemon=True, name=f"hwo-agent-{child.id}",
        )
        t.start()
        done_event.wait(timeout=600)
    finally:
        if parent_id:
            exit_waiting(parent_id)

    if not result_holder:
        mark_agent_finished(child.id, error="timeout")
        return {"ok": False, "msg": f"#{full_name}# timed out."}

    ok = result_holder.get("ok", False)
    return {
        "ok": ok,
        "msg": f"#{full_name}# {'completed' if ok else 'failed'}\n{result_holder.get('msg', '')}",
    }


def _run_parallel(block: HwoParallel, ctx: HwoCtx, inherited: str = "") -> dict:
    """Run all agents in a // block concurrently; wait for all to finish."""
    agents = [s for s in block.body if s.kind == 'agent']
    if not agents:
        return {"ok": True, "msg": "HWO: empty parallel block skipped."}
    if len(agents) > 6:
        return {"ok": False, "msg": "HWO: max 6 agents per // block."}

    # Each member gets the same inherited context snapshot
    results = [None] * len(agents)
    threads = []

    def _run_one(idx: int, agent_step: HwoAgent):
        results[idx] = _run_agent(agent_step, ctx, inherited)

    for i, a in enumerate(agents):
        t = threading.Thread(target=_run_one, args=(i, a), daemon=True,
                             name=f"hwo-par-{i}-{a.name}")
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=600)

    ok = all(r and r.get("ok") for r in results)
    succeeded = sum(1 for r in results if r and r.get("ok"))
    msgs = [r.get("msg", "") if r else "(timeout)" for r in results]
    return {
        "ok": ok,
        "msg": (
            f"HWO parallel block: {succeeded}/{len(agents)} succeeded\n\n"
            + "\n\n".join(msgs)
        ),
    }


# ── Public entry point ────────────────────────────────────────────────────

def run_hwo_file(
    path: str,
    deps,
    session: dict,
    parent_id: Optional[str] = None,
    abort_event=None,
    caller_context: str = "",
) -> dict:
    """Parse and execute a .hwo workflow file.

    Returns {"ok": bool, "msg": str, "summary": str}.
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "msg": f"hwo: cannot read '{path}': {e}"}

    try:
        ast = parse_ast(source)
    except HwoParseError as e:
        return {"ok": False, "msg": f"hwo: parse error — {e}"}

    errors = validate_ast(ast)
    if errors:
        return {"ok": False, "msg": "hwo: validation errors:\n" + "\n".join(errors)}

    steps = [_to_node(d) for d in ast]

    summary = "\n".join(summarize_steps(steps))
    manifest = build_workflow_manifest(steps)

    ctx = HwoCtx(
        deps=deps,
        session=session,
        parent_id=parent_id,
        inherited_context=caller_context,
        abort_event=abort_event,
        workflow_manifest=manifest,
    )
    result = run_sequence(steps, ctx)
    status = "completed" if result.get("ok") else "failed"
    return {
        "ok": result.get("ok", False),
        "msg": f"HWO run {status}: {path}\n\n{summary}\n\n{result.get('msg', '')}",
        "summary": summary,
    }


def compile_hwo_file(path: str) -> dict:
    """Parse and validate a .hwo file without executing it."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "msg": f"hwo: cannot read '{path}': {e}"}

    try:
        ast = parse_ast(source)
    except HwoParseError as e:
        return {"ok": False, "msg": f"hwo: parse error — {e}"}

    errors = validate_ast(ast)
    if errors:
        return {"ok": False, "msg": "hwo: validation errors:\n" + "\n".join(errors)}

    steps = [_to_node(d) for d in ast]

    summary = "\n".join(summarize_steps(steps))
    return {"ok": True, "msg": f"HWO compile OK\n{summary}", "summary": summary}
