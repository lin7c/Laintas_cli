"""Thread-safe observable event store for the full-screen Agents Mode.

Execution code publishes immutable semantic events here.  The store performs
no terminal I/O; prompt_toolkit remains the sole renderer.
"""

from __future__ import annotations

from collections import defaultdict, deque
import copy
from dataclasses import dataclass, field
import re
import threading
import time
import uuid
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class AgentUIEvent:
    seq: int
    event_id: str
    timestamp: float
    monotonic_time: float
    event_type: str
    agent_id: str = ""
    parent_agent_id: str = ""
    target_agent_id: str = ""
    terminal_name: str = ""
    run_id: str = ""
    step_id: str = ""
    tool_call_id: str = ""
    summary: str = ""
    detail: str = ""
    status: str = ""
    data: dict[str, Any] = field(default_factory=dict)


_ATTENTION_TYPES = frozenset({
    "agent_message", "agent_message_failed", "agent_done", "agent_error",
    "approval_requested", "input_rejected", "step_failed", "node_failed",
    "workflow_paused",
})

_DETAIL_LIMIT = 50_000
_DATA_STRING_LIMIT = 10_000
_DATA_COLLECTION_LIMIT = 200
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _sanitize_text(value: Any) -> str:
    text = _ANSI_ESCAPE_RE.sub("", str(value or ""))
    return "".join(
        char for char in text
        if char in "\n\r\t" or (ord(char) >= 32 and ord(char) != 127)
    )


def _one_line(value: Any, limit: int = 180) -> str:
    text = " ".join(_sanitize_text(value).split())
    return text if len(text) <= limit else text[:max(1, limit - 1)] + "…"


def _bounded_text(value: Any, limit: int) -> str:
    text = _sanitize_text(value)
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 20)] + "\n… [truncated]"


def _bounded_data(value: Any, depth: int = 0) -> Any:
    """Detach event metadata from callers and cap retained memory."""
    if depth >= 6:
        return _bounded_text(value, _DATA_STRING_LIMIT)
    if isinstance(value, dict):
        items = list(value.items())[:_DATA_COLLECTION_LIMIT]
        result = {
            str(key): _bounded_data(item, depth + 1)
            for key, item in items
        }
        if len(value) > len(items):
            result["_truncated_items"] = len(value) - len(items)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_DATA_COLLECTION_LIMIT]
        result = [_bounded_data(item, depth + 1) for item in items]
        if len(value) > len(items):
            result.append(f"… {len(value) - len(items)} item(s) truncated")
        return result
    if isinstance(value, str):
        return _bounded_text(value, _DATA_STRING_LIMIT)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return copy.deepcopy(value)
    except Exception:
        return _bounded_text(value, _DATA_STRING_LIMIT)


def _bounded_event_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {"value": value}
    bounded = _bounded_data(value)
    return bounded if isinstance(bounded, dict) else {"value": bounded}


class AgentUIEventHub:
    """Globally ordered bounded event log with per-Agent indexes."""

    def __init__(self, max_events: int = 10_000, per_agent: int = 3_000):
        self.max_events = max(100, int(max_events))
        self.per_agent = max(50, int(per_agent))
        self._lock = threading.RLock()
        self._seq = 0
        self._events: deque[AgentUIEvent] = deque(maxlen=self.max_events)
        self._agent_events: dict[str, deque[AgentUIEvent]] = defaultdict(
            lambda: deque(maxlen=self.per_agent))
        self._subscribers: set[Callable[[AgentUIEvent], None]] = set()

    def subscribe(self, callback: Callable[[AgentUIEvent], None]) -> None:
        with self._lock:
            self._subscribers.add(callback)

    def unsubscribe(self, callback: Callable[[AgentUIEvent], None]) -> None:
        with self._lock:
            self._subscribers.discard(callback)

    def reset(self) -> None:
        """Clear buffered UI state without disturbing live subscribers."""
        with self._lock:
            self._seq = 0
            self._events.clear()
            self._agent_events.clear()

    def forget_agent(self, agent_id: str) -> None:
        """Drop a terminated Agent's per-Agent event index so it cannot leak.

        The global _events deque is bounded, but _agent_events keeps one bounded
        deque per distinct agent_id for the whole process lifetime;
        agent_loop.unregister_agent calls this when a sub-agent is removed. The
        global ordered log is left intact (it is already bounded by maxlen).
        """
        with self._lock:
            self._agent_events.pop(str(agent_id or ""), None)

    def emit(self, event_type: str, **values: Any) -> AgentUIEvent:
        with self._lock:
            self._seq += 1
            event = AgentUIEvent(
                seq=self._seq,
                event_id=str(values.pop("event_id", "") or uuid.uuid4().hex),
                timestamp=float(values.pop("timestamp", 0.0) or time.time()),
                monotonic_time=time.monotonic(),
                event_type=str(event_type or "event"),
                agent_id=str(values.pop("agent_id", "") or ""),
                parent_agent_id=str(values.pop("parent_agent_id", "") or ""),
                target_agent_id=str(values.pop("target_agent_id", "") or ""),
                terminal_name=str(values.pop("terminal_name", "") or ""),
                run_id=str(values.pop("run_id", "") or ""),
                step_id=str(values.pop("step_id", "") or ""),
                tool_call_id=str(values.pop("tool_call_id", "") or ""),
                summary=_one_line(values.pop("summary", "")),
                detail=_bounded_text(values.pop("detail", ""), _DETAIL_LIMIT),
                status=str(values.pop("status", "") or ""),
                data=_bounded_event_data(values.pop("data", {}) or {}),
            )
            self._events.append(event)
            indexed = {event.agent_id, event.target_agent_id} - {""}
            for agent_id in indexed:
                self._agent_events[agent_id].append(event)
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass
        return event

    def ingest(self, agent_id: str, events: Iterable[dict],
               terminal_name: str = "") -> list[AgentUIEvent]:
        result = []
        for raw in events or ():
            item = dict(raw) if isinstance(raw, dict) else {"content": str(raw)}
            kind = str(item.get("type") or item.get("event_type") or "event")
            system_kind = str(item.get("kind") or "") if kind == "system" else ""
            if system_kind == "tool":
                kind = "tool_finished"
            elif system_kind == "output":
                kind = "tool_output"
            content = item.get("content")
            summary = (item.get("summary") or item.get("message") or content
                       or item.get("command") or item.get("stepId")
                       or item.get("node") or kind)
            detail = item.get("detail") or item.get("output") or content or ""
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            if kind in {"tool", "tool_finished"}:
                salient = _one_line(meta.get("salient"), 120)
                summary = f"{content or 'tool'}  {salient}".rstrip()
                detail = ""
            elif kind == "tool_started":
                summary = f"{item.get('name') or content or 'tool'}  " \
                          f"{_one_line(item.get('command'), 120)}".rstrip()
                detail = ""
            effective_agent_id = str(item.get("agentId") or item.get("agent_id")
                                     or agent_id or "")
            effective_terminal = str(
                item.get("terminalName") or terminal_name or "")
            if not effective_terminal and effective_agent_id:
                # Workflow runners intentionally know nothing about terminal
                # topology. Resolve it here so an unlabelled HWO/HWG event can
                # never leak into every terminal's feed.
                try:
                    import agent_loop
                    effective_terminal = str(agent_loop.agent_scope_terminal(
                        agent_loop.get_agent(effective_agent_id)) or "")
                except Exception:
                    effective_terminal = ""
            result.append(self.emit(
                kind,
                agent_id=effective_agent_id,
                parent_agent_id=str(item.get("parentAgentId") or ""),
                target_agent_id=str(item.get("toAgentId") or ""),
                terminal_name=effective_terminal,
                run_id=str(item.get("runId") or ""),
                step_id=str(item.get("stepId") or item.get("node") or ""),
                tool_call_id=str(item.get("toolCallId") or meta.get("call_id") or ""),
                summary=summary,
                detail=detail,
                status=str(item.get("status") or ""),
                data=item,
            ))
        return result

    def events(self, terminal_name: str = "", limit: int = 500
               ) -> list[AgentUIEvent]:
        with self._lock:
            rows = list(self._events)
        if terminal_name:
            rows = [row for row in rows
                    if row.terminal_name == terminal_name
                    or terminal_name in {
                        str(row.data.get("sourceTerminalName") or ""),
                        str(row.data.get("targetTerminalName") or ""),
                    }]
        return rows[-max(1, int(limit)):]

    def agent_events(self, agent_id: str, limit: int = 1000
                     ) -> list[AgentUIEvent]:
        with self._lock:
            rows = list(self._agent_events.get(agent_id, ()))
        return rows[-max(1, int(limit)):]

    @staticmethod
    def needs_attention(event: AgentUIEvent) -> bool:
        return event.event_type in _ATTENTION_TYPES


hub = AgentUIEventHub()
