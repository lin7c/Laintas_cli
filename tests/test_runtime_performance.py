"""Correctness boundaries for sharing measurements and detached snapshots."""
from types import SimpleNamespace
from unittest import mock

import pytest

import agent_loop as loop
import background_compaction as bg
import paths
import session_store
import workgraph


@pytest.fixture
def checkpoint(monkeypatch):
    coordinator = bg.Coordinator()
    token = bg.current.set(coordinator)
    monkeypatch.setattr(loop, "aux_calls_enabled", lambda: True)
    budget = {"window": 1000000, "usable": 1000000, "output": 0,
              "reserved": 0, "overhead": 0}
    monkeypatch.setattr(loop, "compaction_budget", lambda state: budget)
    monkeypatch.setattr(loop, "_compaction_job_key", lambda state: ("test",))
    monkeypatch.setattr(loop, "_provider_window_key", lambda: "test")
    monkeypatch.setattr(loop, "model_capability", lambda model: {})
    read_config = loop.get_runtime_config
    monkeypatch.setattr(loop, "get_runtime_config", lambda key:
                        False if key == "compact_background" else read_config(key))
    try:
        yield SimpleNamespace(coordinator=coordinator, budget=budget,
                              deps=SimpleNamespace(console=mock.Mock()))
    finally:
        coordinator.close()
        bg.current.reset(token)


def test_preflight_counts_unchanged_messages_once(checkpoint):
    messages = [{"role": "user", "content": "unchanged"}]
    state = {"_session_id": "test"}
    counter = loop._ThreadTokenCounter(messages)
    with mock.patch.object(loop, "_thread_tokens", return_value=100) as count:
        loop._coordinate_compaction(messages, checkpoint.deps, {}, "EN", state,
                                    token_counter=counter)
        assert counter.count() < checkpoint.budget["usable"]
        loop._publish_context_headroom(messages, state, tokens=counter.count())
    assert count.call_count == 1
    assert state["_context_live_tokens"] == 100
    assert "_ctx_page_chars" in state


def test_in_place_foreground_pruning_invalidates_measurement(checkpoint, monkeypatch):
    messages = [{"role": "user", "content": "long content"}]
    state = {"_session_id": "test"}
    monkeypatch.setattr(loop, "_thread_tokens", lambda ms: len(ms[0]["content"]))
    monkeypatch.setattr(loop, "aux_calls_enabled", lambda: False)
    counter = loop._ThreadTokenCounter(messages)
    assert counter.count() == 12

    def prune(*args, **kwargs):
        messages[0]["content"] = "short"
        return False  # Pruning can mutate without committing a summary.

    monkeypatch.setattr(loop, "_compact_thread_messages", prune)
    assert not loop._coordinate_compaction(messages, checkpoint.deps, {}, "EN", state,
                                           token_counter=counter)
    assert counter.count() == 5


def test_fresh_checkpoint_does_not_reuse_a_same_length_edit(checkpoint, monkeypatch):
    messages = [{"role": "user", "content": "a"}]
    monkeypatch.setattr(loop, "_thread_tokens", lambda ms: ord(ms[0]["content"]))
    assert loop._ThreadTokenCounter(messages).count() == ord("a")
    messages[0]["content"] = "b"
    assert loop._ThreadTokenCounter(messages).count() == ord("b")


def test_runtime_save_detaches_both_state_views_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(workgraph, "get_active_work", lambda **kwargs: None)
    state = {"_thread_messages": [{"role": "user", "content": ["original"]}]}
    history = [{"role": "user", "content": ["first"]}]
    session = session_store.create_session(str(tmp_path), state, history)
    state["_thread_messages"][0]["content"].append("new turn")
    history[0]["content"].append("second")
    assert session["state"]["_thread_messages"][0]["content"] == ["original"]
    assert session["chat_history"][0]["content"] == ["first"]

    session_store.sync_runtime(session, state, history)
    state["_thread_messages"][0]["content"].append("later")
    history[0]["content"].append("third")
    expected = ["original", "new turn"]
    assert session["state"]["_thread_messages"][0]["content"] == expected
    assert session["agent_state"]["_thread_messages"][0]["content"] == expected
    session["state"]["_thread_messages"][0]["content"].append("local mutation")
    assert session["agent_state"]["_thread_messages"][0]["content"] == expected
    loaded = session_store.load_current_session(str(tmp_path))
    assert loaded["state"]["_thread_messages"][0]["content"] == expected
    assert loaded["chat_history"][0]["content"] == ["first", "second"]
