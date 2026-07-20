"""Auto-Pilot: heuristic task classification and hint injection.

Layer 1 of the progressive auto-pilot system.  Pure heuristic classification
with zero API cost.  Determines whether a user task would benefit from
parallel agents, sequential pipeline, or background monitoring, and injects
a non-prescriptive hint into the agent's context.

The agent retains full autonomy: hints are suggestions, not commands.
Only ``!`` prefix (user override) forces single-agent mode.
"""

import re
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
            "If the subtasks don't share write targets, consider using "
            "spawn_parallel to execute them concurrently — each sub-agent "
            "gets an isolated git worktree automatically. If they share "
            "files, use spawn_chain instead."
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
