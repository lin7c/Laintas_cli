"""Auto-Pilot: heuristic task classification and hint injection.

Progressive auto-pilot system with three layers:

**Layer 1 (Phase 1):** Pure heuristic classification with zero API cost.
Determines whether a user task would benefit from parallel agents, sequential
pipeline, or background monitoring, and injects a non-prescriptive hint.

**Layer 2 (Phase 2):** LLM-based task decomposition.  When the heuristic
classifies a task as ``PARALLEL_HINT`` or ``PIPELINE_HINT``, a background
LLM call decomposes it into concrete subtasks within a configurable timeout.
Falls back to heuristic decomposition on timeout or failure.

**Layer 3 (Phase 3):** Auto-execution with dynamic escalation.  When
``auto_pilot_auto_execute`` is enabled, decomposed subtasks are pre-spawned
as sub-agents before the main loop starts.  The main agent runs as
orchestrator with knowledge of the pre-spawned agents.

The agent retains full autonomy: hints are suggestions, not commands.
Only ``!`` prefix (user override) forces single-agent mode.
"""

import json
import re
import threading
from typing import Optional

# ── Strategy constants ─────────────────────────────────────────────────

SIMPLE = "simple"                # single agent, no hint
PARALLEL_HINT = "parallel_hint"  # task has independent components
PIPELINE_HINT = "pipeline_hint"  # task has sequential phases
MONITOR = "monitor"              # task needs background watching
PIPELINE = "pipeline"            # active workflow exists (workflow handles it)

# ── Heuristic patterns ─────────────────────────────────────────────────

_QUESTION_RE = re.compile(
    r"^\s*(?:what|why|how|where|when|who|which|whose|is|are|can|does|do|did"
    r"|will|would|could|should|may|might)\b",
    re.IGNORECASE,
)

_MONITOR_RE = re.compile(
    r"\b(?:watch|monitor|tail\b|keep\s+an\s+eye|alert\s+me|notify\s+me"
    r"|while\s+(?:running|executing|building|serving)"
    r"|wait\s+for\s+[\w\s]+?\s+(?:to\s+)?(?:appear|show|print|output|finish|complete))\b",
    re.IGNORECASE,
)

# Sequential structure: 2+ sequence connectors in one task
_PIPELINE_RE = re.compile(
    r"\b(?:then|after\s+that|finally|next|step\s+\d|first\b|lastly"
    r"|once\s+(?:that|this|done)|before\s+\w+\s+(?:do|run|start))\b",
    re.IGNORECASE,
)

_ACTION_VERB_RE = re.compile(
    r"\b(?:create|update|delete|modify|refactor|add|remove|fix|test|deploy"
    r"|build|implement|rewrite|optimize|migrate|install|configure|run"
    r"|write|review|analyze|debug|setup|integrate|generate|convert"
    r"|extract|validate|document|clean)\b",
    re.IGNORECASE,
)

# File-like reference: "foo.py", "bar.json", "src/auth"
_FILE_REF_RE = re.compile(r"\b[\w/]+\.\w{1,6}\b")
_DIR_REF_RE = re.compile(
    r"\b(?:src|test|tests|lib|bin|config|docs?|scripts?|api|ui|db|migrations?)/\w+",
    re.IGNORECASE,
)

_AND_RE = re.compile(r"\b(?:and|also|as\s+well\s+as)\b", re.IGNORECASE)

# ── Phase 2: Decomposition callback ────────────────────────────────────

_decompose_cb: Optional[callable] = None

_last_decompose_source: Optional[str] = None


def get_last_decompose_source() -> Optional[str]:
    """Return the source of the last successful decomposition: 'llm', 'heuristic', or None."""
    return _last_decompose_source


def set_decompose_callback(fn: Optional[callable]) -> None:
    """Register or clear the LLM decomposition callback.

    The callback signature is::
        fn(task: str, strategy: str, timeout: float) -> Optional[list[str]]

    It should return a list of subtask strings, or None on failure.
    ``laintas_cli.py`` registers this at startup to bridge to the backend LLM.
    """
    global _decompose_cb
    _decompose_cb = fn


def decompose_task(task: str, strategy: str, timeout: float = 20.0) -> Optional[list[str]]:
    """Decompose a task into subtasks using LLM with timeout fallback.

    Returns a list of 2+ subtask strings, or None if decomposition is not
    applicable / fails.  Falls back to heuristic decomposition when the LLM
    callback is unavailable or times out.
    """
    global _last_decompose_source
    if strategy not in (PARALLEL_HINT, PIPELINE_HINT):
        _last_decompose_source = None
        return None

    # Try LLM decomposition first.
    if _decompose_cb is not None:
        try:
            result = _run_with_timeout(_decompose_cb, task, strategy, timeout)
            if result and isinstance(result, list) and len(result) >= 2:
                cleaned = [str(s).strip() for s in result if str(s).strip()]
                if len(cleaned) >= 2:
                    _last_decompose_source = "llm"
                    return cleaned
        except Exception:
            pass

    # Heuristic fallback.
    heuristic_result = _heuristic_decompose(task, strategy)
    if heuristic_result:
        _last_decompose_source = "heuristic"
    else:
        _last_decompose_source = None
    return heuristic_result


def _run_with_timeout(fn, *args, timeout: float = 20.0):
    """Run ``fn(*args)`` in a thread with a hard timeout.

    Returns the function's return value, or raises ``TimeoutError`` if the
    thread doesn't complete within ``timeout`` seconds.
    """
    result_holder: dict = {}

    def _worker():
        try:
            result_holder["value"] = fn(*args)
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=_worker, daemon=True, name="auto-pilot-decompose")
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"decompose_task timed out after {timeout}s")
    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder.get("value")


def _heuristic_decompose(task: str, strategy: str) -> Optional[list[str]]:
    """Fallback: split task on conjunctions / sequence markers.

    Returns 2+ subtasks if the task can be split, None otherwise.
    """
    # Split on sequence markers for pipeline tasks.
    if strategy == PIPELINE_HINT:
        parts = re.split(
            r"\s*(?:,?\s+then\s+|,?\s+after\s+that\s+|,?\s+next\s+"
            r"|,?\s+finally\s+|,?\s+lastly\s+|;\s*)",
            task, flags=re.IGNORECASE,
        )
        parts = [p.strip() for p in parts if len(p.strip()) > 10]
        if len(parts) >= 2:
            return parts[:6]

    # Split on " and " / " also " for parallel tasks.
    if strategy == PARALLEL_HINT:
        parts = re.split(
            r"\s+(?:and|also|as\s+well\s+as)\s+",
            task, flags=re.IGNORECASE,
        )
        parts = [p.strip() for p in parts if len(p.strip()) > 15]
        if len(parts) >= 2:
            return parts[:6]

    return None


def build_decomposed_hint(strategy: str, subtasks: list[str]) -> str:
    """Build a rich hint that includes the decomposed subtasks.

    Replaces the generic Phase 1 hint when decomposition succeeds.
    """
    if not subtasks or len(subtasks) < 2:
        return build_hint(strategy)

    subtask_list = "\n".join(
        f"  {i+1}. {st}" for i, st in enumerate(subtasks)
    )

    if strategy == PARALLEL_HINT:
        return (
            "[auto-pilot] This task has been decomposed into independent subtasks:\n"
            f"{subtask_list}\n\n"
            "Use spawn_parallel to execute them concurrently - "
            "each sub-agent gets an isolated git worktree automatically. "
            "If subtasks share files, use spawn_chain instead. "
            "Do NOT use HWO for this - HWO is for reusable workflows with "
            "structured I/O contracts, not one-off parallel work. "
            "You may adjust the decomposition if it doesn't fit."
        )
    if strategy == PIPELINE_HINT:
        return (
            "[auto-pilot] This task has been decomposed into sequential phases:\n"
            f"{subtask_list}\n\n"
            "Consider using spawn_chain for ordered execution with handoff "
            "between steps. For complex multi-phase work, suggest the user "
            "start a workflow with /workflow start. "
            "You may adjust the decomposition if it doesn't fit."
        )
    return build_hint(strategy)


# ── Phase 3: Auto-execution orchestrator ──────────────────────────────

# Thread-local storage for passing the auto-pilot plan from
# _run_agent_loop_with_interrupt (laintas_cli.py) to run_agent_loop
# (agent_loop.py).  This avoids modifying run_agent_loop's signature and
# is thread-safe: each thread gets its own plan.
_auto_pilot_state = threading.local()


def set_pending_plan(plan: Optional[dict]) -> None:
    """Set the auto-pilot execution plan for the current thread.

    Called by ``_run_agent_loop_with_interrupt`` after decomposition.
    ``run_agent_loop`` reads and clears this at depth==0.
    """
    _auto_pilot_state.plan = plan


def get_pending_plan() -> Optional[dict]:
    """Get and clear the auto-pilot execution plan for the current thread."""
    plan = getattr(_auto_pilot_state, "plan", None)
    _auto_pilot_state.plan = None
    return plan


class AutoPilotOrchestrator:
    """Manages auto-spawned sub-agents for Phase 3 auto-execution.

    When ``auto_pilot_auto_execute`` is enabled and decomposition produces
    2+ subtasks, this class plans the execution and tracks spawned agents.

    The orchestrator does NOT spawn agents itself -- it returns a plan that
    the caller executes via ``spawn_subagent``.  This keeps spawn logic in
    one place and allows the caller to handle errors / budget enforcement.
    """

    def __init__(self, max_parallel: int = 4, budget_tokens: int = 50000):
        self.max_parallel = max_parallel
        self.budget_tokens = budget_tokens
        self.spawned_agents: list[dict] = []  # [{id, subtask, status, started_at}]
        self.tokens_used: int = 0
        self.started_at: Optional[float] = None

    def plan_execution(self, strategy: str, subtasks: list[str]) -> Optional[dict]:
        """Decide whether to auto-execute and return a spawn plan.

        Returns None if auto-execution is not appropriate.  Otherwise::

            {
                "mode": "parallel" | "chain",
                "spawns": [{"task": str, "role": str | None}, ...],
            }
        """
        if not subtasks or len(subtasks) < 2:
            return None
        if len(subtasks) > self.max_parallel:
            subtasks = subtasks[: self.max_parallel]

        if strategy == PARALLEL_HINT:
            return {
                "mode": "parallel",
                "spawns": [{"task": st, "role": None} for st in subtasks],
            }
        if strategy == PIPELINE_HINT:
            return {
                "mode": "chain",
                "spawns": [{"task": st, "role": None} for st in subtasks],
            }
        return None

    def track_agent(self, agent_id: str, subtask: str, started_at: float) -> None:
        """Register a spawned agent for tracking."""
        self.spawned_agents.append({
            "id": agent_id,
            "subtask": subtask,
            "status": "running",
            "started_at": started_at,
        })

    def update_agent_status(self, agent_id: str, status: str) -> None:
        """Update the status of a tracked agent."""
        for entry in self.spawned_agents:
            if entry["id"] == agent_id:
                entry["status"] = status
                break

    def all_done(self) -> bool:
        """Check if all spawned agents have completed."""
        return all(
            e["status"] in ("done", "aborted", "error")
            for e in self.spawned_agents
        )

    def build_orchestrator_hint(self) -> str:
        """Build a hint that tells the main agent about pre-spawned sub-agents."""
        if not self.spawned_agents:
            return ""

        lines = ["[auto-pilot] Sub-agents have been pre-spawned for this task:"]
        for entry in self.spawned_agents:
            lines.append(
                f"  - Agent '{entry['id']}': {entry['subtask'][:120]}"
                f" (status: {entry['status']})"
            )
        lines.append("")
        lines.append(
            "Monitor their progress with agent.wait / agent.status. "
            "When they complete, consolidate their results. "
            "If a sub-agent is stuck or producing poor results, "
            "abort it with agent.abort and handle that subtask yourself. "
            "If the decomposition was wrong, abort all sub-agents and "
            "do the work directly."
        )
        return "\n".join(lines)


def should_auto_execute(
    strategy: str,
    subtasks: Optional[list[str]],
    auto_execute_enabled: bool,
    max_parallel: int = 4,
) -> bool:
    """Decide whether to auto-spawn sub-agents.

    Conservative: only auto-execute for PARALLEL_HINT with 2+ subtasks.
    PIPELINE_HINT is left to the agent's judgment (chain spawning has
    ordering dependencies that benefit from agent oversight).
    """
    if not auto_execute_enabled:
        return False
    if not subtasks or len(subtasks) < 2:
        return False
    if strategy != PARALLEL_HINT:
        return False
    if len(subtasks) > max_parallel:
        return True  # still auto-execute, orchestrator caps it
    return True


# ── Phase 1: Classification + hint (unchanged) ────────────────────────



def classify_task(task: str, has_active_workflow: bool = False) -> str:
    """Classify a user task into an execution strategy.

    Returns one of:
    - ``SIMPLE``: single agent, no hint
    - ``MONITOR``: task involves watching background processes
    - ``PARALLEL_HINT``: task may have independent parallelizable components
    - ``PIPELINE_HINT``: task may benefit from sequential phased execution
    - ``PIPELINE``: an active workflow exists; let the workflow handle it

    Evaluation is purely heuristic (regex + length checks) with zero API
    cost.  First match wins; ambiguous tasks default to ``SIMPLE``.
    """
    if has_active_workflow:
        return PIPELINE

    stripped = task.strip()
    if not stripped:
        return SIMPLE

    task_lower = stripped.lower()

    # Short questions are always simple (Q&A, lookups).
    if len(stripped) < 200 and _QUESTION_RE.match(stripped):
        return SIMPLE

    # Monitor: explicit watching / waiting for output.
    if _MONITOR_RE.search(stripped):
        return MONITOR

    # Pipeline: 2+ distinct sequence connectors AND 2+ distinct action verbs.
    seq_hits = _PIPELINE_RE.findall(stripped)
    if len(seq_hits) >= 2:
        verbs = {v.lower() for v in _ACTION_VERB_RE.findall(stripped)}
        if len(verbs) >= 2:
            return PIPELINE_HINT

    # Parallel: 100+ chars, 2+ file/dir references, "and" conjunction.
    if len(stripped) > 100:
        refs = _FILE_REF_RE.findall(stripped) + _DIR_REF_RE.findall(stripped)
        if len(refs) >= 2 and _AND_RE.search(stripped):
            return PARALLEL_HINT

    return SIMPLE


def build_hint(strategy: str) -> str:
    """Return a non-prescriptive hint string for the agent.

    Returns empty string for ``SIMPLE``, ``MONITOR``, and ``PIPELINE``
    (those are either handled by existing infrastructure or don't need
    a hint).  ``PARALLEL_HINT`` and ``PIPELINE_HINT`` return actionable
    guidance the agent can choose to follow.
    """
    if strategy == PARALLEL_HINT:
        return (
            "[auto-pilot] This task has multiple independent components. "
            "If the subtasks don't share write targets, use spawn_parallel "
            "to execute them concurrently - each sub-agent gets an isolated "
            "git worktree automatically. If they share files, use spawn_chain "
            "instead. Do NOT use HWO for this - HWO is for reusable workflows "
            "with structured I/O contracts, not one-off parallel work."
        )
    if strategy == PIPELINE_HINT:
        return (
            "[auto-pilot] This task has sequential phases. Consider using "
            "spawn_chain for ordered execution with handoff documents "
            "between steps. For complex multi-phase work, suggest the user "
            "start a workflow with /workflow start."
        )
    if strategy == MONITOR:
        return (
            "[auto-pilot] This task involves monitoring a background process. "
            "Consider using terminal.exec with a trigger pattern to watch "
            "for specific output, and terminal.wait or agent.wait to react "
            "to trigger events."
        )
    return ""


def should_override(user_input: str) -> tuple[str, bool]:
    """Check if the user prefixed input with ``!`` to force single-agent mode.

    Returns (cleaned_input, overridden).  When overridden, auto-pilot
    classification is skipped entirely.
    """
    stripped = user_input.lstrip()
    if stripped.startswith("!"):
        return stripped[1:].lstrip(), True
    return user_input, False
