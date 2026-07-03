import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_system
import paths
import policy
import tools
import agent_loop


class MemoryCapabilityTests(unittest.TestCase):
    def test_persistent_memory_round_trip_and_search(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(memory_system, "MEMORY_DIR", Path(tmp)), \
                mock.patch.object(memory_system, "MEMORY_INDEX", Path(tmp) / "MEMORY.md"), \
                mock.patch.object(paths, "MEMORY_DIR", Path(tmp)), \
                mock.patch.object(paths, "MEMORY_INDEX", Path(tmp) / "MEMORY.md"):
            ok, _ = memory_system.write_memory(
                "release-process", "project", "How releases work",
                "Run the signed release pipeline after the full test suite.")
            self.assertTrue(ok)

            ctx = tools.ToolCtx(cwd=tmp)
            read = tools._bi_mem_read({"name": "release-process"}, ctx)
            self.assertTrue(read["ok"])
            self.assertIn("signed release pipeline", read["result"]["body"])

            found = tools._bi_mem_list({"query": "signed pipeline"}, ctx)
            self.assertTrue(found["ok"])
            self.assertEqual(found["result"][0]["name"], "release-process")

    def test_memory_name_cannot_escape_memory_directory(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(memory_system, "MEMORY_DIR", Path(tmp) / "memory"), \
                mock.patch.object(memory_system, "MEMORY_INDEX", Path(tmp) / "memory" / "MEMORY.md"):
            ok, message = memory_system.write_memory(
                "../escape", "project", "bad", "must not be written")
            self.assertFalse(ok)
            self.assertIn("slug", message)
            self.assertFalse((Path(tmp) / "escape.md").exists())

    def test_existing_safe_legacy_memory_name_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(memory_system, "MEMORY_DIR", Path(tmp)), \
                mock.patch.object(memory_system, "MEMORY_INDEX", Path(tmp) / "MEMORY.md"):
            memory_system.ensure_memory_dir()
            (Path(tmp) / "Legacy Name.md").write_text(
                "---\nname: Legacy Name\ndescription: old\nmetadata:\n"
                "  type: user\nscope: user\nscope_id: local-user\n---\nlegacy body",
                encoding="utf-8")
            data = memory_system.read_memory("Legacy Name")
            self.assertIsNotNone(data)
            self.assertEqual(data["body"], "legacy body")


class ToolSchemaTests(unittest.TestCase):
    def test_nested_schema_constraints_are_enforced_without_optional_dependency(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["safe"]},
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "minLength": 2}},
                        "required": ["name"],
                    },
                },
            },
            "required": ["mode", "count", "items"],
        }
        self.assertIsNone(tools._validate_params(
            {"mode": "safe", "count": 2, "items": [{"name": "ok"}]}, schema))
        self.assertIn("enum", tools._validate_params(
            {"mode": "unsafe", "count": 2, "items": [{"name": "ok"}]}, schema))
        self.assertIn("maximum", tools._validate_params(
            {"mode": "safe", "count": 9, "items": [{"name": "ok"}]}, schema))
        self.assertIn("unexpected", tools._validate_params(
            {"mode": "safe", "count": 2, "items": [], "extra": True}, schema))

    def test_openai_tool_export_can_be_filtered(self):
        registry = tools.ToolRegistry()
        registry.register(tools.Tool("fs.read", "read", {"type": "object", "properties": {}}, lambda p, c: {}))
        registry.register(tools.Tool("fs.write", "write", {"type": "object", "properties": {}}, lambda p, c: {}))
        exported, mapping = registry.to_openai_tools(allowed_names={"fs.read"})
        self.assertEqual(len(exported), 1)
        self.assertEqual(set(mapping.values()), {"fs.read"})

    def test_runtime_visibility_uses_dispatch_restrictions(self):
        with mock.patch.object(agent_loop.plan_mode, "is_plan_mode", return_value=False), \
                mock.patch.object(
                    agent_loop.mode_manager, "is_tool_allowed",
                    side_effect=lambda name: name == "fs.read"), \
                mock.patch.object(
                    agent_loop.agent_roles, "is_tool_allowed_for_role",
                    return_value=True), \
                mock.patch.object(
                    agent_loop.workflow_engine, "is_tool_allowed_in_workflow",
                    return_value=True):
            self.assertEqual(
                agent_loop._allowed_tool_names_for_state({}), {"fs.read"})


class WritePolicyTests(unittest.TestCase):
    def test_write_requiring_approval_fails_closed_without_channel(self):
        decision = policy.PolicyDecision("needs_approval", "", "approval required")
        with mock.patch.object(tools._policy_mod, "evaluate_file_write", return_value=decision):
            result = tools._check_file_write_policy(
                "/tmp/example", tools.ToolCtx(deps=None, cwd="/tmp"), "diff")
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertIn("no approval channel", result["error"].lower())
