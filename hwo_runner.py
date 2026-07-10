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
import re
import threading
import time
import uuid
import json
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
    model: Optional[str] = None                 # optional #name@model# backend-model pin
    io: Optional[dict] = None                   # optional [in(...), out(...)] contract


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
            model=d.get("model"),
            io=d.get("io"),
            body=[_to_node(c) for c in d["body"]],
        )
    if t == "parallel":
        return HwoParallel(body=[_to_node(c) for c in d["body"]])
    raise HwoParseError(f"Unknown HWO node type: {t!r}", 0)


def parse_hwo(source: str) -> list:
    """Parse `source` into the local dataclass AST (executor + hwo_ui)."""
    return [_to_node(d) for d in parse_ast(source) if d.get("type") != "workflow"]


def summarize_steps(steps: list, indent: int = 0) -> list:
    pad = '  ' * indent
    lines = []
    for step in steps:
        if step.kind == 'task':
            lines.append(f"{pad}- task: {step.text.replace(chr(10), ' ')[:100]}")
        elif step.kind == 'agent':
            _model = f"@{step.model}" if step.model else ""
            _io = _format_io_summary(step.io) if step.io else ""
            lines.append(f"{pad}- agent: #{step.name}{_model}#{_io}")
            lines.extend(summarize_steps(step.body, indent + 1))
        elif step.kind == 'parallel':
            lines.append(f"{pad}- parallel:")
            lines.extend(summarize_steps(step.body, indent + 1))
    return lines


def _format_io_summary(io: Optional[dict]) -> str:
    if not io:
        return ""
    def names(kind: str) -> str:
        return ", ".join(p.get("name", "") for p in io.get(kind, [])) or "-"
    return f" [in({names('in')}), out({names('out')})]"


def _literal_value(raw: Optional[str]):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        if re.match(r"^-?\d+(?:\.\d+)?$", s):
            return float(s) if "." in s else int(s)
    except Exception:
        pass
    return s


def _resolve_input_ref(ref: str, workflow_inputs: dict, output_scope: dict, self_scope: dict):
    if ref.startswith("$input."):
        return workflow_inputs.get(ref[len("$input."):])
    if ref.startswith("$self."):
        return self_scope.get(ref[len("$self."):])
    m = re.match(r"^#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(?:\[-1\])?$", ref)
    if m:
        return (output_scope.get(m.group(1)) or {}).get(m.group(2))
    return _literal_value(ref)


def _build_agent_inputs(agent: HwoAgent, workflow_inputs: Optional[dict], output_scope: Optional[dict], self_scope: Optional[dict] = None) -> dict:
    values = {}
    workflow_inputs = workflow_inputs or {}
    output_scope = output_scope or {}
    self_scope = self_scope or {}
    if not agent.io:
        return values
    for p in agent.io.get("in", []):
        name = p.get("name", "")
        src = p.get("source") or p.get("default")
        if src:
            values[name] = _resolve_input_ref(src, workflow_inputs, output_scope, self_scope)
        elif name in workflow_inputs:
            values[name] = workflow_inputs.get(name)
        else:
            values[name] = None
    return values


def _workflow_default_inputs(ast: list, provided_inputs: Optional[dict] = None) -> dict:
    values = {}
    for item in ast:
        if item.get("type") != "workflow":
            continue
        for p in (item.get("io") or {}).get("in", []):
            if p.get("default") is not None:
                values[p.get("name", "")] = _literal_value(p.get("default"))
    values.update(provided_inputs or {})
    return values


def _workflow_input_types(ast: list) -> dict:
    values = {}
    for item in ast:
        if item.get("type") != "workflow":
            continue
        for p in (item.get("io") or {}).get("in", []):
            if p.get("name"):
                values[p.get("name")] = _short_io_type(p.get("type"))
    return values


def _describe_io_type(typ: Optional[str]) -> str:
    t = str(typ or "").strip()
    if not t:
        return "any JSON value"
    m = re.match(r"^enum\s*\((.*)\)$", t, re.I)
    if m:
        values = ", ".join(v.strip() for v in m.group(1).split(",") if v.strip())
        return f"one of: {values}"
    if re.match(r"^string$", t, re.I):
        return "JSON string"
    if re.match(r"^number$", t, re.I):
        return "JSON number"
    if re.match(r"^boolean$", t, re.I):
        return "JSON boolean"
    if re.match(r"^file$", t, re.I):
        return "file path string; create or reference a real file instead of returning large inline content"
    if re.match(r"^object$", t, re.I):
        return "JSON object"
    if re.match(r"^array$", t, re.I):
        return "JSON array"
    return t


def _type_guidance(io: dict) -> list:
    seen = {}
    for p in list(io.get("in", [])) + list(io.get("out", [])):
        typ = p.get("type")
        if typ:
            seen[str(typ)] = _describe_io_type(typ)
    if not seen:
        return []
    return ["Type rules:"] + [f"- {typ}: {rule}" for typ, rule in seen.items()]


def _short_io_type(typ: Optional[str]) -> str:
    return str(typ or "any").strip() or "any"


def _agent_self_types(agent: HwoAgent) -> dict:
    values = {}
    if not agent.io:
        return values
    for p in list(agent.io.get("in", [])) + list(agent.io.get("out", [])):
        if p.get("name"):
            values[p.get("name")] = _short_io_type(p.get("type"))
    return values


def _collect_agent_output_types(steps: list) -> dict:
    values = {}

    def walk(items: list):
        for item in items:
            if item.kind == "agent":
                own = {}
                if item.io:
                    for p in item.io.get("out", []):
                        if p.get("name"):
                            own[p.get("name")] = _short_io_type(p.get("type"))
                values[item.name] = own
                walk(item.body)
            elif item.kind == "parallel":
                walk(item.body)

    walk(steps)
    return values


def _format_var_value(value, typ: Optional[str] = None) -> str:
    if value is None or value == "":
        return "None"
    if re.match(r"^file$", str(typ or "").strip(), re.I):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _format_inline_value(value) -> str:
    if value is None or value == "":
        return "None"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _typed_inline_value(typ: Optional[str], value) -> str:
    return f"{_short_io_type(typ)}:{_format_inline_value(value)}"


def _embed_task_variables(text: str, ctx: "HwoCtx") -> str:
    output_scope = ctx.output_scope or {}

    def agent_ref(match):
        agent, output = match.group(1), match.group(2)
        value = (output_scope.get(agent) or {}).get(output)
        typ = ((ctx.agent_output_types or {}).get(agent) or {}).get(output, "any")
        return _typed_inline_value(typ, value)

    def input_ref(match):
        name = match.group(1)
        return _typed_inline_value((ctx.workflow_input_types or {}).get(name, "any"), (ctx.workflow_inputs or {}).get(name))

    def self_ref(match):
        name = match.group(1)
        return _typed_inline_value((ctx.self_types or {}).get(name, "any"), (ctx.self_scope or {}).get(name))

    text = re.sub(r"#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(?:\[-1\])?", agent_ref, text)
    text = re.sub(r"\$input\.([A-Za-z_][A-Za-z0-9_-]*)", input_ref, text)
    text = re.sub(r"\$self\.([A-Za-z_][A-Za-z0-9_-]*)", self_ref, text)
    return text


def _format_io_prompt(agent: HwoAgent, resolved_inputs: Optional[dict] = None) -> str:
    if not agent.io:
        return ""
    lines = []
    for p in agent.io.get("in", []):
        name = p.get("name", "")
        value = resolved_inputs.get(name) if resolved_inputs is not None and name in resolved_inputs else None
        lines.append(f"$self.{name} input({_short_io_type(p.get('type'))}):{_format_var_value(value, p.get('type'))}")
    for p in agent.io.get("out", []):
        value = _literal_value(p.get("default")) if p.get("default") is not None else None
        lines.append(f"$self.{p.get('name', '')} output({_short_io_type(p.get('type'))}):{_format_var_value(value, p.get('type'))}")
    lines.append("Use agent_send/agent_receive only for runtime coordination; mailbox messages are not structured outputs.")
    return "\n".join(lines)


def _parse_structured_return(value: str) -> Optional[dict]:
    raw = str(value or "").strip()
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _declared_outputs(agent: HwoAgent, value: str) -> dict:
    parsed = _parse_structured_return(value) or {}
    if not agent.io or not agent.io.get("out"):
        return parsed
    return {p.get("name"): parsed[p.get("name")] for p in agent.io.get("out", []) if p.get("name") in parsed}


def _declared_output_defaults(agent: HwoAgent) -> dict:
    if not agent.io or not agent.io.get("out"):
        return {}
    values = {}
    for p in agent.io.get("out", []):
        name = p.get("name")
        if name and p.get("default") is not None:
            values[name] = _literal_value(p.get("default"))
    return values


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


def _format_hwo_todo_goal(texts: list[str], ctx: "HwoCtx", inherited: str = "") -> str:
    embedded = [_embed_task_variables(text, ctx) for text in texts]
    lines = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(embedded))
    prompt = (
        "[HWO ORDERED TODO]\n"
        "Complete every workflow step below in order within this same agent run. Do not stop after the first item.\n"
        "Use task_create/task_update if available to track progress: mark one item in_progress, then done.\n"
        "Do not call agent_return after each item. Call agent_return only when declared output variables are ready.\n\n"
        f"{lines}"
    )
    if inherited:
        prompt += (
            f"\n\n[WORKFLOW CONTEXT — inherited from the parent agent and earlier workflow work]\n{inherited}\n"
            "[END CONTEXT]"
        )
    return prompt


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
    """Resolve a (prompt.md) prefix. Only relative paths are allowed; use ../ for parent directories."""
    try:
        raw = str(path or "").strip()
        if (
            not raw
            or raw.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", raw)
            or "://" in raw
        ):
            return ""
        return (Path.cwd() / raw).read_text(encoding="utf-8")
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
    model_override: Optional[str] = None       # #name@model# backend-model pin, inherited down the tree
    workflow_inputs: Optional[dict] = None     # structured inputs from hwo(...) or HWG
    workflow_input_types: Optional[dict] = None
    output_scope: Optional[dict] = None         # completed sibling outputs in the current sequence scope
    agent_output_types: Optional[dict] = None
    self_scope: Optional[dict] = None           # current agent's resolved inputs and submitted outputs
    self_types: Optional[dict] = None
    run_state: Optional[dict] = None
    run_id: Optional[str] = None
    workflow_tasks: dict = field(default_factory=dict)
    events_cb: Optional[object] = None
    event_lock: object = field(default_factory=threading.RLock)


# ── Execution ─────────────────────────────────────────────────────────────

def run_sequence(steps: list, ctx: HwoCtx) -> dict:
    """Execute steps serially with context inheritance.  Returns {ok, msg}."""
    import agent_loop as _al

    context = ctx.inherited_context
    outputs = []
    structured_outputs = {}
    agent_outputs = {}
    output_scope = ctx.output_scope if ctx.output_scope is not None else {}
    pending_tasks = []

    def flush_tasks():
        nonlocal context, pending_tasks
        if not pending_tasks:
            return None
        group = pending_tasks
        pending_tasks = []
        texts = [item.text for item in group]
        for item in group:
            _step_started(ctx, item)
        result = _run_task_group(texts, ctx, context)
        outputs.append(result.get("msg", ""))
        if not result.get("ok"):
            for item in group:
                _step_finished(ctx, item, False, {"error": result.get("msg", "")})
            return result
        for item in group:
            _step_finished(ctx, item, True)
        structured_outputs.update(result.get("outputs") or {})
        label = f"todo group: {len(group)} step{'s' if len(group) != 1 else ''}"
        context = _append_context(context, label, result.get("msg", ""))
        return None

    for step in steps:
        if ctx.abort_event and ctx.abort_event.is_set():
            return {"ok": False, "msg": "HWO run cancelled."}

        if step.kind == 'task':
            pending_tasks.append(step)
            continue
        elif step.kind == 'agent':
            task_failure = flush_tasks()
            if task_failure:
                return task_failure
            _step_started(ctx, step, {"agent": step.name})
            result = _run_agent(step, ctx, context)
        elif step.kind == 'parallel':
            task_failure = flush_tasks()
            if task_failure:
                return task_failure
            _step_started(ctx, step, {"agents": [a.name for a in step.body if a.kind == "agent"]})
            result = _run_parallel(step, ctx, context)
        else:
            continue

        outputs.append(result.get("msg", ""))
        if not result.get("ok"):
            _step_finished(ctx, step, False, {"error": result.get("msg", "")})
            _checkpoint(ctx, "step_failed", {"step": _step_label(step), "msg": result.get("msg", "")})
            return result
        structured_outputs.update(result.get("outputs") or {})
        if step.kind == "agent":
            own = result.get("outputs") or {}
            output_scope[step.name] = own
            agent_outputs[step.name] = own
        elif step.kind == "parallel":
            for name, own in (result.get("agent_outputs") or {}).items():
                output_scope[name] = own
                agent_outputs[name] = own
        context = _append_context(context, _step_label(step), result.get("msg", ""))
        _step_finished(ctx, step, True, {"outputs": result.get("outputs") or {}})
        _checkpoint(ctx, "step_finished", {"step": _step_label(step), "outputs": result.get("outputs") or {}})

    task_failure = flush_tasks()
    if task_failure:
        return task_failure

    return {
        "ok": True,
        "msg": "\n\n".join(filter(None, outputs)) or "HWO sequence completed.",
        "outputs": structured_outputs,
        "agent_outputs": agent_outputs,
    }


def _checkpoint(ctx: HwoCtx, label: str, payload: Optional[dict] = None) -> None:
    if not ctx.run_state:
        return
    try:
        import workflow_state
        with ctx.event_lock:
            ctx.run_state = workflow_state.checkpoint(ctx.run_state, label, payload or {})
    except Exception:
        pass


def _step_id(ctx: HwoCtx, step: HwoStep) -> Optional[str]:
    entry = ctx.workflow_tasks.get(id(step)) if ctx.workflow_tasks else None
    return entry.get("stepId") if isinstance(entry, dict) else None


def _emit(ctx: HwoCtx, event_type: str, payload: Optional[dict] = None) -> None:
    body = {"runId": ctx.run_id, "type": event_type, **(payload or {})}
    try:
        with ctx.event_lock:
            if ctx.run_state:
                import workflow_state
                ctx.run_state = workflow_state.emit(ctx.run_state, event_type, body)
            if callable(ctx.events_cb):
                ctx.events_cb([body])
    except Exception:
        # Progress reporting must never change workflow execution semantics.
        pass


def _forward_agent_events(ctx: HwoCtx, events) -> None:
    """Attach workflow identity to nested agent-loop events."""
    if not callable(ctx.events_cb) or not isinstance(events, list):
        return
    enriched = []
    for event in events:
        item = dict(event) if isinstance(event, dict) else {"content": str(event)}
        item.setdefault("runId", ctx.run_id)
        enriched.append(item)
    try:
        ctx.events_cb(enriched)
    except Exception:
        pass


def _update_step(ctx: HwoCtx, step: HwoStep, status: str,
                 progress: Optional[int] = None, notes: Optional[str] = None) -> None:
    entry = ctx.workflow_tasks.get(id(step)) if ctx.workflow_tasks else None
    if not isinstance(entry, dict):
        return
    try:
        import task_manager
        fields = {"status": status}
        if progress is not None:
            fields["progress"] = max(0, min(100, int(progress)))
        if notes:
            fields["notes"] = notes
        task_manager.update_task(entry["taskId"], cwd=entry.get("cwd"), **fields)
    except Exception:
        pass


def _step_started(ctx: HwoCtx, step: HwoStep, payload: Optional[dict] = None) -> None:
    _update_step(ctx, step, "in_progress", 0)
    _emit(ctx, "step_started", {"stepId": _step_id(ctx, step), **(payload or {})})


def _step_finished(ctx: HwoCtx, step: HwoStep, ok: bool,
                   payload: Optional[dict] = None) -> None:
    status = "completed" if ok else "blocked"
    _update_step(ctx, step, status, 100 if ok else None)
    _emit(ctx, "step_completed" if ok else "step_failed", {
        "stepId": _step_id(ctx, step), **(payload or {})
    })


def _prepare_workflow_tasks(steps: list, run_id: str, cwd: str) -> dict:
    """Create one durable task per executable HWO step before execution."""
    mapping = {}
    try:
        import task_manager
        counter = 0

        def walk(items, path, parent_id=None):
            nonlocal counter
            for index, step in enumerate(items):
                step_path = f"{path}.{index}" if path else str(index)
                if step.kind in ("task", "agent"):
                    counter += 1
                    title = step.text if step.kind == "task" else f"Agent #{step.name}#"
                    task = task_manager.create_task(
                        title.replace("\\n", " ")[:200],
                        metadata={"workflowRunId": run_id, "stepId": step_path,
                                  "kind": step.kind, "agent": getattr(step, "name", "")},
                        session_only=True, parent_task_id=parent_id, cwd=cwd)
                    mapping[id(step)] = {"taskId": task["id"], "stepId": step_path, "cwd": cwd}
                    child_parent = task["id"]
                else:
                    child_parent = parent_id
                if step.kind == "agent":
                    walk(step.body, step_path, child_parent)
                elif step.kind == "parallel":
                    walk(step.body, step_path, parent_id)
        walk(steps, "")
    except Exception:
        return {}
    return mapping


def _run_task_group(texts: list[str], ctx: HwoCtx, inherited: str = "") -> dict:
    """Execute one or more plain-text HWO steps as one ordered todo run."""
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
    if ctx.model_override:
        child.state['_model_override'] = ctx.model_override

    full_input = _format_hwo_todo_goal(texts, ctx, inherited)

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
                events_cb=lambda events: _forward_agent_events(ctx, events),
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
        first = texts[0] if texts else ""
        return {"ok": False, "msg": f"Task timed out: {first[:80]}"}
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

    # Model pin: this agent's own `@model` wins, else inherit the parent's pin.
    model_override = agent_step.model or ctx.model_override
    if model_override:
        child.state['_model_override'] = model_override

    team_manifest = generate_team_manifest(agent_step.name, ctx.workflow_manifest) if ctx.workflow_manifest else ""
    returned_outputs = {}
    parent_self_scope = ctx.self_scope or {}
    resolved_inputs = _build_agent_inputs(agent_step, ctx.workflow_inputs, ctx.output_scope, parent_self_scope)
    self_scope = {**resolved_inputs, **_declared_output_defaults(agent_step)}
    self_types = _agent_self_types(agent_step)
    io_prompt = _format_io_prompt(agent_step, resolved_inputs)
    body_parts = [p for p in (team_manifest, io_prompt, inherited) if p]
    body_context = "\n\n".join(body_parts)

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
                task_ctx = HwoCtx(
                    deps=ctx.deps,
                    session=ctx.session,
                    parent_id=child.id,
                    name_path=[*ctx.name_path, agent_step.name],
                    inherited_context=body_context,
                    abort_event=ctx.abort_event,
                    workflow_manifest=ctx.workflow_manifest,
                    prompt_override=prompt_override,
                    model_override=model_override,
                    workflow_inputs=ctx.workflow_inputs,
                    workflow_input_types=ctx.workflow_input_types,
                    output_scope=ctx.output_scope,
                    agent_output_types=ctx.agent_output_types,
                    self_scope=self_scope,
                    self_types=self_types,
                    run_state=ctx.run_state,
                    run_id=ctx.run_id,
                    workflow_tasks=ctx.workflow_tasks,
                    events_cb=ctx.events_cb,
                    event_lock=ctx.event_lock,
                )
                full_input = _format_hwo_todo_goal([step.text for step in agent_step.body], task_ctx, body_context)
                r = run_agent_loop(
                    ctx.deps, full_input, ctx.session, child.state, child.chat_history,
                    depth=child.depth, agent_id=child.id,
                    events_cb=lambda events: _forward_agent_events(ctx, events),
                )
                reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
                hwo_return = child.state.pop('_hwo_return', None)
                if hwo_return is not None:
                    returned_outputs.update(_declared_outputs(agent_step, hwo_return))
                    self_scope.update(returned_outputs)
                outputs = {
                    **_declared_output_defaults(agent_step),
                    **_declared_outputs(agent_step, reply),
                    **returned_outputs,
                }
                msg = (
                    "Stored outputs for "
                    f"#{agent_step.name}#: "
                    + ", ".join(f"$self.{k}" for k in outputs.keys())
                ) if outputs else (reply or "(done)")
                mark_agent_finished(child.id, result=msg)
                result_holder.update({"ok": True, "msg": msg, "outputs": outputs})
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
                    model_override=model_override,
                    workflow_inputs=ctx.workflow_inputs,
                    workflow_input_types=ctx.workflow_input_types,
                    output_scope={},
                    agent_output_types=ctx.agent_output_types,
                    self_scope=self_scope,
                    self_types=self_types,
                    run_state=ctx.run_state,
                    run_id=ctx.run_id,
                    workflow_tasks=ctx.workflow_tasks,
                    events_cb=ctx.events_cb,
                    event_lock=ctx.event_lock,
                )
                result = run_sequence(agent_step.body, sub_ctx)
                msg = result.get("msg", "")
                hwo_return = child.state.pop('_hwo_return', None)
                if hwo_return is not None:
                    returned_outputs.update(_declared_outputs(agent_step, hwo_return))
                    self_scope.update(returned_outputs)
                outputs = {
                    **_declared_output_defaults(agent_step),
                    **_declared_outputs(agent_step, msg),
                    **returned_outputs,
                }
                if outputs:
                    msg = (
                        "Stored outputs for "
                        f"#{agent_step.name}#: "
                        + ", ".join(f"$self.{k}" for k in outputs.keys())
                    )
                if result.get("ok"):
                    mark_agent_finished(child.id, result=msg)
                else:
                    mark_agent_finished(child.id, error=msg)
                result_holder.update({**result, "msg": msg, "outputs": {**(result.get("outputs") or {}), **outputs}})
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
    nested_outputs = result_holder.get("outputs") or {}
    agent_outputs = {k: v for k, v in nested_outputs.items() if ok}
    return {
        "ok": ok,
        "msg": f"#{full_name}# {'completed' if ok else 'failed'}\n{result_holder.get('msg', '')}",
        "outputs": nested_outputs,
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
    structured_outputs = {}
    agent_outputs = {}
    for r in results:
        if r:
            own = r.get("outputs") or {}
            structured_outputs.update(own)
    for i, r in enumerate(results):
        if r:
            agent_outputs[agents[i].name] = r.get("outputs") or {}
    return {
        "ok": ok,
        "msg": (
            f"HWO parallel block: {succeeded}/{len(agents)} succeeded\n\n"
            + "\n\n".join(msgs)
        ),
        "outputs": structured_outputs,
        "agent_outputs": agent_outputs,
    }


# ── Public entry point ────────────────────────────────────────────────────

def run_hwo_file(
    path: str,
    deps,
    session: dict,
    parent_id: Optional[str] = None,
    abort_event=None,
    caller_context: str = "",
    inputs: Optional[dict] = None,
    events_cb=None,
) -> dict:
    """Parse and execute a .hwo workflow file.

    Returns {"ok": bool, "msg": str, "summary": str}.
    """
    try:
        import workflow_state
        run_state = workflow_state.new_run("hwo", path, inputs or {})
        run_state = workflow_state.checkpoint(run_state, "run_started", {"path": path})
    except Exception:
        workflow_state = None
        run_state = None

    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        if run_state and workflow_state:
            run_state["status"] = "failed"
            workflow_state.checkpoint(run_state, "read_failed", {"error": str(e)})
        return {"ok": False, "msg": f"hwo: cannot read '{path}': {e}"}

    try:
        ast = parse_ast(source)
    except HwoParseError as e:
        if run_state and workflow_state:
            run_state["status"] = "failed"
            workflow_state.checkpoint(run_state, "parse_failed", {"error": str(e)})
        return {"ok": False, "msg": f"hwo: parse error — {e}"}

    errors = validate_ast(ast)
    if errors:
        if run_state and workflow_state:
            run_state["status"] = "failed"
            workflow_state.checkpoint(run_state, "validation_failed", {"errors": errors})
        return {"ok": False, "msg": "hwo: validation errors:\n" + "\n".join(errors)}

    steps = [_to_node(d) for d in ast if d.get("type") != "workflow"]
    effective_inputs = _workflow_default_inputs(ast, inputs)
    effective_input_types = _workflow_input_types(ast)
    agent_output_types = _collect_agent_output_types(steps)

    summary = "\n".join(summarize_steps(steps))
    manifest = build_workflow_manifest(steps)
    run_id = run_state.get("runId") if run_state else f"hwo-{uuid.uuid4().hex[:12]}"
    workflow_tasks = _prepare_workflow_tasks(steps, run_id, str(Path.cwd()))

    ctx = HwoCtx(
        deps=deps,
        session=session,
        parent_id=parent_id,
        inherited_context=caller_context,
        abort_event=abort_event,
        workflow_manifest=manifest,
        workflow_inputs=effective_inputs,
        workflow_input_types=effective_input_types,
        output_scope={},
        agent_output_types=agent_output_types,
        run_state=run_state,
        run_id=run_id,
        workflow_tasks=workflow_tasks,
        events_cb=events_cb,
    )
    _emit(ctx, "workflow_started", {"path": path, "stepCount": len(workflow_tasks)})
    result = run_sequence(steps, ctx)
    cancelled = bool(abort_event is not None and abort_event.is_set()) or bool(result.get("cancelled"))
    status = "cancelled" if cancelled else ("completed" if result.get("ok") else "failed")
    if run_state and workflow_state:
        run_state["status"] = status
        run_state["agentOutputs"] = result.get("agent_outputs") or {}
        run_state = workflow_state.checkpoint(run_state, f"run_{status}", {"outputs": result.get("outputs") or {}})
    _emit(ctx, "workflow_cancelled" if cancelled else ("workflow_completed" if result.get("ok") else "workflow_failed"), {
        "path": path, "status": status, "outputs": result.get("outputs") or {}
    })
    return {
        "ok": result.get("ok", False),
        "msg": f"HWO run {status}: {path}\n\n{summary}\n\n{result.get('msg', '')}",
        "summary": summary,
        "outputs": result.get("outputs") or {},
        "runId": run_state.get("runId") if run_state else None,
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

    steps = [_to_node(d) for d in ast if d.get("type") != "workflow"]

    summary = "\n".join(summarize_steps(steps))
    return {"ok": True, "msg": f"HWO compile OK\n{summary}", "summary": summary}


def status(run_id: Optional[str] = None) -> dict:
    """Return the latest durable HWO run state for local inspection."""
    import workflow_state
    if run_id:
        run = workflow_state.load_run(run_id)
        if not run or run.get("kind") != "hwo":
            return {"ok": False, "msg": f"HWO run not found: {run_id}"}
        return {"ok": True, "run": run, "msg": _format_status(run)}
    runs = [r for r in workflow_state.list_runs() if r.get("kind") == "hwo"]
    if not runs:
        return {"ok": True, "msg": "No HWO workflow runs."}
    return {"ok": True, "runs": runs, "msg": "\n".join(_format_status(r, True) for r in runs[:20])}


def _format_status(run: dict, one_line: bool = False) -> str:
    if one_line:
        return f"{run.get('runId')}  {run.get('status')}  {run.get('source')}  events={len(run.get('events') or [])}"
    return (
        f"Run: {run.get('runId')}\n"
        f"Status: {run.get('status')}\n"
        f"Source: {run.get('source')}\n"
        f"Events: {len(run.get('events') or [])}\n"
        f"Updated: {run.get('updatedAt')}"
    )
