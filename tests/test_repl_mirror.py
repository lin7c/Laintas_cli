import io
import sys
import unittest
from unittest import mock

import repl_mirror


class ReplMirrorTransientTests(unittest.TestCase):
    def test_workbench_subscriber_receives_visible_chunks_after_lock(self):
        hub = repl_mirror.MirrorHub()
        tee = repl_mirror.TeeFile(lambda: "primary", hub)
        observed = []
        hub.subscribe(lambda agent_id, text, recording: observed.append(
            (agent_id, text, recording)))

        hub.set_owner("agents")
        tee.write("command output\n")
        hub.start_recording()
        tee.write("agent output\n")
        hub.stop_recording()

        self.assertEqual(observed, [
            ("primary", "command output\n", False),
            ("primary", "agent output\n", True),
        ])

    def test_workbench_subscriber_can_be_removed(self):
        hub = repl_mirror.MirrorHub()
        tee = repl_mirror.TeeFile(lambda: "primary", hub)
        observed = []
        callback = lambda *_args: observed.append(True)
        hub.subscribe(callback)
        hub.unsubscribe(callback)
        hub.set_owner("agents")
        tee.write("not observed\n")
        self.assertEqual(observed, [])

    def test_transient_live_output_reaches_cli_but_not_history(self):
        hub = repl_mirror.MirrorHub()
        tee = repl_mirror.TeeFile(lambda: "primary", hub)
        output = io.StringIO()
        hub.start_recording()

        with mock.patch.object(sys, "stdout", output):
            tee.write("before\n")
            with tee.transient_output():
                tee.write("L› Thinking… 0.0s · default · ACT")
            tee.write("after\n")

        self.assertEqual(hub.read_lines("primary"), ["before", "after"])
        self.assertIn("Thinking…", output.getvalue())

    def test_transient_output_is_not_replayed_after_agents_mode(self):
        hub = repl_mirror.MirrorHub()
        tee = repl_mirror.TeeFile(lambda: "primary", hub)
        output = io.StringIO()
        hub.start_recording()

        with mock.patch.object(sys, "stdout", output):
            hub.set_owner("agents")
            with tee.transient_output():
                tee.write("L» Writing… 1.8s · model · ACT")
            hub.set_owner("cli")

        self.assertEqual(output.getvalue(), "")
        self.assertEqual(hub.read_lines("primary"), [])

    def test_split_live_status_text_is_filtered_defensively(self):
        self.assertEqual(
            repl_mirror._filter_for_mirror(
                "L› \x1b[1mT\x1b[0mhinking… 0.0s · default · ACT"),
            "",
        )

    def test_legacy_braille_status_is_still_filtered(self):
        self.assertEqual(
            repl_mirror._filter_for_mirror(
                "⠋ Thinking… 0.0s · default · ACT"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
