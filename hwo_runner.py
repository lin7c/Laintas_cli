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


@dataclass
class HwoParallel:
    kind: str = "parallel"
    body: list = field(default_factory=list)   # list[HwoAgent] only


HwoStep = Union[HwoTask, HwoAgent, HwoParallel]


# ── Parser (ported from hwo.ts HwoParser) ────────────────────────────────

class HwoParseError(Exception):
    def __init__(self, message: str, index: int):
        super().__init__(f"{message} at position {index}")
        self.index = index


class HwoParser:
    def __init__(self, source: str):
        self.source = source
        self.i = 0

    def parse(self) -> list:
        steps = self._parse_sequence()
        self._skip_ws()
        if not self._eof():
            snippet = self.source[self.i:self.i + 16]
            raise HwoParseError(f'Unexpected token "{snippet}"', self.i)
        return steps

    def _parse_sequence(self, stop=None) -> list:
        """stop: None | 'brace' | 'parallel'"""
        steps = []
        while not self._eof():
            self._skip_ws()
            if self._eof():
                break
            if stop == 'brace' and self._peek() == '}':
                break
            if stop == 'parallel' and self._at_parallel_marker():
                break
            if self._starts('```'):
                self._skip_comment_block()
                continue
            if self._starts('->'):
                self.i += 2
                continue
            if self._at_parallel_marker():
                steps.append(self._parse_parallel())
                continue
            if self._peek() == '#':
                steps.append(self._parse_agent())
                continue
            text = self._read_text(stop).strip()
            if text:
                steps.append(HwoTask(text=text))
        return steps

    def _parse_parallel(self) -> HwoParallel:
        self._expect('//')
        body = self._parse_sequence('parallel')
        if not self._at_parallel_marker():
            raise HwoParseError(
                'Unclosed parallel block, expected // to close', self.i
            )
        self._expect('//')
        return HwoParallel(body=body)

    def _parse_agent(self) -> HwoAgent:
        start = self.i
        self._expect('#')
        name_start = self.i
        while not self._eof() and self._peek() != '#':
            self.i += 1
        if self._eof():
            raise HwoParseError('Unclosed agent name, expected #', start)
        name = self.source[name_start:self.i].strip()
        if not name:
            raise HwoParseError('Empty agent name', start)
        self._expect('#')
        self._skip_ws()
        if self._peek() != '{':
            raise HwoParseError(
                f'Agent "{name}" must be followed by {{', self.i
            )
        self._expect('{')
        body = self._parse_sequence('brace')
        if self._peek() != '}':
            raise HwoParseError(
                f'Unclosed body for agent "{name}", expected }}', self.i
            )
        self._expect('}')
        return HwoAgent(name=name, body=body)

    def _read_text(self, stop=None) -> str:
        start = self.i
        while not self._eof():
            if self._starts('```'):
                break
            if self._starts('->'):
                break
            if self._at_parallel_marker():
                break
            if stop == 'brace' and self._peek() == '}':
                break
            if self._peek() == '#' and self._looks_like_agent():
                break
            self.i += 1
        return self.source[start:self.i]

    def _skip_comment_block(self) -> None:
        self._expect('```')
        end = self.source.find('```', self.i)
        if end == -1:
            raise HwoParseError('Unclosed comment block, expected ```', self.i)
        self.i = end + 3

    def _looks_like_agent(self) -> bool:
        import re
        rest = self.source[self.i:]
        return bool(re.match(r'^#[^#{}\n\r]+#\s*\{', rest))

    def _at_parallel_marker(self) -> bool:
        if not self._starts('//'):
            return False
        if self.i == 0:
            return True
        prev = self.source[self.i - 1]
        return prev in ('\n', ' ', '\t', '\r', '{', '}')

    def _skip_ws(self) -> None:
        while not self._eof() and self.source[self.i] in ' \t\n\r':
            self.i += 1

    def _starts(self, text: str) -> bool:
        return self.source.startswith(text, self.i)

    def _expect(self, text: str) -> None:
        if not self._starts(text):
            raise HwoParseError(f'Expected "{text}"', self.i)
        self.i += len(text)

    def _peek(self) -> str:
        return self.source[self.i] if self.i < len(self.source) else ''

    def _eof(self) -> bool:
        return self.i >= len(self.source)


def parse_hwo(source: str) -> list:
    return HwoParser(source).parse()


# ── Validation (mirrors hwo.ts validateParallelBlocks + validateUniqueNames) ─

def validate_parallel_blocks(steps: list) -> list:
    errors = []
    def walk(items, path):
        for item in items:
            if item.kind == 'parallel':
                for child in item.body:
                    if child.kind != 'agent':
                        errors.append(
                            f"{path}: parallel blocks may only contain #agent# {{...}} entries."
                        )
                walk(item.body, f"{path}//")
            elif item.kind == 'agent':
                walk(item.body, f"{path}#{item.name}#")
    walk(steps, 'root')
    return errors


def validate_unique_names(steps: list) -> list:
    errors = []
    def walk_scope(items, scope_path):
        seen = set()
        def visit(lst):
            for item in lst:
                if item.kind == 'agent':
                    if item.name in seen:
                        errors.append(
                            f"{scope_path}: duplicate agent name \"#{item.name}#\" — "
                            "sibling agents must have unique names in the same scope."
                        )
                    else:
                        seen.add(item.name)
                    walk_scope(item.body, f"{scope_path} > #{item.name}#")
                elif item.kind == 'parallel':
                    visit(item.body)
        visit(items)
    walk_scope(steps, 'root')
    return errors


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


# ── Execution context ─────────────────────────────────────────────────────

@dataclass
class HwoCtx:
    deps: object                        # LoopDeps from agent_loop
    session: dict
    parent_id: Optional[str] = None    # the owning agent (for waiting + child registration)
    name_path: list = field(default_factory=list)   # ancestor name path for naming
    inherited_context: str = ""        # parent's accumulated step context
    abort_event: Optional[object] = None


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

    spawn_ctx = (
        "[HWO] Execute this workflow step exactly. Do not reinterpret the workflow; "
        "the HWO runner controls ordering and parallelism."
    )
    if inherited:
        spawn_ctx += (
            f"\n\n[WORKFLOW CONTEXT — earlier step results]\n{inherited}\n"
            "[END CONTEXT] Your goal is ONLY the step text."
        )

    done_event = threading.Event()
    result_holder = {}

    def _runner(ok: bool):
        if not ok:
            result_holder.update({"ok": False, "msg": "Task cancelled while queued."})
            done_event.set()
            return
        try:
            r = run_agent_loop(
                ctx.deps, text, ctx.session, child.state, child.chat_history,
                depth=child.depth, agent_id=child.id,
            )
            reply = (r.get("state") or {}).get("lastReply", "") if isinstance(r, dict) else ""
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
    """Spawn a child agent, run its body as a sub-sequence, wait for it."""
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

    done_event = threading.Event()
    result_holder = {}

    def _runner(ok: bool):
        if not ok:
            result_holder.update({"ok": False, "msg": f"#{full_name}# cancelled while queued."})
            done_event.set()
            return
        try:
            sub_ctx = HwoCtx(
                deps=ctx.deps,
                session=ctx.session,
                parent_id=child.id,
                name_path=[*ctx.name_path, agent_step.name],
                inherited_context=inherited,
                abort_event=ctx.abort_event,
            )
            result = run_sequence(agent_step.body, sub_ctx)
            msg = result.get("msg", "")
            if result.get("ok"):
                mark_agent_finished(child.id, result=msg)
            else:
                mark_agent_finished(child.id, error=msg)
            result_holder.update(result)
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
        steps = parse_hwo(source)
    except HwoParseError as e:
        return {"ok": False, "msg": f"hwo: parse error — {e}"}

    errors = validate_parallel_blocks(steps) + validate_unique_names(steps)
    if errors:
        return {"ok": False, "msg": "hwo: validation errors:\n" + "\n".join(errors)}

    summary = "\n".join(summarize_steps(steps))

    ctx = HwoCtx(
        deps=deps,
        session=session,
        parent_id=parent_id,
        inherited_context=caller_context,
        abort_event=abort_event,
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
        steps = parse_hwo(source)
    except HwoParseError as e:
        return {"ok": False, "msg": f"hwo: parse error — {e}"}

    errors = validate_parallel_blocks(steps) + validate_unique_names(steps)
    if errors:
        return {"ok": False, "msg": "hwo: validation errors:\n" + "\n".join(errors)}

    summary = "\n".join(summarize_steps(steps))
    return {"ok": True, "msg": f"HWO compile OK\n{summary}", "summary": summary}
