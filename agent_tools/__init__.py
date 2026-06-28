"""Vendored copy of the agent_gateway unified tool catalog + Python adapter.

Source of truth: /root/agent_gateway/tools/ (catalog.json + adapter.py).
Re-sync with: cp /root/agent_gateway/tools/{catalog.json,adapter.py} agent_tools/
Kept as a vendored copy because laintas_cli deploys independently of the
gateway. Do not edit catalog.json here — edit it in agent_gateway and re-sync.
"""
from .adapter import Catalog, load, reload  # noqa: F401
