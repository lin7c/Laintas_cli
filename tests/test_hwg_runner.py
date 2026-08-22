import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hwg_runner
import workflow_state
from hwg_adapter import parse as parse_hwg, validate as validate_hwg


class _Chdir:
    def __init__(self, path):
        self.path = path
        self.old = None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old)


class HwgRunnerTests(unittest.TestCase):
    def test_events_identify_only_the_active_node(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "(a.hwo)#a#\n(b.hwo)#b#\n#a# -> #b#\n",
                encoding="utf-8",
            )
            events = []
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", return_value={
                    "ok": True, "msg": "done", "outputs": {}}):
                result = hwg_runner.run_hwg_file(
                    "flow.hwg", deps=object(), session={},
                    events_cb=lambda rows: events.extend(rows))

        self.assertTrue(result["ok"], result)
        transitions = [(row["type"], row.get("node")) for row in events
                       if row["type"] in {"node_started", "node_completed"}]
        self.assertEqual(transitions, [
            ("node_started", "a"), ("node_completed", "a"),
            ("node_started", "b"), ("node_completed", "b"),
        ])

    def test_parser_accepts_node_policy(self):
        ast = parse_hwg('(a.hwo)#a# { retry: 2, timeout: "10m", cache: "1h" }')
        self.assertEqual(ast[0]["policy"], {
            "retry": 2,
            "timeout": "10m",
            "cache": "1h",
        })

    def test_compile_summarizes_policy(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(a.hwo)#a# { retry: 1, timeout: "5s" }\n',
                encoding="utf-8",
            )

            result = hwg_runner.compile_hwg_file("flow.hwg")

            self.assertTrue(result["ok"], result["msg"])
            self.assertIn("retry", result["msg"])
            self.assertIn("timeout", result["msg"])

    def test_run_pauses_at_manual_node_and_resume_completes(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "!(review.hwo)#review#\n(report.hwo)#report#\n"
                "#review# -> { on: PASS } #report#\n",
                encoding="utf-8",
            )

            first = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})
            self.assertTrue(first["paused"])
            run_id = first["runId"]
            graph_tasks = [
                task for task in __import__("task_manager").list_tasks(cwd=tmp)
                if task.get("metadata", {}).get("scopeType") == "hwg-run"
            ]
            self.assertTrue(graph_tasks)
            self.assertTrue(all(not task["session_only"] for task in graph_tasks))

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", return_value={
                "ok": True,
                "msg": '{"verdict":"PASS","report":"done"}',
                "outputs": {"verdict": "PASS", "report": "done"},
            }):
                resumed = hwg_runner.resume_hwg_run(run_id, deps=object(), session={})

            self.assertTrue(resumed["ok"], resumed["msg"])
            stored = workflow_state.load_run(run_id)
            self.assertEqual(stored["status"], "completed")
            self.assertEqual(stored["history"], ["review", "report"])

    def test_retry_then_success_and_structured_verdict(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(test.hwo)#test# [out(verdict: string)] { retry: 1 }\n'
                '(done.hwo)#done#\n'
                '#test# -> { on: verdict == "PASS" } #done#\n',
                encoding="utf-8",
            )
            calls = []

            def fake_run(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return {"ok": False, "msg": "temporary failure", "outputs": {}}
                return {"ok": True, "msg": '{"verdict":"PASS"}', "outputs": {"verdict": "PASS"}}

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertTrue(result["ok"], result["msg"])
            self.assertEqual(len(calls), 3)  # test twice, done once
            run_id = result["runId"]
            self.assertEqual(workflow_state.load_run(run_id)["status"], "completed")

    def test_declared_outputs_must_be_returned(self):
        """A node that declares out(...) and stays silent fails the run instead
        of counting as PASS and feeding the next node."""
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "(a.hwo)#a# [out(verdict: string, report: string)]\n"
                "(b.hwo)#b#\n"
                "#a# -> { on: PASS } #b#\n",
                encoding="utf-8",
            )
            calls = []

            def fake_run(**kwargs):
                calls.append(kwargs)
                return {"ok": True, "msg": "I have finished the task.", "outputs": {}}

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertFalse(result["ok"])
            self.assertEqual(len(calls), 1, "downstream node must not run")
            self.assertIn("missing declared output(s): report", result["msg"])
            self.assertIn("no verdict", result["msg"])
            self.assertEqual(
                workflow_state.load_run(result["runId"])["status"], "failed")

    def test_partial_outputs_still_violate_the_contract(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "(a.hwo)#a# [out(verdict: string, passRate: string)]\n"
                "(b.hwo)#b#\n"
                "#a# -> { on: PASS } #b#\n",
                encoding="utf-8",
            )
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", return_value={
                    "ok": True, "msg": '{"verdict":"PASS"}', "outputs": {"verdict": "PASS"}}):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertFalse(result["ok"])
            self.assertIn("missing declared output(s): passRate", result["msg"])

    def test_contract_violation_is_retried_before_failing(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "(a.hwo)#a# [out(verdict: string, report: string)] { retry: 1 }\n"
                "(b.hwo)#b#\n"
                "#a# -> { on: PASS } #b#\n",
                encoding="utf-8",
            )
            calls = []

            def fake_run(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return {"ok": True, "msg": "done, I think", "outputs": {}}
                return {"ok": True, "msg": '{"verdict":"PASS","report":"r.md"}',
                        "outputs": {"verdict": "PASS", "report": "r.md"}}

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertTrue(result["ok"], result["msg"])
            self.assertEqual(len(calls), 3)  # a twice (retry), then b
            self.assertEqual(
                workflow_state.load_run(result["runId"])["nodeOutputs"]["a"]["report"],
                "r.md")

    def test_marker_verdict_satisfies_the_contract(self):
        """Verdict may arrive as a #RESULT:# marker rather than JSON."""
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "(a.hwo)#a# [out(verdict: string)]\n"
                "(b.hwo)#b#\n"
                "#a# -> { on: PASS } #b#\n",
                encoding="utf-8",
            )
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", return_value={
                    "ok": True, "msg": "all good #RESULT: PASS#", "outputs": {}}):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertTrue(result["ok"], result["msg"])
            self.assertEqual(
                workflow_state.load_run(result["runId"])["history"], ["a", "b"])

    def test_node_without_declared_outputs_is_unchanged(self):
        """Backwards compatibility: no out(...) means ok still maps to PASS."""
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text("(a.hwo)#a#\n(b.hwo)#b#\n#a# -> #b#\n",
                                        encoding="utf-8")
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", return_value={
                    "ok": True, "msg": "", "outputs": {}}):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})

            self.assertTrue(result["ok"], result["msg"])

    def test_missing_required_graph_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                "@graph [in(workspace: string)]\n"
                "(a.hwo)#a# [in(workspace = $input.workspace)]\n",
                encoding="utf-8",
            )
            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file") as runner:
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})
                missing = hwg_runner.run_hwg_file(
                    "flow.hwg", deps=object(), session={}, inputs={"workspace": ""})

            self.assertFalse(result["ok"])
            self.assertIn("missing required graph input(s): workspace", result["msg"])
            self.assertFalse(missing["ok"], "empty string counts as missing")
            runner.assert_not_called()



if __name__ == "__main__":
    unittest.main()


class ConditionTests(unittest.TestCase):
    """exists(path) and boolean composition on edges — the branch decisions the
    graph can make from facts instead of taking a node's word for them."""

    def _run(self, hwg_source, hwo_results, files=(), inputs=None):
        """Run a graph whose nodes return hwo_results in order; return (result, calls)."""
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(hwg_source, encoding="utf-8")
            for name in files:
                Path(name).parent.mkdir(parents=True, exist_ok=True)
                Path(name).write_text("x", encoding="utf-8")
            calls = []

            def fake_run(path, **kwargs):
                calls.append((path, kwargs.get("inputs") or {}))
                return hwo_results[min(len(calls) - 1, len(hwo_results) - 1)]

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file(
                    "flow.hwg", deps=object(), session={}, inputs=inputs or {})
            return result, calls

    _BRANCH = (
        '(a.hwo)#a# [out(verdict: string)]\n'
        '(built.hwo)#built#\n'
        '(missing.hwo)#missing#\n'
        '#a# -> { on: %s } #built#\n'
        '#a# -> { on: %s } #missing#\n'
    )

    def test_exists_routes_on_a_real_file(self):
        source = self._BRANCH % ("exists(dist/app.js)", "not exists(dist/app.js)")
        ok = {"ok": True, "msg": '[RETURN #a#]: PASS', "outputs": {"verdict": "PASS"}}
        result, calls = self._run(source, [ok], files=["dist/app.js"])
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual([c[0] for c in calls], ["a.hwo", "built.hwo"])

    def test_missing_file_takes_the_not_exists_branch(self):
        source = self._BRANCH % ("exists(dist/app.js)", "not exists(dist/app.js)")
        ok = {"ok": True, "msg": '[RETURN #a#]: PASS', "outputs": {"verdict": "PASS"}}
        result, calls = self._run(source, [ok])
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual([c[0] for c in calls], ["a.hwo", "missing.hwo"])

    def test_verdict_alone_cannot_claim_a_missing_artifact(self):
        """A node saying PASS is not enough when the edge also demands the file."""
        source = self._BRANCH % (
            'verdict == "PASS" and exists(dist/app.js)',
            'verdict != "PASS" or not exists(dist/app.js)')
        ok = {"ok": True, "msg": '[RETURN #a#]: PASS', "outputs": {"verdict": "PASS"}}
        result, calls = self._run(source, [ok])
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual([c[0] for c in calls], ["a.hwo", "missing.hwo"])

    def test_exists_interpolates_a_graph_input(self):
        source = (
            '@graph [in(target: string)]\n'
            '(a.hwo)#a# [out(verdict: string)]\n'
            '(built.hwo)#built#\n'
            '(missing.hwo)#missing#\n'
            '#a# -> { on: exists($input.target) } #built#\n'
            '#a# -> { on: not exists($input.target) } #missing#\n'
        )
        ok = {"ok": True, "msg": '[RETURN #a#]: PASS', "outputs": {"verdict": "PASS"}}
        result, calls = self._run(source, [ok], files=["out/report.md"],
                                  inputs={"target": "out/report.md"})
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual([c[0] for c in calls], ["a.hwo", "built.hwo"])

    def test_unresolved_reference_makes_exists_false(self):
        """An empty $input must not be tested as a bare relative path (which
        would resolve to the workspace root and always exist)."""
        ctx = {"inputs": {}, "nodeOutputs": {}}
        self.assertFalse(hwg_runner._path_exists("$input.nothing", ctx))
        self.assertFalse(hwg_runner._path_exists("#a.artifact#", ctx))

    def test_legacy_conditions_keep_their_meaning(self):
        edge = {"on": 'verdict == "PASS"'}
        self.assertTrue(hwg_runner._edge_matches(edge, "PASS", {"verdict": "PASS"}))
        self.assertFalse(hwg_runner._edge_matches(edge, "FAIL", {"verdict": "FAIL"}))
        self.assertTrue(hwg_runner._edge_matches({"on": "score >= 3"}, "PASS", {"score": 5}))
        self.assertFalse(hwg_runner._edge_matches({"on": "score >= 3"}, "PASS", {"score": 1}))
        self.assertTrue(hwg_runner._edge_matches({"on": "s in [OK, WARN]"}, "PASS", {"s": "warn"}))
        self.assertTrue(hwg_runner._edge_matches({"on": "NEEDS_WORK"}, "NEEDS_WORK", {}))
        # Free text outside the grammar still falls back to a verdict compare.
        self.assertTrue(hwg_runner._edge_matches({"on": "msg == hello world"}, "X",
                                                 {"msg": "hello world"}))

    def test_malformed_condition_is_a_compile_error(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(a.hwo)#a# [out(verdict: string)]\n'
                '(b.hwo)#b#\n'
                '(c.hwo)#c#\n'
                '#a# -> { on: verdict == "PASS" and } #b#\n'
                '#a# -> { on: FAIL } #c#\n',
                encoding="utf-8")
            result = hwg_runner.compile_hwg_file("flow.hwg")
        self.assertFalse(result["ok"], result)
        self.assertIn("is not valid", result["msg"])


class LoopCountTests(unittest.TestCase):
    def test_loop_count_starts_at_one_and_increments(self):
        node = {"id": "a", "io": {"in": [{"name": "attempt", "source": "$loop.count"}]}}
        self.assertEqual(hwg_runner._build_node_inputs(node, {}, {}, {})["attempt"], 1)
        history = {"a": [{"verdict": "FAIL"}, {"verdict": "FAIL"}]}
        self.assertEqual(hwg_runner._build_node_inputs(node, {}, {}, history)["attempt"], 3)
        # Another node's history does not leak into this one.
        self.assertEqual(
            hwg_runner._build_node_inputs(node, {}, {}, {"b": [{}, {}]})["attempt"], 1)

    def test_loop_count_reaches_a_looping_node(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(plan.hwo)#plan#\n'
                '(fix.hwo)#fix# [in(attempt: int = $loop.count), out(verdict: string)]\n'
                '(done.hwo)#done#\n'
                '#plan# -> #fix#\n'
                '#fix# -> { on: FAIL, maxLoops: 2 } #fix#\n'
                '#fix# -> { on: PASS } #done#\n',
                encoding="utf-8")
            seen = []

            def fake_run(path, **kwargs):
                if path != "fix.hwo":
                    return {"ok": True, "msg": "ok", "outputs": {}}
                inputs = kwargs.get("inputs") or {}
                seen.append(inputs.get("attempt"))
                verdict = "PASS" if len(seen) >= 3 else "FAIL"
                return {"ok": True, "msg": f"[RETURN #fix#]: {verdict}",
                        "outputs": {"verdict": verdict}}

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(seen[:3], [1, 2, 3])


class CacheFingerprintTests(unittest.TestCase):
    def test_cache_is_skipped_when_the_workspace_cannot_be_fingerprinted(self):
        node = {"id": "a", "file": "a.hwo", "policy": {"cache": "1h"}}
        with mock.patch.object(hwg_runner, "_workspace_fingerprint", return_value=None), \
                mock.patch.object(hwg_runner.workflow_state, "cache_get") as cache_get, \
                mock.patch.object(hwg_runner.workflow_state, "cache_set") as cache_set, \
                mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file",
                                  return_value={"ok": True, "msg": "done", "outputs": {}}):
            result = hwg_runner._run_hwo_with_policy(
                node, deps=object(), session={}, parent_id=None, inputs={})
        self.assertTrue(result["ok"])
        cache_get.assert_not_called()
        cache_set.assert_not_called()

    def test_workspace_change_invalidates_the_cache_key(self):
        node = {"id": "a", "file": "a.hwo", "policy": {}}
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("a.hwo").write_text("same source", encoding="utf-8")
            before = hwg_runner._cache_key("a.hwo", node, {}, "fingerprint-1")
            after = hwg_runner._cache_key("a.hwo", node, {}, "fingerprint-2")
        self.assertNotEqual(before, after)


class FanOutTests(unittest.TestCase):
    """`=>` runs every branch and a { join: "all" } node merges them."""

    _GRAPH = (
        '(plan.hwo)#plan# [out(verdict: string)]\n'
        '(lint.hwo)#lint# [out(verdict: string, findings: string)]\n'
        '(test.hwo)#test# [out(verdict: string, failures: string)]\n'
        '(types.hwo)#types# [out(verdict: string, errors: string)]\n'
        '(merge.hwo)#merge# [in(f = #lint.findings#, x = #test.failures#, e = #types.errors#)] '
        '{ join: "all" }\n'
        '(ship.hwo)#ship#\n'
        '#plan# => #lint#\n'
        '#plan# => #test#\n'
        '#plan# => #types#\n'
        '#lint# -> #merge#\n'
        '#test# -> #merge#\n'
        '#types# -> #merge#\n'
        '#merge# -> #ship#\n'
    )

    def _run(self, source, responder):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(source, encoding="utf-8")
            calls = []

            def fake_run(path, **kwargs):
                calls.append((path, kwargs.get("inputs") or {}))
                return responder(path)

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})
            return result, calls

    def test_every_branch_runs_before_the_join(self):
        outs = {
            "lint.hwo": {"verdict": "PASS", "findings": "none"},
            "test.hwo": {"verdict": "PASS", "failures": "0"},
            "types.hwo": {"verdict": "PASS", "errors": "0"},
        }

        def responder(path):
            return {"ok": True, "msg": "[RETURN #n#]: PASS",
                    "outputs": outs.get(path, {"verdict": "PASS"})}

        result, calls = self._run(self._GRAPH, responder)
        self.assertTrue(result["ok"], result["msg"])
        order = [c[0] for c in calls]
        self.assertEqual(order[0], "plan.hwo")
        self.assertEqual(sorted(order[1:4]), ["lint.hwo", "test.hwo", "types.hwo"])
        # The join runs exactly once, and only after all three branches.
        self.assertEqual(order[4:], ["merge.hwo", "ship.hwo"])
        self.assertEqual(order.count("merge.hwo"), 1)

    def test_join_sees_every_branch_output(self):
        outs = {
            "lint.hwo": {"verdict": "PASS", "findings": "2 warnings"},
            "test.hwo": {"verdict": "PASS", "failures": "1 failure"},
            "types.hwo": {"verdict": "PASS", "errors": "no errors"},
        }

        def responder(path):
            return {"ok": True, "msg": "[RETURN #n#]: PASS",
                    "outputs": outs.get(path, {"verdict": "PASS"})}

        _result, calls = self._run(self._GRAPH, responder)
        merge_inputs = next(inputs for path, inputs in calls if path == "merge.hwo")
        self.assertEqual(merge_inputs,
                         {"f": "2 warnings", "x": "1 failure", "e": "no errors"})

    def test_a_branch_that_never_reaches_the_join_fails_the_run(self):
        """A join waiting on a branch that walked off elsewhere must not be
        silently skipped — the run has not done what the graph says it does."""
        source = (
            '(plan.hwo)#plan# [out(verdict: string)]\n'
            '(lint.hwo)#lint# [out(verdict: string)]\n'
            '(test.hwo)#test# [out(verdict: string)]\n'
            '(bail.hwo)#bail#\n'
            '(merge.hwo)#merge# { join: "all" }\n'
            '#plan# => #lint#\n'
            '#plan# => #test#\n'
            '#lint# -> { on: verdict == "PASS" } #merge#\n'
            '#lint# -> { on: verdict == "FAIL" } #bail#\n'
            '#test# -> #merge#\n'
        )

        def responder(path):
            verdict = "FAIL" if path == "lint.hwo" else "PASS"
            return {"ok": True, "msg": f"[RETURN #n#]: {verdict}",
                    "outputs": {"verdict": verdict}}

        result, calls = self._run(source, responder)
        self.assertFalse(result["ok"], result["msg"])
        self.assertIn("#merge#", result["msg"])
        self.assertIn("never arrived", result["msg"])
        # merge must not have run on a partial set of branches.
        self.assertNotIn("merge.hwo", [c[0] for c in calls])

    def test_fanout_shape_errors_are_caught_at_compile_time(self):
        cases = {
            'mixes -> and =>': (
                '(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n(m.hwo)#m# { join: "all" }\n'
                '#a# => #b#\n#a# -> #c#\n#b# -> #m#\n#c# -> #m#\n'),
            'do not converge on one join node': (
                '(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n#a# => #b#\n#a# => #c#\n'),
            'no fan-out (=>) converges on it': (
                '(a.hwo)#a#\n(m.hwo)#m# { join: "all" }\n#a# -> #m#\n'),
        }
        for expected, source in cases.items():
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
                    Path("flow.hwg").write_text(source, encoding="utf-8")
                    result = hwg_runner.compile_hwg_file("flow.hwg")
                self.assertFalse(result["ok"], result)
                self.assertIn(expected, result["msg"])


class IncludeTests(unittest.TestCase):
    """@include splices shared declarations so several graphs can agree on one
    contract instead of restating it."""

    _LIB = '(review.hwo)#review# [in(report: file), out(verdict: string)] { retry: 1 }\n'

    def _compile(self, files, entry="main.hwg"):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            for name, text in files.items():
                Path(name).parent.mkdir(parents=True, exist_ok=True)
                Path(name).write_text(text, encoding="utf-8")
            return hwg_runner.compile_hwg_file(entry)

    def test_included_declarations_join_the_graph(self):
        result = self._compile({
            "lib/contracts.hwg": self._LIB,
            "main.hwg": (
                '@include "lib/contracts.hwg"\n'
                '(write.hwo)#write# [out(report: file)]\n'
                '(publish.hwo)#publish#\n'
                '#write# -> #review#\n'
                '#review# -> { on: verdict == "PASS" } #publish#\n'
                '#review# -> { on: verdict == "FAIL" } #publish#\n'
            ),
        })
        self.assertTrue(result["ok"], result["msg"])
        # The included node keeps its policy and satisfies the edge conditions
        # that reference its declared output.
        self.assertIn("#review# policy={'retry': 1}", result["msg"])

    def test_include_path_is_relative_to_the_including_file(self):
        result = self._compile({
            "shared/contracts.hwg": self._LIB,
            "graphs/lib.hwg": '@include "../shared/contracts.hwg"\n',
            "main.hwg": (
                '@include "graphs/lib.hwg"\n'
                '(write.hwo)#write# [out(report: file)]\n'
                '#write# -> #review#\n'
            ),
        })
        self.assertTrue(result["ok"], result["msg"])

    def test_missing_include_names_the_file_and_its_includer(self):
        result = self._compile({"main.hwg": '@include "nope.hwg"\n(a.hwo)#a#\n'})
        self.assertFalse(result["ok"])
        self.assertIn('@include "nope.hwg" not found', result["msg"])
        self.assertIn('main.hwg', result["msg"])

    def test_include_cycle_is_refused_not_followed(self):
        result = self._compile({
            "main.hwg": '@include "a.hwg"\n(m.hwo)#m#\n',
            "a.hwg": '@include "main.hwg"\n(a.hwo)#a#\n',
        })
        self.assertFalse(result["ok"])
        self.assertIn("@include cycle: main.hwg -> a.hwg -> main.hwg", result["msg"])

    def test_diamond_include_is_not_a_duplicate_id(self):
        """Two libraries including the same contracts file must splice it once."""
        result = self._compile({
            "common.hwg": '(review.hwo)#review# [out(verdict: string)]\n',
            "a.hwg": '@include "common.hwg"\n(a.hwo)#a#\n',
            "b.hwg": '@include "common.hwg"\n(b.hwo)#b#\n',
            "main.hwg": (
                '@include "a.hwg"\n@include "b.hwg"\n'
                '(start.hwo)#start#\n'
                '#start# -> #a#\n#a# -> #b#\n#b# -> #review#\n'
            ),
        })
        self.assertTrue(result["ok"], result["msg"])
        # Declared once, not once per branch that included it.
        self.assertEqual(result["msg"].count("(review.hwo)#review#"), 1)

    def test_a_broken_included_file_is_reported_against_its_own_path(self):
        result = self._compile({
            "main.hwg": '@include "lib/bad.hwg"\n(a.hwo)#a#\n',
            "lib/bad.hwg": '@@@\n',
        })
        self.assertFalse(result["ok"])
        self.assertIn('@include "lib/bad.hwg" failed to parse', result["msg"])

    def test_validate_refuses_an_unresolved_include(self):
        """The guard that stops a product from validating a graph with a hole."""
        errors = validate_hwg(parse_hwg('@include "x.hwg"\n(a.hwo)#a#\n'))
        self.assertIn('@include "x.hwg" was not resolved', errors[0])


class ToolScopeTests(unittest.TestCase):
    """A node's `{ tools: [...] }` reaches the .hwo run that executes it."""

    def test_node_tool_scope_is_passed_to_the_hwo_run(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(
                '(audit.hwo)#audit# [out(verdict: string)] '
                '{ tools: [fs.read, "fs.gr*"] }\n'
                '(apply.hwo)#apply#\n'
                '#audit# -> #apply#\n',
                encoding="utf-8")
            seen = {}

            def fake_run(path, **kwargs):
                seen[path] = kwargs.get("tool_scope")
                return {"ok": True, "msg": "[RETURN #n#]: PASS",
                        "outputs": {"verdict": "PASS"}}

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                result = hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={})
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(seen["audit.hwo"], ["fs.read", "fs.gr*"])
        # A node that declares no scope stays unrestricted.
        self.assertIsNone(seen["apply.hwo"])

    def test_tool_scope_shape_is_checked_at_compile_time(self):
        cases = {
            "tools: [] would leave the node with nothing to call":
                '(a.hwo)#a# { tools: [] }\n(b.hwo)#b#\n#a# -> #b#\n',
            'is not a tool name or glob':
                '(a.hwo)#a# { tools: [not a name] }\n(b.hwo)#b#\n#a# -> #b#\n',
        }
        for expected, source in cases.items():
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
                    Path("flow.hwg").write_text(source, encoding="utf-8")
                    result = hwg_runner.compile_hwg_file("flow.hwg")
                self.assertFalse(result["ok"], result)
                self.assertIn(expected, result["msg"])


class UnhandledFailureTests(unittest.TestCase):
    """A node that failed with no edge saying what a failure means there must
    not end up in a run that reports success."""

    def _run(self, source, responder):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            Path("flow.hwg").write_text(source, encoding="utf-8")
            calls = []

            def fake_run(path, **kwargs):
                calls.append(path)
                return responder(path)

            with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file", side_effect=fake_run):
                return hwg_runner.run_hwg_file("flow.hwg", deps=object(), session={}), calls

    def test_failure_down_an_unconditional_edge_fails_the_run(self):
        source = ('(a.hwo)#a# [out(verdict: string)]\n'
                  '(b.hwo)#b#\n'
                  '#a# -> #b#\n')

        def responder(path):
            if path == "a.hwo":
                return {"ok": False, "msg": "boom", "outputs": {}}
            return {"ok": True, "msg": "[RETURN #b#]: PASS", "outputs": {}}

        result, calls = self._run(source, responder)
        self.assertFalse(result["ok"], result["msg"])
        self.assertIn("#a#", result["msg"])
        self.assertIn("no edge said what a failure there means", result["msg"])
        # The graph still walked on — the author's edge is honoured, the verdict
        # about the run is not.
        self.assertEqual(calls, ["a.hwo", "b.hwo"])

    def test_a_failure_the_author_routed_on_is_handled(self):
        source = ('(a.hwo)#a# [out(verdict: string)]\n'
                  '(ok.hwo)#ok#\n'
                  '(recover.hwo)#recover#\n'
                  '#a# -> { on: verdict == "PASS" } #ok#\n'
                  '#a# -> { on: verdict == "FAIL" } #recover#\n')

        def responder(path):
            if path == "a.hwo":
                return {"ok": False, "msg": "[RETURN #a#]: FAIL",
                        "outputs": {"verdict": "FAIL"}}
            return {"ok": True, "msg": "[RETURN #n#]: PASS", "outputs": {}}

        result, calls = self._run(source, responder)
        self.assertTrue(result["ok"], result["msg"])
        self.assertEqual(calls, ["a.hwo", "recover.hwo"])

    def test_a_failing_end_node_fails_the_run(self):
        source = '(a.hwo)#a#\n(b.hwo)#b#\n#a# -> #b#\n'

        def responder(path):
            return {"ok": path != "b.hwo", "msg": "x", "outputs": {}}

        result, _calls = self._run(source, responder)
        self.assertFalse(result["ok"], result["msg"])
        self.assertIn("#b#", result["msg"])
