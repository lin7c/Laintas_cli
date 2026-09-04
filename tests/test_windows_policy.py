"""The policy engine on the platform it actually ships to on Windows.

The Windows build is not a native port: the installer registers a private WSL
distribution and runs the Linux binary inside it, with `[interop] enabled` and
`appendWindowsPath=true` in wsl.conf. So from any shell call the agent makes,
`powershell.exe`, `cmd.exe` and every Windows binary on the user's PATH are one
word away — and every rule in policy.py was written against Linux command
names. `powershell.exe -Command "Remove-Item -Recurse -Force …"` matched
nothing: not the delete tier, not an approval rule, not a deny rule.

These pin the boundary as it exists on that platform: the wrapper is unwrapped,
the payload is judged, and the user's real files behind /mnt are never inside
an allowed root.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import command_parse
import laintas_cli
import policy


def _isolated_policy(mode: str = "enforce", **extra) -> Path:
    """Point the module at a throwaway config so tests never read the user's."""
    path = Path(tempfile.mkdtemp()) / "policy.json"
    path.write_text(json.dumps({"mode": mode, **extra}), encoding="utf-8")
    policy.CONFIG_PATH = path
    policy._config = None
    policy._config_mtime = 0.0
    return path


class WindowsWrapperParsingTests(unittest.TestCase):
    """A launcher name is not a command. The payload is."""

    def test_powershell_command_payload_is_visible(self):
        analysis = command_parse.analyze(
            'powershell.exe -Command "Remove-Item -Recurse -Force C:/build"')
        self.assertIn("Remove-Item -Recurse -Force C:/build", analysis.commands)

    def test_cmd_slash_c_payload_is_visible(self):
        analysis = command_parse.analyze("cmd.exe /c del /f /s /q C:/tmp")
        self.assertTrue(any(c.startswith("del ") for c in analysis.commands))

    def test_an_encoded_command_is_decoded_rather_than_shrugged_at(self):
        # base64 of UTF-16LE "rm -rf /" — the deliberate way to hide a payload.
        analysis = command_parse.analyze(
            "powershell -enc cgBtACAALQByAGYAIAAvAA==")
        self.assertIn("rm -rf /", analysis.commands)

    def test_an_undecodable_payload_is_reported_unresolved(self):
        analysis = command_parse.analyze("powershell.exe -File install.ps1")
        self.assertIn(command_parse.RISK_UNRESOLVED, analysis.risks)

    def test_a_windows_program_is_one_program_however_it_is_written(self):
        self.assertEqual(command_parse._program_name("C:\\Windows\\CMD.EXE"),
                         "cmd.exe")
        # Linux names stay case-sensitive: Make is not make.
        self.assertEqual(command_parse._program_name("/usr/bin/Make"), "Make")


class WindowsCommandDecisionTests(unittest.TestCase):
    def setUp(self):
        _isolated_policy("enforce")

    def decide(self, command):
        return policy.evaluate(command, cwd="/home/laintas/project")

    def test_a_windows_delete_is_in_the_always_ask_tier(self):
        for command in ("cmd.exe /c del /f /s /q C:/Users/me/Documents",
                        'powershell.exe -Command "Remove-Item -Recurse ./build"',
                        "del report.txt"):
            self.assertEqual(self.decide(command).action, "needs_approval",
                             command)

    def test_a_wrapped_posix_delete_is_too(self):
        """The same hole existed on Linux: quoting hid rm from the tier."""
        decision = self.decide('bash -c "rm -rf ./build"')
        self.assertEqual(decision.action, "needs_approval")
        self.assertIn("delete command", decision.reason)

    def test_the_irreversible_windows_commands_are_denied(self):
        for command in ("format C: /q",
                        "vssadmin.exe delete shadows /all",
                        "wsl.exe --unregister Laintas-CLI",
                        'powershell -Command "Set-MpPreference '
                        '-DisableRealtimeMonitoring $true"'):
            self.assertEqual(self.decide(command).action, "deny", command)

    def test_an_encoded_rm_is_denied_through_the_wrapper(self):
        decision = self.decide("powershell.exe -enc cgBtACAALQByAGYAIAAvAA==")
        self.assertEqual(decision.action, "deny")
        self.assertIn("obfuscated", decision.reason)

    def test_ordinary_powershell_still_asks_rather_than_running_silently(self):
        self.assertEqual(self.decide('powershell.exe -Command "Get-Process"')
                         .action, "needs_approval")

    def test_linux_work_is_not_collateral_damage(self):
        for command in ("ls -la", "git status", "python3 -m pytest -q"):
            self.assertEqual(self.decide(command).action, "allow", command)


class WindowsPathTests(unittest.TestCase):
    def setUp(self):
        _isolated_policy("enforce")

    def test_a_drive_path_is_never_inside_a_linux_root(self):
        """It used to be: joined to cwd, `C:\\Users\\me` landed under it."""
        decision = policy._check_paths(
            "del /f /s /q C:\\Users\\me\\Documents",
            "/home/laintas/project", ["/home/laintas"])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "needs_approval")
        self.assertIn("C:\\Users\\me\\Documents", decision.reason)

    def test_the_mounted_windows_side_is_outside_the_roots(self):
        decision = policy._check_paths(
            "cp x.md /mnt/c/Users/me/Desktop/x.md",
            "/home/laintas/project", ["/home/laintas", "/tmp"])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "needs_approval")

    def test_cmd_switches_are_not_read_as_absolute_paths(self):
        """`/s` is a switch. Reporting it as a path made every delete noisy."""
        self.assertNotIn("/s", policy._extract_paths("del /f /s /q report.txt"))
        self.assertNotIn("/MIR", policy._extract_paths(
            "robocopy /MIR C:\\a C:\\b"))

    def test_a_windows_path_survives_extraction_intact(self):
        self.assertEqual(
            policy._extract_paths('del /q "C:\\Users\\me\\My Docs"'),
            ["C:\\Users\\me\\My Docs"])

    def test_windows_side_secrets_are_denied_for_writes(self):
        for path in ("/mnt/c/Users/me/.ssh/id_rsa",
                     "/mnt/c/Users/me/AppData/Roaming/Microsoft/Crypto/k",
                     "/mnt/c/Windows/System32/drivers/etc/hosts"):
            self.assertEqual(
                policy.evaluate_file_write(path, "/home/laintas").action,
                "deny", path)


class AllowedRootsTests(unittest.TestCase):
    def test_the_defaults_describe_this_machine_not_a_developer_box(self):
        roots = policy._default_allowed_roots()
        self.assertIn(str(Path.home()), roots)
        self.assertNotIn("/root/Helpwo", roots)
        # The Windows side stays outside: a write there is worth one question.
        self.assertFalse(any(r.startswith("/mnt/") for r in roots))

    def test_a_config_written_before_windows_support_inherits_the_rules(self):
        """Existing users get these by migration, not only new installs."""
        path = _isolated_policy(
            "enforce", allow=[], needs_approval=[r"^git\s+push"],
            deny=[r"^rm\s+-rf\s+/"], denyFileWrite=[r"\.env$"])
        policy._load_config()
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(any(r.startswith("(?i)") for r in saved["deny"]))
        self.assertTrue(any(r.startswith("(?i)")
                            for r in saved["needs_approval"]))
        self.assertTrue(any(".ssh" in r for r in saved["denyFileWrite"]))


class PolicyCheckToolTests(unittest.TestCase):
    """The agent may ask what the policy says. It may never change it."""

    @classmethod
    def setUpClass(cls):
        import tools
        tools.register_builtin_tools()
        cls.tools = tools

    def setUp(self):
        _isolated_policy("enforce")

    def test_it_answers_for_a_command_without_running_it(self):
        out = self.tools.get_registry().invoke(
            "policy.check", {"command": "rm -rf build"},
            self.tools.ToolCtx(cwd=os.getcwd()))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["command"]["action"], "needs_approval")
        self.assertTrue(out["result"]["command"]["always_asks"])

    def test_it_answers_for_a_write_target(self):
        out = self.tools.get_registry().invoke(
            "policy.check", {"write_path": "/etc/hosts"},
            self.tools.ToolCtx(cwd=os.getcwd()))
        self.assertIn(out["result"]["write"]["action"],
                      {"deny", "needs_approval"})

    def test_it_needs_something_to_check(self):
        out = self.tools.get_registry().invoke(
            "policy.check", {}, self.tools.ToolCtx(cwd=os.getcwd()))
        self.assertFalse(out["ok"])

    def test_no_tool_can_write_policy(self):
        """The absence is the feature — keep it absent."""
        names = [t.name for t in self.tools.get_registry().list()]
        writers = [n for n in names
                   if n.startswith("policy.") and n != "policy.check"]
        self.assertEqual(writers, [], f"policy is writable by a tool: {writers}")


if __name__ == "__main__":
    unittest.main()


class HostLabelTests(unittest.TestCase):
    """The banner named the shell's kernel, not the user's computer.

    On the Windows product `platform.system()` answers "Linux", which is true
    of the shell and wrong about the machine: the user is at a Windows PC and
    a startup line reading "Linux" says the installer put something else on
    it. The model's own OS line stays "Linux (private WSL 2 distribution…)" —
    it has to know the shell is Linux — so the two cannot share one string.
    """

    def test_the_windows_product_calls_itself_windows(self):
        with mock.patch.dict(os.environ, {"LAINTAS_HOST": "windows"}):
            self.assertEqual(laintas_cli.host_label(), "Windows (WSL 2)")

    def test_a_renamed_distribution_is_still_the_windows_product(self):
        environment = {"WSL_DISTRO_NAME": "Laintas-CLI"}
        with mock.patch.dict(os.environ, environment, clear=False):
            os.environ.pop("LAINTAS_HOST", None)
            self.assertEqual(laintas_cli.host_label(), "Windows (WSL 2)")

    def test_a_plain_linux_install_is_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAINTAS_HOST", None)
            os.environ.pop("WSL_DISTRO_NAME", None)
            self.assertEqual(laintas_cli.host_label(), laintas_cli.SYSTEM)


class HeredocBodyTests(unittest.TestCase):
    """A heredoc body is what gets written, not what gets run.

    Parsing it as commands made every generated script look like a chain of
    unknown programs: a PowerShell body opening with `$apps = …` read as "a
    command whose name comes from a variable", so policy demanded approval for
    writing a file. Three approvals in a row for the same harmless write is
    what teaches an agent that acting is expensive and asking is cheap.
    """

    def test_writing_a_script_is_not_an_unresolvable_command(self):
        analysis = command_parse.analyze(
            "cat << 'EOF' > report.ps1\n"
            "$apps = Get-ItemProperty HKLM:\\Software\\*\n"
            "$apps | Export-Csv out.csv\n"
            "EOF")
        self.assertNotIn(command_parse.RISK_UNRESOLVED, analysis.risks)
        self.assertEqual(["cat << EOF > report.ps1"], analysis.commands)

    def test_body_text_is_not_matched_as_a_command(self):
        """Otherwise a file's *contents* are judged as if they would run."""
        analysis = command_parse.analyze(
            "cat << 'EOF' > notes.txt\nrm -rf /\nEOF")
        self.assertNotIn("rm -rf /", analysis.commands)

    def test_a_body_piped_into_a_shell_is_still_a_script(self):
        """`cat <<EOF | bash` is the case where the body really does run."""
        analysis = command_parse.analyze(
            "cat << 'EOF' | bash\nrm -rf /tmp/victim\nEOF")
        self.assertIn("rm -rf /tmp/victim", analysis.commands)

    def test_an_unquoted_delimiter_still_expands_substitutions(self):
        """`<<EOF` (unquoted) runs `$( … )` while writing the file."""
        analysis = command_parse.analyze(
            "cat <<EOF > out\nowner=$(whoami)\nEOF")
        self.assertIn("whoami", analysis.commands)
        self.assertIn(command_parse.RISK_SUBSTITUTION, analysis.risks)


class OpaquePayloadTests(unittest.TestCase):
    """"Unreadable" has two shapes and the prompt has to name the right one."""

    def test_a_script_file_is_not_reported_as_a_substitution(self):
        analysis = command_parse.analyze("powershell.exe -File ./report.ps1")
        self.assertIn(command_parse.RISK_UNRESOLVED, analysis.risks)
        self.assertIn(command_parse.RISK_OPAQUE_PAYLOAD, analysis.risks)

        _isolated_policy("enforce")
        decision = policy.evaluate("powershell.exe -File ./report.ps1")
        self.assertEqual("needs_approval", decision.action)
        self.assertIn("cannot be read", decision.reason)
        self.assertNotIn("produced at runtime", decision.reason)

    def test_a_real_substitution_keeps_its_own_message(self):
        _isolated_policy("enforce")
        decision = policy.evaluate("$(echo rm) -rf /tmp/x")
        self.assertEqual("needs_approval", decision.action)
        self.assertIn("produced at runtime", decision.reason)


class PowerShellQuotingTests(unittest.TestCase):
    """Two shells on one line, and bash goes first.

    `-Command "… { $_.DisplayName } …"` reaches PowerShell with `$_` already
    replaced by bash -- with the previous command's last argument, which in
    this runtime is the internal marker it echoes before every command. What
    ran was valid PowerShell about a name nobody wrote, exit code 0, and
    several screens of cmdlet-not-found noise blamed on the cmdlet.
    """

    def test_double_quoted_powershell_variables_are_caught(self):
        self.assertEqual(
            "$_",
            command_parse.powershell_expansion_conflict(
                'powershell.exe -Command "Get-X | ? { $_.Name }"'))
        self.assertEqual(
            "$env:",
            command_parse.powershell_expansion_conflict(
                'powershell.exe -Command "ls $env:LOCALAPPDATA"'))

    def test_single_quotes_are_the_fix_and_are_not_flagged(self):
        self.assertEqual(
            "",
            command_parse.powershell_expansion_conflict(
                "powershell.exe -Command 'Get-X | ? { $_.Name }'"))

    def test_bash_variables_in_ordinary_commands_are_left_alone(self):
        for command in ('echo "$_ was the last argument"',
                        'grep "$PATH" file',
                        'powershell.exe -File ./a.ps1'):
            self.assertEqual(
                "", command_parse.powershell_expansion_conflict(command),
                command)
