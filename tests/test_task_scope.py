import sqlite3
import tempfile
import unittest

import task_manager
import tools
import workgraph


class TaskScopeTests(unittest.TestCase):
    def test_task_tools_force_runtime_session_and_owner_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = f"{tmp}/isolated-worktree"
            ctx = tools.ToolCtx(
                cwd=worktree, task_cwd=tmp,
                session_id="session-1", agent_id="child-1",
                parent_agent_id="root")
            created = tools._bi_task_create({
                "subject": "runtime scoped", "session_only": False,
                "metadata": {"session_id": "spoof", "owner_agent_id": "spoof"},
            }, ctx)

            self.assertTrue(created["ok"])
            task = created["result"]
            self.assertTrue(task["session_only"])
            self.assertEqual(task["session_id"], "session-1")
            self.assertEqual(task["owner_agent_id"], "child-1")
            self.assertEqual(task["parent_agent_id"], "root")
            self.assertNotIn("session_id", task["metadata"])
            self.assertNotIn("owner_agent_id", task["metadata"])

            other = tools._bi_task_list({}, tools.ToolCtx(
                cwd=tmp, session_id="session-2", agent_id="child-1"))
            sibling = tools._bi_task_list({}, tools.ToolCtx(
                cwd=tmp, session_id="session-1", agent_id="child-2"))
            self.assertEqual(other["result"], [])
            self.assertEqual(sibling["result"], [])

    def test_task_complete_blocks_only_calling_agent_subtree(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = task_manager.create_task(
                "own open", cwd=tmp, session_id="s1",
                owner_agent_id="child-a", parent_agent_id="root")
            task_manager.create_task(
                "sibling open", cwd=tmp, session_id="s1",
                owner_agent_id="child-b", parent_agent_id="root")
            ctx = tools.ToolCtx(
                cwd=tmp, session_id="s1", agent_id="child-a",
                parent_agent_id="root", state={}, depth=1)

            blocked = tools._bi_task_complete({"summary": "done"}, ctx)
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["open_task_ids"], [own["id"]])
            task_manager.update_task(
                own["id"], cwd=tmp, session_id="s1",
                owner_agent_id="child-a", status="completed")
            complete = tools._bi_task_complete({"summary": "done"}, ctx)
            self.assertTrue(complete["ok"])

    def test_session_active_work_and_lists_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = workgraph.ensure_active_work(
                "first session", cwd=tmp, session_id="session-1")
            second = workgraph.ensure_active_work(
                "second session", cwd=tmp, session_id="session-2")

            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(
                workgraph.get_active_work(cwd=tmp, session_id="session-1")["id"],
                first["id"],
            )
            self.assertEqual(
                workgraph.get_active_work(cwd=tmp, session_id="session-2")["id"],
                second["id"],
            )
            self.assertEqual(
                [item["id"] for item in workgraph.list_work(
                    cwd=tmp, session_id="session-1")],
                [first["id"]],
            )
            self.assertIsNone(workgraph.get_work(
                first["id"], cwd=tmp, session_id="session-2"))

    def test_task_owner_filters_reads_and_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            owned = task_manager.create_task(
                "owned by child", cwd=tmp, session_id="session-1",
                owner_agent_id="child-1", parent_agent_id="root",
                metadata={
                    "safe": "value",
                    "session_id": "spoofed",
                    "owner_agent_id": "spoofed",
                    "parent_agent_id": "spoofed",
                },
            )
            task_manager.create_task(
                "owned by sibling", cwd=tmp, session_id="session-1",
                owner_agent_id="child-2", parent_agent_id="root")

            self.assertEqual(owned["owner_agent_id"], "child-1")
            self.assertEqual(owned["parent_agent_id"], "root")
            self.assertEqual(owned["session_id"], "session-1")
            self.assertEqual(owned["metadata"], {"safe": "value"})
            self.assertEqual(
                [item["subject"] for item in task_manager.list_tasks(
                    cwd=tmp, session_id="session-1",
                    owner_agent_id="child-1")],
                ["owned by child"],
            )
            self.assertIsNone(task_manager.get_task(
                owned["id"], cwd=tmp, session_id="session-1",
                owner_agent_id="child-2"))

            ok, _, updated = task_manager.update_task(
                owned["id"], cwd=tmp, session_id="session-1",
                owner_agent_id="child-1", progress=25,
                metadata={"owner_agent_id": "child-2", "new": True})
            self.assertTrue(ok)
            self.assertEqual(updated["progress"], 25)
            self.assertEqual(updated["owner_agent_id"], "child-1")
            self.assertEqual(updated["metadata"], {"safe": "value", "new": True})

            ok, _, updated = task_manager.update_task(
                owned["id"], cwd=tmp, session_id="session-1",
                owner_agent_id="child-2", progress=90)
            self.assertFalse(ok)
            self.assertIsNone(updated)
            self.assertEqual(task_manager.get_task(
                owned["id"], cwd=tmp, session_id="session-1",
                owner_agent_id="child-1")["progress"], 25)

    def test_existing_database_gets_only_required_lineage_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = workgraph.db_path(tmp)
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as conn:
                conn.execute("""
                    CREATE TABLE steps (
                      work_id TEXT NOT NULL,
                      id TEXT NOT NULL,
                      subject TEXT NOT NULL,
                      description TEXT NOT NULL DEFAULT '',
                      status TEXT NOT NULL DEFAULT 'pending',
                      progress INTEGER NOT NULL DEFAULT 0,
                      parent_id TEXT,
                      owner_agent_id TEXT,
                      metadata TEXT NOT NULL DEFAULT '{}',
                      notes TEXT NOT NULL DEFAULT '[]',
                      result TEXT NOT NULL DEFAULT '',
                      session_only INTEGER NOT NULL DEFAULT 0,
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL,
                      PRIMARY KEY(work_id, id)
                    )
                """)

            workgraph.list_work(cwd=tmp)

            with sqlite3.connect(path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(steps)")
                }
            self.assertIn("owner_agent_id", columns)
            self.assertIn("parent_agent_id", columns)


if __name__ == "__main__":
    unittest.main()
