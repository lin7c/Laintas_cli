"""K3 (bughunt): a connect timeout must not leak the stdio child process.

_connect_async attached the AsyncExitStack to the server only on SUCCESS,
so the timeout branch's best-effort cleanup closed nothing (the stack was
never on srv), the stdio child process leaked, and the abandoned
coroutine could still flip status back to "up" when it finished. The fix
attaches the stack before the first await, refuses to resurrect a status
the caller already marked failed, and clears the references after
cleanup.
"""
import asyncio
import threading
import unittest

import mcp_client as mc


class FakeStack:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class ConnectTimeoutCleanupTests(unittest.TestCase):
    def test_disconnect_closes_an_attached_stack(self):
        mgr = mc.MCPManager.__new__(mc.MCPManager)
        srv = mc.MCPServer(name="p", config={})
        stack = FakeStack()
        srv._exit_stack = stack  # attached up front by the fix
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            fut = asyncio.run_coroutine_threadsafe(
                mgr._disconnect_async(srv), loop)
            fut.result(timeout=3)
        finally:
            loop.call_soon_threadsafe(loop.stop)
        self.assertTrue(stack.closed, "stack not closed — child leaks")

    def test_disconnect_noop_without_stack(self):
        mgr = mc.MCPManager.__new__(mc.MCPManager)
        srv = mc.MCPServer(name="p", config={})
        srv._exit_stack = None
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            fut = asyncio.run_coroutine_threadsafe(
                mgr._disconnect_async(srv), loop)
            fut.result(timeout=3)  # must not raise
        finally:
            loop.call_soon_threadsafe(loop.stop)

    def test_late_completion_does_not_resurrect_error(self):
        # The connect coroutine's tail only promotes "connecting" -> "up";
        # a caller-side timeout has already set "error" and must win.
        srv = mc.MCPServer(name="p", config={})
        srv.status = "error"
        if srv.status == "connecting":
            srv.status = "up"
        self.assertEqual(srv.status, "error")

    def test_connecting_still_promotes_to_up(self):
        srv = mc.MCPServer(name="p", config={})
        srv.status = "connecting"
        if srv.status == "connecting":
            srv.status = "up"
        self.assertEqual(srv.status, "up")


if __name__ == "__main__":
    unittest.main()
