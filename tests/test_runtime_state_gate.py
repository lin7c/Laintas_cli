"""Runtime state under ~/.laintas is gated; reading it stays free.

The runtime's own state directory holds the live event stream, sessions,
memories, the cross-instance registry AND policy.json — the configuration
this very gate reads. Before this nothing protected it, so

    echo '{"mode":"disabled"}' > ~/.laintas/policy.json

classified as an ordinary `allow`: the approval gate could switch itself off
with one redirect. `mv ~/.laintas/sessions …`, `sed -i … events.jsonl` and
`chmod -R 000 ~/.laintas` ran with no prompt either.

Two halves, and the second is what keeps the guard usable:

  * MUTATION — must never come back `allow`.
  * READ     — must stay `allow`. Inspecting events.jsonl is routine work,
               and a guard that prompts through it gets switched off.

Deletion is deliberately absent from the mutation list: `is_delete_command`
already puts every `rm`/`find -delete` in the always-ask tier, and is covered
separately below so the two guards cannot silently drift apart.
"""

import json
import time
import unittest
from pathlib import Path
from unittest import mock

import paths
import policy


ROOT = str(paths.ROOT_HOME)            # ~/.laintas — every account profile
ACCT = str(paths.LAINTAS_HOME)         # the active profile inside it
POLICY_FILE = f"{ACCT}/policy.json"
EVENTS = f"{ACCT}/events.jsonl"

# Every one of these mutates runtime state and must never come back `allow`.
MUTATIONS = [
    ("redirect over policy.json", f"echo '{{\"mode\":\"disabled\"}}' > {POLICY_FILE}"),
    ("truncate via bare colon", f": > {POLICY_FILE}"),
    ("append to the event stream", f"echo x >> {EVENTS}"),
    ("relocate the session store", f"mv {ACCT}/sessions {ACCT}/sessions.bak"),
    ("in-place sed on events", f"sed -i 's/a/b/' {EVENTS}"),
    ("in-place sed with suffix", f"sed -i.bak s/a/b/ {EVENTS}"),
    ("combined short flags -ni", f"sed -ni p {EVENTS}"),
    ("in-place perl", f"perl -i -pe 's/a/b/' {EVENTS}"),
    ("chmod the whole tree", f"chmod -R 000 {ACCT}"),
    ("chown the policy file", f"chown root {POLICY_FILE}"),
    ("copy out of the state dir", f"cp {POLICY_FILE} /tmp/p"),
    ("tee onto the policy file", f"tee {POLICY_FILE}"),
    ("dd into the event stream", f"dd if=/dev/zero of={EVENTS}"),
    ("symlink over the policy file", f"ln -s /tmp/x {POLICY_FILE}"),
    ("rsync into the state dir", f"rsync -a /tmp/x/ {ACCT}/"),
    ("shred the event stream", f"shred {EVENTS}"),
    ("install over the policy file", f"install /tmp/x {POLICY_FILE}"),
    ("tilde-spelled target", "echo x > ~/.laintas/policy.json"),
    ("$HOME-spelled target", f"echo x > $HOME/.laintas/events.jsonl"),
    ("the state root itself", f"echo x > {ROOT}/registry.json"),
    ("wrapped in bash -c", f"bash -c 'echo x > {POLICY_FILE}'"),
    ("wrapped in sudo", f"sudo mv {ACCT}/sessions /tmp/x"),
    ("second in a chain", f"echo x > {POLICY_FILE}; echo done"),
    ("after &&", f"cat /tmp/a && echo x > {POLICY_FILE}"),
    ("with a trailing redirect", f"echo x > {POLICY_FILE} > /dev/null"),
]

# Variable indirection. A literal path getting refused makes an assignment the
# first thing an agent reaches for, so these are the cases that matter most.
INDIRECT = [
    ("assigned then redirected", f"P={POLICY_FILE}; echo x > $P"),
    ("braced expansion", f"P=~/.laintas/policy.json; echo '{{}}' > ${{P}}"),
    ("exported", f"export P={POLICY_FILE}; cp /tmp/x $P"),
    ("dir assigned, file appended", f"P={ROOT}; tee $P/policy.json"),
    ("subpath of a variable", f"P={ROOT}; echo x > $P/registry.json"),
    ("loop variable relocated", f"for f in {ACCT}/*.json; do mv $f /tmp/x; done"),
    ("in-place edit via variable", f"T={EVENTS}; sed -i 's/a/b/' $T"),
    ("chmod via variable", f"D={ACCT}; chmod -R 000 $D"),
]

# Everyday work. Every one of these must stay `allow` — including the ones
# chosen to look like a mutation and not be one.
READS = [
    ("read the event stream", f"cat {EVENTS}"),
    ("read it into a temp file", f"cat {EVENTS} > /tmp/out"),
    ("list the state dir", f"ls {ACCT}"),
    ("search the state dir", f"grep x {EVENTS}"),
    ("read-only sed", f"sed 's/a/b/' {EVENTS}"),
    ("sed -n line range", f"sed -n '1,10p' {EVENTS}"),
    ("sed -e script", f"sed -e 's/a/b/' {EVENTS}"),
    ("read-only perl", f"perl -ne 'print' {EVENTS}"),
    ("case-insensitive grep", f"grep -i x {EVENTS}"),
    ("tail a log elsewhere", "tail -f /root/laintas_cli/x.log"),
    ("echo $HOME", "echo $HOME"),
]

ORDINARY = [
    ("write outside the state dir", "echo hi > /tmp/out"),
    ("edit a source file", "sed 's/a/b/' tools.py"),
    ("move temp files", "mv /tmp/a /tmp/b"),
    ("chmod a source file", "chmod +x tools.py"),
    ("copy temp files", "cp /tmp/a /tmp/b"),
    ("variable bound elsewhere", "P=/tmp/a; echo x > $P"),
    ("loop over temp files", "for f in /tmp/*.json; do mv $f /tmp/x; done"),
    ("chmod via unrelated variable", "D=/root/laintas_cli; chmod -R 000 $D"),
    ("git status", "git status"),
    ("assignment-heavy build", "CC=gcc CFLAGS=-O2 LDFLAGS=-lm A=1 B=2 make all"),
    ("inline env assignments", "A=1 B=2 C=3 echo hi"),
    ("sibling dir, not state", "echo x > /root/.laintasX/evil.json"),
    ("traversal escaping state", f"echo x > {ROOT}/../../etc/hostname"),
]


def _cfg(mode: str) -> dict:
    """A policy config in *mode*, cloned from the shipped defaults."""
    cfg = json.loads(json.dumps(policy._DEFAULT_CONFIG))
    cfg["mode"] = mode
    return cfg


class _PolicyCase(unittest.TestCase):
    """Shared isolation: no audit writes, no config reads from disk."""

    def setUp(self):
        # Keep tilde/$HOME examples meaningful with an isolated LAINTAS_HOME.
        home = mock.patch.object(policy.Path, "home", return_value=Path(ROOT).parent)
        home.start()
        self.addCleanup(home.stop)
        patcher = mock.patch.object(policy, "_write_audit", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_mode("audit")

    def set_mode(self, mode: str):
        patcher = mock.patch.object(policy, "_load_config", lambda: _cfg(mode))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestStatePathDetection(_PolicyCase):

    def test_state_paths_recognized(self):
        for label, path in [
            ("policy file", POLICY_FILE),
            ("event stream", EVENTS),
            ("nested session", f"{ACCT}/sessions/x.json"),
            ("account root", ROOT),
            ("active profile", ACCT),
            ("registry", f"{ROOT}/registry.json"),
        ]:
            with self.subTest(label):
                self.assertTrue(policy.is_runtime_state_path(path))

    def test_non_state_paths_rejected(self):
        for label, path in [
            ("repo source", "/root/laintas_cli/tools.py"),
            ("temp file", "/tmp/out.txt"),
            ("sibling of the state root", "/root/.laintasX/evil.json"),
            ("traversal escaping state", f"{ROOT}/../../etc/passwd"),
        ]:
            with self.subTest(label):
                self.assertFalse(policy.is_runtime_state_path(path))

    def test_root_home_covers_every_account_profile(self):
        """ROOT_HOME must gate *all* profiles, not just the active one.

        Gating only LAINTAS_HOME would leave `rm -rf ~/.laintas` — every
        account, every session — as an ordinary command, and a `~`-spelled
        target would resolve outside the guard.
        """
        with mock.patch.object(paths, "LAINTAS_HOME", Path(ROOT) / "accounts/active-profile"):
            self.assertTrue(policy.is_runtime_state_path(f"{ROOT}/accounts/other-profile"))
            self.assertTrue(policy.is_runtime_state_path(f"{ROOT}/accounts"))
            self.assertTrue(policy.is_runtime_state_path(str(paths.LAINTAS_HOME)))

    def test_policy_config_path_identified(self):
        self.assertTrue(policy.is_policy_config_path(POLICY_FILE))
        self.assertTrue(policy.is_policy_config_path(
            str(paths.account_path("POLICY_FILE"))))
        self.assertFalse(policy.is_policy_config_path(EVENTS))
        self.assertFalse(policy.is_policy_config_path("/root/laintas_cli/policy.py"))


class TestShellMutationDetection(_PolicyCase):

    def test_mutations_detected(self):
        for label, command in MUTATIONS:
            with self.subTest(label):
                self.assertTrue(policy.is_runtime_state_mutation(command), command)

    def test_variable_indirection_detected(self):
        for label, command in INDIRECT:
            with self.subTest(label):
                self.assertTrue(policy.is_runtime_state_mutation(command), command)

    def test_reads_stay_free(self):
        for label, command in READS:
            with self.subTest(label):
                self.assertFalse(policy.is_runtime_state_mutation(command), command)

    def test_ordinary_commands_stay_free(self):
        for label, command in ORDINARY:
            with self.subTest(label):
                self.assertFalse(policy.is_runtime_state_mutation(command), command)


class TestShellGate(_PolicyCase):
    """policy.evaluate() must ask in BOTH modes, not just enforce."""

    def test_mutations_never_allowed_in_audit_mode(self):
        self.set_mode("audit")
        for label, command in MUTATIONS + INDIRECT:
            with self.subTest(label):
                self.assertNotEqual(
                    policy.evaluate(command, cwd="/root/laintas_cli").action,
                    "allow", command)

    def test_mutations_never_allowed_in_enforce_mode(self):
        self.set_mode("enforce")
        for label, command in MUTATIONS + INDIRECT:
            with self.subTest(label):
                self.assertNotEqual(
                    policy.evaluate(command, cwd="/root/laintas_cli").action,
                    "allow", command)

    def test_reads_allowed_in_audit_mode(self):
        self.set_mode("audit")
        for label, command in READS:
            with self.subTest(label):
                self.assertEqual(
                    policy.evaluate(command, cwd="/root/laintas_cli").action,
                    "allow", command)

    def test_reason_names_the_state_dir(self):
        decision = policy.evaluate(f"echo x > {POLICY_FILE}", cwd="/root/laintas_cli")
        self.assertEqual(decision.action, "needs_approval")
        self.assertIn(".laintas", decision.reason)

    def test_descriptor_and_force_redirections_require_approval(self):
        for mode in ("audit", "enforce"):
            self.set_mode(mode)
            for operator in (">", ">>", "1>", "2>", "1>>", "2>>", ">|", "1>|", "&>", "&>>", ">&"):
                with self.subTest(mode=mode, operator=operator):
                    command = f"echo x {operator} '{POLICY_FILE}'"
                    self.assertEqual(policy.evaluate(command, cwd="/tmp").action,
                                     "needs_approval", command)
        self.assertFalse(policy.is_runtime_state_mutation("echo x 2>&1", "/tmp"))

    def test_relative_writes_use_execution_directory_and_cd(self):
        cases = [
            ("echo x > policy.json", ACCT),
            ("P=policy.json; echo x > $P", ACCT),
            ("sed -i s/a/b/ events.jsonl", ACCT),
            (f"echo x > {Path(ROOT).name}/registry.json", str(Path(ROOT).parent)),
            (f"echo x > ./{Path(ROOT).name}/registry.json", str(Path(ROOT).parent)),
            (f"cd {ACCT}; echo x > policy.json", "/tmp"),
            (f"cd -- '{ACCT}' && echo x > policy.json", "/tmp"),
            (f"D={ACCT}; cd $D; echo x > policy.json", "/tmp"),
            (f"cd {ACCT}; cd .; echo x > ./policy.json", "/tmp"),
            (f"bash -c 'cd {ACCT}; echo x > policy.json'", "/tmp"),
        ]
        for mode in ("audit", "enforce"):
            self.set_mode(mode)
            for command, cwd in cases:
                with self.subTest(mode=mode, command=command):
                    self.assertEqual(policy.evaluate(command, cwd=cwd).action,
                                     "needs_approval", command)
        self.assertFalse(policy.is_runtime_state_mutation("echo x > policy.json", "/tmp"))
        self.assertFalse(policy.is_runtime_state_mutation("cat events.jsonl > /tmp/out", ACCT))


class TestFileWriteGate(_PolicyCase):
    """policy.evaluate_file_write() — the fs.write / fs.edit path."""

    def test_state_write_needs_approval_in_audit_mode(self):
        self.set_mode("audit")
        for label, path in [("policy file", POLICY_FILE), ("events", EVENTS),
                            ("nested session", f"{ACCT}/sessions/x.json"),
                            ("registry", f"{ROOT}/registry.json")]:
            with self.subTest(label):
                decision = policy.evaluate_file_write(path, cwd="/root/laintas_cli")
                self.assertEqual(decision.action, "needs_approval")

    def test_state_write_needs_approval_in_enforce_mode(self):
        self.set_mode("enforce")
        decision = policy.evaluate_file_write(EVENTS, cwd="/root/laintas_cli")
        self.assertEqual(decision.action, "needs_approval")

    def test_policy_file_gets_its_own_reason(self):
        """Rewriting policy.json can disable the gate, so say so explicitly."""
        decision = policy.evaluate_file_write(POLICY_FILE, cwd="/root/laintas_cli")
        self.assertEqual(decision.action, "needs_approval")
        self.assertIn("policy file", decision.reason)

    def test_ordinary_write_unaffected(self):
        decision = policy.evaluate_file_write(
            "/root/laintas_cli/tools.py", cwd="/root/laintas_cli")
        self.assertEqual(decision.action, "allow")

    def test_sandbox_grant_does_not_reopen_state(self):
        """A worktree write grant must never cover the runtime's own state.

        Grants exist so disposable sandboxes can write without a prompt; they
        are evaluated after the deny loop for exactly this reason.
        """
        granted = policy.grant_file_write_prefix(ROOT)
        self.addCleanup(policy.revoke_file_write_prefix, granted)
        self.assertTrue(policy._sandbox_write_allowed(POLICY_FILE))
        decision = policy.evaluate_file_write(POLICY_FILE, cwd="/root/laintas_cli")
        self.assertEqual(decision.action, "needs_approval")


class TestDeletionStillCovered(_PolicyCase):
    """The mutation guard omits rm on purpose; prove the other guard holds."""

    def test_delete_of_state_is_gated(self):
        for label, command in [
            ("rm the active profile", f"rm -rf {ACCT}"),
            ("rm every account", f"rm -rf {ROOT}/accounts"),
            ("rm the whole state dir", f"rm -rf {ROOT}"),
            ("rm one state file", f"rm {POLICY_FILE}"),
            ("find -delete over state", f"find {ACCT} -delete"),
            ("sudo rm", f"sudo rm -rf {ROOT}"),
        ]:
            with self.subTest(label):
                self.assertTrue(policy.is_delete_command(command), command)
                self.assertNotEqual(
                    policy.evaluate(command, cwd="/root/laintas_cli").action,
                    "allow", command)


class TestHotPathCost(_PolicyCase):
    """This runs on every shell command, so a blow-up here is a real regression.

    The bound is deliberately loose: it exists to catch quadratic regex
    backtracking, not to police microseconds on a loaded machine.
    """

    def test_assignment_heavy_command_is_not_quadratic(self):
        command = ("CC=gcc CFLAGS=-O2 LDFLAGS=-lm A=1 B=2 C=3 D=4 E=5 F=6 "
                   "G=7 H=8 I=9 J=10 K=11 L=12 make all")
        policy.is_runtime_state_mutation(command)          # warm the roots cache
        start = time.perf_counter()
        for _ in range(200):
            policy.is_runtime_state_mutation(command)
        per_call_us = (time.perf_counter() - start) / 200 * 1e6
        self.assertLess(per_call_us, 5000,
                        f"{per_call_us:.0f}us per call — regex backtracking?")

    def test_roots_are_cached_across_calls(self):
        first = policy._runtime_state_roots()
        second = policy._runtime_state_roots()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
