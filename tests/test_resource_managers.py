import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import laintas_cli
import resource_ui
from agent_loop import DebugEntry


class UnifiedManagerTests(unittest.TestCase):
    def _capture_browser(self):
        patcher = mock.patch.object(laintas_cli.resource_ui, "ResourceBrowser")
        browser_cls = patcher.start()
        self.addCleanup(patcher.stop)
        browser_cls.return_value.run.return_value = resource_ui.UIOutcome("cancel")
        return browser_cls

    def test_memory_manager_exposes_full_body_and_delete_action(self):
        import memory_system
        browser_cls = self._capture_browser()
        entry = {
            "name": "preferences", "description": "How I work",
            "scope": "user", "type": "preference", "importance": .8,
        }
        with mock.patch.object(memory_system, "list_memories", return_value=[entry]), \
                mock.patch.object(memory_system, "read_memory", return_value={
                    "meta": {"description": "How I work"}, "body": "full body"}):
            laintas_cli._memory_manager()
            kwargs = browser_cls.call_args.kwargs
            rows = kwargs["load_items"]()
            detail = kwargs["load_detail"](rows[0])
        self.assertEqual(rows[0].title, "preferences")
        self.assertIn("full body", [line.text for line in detail.lines])
        self.assertEqual(kwargs["actions"][0].name, "delete")
        self.assertEqual(kwargs["presentation"], "document")
        self.assertEqual(kwargs["pane_labels"], ("MEMORIES", "CONTENT"))

    def test_skill_manager_has_in_place_lifecycle_actions(self):
        browser_cls = self._capture_browser()
        skill = {"name": "docs", "description": "Read docs", "loaded": False}
        metadata = SimpleNamespace(
            version="1", dir_path="/skills/docs", description="Read docs")
        registry = mock.Mock()
        registry.list_by_source.return_value = {}
        with mock.patch.object(laintas_cli.skills_mod, "list_skills",
                               return_value=[skill]), \
                mock.patch.object(laintas_cli.skills_mod, "get_all_metadata",
                                  return_value={"docs": metadata}), \
                mock.patch.object(laintas_cli.tools_mod, "get_registry",
                                  return_value=registry):
            laintas_cli.show_skill_manager()
            kwargs = browser_cls.call_args.kwargs
            rows = kwargs["load_items"]()
        self.assertEqual(
            {action.name for action in kwargs["actions"]},
            {"toggle", "load", "unload", "reload"})
        self.assertNotIn("primary_action", kwargs)
        self.assertEqual(
            next(action for action in kwargs["actions"]
                 if action.name == "toggle").key,
            "t")
        self.assertEqual(rows[0].status, "available")
        self.assertEqual(kwargs["presentation"], "document")
        self.assertEqual(kwargs["pane_labels"], ("SKILLS", "SOURCE & TOOLS"))

    def test_skill_detail_contains_complete_sources_and_tool_schema(self):
        browser_cls = self._capture_browser()
        registry = mock.Mock()
        tool = SimpleNamespace(
            name="docs.lookup", description="Look up documentation",
            source="skill:docs", trust_level="trusted",
            capabilities=frozenset({"fs.read"}),
            schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "full query"}}},
        )
        registry.list_by_source.return_value = {"skill:docs": [tool]}
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "docs"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: docs\n---\n# Complete manual\nDo the exact thing.\n",
                encoding="utf-8")
            (skill_dir / "extension.json").write_text(json.dumps({
                "name": "docs", "permissions": ["read-project"]}),
                encoding="utf-8")
            (skill_dir / "reference.txt").write_text(
                "reference body", encoding="utf-8")
            skill = {"name": "docs", "description": "Read docs", "loaded": True}
            metadata = SimpleNamespace(
                version="2", dir_path=str(skill_dir), description="Read docs")
            with mock.patch.object(laintas_cli.skills_mod, "list_skills",
                                   return_value=[skill]), \
                    mock.patch.object(laintas_cli.skills_mod, "get_all_metadata",
                                      return_value={"docs": metadata}), \
                    mock.patch.object(laintas_cli.tools_mod, "get_registry",
                                      return_value=registry):
                laintas_cli.show_skill_manager()
                kwargs = browser_cls.call_args.kwargs
                detail = kwargs["load_detail"](kwargs["load_items"]()[0])
        body = "\n".join(line.text for line in detail.lines)
        self.assertIn("# Complete manual", body)
        self.assertIn("Do the exact thing.", body)
        self.assertIn('"read-project"', body)
        self.assertIn("reference.txt", body)
        self.assertIn("docs.lookup", body)
        self.assertIn('"query"', body)
        self.assertIn("full query", body)

    def test_resume_preserves_original_enter_to_resume_behavior(self):
        blob = {
            "id": "s1", "kind": "session", "timestamp": 1,
            "chat_history": [
                {"role": "user", "content": "prompt"},
                {"role": "assistant", "content": "answer"},
            ],
        }
        with mock.patch.object(laintas_cli, "_resume_choices",
                               return_value=[blob]), \
                mock.patch.object(laintas_cli, "select_dialog",
                                  return_value=("resume", 0)) as picker:
            result = laintas_cli.show_resume_picker("/demo")
        self.assertIs(result, blob)
        self.assertEqual(picker.call_args.kwargs["enter_action"], "resume")
        self.assertEqual(
            picker.call_args.kwargs["action_keys"],
            {"d": "details", "x": "delete"})

    def test_terminal_manager_is_live_and_keeps_full_output(self):
        browser_cls = self._capture_browser()
        session = mock.Mock()
        session.is_alive.return_value = True
        session.command = "long-running command"
        session.full_output = "line one\nline two"
        session.returncode = -1
        with mock.patch.object(laintas_cli, "get_all_terminals", return_value=[]):
            laintas_cli.show_terminal_manager(session)
        kwargs = browser_cls.call_args.kwargs
        self.assertEqual(kwargs["refresh_interval"], .5)
        row = kwargs["load_items"]()[0]
        detail = kwargs["load_detail"](row)
        self.assertIn("line two", [line.text for line in detail.lines])
        self.assertNotIn("primary_action", kwargs)
        self.assertEqual(kwargs["presentation"], "operations")
        self.assertEqual(kwargs["pane_labels"], ("TERMINALS", "LIVE OUTPUT"))
        enter = next(action for action in kwargs["actions"]
                     if action.name == "enter")
        self.assertEqual(enter.key, "e")

    def test_plan_manager_allows_new_plan_when_empty(self):
        import plan_mode
        browser_cls = self._capture_browser()
        with mock.patch.object(plan_mode, "list_plans", return_value=[]), \
                mock.patch.object(plan_mode, "get_current_plan", return_value=None), \
                mock.patch.object(plan_mode, "is_plan_mode", return_value=False):
            laintas_cli.show_plan_picker()
            kwargs = browser_cls.call_args.kwargs
            rows = kwargs["load_items"]()
        new_action = next(action for action in kwargs["actions"]
                          if action.name == "new")
        self.assertTrue(new_action.allow_empty)
        self.assertEqual(rows, [])

    def test_plan_detail_reads_the_complete_saved_plan(self):
        import plan_mode
        browser_cls = self._capture_browser()
        plan = {"name": "migration", "title": "Migration",
                "status": "draft", "file": "/plans/migration.md"}
        body = "# Migration\n\n1. First complete step\n2. Final complete step"
        with mock.patch.object(plan_mode, "list_plans", return_value=[plan]), \
                mock.patch.object(plan_mode, "get_current_plan", return_value=None), \
                mock.patch.object(plan_mode, "is_plan_mode", return_value=False), \
                mock.patch.object(plan_mode, "read_plan", return_value=body):
            laintas_cli.show_plan_picker()
            kwargs = browser_cls.call_args.kwargs
            detail = kwargs["load_detail"](kwargs["load_items"]()[0])
        rendered = "\n".join(line.text for line in detail.lines)
        self.assertEqual(rendered, body)
        self.assertIn("/plans/migration.md", detail.subtitle)
        self.assertEqual(kwargs["presentation"], "document")
        self.assertEqual(kwargs["pane_labels"], ("PLANS", "PLAN DOCUMENT"))

    def test_debug_detail_keeps_request_reply_tool_and_raw_response(self):
        browser_cls = self._capture_browser()
        entry = DebugEntry(
            timestamp="2026-08-13 10:00:00", loop=7,
            user_input="inspect this", current_path="/project",
            context_sizes={"prompt": 12},
            request_body={"message": "complete request",
                          "promptPreview": "complete preview"},
            reply="complete AI response", command="fs.read(path)",
            exec_command="ls", exec_stdout="complete stdout",
            exec_stderr="complete stderr", exec_returncode=1,
            response_raw={"finish_reason": "stop", "usage": {"tokens": 42}},
        )
        with mock.patch.object(laintas_cli, "get_debug_logs",
                               return_value=[entry]):
            laintas_cli.show_debug_browser_interactive()
            kwargs = browser_cls.call_args.kwargs
            detail = kwargs["load_detail"](kwargs["load_items"]()[0])
        rendered = "\n".join(line.text for line in detail.lines)
        for expected in ("complete request", "complete preview",
                         "complete AI response", "fs.read(path)",
                         "complete stdout", "complete stderr",
                         '"tokens": 42'):
            self.assertIn(expected, rendered)
        self.assertEqual(kwargs["presentation"], "timeline")
        self.assertEqual(kwargs["pane_labels"], ("ACTIVITY", "EVENT DETAIL"))

    def test_work_resume_detail_keeps_objective_and_all_step_titles(self):
        browser_cls = self._capture_browser()
        work = {"id": "work-1", "objective": "complete objective text",
                "status": "EXECUTING", "current_revision": 3,
                "approved_revision": 2}
        steps = [
            {"id": "step-1", "status": "completed", "title": "first full step"},
            {"id": "step-2", "status": "pending", "title": "second full step"},
        ]
        fake_stdin = SimpleNamespace(isatty=lambda: True)
        with mock.patch.object(laintas_cli.sys, "stdin", fake_stdin), \
                mock.patch.object(laintas_cli.workgraph, "list_work",
                                  return_value=[work]), \
                mock.patch.object(laintas_cli.workgraph, "list_steps",
                                  return_value=steps):
            laintas_cli._cmd_work(["/work", "resume"])
            kwargs = browser_cls.call_args.kwargs
            detail = kwargs["load_detail"](kwargs["load_items"]()[0])
        rendered = "\n".join(line.text for line in detail.lines)
        self.assertIn("complete objective text", rendered)
        self.assertIn("first full step", rendered)
        self.assertIn("second full step", rendered)
        self.assertEqual(kwargs["presentation"], "operations")

    def test_task_detail_keeps_description_notes_and_dependencies(self):
        browser_cls = self._capture_browser()
        task = {
            "id": "task-1", "subject": "Detailed task", "status": "blocked",
            "progress": 45, "owner_agent_id": "agent-a",
            "parent_agent_id": "root", "blockedBy": ["task-0"],
            "blocks": ["task-2"], "description": "complete task description",
            "notes": ["first complete note", "second complete note"],
        }
        agent = SimpleNamespace(
            id="agent-a", parent_id="root",
            state={"_task_cwd": "/project", "_session_id": "session-1"})
        fake_stdin = SimpleNamespace(isatty=lambda: True)
        with mock.patch.object(laintas_cli.sys, "stdin", fake_stdin), \
                mock.patch.object(laintas_cli.console, "_force_terminal", True,
                                  create=True), \
                mock.patch.object(laintas_cli, "get_current_agent",
                                  return_value=agent), \
                mock.patch.object(laintas_cli.task_manager, "list_tasks",
                                  return_value=[task]):
            laintas_cli._cmd_task("", ["/task"])
            kwargs = browser_cls.call_args.kwargs
            detail = kwargs["load_detail"](kwargs["load_items"]()[0])
        rendered = "\n".join(line.text for line in detail.lines)
        for expected in ("complete task description", "first complete note",
                         "second complete note", "task-0", "task-2", "45%"):
            self.assertIn(expected, rendered)
        self.assertEqual(kwargs["presentation"], "operations")

    def test_detail_trace_page_keeps_whole_file_and_final_ai_output(self):
        browser_cls = self._capture_browser()
        chat = [
            {"role": "user", "content": "edit it", "detail_trace": True},
            {"role": "tool", "content": "edited", "summary": "sample.py",
             "trace": {"tool": "fs.edit", "display_name": "Edit",
                       "ok": True, "elapsed_seconds": .2, "path": "sample.py",
                       "before": "unchanged one\nold value\nunchanged three\n",
                       "after": "unchanged one\nnew value\nunchanged three\n"}},
            {"role": "assistant", "content": "complete final answer",
             "message_kind": "final"},
        ]
        laintas_cli._browse_detail_trace(chat, 1)
        kwargs = browser_cls.call_args.kwargs
        rows = kwargs["load_items"]()
        edit_detail = kwargs["load_detail"](rows[0])
        ai_detail = kwargs["load_detail"](rows[1])
        edit_rendered = "\n".join(line.text for line in edit_detail.lines)
        self.assertIn("unchanged one", edit_rendered)
        self.assertIn("old value", edit_rendered)
        self.assertIn("new value", edit_rendered)
        self.assertIn("unchanged three", edit_rendered)
        self.assertIn("complete final answer",
                      [line.text for line in ai_detail.lines])
        self.assertEqual(kwargs["presentation"], "timeline")
        self.assertEqual(kwargs["pane_labels"], ("EVENTS", "RAW EVIDENCE"))


if __name__ == "__main__":
    unittest.main()
