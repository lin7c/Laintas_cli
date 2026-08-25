import json
import tempfile
import unittest
from pathlib import Path

import extension_scanner


class ExtensionScannerTests(unittest.TestCase):
    def test_deterministic_scan_finds_process_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text(
                "import subprocess\nsubprocess.run(['whoami'])\n", encoding="utf-8")
            report = extension_scanner.deterministic_scan(root)
            self.assertEqual(report.risk, "high")
            self.assertTrue(any(item.category == "process-execution"
                                for item in report.findings))

    def test_ai_scan_merges_risk_and_uses_toolless_callback_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def setup(ctx):\n    pass\n", encoding="utf-8")
            captured = {}

            def invoke(system, prompt):
                captured["system"] = system
                captured["prompt"] = prompt
                return {"reply": json.dumps({
                    "risk": "medium",
                    "findings": [{
                        "severity": "medium", "file": "main.py", "line": 1,
                        "category": "review", "description": "Manual review suggested."
                    }],
                    "summary": "Review completed."
                })}

            report = extension_scanner.ai_scan(root, invoke)
            self.assertEqual(report.risk, "medium")
            self.assertIn("untrusted data", captured["system"])
            self.assertIn("sourceFiles", captured["prompt"])

    def test_ai_scan_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("pass\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                extension_scanner.ai_scan(root, lambda _system, _prompt: {"error": True})


if __name__ == "__main__":
    unittest.main()
