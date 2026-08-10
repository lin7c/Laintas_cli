import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend_profiles
import skills


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self.payload


class _Requests:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def _catalog():
    return {
        "schema_version": 1,
        "skills": [{
            "name": "ppos-authoring",
            "description": "Create and publish interactive PPOS works.",
            "revision": "gateway-0123456789ab",
            "sha256": "a" * 64,
            "manual": "# PPOS authoring\n\nUse the native PPOS tools.",
            "clients": {"laintas_cli": {"trigger_patterns": ["PPOS", "ppos-app"]}},
        }],
    }


class GatewaySkillSyncTests(unittest.TestCase):
    def test_installs_only_a_managed_documentation_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = _Requests(_catalog())
            profile = backend_profiles.BackendProfile(
                "test", "official", "https://laintas.com")
            with mock.patch.object(skills, "SKILLS_DIR", root), \
                    mock.patch.object(skills.backend_profiles, "resolve", return_value=profile):
                result = skills.sync_gateway_skills(
                    {"userId": "u", "headers": {"Authorization": "Bearer token"}},
                    requests_module=fake,
                )

            target = root / "ppos-authoring"
            self.assertTrue(result[0][1])
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertFalse((target / "skill.py").exists())
            self.assertFalse((target / "extension.json").exists())
            marker = json.loads((target / skills.MANAGED_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["managed_by"], "gateway")
            text = (target / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("version: gateway-0123456789ab", text)
            self.assertIn("  - \"ppos-app\"", text)
            self.assertEqual(fake.calls[0][1]["headers"]["Authorization"], "Bearer token")

    def test_user_owned_skill_with_same_name_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "ppos-authoring"
            target.mkdir(parents=True)
            custom = "---\nname: ppos-authoring\ndescription: Mine\n---\nDo not replace.\n"
            (target / "SKILL.md").write_text(custom, encoding="utf-8")
            fake = _Requests(_catalog())
            profile = backend_profiles.BackendProfile(
                "test", "official", "https://laintas.com")
            with mock.patch.object(skills, "SKILLS_DIR", root), \
                    mock.patch.object(skills.backend_profiles, "resolve", return_value=profile):
                result = skills.sync_gateway_skills(
                    {"userId": "u"}, requests_module=fake)

            self.assertTrue(result[0][1])
            self.assertIn("left alone", result[0][2])
            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), custom)
            self.assertFalse((target / skills.MANAGED_MARKER).exists())


if __name__ == "__main__":
    unittest.main()
