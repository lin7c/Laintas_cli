"""Regression coverage for bounded, evidence-grounded compaction."""
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

import agent_loop as loop
from context_policy import adapter


SUMMARY = "## Goal\nFix build\n## Progress\nBlocked\n## Next Steps\nRetry\n## Critical Context\nexit 7"


def test_review_receives_source_and_previous_summary():
    backend = mock.Mock(return_value={"reply": SUMMARY})
    loop._llm_review_summary(SimpleNamespace(call_backend=backend), {}, "/tmp",
                             "Tests FAILED: exit 7", SUMMARY, "No deployment", "EN")
    message = backend.call_args.kwargs["message"]
    assert "<source-transcript>\nTests FAILED: exit 7\n</source-transcript>" in message
    assert "<trusted-previous-summary>\nNo deployment" in message


def test_backend_error_does_not_trigger_a_paid_review_call():
    backend = mock.Mock(return_value={"error": True, "reply": "connection failed"})
    assert loop._summarize_head_in_chunks(
        SimpleNamespace(call_backend=backend), {}, [{"role": "user", "content": "fix"}],
        None, "EN", "test") is None
    assert backend.call_count == 1


def test_review_error_cannot_replace_the_draft_even_with_markdown():
    backend = mock.Mock(return_value={"error": True, "reply": SUMMARY + "\nwrong"})
    assert loop._llm_review_summary(SimpleNamespace(call_backend=backend), {}, "/tmp",
                                    "source", SUMMARY, None, "EN") == SUMMARY


def test_log_tail_and_tool_arguments_survive_packing():
    head = []
    for i in range(8):
        head.extend([
            {"role": "assistant", "tool_calls": [{"id": str(i), "function": {
                "name": "shell", "arguments": '{"command":"pytest regression.py"}'}}]},
            {"role": "tool", "name": "shell", "content":
             "build start\n" + "ordinary output\n" * 10000 + f"FAILED assertion_{i}: exit 7"},
        ])
    chunks = loop._summary_source_chunks(head, 4000)
    assert len(chunks) < 8
    for chunk in chunks:
        assert loop.tokenizer.count_tokens(chunk) <= 4000
    source = "\n".join(chunks)
    assert "pytest regression.py" in source
    for i in range(8):
        assert f"FAILED assertion_{i}: exit 7" in source


@pytest.mark.parametrize("text", ["word " * 10000, "禁止部署。验证失败。" * 3000])
def test_oversized_message_is_bounded_and_lossless(text):
    message = {"role": "user", "content": text}
    chunks = loop._summary_source_chunks([message], 4000)
    assert len(chunks) > 1
    assert "".join(chunks) == loop._serialize_thread_msg(message)
    assert all(loop.tokenizer.count_tokens(chunk) <= 4000 for chunk in chunks)


def test_protected_tool_is_not_truncated_during_serialization():
    content = "user rule\n" * 1000 + "DO NOT DEPLOY"
    assert content in loop._serialize_thread_msg({
        "role": "tool", "name": "skill_load", "content": content})


def test_tool_exchange_moves_together_to_next_chunk():
    head = [{"role": "user", "content": "word " * 2800},
            {"role": "assistant", "tool_calls": [{"function": {
                "name": "shell", "arguments": "pytest"}}]},
            {"role": "tool", "name": "shell", "content": "failure " * 250}]
    chunks = loop._summary_source_chunks(head, 4000)
    containing = [c for c in chunks if "[Tool shell]" in c]
    assert len(containing) == 1
    assert "pytest" in containing[0]


@pytest.mark.parametrize("ratio", [-2, 0, 0.35, 1, 3])
def test_tail_ratio_cannot_expand_the_payload_budget(ratio):
    text = "A" * 5000 + "Z" * 5000
    result = adapter.truncate_tool_output(text, {
        "tool_output_max_chars": 2000, "tool_output_tail_ratio": ratio})
    assert len(result) < 2100
    assert "truncated 8000 chars" in result


def test_interrupt_during_final_review_does_not_return_a_summary():
    event = threading.Event()
    def review(*args):
        event.set()
        return SUMMARY
    with mock.patch.object(loop, "_llm_summarize", return_value=SUMMARY), \
            mock.patch.object(loop, "_llm_review_summary", side_effect=review):
        assert loop._summarize_head_in_chunks(
            None, {}, [{"role": "user", "content": "fix build"}],
            None, "EN", "test", event) is None


@pytest.mark.parametrize('ending', [
    {'_truncated': True}, {'finish_reason': 'length'},
    {'finish_reason': 'content_filter'}, {'finish_reason': 'tool_calls'},
])
def test_incomplete_summary_is_never_committed(ending):
    backend = mock.Mock(return_value={'reply': SUMMARY, **ending})
    deps = SimpleNamespace(call_backend=backend)
    assert loop._llm_summarize(deps, {}, '/tmp', 'source', None, 'EN') is None
    assert loop._llm_review_summary(deps, {}, '/tmp', 'source',
                                    SUMMARY + '\ncomplete', None, 'EN') == SUMMARY + '\ncomplete'


def test_auxiliary_requests_include_output_limit_and_fit_small_window():
    calls = []
    def backend(**kwargs):
        calls.append(kwargs)
        return {'reply': SUMMARY, 'finish_reason': 'stop'}
    with mock.patch.object(loop, '_summary_window', return_value=8192):
        result = loop._summarize_head_in_chunks(
            SimpleNamespace(call_backend=backend), {},
            [{'role': 'user', 'content': 'Never deploy. ' * 10000}], None, 'EN', 'budget')
    assert result == SUMMARY
    assert len(calls) > 2
    for call in calls:
        assert call['max_tokens_override'] == 1024
        assert (loop._summary_token_count(call['message'])
                + loop._summary_token_count(call['system_prompt']) + 1024 + 512 <= 8192)


def test_oversized_previous_summary_fails_without_sending_overbudget_request():
    backend = mock.Mock()
    with mock.patch.object(loop, '_summary_window', return_value=8192):
        assert loop._summarize_head_in_chunks(
            SimpleNamespace(call_backend=backend), {}, [{'role': 'user', 'content': 'new'}],
            'previous ' * 20000, 'EN', 'budget') is None
    backend.assert_not_called()


@pytest.mark.parametrize('truncated', [False, True])
def test_huge_single_tool_turn_compacts_atomically(truncated):
    import copy
    messages = [
        {'role': 'user', 'content': 'Inspect build; never deploy'},
        {'role': 'assistant', 'tool_calls': [{'id': 'one', 'function': {
            'name': 'shell', 'arguments': '{"command":"build"}'}}]},
        {'role': 'tool', 'tool_call_id': 'one', 'name': 'shell',
         'content': 'build output\n' * 20000 + 'FAILED exit 7'},
    ]
    original = copy.deepcopy(messages)
    backend = mock.Mock(return_value={'reply': SUMMARY, '_truncated': truncated})
    state = {'_thread_messages': messages}
    with mock.patch.object(loop, 'compaction_budget', return_value={
        'window': 32000, 'usable': 4000}), mock.patch.object(
            loop, '_consolidate_memories_on_compact'):
        changed = loop._compact_thread_messages(messages, SimpleNamespace(call_backend=backend),
                                                {}, 'EN', state, force=True)
    assert changed is not truncated
    if truncated:
        assert messages == original
        assert '_thread_summary' not in state
    else:
        assert len(messages) == 1
        assert loop._thread_tokens(messages) < 4000
        assert 'FAILED exit 7' in backend.call_args_list[0].kwargs['message']


def test_fixed_overhead_does_not_invent_context_space():
    with mock.patch.object(loop, '_effective_context_window', return_value=32000), \
            mock.patch.object(loop, '_per_request_overhead_tokens', return_value=35000):
        assert loop.compaction_budget()['usable'] == 0
        backend = mock.Mock()
        result = loop.compact_session_context(SimpleNamespace(call_backend=backend), {}, {
            '_thread_messages': [{'role': 'user', 'content': 'large ' * 10000}]})
    assert result['ok'] is False
    backend.assert_not_called()


def test_model_window_switch_and_smaller_real_window(monkeypatch, tmp_path):
    monkeypatch.setattr(loop.paths, 'LAINTAS_HOME', tmp_path)
    monkeypatch.setattr(loop, '_provider_context_window', 0)
    monkeypatch.setattr(loop, '_provider_window_cache_loaded', False)
    monkeypatch.setattr(loop, '_provider_window_model', None)
    monkeypatch.setattr(loop, '_provider_window_persisted', {})
    monkeypatch.setattr(loop, '_provider_window_key', lambda: 'small')
    with mock.patch.object(loop, 'get_runtime_config', side_effect=lambda k:
                           {'model_context_window': 64000, 'context_window_adopt_cap': 200000}.get(k)):
        loop._note_provider_context_window(32768)
        assert loop._effective_context_window() == 32768
        monkeypatch.setattr(loop, '_provider_window_key', lambda: 'large')
        assert loop._effective_context_window() == 64000
        loop._note_provider_context_window(1000000)
        assert loop._effective_context_window() == 200000
        monkeypatch.setattr(loop, '_provider_window_key', lambda: 'small')
        assert loop._effective_context_window() == 32768


def test_huge_latest_user_turn_with_old_history_has_emergency_fallback():
    messages = [{'role': 'user', 'content': 'old task'},
                {'role': 'assistant', 'content': 'old answer'},
                {'role': 'user', 'content': '禁止部署，修复测试。' * 10000}]
    backend = mock.Mock(return_value={'reply': SUMMARY})
    with mock.patch.object(loop, 'compaction_budget', return_value={
            'window': 32000, 'usable': 4000}), \
            mock.patch.object(loop, '_consolidate_memories_on_compact'):
        assert loop._compact_thread_messages(messages, SimpleNamespace(call_backend=backend),
                                              {}, 'EN', {}, force=True)
    assert len(messages) == 1
    assert backend.call_count > 2


def test_incomplete_tool_batch_is_not_emergency_compacted():
    messages = [{'role': 'user', 'content': 'large ' * 20000},
                {'role': 'assistant', 'tool_calls': [{'id': 'pending', 'function': {
                    'name': 'shell', 'arguments': '{}'}}]}]
    backend = mock.Mock()
    with mock.patch.object(loop, 'compaction_budget', return_value={
            'window': 32000, 'usable': 4000}):
        assert not loop._compact_thread_messages(messages, SimpleNamespace(call_backend=backend),
                                                  {}, 'EN', {}, force=True)
    backend.assert_not_called()
    assert messages[-1]['tool_calls'][0]['id'] == 'pending'


def test_observed_auxiliary_window_limits_next_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(loop.paths, 'LAINTAS_HOME', tmp_path)
    monkeypatch.setattr(loop, '_summary_observed_windows', {})
    model = loop.aux_model_override()[0]
    backend = mock.Mock(return_value={'error': True, '_budget': {'contextWindow': 8192}})
    assert loop._llm_summarize(SimpleNamespace(call_backend=backend), {}, '/tmp',
                               'source', None, 'EN') is None
    assert loop._summary_window(model) == 8192
    assert loop._summary_output_limit() == 1024
