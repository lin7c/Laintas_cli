"""Deterministic concurrency tests: no model/network calls and no sleeping."""
import contextvars
import copy
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import agent_loop as loop
import background_compaction as bg


SUMMARY = "## Goal\nFix service\n## Progress\nTests failed\n## Next Steps\nRepair\n## Critical Context\nNo deployment"


@pytest.fixture
def env(monkeypatch):
    loop.reset_runtime_config()
    bg.forget_folds()
    bg._parked.clear()
    coordinator = bg.Coordinator()
    token = bg.current.set(coordinator)
    messages = [{"role": "user", "content": "a" * 6000},
                {"role": "assistant", "content": "b" * 1000},
                {"role": "user", "content": "recent"},
                {"role": "assistant", "content": "working"}]
    state = {"_session_id": "test-background", "_agent_id": "test-agent",
             "_task_cwd": "/snapshot/cwd", "_run_id": "run-one",
             "_thread_messages": messages, "objective": "unchanged"}
    monkeypatch.setattr(loop, "compaction_budget", lambda state: {
        "window": 30000, "usable": 10000, "reserved": 12000, "overhead": 2000})
    monkeypatch.setattr(loop, "_thread_tokens", lambda ms: sum(
        len(m.get("content", "")) + 50 for m in ms))
    monkeypatch.setattr(loop.ctxpol, "keep_recent_tokens", lambda usable: 1000)
    monkeypatch.setattr(loop, "_consolidate_memories_on_compact", mock.Mock())
    deps = SimpleNamespace(call_backend=mock.Mock(), console=mock.Mock(render_terminal=False))
    entered, release = threading.Event(), threading.Event()

    def summarize(*args, **kwargs):
        entered.set()
        while not release.wait(0.01):
            if args[6].is_set():
                return None
        return SUMMARY

    summarizer = mock.Mock(side_effect=summarize)
    monkeypatch.setattr(loop, "_summarize_head_in_chunks", summarizer)

    def check(**kwargs):
        return loop._coordinate_compaction(messages, deps, {}, "EN", state, **kwargs)

    data = SimpleNamespace(**locals())
    yield data
    release.set()
    job = coordinator.job
    coordinator.close()
    for parked in list(bg._parked.values()):
        parked.close()
    bg._parked.clear()
    if job is not None:
        job.thread.join(2)
        assert not job.thread.is_alive()
    bg.current.reset(token)
    loop.reset_runtime_config()
    assert bg.status(loop._compaction_owner(state)) == "idle"
    bg.forget_folds()
    bg._parked.clear()


def start(e):
    assert not e.check()
    assert e.entered.wait(2)
    return e.coordinator.job


def finish(e):
    e.release.set()
    assert e.coordinator.job.done.wait(2)


def test_background_does_not_block_or_mutate_and_commits_appended_messages(env):
    e = env
    original = copy.deepcopy(e.messages)
    job = start(e)
    assert not job.done.is_set()
    assert e.messages == original
    assert "_thread_summary" not in e.state
    fresh = [{"role": "assistant", "tool_calls": [{"id": "new-call"}]},
             {"role": "tool", "tool_call_id": "new-call", "content": "new result"}]
    e.messages.extend(fresh)
    assert not e.check()
    assert e.summarizer.call_count == 1
    finish(e)
    assert e.check()
    assert e.messages[1:] == original[2:] + fresh
    assert e.state["objective"] == "unchanged"
    assert e.state["_thread_summary"] == SUMMARY
    assert e.summarizer.call_args.kwargs["current_path"] == "/snapshot/cwd"
    assert e.deps.console.print.call_count == 0
    assert not job.thread.is_alive()


@pytest.mark.parametrize("mutation", ["prefix", "summary", "session", "config"])
def test_stale_result_never_overwrites_live_history(env, mutation):
    e = env
    start(e)
    finish(e)
    if mutation == "prefix":
        e.messages[0]["content"] += "changed"
    elif mutation == "summary":
        e.state["_thread_summary"] = "newer summary"
    elif mutation == "session":
        e.state["_session_id"] = "another session"
    else:
        loop.set_runtime_config("compact_review_effort", "low")
    before = copy.deepcopy(e.messages)
    assert not e.check()
    assert e.messages == before


def test_foreground_threshold_waits_for_same_job_without_duplicate_summary(env, monkeypatch):
    e = env
    start(e)
    e.messages.append({"role": "assistant", "content": "new" * 1000})
    waiting, result = threading.Event(), []
    original_wait = e.coordinator.wait

    def wait(event):
        waiting.set()
        return original_wait(event)

    monkeypatch.setattr(e.coordinator, "wait", wait)
    fallback = mock.Mock()
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    context = contextvars.copy_context()
    thread = threading.Thread(target=lambda: context.run(lambda: result.append(e.check())))
    thread.start()
    try:
        assert waiting.wait(2)
        assert not result
        e.release.set()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        e.release.set()
        thread.join(2)
    assert result == [True]
    assert not fallback.called
    assert e.summarizer.call_count == 1
    assert e.messages[-1]["content"] == "new" * 1000


def test_overflow_reuses_ready_background_result(env, monkeypatch):
    start(env)
    finish(env)
    fallback = mock.Mock()
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    assert env.check(force=True)
    fallback.assert_not_called()


def test_failed_background_job_falls_back_at_hard_threshold(env, monkeypatch):
    env.summarizer.side_effect = lambda *a, **k: None
    env.check()
    assert env.coordinator.job.done.wait(2)
    env.messages.append({"role": "assistant", "content": "x" * 3000})
    fallback = mock.Mock(return_value=True)
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    assert env.check()
    fallback.assert_called_once()


def test_failed_attempt_is_not_repeated_at_every_checkpoint(env):
    env.summarizer.side_effect = lambda *a, **k: None
    loop.set_runtime_config("compact_background_cooldown", 0)
    env.check()
    assert env.coordinator.job.done.wait(2)
    for _ in range(4):
        env.check()
    assert env.summarizer.call_count == 1


def test_cooldown_prevents_immediate_new_attempt_with_different_prefix(env):
    env.summarizer.side_effect = lambda *a, **k: None
    env.check()
    assert env.coordinator.job.done.wait(2)
    env.check()
    env.messages[0]["content"] += "more evidence"
    env.check()
    assert env.summarizer.call_count == 1


def test_expired_job_is_discarded(env, monkeypatch):
    job = start(env)
    job.cancel.deadline = time.monotonic() - 1
    assert job.done.wait(2)
    original = copy.deepcopy(env.messages)
    assert not env.check()
    assert env.messages == original


def test_completed_summary_does_not_expire_while_main_model_is_busy(env):
    job = start(env)
    finish(env)
    job.cancel.deadline = time.monotonic() - 1
    assert env.check()


def test_turn_exit_only_commits_ready_work_and_never_starts_or_waits(env, monkeypatch):
    assert not env.check(finish_only=True)
    assert env.coordinator.job is None
    start(env)
    env.messages.append({"role": "assistant", "content": "x" * 3000})
    fallback = mock.Mock()
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    assert not env.check(finish_only=True)
    finish(env)
    assert env.check(finish_only=True)
    fallback.assert_not_called()


def test_interrupt_cancels_without_committing_or_starting_fallback(env, monkeypatch):
    event = threading.Event()
    assert not env.check(interrupt_event=event)
    assert env.entered.wait(2)
    job = env.coordinator.job
    fallback = mock.Mock()
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    event.set()
    assert not env.check(force=True, interrupt_event=event)
    assert job.done.wait(2)
    assert not job.thread.is_alive()
    assert "_thread_summary" not in env.state
    fallback.assert_not_called()


def test_manual_compaction_cancels_speculation_first(env, monkeypatch):
    job = start(env)
    fallback = mock.Mock(return_value=False)
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    loop.compact_session_context(env.deps, {}, env.state)
    assert job.cancel.requested()
    assert not job.thread.is_alive()
    fallback.assert_called_once()


@pytest.mark.parametrize("disable", ["policy", "config"])
def test_disabling_background_invalidates_pending_result(env, monkeypatch, disable):
    job = start(env)
    if disable == "policy":
        monkeypatch.setattr(loop.ctxpol, "load", lambda: {"auto": False})
    else:
        loop.set_runtime_config("compact_background", False)
    env.check()
    assert job.cancel.requested()
    assert job.done.wait(2)
    assert not env.check()
    assert "_thread_summary" not in env.state


def test_automatic_policy_off_still_allows_forced_recovery(env, monkeypatch):
    monkeypatch.setattr(loop.ctxpol, "load", lambda: {"auto": False})
    fallback = mock.Mock(return_value=True)
    monkeypatch.setattr(loop, "_compact_thread_messages", fallback)
    assert not env.check()
    assert env.check(force=True)
    fallback.assert_called_once()


def test_no_speculation_below_threshold(env):
    env.messages[0]["content"] = "x" * 4000
    assert not env.check()
    assert env.coordinator.job is None


def test_cooldown_survives_repl_state_preparation(env):
    env.state["_compact_background_at"] = time.time()
    prepared = loop.prepare_state_for_repl(env.state)
    assert prepared["_compact_background_at"] == env.state["_compact_background_at"]
    assert not env.check()
    assert env.coordinator.job is None


def test_context_status_reports_coordinated_thresholds_and_worker(env):
    info = loop.session_context_status(env.state)
    assert (info["background_at"], info["auto_at"], info["target_tokens"]) == (7000, 9000, 5000)
    assert info["background_status"] == "idle"
    start(env)
    assert loop.session_context_status(env.state)["background_status"] == "running"
    finish(env)
    assert loop.session_context_status(env.state)["background_status"] == "ready"


def test_worker_start_failure_does_not_break_the_turn_or_keep_a_slot(env, monkeypatch):
    monkeypatch.setattr(bg.threading.Thread, "start", mock.Mock(side_effect=RuntimeError("no threads")))
    assert not env.check()
    assert env.coordinator.job is None
    assert bg.status(loop._compaction_owner(env.state)) == "idle"


def test_status_command_displays_background_and_foreground_thresholds(env, monkeypatch):
    import io
    import laintas_cli
    from rich.console import Console
    output = io.StringIO()
    monkeypatch.setattr(laintas_cli, "console", Console(file=output, width=200))
    monkeypatch.setattr(laintas_cli.handle_meta_command, "_last_agent_state", env.state, raising=False)
    monkeypatch.setattr(laintas_cli.handle_meta_command, "_last_deps", env.deps, raising=False)
    monkeypatch.setattr(laintas_cli.handle_meta_command, "_last_session", {}, raising=False)
    laintas_cli._cmd_compact(["/compact", "status"], {})
    assert "background at" in output.getvalue()
    assert "foreground at" in output.getvalue()
    assert "background task: idle" in output.getvalue()


def test_background_summary_must_reclaim_enough_tokens(env):
    env.summarizer.side_effect = lambda *a, **k: SUMMARY + "x" * 6000
    before = copy.deepcopy(env.messages)
    env.check()
    assert env.coordinator.job.done.wait(2)
    assert not env.check()
    assert env.messages == before


@pytest.mark.parametrize("key,value", [("compact_background_ratio", .95),
    ("compact_target_ratio", .75), ("compact_auto_ratio", .6),
    ("compact_background_ratio", float("nan")), ("compact_background_timeout", 0)])
def test_invalid_thresholds_are_rejected(env, key, value):
    with pytest.raises(ValueError):
        loop.set_runtime_config(key, value)


def test_scope_parks_work_even_when_the_run_raises(env):
    jobs = []

    @bg.scoped
    def run():
        env.check()
        jobs.append(bg.current.get().job)
        assert env.entered.wait(2)
        raise RuntimeError("loop failed")

    with pytest.raises(RuntimeError, match="loop failed"):
        run()
    # A crashed run does not invalidate a summary of the thread's head, and the
    # scope must still be restored.
    assert not jobs[0].cancel.requested()
    assert bg._parked
    assert bg.current.get() is env.coordinator


def test_process_and_session_limits_prevent_duplicate_workers():
    coordinators = [bg.Coordinator() for _ in range(4)]
    release = threading.Event()
    def worker(job):
        release.wait(2)
        return SUMMARY
    def start_one(i, owner):
        return coordinators[i].start(owner=(owner,), key=(), prefix=[], previous=None,
            worker=worker, parent=None, timeout=10, cooldown=0, signature=i)
    try:
        assert start_one(0, "first")
        assert not start_one(1, "first")
        assert start_one(2, "second")
        assert not start_one(3, "third")
    finally:
        release.set()
        for coordinator in coordinators:
            coordinator.close()
            if coordinator.job:
                assert not coordinator.job.thread.is_alive()


def test_summary_started_in_one_run_is_committed_by_the_next(env):
    """A turn boundary is not a reason to throw away a paid-for summary.

    Cancelling the job at run exit (and keying it by `_run_id`) meant every
    speculative summary that outlived its turn was discarded, so on short turns
    the background path could never commit anything at all.
    """
    e = env
    original = copy.deepcopy(e.messages)
    started = []

    @bg.scoped
    def first_turn():
        assert not e.check()
        assert e.entered.wait(2)
        started.append(bg.current.get().job)

    first_turn()
    job = started[0]
    assert not job.cancel.requested()        # parked, not killed
    assert bg._parked
    e.release.set()
    assert job.done.wait(2)

    @bg.scoped
    def second_turn():
        return e.check()

    assert second_turn()                     # adopted and committed
    assert e.state["_thread_summary"] == SUMMARY
    assert e.messages[1:] == original[2:]
    assert e.summarizer.call_count == 1
    assert not bg._parked


def test_run_exit_still_cancels_an_interrupted_job(env):
    e = env
    interrupt = threading.Event()

    @bg.scoped
    def run():
        assert not e.check(interrupt_event=interrupt)
        assert e.entered.wait(2)
        job = bg.current.get().job
        interrupt.set()
        return job

    job = run()
    assert job.done.wait(2)
    assert job.cancelled
    assert not bg._parked


@pytest.fixture
def folding(monkeypatch):
    """Drive `_summarize_head_in_chunks` directly over two known chunks."""
    bg.forget_folds()
    folded = []
    stop = threading.Event()

    def summarize(deps, session, cwd, text, summary, lang, trajectory, cancel):
        folded.append(text)
        return "merged:" + text

    monkeypatch.setattr(loop, "_llm_summarize", summarize)
    monkeypatch.setattr(loop, "_llm_review_summary",
                        lambda d, s, c, text, merged, prev, lang, t, cancel: SUMMARY + merged)
    monkeypatch.setattr(loop, "_valid_structured_summary", lambda summary, lang: bool(summary))
    monkeypatch.setattr(loop, "_summary_source_chunks", lambda head, budget: ["one", "two"])
    monkeypatch.setattr(loop, "_summary_window", lambda model: 100000)

    def fold(interrupt=None):
        return loop._summarize_head_in_chunks(
            mock.Mock(), {}, [{"role": "user", "content": "evidence"}],
            None, "EN", "traj", interrupt)

    yield SimpleNamespace(folded=folded, stop=stop, fold=fold)
    bg.forget_folds()


def test_completed_folds_survive_an_abandoned_attempt(folding):
    """The cache is what lets a head bigger than one attempt ever compact."""
    give_up = threading.Event()
    original = loop._llm_review_summary

    def review(*args):
        result = original(*args)
        give_up.set()                        # stall once the first fold is in
        return result

    loop._llm_review_summary = review
    try:
        assert folding.fold(give_up) is None
    finally:
        loop._llm_review_summary = original
    assert folding.folded == ["one"]

    folding.folded.clear()
    assert folding.fold(threading.Event()).endswith("merged:two")
    assert folding.folded == ["two"]         # chunk 1 reused, not re-billed


def test_stall_deadline_restarts_on_every_completed_fold(folding):
    """A four-chunk head must not die because the JOB took longer than one."""
    cancel = bg.Cancellation(None, 1)
    cancel.timeout = 0.3
    cancel.deadline = time.monotonic() + 0.3
    original = loop._llm_summarize

    def summarize(*args):
        time.sleep(0.2)                      # each fold outlives a total budget
        return original(*args)

    loop._llm_summarize = summarize
    try:
        assert folding.fold(cancel) is not None
    finally:
        loop._llm_summarize = original
    assert folding.folded == ["one", "two"]
