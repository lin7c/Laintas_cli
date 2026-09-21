"""L1 (bughunt): a failed fan-out node must not fan out unconditionally.

_open_fanout ignored node_failed, so a failed node's unconditional `=>`
branches all fired and the graph could end "completed" while nothing
worked — the exact green-but-empty shape _note_unhandled_failure's
docstring warns about. The fix aligns fan-out with _choose_next: an `on:`
condition declares what a failure means there; without one the failure is
recorded and the run ends failed.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hwg_runner


class _Chdir:
    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old)


_FANOUT_GRAPH = (
    '(a.hwo)#a# [out(verdict: string)]\n'
    '(b.hwo)#b#\n(c.hwo)#c#\n'
    '(m.hwo)#m# { join: "all" }\n(s.hwo)#s#\n'
    '#a# => #b#\n#a# => #c#\n#b# -> #m#\n#c# -> #m#\n#m# -> #s#\n'
)
_FANOUT_ON_GRAPH = _FANOUT_GRAPH.replace(
    '#a# => #b#\n', '#a# => { on: verdict == "PASS" } #b#\n'
).replace(
    '#a# => #c#\n', '#a# => { on: verdict == "FAIL" } #c#\n'
)


def _run_graph(graph, results):
    def fake_run(path, **kw):
        nid = Path(path).stem
        ok = results[nid]
        verdict = "PASS" if ok else "FAIL"
        return {"ok": ok, "msg": verdict, "outputs": {"verdict": verdict}}

    with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
        Path("flow.hwg").write_text(graph, encoding="utf-8")
        with mock.patch.object(hwg_runner.hwo_runner, "run_hwo_file",
                               side_effect=fake_run):
            return hwg_runner.run_hwg_file("flow.hwg", deps=object(),
                                           session={})


_ALL_OK = {"a": True, "b": True, "c": True, "m": True, "s": True}
_A_FAILS = {"a": False, "b": True, "c": True, "m": True, "s": True}


class FanoutFailureTests(unittest.TestCase):
    def test_failed_node_fanout_unconditional_ends_failed(self):
        result = _run_graph(_FANOUT_GRAPH, _A_FAILS)
        self.assertFalse(result["ok"])
        self.assertIn("no edge said what a failure", result["msg"])

    def test_failed_node_fanout_with_on_conditions_is_handled(self):
        result = _run_graph(_FANOUT_ON_GRAPH, _A_FAILS)
        self.assertTrue(result["ok"], result["msg"])

    def test_successful_node_fanout_still_completes(self):
        result = _run_graph(_FANOUT_GRAPH, _ALL_OK)
        self.assertTrue(result["ok"], result["msg"])

    def test_sequential_failed_node_unconditional_edge_still_noted(self):
        # Regression guard for the _choose_next path the fix was aligned to.
        graph = ('(a.hwo)#a# [out(verdict: string)]\n(b.hwo)#b#\n#a# -> #b#\n')
        result = _run_graph(graph, {"a": False, "b": True})
        self.assertFalse(result["ok"])
        self.assertIn("no edge said what a failure", result["msg"])


if __name__ == "__main__":
    unittest.main()
