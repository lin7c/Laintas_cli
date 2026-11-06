"""approval_broker.py — one place where background Agents' approvals are asked.

Background Agents (``@name task`` assignments, /station work, triggers) run on
their own threads, but the terminal has one owner at a time. While the REPL is
idle at its prompt, prompt_toolkit holds the terminal, so an approval asked
from a worker thread used to time out on the arbiter and come back as a
denial nobody was asked about.

The broker fixes that by handing the question to whoever owns the terminal:

* the worker ``submit``s a request and wakes the main loop;
* the main loop, between prompts, ``claim``s the oldest request and asks it
  on the main thread (where the prompt lives);
* if nobody claims it — the REPL started a turn instead — the worker
  ``withdraw``s it and asks inline, the path that already works mid-turn.

``claim`` and ``withdraw`` are atomic against each other, so a request is
asked exactly once. ``pending_agents`` lets read-only views show "waiting for
approval" without being able to answer it.

Display-free: callers own all printing.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ApprovalRequest:
    agent_id: str
    title: str
    body: str
    question: str
    allow_always: bool = False
    destructive: bool = False
    created_at: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event)
    result: str = "no"
    claimed: bool = False
    withdrawn: bool = False


_lock = threading.Lock()
_queue: deque = deque()
# agent_id -> number of approvals that agent is currently waiting on, whoever
# ends up asking them (the main loop or the worker itself).
_waiting: dict = {}


def submit(request: ApprovalRequest) -> ApprovalRequest:
    with _lock:
        _queue.append(request)
    return request


def claim() -> Optional[ApprovalRequest]:
    """Take the oldest unclaimed request, or None."""
    with _lock:
        while _queue:
            request = _queue.popleft()
            if request.withdrawn:
                continue
            request.claimed = True
            return request
    return None


def withdraw(request: ApprovalRequest) -> bool:
    """Take a request back. False when the main loop already claimed it."""
    with _lock:
        if request.claimed:
            return False
        request.withdrawn = True
        try:
            _queue.remove(request)
        except ValueError:
            pass
        return True


def resolve(request: ApprovalRequest, result: str) -> None:
    request.result = str(result or "no")
    request.done.set()


def has_pending() -> bool:
    with _lock:
        return any(not r.withdrawn for r in _queue)


class waiting:
    """Context manager marking *agent_id* as waiting on an approval."""

    def __init__(self, agent_id: str):
        self.agent_id = str(agent_id or "")

    def __enter__(self):
        with _lock:
            _waiting[self.agent_id] = _waiting.get(self.agent_id, 0) + 1
        return self

    def __exit__(self, *_exc: Any) -> None:
        with _lock:
            count = _waiting.get(self.agent_id, 0) - 1
            if count > 0:
                _waiting[self.agent_id] = count
            else:
                _waiting.pop(self.agent_id, None)


def pending_agents() -> set:
    """Agent ids that are blocked on an approval right now."""
    with _lock:
        return set(_waiting)


def reset() -> None:
    """Tests only."""
    with _lock:
        _queue.clear()
        _waiting.clear()
