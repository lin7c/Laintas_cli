"""Vendored copy of the agent_gateway HWG grammar + model-facing prompts.

Source of truth: /root/agent_gateway/hwg/ (adapter.py/.ts + prompt/*.txt).
Re-sync with: agent_gateway/scripts/sync_hwg.sh
Pure parser/validation + prompt text only — executors stay product-local.
"""

from .adapter import HwgParseError, as_graph, parse, validate  # noqa: F401
