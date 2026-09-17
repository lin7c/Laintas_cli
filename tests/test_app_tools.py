"""Agent-facing /app tools: list / manifest.get / trust.request / start / stop.

The properties pinned here are the ones the design depends on:
  * discovery is read-only and reports broken manifests instead of failing;
  * trust is only ever recorded behind the user's approval, and never when
    no approval channel exists (that is not the user saying no);
  * start refuses untrusted apps before any approval is requested;
  * stop only touches a live application sub-terminal, behind approval.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app_host
import context_router
import paths
import tools


DEMO_MANIFEST = {
    "name": "demo",
    "description": "demo app",
    "command": "node server.js",
    "prompt": "You serve the demo app.",
}


def _alive_app_terminal(app: str) -> SimpleNamespace:
    """A registry entry that is exactly `app`'s live sub-terminal."""
    return SimpleNamespace(command=app_host.terminal_command(app),
                           session=SimpleNamespace(is_alive=lambda: True))


class _AppToolBase(unittest.TestCase):
    """Temp LAINTAS_HOME + project dir with one valid and one broken manifest."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.work = Path(tmp.name) / "work"
        (self.work / ".laintas" / "apps").mkdir(parents=True)
        patcher = mock.patch.object(paths, "LAINTAS_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.work / ".laintas" / "apps" / "demo.json").write_text(
            tools.json.dumps(DEMO_MANIFEST), encoding="utf-8")
        # Name must match [a-z0-9][a-z0-9._-]{0,31}: a space makes it invalid.
        (self.work / ".laintas" / "apps" / "broken.json").write_text(
            tools.json.dumps(dict(DEMO_MANIFEST, name="Bad Name")), encoding="utf-8")

    def _ctx(self, *, approve=None, get_terminal=None):
        deps = None
        if approve is not None:
            deps = SimpleNamespace(request_command_approval=approve)
        return tools.ToolCtx(deps=deps, session={}, cwd=str(self.work),
                             get_terminal=get_terminal)


class AppListTests(_AppToolBase):
    def test_lists_valid_manifests_and_reports_broken_ones(self):
        ctx = self._ctx()
        result = tools._bi_app_list({}, ctx)
        self.assertTrue(result["ok"])
        names = [app["name"] for app in result["apps"]]
        self.assertEqual(names, ["demo"])
        entry = result["apps"][0]
        self.assertEqual(entry["scope"], "project")
        self.assertFalse(entry["trusted"])
        self.assertFalse(entry["running"])
        self.assertEqual(entry["persistence"], "none")
        self.assertTrue(any("broken.json" in p for p in result["problems"]))

    def test_marks_running_and_trusted_state(self):
        manifest = app_host.discover_manifests(str(self.work))[0]["demo"]
        app_host.trust(manifest)
        ctx = self._ctx(get_terminal=lambda name: _alive_app_terminal("demo"))
        entry = tools._bi_app_list({}, ctx)["apps"][0]
        self.assertTrue(entry["trusted"])
        self.assertTrue(entry["running"])


class AppManifestGetTests(_AppToolBase):
    def test_returns_raw_manifest_with_state(self):
        result = tools._bi_app_manifest_get({"name": "demo"}, self._ctx())
        self.assertTrue(result["ok"])
        self.assertEqual(result["manifest"], DEMO_MANIFEST)
        self.assertEqual(result["scope"], "project")
        self.assertFalse(result["trusted"])

    def test_unknown_name_fails_cleanly(self):
        result = tools._bi_app_manifest_get({"name": "nope"}, self._ctx())
        self.assertFalse(result["ok"])
        self.assertIn("nope", result["error"])

    def test_missing_name_parameter(self):
        result = tools._bi_app_manifest_get({}, self._ctx())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing 'name'")


class AppTrustRequestTests(_AppToolBase):
    def _manifest(self):
        return app_host.discover_manifests(str(self.work))[0]["demo"]

    def test_denial_records_nothing(self):
        ctx = self._ctx(approve=lambda action, detail: False)
        result = tools._bi_app_trust_request({"name": "demo"}, ctx)
        self.assertFalse(result["ok"])
        self.assertTrue(result["_user_denied"])
        self.assertFalse(app_host.is_trusted(self._manifest()))

    def test_approval_records_trust(self):
        ctx = self._ctx(approve=lambda action, detail: True)
        result = tools._bi_app_trust_request(
            {"name": "demo", "note": "runs only shell.exec"}, ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["trusted"], "demo")
        self.assertTrue(app_host.is_trusted(self._manifest()))

    def test_no_approval_channel_is_not_a_denial(self):
        result = tools._bi_app_trust_request({"name": "demo"}, self._ctx())
        self.assertFalse(result["ok"])
        self.assertIn("no approval channel", result["error"])
        self.assertNotIn("_user_denied", result)
        self.assertFalse(app_host.is_trusted(self._manifest()))

    def test_already_trusted_is_idempotent_without_asking(self):
        app_host.trust(self._manifest())
        asked = []

        def _ask(action, detail):
            asked.append(action)
            return True

        result = tools._bi_app_trust_request({"name": "demo"},
                                             self._ctx(approve=_ask))
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_trusted"])
        self.assertEqual(asked, [])


class AppStartTests(_AppToolBase):
    def test_untrusted_refused_before_any_approval(self):
        asked = []
        ctx = self._ctx(approve=lambda a, d: asked.append(a) or True)
        result = tools._bi_app_start({"name": "demo"}, ctx)
        self.assertFalse(result["ok"])
        self.assertIn("not trusted", result["error"])
        self.assertEqual(asked, [])

    def test_approval_then_launch(self):
        manifest = app_host.discover_manifests(str(self.work))[0]["demo"]
        app_host.trust(manifest)
        ctx = self._ctx(approve=lambda a, d: True)
        with mock.patch("laintas_cli._launch_app_subterminal",
                        return_value={"status": "ready"}) as launch:
            result = tools._bi_app_start({"name": "demo"}, ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready")
        launch.assert_called_once_with(
            "demo", persistent=False, options={}, agent_registry=None,
            open_url=False)

    def test_failed_launch_is_an_error_not_a_fake_success(self):
        manifest = app_host.discover_manifests(str(self.work))[0]["demo"]
        app_host.trust(manifest)
        ctx = self._ctx(approve=lambda a, d: True)
        with mock.patch("laintas_cli._launch_app_subterminal",
                        return_value=None):
            result = tools._bi_app_start({"name": "demo"}, ctx)
        self.assertFalse(result["ok"])
        self.assertIn("could not start", result["error"])

    def test_already_running_neither_asks_nor_launches(self):
        manifest = app_host.discover_manifests(str(self.work))[0]["demo"]
        app_host.trust(manifest)
        asked = []
        ctx = self._ctx(approve=lambda a, d: asked.append(a) or True,
                        get_terminal=lambda name: _alive_app_terminal("demo"))
        with mock.patch("laintas_cli._launch_app_subterminal") as launch:
            result = tools._bi_app_start({"name": "demo"}, ctx)
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_running"])
        self.assertEqual(asked, [])
        launch.assert_not_called()


class AppStopTests(_AppToolBase):
    def test_not_running_fails_cleanly(self):
        result = tools._bi_app_stop({"name": "demo"}, self._ctx())
        self.assertFalse(result["ok"])
        self.assertIn("not running", result["error"])

    def test_denial_stops_nothing(self):
        ctx = self._ctx(approve=lambda a, d: False,
                        get_terminal=lambda name: _alive_app_terminal("demo"))
        with mock.patch("laintas_cli._close_app_subterminal") as close:
            result = tools._bi_app_stop({"name": "demo"}, ctx)
        self.assertFalse(result["ok"])
        self.assertTrue(result["_user_denied"])
        close.assert_not_called()

    def test_approval_closes(self):
        ctx = self._ctx(approve=lambda a, d: True,
                        get_terminal=lambda name: _alive_app_terminal("demo"))
        with mock.patch("laintas_cli._close_app_subterminal",
                        return_value=True) as close:
            result = tools._bi_app_stop({"name": "demo"}, ctx)
        self.assertTrue(result["ok"])
        close.assert_called_once_with("demo")


class AppRoutingTests(unittest.TestCase):
    def test_hosting_phrases_route_to_app_tools(self):
        catalogue = [SimpleNamespace(name=name) for name in
                     ("app.list", "app.manifest.get", "app.trust.request",
                      "app.start", "app.stop", "fs.read", "terminal.exec")]
        for query in ("host the application for my node server",
                      "app manifest for the demo",
                      "托管应用"):
            selected = context_router.select_tool_names(query, catalogue)
            self.assertIn("app.list", selected, query)
            self.assertIn("app.start", selected, query)


if __name__ == "__main__":
    unittest.main()
