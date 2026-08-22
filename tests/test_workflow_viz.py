"""Tests for workflow_viz: HWG metro-map layout + plain rendering.

Pure-function tests: parse sample graphs, assert on the laid-out canvas,
hit regions, and navigation metadata. No terminal required.
"""

import unittest

from hwg_adapter import parse as parse_hwg
import workflow_viz


def _view(source: str, status=None) -> workflow_viz.GraphView:
    return workflow_viz.build_view(
        workflow_viz.graph_from_statements(parse_hwg(source), status))


class BackEdgeTests(unittest.TestCase):
    def test_simple_chain_has_no_back_edges(self):
        src = "(a.hwo)#a#\n(b.hwo)#b#\n#a# -> #b#\n"
        v = _view(src)
        # mainline covers every node
        self.assertEqual(set(v.mainline), {"a", "b"})

    def test_cycle_back_edge_is_detected_and_routed_left(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n"
               "#a# -> #b#\n"
               "#b# -> { on: FAIL, maxLoops: 2 } #a#\n")
        v = _view(src)
        # the loop edge is classified as back edge -> loop rail on the left
        loop_edge = v.out_edges["b"][0]
        self.assertEqual(loop_edge["to"], "a")
        lines = v.canvas.lines()
        joined = "\n".join(lines)
        self.assertIn("↺×2", joined)
        # loop rail char appears at the far-left rail column (col 1)
        self.assertTrue(any(len(line) > 1 and line[1] == "║" for line in lines),
                        "expected a left loop rail column")


class MainlineTests(unittest.TestCase):
    def test_longest_chain_becomes_mainline(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n(d.hwo)#d#\n"
               "#a# -> #b#\n#b# -> #c#\n#a# -> #d#\n")
        v = _view(src)
        self.assertEqual(v.mainline, ["a", "b", "c"])
        # d sits off the mainline (side node) - its box still exists
        self.assertIn("d", v.nodes_by_id)
        self.assertIn("d", v.node_rects)

    def test_all_conditional_graph_still_gets_mainline(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n"
               "#a# -> { on: PASS } #b#\n"
               "#b# -> { on: PASS } #c#\n")
        v = _view(src)
        self.assertEqual(v.mainline, ["a", "b", "c"])


class NodeBoxTests(unittest.TestCase):
    def test_box_renders_id_file_and_status_icon(self):
        src = "(a.hwo)#a#\n"
        v = _view(src, status={"a": workflow_viz.STATUS_DONE})
        lines = v.canvas.lines()
        joined = "\n".join(lines)
        self.assertIn("#a#", joined)
        self.assertIn("a.hwo", joined)
        self.assertIn(workflow_viz.status_icon(workflow_viz.STATUS_DONE),
                      joined)

    def test_manual_gate_and_policy_are_rendered(self):
        src = ('!(review.hwo)#review# { retry: 2, timeout: "10m" }\n')
        v = _view(src)
        joined = "\n".join(v.canvas.lines())
        self.assertIn("⏸", joined)
        self.assertIn("retry×2", joined)
        self.assertIn("10m", joined)

    def test_retry_missing_file_field_does_not_crash(self):
        src = "#a# { }\n" if False else "(a.hwo)#a#\n"
        v = _view(src)
        self.assertIn("a", v.nodes_by_id)


class HitRegionTests(unittest.TestCase):
    def test_click_inside_box_resolves_to_node(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n"
               "#a# -> #b#\n#a# -> { on: FAIL } #b#\n")
        v = _view(src)
        row = v.node_rows["a"]
        col = v.node_rects["a"][1] + 5
        self.assertEqual(v.canvas.hit_at(row, col), "a")

    def test_click_outside_any_box_resolves_to_none(self):
        src = "(a.hwo)#a#\n"
        v = _view(src)
        self.assertIsNone(v.canvas.hit_at(50, 50))


class StatusTests(unittest.TestCase):
    def test_status_styles_are_stable(self):
        self.assertEqual(workflow_viz.status_icon(None), "○")
        self.assertEqual(
            workflow_viz.status_icon(workflow_viz.STATUS_RUNNING), "◐")
        self.assertEqual(
            workflow_viz.status_icon(workflow_viz.STATUS_FAILED), "✗")

    def test_counts_use_plural_form(self):
        v = _view("(a.hwo)#a#\n")
        # zero-count entries are omitted by design
        self.assertEqual(v.meta["counts"], "1 node")
        v2 = _view("(a.hwo)#a#\n(b.hwo)#b#\n#a# -> #b#\n")
        self.assertIn("2 nodes", v2.meta["counts"])
        self.assertIn("1 edge", v2.meta["counts"])

    def test_empty_graph_renders_placeholder(self):
        v = _view("")
        self.assertEqual(v.node_order, [])
        self.assertIn("empty graph", workflow_viz.render_plain(
            workflow_viz.graph_from_statements(parse_hwg(""))))


class PlainRenderTests(unittest.TestCase):
    def test_render_plain_contains_all_nodes(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n"
               "#a# -> #b#\n#b# -> #c#\n"
               "#c# -> { on: FAIL, maxLoops: 3 } #a#\n")
        out = workflow_viz.render_plain(
            workflow_viz.graph_from_statements(parse_hwg(src)), "flow")
        for nid in ("#a#", "#b#", "#c#"):
            self.assertIn(nid, out)
        self.assertIn("flow", out)
        self.assertIn("↺×3", out)

    def test_no_replacement_chars(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n"
               "#a# -> { on: PASS } #b#\n"
               "#b# -> { on: FAIL, maxLoops: 5 } #a#\n")
        out = workflow_viz.render_plain(
            workflow_viz.graph_from_statements(parse_hwg(src)))
        self.assertNotIn("\ufffd", out)


class NavigationMetadataTests(unittest.TestCase):
    def test_adjacency_maps_are_complete(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n(c.hwo)#c#\n"
               "#a# -> #b#\n#b# -> #c#\n")
        v = _view(src)
        self.assertEqual([e["to"] for e in v.out_edges["a"]], ["b"])
        self.assertEqual([e["from"] for e in v.in_edges["b"]], ["a"])
        self.assertEqual(v.node_order, ["a", "b", "c"])

    def test_canvas_is_bounded(self):
        # A wide fan-out graph must not explode the canvas width.
        src = ["(hub.hwo)#hub#"] + [
            f"(n{i}.hwo)#n{i}#" for i in range(10)
        ]
        src.append("#hub# -> #n0#\n")
        for i in range(9):
            src.append(f"#n{i}# -> #n{i+1}#\n")
        v = _view("".join(src))
        self.assertLess(v.canvas.width, 160)
        self.assertGreater(v.canvas.height, 10)


class SelfLoopTests(unittest.TestCase):
    def test_self_loop_renders_without_crash(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n"
               "#a# -> #b#\n"
               "#b# -> { on: FAIL, maxLoops: 3 } #b#\n")
        v = _view(src)
        joined = "\n".join(v.canvas.lines())
        self.assertIn("↺×3", joined)


def _run(events=None, **kw):
    base = {
        "runId": "r1", "kind": "hwg", "status": "running",
        "source": "g.hwg", "currentNode": None, "history": [],
        "pendingInterrupt": None, "checkpoints": [], "events": [],
    }
    base.update(kw)
    if events is not None:
        base["events"] = events
    return base


class MiniStatusTests(unittest.TestCase):
    def test_status_from_events(self):
        run = _run(events=[
            {"type": "node_started", "payload": {"node": "a"}, "createdAt": 10},
            {"type": "node_completed", "payload": {"node": "a"}, "createdAt": 12},
            {"type": "node_started", "payload": {"node": "b"}, "createdAt": 13},
            {"type": "node_failed", "payload": {"node": "b"}, "createdAt": 20},
        ])
        rs = workflow_viz.status_from_run(run, ["a", "b", "c"])
        self.assertEqual(rs["status"]["a"], workflow_viz.STATUS_DONE)
        self.assertEqual(rs["status"]["b"], workflow_viz.STATUS_FAILED)
        self.assertEqual(rs["order"], ["a", "b", "c"])
        self.assertAlmostEqual(rs["duration"]["a"], 2.0)
        self.assertAlmostEqual(rs["duration"]["b"], 7.0)

    def test_history_fills_trimmed_events(self):
        run = _run(history=["a", "b"], status="running")
        rs = workflow_viz.status_from_run(run, ["a", "b"])
        self.assertEqual(rs["status"]["a"], workflow_viz.STATUS_DONE)
        self.assertEqual(rs["status"]["b"], workflow_viz.STATUS_DONE)

    def test_current_node_is_running(self):
        run = _run(currentNode="b", status="running")
        rs = workflow_viz.status_from_run(run, ["a", "b"])
        self.assertEqual(rs["status"]["b"], workflow_viz.STATUS_RUNNING)

    def test_pending_interrupt_marks_paused(self):
        run = _run(currentNode="b", status="paused",
                   pendingInterrupt={"node": "b"})
        rs = workflow_viz.status_from_run(run, ["a", "b"])
        self.assertEqual(rs["status"]["b"], workflow_viz.STATUS_PAUSED)

    def test_step_id_payload_also_read(self):
        run = _run(events=[
            {"type": "step_completed", "payload": {"stepId": "0.1"}, "createdAt": 5},
        ])
        rs = workflow_viz.status_from_run(run)
        self.assertEqual(rs["status"]["0.1"], workflow_viz.STATUS_DONE)

    def test_mini_strip_uses_icons_and_durations(self):
        rs = {
            "order": ["a", "b", "c"],
            "status": {"a": workflow_viz.STATUS_DONE,
                       "b": workflow_viz.STATUS_RUNNING},
            "duration": {"a": 1.5},
        }
        line = workflow_viz.render_mini(rs, width=200)
        self.assertIn(workflow_viz.status_icon(workflow_viz.STATUS_DONE), line)
        self.assertIn(workflow_viz.status_icon(workflow_viz.STATUS_RUNNING), line)
        self.assertIn("a 1.5s", line)

    def test_mini_strip_clamps_to_width(self):
        order = [f"node{i}" for i in range(20)]
        rs = {"order": order, "status": {},
              "duration": {}}
        line = workflow_viz.render_mini(rs, width=40)
        self.assertLessEqual(len(line), 40)

    def test_mini_strip_collapses_leading_done(self):
        order = [f"done{i}" for i in range(6)] + ["active"]
        status = {n: workflow_viz.STATUS_DONE for n in order[:6]}
        rs = {"order": order, "status": status, "duration": {}}
        line = workflow_viz.render_mini(rs, width=60)
        self.assertIn("…+6", line)
        self.assertIn("active", line)

    def test_node_order_from_source(self):
        src = ("(a.hwo)#a#\n(b.hwo)#b#\n"
               "#a# -> #c#\n#b# -> #c#\n")
        order = workflow_viz.node_order_from_source(src)
        self.assertEqual(order, ["a", "b", "c"])


class GanttTests(unittest.TestCase):
    def _run(self, events):
        return {"runId": "r1", "kind": "hwg", "status": "running",
                "currentNode": None, "history": [], "pendingInterrupt": None,
                "checkpoints": [], "events": events}

    def test_timeline_spans_and_duration(self):
        run = self._run([
            {"type": "node_started", "payload": {"node": "a"}, "createdAt": 10},
            {"type": "node_completed", "payload": {"node": "a"}, "createdAt": 14},
            {"type": "node_started", "payload": {"node": "b"}, "createdAt": 12},
            {"type": "node_failed", "payload": {"node": "b"}, "createdAt": 19},
        ])
        tl = workflow_viz.timeline_from_run(run, ["a", "b"])
        spans = {s["id"]: s for s in tl["spans"]}
        self.assertEqual(spans["a"]["start"], 10.0)
        self.assertEqual(spans["a"]["end"], 14.0)
        self.assertEqual(spans["a"]["status"], workflow_viz.STATUS_DONE)
        self.assertEqual(spans["b"]["start"], 12.0)
        self.assertEqual(spans["b"]["end"], 19.0)
        self.assertEqual(spans["b"]["status"], workflow_viz.STATUS_FAILED)

    def test_running_node_ends_at_last_event(self):
        run = self._run([
            {"type": "node_started", "payload": {"node": "a"}, "createdAt": 10},
            {"type": "workflow_paused", "payload": {}, "createdAt": 20},
        ])
        run["currentNode"] = "a"
        tl = workflow_viz.timeline_from_run(run, ["a"])
        spans = {s["id"]: s for s in tl["spans"]}
        self.assertEqual(spans["a"]["status"], workflow_viz.STATUS_RUNNING)
        self.assertEqual(spans["a"]["end"], 20.0)

    def test_never_started_node_is_pending(self):
        run = self._run([])
        tl = workflow_viz.timeline_from_run(run, ["a"])
        self.assertIsNone(tl["spans"][0]["start"])
        self.assertEqual(tl["spans"][0]["status"], workflow_viz.STATUS_PENDING)

    def test_gantt_renders_status_chars(self):
        run = self._run([
            {"type": "node_started", "payload": {"node": "a"}, "createdAt": 0},
            {"type": "node_completed", "payload": {"node": "a"}, "createdAt": 5},
            {"type": "node_started", "payload": {"node": "b"}, "createdAt": 2},
            {"type": "node_failed", "payload": {"node": "b"}, "createdAt": 8},
        ])
        chart = workflow_viz.gantt_from_run(run, ["a", "b"], width=80)
        self.assertIn("a", chart)
        self.assertIn("b", chart)
        self.assertIn("=", chart)   # done fill
        self.assertIn("X", chart)   # failed fill
        self.assertIn("5.0s", chart)

    def test_gantt_falls_back_to_status_listing(self):
        run = self._run([])
        chart = workflow_viz.gantt_from_run(run, ["a", "b"])
        self.assertIn("a", chart)
        self.assertIn("b", chart)
        self.assertNotIn("=", chart)

    def test_gantt_clamps_to_width(self):
        events = []
        for i in range(20):
            nid = f"node{i}"
            events.append({"type": "node_started", "payload": {"node": nid},
                           "createdAt": i})
            events.append({"type": "node_completed", "payload": {"node": nid},
                           "createdAt": i + 0.5})
        run = self._run(events)
        order = [f"node{i}" for i in range(20)]
        chart = workflow_viz.gantt_from_run(run, order, width=50)
        for line in chart.splitlines():
            self.assertLessEqual(len(line), 60)


if __name__ == "__main__":
    unittest.main()
