"""Vendored copy of the agent_gateway fault-tolerant edit ("apply_patch") logic.

Source of truth: /root/agent_gateway/patch/ (adapter.py).
Re-sync with: cp /root/agent_gateway/patch/adapter.py patch_adapter/
Pure fuzzy-matching only — the file I/O stays in fs.edit. Do not edit here;
edit it in agent_gateway and re-sync. Ported from opencode's edit replacers.
"""
from .adapter import apply_edit  # noqa: F401
