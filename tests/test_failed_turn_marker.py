"""A turn that dies before answering must say so in the transcript.

Reconstructed from 2026-08-26: a question failed 4s in with a backend error and
stayed as the last message in the thread. Three hours later an unrelated
question was appended behind it, the provider received two consecutive user
turns, and the model spent 45 minutes serving both at once — twice flagged
off-track by the critic, twice interrupted by the user.
"""
import unittest
from unittest import mock

import agent_loop


class _Console:
    def __init__(self):
        self.lines = []

    def print(self, *a, **k):
        self.lines.append(" ".join(str(x) for x in a))


class _Deps:
    def __init__(self):
        self.console = _Console()


class FailedTurnMarkerTests(unittest.TestCase):
    def setUp(self):
        self.deps = _Deps()

    def close(self, history, thread=None, reason=agent_loop.TRANSITION_BACKEND_ERROR,
              state=None):
        state = {} if state is None else state
        agent_loop._close_failed_turn(history, thread, reason, self.deps, state)
        return state

    def test_unanswered_question_gets_a_marker(self):
        history = [{"role": "user", "content": "which callers use /api/ocr?"}]
        thread = [{"role": "user", "content": "which callers use /api/ocr?"}]
        self.close(history, thread)
        self.assertEqual(history[-1]["message_kind"], "turn_failed")
        self.assertIn("NOT answered", history[-1]["content"])
        # The marker must reach the transcript that is actually sent upstream.
        self.assertEqual(thread[-1]["role"], "assistant")
        self.assertIn("NOT answered", thread[-1]["content"])

    def test_the_next_question_no_longer_follows_a_bare_user_turn(self):
        history = [{"role": "user", "content": "question one"}]
        thread = [{"role": "user", "content": "question one"}]
        self.close(history, thread)
        thread.append({"role": "user", "content": "question two"})
        roles = [m["role"] for m in thread]
        self.assertNotIn(["user", "user"],
                         [roles[i:i + 2] for i in range(len(roles) - 1)])

    def test_provider_error_text_is_carried_into_the_marker(self):
        history = [{"role": "user", "content": "question one"}]
        state = {"shortTermMemory": "-Error: Server Error: key not accepted\n"}
        self.close(history, None, state=state)
        self.assertIn("key not accepted", history[-1]["content"])

    def test_the_user_is_told_on_screen(self):
        history = [{"role": "user", "content": "which callers use /api/ocr?"}]
        self.close(history)
        self.assertTrue(any("not answered" in ln.lower()
                            for ln in self.deps.console.lines))

    def test_an_answered_turn_is_untouched(self):
        history = [{"role": "user", "content": "q"},
                   {"role": "assistant", "content": "a"}]
        self.close(history, reason=agent_loop.TRANSITION_BACKEND_ERROR)
        self.assertEqual(len(history), 2)

    def test_a_clean_exit_is_untouched(self):
        history = [{"role": "user", "content": "q"}]
        self.close(history, reason=agent_loop.TRANSITION_END_TURN)
        self.assertEqual(len(history), 1)

    def test_an_interrupt_is_untouched(self):
        # The user stopped it on purpose and knows the state; a marker there
        # would be noise on every Ctrl+C.
        history = [{"role": "user", "content": "q"}]
        self.close(history, reason=agent_loop.TRANSITION_INTERRUPTED)
        self.assertEqual(len(history), 1)

    def test_pending_prompt_is_recorded_in_state(self):
        history = [{"role": "user", "content": "which callers use /api/ocr?"}]
        state = self.close(history)
        self.assertEqual(state["_unanswered_prompt"]["reason"],
                         agent_loop.TRANSITION_BACKEND_ERROR)
        self.assertIn("/api/ocr", state["_unanswered_prompt"]["text"])

    def test_empty_history_is_safe(self):
        self.close([])
        self.close([{"role": "tool", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
