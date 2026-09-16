"""Deterministic routing decisions. No runtime imports or side effects."""
from dataclasses import dataclass
import re
from typing import Optional


@dataclass(frozen=True)
class RouteRequest:
    owner_id: str
    task: str
    request_id: str
    session_id: str = ""
    run_id: str = ""
    target_id: str = ""
    role: str = ""
    required_tools: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    allow_spawn: bool = True
    # Unstructured tasks cannot prove employee filesystem isolation. Route
    # them to the existing worktree-backed child runtime instead.
    reuse_employee: bool = False
    group_id: str = ""
    concurrency_limit: int = 0


@dataclass(frozen=True)
class Candidate:
    id: str
    parent_id: str
    role: str
    specialist_role: str
    tags: tuple[str, ...]
    allowed_tools: Optional[tuple[str, ...]]
    denied_tools: tuple[str, ...]
    busy: bool
    terminated: bool
    terminal_alive: bool


@dataclass(frozen=True)
class RouteDecision:
    action: str
    agent_id: str = ""
    reason: str = ""


def suggest_role(task: str) -> str:
    """Conservative specialization. Mixed/implementation tasks stay general.

    A role only narrows the existing tool policy; it never grants a tool.
    Anchoring to the requested action avoids routing 'fix the review UI' to
    a read-only reviewer just because it contains the word review.
    """
    text = task.strip().lower()
    if re.search(r"\b(?:and|then)\s+(?:fix|implement|write|modify)\b"
                 r"|\u5e76(?:\u4fee\u590d|\u4fee\u6539)|\u7136\u540e", text):
        return ""
    for role, pattern in (
        ("reviewer", r"^(?:review\b|audit\b|\u5ba1\u67e5|\u5ba1\u9605|\u5ba1\u6838)"),
        ("explorer", r"^(?:explore\b|trace\b|locate\b|inspect\b"
                      r"|\u68b3\u7406|\u5b9a\u4f4d|\u67e5\u627e)"),
    ):
        if re.search(pattern, text):
            return role
    return ""


def decide(request: RouteRequest, candidates: tuple[Candidate, ...], *,
           can_spawn: bool) -> RouteDecision:
    if not request.task.strip():
        return RouteDecision("reject", reason="Task cannot be empty.")
    eligible = []
    rejected = []
    for c in sorted(candidates, key=lambda item: item.id):
        if request.target_id and c.id != request.target_id:
            continue
        reason = ""
        if c.parent_id != request.owner_id:
            reason = "Agent is not a direct child of this manager."
        elif c.role not in {"pool", "deployed"}:
            reason = "Only persistent employees accept assignments."
        elif c.terminated or not c.terminal_alive:
            reason = "Agent or its deployment terminal has ended."
        elif c.busy:
            reason = "Agent already has active work."
        elif request.role and c.specialist_role != request.role:
            reason = "Specialist role does not match."
        elif not set(request.capability_tags).issubset(c.tags):
            reason = "Required capabilities are missing."
        elif (set(request.required_tools) & set(c.denied_tools)
              or (c.allowed_tools is not None
                  and not set(request.required_tools).issubset(c.allowed_tools))):
            reason = "Employee tool policy does not permit this task."
        if reason:
            rejected.append(reason)
        else:
            eligible.append(c)
    if eligible and (request.target_id or request.reuse_employee):
        return RouteDecision("assign", eligible[0].id,
                             "Direct child; available; role and tool requirements match.")
    if request.target_id:
        return RouteDecision("reject", reason=rejected[0] if rejected else "Agent not found.")
    if request.allow_spawn and can_spawn:
        return RouteDecision("spawn", reason="Use an isolated child run; preserve employee ownership.")
    return RouteDecision("parent", reason="No eligible execution target; keep this task with its parent.")
