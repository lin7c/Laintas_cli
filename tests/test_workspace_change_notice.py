"""Tests for shell-command workspace-change detection (S4).

`git checkout -- .`, `git apply -R`, `sed -i` and friends rewrite the
workspace through shell.exec, bypassing every fs.* write path — so
peer_coordination never saw them. Two consequences, both covered here:

  * the agent that ran the command was blocked by its OWN command on the next
    fs.edit, with an error blaming "another instance";
  * the change was absent from the write log, so it could not be attributed.

The fix reports, once per change, which tracked files the command invalidated,
and deliberately leaves the read fingerprint stale so the write-side CAS still
refuses an edit made against a snapshot that is no longer on disk.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths                      # noqa: E402
import peer_coordination          # noqa: E402
import tools                      # noqa: E402


class WorkspaceChangeNoticeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # Keep the write log out of the real ~/.laintas.
        paths.INSTANCES_DIR = root / "instances"
        paths.WRITES_DIR = root / "writes"
        paths.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
        peer_coordination._coord = None
        peer_coordination._OFF_OVERRIDE = None

        self._cwd = str(root / "work")
        Path(self._cwd).mkdir(parents=True, exist_ok=True)
        self.coord = peer_coordination.get_coord()
        # Simulate the only precondition that matters: a second instance shares
        # this cwd, so fingerprint tracking and the write log are live.
        self.coord._enabled = True
        self.coord._cwd_hash = peer_coordination._cwd_hash(self._cwd)
        self.ctx = tools.ToolCtx(cwd=self._cwd)

    def tearDown(self):
        peer_coordination._coord = None
        peer_coordination._OFF_OVERRIDE = None
        paths.INSTANCES_DIR = paths.LAINTAS_HOME / "instances"
        paths.WRITES_DIR = paths.LAINTAS_HOME / "writes"
        self._tmp.cleanup()

    # ── helpers ───────────────────────────────────────────────────────
    def _file(self, name="f.py", content="original\n"):
        p = Path(self._cwd) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def _read(self, abs_path):
        """What fs.read does: record this instance's view of the file."""
        self.coord.note_read(abs_path)

    def _shell(self, command):
        return tools._bi_shell_exec({"command": command}, self.ctx)

    def _log_ops(self):
        """(op, path) pairs from today's write log, oldest first."""
        import json
        day = __import__("time").strftime("%Y%m%d")
        log = paths.WRITES_DIR / self.coord._cwd_hash / f"{day}.jsonl"
        if not log.exists():
            return []
        out = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                out.append((entry.get("op", ""), entry.get("path", "")))
        return out

    # ── the reported gap ──────────────────────────────────────────────
    def test_shell_rewrite_of_a_read_file_is_reported(self):
        f = self._file()
        self._read(f)
        res = self._shell(f"printf 'reverted\\n' > {f}")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("workspace_changed"), [os.path.realpath(f)])

    def test_notice_names_the_file_and_tells_the_model_to_re_read(self):
        f = self._file()
        self._read(f)
        note = self._shell(f"printf 'reverted\\n' > {f}").get("note") or ""
        self.assertIn("f.py", note)
        self.assertIn("stale", note)
        self.assertIn("re-read", note)

    def test_reported_once_per_change_not_once_per_command(self):
        """The bug the first implementation had: without this, every later
        command re-announced the same file and the notice became noise."""
        f = self._file()
        self._read(f)
        self._shell(f"printf 'reverted\\n' > {f}")
        for cmd in ("echo one", "echo two", "true"):
            self.assertIsNone(self._shell(cmd).get("workspace_changed"), cmd)

    def test_a_second_change_is_reported_again(self):
        f = self._file()
        self._read(f)
        self._shell(f"printf 'v2\\n' > {f}")
        res = self._shell(f"printf 'v3\\n' > {f}")
        self.assertEqual(res.get("workspace_changed"), [os.path.realpath(f)])

    def test_cas_still_refuses_an_edit_on_the_stale_snapshot(self):
        """Reporting must not refresh the fingerprint: that would let the next
        fs.edit land on content the agent never saw — the lost update this
        module exists to prevent."""
        f = self._file()
        self._read(f)
        self._shell(f"printf 'reverted\\n' > {f}")
        self.assertIsNotNone(self.coord.assert_unchanged(f))

    def test_re_reading_clears_the_block(self):
        f = self._file()
        self._read(f)
        self._shell(f"printf 'reverted\\n' > {f}")
        self.assertIsNotNone(self.coord.assert_unchanged(f))
        self._read(f)
        self.assertIsNone(self.coord.assert_unchanged(f))

    def test_change_is_attributed_in_the_write_log(self):
        f = self._file()
        self._read(f)
        self._shell(f"printf 'reverted\\n' > {f}")
        ops = [op for op, path in self._log_ops()
               if path == os.path.realpath(f) or path == f]
        self.assertTrue(any(op.startswith("shell:") for op in ops), ops)

    def test_deletion_of_a_read_file_is_reported(self):
        f = self._file()
        self._read(f)
        res = self._shell(f"rm -f {f}")
        self.assertEqual(res.get("workspace_changed"), [os.path.realpath(f)])

    # ── what must NOT be reported ─────────────────────────────────────
    def test_untouched_tracked_file_is_not_reported(self):
        f = self._file()
        g = self._file("g.py", "g\n")
        self._read(f)
        self._read(g)
        self._shell(f"printf 'reverted\\n' > {f}")
        self.assertNotIn(os.path.realpath(g),
                         self._shell("echo hi").get("workspace_changed") or [])

    def test_read_but_unmodified_file_reports_nothing(self):
        f = self._file()
        self._read(f)
        self.assertIsNone(self._shell("echo hello").get("workspace_changed"))

    def test_untracked_file_rewrite_is_not_reported(self):
        """Nobody read it, so no in-context snapshot can go stale. This is what
        keeps the scan off the repo walk: the tracked set IS the watch list."""
        f = self._file()
        self._shell(f"printf 'reverted\\n' > {f}")
        self.assertIsNone(self._shell("echo hi").get("workspace_changed"))

    def test_same_content_rewrite_is_not_reported(self):
        """A formatter or `git checkout` that lands identical bytes changed
        nothing the agent cares about."""
        f = self._file()
        self._read(f)
        self._shell(f"printf 'original\\n' > {f}")
        self.assertIsNone(self._shell("echo hi").get("workspace_changed"))

    def test_inert_while_coordination_is_off(self):
        """Single-instance default: no scan, no notice, no keys added."""
        self.coord._enabled = False
        f = self._file()
        self._read(f)
        self._shell(f"printf 'reverted\\n' > {f}")
        res = self._shell("echo hi")
        self.assertIsNone(res.get("workspace_changed"))
        self.assertNotIn("note", res)

    def test_another_actors_view_is_not_reported_as_ours(self):
        """An attached Helpwo keeps a separate fingerprint namespace; its stale
        reads are its own to resolve."""
        f = self._file()
        self.coord.note_read(f, actor="helpwo-bridge")
        self._shell(f"printf 'reverted\\n' > {f}")
        self.assertIsNone(self._shell("echo hi").get("workspace_changed"))

    def test_notice_is_bounded(self):
        for i in range(12):
            p = self._file(f"f{i}.py", f"v{i}\n")
            self._read(p)
        cmd = " && ".join(
            f"printf 'x{i}\\n' > {Path(self._cwd) / f'f{i}.py'}"
            for i in range(12))
        res = self._shell(cmd)
        changed = res.get("workspace_changed") or []
        self.assertEqual(len(changed), 12)
        self.assertIn("+4 more", res.get("note") or "")

    def test_scan_limit_caps_the_report(self):
        for i in range(5):
            self._read(self._file(f"g{i}.py", f"v{i}\n"))
        cmd = " && ".join(
            f"printf 'y{i}\\n' > {Path(self._cwd) / f'g{i}.py'}"
            for i in range(5))
        self.coord.note_shell_change(cmd)          # prime, then re-change
        cmd2 = " && ".join(
            f"printf 'z{i}\\n' > {Path(self._cwd) / f'g{i}.py'}"
            for i in range(5))
        self._shell(cmd2)
        self.assertEqual(len(self.coord.note_shell_change(cmd2, limit=2)), 0)

    def test_existing_note_is_preserved(self):
        f = self._file()
        self._read(f)
        res = self._shell(f"printf 'reverted\\n' > {f}")
        res["note"] = "prior notice"
        out = tools._notify_workspace_changes("true", self.ctx, res)
        # Second pass finds nothing new, so the prior note survives untouched.
        self.assertEqual(out.get("note"), "prior notice")

    def test_non_dict_result_passes_through(self):
        self.assertIsNone(
            tools._notify_workspace_changes("true", self.ctx, None))

    def test_coordinator_failure_never_breaks_the_command(self):
        """Detection is advisory; a broken scan must not fail the user's shell."""
        f = self._file()
        self._read(f)
        original = self.coord.note_shell_change
        self.coord.note_shell_change = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("scan exploded"))
        try:
            res = self._shell(f"printf 'reverted\\n' > {f}")
        finally:
            self.coord.note_shell_change = original
        self.assertTrue(res.get("ok"))
        self.assertIsNone(res.get("workspace_changed"))


if __name__ == "__main__":
    unittest.main()
