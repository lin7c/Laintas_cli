"""Vendored copy of the agent_gateway context-compaction policy.

Source of truth: /root/agent_gateway/context/ (policy.json + adapter.py + summary_prompt.py).
Re-sync with: cp /root/agent_gateway/context/{policy.json,adapter.py,summary_prompt.py} context_policy/
Kept as a vendored copy because laintas_cli deploys independently of the
gateway. Do not edit policy.json here — edit it in agent_gateway and re-sync.

Provides the budget arithmetic + summary prompt that drive opencode-style
compaction; the compaction MECHANISM lives in agent_loop.py.
"""
from .adapter import (  # noqa: F401
    load,
    reload,
    estimate_tokens,
    usable_tokens,
    keep_recent_tokens,
    is_overflow,
    is_protected_tool,
    truncate_tool_output,
    read_retention,
    is_read_tool,
    is_edit_tool,
    repeat_stop,
)
from .summary_prompt import summary_prompt  # noqa: F401
