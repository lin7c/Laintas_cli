"""company-process skill — run Helpwo company pipelines on the CLI.

Executes each stage as a hired employee (register_agent + EmployeeProfile.prompt
= the persona) stationed on a dedicated sub-terminal (the same recipe /station
uses), run via the official start_agent_assignment path. A background scheduler
fires runs on the local clock. NOTHING in laintas_cli core is modified — this
lives entirely in the skill and drives the public agent_loop API + the ctx the
CLI hands every tool.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from tools import Tool
import paths
import agent_loop as _al

PROC_DIR = Path(paths.LAINTAS_HOME) / "processes"
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
START_GRACE = timedelta(minutes=3)
MAX_TOTAL_STEPS = 40
MAX_DEPTH = 3

# Runtime handle captured from a tool ctx so the scheduler can run stages without
# a live ctx (deps/session/events_cb belong to the connected REPL session).
_RT = {"deps": None, "session": None, "events_cb": None}
_RT_LOCK = threading.Lock()
_SCHED_STARTED = False
_SCHED_STOP = threading.Event()


# ── Storage (~/.laintas/processes/) ─────────────────────────────────────────

def _safe(name: str) -> str:
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", name or ""):
        raise ValueError(f"invalid process name {name!r}")
    return name


def _ppath(name: str) -> Path:
    return PROC_DIR / f"{_safe(name)}.json"


def _spath(name: str) -> Path:
    return PROC_DIR / f"{_safe(name)}.state.json"


def _register(bundle: dict) -> str:
    name = _safe(bundle.get("name", ""))
    if not isinstance(bundle.get("stages"), list) or not bundle["stages"]:
        raise ValueError("process needs a non-empty stages list")
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    _ppath(name).write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    return str(_ppath(name))


def _load(name: str):
    p = _ppath(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _names() -> list:
    if not PROC_DIR.exists():
        return []
    return sorted(f.stem for f in PROC_DIR.glob("*.json") if not f.name.endswith(".state.json"))


def _read_state(name: str) -> dict:
    p = _spath(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_state(name: str, st: dict) -> None:
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    _spath(name).write_text(json.dumps(st, ensure_ascii=False, indent=2))


# ── State-machine helpers ───────────────────────────────────────────────────

def _by_id(proc: dict) -> dict:
    return {s["id"]: s for s in proc.get("stages", []) if isinstance(s, dict) and s.get("id")}


def _verdict(output: str):
    v = None
    for line in (output or "").splitlines():
        m = re.match(r"\s*VERDICT\s*[:：]\s*([A-Za-z0-9_-]+)", line, re.IGNORECASE)
        if m:
            v = m.group(1)
    return v


def _choose_next(stage: dict, output: str):
    edges = [e for e in (stage.get("next") or []) if isinstance(e, dict)]
    if not edges:
        return None
    conds = [e for e in edges if e.get("on")]
    if not conds:
        return edges[0]
    v = _verdict(output)
    if v:
        for e in conds:
            if str(e["on"]).strip().lower() == v.strip().lower():
                return e
    low = (output or "").lower()
    for e in conds:
        if re.search(r"\b" + re.escape(str(e["on"]).lower()) + r"\b", low):
            return e
    uncond = [e for e in edges if not e.get("on")]
    return uncond[0] if uncond else conds[0]


def _stage_input(proc: dict, stage: dict, context: str, workspace: str) -> str:
    parts = [
        f"[COMPANY PROCESS: {proc.get('name','')} / STAGE: {stage['id']} — role {stage.get('role','')}]",
        stage.get("task", ""),
        f"\n[RUN WORKSPACE] {workspace}\nWrite ALL your outputs as files under this "
        f"workspace folder and read earlier stages' outputs from there. Keep everything inside it.",
    ]
    if context:
        parts.append(
            "\n[CONTEXT — summaries of earlier stages; full artifacts are files in the workspace]\n"
            + context + "\n[END CONTEXT]"
        )
    if any(e.get("on") for e in (stage.get("next") or [])):
        labels = sorted({str(e["on"]) for e in stage["next"] if e.get("on")})
        parts.append(f"\nWhen finished, put on the LAST line exactly:\nVERDICT: <one of: {', '.join(labels)}>")
    return "\n".join(parts)


def _append_ctx(acc: str, sid: str, out: str) -> str:
    capped = out if len(out) <= 1000 else out[:1000] + "…"
    nxt = (acc + "\n\n" if acc else "") + f"[DONE {sid}]\n{capped}"
    return nxt if len(nxt) <= 4000 else "…" + nxt[-4000:]


# ── Employee + terminal (the CLI's own hire/station recipe) ──────────────────

def _ensure_employee(role: str, persona: str):
    emp = _al.get_agent(role)
    if emp is None:
        prof = _al.EmployeeProfile(title=role, description=f"Company employee {role}", prompt=persona or "")
        emp = _al.register_agent(name=role, role="deployed", profile=prof)
    elif persona:
        try:
            emp.profile.prompt = persona
        except Exception:
            pass
    return emp


def _ensure_terminal(role: str):
    """Create/reuse a dedicated sub-terminal for the role — same recipe /station uses."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", f"stn-{role}")[:64]
    existing = _al.get_terminal(name)
    if existing and getattr(existing, "session", None) and existing.session.is_alive():
        return name
    try:
        import laintas_cli as _cli
        if existing:
            _al.unregister_terminal(name)
        sub = _cli.SubTerminalSession(_cli.DEFAULT_SHELL)
        sub.start()
        time.sleep(0.1)
        if not sub.is_alive():
            return None
        sub.read_output(timeout=0.1)
        _al.register_terminal(
            sub, _cli.DEFAULT_SHELL, 0, name=name,
            parent_terminal="term0")
    except Exception:
        return None
    return name


def _run_stage(role: str, persona: str, task: str, deps, session, events_cb, abort_ev) -> dict:
    term = _ensure_terminal(role)
    if not term:
        return {"ok": False, "output": f"could not station employee '{role}' on a terminal"}
    employee = _ensure_employee(role, persona)
    if not _al.station_agent(employee.id, term):
        return {"ok": False, "output": f"could not deploy employee '{role}' to terminal '{term}'"}
    ok, msg, assignment = _al.start_agent_assignment(role, task, deps, session=session, events_cb=events_cb)
    if not ok or assignment is None:
        return {"ok": False, "output": msg}
    # Wait for the assignment (runs in its own thread) to reach a terminal state.
    while assignment.status in ("queued", "running") and not abort_ev.is_set():
        time.sleep(1.0)
    if abort_ev.is_set():
        try:
            emp = _al.get_agent(role)
            if emp:
                emp.abort_event.set()
        except Exception:
            pass
    return {"ok": assignment.status == "completed", "output": assignment.result or assignment.error or ""}


# ── Coordinator ─────────────────────────────────────────────────────────────

def _deadline(proc: dict, now: datetime):
    dl = _parse_hhmm((proc.get("schedule") or {}).get("deadline", ""))
    return now.replace(hour=dl[0], minute=dl[1], second=0, microsecond=0) if dl else None


def _run_process(name: str, proc: dict, deps, session, events_cb, abort_ev) -> None:
    roles = {r["name"]: (r.get("persona", "") if isinstance(r, dict) else "")
             for r in (proc.get("roles") or []) if isinstance(r, dict) and r.get("name")}
    by_id = _by_id(proc)
    stages = proc.get("stages", [])
    if not stages:
        return
    now0 = datetime.now()
    workspace = str(Path.cwd() / "company_runs" / re.sub(r"[^A-Za-z0-9_-]", "_", name) / now0.strftime("%Y%m%d-%H%M%S"))
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    deadline = _deadline(proc, now0)

    def emit(line: str) -> None:
        try:
            import laintas_cli as _cli  # console is optional; skip if unavailable
            _cli.console.print(f"[cyan]{line}[/cyan]")
        except Exception:
            pass
        try:
            if events_cb:
                events_cb([{"type": "system", "kind": "status", "content": f"[process] {line}"}])
        except Exception:
            pass

    emit(f"▶ '{name}' started · workspace {workspace}")
    current = stages[0]["id"]
    context = ""
    loop_counts: dict = {}
    steps = 0
    status = "completed"
    while current and steps < MAX_TOTAL_STEPS and not abort_ev.is_set():
        if deadline and datetime.now() >= deadline:
            emit(f"⏰ deadline reached at {current} — delivering partial"); status = "timeout"; break
        stage = by_id.get(current)
        if not stage:
            emit(f"! unknown stage {current}"); status = "error"; break
        if stage.get("manual"):
            emit(f"⏸ {current} is a manual step — waiting for a human")
            _write_state(name, {"last_run": {"status": "waiting", "stage": current, "workspace": workspace,
                                             "at": datetime.now().isoformat(timespec='seconds')}})
            return
        role = stage.get("role", "")
        emit(f"→ {current} ({role})")
        res = _run_stage(role, roles.get(role, ""), _stage_input(proc, stage, context, workspace),
                         deps, session, events_cb, abort_ev)
        out = res.get("output", "")
        v = _verdict(out) or ""
        emit(f"✓ {current} done" + (f" · {v}" if v else ""))
        context = _append_ctx(context, current, out)
        steps += 1
        edge = _choose_next(stage, out)
        if not edge or not edge.get("to"):
            break
        if edge.get("maxLoops"):
            key = f"{current}->{edge['to']}:{edge.get('on','')}"
            loop_counts[key] = loop_counts.get(key, 0) + 1
            if loop_counts[key] > int(edge["maxLoops"]):
                emit(f"↩ loop limit at {current} → stopping"); break
        current = edge["to"]
    if abort_ev.is_set():
        status = "aborted"
    emit(f"■ '{name}' finished: {status} · deliverables in {workspace}")
    _write_state(name, {"last_run": {"status": status, "workspace": workspace,
                                     "at": datetime.now().isoformat(timespec='seconds')}})


# ── Scheduler (local clock; missed = skip) ──────────────────────────────────

def _parse_hhmm(s: str):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if (0 <= h < 24 and 0 <= mi < 60) else None


def _due(proc: dict, now: datetime, last_date):
    sched = proc.get("schedule") or {}
    start = _parse_hhmm(sched.get("start", ""))
    if not start:
        return ""
    days = sched.get("days")
    if days and _WEEKDAYS[now.weekday()] not in days:
        return ""
    if last_date == now.date().isoformat():
        return ""
    start_dt = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    if now < start_dt:
        return ""
    return "run" if now <= start_dt + START_GRACE else "skip"


def _sched_loop() -> None:
    while not _SCHED_STOP.wait(120):
        try:
            now = datetime.now()
            for nm in _names():
                proc = _load(nm)
                if not proc or not (proc.get("schedule") or {}).get("start"):
                    continue
                st = _read_state(nm)
                v = _due(proc, now, st.get("last_run_date"))
                if not v:
                    continue
                st["last_run_date"] = now.date().isoformat()
                _write_state(nm, st)
                if v == "run":
                    with _RT_LOCK:
                        deps, session, ecb = _RT["deps"], _RT["session"], _RT["events_cb"]
                    if deps is None:
                        continue  # no runtime handle captured yet (deploy once to arm it)
                    threading.Thread(target=_run_process, args=(nm, proc, deps, session, ecb, threading.Event()),
                                     daemon=True, name=f"proc-sched-{nm}").start()
        except Exception:
            pass


def _start_scheduler() -> None:
    global _SCHED_STARTED
    if _SCHED_STARTED:
        return
    _SCHED_STARTED = True
    threading.Thread(target=_sched_loop, daemon=True, name="company-process-scheduler").start()


# ── Tools ───────────────────────────────────────────────────────────────────

def _capture(ctx) -> None:
    with _RT_LOCK:
        _RT["deps"] = ctx.deps
        _RT["session"] = ctx.session
        _RT["events_cb"] = ctx.events_cb


def _t_deploy(params: dict, ctx) -> dict:
    proc = params.get("process")
    if not isinstance(proc, dict) or not proc.get("name") or not isinstance(proc.get("stages"), list):
        return {"ok": False, "error": "process must be an object with a name and a stages list"}
    _capture(ctx)
    _start_scheduler()
    for r in proc.get("roles", []) or []:
        if isinstance(r, dict) and r.get("name"):
            try:
                _ensure_employee(r["name"], r.get("persona", ""))
            except Exception:
                pass
    try:
        path = _register(proc)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if params.get("runNow"):
        threading.Thread(target=_run_process, args=(proc["name"], proc, ctx.deps, ctx.session, ctx.events_cb, threading.Event()),
                         daemon=True, name=f"proc-{proc['name']}").start()
    return {"ok": True, "result": f"deployed '{proc['name']}' ({len(proc['stages'])} stages) → {path}"
            + (" · running one cycle now" if params.get("runNow") else "")}


def _t_run(params: dict, ctx) -> dict:
    nm = params.get("name", "")
    proc = _load(nm)
    if not proc:
        return {"ok": False, "error": f"no process '{nm}' (deploy it first)"}
    _capture(ctx)
    _start_scheduler()
    threading.Thread(target=_run_process, args=(nm, proc, ctx.deps, ctx.session, ctx.events_cb, threading.Event()),
                     daemon=True, name=f"proc-{nm}").start()
    return {"ok": True, "result": f"process '{nm}' started"}


def _t_list(params: dict, ctx) -> dict:
    out = []
    for nm in _names():
        proc = _load(nm) or {}
        sched = (proc.get("schedule") or {}).get("start")
        st = _read_state(nm).get("last_run", {})
        out.append({"name": nm, "stages": len(proc.get("stages", [])), "start": sched,
                    "last": st.get("status")})
    return {"ok": True, "result": json.dumps(out, ensure_ascii=False)}


def _t_status(params: dict, ctx) -> dict:
    nm = params.get("name", "")
    if not _load(nm):
        return {"ok": False, "error": f"no process '{nm}'"}
    return {"ok": True, "result": json.dumps(_read_state(nm), ensure_ascii=False)}


def get_tools():
    return [
        Tool(name="company.deploy", description="Register a Helpwo company process on the CLI and pre-hire its employees (set runNow to also run one cycle).",
             schema={"type": "object", "properties": {"process": {"type": "object"}, "runNow": {"type": "boolean"}}, "required": ["process"]},
             invoke=_t_deploy, source="skill:company-process"),
        Tool(name="company.run", description="Run one cycle of a registered company process now.",
             schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             invoke=_t_run, source="skill:company-process"),
        Tool(name="company.list", description="List registered company processes and their last run status.",
             schema={"type": "object", "properties": {}}, invoke=_t_list, source="skill:company-process"),
        Tool(name="company.status", description="Show the last-run status and workspace of a company process.",
             schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
             invoke=_t_status, source="skill:company-process"),
    ]
