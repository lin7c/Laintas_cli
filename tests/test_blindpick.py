"""Regression tests for the blindpick extension.

Covers the parts that were silently wrong rather than loudly broken: an
unset challenger rendering as a real model, a verdict that never reached
the ledger, a file count grepped out of the patch body, and `pick` acting
on whichever round happened to be oldest.
"""

import importlib.util
import json
import threading
import time
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXT_DIR = Path(__file__).resolve().parent.parent / "extensions" / "blindpick"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"blindpick_{name}_test", str(EXT_DIR / f"{name}.py"),
        submodule_search_locations=[str(EXT_DIR)])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    """Minimal extension context: a console that records printed lines."""

    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.lines: list[str] = []
        console = self

        class _Console:
            def print(self, text="", **_kwargs):
                console.lines.append(str(text))

        self.console = _Console()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class BlindpickTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.main = _load("main")
        self.ctx = _Ctx(self.cwd)
        self.main._ctx = self.ctx
        # ~/.laintas/blindpick.json can point the ledger at a machine-wide
        # directory; tests must never write into the operator's real data.
        self.main._store().GLOBAL_CONFIG = self.cwd / "no-such-config.json"

    def _round(self, **fields):
        row = {"round_id": "r" * 12, "status": "pending", "task": "t",
               "incumbent_model": "model-inc", "challenger_model": "model-ch"}
        row.update(fields)
        self.main._save_rounds(self.main._load_rounds() + [row])
        return row


class LabelTests(BlindpickTestCase):
    def test_unset_challenger_is_empty_not_auto_routing(self):
        # _model_label("") is "auto-routing", which is truthy — status used to
        # report a challenger that had never been chosen.
        self.main._state = {"challenger": "", "challenger_provider": ""}
        self.assertEqual("", self.main._challenger_label())
        self.main._state = {"challenger": "gpt-x", "challenger_provider": ""}
        self.assertEqual("gpt-x", self.main._challenger_label())

    def test_status_says_unset_when_no_challenger(self):
        self.main._state = {"challenger": "", "challenger_provider": ""}
        self.main._incumbent = lambda: ("auto-routing", "", "")
        self.main._cmd_status()
        self.assertIn("未设置", self.ctx.text)


class DiffRenderTests(BlindpickTestCase):
    DIFF = (
        "diff --git a/one.py b/one.py\n"
        "--- a/one.py\n+++ b/one.py\n@@ -1 +1,2 @@\n"
        " keep\n+added\n"
        "diff --git a/two.py b/two.py\n"
        "--- a/two.py\n+++ b/two.py\n@@ -1,2 +1 @@\n"
        "-gone\n+++ literal plus line\n"
    )

    def test_split_uses_the_post_image_path(self):
        parts = self.main._split_diff_files(self.DIFF)
        self.assertEqual(["one.py", "two.py"], [p for p, _ in parts])
        self.assertTrue(parts[0][1].startswith("diff --git a/one.py"))
        self.assertIn("literal plus line", parts[1][1])

    def test_stat_counts_files_by_header_not_by_plus_lines(self):
        files, adds, dels = self.main._diff_stat(self.DIFF)
        # Two files — the "+++ literal plus line" content line must not count
        # as a third; that grep was the old file counter in _apply.
        self.assertEqual(2, files)
        self.assertEqual(2, adds)
        self.assertEqual(1, dels)

    def test_empty_diff_is_reported_not_rendered(self):
        row = self._round(incumbent_diff="")
        self.main._render_side("【A】", row, "incumbent")
        self.assertIn("没有产生任何改动", self.ctx.text)


class RoundResolutionTests(BlindpickTestCase):
    def test_single_pending_round_needs_no_id(self):
        row = self._round(round_id="a" * 12)
        self.assertEqual(row["round_id"],
                         self.main._resolve_round()["round_id"])

    def test_several_pending_rounds_refuse_to_guess(self):
        self._round(round_id="a" * 12)
        self._round(round_id="b" * 12)
        self.assertIsNone(self.main._resolve_round())
        self.assertIn("请指定对局 id", self.ctx.text)

    def test_id_prefix_selects_the_round(self):
        self._round(round_id="a" * 12)
        self._round(round_id="b" * 12)
        chosen = self.main._resolve_round("bbb")
        self.assertEqual("b" * 12, chosen["round_id"])

    def test_unknown_id_is_rejected(self):
        self._round(round_id="a" * 12)
        self.assertIsNone(self.main._resolve_round("zzz"))
        self.assertIn("没有", self.ctx.text)


class LedgerTests(BlindpickTestCase):
    """The store existed from v1 but nothing ever wrote to it."""

    def _finished_round(self):
        return self._round(
            incumbent_diff="diff --git a/x b/x\n+inc\n",
            challenger_diff="diff --git a/x b/x\n+chl\n",
            incumbent_reply="did it", challenger_reply="also did it",
            incumbent_run_id="run-inc", challenger_run_id="run-chl")

    def test_match_then_vote_produces_a_rating(self):
        row = self._finished_round()
        self.main._record_match(row)
        row = self.main._load_rounds_by_id(row["round_id"])
        self.assertTrue(row.get("match_id"), "match was never recorded")
        self.main._record_vote(row, "right", ["incumbent", "challenger"])

        table = self.main._store().ratings(self.main._project_dir())
        self.assertEqual({"model-inc", "model-ch"}, set(table))
        self.assertGreater(table["model-ch"]["rating"],
                           table["model-inc"]["rating"])
        bias = self.main._store().position_bias(self.main._project_dir())
        self.assertEqual(1, bias["decisive"])
        self.assertEqual(0, bias["first_won"])  # the second-shown side won

    def test_match_is_recorded_once(self):
        row = self._finished_round()
        self.main._record_match(row)
        first = self.main._load_rounds_by_id(row["round_id"])["match_id"]
        self.main._record_match(self.main._load_rounds_by_id(row["round_id"]))
        self.assertEqual(
            first, self.main._load_rounds_by_id(row["round_id"])["match_id"])

    def test_export_writes_dpo_pairs_with_run_ids(self):
        row = self._finished_round()
        self.main._record_match(row)
        row = self.main._load_rounds_by_id(row["round_id"])
        self.main._record_vote(row, "left", ["challenger", "incumbent"])
        out = self.cwd / "prefs.jsonl"
        written = self.main._store().export_preferences(
            self.main._project_dir(), out)
        self.assertEqual(1, written)
        pair = json.loads(out.read_text().splitlines()[0])
        self.assertEqual("model-inc", pair["chosen_model"])
        self.assertEqual("run-chl", pair["rejected_run_id"])

    def test_ties_are_recorded_but_not_exported(self):
        row = self._finished_round()
        self.main._record_match(row)
        row = self.main._load_rounds_by_id(row["round_id"])
        self.main._record_vote(row, "tie", ["incumbent", "challenger"])
        out = self.cwd / "prefs.jsonl"
        self.assertEqual(0, self.main._store().export_preferences(
            self.main._project_dir(), out))
        table = self.main._store().ratings(self.main._project_dir())
        self.assertEqual(1, table["model-inc"]["ties"])


class OutputSinkTests(BlindpickTestCase):
    """The round worker prints from its own thread; the workspace owns the
    screen. Without a sink that write lands on the alternate screen."""

    def test_sink_swap_captures_and_restores(self):
        sink: list[str] = []
        self.assertIsNone(self.main.capture_output(sink))
        self.main._say("[green]done[/green]")
        self.main._say_raw("raw [x]")
        self.assertEqual(["[green]done[/green]", "raw [x]"], sink)
        self.assertIs(sink, self.main.capture_output(None))
        self.main._say("to console")
        self.assertEqual(2, len(sink))
        self.assertIn("to console", self.ctx.text)

    def test_background_writes_reach_the_workspace_sink(self):
        import threading
        sink: list[str] = []
        self.main.capture_output(sink)
        self.addCleanup(self.main.capture_output, None)
        worker = threading.Thread(target=self.main._say, args=("从后台线程",))
        worker.start()
        worker.join(5)
        self.assertEqual(["从后台线程"], sink)
        self.assertEqual("", self.ctx.text)


class ArenaRenderTests(BlindpickTestCase):
    """The arena renders round data as plain fragments; the pure parts."""

    def setUp(self):
        super().setUp()
        self.ui = _load("ui")

    DIFF = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "new file mode 100644\n"
        "index 0000000..abc1234\n"
        "--- /dev/null\n+++ b/src/foo.py\n"
        "@@ -0,0 +1,2 @@\n+one\n+two\n"
    )

    def _row(self, **fields):
        row = self._round(**fields)
        return row

    def test_crop_never_exceeds_its_budget(self):
        for width in (1, 2, 3, 4, 12, 30):
            for text in ("abcdefghijkl", "中文中文中文中文", "a中b中c中d中"):
                cropped = self.ui._crop(text, width)
                self.assertLessEqual(self.ui._disp_len(cropped), width)

    def test_wrap_fits_the_width_and_keeps_every_character(self):
        for text in ("短", "the quick brown fox jumps over the lazy dog",
                     "把这段中文折行不能丢字也不能超宽" * 3):
            for width in (8, 17, 40):
                rows = self.ui._wrap(text, width)
                for row in rows:
                    self.assertLessEqual(self.ui._disp_len(row), width)
                self.assertEqual(text.replace(" ", ""),
                                 "".join(rows).replace(" ", ""))

    def test_diff_stream_drops_git_headers_and_keeps_hunks(self):
        row = self._row(incumbent_diff=self.DIFF)
        text = "\n".join(t for _s, t in self.ui._diff_stream(
            self.main, row, "incumbent", 60))
        self.assertIn("src/foo.py", text)
        self.assertIn("+one", text)
        self.assertIn("@@ -0,0 +1,2 @@", text)
        for noise in ("new file mode", "index 0000000", "+++ b/", "--- /dev"):
            self.assertNotIn(noise, text)

    def test_diff_stream_marks_a_metadata_only_change(self):
        row = self._row(incumbent_diff=(
            "diff --git a/x.png b/x.png\n"
            "Binary files a/x.png and b/x.png differ\n"))
        text = "\n".join(t for _s, t in self.ui._diff_stream(
            self.main, row, "incumbent", 60))
        self.assertIn("二进制", text)

    def test_diff_stream_respects_the_cap(self):
        big = ("diff --git a/b.py b/b.py\n@@ -1,600 +1,600 @@\n"
               + "".join(f"+line{i}\n" for i in range(600)))
        row = self._row(incumbent_diff=big)
        lines = self.ui._diff_stream(self.main, row, "incumbent", 60)
        self.assertLessEqual(len(lines), self.ui.DIFF_CAP + 2)
        self.assertIn("截断", "\n".join(t for _s, t in lines))

    def test_control_characters_never_reach_the_screen(self):
        row = self._row(incumbent_diff=(
            "diff --git a/x b/x\n@@ -1 +1 @@\n+\x1b[31mred\x07\n"))
        text = "".join(t for _s, t in self.ui._diff_stream(
            self.main, row, "incumbent", 60))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)


class ArenaLiveTests(BlindpickTestCase):
    """The live half: both sides stream from the shared UI event hub."""

    def setUp(self):
        super().setUp()
        self.ui = _load("ui")
        import agent_ui_events
        self.hub = agent_ui_events.hub
        self.addCleanup(self.hub.reset)

    def _emit(self, agent_id, kind, **fields):
        self.hub.emit(kind, agent_id=agent_id, **fields)

    def test_stream_renders_assistant_text_tools_and_errors(self):
        self._emit("child-A", "ai_stream", detail="正在读取 ")
        self._emit("child-A", "ai_stream", detail="app.py")
        self._emit("child-A", "ai_end")
        self._emit("child-A", "tool_started", summary="fs.read app.py",
                   tool_call_id="t1")
        self._emit("child-A", "tool_finished", summary="fs.read app.py 2.1kB",
                   tool_call_id="t1")
        self._emit("child-A", "agent_error", summary="boom")
        lines = self.ui._side_stream("child-A", 40)
        text = "\n".join(t for _s, t in lines)
        self.assertIn("正在读取 app.py", text)
        # The finished call replaces its own started line, never stacks.
        self.assertEqual(1, text.count("fs.read app.py"))
        self.assertIn("2.1kB", text)
        self.assertIn("boom", text)

    def test_stream_wraps_to_the_column_width(self):
        self._emit("child-A", "ai", detail="很长的一段中文输出" * 12)
        for _style, text in self.ui._side_stream("child-A", 30):
            self.assertLessEqual(self.ui._disp_len(text), 30)

    def test_stream_is_empty_before_the_child_exists(self):
        self.assertIn("等待启动", self.ui._side_stream("", 40)[0][1])

    def test_arena_never_shows_a_model_name_before_the_verdict(self):
        row = self._round(
            status="running", display_order=["challenger", "incumbent"],
            incumbent_child="child-A", challenger_child="child-B",
            incumbent_model="secret-inc", challenger_model="secret-chl")
        self._emit("child-A", "ai", detail="hello")
        arena = self.ui.Arena.__new__(self.ui.Arena)
        arena.bp = self.main
        arena.rounds = [row]
        arena.index = 0
        arena.mode_override = {}
        arena.scroll = -1
        rendered = "".join(
            t for label in ("A", "B")
            for _s, t in ([arena.head_line(label, row, 40)]
                          + arena.pane_lines(label, 40)))
        self.assertNotIn("secret-inc", rendered)
        self.assertNotIn("secret-chl", rendered)
        self.assertIn("〔A〕", rendered)
        self.assertIn("〔B〕", rendered)

    def test_side_of_follows_the_order_fixed_at_creation(self):
        row = self._round(display_order=["challenger", "incumbent"])
        arena = self.ui.Arena.__new__(self.ui.Arena)
        arena.bp, arena.rounds, arena.index = self.main, [row], 0
        self.assertEqual("challenger", arena.side_of("A"))
        self.assertEqual("incumbent", arena.side_of("B"))


class ArenaShellTests(BlindpickTestCase):
    """Chrome, geometry, approvals and off-thread actions."""

    def setUp(self):
        super().setUp()
        self.ui = _load("ui")
        self.main._incumbent = lambda: ("model-inc", "model-inc", "")

    def _arena(self, cols: int = 100, rows: int = 30):
        import os
        for name, value in (("COLUMNS", str(cols)), ("LINES", str(rows))):
            old = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(
                lambda n=name, o=old: os.environ.__setitem__(n, o)
                if o is not None else os.environ.pop(n, None))
        return self.ui.Arena(self.main)

    def test_key_hints_fit_and_always_keep_the_exit_key(self):
        self._round(status="pending", task="判一个很长的任务描述 " * 4)
        for cols in (200, 100, 72, 50, 30):
            arena = self._arena(cols)
            width = sum(self.ui._disp_len(text)
                        for _style, text in arena.key_fragments())
            self.assertLessEqual(width, cols, f"key hints overflowed at {cols}")
            text = "".join(t for _s, t in arena.key_fragments())
            self.assertIn("退出", text)

    def test_header_has_no_dangling_separator(self):
        arena = self._arena()
        text = "".join(t for _s, t in arena.header_fragments())
        self.assertFalse(text.rstrip().endswith(self.ui._sym.BULLET))
        self.assertIn("空闲", text)

    def test_narrow_terminals_stack_instead_of_splitting(self):
        self.assertTrue(self._arena(cols=120).split())
        self.assertFalse(self._arena(cols=44).split())

    def test_columns_are_padded_to_exactly_one_column_wide(self):
        self._round(status="pending", incumbent_diff="", challenger_diff="")
        arena = self._arena(cols=120)
        width = arena.column_width()
        for label in ("A", "B"):
            rows = [t for style, t in arena.column_fragments(label)
                    if t != "\n"]
            self.assertTrue(rows)
            for text in rows:
                self.assertEqual(width, self.ui._disp_len(text))

    def test_approval_from_a_worker_thread_blocks_until_answered(self):
        arena = self._arena()
        answer = {}

        def _ask():
            answer["value"] = arena._request_approval(
                "child-A", "confirm", "写入沙箱？", "细节")
        worker = threading.Thread(target=_ask)
        worker.start()
        for _ in range(50):
            if arena.pending_approval() is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(arena.pending_approval(),
                             "approval never reached the UI")
        self.assertIn("写入沙箱", "".join(
            t for _s, t in arena.approval_fragments()))
        self.assertNotIn("value", answer, "request returned before an answer")
        arena.resolve_approval(True)
        worker.join(5)
        self.assertTrue(answer["value"])

    def test_closing_the_arena_denies_instead_of_hanging(self):
        arena = self._arena()
        answer = {}

        def _ask():
            answer["value"] = arena._request_approval("c", "confirm", "s", "d")
        worker = threading.Thread(target=_ask)
        worker.start()
        for _ in range(50):
            if arena.pending_approval() is not None:
                break
            time.sleep(0.02)
        arena.close_approvals()
        worker.join(5)
        self.assertFalse(answer["value"])
        self.assertIsNone(arena.pending_approval())

    def test_actions_run_off_the_ui_loop_and_report_back(self):
        arena = self._arena()
        started = threading.Event()
        release = threading.Event()

        def _slow():
            started.set()
            release.wait(5)
            self.main._say("[green]开局成功[/green]")

        arena.run_action("开局", _slow)
        self.assertTrue(started.wait(5), "action did not start")
        self.assertEqual("开局", arena.busy)     # the loop is not blocked
        release.set()
        for _ in range(100):
            if not arena.busy:
                break
            time.sleep(0.02)
        self.assertEqual("", arena.busy)
        self.assertEqual("开局成功", arena.notice)   # markup stripped

    def test_a_second_action_is_refused_while_one_runs(self):
        arena = self._arena()
        release = threading.Event()
        arena.run_action("开局", lambda: release.wait(5))
        for _ in range(50):
            if arena.busy:
                break
            time.sleep(0.02)
        arena.run_action("裁决", lambda: None)
        self.assertIn("稍等", arena.notice)
        release.set()


class ArenaKeyPathTests(BlindpickTestCase):
    """The path that used to do nothing: n, type a task, Enter, approve."""

    def setUp(self):
        super().setUp()
        self.ui = _load("ui")
        self.main._incumbent = lambda: ("model-inc", "model-inc", "")
        self.main._state = {"challenger": "model-chl",
                            "challenger_provider": ""}

    def _drive(self, keys, *, wait_for=None, timeout=6.0, register=False,
               then=None):
        """Run the real application over a pipe, feeding keys."""
        import os
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        for name, value in (("COLUMNS", "100"), ("LINES", "30")):
            os.environ[name] = value
        ctx = create_pipe_input()
        pipe = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        with create_app_session(input=pipe, output=DummyOutput()):
            arena = self.ui.Arena(self.main)
            self.addCleanup(arena.close_approvals)
            if register:
                # What run_ui does: makes this view the CLI's approval sink.
                import laintas_cli
                laintas_cli._enter_agents_view(arena)
                self.addCleanup(laintas_cli._exit_agents_view)

            def feed():
                time.sleep(0.4)
                for key in keys:
                    pipe.send_text(key)
                    time.sleep(0.25)
                deadline = time.time() + timeout
                while wait_for is not None and time.time() < deadline:
                    if wait_for(arena):
                        break
                    time.sleep(0.05)
                if then is not None:
                    then(arena, pipe)
                pipe.send_text("q")
            threading.Thread(target=feed, daemon=True).start()
            arena.run()
        return arena

    def test_typing_a_task_starts_a_round_without_leaving_the_screen(self):
        started = {}

        def _fake_run_round(task):
            started["task"] = task
            self.main._say("[green]对局已启动[/green]")
            return True
        self.main._run_round = _fake_run_round

        arena = self._drive(["n", "修一个 bug", "\r"],
                            wait_for=lambda a: bool(started))
        self.assertEqual("修一个 bug", started.get("task"),
                         "Enter in the composer never reached _run_round")
        # And the message it printed is on screen, not swallowed.
        self.assertIn("对局已启动", arena.notice)

    def test_an_empty_task_just_closes_the_composer(self):
        self.main._run_round = lambda task: self.fail("should not run")
        arena = self._drive(["n", "\r"])
        self.assertFalse(arena.composing)

    def test_the_sandbox_approval_lands_in_the_arena_and_y_answers_it(self):
        decision = {}
        seen = {}

        def _fake_run_round(task):
            import laintas_cli
            # Exactly what main._run_round does. The CLI routes the prompt to
            # whichever view owns the screen — which is why the round can now
            # start without the view stepping aside first.
            decision["choice"] = laintas_cli._blocking_approval_prompt(
                "blindpick", "两个模型将写入沙箱", "允许？", allow_always=True)
        self.main._run_round = _fake_run_round

        def _answer(arena, pipe):
            seen["bar"] = "".join(t for _s, t in arena.approval_fragments())
            seen["blocked"] = "choice" not in decision
            pipe.send_text("y")
            deadline = time.time() + 5
            while "choice" not in decision and time.time() < deadline:
                time.sleep(0.05)

        self._drive(["n", "任务", "\r"], then=_answer, register=True,
                    wait_for=lambda a: a.pending_approval() is not None)
        self.assertIn("两个模型将写入沙箱", seen.get("bar", ""),
                      "the approval never appeared in the arena")
        self.assertTrue(seen.get("blocked"),
                        "the prompt returned before the user answered")
        self.assertEqual("yes", decision.get("choice"))

    def test_judging_refuses_a_round_that_is_not_pending(self):
        self._round(status="running")
        arena = self.ui.Arena(self.main)
        arena.judge("a")
        self.assertIn("还不能裁决", arena.notice)

    def test_refresh_notices_the_worker_finishing_the_round(self):
        row = self._round(status="running")
        arena = self.ui.Arena(self.main)
        self.assertEqual("running", arena.current()["status"])
        self.assertFalse(arena.refresh(), "nothing changed yet")
        # What the round worker does when both children are done. Nothing
        # emits a UI event for this, so polling is the only way the arena
        # learns the round is judgeable.
        time.sleep(0.01)
        self.main._update_round(row["round_id"], status="pending")
        self.assertTrue(arena.refresh(), "arena missed the status change")
        self.assertEqual("pending", arena.current()["status"])

    def test_a_new_round_takes_the_selection(self):
        # Start on an older round, then have the worker create a new one:
        # the arena must follow it, not leave you reading history.
        old = self._round(round_id="o" * 12, status="both_bad")
        arena = self.ui.Arena(self.main)
        self.assertEqual("o" * 12, arena.round_id())
        time.sleep(0.01)
        self._round(round_id="n" * 12, status="running")
        self.assertTrue(arena.refresh())
        self.assertEqual("n" * 12, arena.round_id())

    def test_browsing_history_is_not_yanked_by_unrelated_writes(self):
        self._round(round_id="o" * 12, status="both_bad")
        self._round(round_id="p" * 12, status="pending")
        arena = self.ui.Arena(self.main)
        arena.index = arena.rounds.index(
            next(r for r in arena.rounds if r["round_id"] == "o" * 12))
        time.sleep(0.01)
        self.main._update_round("p" * 12, note="unrelated")
        arena.refresh()
        self.assertEqual("o" * 12, arena.round_id())

    def test_settled_rounds_open_on_the_reply_not_an_empty_live_pane(self):
        # The children are gone and the hub has forgotten their events, so
        # "live" for a settled round is a blank pane. For a read-only task
        # the closing message is the entire deliverable.
        self._round(round_id="s" * 12, status="applied",
                    applied_side="incumbent")
        arena = self.ui.Arena(self.main)
        self.assertEqual("reply", arena.mode())
        arena.mode_override[arena.round_id()] = "diff"
        self.assertEqual("diff", arena.mode())

    def test_the_reply_pane_renders_markdown_and_the_change_size(self):
        self._round(
            round_id="m" * 12, status="pending",
            display_order=["incumbent", "challenger"],
            incumbent_reply="# 结论\n\n这是 **laintas_cli** 项目。\n\n"
                            "- 一个 `CLI` 工具\n- 还有别的\n",
            incumbent_diff="diff --git a/x b/x\n@@ -1 +1 @@\n+one\n")
        arena = self.ui.Arena(self.main)
        row = arena.current()
        lines = arena._reply_lines(row, "incumbent", 40)
        styles = {style for style, _text in lines}
        text = "\n".join(t for _s, t in lines)
        self.assertIn("md.h", styles)
        self.assertIn("md.list", styles)
        self.assertIn("结论", text)
        # Emphasis markers are stripped, not left on screen unrendered.
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertIn("laintas_cli", text)
        self.assertIn("改动 1 个文件 +1 −0", text)

    def test_a_side_with_nothing_to_show_says_so(self):
        self._round(round_id="e" * 12, status="pending",
                    display_order=["incumbent", "challenger"])
        arena = self.ui.Arena(self.main)
        text = "\n".join(t for _s, t in arena._reply_lines(
            arena.current(), "incumbent", 40))
        self.assertIn("既没有改动，也没有留下说明", text)

    def test_markdown_never_exceeds_the_column(self):
        body = ("## 很长的标题" * 3 + "\n\n- " + "很长的条目" * 8
                + "\n\n```\n" + "x" * 200 + "\n```\n> " + "引用" * 30)
        for width in (24, 40, 70):
            for _style, text in self.ui._markdown_lines(body, width):
                self.assertLessEqual(self.ui._disp_len(text), width)

    def test_running_rounds_open_live(self):
        self._round(round_id="r" * 12, status="running")
        self.assertEqual("live", self.ui.Arena(self.main).mode())

    def test_a_sides_head_shows_its_own_work_not_the_rounds_verdict(self):
        row = self._round(status="both_bad",
                          display_order=["incumbent", "challenger"])
        arena = self.ui.Arena(self.main)
        head = arena.head_line("A", row, 40)[1]
        self.assertNotIn("都不行", head)
        self.assertIn("无改动", head)

    def test_the_applied_side_is_marked_after_the_verdict(self):
        row = self._round(status="applied", applied_side="incumbent",
                          display_order=["incumbent", "challenger"])
        arena = self.ui.Arena(self.main)
        self.assertIn("已采纳", arena.head_line("A", row, 40)[1])
        self.assertIn("未采纳", arena.head_line("B", row, 40)[1])

    def test_action_output_is_shown_in_full_not_just_its_first_line(self):
        arena = self.ui.Arena(self.main)
        arena.show_log([f"line {i}" for i in range(20)])
        self.assertEqual(arena.LOG_ROWS + 1, len(arena.reveal))
        self.assertIn("还有", arena.reveal[-1][1])


class RetentionTests(BlindpickTestCase):
    """Nothing this extension creates may outlive the record that names it."""

    def setUp(self):
        super().setUp()
        self.repo = self.cwd / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        (self.repo / "f.txt").write_text("one\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        # The sweep resolves the repo from the extension's cwd, the way it
        # does in a real session.
        self.ctx.cwd = str(self.repo)

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo,
                              capture_output=True, text=True, check=False)

    def _branch(self, name):
        self._git("branch", name)
        return name

    def _round_with_branches(self, round_id, status, age_days=0.0):
        prefix = f"laintas/blindpick-{round_id[:8]}"
        row = self._round(
            round_id=round_id, status=status,
            created_at=time.time() - age_days * 86400,
            repo_root=str(self.repo),
            incumbent_branch=self._branch(f"{prefix}-incumbent"),
            challenger_branch=self._branch(f"{prefix}-challenger"))
        return row

    def _branches(self):
        out = self._git("branch", "--list", "laintas/*").stdout
        return {line.strip(" *+") for line in out.splitlines() if line.strip()}

    def test_deleting_a_round_takes_its_branches_with_it(self):
        row = self._round_with_branches("a" * 12, "applied")
        self.assertEqual(2, len(self._branches()))
        deleted, leftovers = self.main._delete_rounds([row])
        self.assertEqual(1, deleted)
        self.assertEqual([], leftovers)
        self.assertEqual(set(), self._branches())
        self.assertEqual([], self.main._load_rounds())

    def test_retention_keeps_the_newest_and_erases_the_rest(self):
        for index in range(5):
            self._round_with_branches(f"{index}" * 12, "applied")
        deleted, _ = self.main._prune(keep=2, days=0)
        self.assertEqual(3, deleted)
        kept = [r["round_id"] for r in self.main._load_rounds()]
        self.assertEqual(["3" * 12, "4" * 12], kept)
        self.assertEqual(4, len(self._branches()))   # 2 rounds x 2 sides

    def test_retention_never_touches_running_or_pending_rounds(self):
        self._round_with_branches("r" * 12, "running", age_days=90)
        self._round_with_branches("p" * 12, "pending", age_days=90)
        self._round_with_branches("a" * 12, "applied", age_days=90)
        deleted, _ = self.main._prune(keep=1, days=1)
        self.assertEqual(1, deleted)
        self.assertEqual({"r" * 12, "p" * 12},
                         {r["round_id"] for r in self.main._load_rounds()})

    def test_age_limit_prunes_even_within_the_count_limit(self):
        self._round_with_branches("o" * 12, "tie", age_days=30)
        self._round_with_branches("n" * 12, "tie", age_days=1)
        deleted, _ = self.main._prune(keep=50, days=14)
        self.assertEqual(1, deleted)
        self.assertEqual(["n" * 12],
                         [r["round_id"] for r in self.main._load_rounds()])

    def test_zero_means_no_limit(self):
        for index in range(3):
            self._round_with_branches(f"{index}" * 12, "tie", age_days=999)
        self.assertEqual(0, self.main._prune(keep=0, days=0)[0])

    def test_orphan_branches_are_swept_but_only_orphans(self):
        # One round still owns its pair; one pair belongs to no record at all
        # (a killed CLI, or a version that predates the record format).
        self._round_with_branches("k" * 12, "pending")
        self._branch("laintas/blindpick-dead1234-incumbent")
        self._branch("laintas/blindpick-dead1234-challenger")
        self._branch("laintas/some-other-tool")
        self._branch("feature/mine")

        removed = self.main._sweep_orphan_branches(max_age=0)

        self.assertEqual({"laintas/blindpick-dead1234-incumbent",
                          "laintas/blindpick-dead1234-challenger"},
                         set(removed))
        survivors = self._branches()
        self.assertIn("laintas/some-other-tool", survivors)   # not ours
        self.assertIn("laintas/blindpick-kkkkkkkk-incumbent", survivors)
        self.assertIn("feature/mine", set(
            line.strip(" *+") for line in
            self._git("branch", "--list").stdout.splitlines()))

    def test_young_orphans_are_left_for_the_grace_period(self):
        self._branch("laintas/blindpick-fresh123-incumbent")
        self.assertEqual([], self.main._sweep_orphan_branches(max_age=3600))

    def test_a_checked_out_branch_is_never_swept(self):
        import worktree_manager
        info = worktree_manager.create_isolated_worktree(
            str(self.repo), label="blindpick-live")
        self.addCleanup(worktree_manager.remove_worktree, info)
        self.assertEqual([], self.main._sweep_orphan_branches(max_age=0))
        self.assertIn(info.branch, self._branches())


class ChildWiringTests(BlindpickTestCase):
    """How the two competitors are wired before they are let loose."""

    def test_each_side_gets_its_own_console(self):
        # rich permits one live display per Console. Both children sharing
        # one killed the second with LiveError the moment they both streamed
        # — which is how every real round lost its challenger.
        import laintas_cli
        first = self.main._child_deps(laintas_cli)
        second = self.main._child_deps(laintas_cli)
        self.assertIsNot(first.console, second.console)
        self.assertIsNot(first.console, laintas_cli.get_loop_deps().console)

    def test_child_renderers_are_silenced(self):
        import laintas_cli
        deps = self.main._child_deps(laintas_cli)
        deps.console.print("must not reach the terminal")
        for renderer in ("display_command_output", "display_file_diff",
                         "display_task_list"):
            getattr(deps, renderer)("x", 0, "y")


class GitBackedTests(BlindpickTestCase):
    """_side_diff must prefer git over the truncated copy on the row."""

    def setUp(self):
        super().setUp()
        self.repo = self.cwd / "repo"
        self.repo.mkdir()
        run = lambda *a: subprocess.run(
            ["git", *a], cwd=self.repo, capture_output=True, check=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        (self.repo / "f.txt").write_text("one\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        self.base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True).stdout.strip()
        (self.repo / "f.txt").write_text("one\ntwo\n")
        run("add", "-A")
        run("commit", "-qm", "result")
        self.result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True).stdout.strip()

    def test_live_diff_beats_the_stored_truncated_copy(self):
        row = {"repo_root": str(self.repo), "incumbent_base": self.base,
               "incumbent_result": self.result,
               "incumbent_diff": "truncated fallback"}
        diff = self.main._side_diff(row, "incumbent")
        self.assertIn("+two", diff)
        self.assertNotIn("truncated fallback", diff)

    def test_falls_back_when_the_commits_are_gone(self):
        row = {"repo_root": str(self.repo), "incumbent_base": "",
               "incumbent_result": "", "incumbent_diff": "stored copy"}
        self.assertEqual("stored copy", self.main._side_diff(row, "incumbent"))


if __name__ == "__main__":
    unittest.main()
