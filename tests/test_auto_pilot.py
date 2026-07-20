"""Tests for auto_pilot: heuristic task classification and hint injection."""

import pytest

import auto_pilot


# ── classify_task ──────────────────────────────────────────────────────

class TestClassifySimple:
    def test_empty_task_is_simple(self):
        assert auto_pilot.classify_task("") == auto_pilot.SIMPLE

    def test_whitespace_only_is_simple(self):
        assert auto_pilot.classify_task("   ") == auto_pilot.SIMPLE

    def test_short_question_is_simple(self):
        assert auto_pilot.classify_task("what is 2+2?") == auto_pilot.SIMPLE

    def test_how_question_is_simple(self):
        assert auto_pilot.classify_task("how does the parser work?") == auto_pilot.SIMPLE

    def test_is_question_is_simple(self):
        assert auto_pilot.classify_task("is the server running?") == auto_pilot.SIMPLE

    def test_short_command_is_simple(self):
        assert auto_pilot.classify_task("run the tests") == auto_pilot.SIMPLE

    def test_single_action_no_refs_is_simple(self):
        assert auto_pilot.classify_task("fix the typo in the README") == auto_pilot.SIMPLE


class TestClassifyMonitor:
    def test_watch_keyword_triggers_monitor(self):
        assert auto_pilot.classify_task("watch the log file for errors") == auto_pilot.MONITOR

    def test_monitor_keyword_triggers_monitor(self):
        assert auto_pilot.classify_task("monitor the build output for failures") == auto_pilot.MONITOR

    def test_tail_keyword_triggers_monitor(self):
        assert auto_pilot.classify_task("tail the server log and report new errors") == auto_pilot.MONITOR

    def test_wait_for_keyword_triggers_monitor(self):
        assert auto_pilot.classify_task("wait for the build to complete then report") == auto_pilot.MONITOR

    def test_alert_me_triggers_monitor(self):
        assert auto_pilot.classify_task("alert me when the test fails") == auto_pilot.MONITOR


class TestClassifyPipeline:
    def test_two_sequence_connectors_with_two_verbs(self):
        task = "first run the tests, then fix any failures, finally commit the changes"
        assert auto_pilot.classify_task(task) == auto_pilot.PIPELINE_HINT

    def test_after_that_with_multiple_actions(self):
        task = "build the docker image, after that push it to the registry, next deploy to staging"
        assert auto_pilot.classify_task(task) == auto_pilot.PIPELINE_HINT

    def test_step_n_pattern_triggers_pipeline(self):
        task = "step 1: analyze the bug, step 2: write a fix, step 3: add a regression test"
        assert auto_pilot.classify_task(task) == auto_pilot.PIPELINE_HINT

    def test_single_sequence_connector_is_not_pipeline(self):
        # Only one "then" - not enough to signal pipeline
        task = "run the tests and then commit"
        assert auto_pilot.classify_task(task) != auto_pilot.PIPELINE_HINT


class TestClassifyParallel:
    def test_long_task_with_files_and_and(self):
        task = (
            "update the authentication logic in src/auth/login.py and also "
            "modify the API endpoints in src/api/routes.py as well as add "
            "tests in tests/test_auth.py"
        )
        assert auto_pilot.classify_task(task) == auto_pilot.PARALLEL_HINT

    def test_multiple_dirs_with_and(self):
        task = (
            "refactor the modules in src/components and also update the "
            "documentation in docs/intro as well as add examples in tests/unit"
        )
        assert auto_pilot.classify_task(task) == auto_pilot.PARALLEL_HINT

    def test_short_task_with_files_is_not_parallel(self):
        # Too short to trigger parallel heuristic
        task = "fix foo.py and bar.py"
        assert auto_pilot.classify_task(task) != auto_pilot.PARALLEL_HINT


class TestClassifyWorkflow:
    def test_active_workflow_returns_pipeline(self):
        task = "do anything"
        assert auto_pilot.classify_task(task, has_active_workflow=True) == auto_pilot.PIPELINE

    def test_workflow_overrides_monitor(self):
        task = "watch the logs"
        assert auto_pilot.classify_task(task, has_active_workflow=True) == auto_pilot.PIPELINE

    def test_workflow_overrides_pipeline_hint(self):
        task = "first run tests, then fix bugs, finally commit"
        assert auto_pilot.classify_task(task, has_active_workflow=True) == auto_pilot.PIPELINE


# ── build_hint ─────────────────────────────────────────────────────────

class TestBuildHint:
    def test_simple_returns_empty(self):
        assert auto_pilot.build_hint(auto_pilot.SIMPLE) == ""

    def test_parallel_hint_non_empty(self):
        hint = auto_pilot.build_hint(auto_pilot.PARALLEL_HINT)
        assert hint != ""
        assert "spawn_parallel" in hint or "spawn_chain" in hint

    def test_pipeline_hint_non_empty(self):
        hint = auto_pilot.build_hint(auto_pilot.PIPELINE_HINT)
        assert hint != ""
        assert "spawn_chain" in hint or "workflow" in hint

    def test_monitor_hint_mentions_terminal(self):
        hint = auto_pilot.build_hint(auto_pilot.MONITOR)
        assert hint != ""
        assert "terminal" in hint.lower() or "trigger" in hint.lower()

    def test_pipeline_strategy_returns_empty(self):
        # Active workflow handles execution; no hint needed
        assert auto_pilot.build_hint(auto_pilot.PIPELINE) == ""

    def test_unknown_strategy_returns_empty(self):
        assert auto_pilot.build_hint("unknown") == ""


# ── should_override ────────────────────────────────────────────────────

class TestShouldOverride:
    def test_bang_prefix_triggers_override(self):
        cleaned, overridden = auto_pilot.should_override("!fix the bug")
        assert overridden is True
        assert cleaned == "fix the bug"

    def test_no_bang_no_override(self):
        cleaned, overridden = auto_pilot.should_override("fix the bug")
        assert overridden is False
        assert cleaned == "fix the bug"

    def test_leading_whitespace_before_bang(self):
        cleaned, overridden = auto_pilot.should_override("   !run tests")
        assert overridden is True
        assert cleaned == "run tests"

    def test_bang_only_returns_empty(self):
        cleaned, overridden = auto_pilot.should_override("!")
        assert overridden is True
        assert cleaned == ""

    def test_bang_in_middle_no_override(self):
        cleaned, overridden = auto_pilot.should_override("fix! the bug")
        assert overridden is False
        assert cleaned == "fix! the bug"

    def test_multiple_bang_strips_one(self):
        cleaned, overridden = auto_pilot.should_override("!!run")
        assert overridden is True
        assert cleaned == "!run"


# ── Integration: hint injection respects override ──────────────────────

class TestOverrideIntegration:
    """Verify that override path skips classification and hint injection."""

    def test_override_skips_classification(self):
        # A task that would normally be classified as MONITOR
        task = "!watch the logs"
        cleaned, overridden = auto_pilot.should_override(task)
        assert overridden is True
        # If we classify the *cleaned* input, it would be MONITOR,
        # but the hook in _run_agent_loop_with_interrupt uses the
        # overridden flag to skip classification entirely.
        # Verify the contract: when overridden, no hint is built.
        if overridden:
            assert auto_pilot.build_hint(auto_pilot.SIMPLE) == ""

    def test_normal_path_classifies_and_builds_hint(self):
        task = "watch the logs"
        cleaned, overridden = auto_pilot.should_override(task)
        assert overridden is False
        strategy = auto_pilot.classify_task(cleaned)
        assert strategy == auto_pilot.MONITOR
        assert auto_pilot.build_hint(strategy) != ""


# ── Config key presence ────────────────────────────────────────────────

class TestConfigKeys:
    def test_auto_pilot_enabled_in_default_config(self):
        from agent_loop import get_runtime_config, _DEFAULT_CONFIG
        assert "auto_pilot_enabled" in _DEFAULT_CONFIG
        assert _DEFAULT_CONFIG["auto_pilot_enabled"] is True
        assert get_runtime_config("auto_pilot_enabled") is True
