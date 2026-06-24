"""MCP client manager for laintas_cli — Phase 3c.

Bridges the async `mcp` Python SDK to laintas's synchronous Tool registry.

Architecture
------------
                 main thread (REPL / agent loop)
                       │  sync calls
                       ▼
                 MCPManager._submit(coro)
                       │  run_coroutine_threadsafe
                       ▼
             dedicated background asyncio loop thread
                       │
                  ┌────┴────┐
                  ▼         ▼
              MCPServer  MCPServer ...    (one per configured server,
              stdio_client + ClientSession  kept open via AsyncExitStack)

Each configured MCP server runs as a child subprocess (npx / uvx /
custom) speaking JSON-RPC over stdio. On connect we initialize, list
tools, and register each tool with the global ToolRegistry tagged
`source="mcp:<server>"`. Tool invocations are submitted back into the
loop and the result is awaited synchronously by the caller (with a
configurable per-call timeout).

If the `mcp` package isn't installed, this module still imports cleanly
— every operation returns a friendly "MCP SDK not installed" error so
the CLI keeps working without it.

Config file: ~/.laintas/mcp.json
{
  "servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {},
      "cwd": null,
      "enabled": true,
      "call_timeout": 30
    }
  }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools import Tool, ToolCtx, get_registry


# ── Lazy MCP SDK import — never let it block module import ──────────────
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
    MCP_IMPORT_ERROR: Optional[str] = None
except Exception as _e:
    ClientSession = None       # type: ignore
    StdioServerParameters = None  # type: ignore
    stdio_client = None        # type: ignore
    MCP_AVAILABLE = False
    MCP_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"


import paths

CONFIG_PATH = paths.MCP_FILE
DEFAULT_CALL_TIMEOUT = 30.0     # seconds — per tool call
CONNECT_TIMEOUT = 15.0          # seconds — per server initialize
LIST_TOOLS_TIMEOUT = 10.0


# ── Per-server state ───────────────────────────────────────────────────

@dataclass
class MCPServer:
    name: str
    config: dict
    status: str = "down"               # down / connecting / up / error
    last_error: Optional[str] = None
    tools: list = field(default_factory=list)   # mcp.types.Tool list
    _session: Any = None               # ClientSession when up
    _exit_stack: Any = None            # AsyncExitStack when up

    def call_timeout(self) -> float:
        try:
            return float(self.config.get("call_timeout") or DEFAULT_CALL_TIMEOUT)
        except (TypeError, ValueError):
            return DEFAULT_CALL_TIMEOUT


# ── Manager ────────────────────────────────────────────────────────────

class MCPManager:
    """Owns a single background asyncio loop and one MCPServer per config entry."""

    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._lock = threading.RLock()

    # ── Loop lifecycle ────────────────────────────────────────────────
    def _ensure_loop(self) -> bool:
        if not MCP_AVAILABLE:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._loop_ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                         name="laintas-mcp-loop")
        self._thread.start()
        self._loop_ready.wait(timeout=2.0)
        return self._loop is not None

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _submit(self, coro, timeout: float):
        """Run a coroutine on the manager's loop, block until result or timeout."""
        if self._loop is None:
            raise RuntimeError("MCP loop not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def shutdown(self):
        """Disconnect every server then stop the loop. Best-effort, idempotent."""
        if self._loop is None:
            return
        # Schedule disconnects, wait briefly, then stop the loop.
        for name in list(self.servers):
            try:
                self.disconnect(name, timeout=3.0)
            except Exception:
                pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # ── Config ────────────────────────────────────────────────────────
    @staticmethod
    def load_config() -> dict:
        if not CONFIG_PATH.is_file():
            return {"servers": {}}
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"servers": {}}
            data.setdefault("servers", {})
            return data
        except (OSError, ValueError):
            return {"servers": {}}

    @staticmethod
    def write_template_config() -> tuple[bool, str]:
        if CONFIG_PATH.exists():
            return False, f"already exists: {CONFIG_PATH}"
        template = {
            "servers": {
                "filesystem_example": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {},
                    "enabled": False,
                    "call_timeout": 30,
                }
            }
        }
        try:
            CONFIG_PATH.write_text(json.dumps(template, indent=2), encoding="utf-8")
            return True, str(CONFIG_PATH)
        except OSError as e:
            return False, str(e)

    # ── Connect / Disconnect ─────────────────────────────────────────
    def connect_all_enabled(self) -> list[tuple[str, bool, str]]:
        if not MCP_AVAILABLE:
            return [("(none)", False, f"mcp package not installed: {MCP_IMPORT_ERROR}")]
        cfg = self.load_config()
        results = []
        for name, sc in cfg.get("servers", {}).items():
            if not sc.get("enabled", True):
                results.append((name, False, "disabled"))
                continue
            ok, msg = self.connect(name, sc)
            results.append((name, ok, msg))
        return results

    def connect(self, name: str, server_config: Optional[dict] = None) -> tuple[bool, str]:
        if not MCP_AVAILABLE:
            return False, f"mcp package not installed: {MCP_IMPORT_ERROR}"
        if server_config is None:
            cfg = self.load_config()
            server_config = cfg.get("servers", {}).get(name)
            if server_config is None:
                return False, f"no server '{name}' in config"

        if not self._ensure_loop():
            return False, "failed to start mcp event loop"

        with self._lock:
            # If already up, disconnect first so we reconnect cleanly.
            if name in self.servers and self.servers[name].status == "up":
                self.disconnect(name)
            srv = MCPServer(name=name, config=server_config, status="connecting")
            self.servers[name] = srv

        try:
            self._submit(self._connect_async(srv), timeout=CONNECT_TIMEOUT + 5)
        except Exception as e:
            srv.status = "error"
            srv.last_error = f"{type(e).__name__}: {e}"
            # Best-effort cleanup
            try:
                self._submit(self._disconnect_async(srv), timeout=3.0)
            except Exception:
                pass
            return False, srv.last_error

        # Register each tool in the global registry.
        registry = get_registry()
        registered = 0
        for t in srv.tools:
            ltool = self._adapt_tool(srv, t)
            registry.register(ltool)
            registered += 1
        return True, f"connected ({registered} tools)"

    async def _connect_async(self, srv: MCPServer):
        params = StdioServerParameters(
            command=srv.config.get("command"),
            args=list(srv.config.get("args", [])),
            env=srv.config.get("env") or None,
            cwd=srv.config.get("cwd") or None,
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)
        result = await asyncio.wait_for(session.list_tools(), timeout=LIST_TOOLS_TIMEOUT)
        srv._exit_stack = stack
        srv._session = session
        srv.tools = list(result.tools) if hasattr(result, "tools") else []
        srv.status = "up"
        srv.last_error = None

    def disconnect(self, name: str, timeout: float = 5.0) -> tuple[bool, str]:
        with self._lock:
            srv = self.servers.get(name)
        if srv is None:
            return False, f"no server '{name}'"
        # Drop tools first so subsequent invokes can't race onto a dead session.
        get_registry().unregister_source(f"mcp:{name}")
        try:
            if srv._exit_stack is not None and self._loop is not None:
                self._submit(self._disconnect_async(srv), timeout=timeout)
        except Exception as e:
            srv.last_error = f"disconnect: {type(e).__name__}: {e}"
        srv._session = None
        srv._exit_stack = None
        srv.tools = []
        srv.status = "down"
        return True, "disconnected"

    async def _disconnect_async(self, srv: MCPServer):
        if srv._exit_stack is not None:
            try:
                await srv._exit_stack.aclose()
            except Exception:
                pass

    def reload(self) -> list[tuple[str, bool, str]]:
        """Disconnect every active server, re-read config, reconnect enabled."""
        for name in list(self.servers):
            try:
                self.disconnect(name)
            except Exception:
                pass
        self.servers.clear()
        return self.connect_all_enabled()

    # ── Tool adapter ─────────────────────────────────────────────────
    def _adapt_tool(self, srv: MCPServer, mcp_tool) -> Tool:
        name = f"{srv.name}.{mcp_tool.name}"
        description = (getattr(mcp_tool, "description", "") or "").strip() \
                      or f"MCP tool {mcp_tool.name} from {srv.name}"
        schema = getattr(mcp_tool, "inputSchema", None) or \
                 getattr(mcp_tool, "input_schema", None) or {}
        if hasattr(schema, "model_dump"):    # pydantic model
            schema = schema.model_dump()
        elif not isinstance(schema, dict):
            schema = {}

        srv_name = srv.name
        raw_name = mcp_tool.name
        manager_ref = self

        def _invoke(params: dict, ctx: ToolCtx) -> dict:
            srv_now = manager_ref.servers.get(srv_name)
            if srv_now is None or srv_now.status != "up" or srv_now._session is None:
                return {"ok": False, "error": f"server '{srv_name}' is not connected"}
            try:
                result = manager_ref._submit(
                    srv_now._session.call_tool(raw_name, arguments=params or {}),
                    timeout=srv_now.call_timeout(),
                )
            except asyncio.TimeoutError:
                return {"ok": False, "error": f"timeout after {srv_now.call_timeout()}s"}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "trace": traceback.format_exc(limit=3)}

            # CallToolResult → {ok, result, isError}
            content_blocks = getattr(result, "content", []) or []
            text_parts: list = []
            other: list = []
            for c in content_blocks:
                if hasattr(c, "text"):
                    text_parts.append(c.text)
                elif hasattr(c, "model_dump"):
                    other.append(c.model_dump())
                else:
                    other.append(repr(c))
            payload: dict = {"ok": not bool(getattr(result, "isError", False))}
            if text_parts:
                payload["result"] = "\n".join(text_parts)
            if other:
                payload["blocks"] = other
            return payload

        return Tool(
            name=name,
            description=description,
            schema=schema,
            invoke=_invoke,
            source=f"mcp:{srv.name}",
        )


# Module-level singleton
_manager = MCPManager()


def get_manager() -> MCPManager:
    return _manager
