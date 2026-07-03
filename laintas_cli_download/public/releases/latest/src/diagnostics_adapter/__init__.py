"""Vendored copy of the agent_gateway post-edit diagnostics registry + picker.

Source of truth: /root/agent_gateway/diagnostics/ (registry.json + adapter.py).
Re-sync with: cp /root/agent_gateway/diagnostics/{registry.json,adapter.py} diagnostics_adapter/
Picks the checker; the CLI runs it. Do not edit registry.json here — edit it in
agent_gateway and re-sync.
"""
from .adapter import load, reload, pick_checker, timeout_seconds, max_output_chars  # noqa: F401
