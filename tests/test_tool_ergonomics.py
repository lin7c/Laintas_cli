"""Tool-call ergonomics: coercion, self-correcting errors, the soft test gate.

All three were derived from the event log (~/.laintas/events.jsonl and the
per-project copies), where they were the top causes of failed tool calls:

  * 99 failures were a model sending "100" where the schema said integer
    (fs.read limit/offset, shell.exec timeout, fs.grep max_results).
  * 14 were task.update against an id from a previous session, answered with a
    bare "Task 's3' not found" that gave the model nothing to recover with.
  * 23 were task.complete's test gate firing at agents that HAD run tests,
    because the detector matched substrings and missed `python3 -m unittest`
    and `npm run test`.
"""
import os
import tempfile
import shutil
import types
import unittest

import agent_loop
import tools


SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
        "ratio": {"type": "number"},
        "recursive": {"type": "boolean"},
        "names": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array",
                  "items": {"type": "object",
                            "properties": {"n": {"type": "integer"}}}},
        "meta": {"type": "object",
                 "properties": {"count": {"type": "integer"}}},
        "either": {"type": ["string", "integer"]},
    },
}


class CoercionTests(unittest.TestCase):
    def _coerce(self, params):
        return tools._coerce_params(params, SCHEMA)

    def test_numeric_strings_become_numbers(self):
        self.assertEqual(self._coerce({"limit": "100"}), {"limit": 100})
        self.assertEqual(self._coerce({"limit": " 42 "}), {"limit": 42})
        self.assertEqual(self._coerce({"ratio": "1.5"}), {"ratio": 1.5})
        self.assertEqual(self._coerce({"ratio": "2"}), {"ratio": 2.0})

    def test_boolean_strings_become_booleans(self):
        for text, expected in (("true", True), ("True", True), ("yes", True),
                               ("on", True), ("1", True), ("false", False),
                               ("no", False), ("off", False), ("0", False)):
            with self.subTest(text=text):
                self.assertIs(self._coerce({"recursive": text})["recursive"],
                              expected)

    def test_scalar_becomes_single_item_array(self):
        self.assertEqual(self._coerce({"names": "solo"}), {"names": ["solo"]})

    def test_recurses_into_objects_and_arrays(self):
        self.assertEqual(self._coerce({"steps": [{"n": "3"}]}),
                         {"steps": [{"n": 3}]})
        self.assertEqual(self._coerce({"meta": {"count": "7"}}),
                         {"meta": {"count": 7}})

    def test_leaves_ambiguous_and_invalid_values_alone(self):
        """Anything lossy stays put so validation reports the real problem."""
        for params in ({"limit": "abc"},      # not a number at all
                       {"limit": "1.5"},      # would lose precision
                       {"limit": "1e3"},      # not a plain int literal
                       {"path": "123"},       # already the declared type
                       {"either": "7"},       # string is valid for this union
                       {"limit": None},
                       {"unknown": "x"}):     # not in the schema
            with self.subTest(params=params):
                self.assertEqual(self._coerce(dict(params)), params)

    def test_does_not_mutate_the_caller_dict(self):
        original = {"limit": "5"}
        tools._coerce_params(original, SCHEMA)
        self.assertEqual(original, {"limit": "5"})

    def test_real_tools_accept_stringified_numbers(self):
        import os
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "sample.txt")
            with open(path, "w") as fh:
                fh.write("\n".join(f"line {i}" for i in range(1, 60)))
            ctx = tools.ToolCtx(cwd=tmp)
            reg = tools.get_registry()
            for name, params in (
                ("fs.read", {"path": path, "limit": "5"}),
                ("fs.read", {"path": path, "offset": "10", "limit": "3"}),
                ("fs.grep", {"pattern": "line 1", "path": tmp,
                             "max_results": "2"}),
                ("shell.exec", {"command": "echo hi", "timeout": "10"}),
            ):
                with self.subTest(name=name, params=params):
                    self.assertTrue(reg.invoke(name, dict(params), ctx)["ok"])
            # A value that really is wrong still fails, truthfully.
            bad = reg.invoke("fs.read", {"path": path, "limit": "abc"}, ctx)
            self.assertFalse(bad["ok"])
            self.assertIn("expected integer", bad["error"])
        finally:
            shutil.rmtree(tmp)


class TestCommandDetectionTests(unittest.TestCase):
    RUNS_TESTS = (
        # every one of these was MISSED by the old substring list
        "python3 -m unittest discover -s tests",
        "python3 -m unittest tests.test_git_policy",
        "npm run test",
        "npm run test:unit",
        "pnpm run test -- --watch=false",
        "bun test",
        "deno test -A",
        "./run_tests.sh",
        "ctest --output-on-failure",
        "mix test",
        "swift test",
        "gradlew test",
        "bazel test //...",
        # and these already worked
        "pytest -q", "python -m pytest", "npm test", "cargo test",
        "go test ./...", "mvn test", "make test", "dotnet test",
        "vitest run", "source venv/bin/activate && python3 -m pytest tests/",
    )
    NOT_TESTS = ("ls tests/", "cat test_foo.py", "git commit -m 'add tests'",
                 "npm install", "go build ./...", "rm -rf test", "")

    def test_recognises_real_test_runs(self):
        for command in self.RUNS_TESTS:
            with self.subTest(command=command):
                self.assertTrue(tools._looks_like_test_command(command))

    def test_does_not_mistake_other_commands_for_a_test_run(self):
        for command in self.NOT_TESTS:
            with self.subTest(command=command):
                self.assertFalse(tools._looks_like_test_command(command))


class AdvisoryFormattingTests(unittest.TestCase):
    def test_advisory_is_not_labelled_a_tool_error(self):
        rendered = agent_loop._format_tool_result_for_loop(
            "task.complete",
            {"ok": False, "error": "run the suite first", "_advisory": True},
            max_chars=500)
        self.assertIn("[action needed]", rendered)
        self.assertNotIn("[tool error]", rendered)

    def test_ordinary_failures_are_still_tool_errors(self):
        rendered = agent_loop._format_tool_result_for_loop(
            "fs.read", {"ok": False, "error": "no such file"}, max_chars=500)
        self.assertIn("[tool error]", rendered)


class TaskErrorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = tools.get_registry()
        self.ctx = tools.ToolCtx(cwd=self.tmp, session_id="sess-A",
                                 agent_id="primary")
        self.reg.invoke("task.create", {"subject": "ship v1.13.0"}, self.ctx)
        self.reg.invoke("task.create", {"subject": "fix rprompt wrap"}, self.ctx)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_id_error_names_the_live_ids(self):
        """The logged failure: an id carried over from a previous session."""
        error = self.reg.invoke(
            "task.update", {"id": "s3", "status": "in_progress"},
            self.ctx)["error"]
        self.assertIn("not found", error)
        self.assertIn("s1", error)
        self.assertIn("s2", error)
        self.assertIn("ship v1.13.0", error)

    def test_missing_id_error_names_the_live_ids(self):
        """The follow-up failure: the model dropped `id` entirely."""
        error = self.reg.invoke(
            "task.update", {"progress": 100, "status": "completed"},
            self.ctx)["error"]
        self.assertIn("missing 'id'", error)
        self.assertIn("s1", error)

    def test_task_get_reports_the_same_way(self):
        error = self.reg.invoke("task.get", {"id": "s9"}, self.ctx)["error"]
        self.assertIn("s1", error)
        self.assertIn("s2", error)

    def test_new_session_explains_that_ids_are_session_scoped(self):
        other = tools.ToolCtx(cwd=self.tmp, session_id="sess-B",
                              agent_id="primary")
        error = self.reg.invoke(
            "task.update", {"id": "s1", "status": "completed"}, other)["error"]
        self.assertIn("session-scoped", error)
        self.assertIn("task_create", error)

    def test_valid_update_still_works_and_coerces(self):
        result = self.reg.invoke(
            "task.update",
            {"id": "s1", "status": "in_progress", "progress": "25"}, self.ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["progress"], 25)


class EditAnchorTests(unittest.TestCase):
    """multi_edit used to reject anchors fs.edit accepted.

    Three failure modes, all of them "the anchor does not match the bytes on
    disk" rather than anything wrong with the applier:
      * whitespace/indentation drift -- fs.edit fell back to the vendored
        opencode replacers, multi_edit did not, so the same edit landed through
        one tool and failed through the other;
      * anchors carrying fs.read's `N->` display prefixes, which every prompt
        invited by saying to copy verbatim from what was read;
      * one bad anchor discarding a whole batch with an error
        ("old_string not found") that named nothing to correct, so the model
        re-guessed all of them and the batch never converged.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = ("def handler(req):\n"
                    "    if req.ok:\n"
                    "        return process(req)\n"
                    "    return None\n")
        self.path = os.path.join(self.tmp, "a.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(self.src)
        # Writes are policy-gated; grant approval so the test exercises matching.
        self.ctx = tools.ToolCtx(
            cwd=self.tmp,
            deps=types.SimpleNamespace(
                request_file_write_approval=lambda *a, **k: True))

    def _multi_edit(self, edits):
        return tools.get_registry().invoke(
            "fs.multi_edit", {"path": "a.py", "edits": edits}, self.ctx)

    def test_indentation_drift_lands_like_it_does_through_fs_edit(self):
        result = self._multi_edit([
            {"old_string": "def handler(req):",
             "new_string": "def handler(req, cfg):"},
            # Second line under-indented by four spaces -- the classic drift.
            {"old_string": "    if req.ok:\n    return process(req)",
             "new_string": "    if req.ok and req.body:\n        return process(req)"},
        ])
        self.assertTrue(result.get("ok"), result.get("error"))
        self.assertEqual([e["match"] for e in result["edits_applied"]],
                         ["exact", "line-trimmed"])
        body = open(self.path, encoding="utf-8").read()
        self.assertIn("def handler(req, cfg):", body)
        self.assertIn("    if req.ok and req.body:\n        return process(req)", body)

    def test_anchors_copied_out_of_fs_read_keep_their_prefixes_and_still_match(self):
        read = tools.get_registry().invoke(
            "fs.read", {"path": "a.py"}, self.ctx)["result"]
        # Exactly what the model sees, prefixes and all.
        first, last = read.split("\n")[0], read.split("\n")[3]
        self.assertTrue(first.startswith("1\u2192"))
        result = self._multi_edit([
            {"old_string": first, "new_string": "def handler(req, cfg):"},
            {"old_string": last, "new_string": "    return req.fallback"},
        ])
        self.assertTrue(result.get("ok"), result.get("error"))
        body = open(self.path, encoding="utf-8").read()
        self.assertNotIn("\u2192", body)
        self.assertIn("def handler(req, cfg):", body)
        self.assertIn("    return req.fallback", body)

    def test_one_bad_anchor_reports_which_edit_failed_and_which_matched(self):
        result = self._multi_edit([
            {"old_string": "def handler(req):",
             "new_string": "def handler(req, cfg):"},
            {"old_string": "    raise NotImplementedError()",
             "new_string": "    pass"},
            {"old_string": "    return None", "new_string": "    return req.fallback"},
        ])
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("failed_edit"), 2)
        self.assertEqual(result.get("matching_edits"), [1, 3])
        self.assertIn("only edit #2", result["error"])
        # All-or-nothing still holds.
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.src)

    def test_a_near_miss_names_the_line_it_is_closest_to(self):
        result = self._multi_edit([
            {"old_string": "def handler(req, ctx):",
             "new_string": "def handler(req, cfg):"},
        ])
        self.assertFalse(result.get("ok"))
        self.assertIn("line 1", result["error"])
        self.assertIn("def handler(req):", result["error"])

    def test_prefix_stripping_only_fires_on_a_fully_numbered_anchor(self):
        # Real code that merely contains the arrow must not be mangled.
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("label = \"1\u2192one\"\nother = 2\n")
        result = self._multi_edit([
            {"old_string": "label = \"1\u2192one\"",
             "new_string": "label = \"1\u2192uno\""},
        ])
        self.assertTrue(result.get("ok"), result.get("error"))
        self.assertIn("1\u2192uno", open(self.path, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
