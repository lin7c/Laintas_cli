"""A sub-agent's contract, and the gate that decides whether it was met.

`agent.spawn` used to take free text and return free text, so "done" meant "the
child stopped talking" and a parent had to re-read prose to find out whether it
could use the result. Measured on the 2026-08-28 review batch: two of six
children "succeeded", both exited on `provider_stop` rather than the completion
protocol, and nothing noticed.
"""
import os
import tempfile
import unittest
from unittest import mock

import agent_contract
import agent_loop
import agent_roles
import tools


class NormalizeTests(unittest.TestCase):
    def test_a_contract_with_no_outputs_is_rejected_at_authoring_time(self):
        """A contract nobody can fail reads like a guarantee and is not one."""
        with self.assertRaises(agent_contract.ContractError):
            agent_contract.normalize({"goal": "look around"})

    def test_unknown_types_and_checks_are_refused(self):
        with self.assertRaises(agent_contract.ContractError):
            agent_contract.normalize({"outputs": [{"name": "x", "type": "blob"}]})
        with self.assertRaises(agent_contract.ContractError):
            agent_contract.normalize({
                "outputs": [{"name": "x"}],
                "acceptance": [{"kind": "vibes", "output": "x"}]})

    def test_no_contract_is_not_an_error(self):
        self.assertIsNone(agent_contract.normalize(None))
        self.assertIsNone(agent_contract.normalize({}))

    def test_a_bare_output_name_is_accepted_and_defaults_to_required_string(self):
        c = agent_contract.normalize({"outputs": ["report"]})
        self.assertEqual([{"name": "report", "type": "string",
                           "required": True, "description": ""}], c["outputs"])

    def test_hwo_optional_and_default_outputs_are_not_made_required(self):
        c = agent_contract.from_io({"out": [
            {"name": "required", "type": "file"},
            {"name": "optional", "type": "string", "optional": True},
            {"name": "fallback", "type": "number", "default": "3"},
        ]})
        required = {out["name"]: out["required"] for out in c["outputs"]}
        self.assertEqual({"required": True, "optional": False,
                          "fallback": False}, required)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = os.path.join(self.tmp.name, "src.py")
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"line {i}" for i in range(1, 51)))

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return name

    def test_a_missing_required_output_is_named(self):
        c = agent_contract.normalize({"outputs": [{"name": "report", "type": "file"}]})
        result = agent_contract.verify(c, {}, self.tmp.name)
        self.assertFalse(result["ok"])
        self.assertIn("missing required output 'report'", result["gaps"][0])

    def test_a_file_output_must_actually_be_a_file(self):
        c = agent_contract.normalize({"outputs": [{"name": "report", "type": "file"}]})
        claimed = agent_contract.verify(c, {"report": "report.md"}, self.tmp.name)
        self.assertFalse(claimed["ok"])
        self.assertIn("not a file that exists", claimed["gaps"][0])

        self._write("report.md", "findings")
        self.assertTrue(agent_contract.verify(
            c, {"report": "report.md"}, self.tmp.name)["ok"])

    def test_an_empty_file_does_not_count_as_delivered(self):
        c = agent_contract.normalize({"outputs": [{"name": "report", "type": "file"}]})
        self._write("report.md", "")
        result = agent_contract.verify(c, {"report": "report.md"}, self.tmp.name)
        self.assertFalse(result["ok"])
        self.assertIn("empty file", result["gaps"][0])

    def test_types_are_checked(self):
        c = agent_contract.normalize({"outputs": [
            {"name": "count", "type": "number"},
            {"name": "items", "type": "array"},
        ]})
        result = agent_contract.verify(
            c, {"count": "seven", "items": {"a": 1}}, self.tmp.name)
        self.assertEqual(2, len(result["gaps"]))
        self.assertTrue(agent_contract.verify(
            c, {"count": 7, "items": [1, 2]}, self.tmp.name)["ok"])

    def test_a_check_reads_the_file_a_file_output_points_at(self):
        c = agent_contract.normalize({
            "outputs": [{"name": "report", "type": "file"}],
            "acceptance": [{"kind": "contains", "output": "report",
                            "value": "## Findings"}]})
        self._write("report.md", "nothing to see")
        self.assertFalse(agent_contract.verify(
            c, {"report": "report.md"}, self.tmp.name)["ok"])
        self._write("report.md", "## Findings\nreal ones")
        self.assertTrue(agent_contract.verify(
            c, {"report": "report.md"}, self.tmp.name)["ok"])

    def test_a_citation_whose_line_does_not_exist_is_not_evidence(self):
        """The check that makes 'cite path:line' mean something.

        A model that has stopped reading and started producing will still emit
        confident-looking citations; only the file can say whether the line is
        there.
        """
        c = agent_contract.normalize({
            "outputs": [{"name": "report", "type": "file"}],
            "acceptance": [{"kind": "line_ref", "output": "report", "value": 2}]})
        self._write("report.md", "bug at src.py:12 and src.py:900")
        result = agent_contract.verify(c, {"report": "report.md"}, self.tmp.name)
        self.assertFalse(result["ok"])
        self.assertIn("cites 1 real path:line", result["gaps"][0])

        self._write("report.md", "bug at src.py:12 and src.py:31")
        self.assertTrue(agent_contract.verify(
            c, {"report": "report.md"}, self.tmp.name)["ok"])

    def test_evidence_requires_at_least_one_real_location(self):
        c = agent_contract.normalize({
            "outputs": [{"name": "summary", "type": "string"}],
            "evidence": ["path:line for every finding"]})
        self.assertFalse(agent_contract.verify(
            c, {"summary": "found some problems"}, self.tmp.name)["ok"])
        self.assertTrue(agent_contract.verify(
            c, {"summary": "problem at src.py:5"}, self.tmp.name)["ok"])

    def test_no_contract_verifies_trivially(self):
        self.assertTrue(agent_contract.verify(None, {}, self.tmp.name)["ok"])


class RoleScopeTests(unittest.TestCase):
    def test_a_role_scope_can_never_remove_the_completion_protocol(self):
        """The reviewer role's prompt told it to call tools its whitelist
        forbade, so review children ended on provider_stop instead of
        reporting."""
        self.assertNotIn("task.complete",
                         agent_roles.get_role("reviewer").allowed_tools)
        for tool in ("task.complete", "agent_return"):
            self.assertTrue(agent_roles.is_tool_allowed_for_role(tool, "reviewer"))
        self.assertFalse(agent_roles.is_tool_allowed_for_role("shell.exec", "reviewer"))

    def test_path_scope_resolves_symlinks_before_allowing_a_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            allowed = os.path.join(tmp, "allowed")
            outside = os.path.join(tmp, "outside")
            os.makedirs(allowed)
            os.makedirs(outside)
            os.symlink(outside, os.path.join(allowed, "escape"))
            contract = agent_contract.normalize({
                "outputs": ["report"],
                "scope": {"paths": ["allowed"]},
            })
            self.assertTrue(agent_contract.path_in_scope(
                contract, os.path.join(allowed, "new.txt"), tmp))
            self.assertFalse(agent_contract.path_in_scope(
                contract, os.path.join(allowed, "escape", "new.txt"), tmp))

    def test_path_scope_blocks_unbounded_shell_and_terminal_commands(self):
        contract = agent_contract.normalize({
            "outputs": ["report"], "scope": {"paths": ["src"]}})
        for name in ("shell.exec", "terminal.create", "terminal.send",
                     "terminal.exec"):
            result = agent_loop._authorize_tool_call(
                name, "touch ../escape", {"_contract": contract},
                agent_id="child", allowed_tool_names={name},
                is_shell_flavored=True, fail_ledger={}, fail_ledger_err={},
                repeat_block_limit=3)
            self.assertIsNotNone(result, name)
            self.assertIn("scope.paths", result["error"])

    def test_role_refusal_is_recorded_as_a_capability_gap(self):
        state = {}
        refusal = agent_loop._authorize_tool_call(
            "shell.exec", "git diff", state, agent_id="reviewer-child",
            allowed_tool_names=set(), is_shell_flavored=True,
            fail_ledger={}, fail_ledger_err={}, repeat_block_limit=3)
        agent_loop._record_capability_gap(state, "shell.exec", refusal)
        agent_loop._record_capability_gap(state, "shell.exec", refusal)

        self.assertEqual([{
            "tool": "shell.exec",
            "kind": "unavailable",
            "reason": ("BLOCKED: tool 'shell.exec' is not available to agent "
                       "'reviewer-child'."),
        }], state["_capability_gaps"])


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.contract = agent_contract.normalize(
            {"outputs": [{"name": "report", "type": "string"}]})

    def test_task_complete_records_declared_outputs_as_data(self):
        state = {"_contract": self.contract, "cwd": self.tmp.name}
        ctx = tools.ToolCtx(cwd=self.tmp.name, state=state, agent_id="child")
        result = tools._bi_task_complete(
            {"summary": "done", "outputs": {"report": "all clear"}}, ctx)
        self.assertTrue(result.get("ok", True), result)
        self.assertEqual({"report": "all clear"}, state["_submitted_outputs"])

    def test_finishing_without_outputs_is_declined_while_the_child_can_fix_it(self):
        state = {"_contract": self.contract, "cwd": self.tmp.name}
        ctx = tools.ToolCtx(cwd=self.tmp.name, state=state, agent_id="child")
        result = tools._bi_task_complete({"summary": "done"}, ctx)
        self.assertFalse(result["ok"])
        self.assertTrue(result["_advisory"])
        self.assertIn("outputs={...}", result["error"])
        self.assertNotIn("_submitted_outputs", state)

    def test_a_json_string_of_outputs_is_accepted(self):
        state = {"_contract": self.contract, "cwd": self.tmp.name}
        ctx = tools.ToolCtx(cwd=self.tmp.name, state=state, agent_id="child")
        tools._bi_task_complete(
            {"summary": "done", "outputs": '{"report": "ok"}'}, ctx)
        self.assertEqual({"report": "ok"}, state["_submitted_outputs"])

    def test_an_uncontracted_task_complete_is_unchanged(self):
        state = {"cwd": self.tmp.name}
        ctx = tools.ToolCtx(cwd=self.tmp.name, state=state, agent_id="child")
        self.assertTrue(tools._bi_task_complete({"summary": "done"}, ctx)
                        .get("ok", True))


class AcceptanceGateTests(unittest.TestCase):
    """The gate itself: a claim is not a finding."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # event_log writes to <cwd>/.laintas/events.jsonl. Without this the
        # gate's `contract_checked` events land in the REPO's own event log and
        # show up in a real session's history as agents that never ran — which
        # is exactly what happened the first time this suite was run.
        _old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, _old_cwd)
        self.child = agent_loop.register_agent(name="gate-child", depth=1,
                                               role="subagent")
        self.addCleanup(agent_loop.unregister_agent, self.child.id)
        self.child.contract = agent_contract.normalize(
            {"outputs": [{"name": "report", "type": "file"}]})
        self.child.state["cwd"] = self.tmp.name

    def _settle(self, submissions):
        """Drive the single-pass gate and detect any accidental model rerun."""
        self.child.state["_submitted_outputs"] = list(submissions)[0]
        with mock.patch.object(agent_loop, "run_agent_loop") as rerun:
            result = agent_loop._settle_contract(
                self.child, {"success": True}, deps=None, session={},
                events_cb=None)
        return result, rerun

    def _write_report(self):
        with open(os.path.join(self.tmp.name, "r.md"), "w", encoding="utf-8") as fh:
            fh.write("findings")

    def test_a_satisfied_contract_is_verified_without_a_repair_round(self):
        self._write_report()
        _result, rerun = self._settle([{"report": "r.md"}])
        rerun.assert_not_called()
        self.assertEqual(agent_contract.STAGE_VERIFIED, self.child.stage)
        self.assertTrue(self.child.verification["ok"])

    def test_a_gap_is_rejected_without_automatic_repair(self):
        _result, rerun = self._settle([{"report": "r.md"}])
        rerun.assert_not_called()
        self.assertEqual(agent_contract.STAGE_REJECTED, self.child.stage)
        self.assertFalse(self.child.verification["ok"])
        self.assertIn("not a file that exists", self.child.verification["gaps"][0])

    def test_an_uncontracted_child_is_not_gated(self):
        self.child.contract = None
        _result, rerun = self._settle([{}])
        rerun.assert_not_called()
        self.assertEqual(agent_contract.STAGE_DONE, self.child.stage)


class RoleContractTests(unittest.TestCase):
    """Judgement roles carry their contract whether or not the caller wrote one.

    Measured 2026-08-28 in a live session: four reviewer children were spawned
    and not one carried a contract, because the contract was purely opt-in and
    the spawning model never opted in. Capability is not adoption.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "a.py")
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("x\n" * 40)

    def _reviewer(self):
        return agent_contract.normalize(
            agent_roles.get_role("reviewer").default_contract)

    def test_the_judgement_roles_carry_a_contract(self):
        for name in ("reviewer", "silent-failure-hunter", "tester"):
            self.assertTrue(agent_roles.get_role(name).default_contract, name)
        # A role that produces code, not a judgement, is not forced into one.
        self.assertIsNone(agent_roles.get_role("simplifier").default_contract)

    def test_a_finding_must_cite_a_line_that_exists(self):
        c = self._reviewer()
        fake = {"findings": "leak at a.py:900 " + "x" * 120, "issue_count": 1}
        result = agent_contract.verify(c, fake, self.tmp.name)
        self.assertFalse(result["ok"])
        self.assertIn("does not count", result["gaps"][0])

    def test_citations_must_match_the_number_of_findings_claimed(self):
        """Five findings and one real location is not five findings."""
        c = self._reviewer()
        sub = {"findings": "a.py:900 a.py:901 a.py:12 " + "x" * 120,
               "issue_count": 3}
        result = agent_contract.verify(c, sub, self.tmp.name)
        self.assertFalse(result["ok"])
        self.assertIn("needs 3", result["gaps"][0])

    def test_a_clean_review_owes_no_citation(self):
        """A gate an honest reviewer cannot pass teaches it to invent findings."""
        c = self._reviewer()
        clean = {"findings": "Read a.py end to end; nothing above the "
                             "confidence bar. " + "x" * 90,
                 "issue_count": 0}
        self.assertTrue(agent_contract.verify(c, clean, self.tmp.name)["ok"])

    def test_a_clean_review_still_has_to_say_what_it_reviewed(self):
        c = self._reviewer()
        result = agent_contract.verify(
            c, {"findings": "looks fine", "issue_count": 0}, self.tmp.name)
        self.assertFalse(result["ok"])
        self.assertIn("needs 120", result["gaps"][0])

    def test_a_caller_contract_adds_to_the_role_contract_and_cannot_drop_it(self):
        role = self._reviewer()
        caller = agent_contract.normalize({
            "outputs": [{"name": "report", "type": "file"}],
            "acceptance": [{"kind": "contains", "output": "report",
                            "value": "## Findings"}]})
        merged = agent_contract.merge(role, caller)
        names = {o["name"] for o in merged["outputs"]}
        self.assertEqual({"findings", "issue_count", "report"}, names)
        kinds = {c["kind"] for c in merged["acceptance"]}
        self.assertEqual({"min_length", "line_ref", "contains"}, kinds)

    def test_a_caller_cannot_make_a_role_output_optional(self):
        role = self._reviewer()
        sneaky = agent_contract.normalize({
            "outputs": [{"name": "findings", "type": "string",
                         "required": False}]})
        merged = agent_contract.merge(role, sneaky)
        findings = next(o for o in merged["outputs"] if o["name"] == "findings")
        self.assertTrue(findings["required"])

    def test_spawning_with_a_judgement_role_applies_the_contract(self):
        captured = {}

        def fake_thread(*_a, **kw):
            captured["started"] = True
            return mock.Mock()

        parent = agent_loop.register_agent(name="rc-parent", role="primary")
        self.addCleanup(agent_loop.unregister_agent, parent.id)
        with mock.patch.object(agent_loop.threading, "Thread", fake_thread):
            child_id = agent_loop.spawn_subagent(
                parent_id=parent.id, task="review a.py", deps=None,
                role="reviewer")
        self.addCleanup(agent_loop.unregister_agent, child_id)
        child = agent_loop.get_agent(child_id)
        self.assertIsNotNone(child.contract, "reviewer spawned without a contract")
        self.assertEqual({"findings", "issue_count"},
                         {o["name"] for o in child.contract["outputs"]})
        self.assertIn("<contract>", child.state["_contract"] and
                      agent_contract.render(child.state["_contract"]))


if __name__ == "__main__":
    unittest.main()
