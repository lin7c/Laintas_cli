"""A branch: one delegated unit of work, supervised whether or not anyone waits.

Why this exists
---------------
The agent registry is a tree, and after 2026-08-28 that tree decides who may
talk to whom. What it never described is a RUN: "these six children are one
piece of work, this is their budget, this is who is watching them, and this is
how it ends". Four different groupings stood in for it — `group_id` written by
the parallel spawner, `batch_id` read back off `child_ids[0].group_id`,
`chain_id`/`chain_step_index`, and HWG's node queue — none of them an object
with state, budget or an owner.

The cost was measured, not theorised:

* Every piece of supervision — the per-child stall clock, the wrap-up nudge
  100s before cutoff, the partial-result rescue, the cut-off itself — lived
  inside `spawn_parallel`'s `wait=true` display loop. When the fan-out became
  asynchronous by default (so the parent could keep working), the supervision
  went with the display: an async child that wedges has no stall bound at all
  and runs until its own max_loops. Supervision had been coupled to the
  spinner.
* Nothing ends a run. `close_all_agents()` fires at CLI shutdown, child threads
  are daemons, so a parent that finishes with children still running leaves
  them burning tokens until the process exits. What stood in for a mechanism
  was a sentence in a prompt.
* A parent sees `Children: AI-3 [running]` in its live state, while the rich
  per-child view exists only inside the blocking barrier — visible exactly when
  the parent can no longer act on it.

So: the branch owns the supervision, the supervision runs on its own thread,
and the live table becomes a RENDERER of branch state rather than its host.

The closed loop
---------------
A branch closes only when every member has reached one of three outcomes:

    verified   the contract was checked and met (or there was no contract and
               the child returned cleanly)
    rejected   checked and not met, with the specific gaps
    aborted    stopped, with the reason and whatever partial result survived

There is no fourth. A member that is still `running` when the branch is asked
to close is aborted with a reason, because "still running" is not an outcome
anyone can act on.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

#: How long a member may show no observable progress before it is stopped.
#: Same number the display loop used, now applied to every branch rather than
#: only to the ones somebody happened to be watching.
STALL_SECONDS = 300.0

#: How long before its own cutoff a member is told to wrap up, so it hands back
#: a partial conclusion instead of being cut off with nothing.
WRAP_UP_LEAD_SECONDS = 100.0

#: Poll cadence of the supervisor. Fast enough that a settled member is noticed
#: promptly, slow enough that six branches cost nothing measurable.
POLL_SECONDS = 0.25

#: Queue-hold ceiling: waiting for a concurrency slot is not stalling, but a
#: slot that never frees must eventually be reported rather than waited on.
QUEUE_HOLD_MAX_SECONDS = 600.0

#: How long a branch may stay unsealed (its opener still adding members).
SEAL_TIMEOUT_SECONDS = 120.0

OUTCOME_VERIFIED = "verified"
OUTCOME_REJECTED = "rejected"
OUTCOME_ABORTED = "aborted"

STATUS_OPEN = "open"
STATUS_DRAINING = "draining"
STATUS_CLOSED = "closed"

_TERMINAL_AGENT_STATUS = frozenset({"done", "error", "aborted"})


@dataclass
class Member:
    """One child inside a branch, and everything the supervisor knows about it."""
    agent_id: str
    goal: str = ""
    started_at: float = field(default_factory=time.time)
    last_progress_at: float = field(default_factory=time.time)
    queued_since: float = field(default_factory=time.time)
    tool_calls: int = 0
    activity: tuple = ("", "starting…")
    seen_calls: set = field(default_factory=set)
    last_reply_seen: str = ""
    subtree_token: Optional[str] = None
    nudged: bool = False
    outcome: str = ""            # "" while running; one of the OUTCOME_* after
    detail: str = ""
    partial: str = ""
    settled_at: float = 0.0

    @property
    def settled(self) -> bool:
        return bool(self.outcome)

    def elapsed(self) -> float:
        return (self.settled_at or time.time()) - self.started_at


@dataclass
class Budget:
    """What a branch may spend. Absent limits mean "bounded by the members'
    own loop caps", which is what every batch had before."""
    wall_clock_max: float = 0.0        # 0 = no branch-level deadline
    stall_seconds: float = STALL_SECONDS


@dataclass
class Branch:
    branch_id: str
    owner_agent_id: str
    kind: str                          # parallel | chain | workflow_node | single
    members: dict = field(default_factory=dict)   # agent_id -> Member
    budget: Budget = field(default_factory=Budget)
    status: str = STATUS_OPEN
    opened_at: float = field(default_factory=time.time)
    closed_at: float = 0.0
    close_reason: str = ""
    interrupted: bool = False
    #: The runtime view this branch was opened against. Captured rather than
    #: read from the module global on every tick: a supervisor that resolves
    #: its runtime late acts on whatever binding exists WHEN IT WAKES, which is
    #: how a stale supervisor came to abort a member belonging to somebody
    #: else's registry.
    runtime: object = None
    #: False while the opener is still adding members. A fan-out registers its
    #: members as each child actually spawns, so a supervisor that applied
    #: "no open members means finished" to a branch that had not been filled
    #: yet closed it in the same millisecond it was created — measured live,
    #: with both children left running outside any branch.
    sealed: bool = True

    def open_members(self) -> list:
        return [m for m in self.members.values() if not m.settled]

    def ledger(self) -> list:
        """The branch's outcome, as data. This is what a caller acts on."""
        return [{
            "agent_id": m.agent_id,
            "outcome": m.outcome or "running",
            "detail": m.detail,
            "elapsed_seconds": round(m.elapsed(), 1),
            "tool_calls": m.tool_calls,
            **({"partial": m.partial} if m.partial else {}),
        } for m in self.members.values()]


_BRANCHES: dict = {}
_LOCK = threading.RLock()
#: Injected by agent_loop at import time so this module never imports it back
#: (agent_loop already imports half the runtime; a cycle here would make the
#: import order load-bearing).
_runtime = None


def bind_runtime(runtime) -> None:
    """Give the supervisor its view of the agent registry."""
    global _runtime
    _runtime = runtime


# ── Registry ───────────────────────────────────────────────────────────────

def open_branch(owner_agent_id: str, kind: str, members: list,
                budget: Optional[Budget] = None,
                supervise: bool = True) -> "Branch":
    """Register a branch and start supervising it.

    `members` is a list of (agent_id, goal) pairs or bare agent ids.
    """
    branch = Branch(
        branch_id=f"b-{uuid.uuid4().hex[:10]}",
        owner_agent_id=owner_agent_id,
        kind=kind,
        budget=budget or Budget(),
        runtime=_runtime,
    )
    for item in members:
        agent_id, goal = item if isinstance(item, (tuple, list)) else (item, "")
        branch.members[agent_id] = Member(agent_id=agent_id, goal=goal)
    branch.sealed = bool(branch.members)
    with _LOCK:
        _BRANCHES[branch.branch_id] = branch
    if supervise:
        # The open event is emitted from the supervisor, not from here: the
        # event log fsyncs and reads its own tail to number a new record, so on
        # a large log the caller's tool call would pay a second or more just to
        # announce that it started something. Opening a branch must be free to
        # the thread that opens it.
        thread = threading.Thread(target=_supervise, args=(branch,),
                                  name=f"branch-{branch.branch_id}", daemon=True)
        thread.start()
    else:
        _emit("branch_opened", branch, members=len(branch.members), kind=kind)
    return branch


def seal(branch_id: str) -> None:
    """The opener has added every member it is going to add."""
    found = get(branch_id)
    if found is not None:
        found.sealed = True


def get(branch_id: str) -> Optional["Branch"]:
    with _LOCK:
        return _BRANCHES.get(branch_id)


def branches_for(owner_agent_id: str, open_only: bool = False) -> list:
    with _LOCK:
        found = [b for b in _BRANCHES.values()
                 if b.owner_agent_id == owner_agent_id]
    return [b for b in found if b.status != STATUS_CLOSED] if open_only else found


def open_branches(owner_agent_id: str) -> list:
    return branches_for(owner_agent_id, open_only=True)


def forget_closed(max_kept: int = 50) -> int:
    """Bound the registry; closed branches are history, not state."""
    with _LOCK:
        closed = sorted((b for b in _BRANCHES.values()
                         if b.status == STATUS_CLOSED),
                        key=lambda b: b.closed_at)
        dropped = 0
        while len(closed) > max_kept:
            _BRANCHES.pop(closed.pop(0).branch_id, None)
            dropped += 1
        return dropped


# ── Supervision ────────────────────────────────────────────────────────────

def _supervise(branch: "Branch") -> None:
    """Watch one branch until every member has an outcome.

    Runs on its own thread precisely so that it does not depend on anyone
    waiting, rendering, or otherwise being interested. That dependency is what
    left asynchronous batches unsupervised.
    """
    deadline = (branch.opened_at + branch.budget.wall_clock_max
                if branch.budget.wall_clock_max else None)
    _emit("branch_opened", branch, members=len(branch.members), kind=branch.kind)
    while True:
        try:
            if branch.status == STATUS_CLOSED:
                return
            now = time.time()
            for member in branch.open_members():
                _poll_member(branch, member, now)
            if branch.sealed and not branch.open_members():
                close(branch.branch_id, "all members settled")
                return
            if not branch.sealed and now - branch.opened_at > SEAL_TIMEOUT_SECONDS:
                # The opener never finished filling it. Closing an empty branch
                # beats leaving one open forever, blocking its owner from ever
                # completing its own task.
                close(branch.branch_id, "opener never sealed the branch")
                return
            if deadline and now > deadline:
                _drain(branch, f"branch budget of "
                               f"{int(branch.budget.wall_clock_max)}s reached")
                return
            if _owner_gone(branch):
                _drain(branch, "the agent that opened this branch is gone")
                return
        except Exception:
            # A supervisor that dies takes the branch's only watchdog with it.
            pass
        time.sleep(POLL_SECONDS)


def _rt(branch: "Branch"):
    return branch.runtime or _runtime


def _owner_gone(branch: "Branch") -> bool:
    runtime = _rt(branch)
    if runtime is None:
        return False
    return runtime.get_agent(branch.owner_agent_id) is None


def _poll_member(branch: "Branch", member: Member, now: float) -> None:
    """One member, one tick: observe progress, nudge, cut off, or settle."""
    runtime = _rt(branch)
    if runtime is None:
        return
    info = runtime.get_agent(member.agent_id)
    if info is None:
        _settle(branch, member, OUTCOME_ABORTED, "agent disappeared")
        return

    _observe(runtime, member, info, now)

    if info.status in _TERMINAL_AGENT_STATUS:
        _settle_from_agent(branch, member, info)
        return

    # Not stalling: a person is deciding, the caller is being asked, or the
    # member is queued behind the concurrency cap and cannot progress by
    # definition. Killing a member for any of these fires the watchdog hardest
    # exactly where it is least deserved.
    if runtime.is_blocked_on_a_decision(member.agent_id):
        member.last_progress_at = now
        member.nudged = False
        return
    if (info.status == "queued"
            and now - member.queued_since < QUEUE_HOLD_MAX_SECONDS):
        member.last_progress_at = now
        return
    if info.status != "queued":
        member.queued_since = now

    stall_at = member.last_progress_at + branch.budget.stall_seconds
    if not member.nudged and now >= stall_at - WRAP_UP_LEAD_SECONDS:
        member.nudged = True
        runtime.send_to_agent(member.agent_id, {
            "from": "branch",
            "kind": "budget_warning",
            "text": (f"You have shown no progress for a while and will be "
                     f"stopped in ~{int(WRAP_UP_LEAD_SECONDS)}s if that does "
                     f"not change. Stop opening new exploration now and reply "
                     f"with the best conclusion you can support from what you "
                     f"have already found — a partial, honest answer beats "
                     f"being cut off with nothing."),
        })
    if now >= stall_at:
        _cut_off(branch, member,
                 f"no observable progress for {int(branch.budget.stall_seconds)}s")


def _observe(runtime, member: Member, info, now: float) -> None:
    """Update the member's activity line and progress clock.

    Progress is any of: a tool call that finished, a tool call that STARTED
    (a five-minute test run is working, not wedged), new reply text (a member
    reasoning its way to a written conclusion calls no tools), or movement
    anywhere in its own subtree (a delegating member shows no signs of its own
    — its children's progress is its progress).
    """
    state = info.state or {}
    history = list(state.get("terminalHistory") or [])
    history.extend(state.get("_pending_history") or [])
    for row in history:
        if not isinstance(row, dict):
            continue
        call_id = row.get("call_id", "")
        if not call_id or call_id in member.seen_calls:
            continue
        member.seen_calls.add(call_id)
        member.tool_calls += 1
        member.activity = (row.get("tool", "?"),
                           str(row.get("command") or "").strip())
        member.last_progress_at = now
        member.nudged = False

    active = state.get("_active_tool")
    if isinstance(active, dict):
        started = float(active.get("started") or 0.0)
        if started > member.last_progress_at:
            member.last_progress_at = started
            member.activity = (str(active.get("name") or ""),
                               str(active.get("arg") or ""))
            member.nudged = False

    reply = str(state.get("lastReply") or "").strip()
    if reply and reply != member.last_reply_seen:
        member.last_reply_seen = reply
        member.last_progress_at = now
        member.nudged = False

    if runtime is not None:
        token = runtime.subtree_progress_token(member.agent_id)
        if token != member.subtree_token:
            if member.subtree_token is not None:
                member.last_progress_at = now
                member.nudged = False
            member.subtree_token = token


# ── Settling ───────────────────────────────────────────────────────────────

def _settle(branch: "Branch", member: Member, outcome: str, detail: str,
            partial: str = "") -> None:
    if member.settled:
        return
    member.outcome = outcome
    member.detail = detail
    member.partial = partial
    member.settled_at = time.time()
    _emit("member_settled", branch, agent_id=member.agent_id,
          outcome=outcome, detail=detail[:300],
          elapsed=round(member.elapsed(), 1), tool_calls=member.tool_calls)


def _settle_from_agent(branch: "Branch", member: Member, info) -> None:
    """Translate the agent's own end state into a branch outcome."""
    stage = getattr(info, "stage", "") or ""
    if info.status == "aborted":
        _settle(branch, member, OUTCOME_ABORTED,
                info.error or "aborted", _partial_of(info))
        return
    if stage == "rejected" or (getattr(info, "verification", None)
                               and not info.verification.get("ok", True)):
        gaps = "; ".join((info.verification or {}).get("gaps") or [])[:400]
        _settle(branch, member, OUTCOME_REJECTED,
                gaps or "contract not satisfied", _partial_of(info))
        return
    if info.status == "error":
        _settle(branch, member, OUTCOME_ABORTED, info.error or "error",
                _partial_of(info))
        return
    _settle(branch, member, OUTCOME_VERIFIED,
            "contract satisfied" if getattr(info, "contract", None) else "returned")


def _partial_of(info) -> str:
    for candidate in ((info.state or {}).get("lastReply"), info.last_reply,
                      info.result):
        text = str(candidate or "").strip()
        if text and text not in ("(interrupted)", "(no reply)"):
            return text[:1200]
    return ""


def _cut_off(branch: "Branch", member: Member, reason: str) -> None:
    """Stop a member that will not finish, keeping whatever it had said.

    The snapshot happens BEFORE the abort, because abort takes the agent apart
    and the wrap-up nudge exists precisely so there is a real partial
    conclusion sitting there to rescue.
    """
    partial = ""
    runtime = _rt(branch)
    if runtime is not None:
        info = runtime.get_agent(member.agent_id)
        if info is not None:
            partial = _partial_of(info)
        try:
            runtime.abort_agent(member.agent_id)
        except Exception:
            pass
    _settle(branch, member, OUTCOME_ABORTED, reason, partial)


def drain(branch_id: str, reason: str) -> Optional["Branch"]:
    """Public: stop every member that has not settled, then close."""
    branch = get(branch_id)
    if branch is None:
        return None
    _drain(branch, reason)
    return branch


def _drain(branch: "Branch", reason: str) -> None:
    branch.status = STATUS_DRAINING
    for member in branch.open_members():
        _cut_off(branch, member, reason)
    close(branch.branch_id, reason)


def close(branch_id: str, reason: str) -> Optional["Branch"]:
    branch = get(branch_id)
    if branch is None or branch.status == STATUS_CLOSED:
        return branch
    for member in branch.open_members():
        # "Still running" is not an outcome anyone can act on.
        _cut_off(branch, member, f"branch closed: {reason}")
    branch.status = STATUS_CLOSED
    branch.closed_at = time.time()
    branch.close_reason = reason
    _emit("branch_closed", branch, reason=reason,
          elapsed=round(branch.closed_at - branch.opened_at, 1),
          outcomes={o: sum(1 for m in branch.members.values() if m.outcome == o)
                    for o in (OUTCOME_VERIFIED, OUTCOME_REJECTED, OUTCOME_ABORTED)})
    forget_closed()
    return branch


def interrupt(branch_id: str) -> None:
    """User interrupt: mark it so the report says interrupted, then drain."""
    branch = get(branch_id)
    if branch is None:
        return
    branch.interrupted = True
    _drain(branch, "interrupted")


# ── Reporting ──────────────────────────────────────────────────────────────

def status_report(branch: "Branch") -> dict:
    """What the owner needs to decide, as data."""
    return {
        "branch_id": branch.branch_id,
        "kind": branch.kind,
        "status": branch.status,
        "elapsed_seconds": round(
            (branch.closed_at or time.time()) - branch.opened_at, 1),
        "open_members": [m.agent_id for m in branch.open_members()],
        "members": branch.ledger(),
    }


def summarize_open(owner_agent_id: str) -> str:
    """One compact block for the owner's live state, every turn.

    The rich per-member view used to exist only inside the blocking barrier —
    available exactly when its reader could no longer act on it.
    """
    branches = open_branches(owner_agent_id)
    if not branches:
        return ""
    lines = []
    for branch in branches:
        open_ids = branch.open_members()
        lines.append(f"{branch.branch_id} ({branch.kind}, "
                     f"{int(time.time() - branch.opened_at)}s): "
                     f"{len(open_ids)} running / {len(branch.members)} total")
        for member in branch.members.values():
            if member.settled:
                lines.append(f"  {member.agent_id}: {member.outcome}"
                             f" — {member.detail[:80]}")
            else:
                tool, arg = member.activity
                lines.append(f"  {member.agent_id}: running "
                             f"{int(member.elapsed())}s, {member.tool_calls} tools"
                             + (f", on {tool} {arg[:60]}" if tool else ""))
    return "\n".join(lines)


def _emit(event: str, branch: "Branch", **fields) -> None:
    try:
        import event_log
        event_log.append(event, branch_id=branch.branch_id,
                         owner=branch.owner_agent_id, **fields)
    except Exception:
        pass
