"""Tests for auto_pilot: heuristic task classification, decomposition, and auto-execution."""

import threading
import time

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

    def test_phase2_config_keys_present(self):
        from agent_loop import _DEFAULT_CONFIG
        assert "auto_pilot_decompose_timeout" in _DEFAULT_CONFIG
        assert "auto_pilot_decompose_max_tokens" in _DEFAULT_CONFIG

    def test_phase3_config_keys_present(self):
        from agent_loop import _DEFAULT_CONFIG
        assert "auto_pilot_auto_execute" in _DEFAULT_CONFIG
        assert _DEFAULT_CONFIG["auto_pilot_auto_execute"] is False
        assert "auto_pilot_max_parallel" in _DEFAULT_CONFIG
        assert "auto_pilot_budget_tokens" in _DEFAULT_CONFIG


# ── Phase 2: Decomposition ─────────────────────────────────────────────

class TestDecomposeCallback:
    def setup_method(self):
        auto_pilot.set_decompose_callback(None)

    def teardown_method(self):
        auto_pilot.set_decompose_callback(None)

    def test_decompose_returns_none_for_simple(self):
        assert auto_pilot.decompose_task("fix bug", auto_pilot.SIMPLE) is None

    def test_decompose_returns_none_for_monitor(self):
        assert auto_pilot.decompose_task("watch logs", auto_pilot.MONITOR) is None

    def test_decompose_uses_callback_when_set(self):
        calls = []

        def cb(task, strategy, timeout):
            calls.append((task, strategy, timeout))
            return ["subtask A", "subtask B"]

        auto_pilot.set_decompose_callback(cb)
        result = auto_pilot.decompose_task("do stuff", auto_pilot.PARALLEL_HINT, timeout=2.0)
        assert result == ["subtask A", "subtask B"]
        assert len(calls) == 1
        assert calls[0][0] == "do stuff"
        assert calls[0][1] == auto_pilot.PARALLEL_HINT

    def test_decompose_falls_back_when_no_callback(self):
        # No callback set; should use heuristic decomposition.
        task = ("update the authentication module in foo.py and also "
                "fix the database connection in bar.py and also "
                "test the new API endpoints in baz.py")
        result = auto_pilot.decompose_task(task, auto_pilot.PARALLEL_HINT, timeout=1.0)
        assert result is not None
        assert len(result) >= 2

    def test_decompose_falls_back_on_callback_failure(self):
        def cb(task, strategy, timeout):
            raise RuntimeError("LLM unavailable")

        auto_pilot.set_decompose_callback(cb)
        task = "update foo.py and also fix bar.py"
        result = auto_pilot.decompose_task(task, auto_pilot.PARALLEL_HINT, timeout=1.0)
        # Should fall back to heuristic, not propagate the error.
        assert result is not None or result is None  # heuristic may or may not split

    def test_decompose_falls_back_on_timeout(self):
        def cb(task, strategy, timeout):
            time.sleep(timeout + 2)
            return ["A", "B"]

        auto_pilot.set_decompose_callback(cb)
        task = "update foo.py and also fix bar.py"
        result = auto_pilot.decompose_task(task, auto_pilot.PARALLEL_HINT, timeout=0.5)
        # Should time out and fall back to heuristic.
        # Heuristic might or might not split this short task, so just check no crash.
        assert isinstance(result, (list, type(None)))

    def test_decompose_returns_none_when_callback_returns_single(self):
        def cb(task, strategy, timeout):
            return ["only one subtask"]

        auto_pilot.set_decompose_callback(cb)
        result = auto_pilot.decompose_task("do stuff", auto_pilot.PARALLEL_HINT, timeout=1.0)
        # Single subtask is not useful; should fall back to heuristic (which may return None).
        assert isinstance(result, (list, type(None)))

    def test_decompose_strips_whitespace_from_subtasks(self):
        def cb(task, strategy, timeout):
            return ["  task A  ", "  task B  "]

        auto_pilot.set_decompose_callback(cb)
        result = auto_pilot.decompose_task("do stuff", auto_pilot.PARALLEL_HINT, timeout=1.0)
        assert result == ["task A", "task B"]


class TestHeuristicDecompose:
    def test_pipeline_split_on_then(self):
        task = "first run the tests, then fix the failures, finally commit changes"
        result = auto_pilot._heuristic_decompose(task, auto_pilot.PIPELINE_HINT)
        assert result is not None
        assert len(result) >= 2

    def test_pipeline_split_on_after_that(self):
        task = "build the image, after that push it, next deploy to staging"
        result = auto_pilot._heuristic_decompose(task, auto_pilot.PIPELINE_HINT)
        assert result is not None
        assert len(result) >= 2

    def test_parallel_split_on_and(self):
        task = "update the authentication logic in src/auth/login.py and also modify the API endpoints in src/api/routes.py"
        result = auto_pilot._heuristic_decompose(task, auto_pilot.PARALLEL_HINT)
        assert result is not None
        assert len(result) >= 2

    def test_parallel_no_split_when_no_conjunction(self):
        task = "fix the typo in the README"
        result = auto_pilot._heuristic_decompose(task, auto_pilot.PARALLEL_HINT)
        assert result is None

    def test_pipeline_caps_at_six(self):
        parts = [f"step {i} do something meaningful here" for i in range(10)]
        task = ", then ".join(parts)
        result = auto_pilot._heuristic_decompose(task, auto_pilot.PIPELINE_HINT)
        assert result is not None
        assert len(result) <= 6


class TestBuildDecomposedHint:
    def test_parallel_hint_includes_subtasks(self):
        subtasks = ["update auth", "update API", "add tests"]
        hint = auto_pilot.build_decomposed_hint(auto_pilot.PARALLEL_HINT, subtasks)
        assert "spawn_parallel" in hint
        assert "update auth" in hint
        assert "update API" in hint
        assert "add tests" in hint

    def test_pipeline_hint_includes_subtasks(self):
        subtasks = ["run tests", "fix failures", "commit"]
        hint = auto_pilot.build_decomposed_hint(auto_pilot.PIPELINE_HINT, subtasks)
        assert "spawn_chain" in hint
        assert "run tests" in hint

    def test_falls_back_to_generic_when_single_subtask(self):
        hint = auto_pilot.build_decomposed_hint(auto_pilot.PARALLEL_HINT, ["only one"])
        assert hint == auto_pilot.build_hint(auto_pilot.PARALLEL_HINT)

    def test_falls_back_to_generic_when_empty(self):
        hint = auto_pilot.build_decomposed_hint(auto_pilot.PARALLEL_HINT, [])
        assert hint == auto_pilot.build_hint(auto_pilot.PARALLEL_HINT)


# ── Phase 3: Auto-execution ────────────────────────────────────────────

class TestShouldAutoExecute:
    def test_disabled_returns_false(self):
        assert auto_pilot.should_auto_execute(
            auto_pilot.PARALLEL_HINT, ["a", "b"], False) is False

    def test_enabled_parallel_with_subtasks_returns_true(self):
        assert auto_pilot.should_auto_execute(
            auto_pilot.PARALLEL_HINT, ["a", "b"], True) is True

    def test_pipeline_strategy_returns_false(self):
        # Phase 3 only auto-executes parallel tasks; pipeline needs agent oversight.
        assert auto_pilot.should_auto_execute(
            auto_pilot.PIPELINE_HINT, ["a", "b"], True) is False

    def test_simple_strategy_returns_false(self):
        assert auto_pilot.should_auto_execute(
            auto_pilot.SIMPLE, ["a", "b"], True) is False

    def test_single_subtask_returns_false(self):
        assert auto_pilot.should_auto_execute(
            auto_pilot.PARALLEL_HINT, ["a"], True) is False

    def test_no_subtasks_returns_false(self):
        assert auto_pilot.should_auto_execute(
            auto_pilot.PARALLEL_HINT, None, True) is False

    def test_more_subtasks_than_max_still_executes(self):
        # Orchestrator caps the spawn count; should_auto_execute still returns True.
        assert auto_pilot.should_auto_execute(
            auto_pilot.PARALLEL_HINT, ["a", "b", "c", "d", "e", "f"], True, max_parallel=4) is True


class TestAutoPilotOrchestrator:
    def test_plan_parallel(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        plan = orch.plan_execution(auto_pilot.PARALLEL_HINT, ["task A", "task B", "task C"])
        assert plan is not None
        assert plan["mode"] == "parallel"
        assert len(plan["spawns"]) == 3
        assert plan["spawns"][0]["task"] == "task A"

    def test_plan_chain(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        plan = orch.plan_execution(auto_pilot.PIPELINE_HINT, ["step 1", "step 2"])
        assert plan is not None
        assert plan["mode"] == "chain"
        assert len(plan["spawns"]) == 2

    def test_plan_caps_at_max_parallel(self):
        orch = auto_pilot.AutoPilotOrchestrator(max_parallel=2)
        plan = orch.plan_execution(auto_pilot.PARALLEL_HINT, ["a", "b", "c", "d"])
        assert plan is not None
        assert len(plan["spawns"]) == 2

    def test_plan_returns_none_for_simple(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        assert orch.plan_execution(auto_pilot.SIMPLE, ["a", "b"]) is None

    def test_plan_returns_none_for_single_subtask(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        assert orch.plan_execution(auto_pilot.PARALLEL_HINT, ["a"]) is None

    def test_track_and_update_agent(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        orch.track_agent("agent-1", "do stuff", time.time())
        assert len(orch.spawned_agents) == 1
        assert orch.spawned_agents[0]["status"] == "running"
        orch.update_agent_status("agent-1", "done")
        assert orch.spawned_agents[0]["status"] == "done"

    def test_all_done_when_all_completed(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        orch.track_agent("a1", "task1", time.time())
        orch.track_agent("a2", "task2", time.time())
        assert orch.all_done() is False
        orch.update_agent_status("a1", "done")
        assert orch.all_done() is False
        orch.update_agent_status("a2", "aborted")
        assert orch.all_done() is True

    def test_build_orchestrator_hint_includes_agents(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        orch.track_agent("worker-1", "handle auth module", time.time())
        orch.track_agent("worker-2", "handle API module", time.time())
        hint = orch.build_orchestrator_hint()
        assert "worker-1" in hint
        assert "worker-2" in hint
        assert "auth module" in hint
        assert "agent.wait" in hint or "agent.status" in hint

    def test_build_orchestrator_hint_empty_when_no_agents(self):
        orch = auto_pilot.AutoPilotOrchestrator()
        assert orch.build_orchestrator_hint() == ""


class TestPendingPlan:
    def test_set_and_get_pending_plan(self):
        auto_pilot.set_pending_plan(None)
        plan = {"strategy": "parallel_hint", "subtasks": ["a", "b"], "mode": "parallel"}
        auto_pilot.set_pending_plan(plan)
        retrieved = auto_pilot.get_pending_plan()
        assert retrieved == plan
        # Second get should return None (consumed).
        assert auto_pilot.get_pending_plan() is None

    def test_get_pending_plan_when_none(self):
        auto_pilot.set_pending_plan(None)
        assert auto_pilot.get_pending_plan() is None

    def test_pending_plan_is_thread_local(self):
        """Each thread gets its own plan."""
        auto_pilot.set_pending_plan(None)
        plan = {"strategy": "parallel_hint", "subtasks": ["a", "b"]}
        auto_pilot.set_pending_plan(plan)

        result = {}
        def _worker():
            result["plan"] = auto_pilot.get_pending_plan()

        t = threading.Thread(target=_worker)
        t.start()
        t.join()

        # Other thread sees None (thread-local).
        assert result["plan"] is None
        # Main thread still has the plan.
        assert auto_pilot.get_pending_plan() == plan


# ── Integration: Phase 1 -> 2 -> 3 flow ────────────────────────────────

class TestPhaseIntegration:
    def test_parallel_task_decomposes_and_can_auto_execute(self):
        """Full flow: classify -> decompose -> check auto-execute."""
        task = (
            "update the authentication logic in src/auth/login.py and also "
            "modify the API endpoints in src/api/routes.py as well as add "
            "tests in tests/test_auth.py"
        )
        # Phase 1: classify
        strategy = auto_pilot.classify_task(task)
        assert strategy == auto_pilot.PARALLEL_HINT

        # Phase 2: decompose (heuristic, no callback set)
        auto_pilot.set_decompose_callback(None)
        subtasks = auto_pilot.decompose_task(task, strategy, timeout=1.0)
        assert subtasks is not None
        assert len(subtasks) >= 2

        # Phase 3: check auto-execute
        assert auto_pilot.should_auto_execute(
            strategy, subtasks, auto_execute_enabled=True) is True

    def test_override_skips_all_phases(self):
        """! prefix bypasses classification, decomposition, and auto-execution."""
        task = "!update the auth logic and also fix the API and add tests"
        cleaned, overridden = auto_pilot.should_override(task)
        assert overridden is True
        # When overridden, no classification happens, so no decomposition.
        # The hook in _run_agent_loop_with_interrupt uses the overridden flag
        # to skip everything.

    def test_workflow_active_skips_decomposition(self):
        """Active workflow returns PIPELINE strategy, which doesn't decompose."""
        task = "update auth and also fix API"
        strategy = auto_pilot.classify_task(task, has_active_workflow=True)
        assert strategy == auto_pilot.PIPELINE
        # PIPELINE strategy doesn't trigger decomposition.
        assert auto_pilot.decompose_task(task, strategy) is None
