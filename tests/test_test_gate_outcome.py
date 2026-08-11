"""The soft test gate must judge the OUTCOME, not the ceremony.

Before this, `_check_tests_before_complete` was satisfied the moment a
test-shaped command appeared in terminalHistory. An agent could run the suite,
watch twelve tests fail, and call task_complete unchallenged — the gate checked
that a ritual was performed, not that the code worked.
"""
import unittest
from types import SimpleNamespace

import tools


def _ctx(rows, state_extra=None):
    state = {"terminalHistory": rows}
    if state_extra:
        state.update(state_extra)
    return SimpleNamespace(state=state)


EDIT = {"tool": "fs.edit", "command": "tools.py@40", "output": "", "returncode": 0}


class TestRunOutcomeTests(unittest.TestCase):
    def test_exit_code_decides_when_present(self):
        self.assertEqual(tools._test_run_outcome({"returncode": 0}), "pass")
        self.assertEqual(tools._test_run_outcome({"returncode": 1}), "fail")
        self.assertEqual(tools._test_run_outcome({"returncode": -1}), "fail")

    def test_falls_back_to_runner_output_when_exit_code_is_unknown(self):
        """terminal.exec leaves returncode None while the terminal is open."""
        for output in ("FAILED tests/test_x.py::test_y",
                       "Ran 10 tests\n\nFAILED (failures=2)",
                       "FAILED (errors=1)",
                       "3 failed, 20 passed",
                       "test result: FAILED. 1 passed; 2 failed",
                       "--- FAIL: TestThing (0.00s)"):
            with self.subTest(output=output):
                self.assertEqual(
                    tools._test_run_outcome({"returncode": None, "output": output}),
                    "fail")

    def test_clean_output_with_unknown_exit_code_is_a_pass(self):
        for output in ("Ran 664 tests in 69s\n\nOK",
                       "763 passed, 6 warnings in 78s",
                       ""):
            with self.subTest(output=output):
                self.assertEqual(
                    tools._test_run_outcome({"returncode": None, "output": output}),
                    "pass")


class GateTests(unittest.TestCase):
    def test_no_code_change_means_no_advisory(self):
        rows = [{"tool": "fs.read", "command": "README.md", "returncode": 0}]
        self.assertIsNone(tools._check_tests_before_complete(_ctx(rows)))

    def test_code_changed_and_no_test_run_still_warns(self):
        message = tools._check_tests_before_complete(_ctx([EDIT]))
        self.assertIsNotNone(message)
        self.assertIn("no test run was detected", message)

    def test_code_changed_and_tests_passed_is_silent(self):
        rows = [EDIT, {"tool": "shell.exec", "command": "python3 -m pytest tests/",
                       "output": "664 passed", "returncode": 0}]
        self.assertIsNone(tools._check_tests_before_complete(_ctx(rows)))

    def test_code_changed_and_tests_FAILED_is_reported(self):
        """The regression this exists for."""
        rows = [EDIT, {"tool": "shell.exec", "command": "python3 -m pytest tests/",
                       "output": "2 failed, 662 passed", "returncode": 1}]
        message = tools._check_tests_before_complete(_ctx(rows))
        self.assertIsNotNone(message)
        self.assertIn("reported failures", message)
        self.assertIn("pytest", message)
        self.assertNotIn("no test run was detected", message)

    def test_last_run_wins_so_a_fixed_failure_is_not_held_against_you(self):
        rows = [EDIT,
                {"tool": "shell.exec", "command": "python3 -m pytest tests/",
                 "output": "5 failed", "returncode": 1},
                {"tool": "fs.edit", "command": "tools.py@41", "returncode": 0},
                {"tool": "shell.exec", "command": "python3 -m pytest tests/",
                 "output": "664 passed", "returncode": 0}]
        self.assertIsNone(tools._check_tests_before_complete(_ctx(rows)))

    def test_a_later_failure_is_not_masked_by_an_earlier_pass(self):
        rows = [EDIT,
                {"tool": "shell.exec", "command": "python3 -m pytest tests/",
                 "output": "664 passed", "returncode": 0},
                {"tool": "fs.edit", "command": "tools.py@42", "returncode": 0},
                {"tool": "shell.exec", "command": "python3 -m pytest tests/",
                 "output": "1 failed", "returncode": 1}]
        message = tools._check_tests_before_complete(_ctx(rows))
        self.assertIsNotNone(message)
        self.assertIn("reported failures", message)

    def test_advisory_is_one_shot_in_both_branches(self):
        for rows in ([EDIT],
                     [EDIT, {"tool": "shell.exec", "command": "pytest",
                             "output": "1 failed", "returncode": 1}]):
            with self.subTest(rows=len(rows)):
                ctx = _ctx(list(rows))
                self.assertIsNotNone(tools._check_tests_before_complete(ctx))
                # Second call proceeds — the agent can always override.
                self.assertIsNone(tools._check_tests_before_complete(ctx))

    def test_tests_run_in_a_sub_terminal_count(self):
        rows = [EDIT, {"tool": "terminal.exec", "command": "python3 -m pytest tests/",
                       "output": "664 passed", "returncode": None}]
        self.assertIsNone(tools._check_tests_before_complete(rows and _ctx(rows)))


class GateResultShapeTests(unittest.TestCase):
    def test_task_complete_marks_the_failure_advisory(self):
        rows = [EDIT, {"tool": "shell.exec", "command": "pytest",
                       "output": "1 failed", "returncode": 1}]
        ctx = tools.ToolCtx(cwd=".")
        ctx.state = {"terminalHistory": rows}
        result = tools._bi_task_complete({"summary": "done"}, ctx)
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("_advisory"))
        self.assertIn("reported failures", result["error"])


if __name__ == "__main__":
    unittest.main()
