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
