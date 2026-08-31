"""Tests for the API contract two agents coordinate through.

The contract is the thing that stops a Helpwo frontend agent and a laintas_cli
backend agent from agreeing on nothing. Its value is entirely in the parts that
refuse: a shape that was never agreed, an implementation claimed before the
shape was settled, a response that does not match what was promised, code that
moved after the promise was made. Those are what these tests pin down.
"""

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer

import contract_store as cs


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


ORDERS = {
    "summary": "List orders",
    "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
        "type": "object",
        "required": ["items", "total"],
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "required": ["id"],
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}},
            "total": {"type": "integer"},
        },
    }}}}},
}


class ContractStateMachineTests(unittest.TestCase):
    def setUp(self):
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def test_an_absent_contract_reads_as_empty_rather_than_failing(self):
        status = cs.status()
        self.assertTrue(status["ok"])
        self.assertFalse(status["exists"])
        self.assertEqual(status["total"], 0)

    def test_operation_keys_must_be_a_method_and_an_absolute_path(self):
        for bad in ("orders", "GET orders", "FETCH /api/orders", ""):
            with self.assertRaises(cs.ContractError):
                cs.normalize_operation(bad)
        key, method, path = cs.normalize_operation("get /api/orders")
        self.assertEqual((key, method, path), ("GET /api/orders", "get", "/api/orders"))

    def test_propose_writes_a_valid_openapi_document(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo", "the list screen needs it")
        spec = json.loads(cs.spec_path().read_text(encoding="utf-8"))
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("get", spec["paths"]["/api/orders"])
        self.assertEqual(spec["paths"]["/api/orders"]["get"]["summary"], "List orders")

    def test_a_consumer_can_build_before_the_provider_exists(self):
        """Proposing must not block: that is the whole point of proposing."""
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        mock = cs.mock_response("GET /api/orders")
        self.assertEqual(mock["status"], "200")
        self.assertEqual(mock["body"], {"items": [{"id": 0, "name": "string"}], "total": 0})

    def test_implementing_an_unagreed_shape_is_refused(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        Path("server.py").write_text("# routes", encoding="utf-8")
        with self.assertRaises(cs.ContractError) as caught:
            cs.implement("GET /api/orders", "cli", ["server.py"])
        self.assertIn("agree", str(caught.exception))

    def test_implement_requires_the_named_files_to_exist(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        with self.assertRaises(cs.ContractError) as caught:
            cs.implement("GET /api/orders", "cli", ["nowhere.py"])
        self.assertIn("do not exist", str(caught.exception))

    def test_agreeing_twice_is_refused_so_a_change_cannot_slip_through(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        with self.assertRaises(cs.ContractError):
            cs.agree("GET /api/orders", "cli")

    def test_reproposing_an_agreed_shape_is_a_counter_offer(self):
        """The other side agreed to something else; it has to look again."""
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        changed = json.loads(json.dumps(ORDERS))
        changed["responses"]["200"]["content"]["application/json"]["schema"]["required"] = ["items"]
        result = cs.propose("GET /api/orders", changed, "helpwo", "total is optional now")
        self.assertEqual(result["state"], "proposed")
        self.assertEqual(result["wasState"], "agreed")

    def test_read_can_be_filtered_to_the_slice_an_agent_needs(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        cs.propose("GET /api/users", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        self.assertEqual(cs.read()["count"], 2)
        agreed = cs.read(state="agreed")
        self.assertEqual(list(agreed["operations"]), ["GET /api/orders"])

    def test_history_records_who_moved_it_and_why(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo", "list screen")
        cs.agree("GET /api/orders", "cli", "fine as written")
        entry = cs.read("GET /api/orders")["lock"]
        self.assertEqual([h["state"] for h in entry["history"]], ["proposed", "agreed"])
        self.assertEqual(entry["history"][0]["actor"], "helpwo")
        self.assertEqual(entry["history"][1]["note"], "fine as written")


class ContractCommittabilityTests(unittest.TestCase):
    """The contract lives under `.laintas/`, which projects gitignore.

    That directory is laintas_cli's runtime state and checking it in is a
    documented mistake — but the contract is not runtime state, and a contract
    that never appears in a diff is not an agreement anybody reviews. Creating
    it therefore has to carve it back out of the ignore rule.
    """

    def setUp(self):
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def _git(self, *args):
        import subprocess
        return subprocess.run(("git",) + args, cwd=self.root, capture_output=True,
                              text=True, timeout=30)

    def test_creating_the_contract_makes_it_visible_to_git(self):
        if self._git("--version").returncode != 0:
            self.skipTest("git is not available")
        self._git("init", "-q", ".")
        Path(".gitignore").write_text("node_modules/\n.laintas/\n", encoding="utf-8")
        (self.root / ".laintas").mkdir(exist_ok=True)
        (self.root / ".laintas" / "memory.json").write_text("{}", encoding="utf-8")

        cs.propose("GET /api/orders", ORDERS, "helpwo")

        listed = self._git("status", "--porcelain", "-uall").stdout
        self.assertIn(".laintas/contract/openapi.json", listed)
        self.assertIn(".laintas/contract/contract.lock.json", listed)
        # Runtime state stays ignored — the carve-out is for the contract only.
        self.assertNotIn(".laintas/memory.json", listed)

    def test_the_exception_is_written_once(self):
        Path(".gitignore").write_text(".laintas/\n", encoding="utf-8")
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        first = Path(".gitignore").read_text(encoding="utf-8")
        cs.propose("GET /api/users", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        self.assertEqual(Path(".gitignore").read_text(encoding="utf-8"), first)
        self.assertEqual(first.count("!.laintas/contract/"), 1)

    def test_a_project_that_never_ignored_laintas_is_left_alone(self):
        Path(".gitignore").write_text("node_modules/\n", encoding="utf-8")
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        self.assertEqual(Path(".gitignore").read_text(encoding="utf-8"), "node_modules/\n")

    def test_no_gitignore_means_nothing_to_amend(self):
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        self.assertFalse(Path(".gitignore").exists())


class ContractDriftTests(unittest.TestCase):
    def setUp(self):
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chdir(self.root)
        Path("server.py").write_text("# routes\n", encoding="utf-8")
        cs.propose("GET /api/orders", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        cs.implement("GET /api/orders", "cli", ["server.py"])

    def tearDown(self):
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def test_a_settled_contract_reports_no_drift(self):
        self.assertTrue(cs.drift()["ok"])

    def test_code_moving_without_the_contract_is_drift(self):
        Path("server.py").write_text("# routes\n# and something else\n", encoding="utf-8")
        report = cs.drift()
        self.assertFalse(report["ok"])
        self.assertEqual(report["drifted"][0]["operation"], "GET /api/orders")
        self.assertIn("implementing file changed", report["drifted"][0]["reasons"][0])

    def test_the_contract_moving_without_the_code_is_also_drift(self):
        spec = json.loads(cs.spec_path().read_text(encoding="utf-8"))
        spec["paths"]["/api/orders"]["get"]["summary"] = "List orders, paginated"
        cs.spec_path().write_text(json.dumps(spec, indent=2), encoding="utf-8")
        report = cs.drift()
        self.assertFalse(report["ok"])
        self.assertIn("declared shape changed", report["drifted"][0]["reasons"][0])

    def test_drift_is_read_only_unless_asked_to_mark(self):
        Path("server.py").write_text("# changed\n", encoding="utf-8")
        cs.drift()
        self.assertEqual(cs.read("GET /api/orders")["lock"]["state"], "implemented")
        cs.drift(mark=True)
        self.assertEqual(cs.read("GET /api/orders")["lock"]["state"], "drift")


class ContractVerificationTests(unittest.TestCase):
    """`implemented` is a claim; `verified` is the service answering correctly."""

    def setUp(self):
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chdir(self.root)
        Path("server.py").write_text("# routes\n", encoding="utf-8")

        self.payload = {"items": [{"id": 1, "name": "first"}], "total": 1}
        self.status_code = 200
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps(test.payload).encode()
                self.send_response(test.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: A003
                pass

        self.port = _free_port()
        self.server = TCPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        cs.propose("GET /api/orders", ORDERS, "helpwo")
        cs.agree("GET /api/orders", "cli")
        cs.implement("GET /api/orders", "cli", ["server.py"],
                     f"http://127.0.0.1:{self.port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def test_a_matching_response_reaches_verified(self):
        report = cs.verify()
        self.assertTrue(report["ok"], report)
        self.assertEqual(cs.read("GET /api/orders")["lock"]["state"], "verified")

    def test_a_wrong_field_type_is_caught(self):
        self.payload = {"items": [], "total": "three"}
        report = cs.verify()
        self.assertFalse(report["ok"])
        self.assertIn("expected integer", report["results"][0]["problems"][0])
        self.assertEqual(cs.read("GET /api/orders")["lock"]["state"], "drift")

    def test_a_missing_required_field_is_caught(self):
        self.payload = {"items": []}
        report = cs.verify()
        self.assertFalse(report["ok"])
        self.assertIn("missing required property 'total'", report["results"][0]["problems"][0])

    def test_a_wrong_type_inside_a_list_item_is_caught(self):
        self.payload = {"items": [{"id": "not-a-number"}], "total": 1}
        report = cs.verify()
        self.assertFalse(report["ok"])
        self.assertIn("items[0].id", report["results"][0]["problems"][0])

    def test_an_undeclared_status_is_caught(self):
        self.status_code = 503
        report = cs.verify()
        self.assertFalse(report["ok"])
        self.assertIn("not declared", report["results"][0]["problems"][0])

    def test_an_unreachable_service_is_reported_not_silently_passed(self):
        self.server.shutdown()
        self.server.server_close()
        report = cs.verify()
        self.assertFalse(report["ok"])
        self.assertIn("could not reach", report["results"][0]["error"])

    def test_verifying_nothing_implemented_is_not_a_failure(self):
        report = cs.verify(cwd=str(self.root / "empty"))
        self.assertTrue(report["ok"])
        self.assertEqual(report["results"], [])


class ContractFingerprintTests(unittest.TestCase):
    """The fingerprint is shared with Helpwo's TypeScript half.

    Both sides hash canonical JSON — sorted keys, no whitespace — and keep the
    first 32 hex characters of SHA-256. If the two ever disagree, every
    operation looks permanently drifted to whichever side did not write last,
    so the canonical form is pinned here rather than left implicit.
    """

    def test_canonical_form_is_key_order_independent(self):
        import hashlib
        spec_a = {"paths": {"/x": {"get": {"b": 2, "a": 1}}}}
        spec_b = {"paths": {"/x": {"get": {"a": 1, "b": 2}}}}
        self.assertEqual(cs._operation_fingerprint(spec_a, "get", "/x"),
                         cs._operation_fingerprint(spec_b, "get", "/x"))
        blob = json.dumps({"a": 1, "b": 2}, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        self.assertEqual(cs._operation_fingerprint(spec_a, "get", "/x"),
                         hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32])

    def test_non_ascii_is_not_escaped(self):
        """ensure_ascii=False matches JSON.stringify, which also leaves it raw."""
        import hashlib
        spec = {"paths": {"/x": {"get": {"summary": "orders"}}}}
        blob = '{"summary":"orders"}'
        self.assertEqual(cs._operation_fingerprint(spec, "get", "/x"),
                         hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32])


if __name__ == "__main__":
    unittest.main()
