"""Structured multi-phase workflow engine for laintas_cli.

Inspired by Claude Code's feature-dev 7-phase workflow, this engine manages
structured workflows with distinct phases, each having its own system prompt
injection, allowed tools, user confirmation requirements, and auto-spawned
agent roles.

Workflows are activated via `/workflow start <name> "description"` and drive
the agent loop through sequential phases. Phase transitions happen when:
  - The AI signals done=true with a phase summary
  - The user confirms (for phases with requires_user_input=True)
  - The engine auto-advances (for phases with exit_condition="auto")

Integration points:
  - agent_loop.py reads get_active_workflow() for {{workflowPhase}} injection
  - _build_user_message() includes <workflow_phase> XML section
  - laintas_cli.py handles /workflow meta-commands
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Data Classes ────────────────────────────────────────────────────────

@dataclass
class WorkflowPhase:
    """Definition of a single workflow phase."""
    name: str
    description: str
    system_prompt_inject: str       # guidance injected into {{workflowPhase}}
    allowed_tools: list[str] = field(default_factory=list)  # empty = all tools
    requires_user_input: bool = False  # pause for user confirmation
    spawn_agents: list[str] = field(default_factory=list)   # auto-spawned role names
    exit_condition: str = "auto"    # "auto" | "user_confirm" | "done_signal"
    max_loops_override: int = 0     # 0 = use default max_loops


@dataclass
class WorkflowInstance:
    """A running workflow instance with state tracking."""
    name: str                       # workflow template name
    description: str                # user's task description
    phases: list[WorkflowPhase]
    current_phase: int = 0
    phase_states: dict = field(default_factory=dict)  # per-phase accumulated data
    started_at: float = field(default_factory=time.time)
    phase_started_at: float = field(default_factory=time.time)
    completed: bool = False
    summary: str = ""

    @property
    def current(self) -> Optional[WorkflowPhase]:
        if 0 <= self.current_phase < len(self.phases):
            return self.phases[self.current_phase]
        return None

    @property
    def progress_str(self) -> str:
        total = len(self.phases)
        cur = self.current_phase + 1 if not self.completed else total
        names = " → ".join(
            f"**{p.name}**" if i == self.current_phase and not self.completed else p.name
            for i, p in enumerate(self.phases)
        )
        return f"Phase {cur}/{total}: {names}"


# ── Built-in Workflow Definitions ───────────────────────────────────────

def _feature_dev_phases() -> list[WorkflowPhase]:
    return [
        WorkflowPhase(
            name="discover",
            description="Understand what needs to be built",
            system_prompt_inject=(
                "## Current Phase: DISCOVER\n"
                "Understand the task requirements. Ask clarifying questions if the "
                "feature is unclear. Summarize your understanding of:\n"
                "- What problem is being solved\n"
                "- What the feature should do\n"
                "- Any constraints or requirements\n"
                "When ready, set done=true and provide a clear task summary."
            ),
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="explore",
            description="Understand existing codebase patterns",
            system_prompt_inject=(
                "## Current Phase: EXPLORE\n"
                "Deeply explore the codebase to understand:\n"
                "- Existing patterns and conventions\n"
                "- Similar features and their implementation\n"
                "- Architecture layers and module boundaries\n"
                "- Key files and entry points\n"
                "Consider spawning explorer sub-agents for parallel exploration.\n"
                "When ready, set done=true with a summary of findings and key files."
            ),
            allowed_tools=[
                "fs.read", "fs.ls", "fs.grep", "fs.glob",
                "shell.exec", "web.search", "web.fetch",
                "agent.spawn", "agent.tell", "agent.wait", "agent.list",
                "task.create", "task.update", "task.list",
            ],
            spawn_agents=["explorer"],
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="clarify",
            description="Resolve ambiguities before designing",
            system_prompt_inject=(
                "## Current Phase: CLARIFY\n"
                "Review findings and identify all ambiguities, edge cases, and "
                "underspecified behaviors. Present specific questions to the user.\n"
                "**DO NOT proceed to design until all questions are answered.**\n"
                "Wait for user responses, then set done=true with clarified requirements."
            ),
            requires_user_input=True,
            exit_condition="user_confirm",
        ),
        WorkflowPhase(
            name="architect",
            description="Design implementation approach",
            system_prompt_inject=(
                "## Current Phase: ARCHITECT\n"
                "Design the complete implementation architecture:\n"
                "- Component design with file paths and responsibilities\n"
                "- Data flow from entry points to outputs\n"
                "- Integration points with existing code\n"
                "- Build sequence (phased implementation steps)\n"
                "Consider spawning architect sub-agents for different approaches.\n"
                "Present your design to the user and wait for approval.\n"
                "Set done=true when architecture is approved."
            ),
            allowed_tools=[
                "fs.read", "fs.ls", "fs.grep", "fs.glob",
                "shell.exec", "web.search", "web.fetch",
                "agent.spawn", "agent.tell", "agent.wait", "agent.list",
                "plan.read", "plan.update", "plan.list",
                "task.create", "task.update", "task.list",
            ],
            spawn_agents=["architect"],
            requires_user_input=True,
            exit_condition="user_confirm",
        ),
        WorkflowPhase(
            name="implement",
            description="Build the feature",
            system_prompt_inject=(
                "## Current Phase: IMPLEMENT\n"
                "Implement the feature following the approved architecture:\n"
                "- Read all relevant files identified in previous phases\n"
                "- Follow codebase conventions strictly\n"
                "- Write clean, well-structured code\n"
                "- Update task progress as you work\n"
                "Set done=true when implementation is complete."
            ),
            exit_condition="done_signal",
            max_loops_override=50,  # allow more loops for implementation
        ),
        WorkflowPhase(
            name="review",
            description="Review code quality",
            system_prompt_inject=(
                "## Current Phase: REVIEW\n"
                "Review the implemented code for quality:\n"
                "- Spawn reviewer sub-agents for different aspects\n"
                "- Check for bugs, style compliance, silent failures\n"
                "- Consolidate findings and identify high-priority issues\n"
                "- Present findings to the user\n"
                "Set done=true with a review summary."
            ),
            allowed_tools=[
                "fs.read", "fs.ls", "fs.grep", "fs.glob",
                "shell.exec",
                "agent.spawn", "agent.tell", "agent.wait", "agent.list",
                "task.update", "task.list",
            ],
            spawn_agents=["reviewer", "silent-failure-hunter"],
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="summarize",
            description="Document what was accomplished",
            system_prompt_inject=(
                "## Current Phase: SUMMARIZE\n"
                "Provide a final summary including:\n"
                "- What was built\n"
                "- Key decisions made\n"
                "- Files modified (list all)\n"
                "- Suggested next steps\n"
                "Set done=true with the complete summary."
            ),
            exit_condition="done_signal",
        ),
    ]


def _bug_fix_phases() -> list[WorkflowPhase]:
    return [
        WorkflowPhase(
            name="reproduce",
            description="Reproduce and understand the bug",
            system_prompt_inject=(
                "## Current Phase: REPRODUCE\n"
                "Understand and reproduce the bug:\n"
                "- Read error messages and stack traces\n"
                "- Trace the failing code path\n"
                "- Identify the root cause hypothesis\n"
                "Set done=true with bug description and root cause hypothesis."
            ),
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="diagnose",
            description="Identify the exact root cause",
            system_prompt_inject=(
                "## Current Phase: DIAGNOSE\n"
                "Pinpoint the exact root cause:\n"
                "- Read all relevant code paths\n"
                "- Check for related issues (similar patterns elsewhere)\n"
                "- Formulate a precise fix strategy\n"
                "Set done=true with exact root cause and fix plan."
            ),
            allowed_tools=[
                "fs.read", "fs.ls", "fs.grep", "fs.glob",
                "shell.exec", "web.search", "web.fetch",
                "agent.spawn", "agent.tell", "agent.wait",
            ],
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="fix",
            description="Implement the fix",
            system_prompt_inject=(
                "## Current Phase: FIX\n"
                "Implement the fix:\n"
                "- Apply minimal, targeted changes\n"
                "- Follow existing code patterns\n"
                "- Add regression test if applicable\n"
                "Set done=true when fix is applied."
            ),
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="verify",
            description="Verify the fix works",
            system_prompt_inject=(
                "## Current Phase: VERIFY\n"
                "Verify the fix:\n"
                "- Re-run the failing scenario\n"
                "- Check for regressions\n"
                "- Review the fix for edge cases\n"
                "Set done=true with verification results."
            ),
            exit_condition="done_signal",
        ),
    ]


def _code_review_phases() -> list[WorkflowPhase]:
    return [
        WorkflowPhase(
            name="analyze",
            description="Analyze code changes",
            system_prompt_inject=(
                "## Current Phase: ANALYZE\n"
                "Analyze the code changes:\n"
                "- Read all modified files\n"
                "- Understand the intent of changes\n"
                "- Spawn specialized reviewers in parallel\n"
                "Set done=true when analysis is complete."
            ),
            allowed_tools=[
                "fs.read", "fs.ls", "fs.grep", "fs.glob",
                "shell.exec",
                "agent.spawn", "agent.tell", "agent.wait", "agent.list",
            ],
            spawn_agents=["reviewer", "silent-failure-hunter", "tester"],
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="report",
            description="Present findings",
            system_prompt_inject=(
                "## Current Phase: REPORT\n"
                "Present consolidated review findings:\n"
                "- Group by severity (Critical / Important / Minor)\n"
                "- Include confidence scores\n"
                "- Provide specific fix suggestions\n"
                "Set done=true with the report."
            ),
            exit_condition="done_signal",
        ),
        WorkflowPhase(
            name="suggest",
            description="Provide improvement suggestions",
            system_prompt_inject=(
                "## Current Phase: SUGGEST\n"
                "Provide actionable improvement suggestions:\n"
                "- Prioritized list of recommended changes\n"
                "- Optional: spawn simplifier agent for code improvements\n"
                "Set done=true with final suggestions."
            ),
            exit_condition="done_signal",
        ),
    ]


# ── Workflow Template Registry ──────────────────────────────────────────

_WORKFLOW_TEMPLATES: dict[str, callable] = {
    "feature-dev": _feature_dev_phases,
    "bug-fix": _bug_fix_phases,
    "code-review": _code_review_phases,
}


def list_workflow_templates() -> list[str]:
    """Return available workflow template names."""
    return sorted(_WORKFLOW_TEMPLATES.keys())


# ── Active Workflow State ───────────────────────────────────────────────

_active_workflow: Optional[WorkflowInstance] = None


def start_workflow(name: str, description: str) -> Optional[WorkflowInstance]:
    """Start a new workflow instance. Returns None if template not found."""
    global _active_workflow
    factory = _WORKFLOW_TEMPLATES.get(name)
    if factory is None:
        return None
    phases = factory()
    wf = WorkflowInstance(
        name=name,
        description=description,
        phases=phases,
        phase_states={p.name: {} for p in phases},
    )
    _active_workflow = wf
    return wf


def get_active_workflow() -> Optional[WorkflowInstance]:
    """Return the currently active workflow, or None."""
    return _active_workflow


def advance_phase(summary: str = "") -> Optional[WorkflowPhase]:
    """Advance to the next phase. Returns the new current phase, or None if completed."""
    global _active_workflow
    wf = _active_workflow
    if wf is None or wf.completed:
        return None

    # Save phase summary
    current = wf.current
    if current:
        wf.phase_states[current.name] = {
            "summary": summary,
            "completed_at": time.time(),
            "duration_s": time.time() - wf.phase_started_at,
        }

    # Advance
    wf.current_phase += 1
    wf.phase_started_at = time.time()

    if wf.current_phase >= len(wf.phases):
        wf.completed = True
        wf.summary = summary
        return None

    return wf.current


def set_phase_data(key: str, value) -> None:
    """Store data in the current phase's state."""
    wf = _active_workflow
    if wf is None or wf.current is None:
        return
    phase_name = wf.current.name
    if phase_name not in wf.phase_states:
        wf.phase_states[phase_name] = {}
    wf.phase_states[phase_name][key] = value


def end_workflow(summary: str = "") -> None:
    """End the active workflow."""
    global _active_workflow
    if _active_workflow:
        _active_workflow.completed = True
        _active_workflow.summary = summary
    _active_workflow = None


def render_workflow_section() -> str:
    """Render the workflow phase section for the system prompt / user message.

    Returns empty string if no active workflow.
    """
    wf = _active_workflow
    if wf is None or wf.completed:
        return ""

    current = wf.current
    if current is None:
        return ""

    lines = [
        f"Active Workflow: {wf.name} — {wf.description}",
        f"Progress: {wf.progress_str}",
        "",
        current.system_prompt_inject,
    ]

    # Show completed phase summaries for context
    completed = []
    for i in range(wf.current_phase):
        phase = wf.phases[i]
        ps = wf.phase_states.get(phase.name, {})
        summary = ps.get("summary", "")
        if summary:
            completed.append(f"  [{phase.name}]: {summary[:200]}")
    if completed:
        lines.append("")
        lines.append("### Completed Phases")
        lines.extend(completed)

    # Show allowed tools for this phase
    if current.allowed_tools:
        lines.append("")
        lines.append(f"### Allowed Tools This Phase")
        lines.append(f"  {', '.join(current.allowed_tools)}")
        lines.append("  (other tools are blocked during this phase)")

    return "\n".join(lines)


def is_tool_allowed_in_workflow(tool_name: str) -> bool:
    """Check if a tool is allowed in the current workflow phase.

    Returns True if no active workflow or the phase has no tool restrictions.
    """
    wf = _active_workflow
    if wf is None or wf.completed:
        return True
    current = wf.current
    if current is None:
        return True
    if not current.allowed_tools:
        return True
    return tool_name in current.allowed_tools


def get_phase_max_loops() -> int:
    """Return the max_loops override for the current phase, or 0 for default."""
    wf = _active_workflow
    if wf is None or wf.completed:
        return 0
    current = wf.current
    if current is None:
        return 0
    return current.max_loops_override


def get_auto_spawn_roles() -> list[str]:
    """Return the roles that should be auto-spawned for the current phase."""
    wf = _active_workflow
    if wf is None or wf.completed:
        return []
    current = wf.current
    if current is None:
        return []
    return list(current.spawn_agents)
