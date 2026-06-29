"""Vendored copy of the agent_gateway HWO grammar + model-facing prompts.

Source of truth: /root/agent_gateway/hwo/ (adapter.py/.ts + prompt/*.txt).
Re-sync with: agent_gateway/scripts/sync_hwo.sh
Pure parser/validation + prompt text only — the executor stays in hwo_runner.py.
Do not edit adapter.py / prompt.py here; edit in agent_gateway and re-sync.
"""
from .adapter import HwoParseError, parse, parse_hwo, validate  # noqa: F401
from .prompt import HWO_COMM_TOOLS, HWO_SYNTAX, HWO_TOOL_DESCRIPTION  # noqa: F401
