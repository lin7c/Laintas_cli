"""The cloud-folder tools, and the two other things the model could not see.

Laintas Storage had exactly one tool (`file_push`, an upload that also pokes a
Helpwo agent), so the model could write into the user's cloud folder and never
read it back, list it, or find out how full it was — while `/shared` had done
all of that for the user since the beginning. These pin the tools that closed
that gap, plus `account.usage` and `diag.tool_failures`, which had the same
shape of problem: a REPL command with no way in from a tool call.
"""

import os
import tempfile
import types
import unittest
from unittest import mock

import agent_loop
import context_router
import tools


class _FakeUsage:
    tier = "pro"
    used_bytes = 1024
    free_bytes = 5 * 1024 * 1024
    max_bytes = 6 * 1024 * 1024
    overage_bytes = 0
    est_cost_cents = 0
    max_file_bytes = 10 * 1024 * 1024


class _FakeEntry:
    def __init__(self, path, name, type_="file", size=12, modified="2026-09-01"):
        self.path, self.name, self.type = path, name, type_
        self.size, self.modified = size, modified


class _FakeClient:
    """Stands in for shared_storage.SharedStorage; records what it was asked."""

    def __init__(self, *_args, **_kwargs):
        self.calls = []

    def usage(self):
        self.calls.append(("usage",))
        return _FakeUsage()

    def list(self, prefix=""):
        self.calls.append(("list", prefix))
        return [_FakeEntry("reports/a.md", "a.md")]

    def push_file(self, local_path, remote_path):
        self.calls.append(("push", local_path, remote_path))
        return os.path.getsize(local_path)

    def pull_file(self, remote_path, local_path):
        self.calls.append(("pull", remote_path, local_path))
        with open(local_path, "w", encoding="utf-8") as handle:
            handle.write("downloaded")
        return 10

    def mkdir(self, path):
        self.calls.append(("mkdir", path))

    def move(self, src, dest):
        self.calls.append(("move", src, dest))

    def remove(self, path):
        self.calls.append(("remove", path))
        return 1


class StorageToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools.register_builtin_tools()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = tools.get_registry()
        self.client = _FakeClient()
        import shared_storage
        patch = mock.patch.object(
            shared_storage, "SharedStorage", lambda *a, **k: self.client)
        patch.start()
        self.addCleanup(patch.stop)
        self.shared_storage = shared_storage

    def ctx(self, deps=None):
        return tools.ToolCtx(cwd=self.tmp.name, session={"userId": "u1"},
                             deps=deps)

    def call(self, name, deps=None, **params):
        return self.registry.invoke(name, params, self.ctx(deps))

    # ---- reading, which is the half that did not exist ----

    def test_usage_reports_the_allowance(self):
        out = self.call("storage.usage")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["tier"], "pro")
        self.assertEqual(out["result"]["max_file_bytes"], 10 * 1024 * 1024)

    def test_list_returns_entries(self):
        out = self.call("storage.list", path="reports/")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["path"], "reports")
        self.assertEqual(out["result"]["entries"][0]["name"], "a.md")

    def _writable(self):
        """A download lands a file on this disk, so it asks like fs.write."""
        return types.SimpleNamespace(
            request_file_write_approval=lambda *_a: True,
            request_file_delete_approval=lambda *_a: True)

    def test_get_defaults_to_the_remote_name_in_the_working_directory(self):
        out = self.call("storage.get", deps=self._writable(), path="reports/a.md")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["local_path"],
                         os.path.join(self.tmp.name, "a.md"))
        self.assertTrue(os.path.isfile(out["result"]["local_path"]))

    def test_get_into_a_directory_keeps_the_remote_name(self):
        target = os.path.join(self.tmp.name, "inbox")
        os.makedirs(target)
        out = self.call("storage.get", deps=self._writable(),
                        path="reports/a.md", local_path=target)
        self.assertEqual(out["result"]["local_path"],
                         os.path.join(target, "a.md"))

    def test_a_download_obeys_the_same_write_gate_as_fs_write(self):
        """Bytes arriving over the network still land as a local file."""
        deps = types.SimpleNamespace(
            request_file_write_approval=lambda *_a: False)
        out = self.call("storage.get", deps=deps, path="reports/a.md")
        self.assertFalse(out["ok"])
        self.assertEqual(self.client.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "a.md")))

    # ---- writing ----

    def test_put_uploads_and_defaults_the_remote_name(self):
        local = os.path.join(self.tmp.name, "summary.md")
        with open(local, "w", encoding="utf-8") as handle:
            handle.write("hello")
        out = self.call("storage.put", local_path="summary.md")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["path"], "summary.md")
        self.assertEqual(out["result"]["bytes"], 5)
        self.assertEqual(self.client.calls[-1], ("push", local, "summary.md"))

    def test_put_refuses_a_path_that_is_not_a_file(self):
        out = self.call("storage.put", local_path="nope.md")
        self.assertFalse(out["ok"])
        self.assertIn("not a file", out["error"])

    def test_a_bad_remote_path_never_reaches_the_network(self):
        out = self.call("storage.list", path="../etc")
        self.assertFalse(out["ok"])
        self.assertEqual(self.client.calls, [])

    # ---- deleting: always asked, in every mode ----

    def test_delete_without_an_approval_channel_is_refused(self):
        out = self.call("storage.delete", path="reports/a.md")
        self.assertFalse(out["ok"])
        self.assertIn("approval", out["error"])
        self.assertEqual(self.client.calls, [])

    def test_delete_honours_a_denial(self):
        deps = types.SimpleNamespace(
            request_file_delete_approval=lambda *_a: False)
        out = self.call("storage.delete", deps=deps, path="reports/a.md")
        self.assertFalse(out["ok"])
        self.assertIn("denied", out["error"])
        self.assertEqual(self.client.calls, [])

    def test_delete_runs_once_approved(self):
        asked = []

        def approve(path, preview, reason):
            asked.append((path, preview, reason))
            return True

        deps = types.SimpleNamespace(request_file_delete_approval=approve)
        out = self.call("storage.delete", deps=deps, path="reports/a.md")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.client.calls[-1], ("remove", "reports/a.md"))
        self.assertTrue(asked, "the user was never asked")

    # ---- labeling and discovery ----

    def test_transfers_are_labeled_for_the_local_side_they_touch(self):
        self.assertIn("fs.write", tools.infer_capabilities("storage.get"))
        self.assertIn("fs.read", tools.infer_capabilities("storage.put"))
        self.assertIn("network", tools.infer_capabilities("storage.list"))

    def test_a_storage_request_routes_the_storage_tools(self):
        """The gap was not only the tools: nothing routed to them."""
        catalog = self.registry.list()
        for query in ("upload the report to cloud storage",
                      "how much space is left in my cloud folder"):
            routed = context_router.select_tool_names(query, catalog)
            self.assertTrue(
                any(name.startswith("storage.") for name in routed),
                f"{query!r} routed no storage tool")


class AccountUsageToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools.register_builtin_tools()

    def test_local_only_needs_no_network(self):
        registry = tools.get_registry()
        with mock.patch("requests.get",
                        side_effect=AssertionError("must not call out")):
            out = registry.invoke("account.usage", {"local_only": True},
                                  tools.ToolCtx(session={}))
        self.assertTrue(out["ok"], out)
        self.assertIn("session", out["result"]["local"])
        self.assertEqual(out["result"]["days"], 30)

    def test_the_window_is_bounded(self):
        registry = tools.get_registry()
        out = registry.invoke("account.usage", {"days": 900, "local_only": True},
                              tools.ToolCtx(session={}))
        self.assertEqual(out["result"]["days"], 90)


class DiagToolFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tools.register_builtin_tools()

    def test_it_reports_another_agent_failure(self):
        rows = [
            {"tool": "shell.exec", "display_name": "shell", "agent_id": "child-1",
             "terminal": "term0", "command": "pytest -q", "error": "exit 1",
             "elapsed_seconds": 3.2, "output_tail": "1 failed\ncollected 2"},
            {"tool": "fs.read", "display_name": "read", "agent_id": "primary",
             "terminal": "term0", "command": "missing.py", "error": "no such file",
             "elapsed_seconds": 0.0, "output_tail": ""},
        ]
        with mock.patch.object(agent_loop, "get_recent_tool_failures",
                               side_effect=lambda **kw: [
                                   r for r in rows
                                   if not kw.get("agent_id")
                                   or r["agent_id"] == kw["agent_id"]]):
            out = tools.get_registry().invoke(
                "diag.tool_failures", {"agent_id": "child-1"}, tools.ToolCtx())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["count"], 1)
        self.assertEqual(out["result"]["failures"][0]["error"], "exit 1")


class WorkStatusToolTests(unittest.TestCase):
    """`/work status` had no tool: an agent could not read its own objective."""

    @classmethod
    def setUpClass(cls):
        tools.register_builtin_tools()

    def test_no_active_work_is_an_answer_not_an_error(self):
        import workgraph
        with mock.patch.object(workgraph, "get_active_work", return_value=None):
            out = tools.get_registry().invoke(
                "work.status", {}, tools.ToolCtx(cwd=os.getcwd()))
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["result"]["active"])

    def test_it_reports_progress_against_the_objective(self):
        import workgraph
        work = {"id": "w1", "objective": "ship the fix", "status": "executing",
                "current_revision": 3, "approved_revision": 2,
                "workflow_template": "", "workflow_phase": "implementation"}
        steps = [{"title": "a", "status": "completed"},
                 {"title": "b", "status": "pending"}]
        with mock.patch.object(workgraph, "get_active_work", return_value=work), \
                mock.patch.object(workgraph, "list_steps", return_value=steps):
            out = tools.get_registry().invoke(
                "work.status", {}, tools.ToolCtx(cwd=os.getcwd()))
        self.assertEqual(out["result"]["objective"], "ship the fix")
        self.assertEqual((out["result"]["steps_done"], out["result"]["steps_total"]),
                         (1, 2))


class BackendProfileHelperTests(unittest.TestCase):
    """`media.*` called a helper that a mail-removal commit had deleted.

    Both generation tools raised NameError on every invocation for as long as
    that lasted, and nothing failed until a user asked for a picture.
    """

    def test_the_helper_the_media_tools_call_exists(self):
        backend_profiles, profile = tools._resolve_backend_profile()
        self.assertTrue(profile.base_url)
        self.assertTrue(hasattr(backend_profiles, "request_auth"))

    def test_media_tools_reach_the_network_instead_of_crashing(self):
        tools.register_builtin_tools()
        with mock.patch("requests.post",
                        side_effect=RuntimeError("network reached")):
            out = tools.get_registry().invoke(
                "media.generate_image", {"prompt": "a cat"},
                tools.ToolCtx(session={}))
        self.assertFalse(out["ok"])
        self.assertNotIn("NameError", str(out.get("error", "")))


if __name__ == "__main__":
    unittest.main()
