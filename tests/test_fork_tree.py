"""Fork-tree placement of resume snapshots.

Covers the contract that every snapshot kind (named fork, /q checkpoint,
autosave) records the branch it was taken on and is rendered under that
branch in the /resume picker.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_loop
import laintas_cli
import paths
import symbols


def _blob(kind, ts, lineage=None, fork_name=None, blob_id=None):
    blob = {"id": blob_id or f"{kind}-{ts}", "kind": kind, "timestamp": ts}
    if lineage is not None:
        blob["fork_lineage"] = lineage
    if fork_name is not None:
        blob["fork_name"] = fork_name
    return blob


def _rows(choices):
    return [(row[0]["id"], row[1])
            for row in laintas_cli._build_fork_tree_rows(choices)]


class NormalizeForkLineageTests(unittest.TestCase):
    def test_rejects_non_list_and_non_string_entries(self):
        self.assertEqual(agent_loop.normalize_fork_lineage(None), [])
        self.assertEqual(agent_loop.normalize_fork_lineage("a"), [])
        self.assertEqual(agent_loop.normalize_fork_lineage(["a", 3, None, "b"]),
                         ["a", "b"])

    def test_collapses_whitespace_drops_empties_and_bounds_size(self):
        self.assertEqual(agent_loop.normalize_fork_lineage(["  a \n b ", "  "]),
                         ["a b"])
        long_name = "x" * 200
        self.assertEqual(
            agent_loop.normalize_fork_lineage([long_name])[0],
            "x" * agent_loop._FORK_NAME_MAX)
        deep = [f"n{i}" for i in range(50)]
        self.assertEqual(len(agent_loop.normalize_fork_lineage(deep)),
                         agent_loop._FORK_LINEAGE_MAX_DEPTH)

    def test_returns_a_copy_callers_can_mutate(self):
        source = ["a"]
        result = agent_loop.normalize_fork_lineage(source)
        result.append("b")
        self.assertEqual(source, ["a"])


class ForkTreeRowTests(unittest.TestCase):
    def test_trunk_entries_stay_root_level_newest_first(self):
        choices = [_blob("autosave", 10, blob_id="old"),
                   _blob("checkpoint", 20, blob_id="new")]
        self.assertEqual(_rows(choices), [("new", ""), ("old", "")])

    def test_branch_snapshots_nest_under_their_named_fork(self):
        choices = [
            _blob("autosave", 5, blob_id="trunk"),
            _blob("fork", 10, lineage=["a"], fork_name="a", blob_id="fork-a"),
            _blob("autosave", 30, lineage=["a"], blob_id="a-auto"),
            _blob("checkpoint", 20, lineage=["a"], blob_id="a-ckpt"),
        ]
        branch = symbols.TREE_BRANCH + " "
        last = symbols.TREE_LAST + " "
        self.assertEqual(_rows(choices), [
            ("trunk", ""),
            ("fork-a", ""),
            ("a-auto", branch),
            ("a-ckpt", last),
        ])

    def test_sub_branches_render_after_own_snapshots_with_connectors(self):
        choices = [
            _blob("fork", 10, lineage=["a"], fork_name="a", blob_id="fork-a"),
            _blob("autosave", 15, lineage=["a"], blob_id="a-auto"),
            _blob("fork", 20, lineage=["a", "b"], fork_name="b",
                  blob_id="fork-b"),
            _blob("autosave", 25, lineage=["a", "b"], blob_id="b-auto"),
        ]
        branch = symbols.TREE_BRANCH + " "
        last = symbols.TREE_LAST + " "
        vert = symbols.TREE_VERT + " "
        self.assertEqual(_rows(choices), [
            ("fork-a", ""),
            ("a-auto", branch),
            ("fork-b", last),
            ("b-auto", "   " + last),
        ])

    def test_continuation_pad_matches_connector_width(self):
        choices = [
            _blob("fork", 10, lineage=["a"], fork_name="a", blob_id="fork-a"),
            _blob("fork", 20, lineage=["a", "b"], fork_name="b",
                  blob_id="fork-b"),
            _blob("autosave", 15, lineage=["a", "b"], blob_id="b-auto"),
            _blob("fork", 30, lineage=["a", "c"], fork_name="c",
                  blob_id="fork-c"),
        ]
        branch = symbols.TREE_BRANCH + " "
        last = symbols.TREE_LAST + " "
        vert = symbols.TREE_VERT + "  "
        self.assertEqual(_rows(choices), [
            ("fork-a", ""),
            ("fork-c", branch),
            ("fork-b", last),
            ("b-auto", "   " + last),
        ])
        # A non-last sub-branch keeps a vertical line under it.
        choices.append(
            _blob("autosave", 5, lineage=["a", "c"], blob_id="c-auto"))
        rows = dict(_rows(choices))
        self.assertEqual(rows["c-auto"], vert + last)

    def test_branch_without_a_fork_snapshot_is_headed_by_its_newest(self):
        choices = [
            _blob("autosave", 10, lineage=["branch-1"], blob_id="b1-old"),
            _blob("autosave", 20, lineage=["branch-1"], blob_id="b1-new"),
        ]
        self.assertEqual(_rows(choices), [
            ("b1-new", ""),
            ("b1-old", symbols.TREE_LAST + " "),
        ])

    def test_deleted_intermediate_branch_collapses_instead_of_dangling(self):
        choices = [
            _blob("fork", 10, lineage=["a", "b"], fork_name="b",
                  blob_id="fork-b"),
        ]
        # "a" has no snapshot left; "b" must still render at root level.
        self.assertEqual(_rows(choices), [("fork-b", "")])

    def test_legacy_fork_without_lineage_uses_its_name(self):
        choices = [_blob("fork", 10, fork_name="a", blob_id="legacy"),
                   _blob("autosave", 20, lineage=["a"], blob_id="a-auto")]
        self.assertEqual(_rows(choices), [
            ("legacy", ""),
            ("a-auto", symbols.TREE_LAST + " "),
        ])

    def test_every_choice_appears_exactly_once(self):
        choices = [
            _blob("autosave", 1, blob_id="trunk"),
            _blob("fork", 2, lineage=["a"], fork_name="a", blob_id="fork-a"),
            _blob("autosave", 3, lineage=["a"], blob_id="a-auto"),
            _blob("fork", 4, lineage=["a", "b"], fork_name="b", blob_id="fk-b"),
            _blob("checkpoint", 5, lineage=["c"], blob_id="c-ckpt"),
            _blob("autosave", 6, lineage=["a", "b"], blob_id="b-auto"),
        ]
        rows = laintas_cli._build_fork_tree_rows(choices)
        self.assertEqual(sorted(row[0]["id"] for row in rows),
                         sorted(choice["id"] for choice in choices))

    def test_garbage_lineage_falls_back_to_the_trunk(self):
        choices = [_blob("autosave", 10, lineage="not-a-list",
                         blob_id="junk")]
        self.assertEqual(_rows(choices), [("junk", "")])


class LineagePersistenceTests(unittest.TestCase):
    def test_prepare_state_for_repl_keeps_lineage_across_turns(self):
        state = {"_fork_lineage": ["a", "b"], "_fork_name": "b"}
        carried = agent_loop.prepare_state_for_repl(state)
        self.assertEqual(carried["_fork_lineage"], ["a", "b"])
        self.assertEqual(carried["_fork_name"], "b")
        # Still bounded on the way through.
        dirty = agent_loop.prepare_state_for_repl(
            {"_fork_lineage": ["a", 7, ""], "_fork_name": "  x  y "})
        self.assertEqual(dirty["_fork_lineage"], ["a"])
        self.assertEqual(dirty["_fork_name"], "x y")

    def test_trunk_state_carries_empty_lineage(self):
        carried = agent_loop.prepare_state_for_repl({})
        self.assertEqual(carried["_fork_lineage"], [])
        self.assertEqual(carried["_fork_name"], "")

    def test_autosave_payload_records_the_branch_it_was_taken_on(self):
        payload = agent_loop._build_resume_payload(
            {"_fork_lineage": ["a", "b"]},
            [{"role": "user", "content": "hi", "input_kind": "prompt"}],
            "/tmp/project", "autosave")
        self.assertEqual(payload["fork_lineage"], ["a", "b"])

    def test_restore_injects_lineage_for_a_branch_autosave(self):
        blob = {"fork_lineage": ["a", "b"], "chat_history": [], "state": {}}
        restored = laintas_cli._restore_resume_blob(blob, [])
        self.assertEqual(restored["_fork_lineage"], ["a", "b"])
        self.assertEqual(restored["_fork_name"], "b")

    def test_restore_clears_lineage_for_a_trunk_snapshot(self):
        blob = {"chat_history": [], "state": {"_fork_lineage": ["stale"]}}
        restored = laintas_cli._restore_resume_blob(blob, [])
        self.assertNotIn("_fork_lineage", restored)
        self.assertNotIn("_fork_name", restored)


class ForkRoundTripTests(unittest.TestCase):
    """The persisted store, not just the in-memory tree builder."""

    def _history(self, text):
        return [{"role": "user", "content": text, "input_kind": "prompt"},
                {"role": "assistant", "content": "ok"}]

    def test_branch_autosave_lands_under_its_fork_in_the_picker_tree(self):
        cwd = "/work/project"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "SESSIONS_DIR", Path(tmp)):
            # Trunk session, then a named fork off it.
            trunk_state = {"_session_id": "trunk-session"}
            agent_loop.save_resume_state(
                trunk_state, self._history("trunk work"), cwd)
            agent_loop.save_fork_state(
                trunk_state, self._history("trunk work"), cwd,
                "alt", ["alt"], "trunk-session")

            # Resuming the fork carries the lineage into the live state, and
            # the next turn's autosave must stay on that branch.
            fork_blob = agent_loop.load_resume_state(cwd, "trunk-session")
            branch_state = laintas_cli._restore_resume_blob(
                {"fork_lineage": ["alt"], "fork_name": "alt",
                 "chat_history": [], "state": {}}, [])
            self.assertEqual(branch_state["_fork_lineage"], ["alt"])
            branch_state["_session_id"] = "branch-session"
            # A full turn boundary happens between resume and autosave.
            branch_state = agent_loop.prepare_state_for_repl(branch_state)
            agent_loop.save_resume_state(
                branch_state, self._history("branch work"), cwd)

            choices = agent_loop.list_resume_states(cwd)
            rows = laintas_cli._build_fork_tree_rows(choices)
            by_kind = {(row[0].get("kind"), row[0].get("session_id")): row[1]
                       for row in rows}
            # Fork heads the branch at root level; its autosave nests under it.
            self.assertEqual(by_kind[("fork", "trunk-session")], "")
            self.assertEqual(by_kind[("autosave", "branch-session")],
                             symbols.TREE_LAST + " ")
            self.assertIsNotNone(fork_blob)

    def test_chained_fork_keeps_its_parent_after_a_turn_boundary(self):
        state = {"_session_id": "s1", "_fork_lineage": ["a"], "_fork_name": "a"}
        carried = agent_loop.prepare_state_for_repl(state)
        chained = agent_loop.normalize_fork_lineage(
            carried["_fork_lineage"] + ["b"])
        self.assertEqual(chained, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
