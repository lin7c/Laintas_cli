"""E4 (bughunt): PowerShell flags are tokens, not prefixes.

`startswith("-e")` swallowed -ExecutionPolicy/-ErrorAction, and
`startswith("-c")` swallowed -ComputerName: the next word was base64-
decoded as an EncodedCommand payload, replacing a legitimate command
with mojibake the policy rules could never match (and flagging
UNRESOLVED on a perfectly resolvable command). The fix matches the
flag token exactly, allowing PowerShell's `-flag:value` form.

Note: `opaque_payload` on `-File script.ps1` is legitimate (a script
file IS opaque to the analyzer) and unrelated to this bug.
"""
import unittest

import command_parse as cp


def _mojibake_variants(cmd: str) -> list:
    """Variants other than the original that contain non-ASCII — the
    signature of a bogus base64 decode."""
    return [v for v in cp.effective_commands(cmd)[1:]
            if any(ord(c) > 127 for c in v)]


class PsFlagTokenTests(unittest.TestCase):
    def test_executionpolicy_not_treated_as_encoded(self):
        cmd = "powershell -ExecutionPolicy Bypass -File script.ps1"
        self.assertEqual(_mojibake_variants(cmd), [])
        # and no bogus decoded variant replaces the real command
        self.assertEqual(cp.effective_commands(cmd), [cmd])

    def test_erroraction_not_treated_as_encoded(self):
        cmd = "pwsh -ErrorAction Continue Get-ChildItem"
        self.assertEqual(_mojibake_variants(cmd), [])
        # Get-ChildItem must survive as the command text, not be replaced
        self.assertIn("Get-ChildItem", cp.effective_commands(cmd)[0])

    def test_computername_not_treated_as_command_flag(self):
        cmd = "powershell -ComputerName host Get-Service"
        self.assertEqual(_mojibake_variants(cmd), [])
        self.assertIn("Get-Service", cp.effective_commands(cmd)[0])

    def test_real_encoded_command_still_decodes(self):
        # "$ec`o" utf-16-le base64: the genuine -e path must keep working.
        import base64
        payload = base64.b64encode("$ec`o".encode("utf-16-le")).decode()
        cmd = f"powershell -e {payload}"
        variants = cp.effective_commands(cmd)
        self.assertIn("$ec`o", variants)

    def test_long_flag_forms_still_decode(self):
        import base64
        payload = base64.b64encode("Write-Host hi".encode("utf-16-le")).decode()
        for flag in ("-encodedcommand", "-enc"):
            variants = cp.effective_commands(f"powershell {flag} {payload}")
            self.assertIn("Write-Host hi", variants)

    def test_inline_flag_value_form_decodes(self):
        import base64
        payload = base64.b64encode("Write-Host hi".encode("utf-16-le")).decode()
        variants = cp.effective_commands(f"powershell -enc:{payload}")
        self.assertIn("Write-Host hi", variants)

    def test_abbreviated_payload_flags_still_resolve(self):
        # PowerShell accepts abbreviations; an exact-token match let `-Encod`
        # and `-Comm` skip payload analysis with no risk flag at all, which
        # downgraded a hard deny to an approvable prompt.
        import base64
        import policy
        payload = base64.b64encode("rm -rf /".encode("utf-16-le")).decode()
        for flag in ("-Encod", "-EncodedC", "-e", "-ec", "-enc"):
            self.assertIn("rm -rf /", cp.effective_commands(
                f"powershell.exe {flag} {payload}"), flag)
        for flag in ("-Comm", "-co", "-c", "-Command"):
            self.assertIn("rm -rf /", cp.effective_commands(
                f'powershell.exe {flag} "rm -rf /"'), flag)
        self.assertEqual(policy.evaluate(
            f"powershell.exe -Encod {payload}").action, "deny")

    def test_file_abbreviation_is_opaque(self):
        _payload, risks = cp._windows_payload("powershell.exe", ["-f", "x.ps1"])
        self.assertIn(cp.RISK_UNRESOLVED, risks)


if __name__ == "__main__":
    unittest.main()
