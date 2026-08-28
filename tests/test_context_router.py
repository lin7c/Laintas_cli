import unittest
from unittest import mock

import agent_loop
import context_router
from tools import Tool


def _tool(name, source="builtin", description=""):
    return Tool(name=name, description=description, schema={},
                invoke=lambda params, ctx: {"ok": True}, source=source)


class DynamicToolRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tools = [
            _tool("fs.read"), _tool("fs.edit"), _tool("shell.exec"),
            _tool("tool.search"),
            _tool("task.complete"), _tool("web.search"), _tool("web.fetch"),
            _tool("browser.open"), _tool("browser.screenshot"),
            _tool("canvas.draw"), _tool("fs.delete"),
            _tool("agent.spawn"), _tool("agent.wait"), _tool("await_spawns"),
            _tool("agent.hire"), _tool("agent.station"),
            _tool("special.lookup", source="skill:special"),
        ]

    def test_plain_coding_task_gets_safe_core_not_specialist_schemas(self):
        names = context_router.select_tool_names("分析 reranker 用量", self.tools)
        self.assertIn("fs.read", names)
        self.assertIn("shell.exec", names)
        self.assertIn("tool.search", names)
        # Search/fetch remain resident because current and recommendation
        # intent is often implicit and a miss otherwise has no safe recovery.
        self.assertIn("web.search", names)
        self.assertNotIn("browser.open", names)
        self.assertNotIn("canvas.draw", names)
        self.assertIn("special.lookup", names)  # loaded skill tools remain usable

    def test_implicit_chinese_recommendation_keeps_web_research_available(self):
        names = context_router.select_tool_names(
            "目前有没有公认比较好的写论文 skill", self.tools)
        self.assertIn("web.search", names)
        self.assertIn("web.fetch", names)

    def test_explicit_capability_discovery_finds_hidden_builtin(self):
        found = context_router.discover_tool_names(
            "take a browser screenshot", self.tools)
        self.assertIn("browser.screenshot", found)

    def test_task_keywords_add_only_the_matching_groups(self):
        names = context_router.select_tool_names(
            "搜索网页并用浏览器截图", self.tools)
        self.assertIn("web.search", names)
        self.assertIn("browser.screenshot", names)
        self.assertNotIn("canvas.draw", names)
        self.assertNotIn("fs.delete", names)

    def test_visibility_grows_monotonically_and_authorization_still_wins(self):
        state = {}
        first = context_router.stable_visible_names("inspect code", self.tools, state)
        second = context_router.stable_visible_names("浏览器截图", self.tools, state)
        self.assertTrue(first <= second)

        authorized = {"fs.read", "task.complete"}
        with mock.patch("agent_loop.get_runtime_config", return_value=True), \
                mock.patch("agent_loop.tools_mod.get_registry") as registry:
            registry.return_value.list.return_value = self.tools
            visible = agent_loop._visible_tool_names_for_task(
                "搜索网页", {}, authorized)
        self.assertEqual(visible, authorized)

    def test_delegation_is_reachable_without_delegation_vocabulary(self):
        """Users describe the work, not the mechanism.

        Routing spawn behind the words "parallel"/"delegate"/"sub-task" meant a
        plainly decomposable task advertised no way to delegate, while the
        system prompt kept telling the model to delegate independent work.
        """
        for task in (
            "fix all failing tests in this repo and update the docs",
            "investigate the 502 in the gateway logs and fix it",
            "refactor this module and write tests for it",
            "review the auth code while I finish the migration",
        ):
            with self.subTest(task=task):
                names = context_router.select_tool_names(task, self.tools)
                self.assertIn("agent.spawn", names)
                self.assertIn("agent.wait", names)
        # Resident does not mean the whole agent surface is resident.
        names = context_router.select_tool_names(
            "fix all failing tests in this repo", self.tools)
        self.assertNotIn("agent.hire", names)
        self.assertNotIn("agent.station", names)


if __name__ == "__main__":
    unittest.main()
