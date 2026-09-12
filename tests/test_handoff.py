"""Tests for handoff.py — the envelope one worker hands to the next.

The properties worth testing here are the ones the design rests on, not the
getters. In order of how much damage their absence does:

  * merge is a CRDT-shaped union (idempotent, commutative, associative), which
    is the entire reason two machines can hold the same envelope without a lock;
  * status is a projection, so two participants who have merged the same events
    cannot disagree about who holds the work — including when they both claimed;
  * "what is left" is read from the workspace, not from the envelope, so it
    cannot be overstated by whoever wrote it;
  * the .gitignore exception composes with contract_store's, because the rule
    that makes them work is also the rule that makes them clobber each other.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handoff  # noqa: E402
import paths  # noqa: E402


class HandoffTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def make(self, title="ship the inbox", actor="ann", **kw):
        return handoff.create(title, actor, cwd=self.cwd, **kw)


class TestIds(HandoffTestCase):
    def test_ids_are_filenames_so_traversal_is_refused_not_sanitised(self):
        for bad in ("../etc/passwd", "a/b", "..", "", "x", "A" * 80, "-lead"):
            with self.assertRaises(handoff.HandoffError):
                handoff.valid_id(bad)

    def test_new_id_is_a_slug_plus_entropy(self):
        first = handoff.new_id("Ship the Inbox!")
        self.assertTrue(first.startswith("ship-the-inbox-"))
        self.assertNotEqual(first, handoff.new_id("Ship the Inbox!"))
        handoff.valid_id(first)

    def test_a_title_with_no_ascii_still_yields_a_usable_id(self):
        # Titles are written by people, and half this project's users write
        # Chinese. A slug that collapses to empty must not produce an id the
        # validator then rejects.
        handoff.valid_id(handoff.new_id("交接给下一个人"))
        handoff.valid_id(handoff.new_id("---"))
        handoff.valid_id(handoff.new_id(""))


class TestCreate(HandoffTestCase):
    def test_a_handoff_needs_a_title(self):
        with self.assertRaises(handoff.HandoffError):
            self.make(title="   ")

    def test_created_envelope_round_trips(self):
        env = self.make(to="bob", avoid=["don't retry the R2 presign path"],
                        note="stopped at the migration")
        again = handoff.load(env["id"], cwd=self.cwd)
        self.assertEqual(again["title"], "ship the inbox")
        self.assertEqual(again["to"], "bob")
        self.assertEqual(again["avoid"], ["don't retry the R2 presign path"])
        self.assertEqual([e["kind"] for e in again["events"]], ["open"])
        self.assertEqual(again["events"][0]["note"], "stopped at the migration")

    def test_an_uncheckable_contract_is_refused_at_creation(self):
        # Not at read time: the successor discovering that the acceptance
        # criteria are nonsense is the moment it is least useful.
        with self.assertRaises(handoff.HandoffError):
            self.make(contract={"outputs": []})
        with self.assertRaises(handoff.HandoffError):
            self.make(contract={"outputs": [{"name": "x"}],
                                "acceptance": [{"kind": "nonsense"}]})

    def test_list_all_is_newest_first_and_skips_unreadable(self):
        a = self.make(title="first")
        b = self.make(title="second")
        (handoff.handoff_dir(self.cwd) / "garbage.json").write_text("{nope", encoding="utf-8")
        ids = [e["id"] for e in handoff.list_all(self.cwd)]
        self.assertEqual(set(ids), {a["id"], b["id"]})
        self.assertEqual(len(ids), 2)


class TestParse(HandoffTestCase):
    def test_a_newer_format_is_refused_rather_than_half_read(self):
        env = self.make()
        raw = dict(env, version=handoff.VERSION + 1)
        with self.assertRaises(handoff.HandoffError) as ctx:
            handoff.parse(raw)
        self.assertIn("upgrade", str(ctx.exception))

    def test_malformed_events_are_dropped_not_fatal(self):
        env = self.make()
        raw = dict(env, events=[
            *env["events"],
            "not a dict",
            {"id": "x", "kind": "claim"},              # no ts
            {"id": "", "kind": "claim", "ts": 1},      # no id
            {"id": "y", "kind": "invented", "ts": 1},  # unknown kind
        ])
        parsed = handoff.parse(raw)
        self.assertEqual([e["kind"] for e in parsed["events"]], ["open"])

    def test_duplicate_event_ids_collapse(self):
        env = self.make()
        raw = dict(env, events=[*env["events"], *env["events"]])
        self.assertEqual(len(handoff.parse(raw)["events"]), 1)


class TestProjection(HandoffTestCase):
    def test_open_then_claimed_then_closed(self):
        env = self.make()
        self.assertEqual(handoff.project(env)["status"], "open")
        env = handoff.append(env["id"], "claim", "bob", cwd=self.cwd)
        state = handoff.project(env)
        self.assertEqual((state["status"], state["holder"]), ("claimed", "bob"))
        env = handoff.append(env["id"], "close", "bob", cwd=self.cwd)
        self.assertEqual(handoff.project(env)["status"], "closed")

    def test_reopen_undoes_close(self):
        env = self.make()
        handoff.append(env["id"], "close", "ann", cwd=self.cwd)
        env = handoff.append(env["id"], "reopen", "ann", cwd=self.cwd)
        self.assertEqual(handoff.project(env)["status"], "claimed" if
                         handoff.project(env)["holder"] else "open")
        self.assertEqual(handoff.project(env)["status"], "open")

    def test_a_second_claimant_is_reported_not_silently_dropped(self):
        # Two people both believing they own the work is the exact failure a
        # handoff protocol exists to prevent, so it must be visible.
        env = self.make()
        env = handoff.append(env["id"], "claim", "bob", cwd=self.cwd)
        env = handoff.append(env["id"], "claim", "cid", cwd=self.cwd)
        state = handoff.project(env)
        self.assertEqual(state["holder"], "bob")
        self.assertEqual([c["actor"] for c in state["contested"]], ["cid"])

    def test_only_the_holder_can_release(self):
        env = self.make()
        env = handoff.append(env["id"], "claim", "bob", cwd=self.cwd)
        env = handoff.append(env["id"], "release", "mallory", cwd=self.cwd)
        self.assertEqual(handoff.project(env)["holder"], "bob")
        env = handoff.append(env["id"], "release", "bob", cwd=self.cwd)
        self.assertEqual(handoff.project(env)["status"], "open")

    def test_release_clears_the_contest_so_the_next_claim_wins_cleanly(self):
        env = self.make()
        env = handoff.append(env["id"], "claim", "bob", cwd=self.cwd)
        env = handoff.append(env["id"], "claim", "cid", cwd=self.cwd)
        env = handoff.append(env["id"], "release", "bob", cwd=self.cwd)
        state = handoff.project(env)
        self.assertEqual((state["status"], state["contested"]), ("open", []))


class TestMerge(HandoffTestCase):
    """The properties that let two machines share an envelope without a lock."""

    def two_copies(self):
        env = self.make()
        base = env["createdAt"]
        local = handoff.parse(dict(env))
        remote = handoff.parse(dict(env))
        local["events"] = handoff._clean_events([
            *local["events"], handoff.make_event("note", "ann", "a", ts=base + 1)])
        remote["events"] = handoff._clean_events([
            *remote["events"], handoff.make_event("note", "bob", "b", ts=base + 2)])
        return local, remote

    def test_union_keeps_both_sides(self):
        local, remote = self.two_copies()
        merged, gained = handoff.merge(local, remote)
        self.assertEqual([e["note"] for e in merged["events"]], ["", "a", "b"])
        self.assertEqual(gained, 1)

    def test_idempotent(self):
        local, remote = self.two_copies()
        once, _ = handoff.merge(local, remote)
        twice, gained = handoff.merge(once, remote)
        self.assertEqual([e["id"] for e in once["events"]],
                         [e["id"] for e in twice["events"]])
        self.assertEqual(gained, 0)

    def test_commutative(self):
        local, remote = self.two_copies()
        a, _ = handoff.merge(local, remote)
        b, _ = handoff.merge(remote, local)
        self.assertEqual([e["id"] for e in a["events"]],
                         [e["id"] for e in b["events"]])

    def test_associative(self):
        env = self.make()
        copies = []
        for i, actor in enumerate(("ann", "bob", "cid")):
            copy = handoff.parse(dict(env))
            copy["events"] = handoff._clean_events([
                *copy["events"],
                handoff.make_event("note", actor, f"n{i}",
                                   ts=env["createdAt"] + 1 + i)])
            copies.append(copy)
        left, _ = handoff.merge(handoff.merge(copies[0], copies[1])[0], copies[2])
        right, _ = handoff.merge(copies[0], handoff.merge(copies[1], copies[2])[0])
        self.assertEqual([e["id"] for e in left["events"]],
                         [e["id"] for e in right["events"]])

    def test_both_sides_project_the_same_holder_after_merging(self):
        # The point of the whole design: two people claim offline, then sync.
        env = self.make()
        local = handoff.parse(dict(env))
        remote = handoff.parse(dict(env))
        base = env["createdAt"]
        local["events"] = handoff._clean_events([
            *local["events"], handoff.make_event("claim", "bob", ts=base + 10)])
        remote["events"] = handoff._clean_events([
            *remote["events"], handoff.make_event("claim", "cid", ts=base + 20)])
        mine, _ = handoff.merge(local, remote)
        theirs, _ = handoff.merge(remote, local)
        self.assertEqual(handoff.project(mine)["holder"], "bob")
        self.assertEqual(handoff.project(theirs)["holder"], "bob")
        self.assertEqual(handoff.project(mine)["contested"],
                         handoff.project(theirs)["contested"])

    def test_simultaneous_claims_break_the_tie_deterministically(self):
        # Identical timestamps are what a tie-break is for. The id decides, and
        # both sides must decide the same way.
        env = self.make()
        same = env["createdAt"] + 5
        one = handoff.make_event("claim", "bob", ts=same)
        two = handoff.make_event("claim", "cid", ts=same)
        winner = "bob" if one["id"] < two["id"] else "cid"
        local = handoff.parse(dict(env))
        remote = handoff.parse(dict(env))
        local["events"] = handoff._clean_events([*local["events"], one, two])
        remote["events"] = handoff._clean_events([*remote["events"], two, one])
        self.assertEqual(handoff.project(local)["holder"], winner)
        self.assertEqual(handoff.project(remote)["holder"], winner)

    def test_different_ids_never_merge(self):
        a, b = self.make(title="one"), self.make(title="two")
        with self.assertRaises(handoff.HandoffError):
            handoff.merge(a, b)

    def test_same_id_different_header_raises_instead_of_picking_a_side(self):
        env = self.make()
        forged = handoff.parse(dict(env, title="something else entirely"))
        with self.assertRaises(handoff.HandoffError) as ctx:
            handoff.merge(env, forged)
        self.assertIn("two different headers", str(ctx.exception))


class TestRemaining(HandoffTestCase):
    """'What is left' is a reading of the workspace, not of the envelope."""

    def contracted(self):
        return self.make(
            contract={
                "goal": "finish the migration",
                "outputs": [{"name": "migration", "type": "file"}],
                "acceptance": [{"kind": "file_exists", "output": "migration"}],
            },
            expect={"migration": "server/migrations/020_handoff.sql"})

    def test_no_contract_means_nothing_to_check(self):
        result = handoff.remaining(self.make(), cwd=self.cwd)
        self.assertEqual((result["checked"], result["ok"]), (False, True))

    def test_a_gap_is_reported_while_the_file_is_missing(self):
        env = self.contracted()
        result = handoff.remaining(env, cwd=self.cwd)
        self.assertTrue(result["checked"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("020_handoff.sql" in g for g in result["gaps"]))

    def test_the_same_envelope_passes_once_the_work_is_actually_done(self):
        env = self.contracted()
        target = Path(self.cwd) / "server" / "migrations"
        target.mkdir(parents=True)
        (target / "020_handoff.sql").write_text("-- done\n", encoding="utf-8")
        result = handoff.remaining(env, cwd=self.cwd)
        self.assertTrue(result["ok"], result["gaps"])
        self.assertEqual(result["gaps"], [])

    def test_closing_the_handoff_does_not_make_the_gap_disappear(self):
        # Status and completion are different questions. Someone can close a
        # handoff with work outstanding; the checks must still say so.
        env = self.contracted()
        env = handoff.append(env["id"], "close", "bob", cwd=self.cwd)
        self.assertEqual(handoff.project(env)["status"], "closed")
        self.assertFalse(handoff.remaining(env, cwd=self.cwd)["ok"])


class TestBaselineAndContractRef(HandoffTestCase):
    def test_a_non_git_workspace_still_gets_a_stable_key(self):
        one = handoff.repo_baseline(self.cwd)
        two = handoff.repo_baseline(self.cwd)
        self.assertEqual(one["key"], two["key"])
        self.assertFalse(one["git"])
        self.assertFalse(one["dirty"])      # never guessed from a failed git call

    def test_remote_path_is_keyed_by_repo_not_by_local_path(self):
        env = self.make()
        self.assertTrue(handoff.remote_path(env).startswith(
            f"{handoff.REMOTE_PREFIX}/{env['repo']['key']}/"))
        self.assertTrue(handoff.remote_path(env).endswith(f"{env['id']}.json"))

    def test_contract_drift_is_unknown_when_no_contract_was_named(self):
        self.assertIsNone(handoff.contract_drifted(self.make(), cwd=self.cwd))

    def test_contract_drift_is_detected_by_digest(self):
        import contract_store
        contract_store.propose("GET /things", {"responses": {}}, "ann", cwd=self.cwd)
        env = self.make()
        self.assertIsNotNone(env["contractRef"])
        self.assertFalse(handoff.contract_drifted(env, cwd=self.cwd))
        contract_store.propose("GET /other", {"responses": {}}, "ann", cwd=self.cwd)
        self.assertTrue(handoff.contract_drifted(env, cwd=self.cwd))


class TestGitignoreException(HandoffTestCase):
    def ignore_file(self):
        return Path(self.cwd) / ".gitignore"

    def test_nothing_happens_without_a_gitignore(self):
        self.assertFalse(paths.ensure_project_path_committable("handoff", cwd=self.cwd))

    def test_nothing_happens_when_laintas_is_not_ignored(self):
        self.ignore_file().write_text("node_modules/\n", encoding="utf-8")
        self.assertFalse(paths.ensure_project_path_committable("handoff", cwd=self.cwd))

    def test_scaffolding_is_written_once_and_is_idempotent(self):
        self.ignore_file().write_text(".laintas/\n", encoding="utf-8")
        self.assertTrue(paths.ensure_project_path_committable("handoff", cwd=self.cwd))
        text = self.ignore_file().read_text(encoding="utf-8")
        self.assertEqual(text.count(".laintas/*"), 1)
        self.assertIn("!.laintas/handoff/", text)
        self.assertFalse(paths.ensure_project_path_committable("handoff", cwd=self.cwd))
        self.assertEqual(self.ignore_file().read_text(encoding="utf-8"), text)

    def test_two_subdirectories_compose_in_either_order(self):
        # The bug this prevents: the second caller appends its own
        # `.laintas/*`, which sits *after* the first caller's exception and
        # silently re-ignores it. Order must not matter.
        for first, second in (("handoff", "contract"), ("contract", "handoff")):
            with self.subTest(first=first):
                self.ignore_file().write_text(".laintas/\n", encoding="utf-8")
                paths.ensure_project_path_committable(first, cwd=self.cwd)
                paths.ensure_project_path_committable(second, cwd=self.cwd)
                text = self.ignore_file().read_text(encoding="utf-8")
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                self.assertEqual(lines.count(".laintas/*"), 1)
                # Both exceptions must come after the single re-exclude line.
                cut = lines.index(".laintas/*")
                self.assertIn("!.laintas/handoff/", lines[cut:])
                self.assertIn("!.laintas/contract/", lines[cut:])

    def test_creating_a_handoff_makes_it_committable(self):
        self.ignore_file().write_text(".laintas/\n", encoding="utf-8")
        self.make()
        self.assertIn("!.laintas/handoff/",
                      self.ignore_file().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class TestCorruptEnvelopesDoNotTakeDownTheListing(HandoffTestCase):
    """One bad file must cost one handoff, never the whole workspace."""

    def write_raw(self, name: str, text: str):
        handoff.handoff_dir(self.cwd).mkdir(parents=True, exist_ok=True)
        (handoff.handoff_dir(self.cwd) / name).write_text(text, encoding="utf-8")

    def test_a_string_timestamp_does_not_break_sorting(self):
        # list_all sorts on createdAt and catches only HandoffError, so an
        # uncoerced string here would raise TypeError out of the listing.
        good = self.make(title="good one")
        raw = handoff.handoff_path(good["id"], self.cwd).read_text(encoding="utf-8")
        self.write_raw("bad.json", raw.replace(
            f'"createdAt": {good["createdAt"]}', '"createdAt": "yesterday"'))
        ids = [e["id"] for e in handoff.list_all(self.cwd)]
        self.assertIn(good["id"], ids)

    def test_missing_handoff_says_so(self):
        with self.assertRaises(handoff.HandoffError) as ctx:
            handoff.load("never-created-abcd1234", cwd=self.cwd)
        self.assertIn("no handoff", str(ctx.exception))

    def test_garbage_and_wrong_shapes_are_skipped_not_fatal(self):
        self.make(title="survivor")
        self.write_raw("truncated.json", '{"version": 1, "id": "x')
        self.write_raw("wrong-type.json", '"a bare string"')
        self.write_raw("no-id.json", '{"version": 1}')
        self.assertEqual(len(handoff.list_all(self.cwd)), 1)
