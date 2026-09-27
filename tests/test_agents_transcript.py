import unittest

import agent_ui_events
import agents_transcript as T


def texts(rows):
    return [row.text for row in rows]


class WrapTests(unittest.TestCase):
    def test_continuation_rows_hang_under_the_text_not_the_bullet(self):
        rows = T.wrap([("", "alpha beta gamma delta epsilon")], 16,
                      [("", "• ")], [("", "  ")])
        self.assertEqual(["• alpha beta", "  gamma delta", "  epsilon"],
                         [T._plain(row) for row in rows])

    def test_identifiers_are_not_split_at_underscores(self):
        rows = T.wrap([("", "see focus_scroll now")], 14)
        self.assertIn("focus_scroll", " ".join(T._plain(row) for row in rows))

    def test_wide_characters_are_measured_in_cells(self):
        for row in T.wrap([("", "界" * 25)], 10):
            self.assertLessEqual(T.cell_width(T._plain(row)), 10)


class MarkdownTests(unittest.TestCase):
    def test_table_aligns_when_it_fits_and_degrades_when_it_does_not(self):
        table = "| a | bb |\n|---|---|\n| ccc | d |"
        wide = texts(T.markdown_rows(table, 40, [], []))
        self.assertEqual(wide[0].index("│"), wide[2].index("│"))
        narrow = texts(T.markdown_rows(
            "| " + "x" * 30 + " | " + "y" * 30 + " |\n|---|---|\n| 1 | 2 |",
            30, [], []))
        self.assertTrue(all(T.cell_width(row) <= 30 for row in narrow))

    def test_code_block_rows_carry_their_background_to_the_edge(self):
        rows = T.markdown_rows("```py\nx = 1\n```", 30, [], [])
        self.assertTrue(all(row.fill == "class:md.codeblock" for row in rows))
        self.assertIn("x = 1", texts(rows)[1])


    def test_deeply_indented_bullet_does_not_crash(self):
        rows = texts(T.markdown_rows("- a\n        - deep\n            - deeper",
                                     60, [], []))
        self.assertTrue(any("deeper" in row for row in rows))


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.hub = agent_ui_events.AgentUIEventHub()

    def blocks(self):
        return T.build_blocks(self.hub.agent_events("a"), "worker", "a")

    def test_tool_row_matches_the_cli_and_output_is_folded(self):
        self.hub.ingest("a", [
            {"type": "tool_started", "toolCallId": "c", "name": "read",
             "command": "f.py"},
            {"type": "system", "kind": "tool", "content": "read",
             "meta": {"call_id": "c", "ok": True}},
            {"type": "system", "kind": "output",
             "content": "\n".join(f"{i}→row{i}" for i in range(1, 21))},
        ])
        collapsed = T.render_rows(self.blocks(), 60)
        # ``● name  hint  20L`` and nothing else: output stays folded.
        self.assertEqual(len(collapsed), 1)
        self.assertTrue(collapsed[0].text.startswith("● read  f.py  20L"))
        self.assertEqual(collapsed[0].action, ("toggle", "c"))
        expanded = T.render_rows(self.blocks(), 60, expanded={"c"})
        self.assertEqual(sum("row" in row.text for row in expanded), 20)

    def test_a_failed_tool_shows_the_start_of_its_error(self):
        self.hub.ingest("a", [
            {"type": "tool_started", "toolCallId": "c", "name": "shell"},
            {"type": "system", "kind": "tool", "content": "shell",
             "meta": {"call_id": "c", "ok": False}},
            {"type": "system", "kind": "output",
             "content": "\n".join(f"err{i}" for i in range(10))},
        ])
        rows = texts(T.render_rows(self.blocks(), 60))
        self.assertIn("failed", rows[0])
        self.assertEqual(sum("err" in row for row in rows),
                         T.TOOL_ERROR_PREVIEW_LINES)
        self.assertTrue(any("+7 lines" in row for row in rows))

    def test_carriage_returns_never_reach_the_screen(self):
        self.hub.ingest("a", [
            {"type": "tool_started", "toolCallId": "c", "name": "shell"},
            {"type": "system", "kind": "tool", "content": "shell",
             "meta": {"call_id": "c", "ok": True}},
            {"type": "system", "kind": "output",
             "content": "one\r\n10%\r55%\r100%\r\n"},
        ])
        rows = texts(T.render_rows(self.blocks(), 60, expand_all=True))
        self.assertFalse(any("\r" in row for row in rows))
        self.assertTrue(any(row.endswith("100%") for row in rows))
        self.assertFalse(any("55%" in row for row in rows))

    def test_task_complete_is_not_printed_twice(self):
        self.hub.ingest("a", [
            {"type": "tool_started", "toolCallId": "c", "name": "task_complete",
             "command": "All done"},
            {"type": "system", "kind": "tool", "content": "task_complete",
             "meta": {"call_id": "c", "ok": True}},
            {"type": "system", "kind": "output", "content": "All done"},
            {"type": "ai", "content": "All done"},
        ])
        rows = texts(T.render_rows(self.blocks(), 60))
        self.assertEqual(sum("All done" in row for row in rows), 1)

    def test_a_finished_turn_adds_no_footer_line(self):
        self.hub.emit("user_message", agent_id="a", detail="go")
        self.hub.ingest("a", [{"type": "ai", "content": "answer"},
                              {"type": "system", "kind": "billing",
                               "content": "m · $0.03 · balance $9.00"}])
        self.hub.emit("agent_done", agent_id="a")
        rows = [row for row in texts(T.render_rows(self.blocks(), 60)) if row]
        self.assertEqual(rows[-1], "● answer")

    def test_approval_is_resolved_in_place(self):
        self.hub.emit("approval_requested", agent_id="a", summary="rm x",
                      data={"approvalId": "p1", "kind": "command"})
        self.hub.emit("approval_resolved", agent_id="a", summary="rm x",
                      status="approved", data={"approvalId": "p1"})
        rows = texts(T.render_rows(self.blocks(), 60))
        self.assertEqual(sum("Approved" in row for row in rows), 1)
        self.assertFalse(any("Needs your approval" in row for row in rows))


if __name__ == "__main__":
    unittest.main()
