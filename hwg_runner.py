"""HWG runner for laintas_cli.

Executes the shared HWG graph DSL by running each node's bound HWO file through
``hwo_runner``. The grammar remains in ``hwg_adapter``; this module owns CLI
runtime semantics: durable run state, resume, retry, timeout and cache.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import hwo_runner
import workflow_state
from hwg_adapter import HwgParseError, as_graph, parse as parse_hwg, validate as validate_hwg
from hwg_adapter.adapter import fanout_joins, parse_condition, resolve_includes


MAX_GRAPH_STEPS = 200


def _prepare_node_tasks(run: dict, nodes: list[dict], cwd: str,
                        owner_agent_id: str = None) -> dict:
    """Create one stable Todo per HWG node and persist the mapping in the run."""
    mapping = dict(run.get("nodeTasks") or {})
    try:
        import task_manager
        for node in nodes:
            node_id = str(node.get("id"))
            if node_id in mapping:
                continue
            task = task_manager.create_task(
                f"HWG node #{node_id}# ({node.get('file', '')})"[:200],
                metadata={
                    "workflowRunId": run.get("runId"),
                    "scopeType": "hwg-run",
                    "nodeId": node_id,
                    "kind": "hwg-node",
                    "file": node.get("file", ""),
                },
                session_only=False,
                cwd=cwd,
                owner_agent_id=owner_agent_id,
            )
            mapping[node_id] = {
                "workId": task["work_id"], "taskId": task["id"],
                "cwd": cwd,
            }
    except Exception:
        return mapping
    run["nodeTasks"] = mapping
    return mapping


def _update_node_task(run: dict, node_id: str, status: str,
                      progress: int = None, notes: str = None) -> None:
    entry = (run.get("nodeTasks") or {}).get(str(node_id))
    if not isinstance(entry, dict):
        return
    try:
        import task_manager
        import workgraph
        fields = {"status": status}
        if progress is not None:
            fields["progress"] = max(0, min(100, int(progress)))
        if notes:
            fields["notes"] = notes[:400]
        if entry.get("workId"):
            workgraph.update_step(
                entry["workId"], entry["taskId"],
                cwd=entry.get("cwd"), **fields)
        else:  # resume mappings created before durable work ids were recorded
            task_manager.update_task(
                entry["taskId"], cwd=entry.get("cwd"), **fields)
    except Exception:
        pass


def _duration_seconds(value) -> Optional[float]:
    if value in (None, "", False):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().strip('"').strip("'")
    m = re.match(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$", s, re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    return n / 1000 if unit == "ms" else n * 60 if unit == "m" else n * 3600 if unit == "h" else n


def _literal_value(raw: str):
    s = str(raw or "").strip()
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


def _resolve_ref(ref: str, graph_inputs: dict, node_outputs: dict, node_output_history: Optional[dict] = None,
                 loop: Optional[dict] = None):
    if ref.startswith("$input."):
        return graph_inputs.get(ref[len("$input."):])
    if ref.startswith("$loop."):
        return (loop or {}).get(ref[len("$loop."):])
    m = re.match(r"^#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)(\[-1\])?#$", ref)
    if m:
        node_id, field, previous = m.group(1), m.group(2), bool(m.group(3))
        if previous:
            history = (node_output_history or {}).get(node_id) or []
            return (history[-1] if history else {}).get(field)
        return (node_outputs.get(node_id) or {}).get(field)
    return _literal_value(ref)


def _build_node_inputs(node: dict, graph_inputs: dict, node_outputs: dict, node_output_history: Optional[dict] = None) -> dict:
    # $loop.count is 1 on a node's first execution and increments every time the
    # graph routes back to it, so a looping node can tell attempt 1 from attempt 3
    # (history is appended after each run, so its length is the count so far).
    history = (node_output_history or {}).get(node.get("id")) or []
    loop = {"count": len(history) + 1}
    values = {}
    for p in (node.get("io") or {}).get("in", []):
        src = p.get("source") or p.get("default")
        values[p.get("name", "")] = _resolve_ref(src, graph_inputs, node_outputs, node_output_history, loop) if src else None
    return values


def _missing_graph_inputs(statements: list, inputs: dict) -> list:
    """Graph inputs declared in @graph in(...) but not supplied at launch.
    Without this they silently resolve to None inside every node."""
    graph = next((s for s in statements if s.get("type") == "graph"), None)
    if not graph:
        return []
    missing = []
    for p in ((graph.get("io") or {}).get("in") or []):
        name = p.get("name")
        if not name or p.get("optional") or p.get("default") is not None:
            continue
        if inputs.get(name) in (None, ""):
            missing.append(name)
    return missing


def _declared_node_outputs(node: dict, raw: dict) -> dict:
    specs = (node.get("io") or {}).get("out", [])
    if not specs:
        return raw
    return {p.get("name"): raw[p.get("name")] for p in specs if p.get("name") in raw}


def _parse_structured_return(text: str) -> dict:
    raw = str(text or "").strip()
    candidates = [raw]
    returns = list(re.finditer(r"\[RETURN\s+#[^#]+#\]:\s*(.+?)(?:\r?\n|$)", raw))
    if returns:
        candidates.insert(0, returns[-1].group(1).strip())
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def _explicit_verdict(output: str, outputs: dict) -> Optional[str]:
    """The verdict the node actually stated, or None if it stated none."""
    if outputs.get("verdict") not in (None, ""):
        return str(outputs["verdict"]).upper()
    returns = list(re.finditer(r"\[RETURN\s+#[^#]+#\]:\s*(.+?)(?:\r?\n|$)", output or ""))
    if returns:
        raw = returns[-1].group(1).strip().strip('"').strip("'")
        if raw and not raw.startswith("{"):
            return raw.upper()
    m = re.search(r"#RESULT:\s*(\S+?)#", output or "", re.I)
    if m:
        return m.group(1).upper()
    return None


def _extract_verdict(output: str, ok: bool, outputs: dict) -> str:
    """Verdict for routing. Nodes that declare out(...) are held to their
    contract by _contract_violation before this fallback is ever reached."""
    explicit = _explicit_verdict(output, outputs)
    if explicit is not None:
        return explicit
    return "PASS" if ok else "FAIL"


# Outputs the runtime can synthesise; every other declared output must be
# returned by the node itself.
_PROTOCOL_OUTPUTS = ("verdict",)


def _declared_out_names(node: dict) -> list:
    return [p.get("name") for p in ((node.get("io") or {}).get("out") or []) if p.get("name")]


def _output_contract_errors(node: dict, msg: str, raw: dict) -> list:
    """A node that declares out(...) must actually return it. Staying silent is
    a protocol error, not a PASS."""
    declared = _declared_out_names(node)
    if not declared:
        return []
    errors = []
    missing = [n for n in declared
               if n not in _PROTOCOL_OUTPUTS and raw.get(n) in (None, "")]
    if missing:
        errors.append("missing declared output(s): " + ", ".join(missing))
    if "verdict" in declared and _explicit_verdict(msg, raw) is None:
        errors.append("declared out(verdict) but returned no verdict")
    return errors


def _contract_violation(node: dict, result: dict) -> Optional[dict]:
    """Turn a "finished cleanly but returned nothing" result into an explicit
    failure. Returns None when the node honoured its contract."""
    msg = str(result.get("msg", ""))
    raw = _parse_structured_return(msg)
    raw.update(result.get("outputs") or {})
    errors = _output_contract_errors(node, msg, raw)
    if not errors:
        return None
    return {
        **result,
        "ok": False,
        "contractError": True,
        "msg": (
            f"HWG node #{node['id']}# ({node.get('file', '')}) finished without honouring "
            f"its output contract: {'; '.join(errors)}.\n"
            f"Declared: out({', '.join(_declared_out_names(node))}). The node's last ordered "
            f"step must be agent_return({{...}}) carrying every declared output.\n\n"
            f"--- node output ---\n{msg}"
        ),
    }


def _coerce_compare(left, right):
    """Coerce two values for ordering comparison. Returns (a, b) as floats if
    both are numeric, as strings if both are non-numeric, or None if
    incomparable (e.g. None vs int)."""
    if left is None or right is None:
        return None
    try:
        return float(left), float(right)
    except (ValueError, TypeError):
        pass
    return str(left), str(right)


def _edge_matches(edge: dict, verdict: str, outputs: dict, ctx: Optional[dict] = None) -> bool:
    """Does this edge fire?

    Conditions the shared grammar understands (including and/or/not and
    exists(path)) are evaluated structurally; anything else falls through to
    _legacy_edge_matches, which is the original single-atom behaviour kept
    verbatim so no pre-existing graph changes meaning.
    """
    on = (edge.get("on") or "").strip()
    if not on:
        return True
    parsed = parse_condition(on)
    if parsed is not None:
        return _eval_condition(parsed, verdict, outputs, ctx or {})
    return _legacy_edge_matches(on, verdict, outputs)


def _eval_condition(node: dict, verdict: str, outputs: dict, ctx: dict) -> bool:
    kind = node.get("kind")
    if kind == "and":
        return all(_eval_condition(i, verdict, outputs, ctx) for i in node.get("items", []))
    if kind == "or":
        return any(_eval_condition(i, verdict, outputs, ctx) for i in node.get("items", []))
    if kind == "not":
        return not _eval_condition(node.get("item") or {}, verdict, outputs, ctx)
    if kind == "exists":
        return _path_exists(node.get("path", ""), ctx)
    if kind == "cmp":
        return _compare(outputs.get(node["field"]), node["op"], _literal_value(node["value"]))
    if kind == "in":
        left = str(outputs.get(node["field"], "")).upper()
        return left in [str(_literal_value(v)).upper() for v in node.get("values", [])]
    if kind == "verdict":
        return node.get("value", "").upper() == verdict
    return False


def _compare(left, op_str: str, right) -> bool:
    if op_str in ("==", "!="):
        equal = str(left).upper() == str(right).upper()
        return equal if op_str == "==" else not equal
    coerced = _coerce_compare(left, right)
    if coerced is None:
        return False
    a, b = coerced
    try:
        if op_str == ">":
            return a > b
        if op_str == "<":
            return a < b
        if op_str == ">=":
            return a >= b
        if op_str == "<=":
            return a <= b
    except TypeError:
        return False
    return False


_PATH_NODE_REF_RE = re.compile(r"#([A-Za-z_][A-Za-z0-9_-]*)\.([A-Za-z_][A-Za-z0-9_-]*)#")
_PATH_INPUT_REF_RE = re.compile(r"\$input\.([A-Za-z_][A-Za-z0-9_-]*)")


def _path_exists(raw: str, ctx: dict) -> bool:
    """exists(path) — the one condition the graph can check for itself.

    $input.x and #node.field# inside the path are substituted first; an
    unresolved reference makes the whole predicate False rather than testing a
    half-built path. Relative paths resolve against the run's working directory.
    """
    inputs = ctx.get("inputs") or {}
    outputs = ctx.get("nodeOutputs") or {}
    unresolved = []

    def sub_node(m):
        value = (outputs.get(m.group(1)) or {}).get(m.group(2))
        if value in (None, ""):
            unresolved.append(m.group(0))
            return ""
        return str(value)

    def sub_input(m):
        value = inputs.get(m.group(1))
        if value in (None, ""):
            unresolved.append(m.group(0))
            return ""
        return str(value)

    resolved = _PATH_INPUT_REF_RE.sub(sub_input, _PATH_NODE_REF_RE.sub(sub_node, raw)).strip()
    if unresolved or not resolved:
        return False
    try:
        path = Path(resolved).expanduser()
        if not path.is_absolute():
            path = Path(ctx.get("cwd") or Path.cwd()) / path
        return path.exists()
    except (OSError, ValueError):
        return False


def _legacy_edge_matches(on: str, verdict: str, outputs: dict) -> bool:
    eq = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*(==|!=|>=|<=|>|<)\s*(.+)$", on)
    if eq:
        left = outputs.get(eq.group(1))
        right = _literal_value(eq.group(3))
        op_str = eq.group(2)
        if op_str in ("==", "!="):
            op = (lambda a, b: str(a).upper() == str(b).upper()) if op_str == "==" else (lambda a, b: str(a).upper() != str(b).upper())
            try:
                return bool(op(left, right))
            except Exception:
                return False
        coerced = _coerce_compare(left, right)
        if coerced is None:
            return False
        a, b = coerced
        try:
            if op_str == ">":
                return a > b
            if op_str == "<":
                return a < b
            if op_str == ">=":
                return a >= b
            if op_str == "<=":
                return a <= b
        except Exception:
            return False
    in_match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s+in\s+\[([^\]]*)\]$", on)
    if in_match:
        left = str(outputs.get(in_match.group(1), "")).upper()
        vals = [str(_literal_value(v)).upper() for v in in_match.group(2).split(",")]
        return left in vals
    return on.upper() == verdict


def _workspace_fingerprint(cwd: Optional[str] = None) -> Optional[str]:
    """Identity of the working tree a cached node result was produced against.

    A node's output depends on the files it read, not just on its own .hwo
    source, so replaying a cached result across a changed workspace serves a
    stale answer. In a git repo the tree is identified by HEAD plus the dirty
    set (with each dirty path's size+mtime, since the porcelain list alone does
    not change when an already-dirty file is edited again). Outside a repo there
    is nothing cheap and exact to hash, so this returns None and the caller
    disables the cache instead of guessing.
    """
    base = cwd or str(Path.cwd())
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=base,
                              capture_output=True, text=True, timeout=5)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=base, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or status.returncode != 0:
        return None
    parts = [head.stdout.strip()]
    for line in status.stdout.splitlines():
        rel = line[3:].strip()
        parts.append(line)
        try:
            st = (Path(base) / rel).stat()
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _cache_key(path: str, node: dict, inputs: dict, workspace: str = "") -> str:
    try:
        source_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        source_hash = ""
    payload = json.dumps({"path": path, "node": node.get("id"), "inputs": inputs, "source": source_hash, "io": node.get("io"), "policy": node.get("policy"), "workspace": workspace}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_hwo_with_policy(node: dict, deps, session: dict, parent_id: Optional[str], inputs: dict,
                         events_cb=None) -> dict:
    policy = node.get("policy") or {}
    attempts = int(policy.get("retry") or 0) + 1
    timeout = _duration_seconds(policy.get("timeout"))
    cache_ttl = _duration_seconds(policy.get("cache"))
    fingerprint = _workspace_fingerprint() if cache_ttl else None
    # No fingerprint means no way to tell a stale result from a fresh one, so
    # the cache is skipped rather than trusted.
    cache_key = _cache_key(node["file"], node, inputs, fingerprint) if fingerprint else None
    if cache_key:
        cached = workflow_state.cache_get(cache_key)
        if cached:
            return {**cached, "cached": True}

    last = {"ok": False, "msg": "not run"}
    for attempt in range(1, max(1, attempts) + 1):
        holder: dict = {}
        abort_event = threading.Event()

        def target():
            holder.update(hwo_runner.run_hwo_file(
                path=node["file"],
                deps=deps,
                session=session,
                parent_id=parent_id,
                inputs=inputs,
                abort_event=abort_event,
                events_cb=events_cb,
                tool_scope=policy.get("tools"),
            ))

        t = threading.Thread(target=target, daemon=True, name=f"hwg-node-{node['id']}")
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            abort_event.set()
            t.join(timeout=2.0)
            last = {"ok": False, "msg": f"HWG node #{node['id']}# timed out after {timeout:g}s."}
            break
        last = holder or {"ok": False, "msg": f"HWG node #{node['id']}# returned no result."}
        if last.get("ok"):
            violation = _contract_violation(node, last)
            if violation is None:
                if cache_key:
                    workflow_state.cache_set(cache_key, last, cache_ttl)
                return last
            # Never cache a contract violation; retry it like any other failure.
            last = violation
        if attempt < attempts:
            time.sleep(min(2 ** (attempt - 1), 8))
    return last


def _read_include(path: str) -> Optional[str]:
    """Reader for @include. None means "no such file" — resolve_includes turns
    that into a named error instead of an exception."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _read_and_validate(path: str) -> tuple[Optional[list], Optional[str]]:
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return None, f"hwg: cannot read '{path}': {e}"
    try:
        statements = parse_hwg(source)
    except HwgParseError as e:
        return None, f"HWG parse error: {e}"
    statements, include_errors = resolve_includes(statements, path, _read_include)
    if include_errors:
        return None, "HWG include errors:\n" + "\n".join(include_errors)
    errors = validate_hwg(statements)
    if errors:
        return None, "HWG validation errors:\n" + "\n".join(errors)
    return statements, None


def compile_hwg_file(path: str) -> dict:
    statements, err = _read_and_validate(path)
    if err:
        return {"ok": False, "msg": err}
    graph = as_graph(statements)
    lines = [f"HWG compile OK: {path}", f"Nodes: {len(graph['nodes'])}", f"Edges: {len(graph['edges'])}"]
    for node in graph["nodes"]:
        policy = node.get("policy") or {}
        pol = f" policy={policy}" if policy else ""
        manual = "!" if node.get("manual") else ""
        lines.append(f"  {manual}({node['file']})#{node['id']}#{pol}")
    for edge in graph["edges"]:
        meta = []
        if edge.get("on"):
            meta.append(f"on: {edge['on']}")
        if edge.get("maxLoops"):
            meta.append(f"maxLoops: {edge['maxLoops']}")
        mid = f" {{ {', '.join(meta)} }}" if meta else ""
        arrow = "=>" if edge.get("fanout") else "->"
        lines.append(f"  #{edge['from']}# {arrow}{mid} #{edge['to']}#")
    # Metro-map diagram (best-effort: a viz failure must not fail compile).
    try:
        import workflow_viz
        diagram = workflow_viz.render_plain(
            workflow_viz.graph_from_statements(statements))
        lines += ["", diagram]
    except Exception:
        pass
    return {"ok": True, "msg": "\n".join(lines)}


def _emit_run(run: dict, event_type: str, payload: dict, events_cb=None) -> dict:
    run = workflow_state.emit(run, event_type, payload)
    if callable(events_cb):
        try:
            events_cb([{"runId": run.get("runId"), "type": event_type, **payload}])
        except Exception:
            pass
    return run


def run_hwg_file(path: str, deps, session: dict, parent_id: Optional[str] = None,
                 inputs: Optional[dict] = None, resume_run: Optional[dict] = None,
                 events_cb=None) -> dict:
    statements, err = _read_and_validate(path)
    if err:
        return {"ok": False, "msg": err}
    if not resume_run:
        missing_inputs = _missing_graph_inputs(statements, inputs or {})
        if missing_inputs:
            return {"ok": False, "msg": (
                f"hwg: missing required graph input(s): {', '.join(missing_inputs)}.\n"
                f"@graph declares them in in(...); supply a value for each when starting the run."
            )}
    graph = as_graph(statements)
    nodes = graph["nodes"]
    edges = graph["edges"]
    node_by_id = {n["id"]: n for n in nodes}
    outgoing = {n["id"]: [] for n in nodes}
    for edge in edges:
        outgoing.setdefault(edge["from"], []).append(edge)
    join_map = fanout_joins(nodes, edges)

    if resume_run:
        run = resume_run
        current = node_by_id.get(run.get("currentNode"))
    else:
        run = workflow_state.new_run("hwg", path, inputs or {})
        # Recorded so a resumed run resolves exists(...) paths against the same
        # working directory it started in.
        run["cwd"] = str(Path.cwd())
        has_incoming = {e["to"] for e in edges}
        starts = [n for n in nodes if n["id"] not in has_incoming]
        current = starts[0] if starts else None
    _prepare_node_tasks(
        run, nodes, str(Path.cwd()), owner_agent_id=parent_id)
    run = workflow_state.checkpoint(run, "run_started" if not resume_run else "run_resumed", {"path": path})
    run = _emit_run(run, "workflow_started", {"path": path, "kind": "hwg"}, events_cb)

    outputs_text = []
    steps = run.get("stepCount", 0) if resume_run else 0
    # Branches queued by a fan-out and joins still waiting for them. Both live on
    # the run so a pause inside a branch can be resumed without losing the others.
    pending = run.setdefault("pending", [])
    run.setdefault("joins", {})
    while current:
        if _join_blocks(current, run):
            current = node_by_id.get(pending.pop(0)) if pending else None
            continue
        if steps > MAX_GRAPH_STEPS:
            run["status"] = "failed"
            workflow_state.checkpoint(run, "step_limit_exceeded", {"limit": MAX_GRAPH_STEPS})
            return {"ok": False, "msg": f"HWG run exceeded {MAX_GRAPH_STEPS} steps.", "runId": run["runId"]}
        steps += 1
        run["stepCount"] = steps
        run["currentNode"] = current["id"]
        run = workflow_state.checkpoint(run, "node_started", {"node": current["id"]})
        _update_node_task(run, current["id"], "in_progress", 0)
        run = _emit_run(run, "node_started", {"node": current["id"], "file": current["file"]}, events_cb)

        if current.get("manual"):
            interrupt = {
                "id": f"{run['runId']}:{current['id']}",
                "node": current["id"],
                "file": current["file"],
                "message": f"Manual node #{current['id']}# requires human action.",
                "resumeOptions": ["PASS", "FAIL", "custom verdict"],
            }
            run["status"] = "paused"
            run["pendingInterrupt"] = interrupt
            _update_node_task(run, current["id"], "blocked", notes="Manual node requires human action")
            workflow_state.checkpoint(run, "interrupt", interrupt)
            run = _emit_run(run, "workflow_paused", interrupt, events_cb)
            return {
                "ok": False,
                "paused": True,
                "runId": run["runId"],
                "msg": (
                    f"HWG run paused at manual node #{current['id']}# ({current['file']}).\n"
                    f"Resume with: /hwg resume {run['runId']} PASS"
                ),
            }

        node_inputs = _build_node_inputs(current, run.get("inputs") or {}, run.get("nodeOutputs") or {}, run.get("nodeOutputHistory"))
        result = _run_hwo_with_policy(current, deps, session, parent_id, node_inputs, events_cb)
        if result.get("contractError"):
            # An authoring/protocol bug, not a domain verdict: stop instead of
            # routing down a FAIL edge as if the node had judged something.
            run["status"] = "failed"
            _update_node_task(run, current["id"], "blocked", None, result.get("msg", ""))
            run = workflow_state.checkpoint(run, "output_contract_violated", {"node": current["id"]})
            run = _emit_run(run, "node_failed", {
                "node": current["id"], "file": current["file"],
                "verdict": "CONTRACT_ERROR", "outputs": {},
            }, events_cb)
            run = _emit_run(run, "workflow_failed", {
                "node": current["id"], "reason": "output_contract",
            }, events_cb)
            outputs_text.append(f"[#{current['id']}# -> CONTRACT_ERROR]\n{result.get('msg', '')}")
            return {"ok": False, "msg": "\n\n".join(outputs_text), "runId": run["runId"]}
        raw_outputs = _parse_structured_return(result.get("msg", ""))
        raw_outputs.update(result.get("outputs") or {})
        current_outputs = _declared_node_outputs(current, raw_outputs)
        verdict = _extract_verdict(result.get("msg", ""), bool(result.get("ok")), current_outputs)
        current_outputs.setdefault("verdict", verdict)
        run.setdefault("nodeOutputHistory", {}).setdefault(current["id"], []).append(current_outputs)
        run.setdefault("nodeOutputs", {})[current["id"]] = current_outputs
        run.setdefault("history", []).append(current["id"])
        outputs_text.append(f"[#{current['id']}# -> {verdict}]\n{result.get('msg', '')}")
        _update_node_task(
            run, current["id"], "completed" if result.get("ok") else "blocked",
            100 if result.get("ok") else None,
            None if result.get("ok") else result.get("msg", ""),
        )
        run = workflow_state.checkpoint(run, "node_finished", {"node": current["id"], "verdict": verdict, "outputs": current_outputs})
        run = _emit_run(run, "node_completed" if result.get("ok") else "node_failed", {
            "node": current["id"], "file": current["file"],
            "verdict": verdict, "outputs": current_outputs,
        }, events_cb)

        next_node = _route_from(current, outgoing.get(current["id"], []), verdict, current_outputs,
                                run, join_map, node_failed=not result.get("ok"))
        if isinstance(next_node, dict) and next_node.get("error"):
            run["status"] = "failed"
            workflow_state.checkpoint(run, "routing_failed", next_node)
            run = _emit_run(run, "workflow_failed", next_node, events_cb)
            return {"ok": False, "msg": next_node["error"] + "\n\n" + "\n\n".join(outputs_text), "runId": run["runId"]}
        current = node_by_id.get(next_node) if next_node else None
        if current is None and pending:
            # This branch ended; pick up the next one the fan-out queued.
            current = node_by_id.get(pending.pop(0))

    if run.get("joins"):
        waiting = ", ".join(f"#{n}#" for n in run["joins"])
        run["status"] = "failed"
        workflow_state.checkpoint(run, "join_never_satisfied", {"joins": list(run["joins"])})
        run = _emit_run(run, "workflow_failed", {"reason": "join_never_satisfied"}, events_cb)
        return {"ok": False, "runId": run["runId"], "msg": (
            f"HWG run ended with {waiting} still waiting for fan-out branches that "
            f"never arrived.\n\n" + "\n\n".join(outputs_text)
        )}

    unhandled = run.get("unhandledFailures") or []
    if unhandled:
        named = ", ".join(f"#{n}#" for n in unhandled)
        run["status"] = "failed"
        run["currentNode"] = None
        workflow_state.checkpoint(run, "unhandled_node_failure", {"nodes": unhandled})
        run = _emit_run(run, "workflow_failed", {"reason": "unhandled_node_failure",
                                                 "nodes": unhandled}, events_cb)
        return {"ok": False, "runId": run["runId"], "msg": (
            f"HWG run reached the end, but {named} failed and no edge said what a "
            f"failure there means. Add an on: condition to route it, or fix the node.\n\n"
            + "\n\n".join(outputs_text)
        )}

    run["status"] = "completed"
    run["currentNode"] = None
    run["pendingInterrupt"] = None
    workflow_state.checkpoint(run, "run_completed", {"history": run.get("history", [])})
    run = _emit_run(run, "workflow_completed", {"history": run.get("history", [])}, events_cb)
    return {
        "ok": True,
        "runId": run["runId"],
        "msg": (
            f"HWG run completed: {path}\n"
            f"Run: {run['runId']}\n"
            f"Path: {' -> '.join('#' + n + '#' for n in run.get('history', []))}\n\n"
            + "\n\n".join(outputs_text)
        ),
    }


def _edge_context(run: dict) -> dict:
    return {
        "inputs": run.get("inputs") or {},
        "nodeOutputs": run.get("nodeOutputs") or {},
        "cwd": run.get("cwd"),
    }


def _join_blocks(node: dict, run: dict) -> bool:
    """Record this branch's arrival at a join node; True while others are still out.

    A join runs exactly once, after the last fan-out branch reaches it. Until
    then the walk parks the join and picks up the next queued branch.
    """
    if not ((node.get("policy") or {}).get("join")):
        return False
    state = (run.get("joins") or {}).get(node["id"])
    if not state:
        # Reached without an open fan-out (a plain -> path); nothing to wait for.
        return False
    state["arrived"] = int(state.get("arrived", 0)) + 1
    if state["arrived"] < int(state.get("expected", 1)):
        return True
    run["joins"].pop(node["id"], None)
    return False


def _route_from(node: dict, outs: list, verdict: str, outputs: dict, run: dict, join_map: dict,
                node_failed: bool = False):
    """Next node id, an {'error': ...} dict, or None when the walk ends here."""
    if any(e.get("fanout") for e in outs):
        return _open_fanout(node, outs, verdict, outputs, run, join_map)
    return _choose_next(node, outs, verdict, outputs, run, node_failed)


def _note_unhandled_failure(node: dict, run: dict) -> None:
    """Record a node failure the graph never explicitly routed on.

    An `on:` condition is the author saying what a failure means here. Walking
    on down an unconditional edge is not handling it — and a run that ends
    "completed" after a node failed is the exact shape of a workflow that looks
    green and did nothing.
    """
    failures = run.setdefault("unhandledFailures", [])
    if node["id"] not in failures:
        failures.append(node["id"])


def _open_fanout(node: dict, outs: list, verdict: str, outputs: dict, run: dict, join_map: dict):
    """Take every matching => branch instead of exactly one.

    Records how many branches the join must wait for, queues the rest, and
    returns the first. The walk stays single-threaded: branches run one after
    another, and the join node fires once the last of them arrives.
    """
    taken = [e for e in outs if _edge_matches(e, verdict, outputs, _edge_context(run))]
    if not taken:
        return {"error": f"HWG stopped at #{node['id']}#: verdict {verdict!r} matched none of its => branches."}
    join_id = join_map.get(node["id"])
    if not join_id:
        return {"error": f"HWG stopped at #{node['id']}#: its => branches do not converge on a join node."}
    run.setdefault("joins", {})[join_id] = {"expected": len(taken), "arrived": 0}
    run.setdefault("pending", []).extend(e["to"] for e in taken[1:])
    return taken[0]["to"]


def _choose_next(current: dict, outs: list, verdict: str, outputs: dict, run: dict,
                 node_failed: bool = False):
    if not outs:
        if node_failed:
            _note_unhandled_failure(current, run)
        return None
    matching = [e for e in outs if _edge_matches(e, verdict, outputs, _edge_context(run))]
    if not matching:
        return {"error": f"HWG stopped at #{current['id']}#: verdict {verdict!r} matched no outgoing edge."}
    for edge in matching:
        key = f"{edge['from']}->{edge['to']}"
        if edge.get("maxLoops"):
            count = int((run.setdefault("loopCounts", {}).get(key) or 0)) + 1
            if count > int(edge["maxLoops"]):
                continue
            run["loopCounts"][key] = count
        if node_failed and not edge.get("on"):
            _note_unhandled_failure(current, run)
        return edge["to"]
    return {"error": f"HWG stopped at #{current['id']}#: all matching edges exhausted maxLoops."}


def resume_hwg_run(run_id: str, deps, session: dict, parent_id: Optional[str] = None,
                   verdict: str = "PASS", outputs: Optional[dict] = None,
                   events_cb=None) -> dict:
    run = workflow_state.load_run(run_id)
    if not run:
        return {"ok": False, "msg": f"HWG run not found: {run_id}"}
    if run.get("kind") != "hwg":
        return {"ok": False, "msg": f"Run {run_id} is not an HWG run."}
    if run.get("status") != "paused" or not run.get("pendingInterrupt"):
        return {"ok": False, "msg": f"HWG run {run_id} is not paused."}
    node_id = run["pendingInterrupt"]["node"]
    node_outputs = dict(outputs or {})
    node_outputs.setdefault("verdict", verdict.upper())
    run.setdefault("nodeOutputHistory", {}).setdefault(node_id, []).append(node_outputs)
    run.setdefault("nodeOutputs", {})[node_id] = node_outputs
    run.setdefault("history", []).append(node_id)
    run["status"] = "running"
    run["pendingInterrupt"] = None

    statements, err = _read_and_validate(run["source"])
    if err:
        return {"ok": False, "msg": err}
    graph = as_graph(statements)
    outgoing = {n["id"]: [] for n in graph["nodes"]}
    node_by_id = {n["id"]: n for n in graph["nodes"]}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["from"], []).append(edge)
    node = node_by_id.get(node_id)
    if node is None:
        run["status"] = "failed"
        workflow_state.checkpoint(run, "resume_node_missing", {"node": node_id})
        return {"ok": False, "msg": f"HWG run {run_id} paused at node #{node_id}#, but that node no longer exists in the source file. Update the .hwg file or cancel the run."}
    next_id = _route_from(node, outgoing.get(node_id, []), node_outputs["verdict"], node_outputs, run,
                          fanout_joins(graph["nodes"], graph["edges"]))
    if isinstance(next_id, dict) and next_id.get("error"):
        run["status"] = "failed"
        workflow_state.checkpoint(run, "resume_routing_failed", next_id)
        return {"ok": False, "msg": next_id["error"], "runId": run_id}
    run["currentNode"] = next_id
    workflow_state.checkpoint(run, "interrupt_resumed", {"node": node_id, "verdict": node_outputs["verdict"]})
    return run_hwg_file(
        run["source"], deps, session, parent_id=parent_id,
        inputs=run.get("inputs") or {}, resume_run=run,
        events_cb=events_cb,
    )


def status(run_id: Optional[str] = None) -> dict:
    if run_id:
        run = workflow_state.load_run(run_id)
        if not run:
            return {"ok": False, "msg": f"Run not found: {run_id}"}
        return {"ok": True, "msg": _format_run(run, mini=True)}
    runs = workflow_state.list_runs()
    if not runs:
        return {"ok": True, "msg": "No workflow runs."}
    return {"ok": True, "msg": "\n".join(_format_run(r, one_line=True) for r in runs[:20])}


def cancel(run_id: str) -> dict:
    run = workflow_state.load_run(run_id)
    if not run:
        return {"ok": False, "msg": f"Run not found: {run_id}"}
    run["status"] = "cancelled"
    workflow_state.checkpoint(run, "run_cancelled", {})
    return {"ok": True, "msg": f"Cancelled {run_id}."}


def gantt(run_id: str) -> dict:
    """Render a Gantt replay for one durable run (best-effort)."""
    run = workflow_state.load_run(run_id)
    if not run:
        return {"ok": False, "msg": f"Run not found: {run_id}"}
    order = None
    source = run.get("source")
    if source:
        try:
            import workflow_viz
            order = workflow_viz.node_order_from_source(
                Path(source).read_text(encoding="utf-8"))
        except Exception:
            order = None
    try:
        import workflow_viz
        chart = workflow_viz.gantt_from_run(run, order)
    except Exception as exc:
        return {"ok": False, "msg": f"gantt failed: {type(exc).__name__}: {exc}"}
    head = (f"Run: {run.get('runId')}\n"
            f"Kind: {run.get('kind')}\n"
            f"Status: {run.get('status')}\n")
    return {"ok": True, "msg": head + chart}


def _format_run(run: dict, one_line: bool = False, mini: bool = False) -> str:
    if one_line:
        return f"{run.get('runId')}  {run.get('kind')}  {run.get('status')}  {run.get('source')}  current={run.get('currentNode') or '-'}"
    text = (
        f"Run: {run.get('runId')}\n"
        f"Kind: {run.get('kind')}\n"
        f"Status: {run.get('status')}\n"
        f"Source: {run.get('source')}\n"
        f"Current: {run.get('currentNode') or '-'}\n"
        f"History: {' -> '.join(run.get('history') or []) or '-'}\n"
        f"Pending interrupt: {json.dumps(run.get('pendingInterrupt'), ensure_ascii=False) if run.get('pendingInterrupt') else '-'}\n"
        f"Checkpoints: {len(run.get('checkpoints') or [])}"
    )
    if mini:
        try:
            import workflow_viz
            order = None
            source = run.get("source")
            if source:
                try:
                    order = workflow_viz.node_order_from_source(
                        Path(source).read_text(encoding="utf-8"))
                except Exception:
                    order = None
            strip = workflow_viz.mini_from_run(run, order)
            if strip:
                text += "\n\n" + strip
        except Exception:
            pass
    return text
