import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import agent_ui_events
import workbench_tui


class _Controller:
    def __init__(self):
        self.terminal_name = "term0"
        self.selected_id = "alpha"
        self.notice = "Ready"
        self._approval_lock = threading.Lock()
        self._closed = threading.Event()
        self.dispatched = []
        self.submitted = []
        self.resolved = []
        self.approval = None
        self.rows = [
            SimpleNamespace(id="alpha", name="Alpha", status="idle",
                            role="primary", created_at=1),
            SimpleNamespace(id="beta", name="Beta", status="thinking",
                            role="pool", created_at=2),
        ]

    def agents(self):
        return list(self.rows)

    def get(self, agent_id):
        return next((row for row in self.rows if row.id == agent_id), None)

    def _display_status(self, agent):
        return agent.status

    def unread(self, agent_id):
        return 1 if agent_id == "beta" else 0

    def _current_task(self, agent):
        return f"Task for {agent.name}"

    def repl_submit_cb(self, text, *, kind="dialogue"):
        self.submitted.append((text, kind))
        return True, "Queued"

    def dispatch(self, text):
        self.dispatched.append(text)
        self.notice = "Sent"

    def select(self, agent_id):
        if self.get(agent_id) is None:
            return False
        self.selected_id = agent_id
        return True

    def cycle_agent(self, delta):
        ids = [row.id for row in self.rows]
        index = ids.index(self.selected_id)
        self.selected_id = ids[(index + delta) % len(ids)]

    def cycle_terminal(self, delta):
        self.terminal_name = "term1" if self.terminal_name == "term0" else "term0"

    def pending_approval(self):
        return self.approval

    def resolve_approval(self, approved):
        self.resolved.append(bool(approved))
        self.approval = None

    def deny_pending_approvals(self, **_kwargs):
        self.approval = None


class WorkbenchTUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        agent_ui_events.hub.reset()
        self.controller = _Controller()
        self.get_agent = mock.patch.object(
            workbench_tui.agent_loop, "get_agent",
            side_effect=self.controller.get)
        self.get_agent.start()

    def tearDown(self):
        self.get_agent.stop()
        agent_ui_events.hub.reset()

    async def test_input_routes_dialogue_slash_and_shell_without_duplication(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            composer = app.query_one("#composer")
            composer.value = "hello agent"
            await pilot.press("enter")
            composer.value = "/model list"
            await pilot.press("enter")
            composer.value = "$ git status"
            await pilot.press("enter")
            await pilot.pause()

        self.assertEqual(self.controller.dispatched, ["hello agent"])
        self.assertEqual(self.controller.submitted, [
            ("/model list", "line"),
            ("git status", "line"),
        ])

    async def test_mouse_send_uses_same_dialogue_route_as_enter(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            app.query_one("#composer").value = "sent by mouse"
            clicked = await pilot.click("#send")
            await pilot.pause()

        self.assertTrue(clicked)
        self.assertEqual(self.controller.dispatched, ["sent by mouse"])

    async def test_mouse_agent_selection_and_keyboard_have_same_state_path(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            selected = await pilot.click("#agent-list", offset=(3, 4))
            await pilot.pause()
            self.assertTrue(selected)
            self.assertEqual(self.controller.selected_id, "beta")
            await pilot.press("alt+up")
            await pilot.pause()
            self.assertEqual(self.controller.selected_id, "alpha")

    async def test_narrow_layout_uses_agent_drawer_with_predictable_escape(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(58, 18)) as pilot:
            rail = app.query_one("#agent-rail")
            center = app.query_one("#center")
            self.assertTrue(rail.has_class("hidden"))
            self.assertFalse(center.has_class("hidden"))
            await pilot.click("#agents-button")
            await pilot.pause()
            self.assertFalse(rail.has_class("hidden"))
            self.assertTrue(center.has_class("hidden"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(rail.has_class("hidden"))
            self.assertFalse(center.has_class("hidden"))

    async def test_inspector_and_activity_follow_breakpoints(self):
        wide = workbench_tui.WorkbenchApp(self.controller)
        async with wide.run_test(size=(160, 36)) as pilot:
            await pilot.pause()
            self.assertFalse(wide.query_one("#context-panel").has_class("hidden"))
            self.assertFalse(wide.query_one("#activity-wrap").has_class("hidden"))

        compact = workbench_tui.WorkbenchApp(self.controller)
        async with compact.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            self.assertTrue(compact.query_one("#context-panel").has_class("hidden"))
            self.assertTrue(compact.query_one("#activity-wrap").has_class("hidden"))

    async def test_live_resize_changes_layout_without_restarting_app(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(160, 36)) as pilot:
            self.assertFalse(app.query_one("#agent-rail").has_class("hidden"))
            self.assertFalse(app.query_one("#context-panel").has_class("hidden"))
            await pilot.resize_terminal(58, 18)
            await pilot.pause()
            self.assertTrue(app.query_one("#agent-rail").has_class("hidden"))
            self.assertTrue(app.query_one("#context-panel").has_class("hidden"))
            await pilot.resize_terminal(160, 36)
            await pilot.pause()
            self.assertFalse(app.query_one("#agent-rail").has_class("hidden"))
            self.assertFalse(app.query_one("#context-panel").has_class("hidden"))

    async def test_deleted_working_directory_does_not_crash_metadata_refresh(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            with mock.patch.object(
                    workbench_tui.os, "getcwd",
                    side_effect=FileNotFoundError("detached")):
                app._refresh_metadata()
                await pilot.pause()
                status = str(app.query_one("#workspace-status").render())
        self.assertIn("detached cwd", status)

    async def test_event_burst_is_bounded_and_stream_updates_are_coalesced(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            for _index in range(6000):
                event = agent_ui_events.AgentUIEvent(
                    seq=_index + 1, event_id=str(_index), timestamp=1,
                    monotonic_time=1, event_type="ai_stream",
                    agent_id="alpha", detail="x")
                app._runtime_event(event)
            await pilot.pause()
            self.assertLessEqual(len(app._stream_text), 5000)
            self.assertTrue(app.query_one("#live-stream").has_class("visible"))

    async def test_approval_has_mouse_and_keyboard_paths(self):
        self.controller.approval = {
            "id": "approval-1", "agent_id": "alpha", "kind": "write",
            "summary": "Change config", "detail": "safe diff",
        }
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#approve")
            await pilot.pause()
        self.assertEqual(self.controller.resolved, [True])

    async def test_fullscreen_command_hands_terminal_back_before_dispatch(self):
        app = workbench_tui.WorkbenchApp(self.controller)
        async with app.run_test(size=(120, 30)) as pilot:
            composer = app.query_one("#composer")
            composer.value = "/resume"
            await pilot.press("enter")
            await pilot.pause()

        self.assertEqual(app.return_value, "/resume")
        self.assertEqual(self.controller.submitted, [])

    def test_event_bridge_is_bounded_and_wakes_once_per_batch(self):
        wakes = []
        bridge = workbench_tui._EventBridge(lambda: wakes.append(True), 5000)

        def produce(start):
            for index in range(3000):
                bridge.push("event", start + index)

        threads = [threading.Thread(target=produce, args=(offset,))
                   for offset in (0, 3000, 6000, 9000)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(wakes), 1)
        self.assertEqual(len(bridge.drain()), 5000)
        bridge.push("event", "next")
        self.assertEqual(len(wakes), 2)

    @unittest.skipIf(workbench_tui._BackgroundLinuxDriver is None,
                     "Linux-only background driver")
    def test_background_driver_does_not_install_process_signal_handlers(self):
        errors = []

        def construct_driver():
            try:
                async def construct():
                    app = workbench_tui.WorkbenchApp(self.controller)
                    driver = app.driver_class(app)
                    self.assertFalse(driver.can_suspend)

                asyncio.run(construct())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=construct_driver)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
