"""Paged reading: a stable page table, eviction on turn, and an index that
survives it.

The behaviour under test was designed against a measured failure (2026-08-28):
a six-agent review spent 107 of 206 tool calls on fs.read, walking 9000-line
files through 120-line windows, because nothing in the stack had an opinion
about reading. Context was never the binding constraint there — so what these
tests protect is not "less context" but "one page per file, and what leaves
context leaves a usable pointer behind".
"""
import json
import os
import tempfile
import unittest

import agent_loop
import file_pager
import tools


def _py_source(functions: int, body_lines: int = 30) -> str:
    out = ['"""module."""', ""]
    for i in range(functions):
        out.append(f"def function_{i:03d}(argument):")
        out.append(f'    """Function {i}."""')
        for j in range(body_lines):
            out.append(f"    value_{j} = argument + {j}  # padding to give the page real width")
        out.append("    return value_0")
        out.append("")
    return "\n".join(out) + "\n"


class PageTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "module.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_py_source(60))

    def test_pages_cover_the_file_exactly_once(self):
        pages = file_pager.build_page_table(self.path, 8_000)
        total = sum(1 for _ in open(self.path, encoding="utf-8"))
        self.assertGreater(len(pages), 1)
        self.assertEqual(1, pages[0][0])
        self.assertEqual(total, pages[-1][1])
        for (a, b), (c, _d) in zip(pages, pages[1:]):
            self.assertLessEqual(a, b)
            self.assertEqual(b + 1, c)          # no gap, no overlap

    def test_the_table_is_byte_identical_for_the_same_inputs(self):
        """A page number is quoted in stubs and notes; it may not drift."""
        self.assertEqual(file_pager.build_page_table(self.path, 8_000),
                         file_pager.build_page_table(self.path, 8_000))

    def test_page_boundaries_land_on_definitions_when_one_is_in_reach(self):
        """Best-effort by design: the nudge is bounded so it cannot distort
        page sizes, so a page whose end has no definition within the search
        span legitimately cuts mid-body. What must not happen is a mid-body cut
        with a usable boundary sitting right there."""
        lines = open(self.path, encoding="utf-8").read().split("\n")
        pages = file_pager.build_page_table(self.path, 8_000)
        aligned = 0
        for (_pstart, pend), (start, _end) in zip(pages, pages[1:]):
            if lines[start - 1].startswith("def "):
                aligned += 1
                continue
            span = max(1, int((pend - _pstart + 1) * file_pager.BOUNDARY_SEARCH_RATIO))
            window = range(max(1, pend - span), min(len(lines), pend + span) + 1)
            reachable = [n for n in window if lines[n - 1].startswith("def ")]
            self.assertEqual([], reachable,
                             f"page starts at {start!r} with a definition in reach")
        self.assertGreater(aligned, len(pages) * 0.8)

    def test_a_bigger_page_budget_means_fewer_pages(self):
        self.assertLess(len(file_pager.build_page_table(self.path, 40_000)),
                        len(file_pager.build_page_table(self.path, 8_000)))

    def test_a_single_line_wider_than_a_page_still_advances(self):
        wide = os.path.join(self.tmp.name, "wide.txt")
        with open(wide, "w", encoding="utf-8") as fh:
            fh.write("x" * 50_000 + "\n" + "y" * 50_000 + "\n")
        self.assertEqual([[1, 1], [2, 2]],
                         file_pager.build_page_table(wide, 8_000))

    def test_page_size_follows_context_headroom_within_bounds(self):
        self.assertEqual(file_pager.PAGE_MIN_CHARS, file_pager.page_chars_for(1_000))
        self.assertEqual(file_pager.PAGE_MAX_CHARS, file_pager.page_chars_for(10_000_000))
        self.assertEqual(file_pager.PAGE_DEFAULT_CHARS, file_pager.page_chars_for(0))


class IndexTests(unittest.TestCase):
    """The index is what an evicted page leaves behind for a later edit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_python_index_names_defs_and_classes_with_real_line_numbers(self):
        path = self._write("m.py", "import os\n\n\ndef alpha():\n    pass\n\n\nclass Beta:\n    def gamma(self):\n        pass\n")
        self.assertEqual(["def alpha 4", "class Beta 8", "def gamma 9"],
                         file_pager.index_entries(path, 1, 20))

    def test_index_is_restricted_to_the_page(self):
        path = self._write("m.py", "def alpha():\n    pass\n\n\ndef beta():\n    pass\n")
        self.assertEqual(["def beta 5"], file_pager.index_entries(path, 3, 6))

    def test_unparsable_python_falls_back_instead_of_returning_nothing(self):
        path = self._write("broken.py", "def alpha(:\n    pass\n")
        self.assertEqual([], file_pager.index_entries(path, 1, 2))

    def test_typescript_and_markdown_get_an_index_too(self):
        ts = self._write("a.ts", "export function alpha() {}\nclass Beta {}\ninterface Gamma {}\n")
        self.assertEqual(["fn alpha 1", "cls Beta 2", "iface Gamma 3"],
                         file_pager.index_entries(ts, 1, 3))
        md = self._write("a.md", "# Title\ntext\n## Section\n")
        self.assertEqual(["# Title 1", "## Section 3"],
                         file_pager.index_entries(md, 1, 3))

    def test_a_stub_carries_range_index_and_note(self):
        stub = file_pager.render_stub("/x/m.py", 2, 5, 100, 200,
                                      ["def alpha 120"], "the retry lives here")
        self.assertIn("page 2/5", stub)
        self.assertIn("lines 100-200", stub)
        self.assertIn("def alpha 120", stub)
        self.assertIn("the retry lives here", stub)
        self.assertIn("page=2", stub)           # how to get it back


class PagedReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "module.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_py_source(60))
        self.state = {"_ctx_headroom_chars": 30_000}
        self.ctx = tools.ToolCtx(cwd=self.tmp.name, agent_id="a1",
                                 state=self.state)

    def read(self, **params):
        params.setdefault("path", self.path)
        return tools._bi_fs_read(params, self.ctx)

    def entry(self):
        return self.state["_pager"][self.path]

    def test_a_bare_read_opens_page_one_and_reports_the_page_count(self):
        r = self.read()
        self.assertEqual(1, r["page"])
        self.assertGreater(r["pages"], 1)
        self.assertEqual(1, r["offset"])
        rendered = agent_loop._format_tool_result_for_loop("fs.read", r, 3000)
        self.assertIn(f"page 1/{r['pages']}", rendered)
        self.assertIn("page='next'", rendered)

    def test_next_advances_and_prev_goes_back(self):
        self.read()
        self.assertEqual(2, self.read(page="next")["page"])
        self.assertEqual(3, self.read(page="next")["page"])
        self.assertEqual(2, self.read(page="prev")["page"])
        self.assertEqual(1, self.read(page="first")["page"])

    def test_next_on_the_last_page_stays_put_rather_than_erroring(self):
        last = self.read(page="last")
        self.assertEqual(last["pages"], last["page"])
        self.assertEqual(last["pages"], self.read(page="next")["page"])
        self.assertIn("last page",
                      agent_loop._format_tool_result_for_loop("fs.read", last, 3000))

    def test_pages_are_contiguous_and_cover_the_file(self):
        seen = []
        page = self.read()
        while True:
            seen.append((page["offset"], page["offset"] + page["lines_returned"] - 1))
            if page["page"] >= page["pages"]:
                break
            page = self.read(page="next")
        self.assertEqual(1, seen[0][0])
        for (_a, b), (c, _d) in zip(seen, seen[1:]):
            self.assertEqual(b + 1, c)

    def test_a_whole_page_is_delivered_not_cut_by_the_generic_budget(self):
        """The page is sized against real headroom; the loop's generic 24k
        per-result budget would otherwise hand back a page the reader cannot
        finish, and the two sizings would contradict each other."""
        roomy = tools.ToolCtx(cwd=self.tmp.name, agent_id="roomy",
                              state={"_ctx_headroom_chars": 400_000})
        r = tools._bi_fs_read({"path": self.path}, roomy)
        rendered = agent_loop._format_tool_result_for_loop("fs.read", r, 3000)
        self.assertNotIn("NOT shown", rendered)
        self.assertGreater(len(rendered), 24_000)
        # Same file, less headroom -> more, smaller pages: the page follows the
        # room available at the moment the file is opened.
        cramped = tools._bi_fs_read(
            {"path": self.path},
            tools.ToolCtx(cwd=self.tmp.name, agent_id="cramped",
                          state={"_ctx_headroom_chars": 30_000}))
        self.assertGreater(cramped["pages"], r["pages"])

    def test_offset_or_limit_is_a_targeted_window_and_does_not_page(self):
        self.read()
        before = dict(self.entry())
        r = self.read(offset=40, limit=10)
        self.assertNotIn("page", r)
        self.assertEqual(10, r["lines_returned"])
        self.assertEqual(before["page"], self.entry()["page"])

    def test_passing_a_window_and_a_page_says_which_one_won(self):
        r = self.read(offset=40, limit=10, page=3)
        self.assertIn("page ignored", r["note"])

    def test_a_note_is_attached_to_the_page_being_left(self):
        self.read()
        self.read(page="next", note="page 1 is the parser")
        stub = file_pager.stub_for(self.state, self.path, 1)
        self.assertIn("page 1 is the parser", stub)
        self.assertIn("def function_000 3", stub)

    def test_turning_without_a_note_says_the_page_was_dropped_uncommented(self):
        self.read()
        r = self.read(page="next")
        self.assertIn("dropped from your context", r["note"])
        self.assertIn("no summary", r["note"])

    def test_repeated_delivery_of_one_page_is_eventually_called_out(self):
        for _ in range(file_pager.REPEAT_STOP):
            r = self.read(page=1)
        self.assertIn("delivered", r["note"])
        self.assertIn("not in re-reading", r["note"])

    def test_editing_the_file_repages_and_says_so(self):
        self.read()
        self.read(page="next")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("\n\ndef extra():\n    pass\n")
        r = self.read()
        self.assertEqual(1, r["page"])
        self.assertIn("pages recomputed", r["note"])

    def test_cursors_are_per_agent_but_the_page_table_is_not_shared_state(self):
        self.read()
        self.read(page="next")
        other_state = {"_ctx_headroom_chars": 30_000}
        other = tools.ToolCtx(cwd=self.tmp.name, agent_id="a2", state=other_state)
        r = tools._bi_fs_read({"path": self.path}, other)
        self.assertEqual(1, r["page"])
        self.assertEqual(2, self.entry()["page"])
        self.assertEqual(self.entry()["pages"],
                         other_state["_pager"][self.path]["pages"])

    def test_an_empty_file_falls_back_to_the_plain_window(self):
        empty = os.path.join(self.tmp.name, "empty.py")
        open(empty, "w").close()
        r = tools._bi_fs_read({"path": empty}, self.ctx)
        self.assertTrue(r["ok"])
        self.assertNotIn("page", r)


class ProjectionTests(unittest.TestCase):
    """What the model is SENT is one page per file; the durable thread is whole."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "module.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_py_source(60))
        self.state = {"_ctx_headroom_chars": 30_000, "_pager_msgs": {}}
        self.ctx = tools.ToolCtx(cwd=self.tmp.name, agent_id="a1",
                                 state=self.state)
        self.thread = [{"role": "user", "content": "review it"}]
        self.calls = 0

    def read(self, **params):
        params.setdefault("path", self.path)
        result = tools._bi_fs_read(params, self.ctx)
        self.calls += 1
        call_id = f"call_{self.calls}"
        if result.get("_read_ref"):
            self.state["_pager_msgs"][call_id] = result.pop("_read_ref")
        self.thread.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": "read", "arguments": "{}"}}]})
        self.thread.append({
            "role": "tool", "tool_call_id": call_id,
            "content": agent_loop._format_tool_result_for_loop(
                "fs.read", result, 3000)})
        return result

    def _tool_contents(self, messages):
        return [m["content"] for m in messages if m.get("role") == "tool"]

    def test_the_page_left_behind_is_replaced_by_its_stub(self):
        self.read()
        self.read(page="next", note="page 1 is the parser")
        sent = agent_loop._project_paged_reads(self.thread, self.state)
        first, second = self._tool_contents(sent)
        self.assertIn("dropped from context", first)
        self.assertIn("page 1 is the parser", first)
        self.assertIn("def function_000 3", first)
        self.assertLess(len(first), 1_500)
        self.assertGreater(len(second), 10_000)   # the live page is untouched

    def test_the_durable_thread_keeps_every_page_verbatim(self):
        self.read()
        self.read(page="next")
        before = list(self._tool_contents(self.thread))
        agent_loop._project_paged_reads(self.thread, self.state)
        self.assertEqual(before, self._tool_contents(self.thread))
        self.assertGreater(len(before[0]), 10_000)

    def test_context_cost_does_not_grow_as_the_reader_pages_through(self):
        self.read()
        sizes = []
        for _ in range(4):
            self.read(page="next", note="a summary")
            sent = agent_loop._project_paged_reads(self.thread, self.state)
            sizes.append(sum(len(m.get("content") or "") for m in sent))
        self.assertLess(max(sizes) - min(sizes), max(sizes) * 0.25)

    def test_a_pinned_page_survives_the_turn(self):
        self.read(pin=True)
        self.read(page="next")
        first, _second = self._tool_contents(
            agent_loop._project_paged_reads(self.thread, self.state))
        self.assertGreater(len(first), 10_000)

    def test_the_pin_budget_is_enforced_and_reported(self):
        self.read(pin=True)
        self.read(page=2, pin=True)
        r = self.read(page=3, pin=True)
        self.assertIn("unpinned", r["note"])
        self.assertEqual(file_pager.MAX_PINS,
                         len(self.state["_pager"][self.path]["pins"]))

    def test_a_targeted_window_is_never_evicted(self):
        self.read(offset=40, limit=10)
        self.read()
        self.read(page="next")
        window = self._tool_contents(
            agent_loop._project_paged_reads(self.thread, self.state))[0]
        self.assertNotIn("dropped from context", window)

    def test_a_thread_with_nothing_to_evict_is_returned_unchanged(self):
        self.assertIs(self.thread,
                      agent_loop._project_paged_reads(self.thread, {}))

    def test_the_cursor_survives_a_repl_turn_boundary(self):
        self.read()
        self.read(page="next")
        carried = agent_loop.prepare_state_for_repl(self.state)
        self.assertEqual(2, carried["_pager"][self.path]["page"])
        self.assertTrue(carried["_pager_msgs"])


class LoopIntegrationTests(unittest.TestCase):
    """Eviction through the REAL loop, not a hand-built thread.

    The first version of this feature passed every unit test and evicted
    nothing in production: the page map was keyed by the DISPATCH call id while
    the thread carries ids minted later during assembly, so the projection
    never matched a single message. Only a test that drives run_agent_loop
    could see that, which is why this one exists.
    """

    def setUp(self):
        import agent_persistence
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent_persistence = agent_persistence

    def _run(self, calls):
        """Drive the loop through `calls` (one tool call per turn), then stop."""
        from unittest import mock
        import io
        from rich.console import Console
        from rich.markdown import Markdown

        turns = list(calls)

        def backend(**kwargs):
            self.sent.append(kwargs.get("messages") or [])
            if turns:
                args = turns.pop(0)
                return {"reply": "", "done": False, "error": False,
                        "finish_reason": "tool_calls",
                        "tool_calls": [{"name": "fs.read", "arguments": args}]}
            return {"reply": "read it", "tool_calls": [], "done": True,
                    "error": False, "finish_reason": "stop"}

        deps = agent_loop.LoopDeps(
            read_file=lambda path: None,
            append_file=lambda path, content: None,
            write_file=lambda path, content: None,
            strip_ansi=lambda text: text,
            generate_prompt=lambda: "You are a test agent.",
            call_backend=backend,
            SubTerminalSession=mock.Mock,
            display_command_output=lambda *a, **k: None,
            display_sub_terminal_preview=lambda *a, **k: None,
            display_file_diff=lambda *a, **k: None,
            console=Console(file=io.StringIO(), force_terminal=False),
            Markdown=Markdown,
        )
        self.sent = []
        old = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            os.makedirs(".laintas", exist_ok=True)
            with open("big.py", "w", encoding="utf-8") as fh:
                fh.write(_py_source(260))
            agent_loop.set_runtime_config("paged_reads", True)
            with mock.patch.object(self.agent_persistence, "AGENTS_DIR",
                                   os.path.join(self.tmp.name, "agents")):
                self.result = agent_loop.run_agent_loop(
                    deps, "read big.py", {}, {}, [], max_loops_override=8)
                return self.result
        finally:
            os.chdir(old)

    def _read_messages(self, payload):
        return [m for m in payload if m.get("role") == "tool"]

    def test_re_reading_what_is_still_on_screen_is_declined(self):
        """The lines are in the transcript; serving them twice buys nothing.

        Helpwo refuses this too, but from a ledger of what was READ. Here the
        refusal is conditioned on what is still VISIBLE, which is why it cannot
        outlive the content the way its does.
        """
        self._run([{"path": "big.py", "page": "last"},
                   {"path": "big.py", "page": "last"}])
        final = self._read_messages(self.sent[-1])
        bodies = [m for m in final if "\u2192def function_" in m["content"]]
        self.assertEqual(1, len(bodies), "the page was delivered twice")
        declines = [m for m in final if "already in your context" in m["content"]]
        self.assertEqual(1, len(declines))
        self.assertIn("[action needed]", declines[0]["content"])

    def test_an_evicted_page_can_always_be_read_again(self):
        """The refusal must never outlive the content it refers to."""
        self._run([{"path": "big.py", "page": 1},
                   {"path": "big.py", "page": 2},
                   {"path": "big.py", "page": 1}])
        final = self._read_messages(self.sent[-1])
        self.assertNotIn("already in your context",
                         "\n".join(m["content"] for m in final))
        bodies = [m for m in final if "\u2192def function_" in m["content"]]
        self.assertEqual(1, len(bodies))   # page 1 came back, page 2 evicted

    def test_turning_a_page_evicts_the_previous_one_from_what_is_sent(self):
        self._run([{"path": "big.py"},
                   {"path": "big.py", "page": "next", "note": "page 1 is setup"},
                   {"path": "big.py", "page": "next"}])
        final = self._read_messages(self.sent[-1])
        self.assertGreaterEqual(len(final), 3)
        stubs = [m["content"] for m in final if "dropped from context" in m["content"]]
        self.assertEqual(len(final) - 1, len(stubs), "only the live page stays whole")
        self.assertTrue(any("page 1 is setup" in s for s in stubs))
        self.assertTrue(any("def function_000" in s for s in stubs))

    def test_context_stops_growing_once_the_reader_is_paging(self):
        self._run([{"path": "big.py"},
                   {"path": "big.py", "page": "next"},
                   {"path": "big.py", "page": "next"},
                   {"path": "big.py", "page": "next"}])
        sizes = [sum(len(m.get("content") or "") for m in payload)
                 for payload in self.sent[2:]]
        self.assertGreater(sizes[0], 10_000)
        self.assertLess(max(sizes), sizes[0] * 1.5,
                        f"context grew while paging: {sizes}")


class VisibilityGateTests(unittest.TestCase):
    """Refuse only what the model can still see (merged from Helpwo)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "module.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_py_source(60))
        self.state = {"_ctx_headroom_chars": 30_000}
        self.ctx = tools.ToolCtx(cwd=self.tmp.name, agent_id="a1",
                                 state=self.state)

    def read(self, **params):
        params.setdefault("path", self.path)
        return tools._bi_fs_read(params, self.ctx)

    def _visible(self, *spans):
        self.state["_visible_reads"] = {self.path: [list(s) for s in spans]}

    def test_a_window_inside_visible_lines_is_declined(self):
        self._visible((1, 500))
        r = self.read(offset=100, limit=50)
        self.assertFalse(r["ok"])
        self.assertTrue(r["_advisory"])
        self.assertIn("already in your context", r["error"])

    def test_a_window_only_partly_visible_is_served(self):
        self._visible((1, 500))
        r = self.read(offset=450, limit=200)
        self.assertTrue(r["ok"])
        self.assertEqual(200, r["lines_returned"])

    def test_nothing_visible_means_nothing_is_declined(self):
        r = self.read(offset=100, limit=50)
        self.assertTrue(r["ok"])

    def test_editing_the_file_reopens_it_for_reading(self):
        """Line numbers move under an edit, so old coverage must not block."""
        self._visible((1, 500))
        import file_pager
        self.read()                       # register the file with the pager
        file_pager.mark_edited(self.state, self.path)
        self.assertTrue(self.read(offset=100, limit=50)["ok"])
        self.assertEqual([self.path], file_pager.stale_files(self.state))

    def test_a_pin_is_never_declined(self):
        self._visible((1, 5_000))
        self.assertTrue(self.read(page=1, pin=True)["ok"])

    def test_the_gate_has_a_kill_switch(self):
        self._visible((1, 500))
        agent_loop.set_runtime_config("read_block_visible", False)
        self.addCleanup(agent_loop.set_runtime_config, "read_block_visible", True)
        self.assertTrue(self.read(offset=100, limit=50)["ok"])

    def test_pruned_content_does_not_count_as_visible(self):
        """The failure mode Helpwo's gate has: refusing content compaction ate.

        A tool message truncated by compaction is no longer a copy of what it
        delivered, so the lines it held stop counting and the read is served.
        """
        state = {"_pager_msgs": {"c1": {"path": self.path, "page": 0,
                                        "lines": [1, 500]}}}
        thread = [{"role": "tool", "tool_call_id": "c1",
                   "content": "1\u2192x\n[truncated 40000 chars for compaction]"}]
        agent_loop._project_paged_reads(thread, state)
        self.assertEqual({}, state["_visible_reads"])

        whole = [{"role": "tool", "tool_call_id": "c1", "content": "1\u2192x"}]
        agent_loop._project_paged_reads(whole, state)
        self.assertEqual({self.path: [[1, 500]]}, state["_visible_reads"])


class BodyCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "module.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_py_source(60))
        file_pager._BODY_CACHE.clear()
        self.addCleanup(file_pager._BODY_CACHE.clear)

    def _ctx(self, agent):
        return tools.ToolCtx(cwd=self.tmp.name, agent_id=agent,
                             state={"_ctx_headroom_chars": 30_000})

    def test_a_second_agent_gets_the_cached_body_byte_for_byte(self):
        first = tools._bi_fs_read({"path": self.path}, self._ctx("a1"))
        second = tools._bi_fs_read({"path": self.path}, self._ctx("a2"))
        self.assertTrue(second.get("cached_view"))
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(first["lines_returned"], second["lines_returned"])
        self.assertIn("served from cache", second["note"])

    def test_the_cache_is_keyed_by_file_version(self):
        tools._bi_fs_read({"path": self.path}, self._ctx("a1"))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("\n\ndef extra():\n    pass\n")
        again = tools._bi_fs_read({"path": self.path}, self._ctx("a2"))
        self.assertFalse(again.get("cached_view"),
                         "a changed file must not be served from the cache")
        tail = tools._bi_fs_read({"path": self.path, "page": "last"},
                                 self._ctx("a3"))
        self.assertIn("def extra", tail["result"])

    def test_the_cache_never_reaches_the_persisted_session_state(self):
        ctx = self._ctx("a1")
        tools._bi_fs_read({"path": self.path}, ctx)
        carried = agent_loop.prepare_state_for_repl(ctx.state)
        blob = json.dumps(carried, default=str)
        self.assertNotIn("padding to give the page real width", blob)


class HandRolledPagingTests(unittest.TestCase):
    """A window that resumes where the last one ended is a page turn by hand.

    Measured in a live session hours after paging shipped: 49 of 61 reads were
    still hand-rolled windows, and on one file 5 of 6 consecutive windows
    started exactly where the previous ended. The tool description alone did
    not change the habit; this names it, in the result, while the caller is
    doing it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "module.py")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_py_source(60))
        self.state = {"_ctx_headroom_chars": 30_000}
        self.ctx = tools.ToolCtx(cwd=self.tmp.name, agent_id="walker",
                                 state=self.state)

    def read(self, **params):
        params.setdefault("path", self.path)
        return tools._bi_fs_read(params, self.ctx)

    def test_a_walk_is_named_and_the_page_that_covers_it_is_offered(self):
        self.read(offset=1, limit=100)
        self.read(offset=101, limit=100)
        third = self.read(offset=201, limit=100)
        self.assertIn("hand-rolled pages", third["note"])
        self.assertRegex(third["note"], r"page=\d+\) covers lines \d+-\d+")

    def test_two_windows_are_not_yet_a_walk(self):
        self.read(offset=1, limit=100)
        self.assertNotIn("note", self.read(offset=101, limit=100))

    def test_a_targeted_read_elsewhere_is_never_called_a_walk(self):
        self.read(offset=1, limit=100)
        self.read(offset=101, limit=100)
        self.read(offset=900, limit=20)          # jumped: not a continuation
        self.assertNotIn("note", self.read(offset=1500, limit=20))

    def test_the_streak_restarts_after_a_jump(self):
        """A jump ends the walk; the next two windows are innocent again."""
        for start in (1, 101, 201):
            self.read(offset=start, limit=100)
        self.assertNotIn("note", self.read(offset=1200, limit=30))
        self.assertNotIn("note", self.read(offset=1230, limit=30))
        # ...and a third consecutive one is a new walk, correctly.
        self.assertIn("hand-rolled", self.read(offset=1260, limit=30)["note"])

    def test_paged_reads_are_not_flagged_as_hand_rolled(self):
        self.read()
        second = self.read(page="next")
        self.assertNotIn("hand-rolled", second.get("note", ""))


if __name__ == "__main__":
    unittest.main()
