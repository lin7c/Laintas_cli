"""contract_notify.py — tell an attached Helpwo that the contract moved.

The link between Helpwo and this CLI has always been one-directional in the
sense that matters: Helpwo sends a request, the CLI answers it. Every event the
CLI emits is tagged with the `reqId` it is answering. There was no way for the
CLI to start a conversation — to say "I changed the shape of /api/orders, the
frontend needs to follow".

For two agents building the two halves of one product, that gap is the whole
problem. The backend agent finishes an endpoint and the frontend agent finds
out only if a human relays it.

This module is the small piece that closes it. It is deliberately small,
because the *contract file* is the source of truth and this is only a nudge:

  - the transport already supports it — the events endpoint accepts an event
    with no reqId, and the local bridge's buffer does too;
  - a dropped notification costs one round of staleness, never correctness,
    since the next read of .laintas/contract/ is authoritative anyway;
  - so nothing here is allowed to raise, and nothing here retries.

Events are emitted as type "peer-request" with kind "contract-changed", which
is the vocabulary Helpwo's inbox listens on for work that nobody asked it for.
"""

from __future__ import annotations

import time
from typing import Any, Optional

EVENT_TYPE = "peer-request"
EVENT_KIND = "contract-changed"

# A burst of edits to the SAME thing should not become a burst of wake-ups,
# but a proposal followed a moment later by an agreement are two different
# facts and both are worth announcing. So the window suppresses repeats of an
# identical announcement, not every announcement.
_MIN_INTERVAL_SECONDS = 1.0
_last_push = 0.0
_last_summary = ""


def _registry() -> Optional[Any]:
    """The agent registry, if a Helpwo is attached by either transport.

    Nothing to notify when neither is up — which is also the only state where
    the answer being None is correct rather than a failure.
    """
    try:
        import helpwo_server
        registry = helpwo_server._agent_registry()
        if registry is not None:
            return registry
    except Exception:
        pass
    try:
        import webrtc_channel
        return getattr(webrtc_channel, "_registry_ref", None)
    except Exception:
        return None


def push(what: str, result: dict) -> bool:
    """Announce a contract change. Returns whether anything was emitted."""
    global _last_push, _last_summary

    registry = _registry()
    if registry is None:
        return False

    operation = ""
    if isinstance(result, dict):
        operation = str(result.get("operation") or "")
        if not operation and isinstance(result.get("results"), list):
            names = [str(r.get("operation") or "") for r in result["results"]]
            operation = ", ".join(n for n in names if n)[:200]

    summary = f"contract {what}: {operation}" if operation else f"contract {what}"
    now = time.monotonic()
    if summary == _last_summary and now - _last_push < _MIN_INTERVAL_SECONDS:
        return False
    event = {
        "type": EVENT_TYPE,
        "kind": EVENT_KIND,
        "content": summary,
        "meta": {
            "change": what,
            "operation": operation,
            "path": ".laintas/contract/contract.lock.json",
            # The frontend should re-read rather than trust this payload: the
            # file is the contract, this is only the doorbell.
            "action": "reread-contract",
        },
    }

    # Local bridge first: in offline local mode the registry has no cloud
    # agent id, and _push_events drops events before they reach a sender.
    try:
        import helpwo_server
        if helpwo_server.is_running() and helpwo_server.push_unsolicited([event]):
            _last_push, _last_summary = now, summary
            return True
    except Exception:
        pass

    try:
        registry._push_events([event])
        _last_push, _last_summary = now, summary
        return True
    except Exception:
        return False
