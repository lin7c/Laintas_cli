"""Vendored copy of the agent_gateway code-formatter registry + picker.

Source of truth: /root/agent_gateway/format/ (registry.json + adapter.py).
Re-sync with: cp /root/agent_gateway/format/{registry.json,adapter.py} format_adapter/
Picks the formatter; the CLI runs it in place. Do not edit registry.json here —
edit it in agent_gateway and re-sync.
"""
from .adapter import load, reload, pick_formatter, timeout_seconds  # noqa: F401
