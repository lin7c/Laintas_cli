"""Password vault: storage, model-facing tools, and entry boundaries.

Pins the vault's core contract:

  * storage — AEAD roundtrip, tamper/truncation rejection, atomic writes,
    autolock, passphrase change, origin canonicalization;
  * password.list — public metadata only, works while locked, no code path
    to secrets;
  * password.fill — fail-closed `broker_unavailable`, never a silent
    fallback to the ordinary model-scriptable browser;
  * /password entry — no arguments accepted at the dispatch layer, and the
    UI refuses non-local (no controlling TTY) and injected (Helpwo/
    extension) entry.

All credentials in this file are generated dummies.
"""

import base64
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock

import pytest
import password_vault as pv
import tools

PASS = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _close_test_vaults(monkeypatch):
    """Do not leave any task-owned autolock timers after a test."""
    instances = []
    original = pv.PasswordVault.__init__

    def tracked(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(pv.PasswordVault, "__init__", tracked)
    yield
    timers = [t for t in threading.enumerate() if t.name == "password-vault-autolock"]
    for vault in instances:
        vault.lock()
    for timer in timers:
        timer.join(timeout=2)
        assert not timer.is_alive()


class VaultDirTest(unittest.TestCase):
    """Each test gets its own throwaway vault directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vault-test-")
        self.vault = pv.PasswordVault(os.path.join(self.dir, "vault"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestStorage(VaultDirTest):

    def test_create_add_roundtrip(self):
        self.vault.create(PASS)
        self.assertTrue(self.vault.is_unlocked())
        eid = self.vault.add_entry(
            "Test site login", ["https://example.com", "https://example.com:8443"],
            "dummy-user", "dummy-password-123", "note-text")
        self.assertRegex(eid, r"^cred_[0-9a-f]{12}$")
        secret = self.vault.get_secret(eid)
        self.assertEqual(secret["username"], "dummy-user")
        self.assertEqual(secret["password"], "dummy-password-123")
        self.assertEqual(secret["notes"], "note-text")

    def test_listing_and_disk_contain_no_secrets(self):
        self.vault.create(PASS)
        eid = self.vault.add_entry("Test site", ["https://example.com"],
                                   "dummy-user", "dummy-password-123", "note")
        listing = json.dumps(self.vault.list_entries())
        blob = open(self.vault._blob_path, "rb").read()
        meta = open(self.vault._meta_path, "rb").read()
        for needle in ("dummy-user", "dummy-password-123", "note"):
            self.assertNotIn(needle, listing)
            self.assertNotIn(needle.encode(), blob)
            self.assertNotIn(needle.encode(), meta)

    def test_permissions_and_magic(self):
        self.vault.create(PASS)
        self.assertTrue(open(self.vault._blob_path, "rb").read(5) == b"LPV1\n")
        self.assertEqual(os.stat(self.vault._blob_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.vault._meta_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)

    def test_nonce_unique_per_encryption(self):
        self.vault.create(PASS)
        self.vault.add_entry("A", ["https://a.example.com"], "u", "p")

        def nonce_of(raw):
            nl = raw.find(b"\n", 5)
            return raw[nl + 1:nl + 13]

        before = open(self.vault._blob_path, "rb").read()
        self.vault.update_entry(
            next(iter(json.loads(
                open(self.vault._meta_path).read())["entries"]))["id"],
            notes="x")
        after = open(self.vault._blob_path, "rb").read()
        self.assertNotEqual(nonce_of(before), nonce_of(after))

    def test_lock_and_locked_listing(self):
        self.vault.create(PASS)
        self.vault.add_entry("A", ["https://a.example.com"], "u", "p")
        self.vault.lock()
        with self.assertRaises(pv.VaultLocked):
            self.vault.get_secret("cred_000000000000")
        self.assertEqual(len(self.vault.list_entries()), 1)

    def test_wrong_passphrase_fails_closed(self):
        self.vault.create(PASS)
        other = pv.PasswordVault(self.vault._dir and
                                 os.path.dirname(self.vault._blob_path))
        self.assertFalse(other.unlock("totally-wrong"))
        self.assertFalse(other.is_unlocked())
        self.assertTrue(other.unlock(PASS))

    def test_tampered_ciphertext_rejected(self):
        self.vault.create(PASS)
        self.vault.add_entry("A", ["https://a.example.com"], "u", "p")
        raw = open(self.vault._blob_path, "rb").read()
        nl = raw.find(b"\n", 5)
        pos = nl + 1 + 12 + 5
        tampered = raw[:pos] + bytes([raw[pos] ^ 0xFF]) + raw[pos + 1:]
        with open(self.vault._blob_path, "wb") as fh:
            fh.write(tampered)
        other = pv.PasswordVault(os.path.dirname(self.vault._blob_path))
        self.assertFalse(other.unlock(PASS))

    def test_tampered_header_rejected_as_vault_error(self):
        """A corrupt header must surface as VaultError, never as a raw
        UnicodeDecodeError escaping the fail-closed boundary."""
        self.vault.create(PASS)
        raw = open(self.vault._blob_path, "rb").read()
        nl = raw.find(b"\n", 5)
        with open(self.vault._blob_path, "wb") as fh:
            fh.write(raw[:20] + b"\xd3\xef" + raw[22:])
        other = pv.PasswordVault(os.path.dirname(self.vault._blob_path))
        with self.assertRaises(pv.VaultError):
            other.unlock(PASS)

    def test_truncated_and_bad_magic_rejected(self):
        self.vault.create(PASS)
        self.vault.add_entry("A", ["https://a.example.com"], "u", "p")
        raw = open(self.vault._blob_path, "rb").read()
        with open(self.vault._blob_path, "wb") as fh:
            fh.write(raw[:-10])
        other = pv.PasswordVault(os.path.dirname(self.vault._blob_path))
        self.assertFalse(other.unlock(PASS))
        with open(self.vault._blob_path, "wb") as fh:
            fh.write(b"XXXX\n" + raw[5:])
        with self.assertRaises(pv.VaultError):
            other.unlock(PASS)

    def test_change_passphrase_reencrypts(self):
        self.vault.create(PASS)
        eid = self.vault.add_entry("A", ["https://a.example.com"], "ua", "pa")
        self.vault.add_entry("B", ["https://b.example.com:9000"], "ub", "pb")
        self.vault.change_passphrase("new-passphrase-42")
        other = pv.PasswordVault(os.path.dirname(self.vault._blob_path))
        self.assertFalse(other.unlock(PASS))
        self.assertTrue(other.unlock("new-passphrase-42"))
        self.assertEqual(len(other.list_entries()), 2)
        self.assertEqual(other.get_secret(eid)["password"], "pa")

    def test_update_delete_missing_entries(self):
        self.vault.create(PASS)
        self.assertFalse(self.vault.update_entry("cred_000000000000", notes="x"))
        self.assertFalse(self.vault.delete_entry("cred_000000000000"))
        eid = self.vault.add_entry("A", ["https://a.example.com"], "u", "p")
        self.assertTrue(self.vault.delete_entry(eid))
        with self.assertRaises(pv.VaultError):
            self.vault.get_secret(eid)

    def test_autolock(self):
        self.vault.create(PASS)
        eid = self.vault.add_entry("A", ["https://a.example.com"], "u", "p")
        self.vault._last_access -= pv.AUTOLOCK_SECONDS + 1
        with self.assertRaises(pv.VaultLocked):
            self.vault.get_secret(eid)
        self.assertFalse(self.vault.is_unlocked())

    def test_idle_timer_drops_key_without_another_operation(self):
        with mock.patch.object(pv, "AUTOLOCK_SECONDS", 0.05):
            self.vault.create(PASS)
            timer = self.vault._timer
            timer.join(timeout=2)
            self.assertFalse(timer.is_alive())
            self.assertIsNone(self.vault._key)
            self.assertIsNone(self.vault._kdf)

    def test_expired_state_can_be_unlocked_directly(self):
        self.vault.create(PASS)
        self.vault._last_access -= pv.AUTOLOCK_SECONDS + 1
        self.assertFalse(self.vault.is_unlocked())
        self.assertTrue(self.vault.unlock(PASS))

    def test_failed_reunlock_discards_previous_key(self):
        self.vault.create(PASS)
        self.assertFalse(self.vault.unlock("wrong"))
        self.assertFalse(self.vault.is_unlocked())

    def test_modified_header_cannot_be_used_with_cached_key(self):
        self.vault.create(PASS)
        with open(self.vault._blob_path, "rb") as stream:
            raw = stream.read()
        kdf, nonce, ciphertext = pv._split_vault_file(raw)
        kdf["salt"] = base64.b64encode(b"x" * pv.SALT_BYTES).decode()
        modified = pv._MAGIC + json.dumps(kdf).encode() + b"\n" + nonce + ciphertext
        with open(self.vault._blob_path, "wb") as stream:
            stream.write(modified)
        with self.assertRaises(pv.VaultAuthError):
            self.vault.add_entry("A", ["https://example.com"], "u", "p")
        self.assertFalse(self.vault.is_unlocked())
        with open(self.vault._blob_path, "rb") as stream:
            self.assertEqual(stream.read(), modified)

    def test_other_instance_rekey_locks_stale_instance(self):
        self.vault.create(PASS)
        eid = self.vault.add_entry("A", ["https://example.com"], "u", "p")
        other = pv.PasswordVault(self.vault._dir)
        self.assertTrue(other.unlock(PASS))
        other.change_passphrase("new-test-pass")
        with self.assertRaises(pv.VaultAuthError):
            self.vault.get_secret(eid)
        self.assertFalse(self.vault.is_unlocked())
        self.assertTrue(self.vault.unlock("new-test-pass"))

    def test_clock_adjustment_does_not_extend_unlock(self):
        self.vault.create(PASS)
        self.vault._last_access -= pv.AUTOLOCK_SECONDS + 1
        with mock.patch.object(pv.time, "time", return_value=0):
            self.assertFalse(self.vault.is_unlocked())

    def test_passphrase_cache_failure_keeps_committed_key(self):
        self.vault.create(PASS)
        eid = self.vault.add_entry("A", ["https://example.com"], "u", "p")
        with mock.patch.object(self.vault, "_rebuild_cache", side_effect=OSError):
            with self.assertRaises(OSError):
                self.vault.change_passphrase("new-test-pass")
        self.assertEqual(self.vault.get_secret(eid)["password"], "p")
        other = pv.PasswordVault(self.vault._dir)
        self.assertFalse(other.unlock(PASS))
        self.assertTrue(other.unlock("new-test-pass"))

    def test_atomic_write_ignores_predictable_symlink(self):
        target = os.path.join(self.dir, "target")
        victim = os.path.join(self.dir, "victim")
        with open(victim, "wb") as stream:
            stream.write(b"preserve")
        os.symlink(victim, f"{target}.tmp.{os.getpid()}")
        pv._atomic_write(target, b"ciphertext")
        with open(victim, "rb") as stream:
            self.assertEqual(stream.read(), b"preserve")

    def test_failed_atomic_write_removes_temporary_file(self):
        target = os.path.join(self.dir, "target")
        with mock.patch.object(pv, "_write_all", side_effect=OSError):
            with self.assertRaises(OSError):
                pv._atomic_write(target, b"ciphertext")
        self.assertEqual(os.listdir(self.dir), [])

    def test_kdf_rejects_invalid_and_excessive_work_before_derivation(self):
        for fields in ({"n": 32769}, {"n": 2 ** 22},
                       {"n": 2 ** 16, "r": 8, "p": 8}):
            with self.subTest(fields=fields):
                kdf = {**pv._new_kdf(), **fields}
                with self.assertRaises(pv.VaultError):
                    pv._validate_kdf(kdf)

    def test_create_refuses_overwrite(self):
        self.vault.create(PASS)
        with self.assertRaises(pv.VaultExists):
            self.vault.create("zzz")

    def test_missing_vault_surfaces_not_initialized(self):
        """No vault on disk must raise VaultNotInitialized — not
        FileNotFoundError from the lock file (regression: O_CREAT cannot
        create the missing parent directory)."""
        with self.assertRaises(pv.VaultNotInitialized):
            self.vault.list_entries()
        with self.assertRaises(pv.VaultNotInitialized):
            self.vault.unlock(PASS)

    def test_origin_canonicalization(self):
        self.assertEqual(pv.normalize_origin("https://example.com:443"),
                         "https://example.com")
        self.assertEqual(pv.normalize_origin("https://EXAMPLE.com"),
                         "https://example.com")
        self.assertEqual(pv.normalize_origin("https://[::1]:8443"),
                         "https://[::1]:8443")
        for bad in ("http://example.com", "https://*.example.com",
                    "https://example.com/login", "https://u:p@example.com",
                    "https://example.com?q=1", "https://@example.com",
                    "https://exam\nple.com", "https://example.com:0",
                    "https://example.com:", "https://bad host.com",
                    "https://example.com\\evil", "https://%65xample.com",
                    "https://-bad.com", None, 123, ""):
            self.assertIsNone(pv.normalize_origin(bad), bad)
        with self.assertRaises(pv.VaultError):
            self.vault.create(PASS) or pv._clean_origins(["http://x.com"])

    def test_description_validation(self):
        with self.assertRaises(pv.VaultError):
            pv._clean_description("   ")
        with self.assertRaises(pv.VaultError):
            pv._clean_description("a\nb")
        with self.assertRaises(pv.VaultError):
            pv._clean_description("x" * 201)
        for value in (42, ["bad"], "site\x1b[2J"):
            with self.assertRaises(pv.VaultError):
                pv._clean_description(value)

    def test_secret_entry_roundtrip(self):
        self.vault.create(PASS)
        sid = self.vault.add_entry("API key", None, kind="secret",
                                   secret="sk-multi\nline2", notes="n")
        self.assertEqual(
            self.vault.get_secret(sid),
            {"kind": "secret", "secret": "sk-multi\nline2", "notes": "n"})
        pub = self.vault.list_entries()[0]
        self.assertEqual(pub["kind"], "secret")
        self.assertEqual(pub["origins"], [])
        self.assertNotIn("secret", pub)
        sid2 = self.vault.add_entry(
            "Webhook token", ["https://hooks.example.com"],
            kind="secret", secret="whk_a91fz")
        self.assertEqual(self.vault.list_entries()[1]["origins"],
                         ["https://hooks.example.com"])
        # a secret value never reaches disk plaintext or the public cache
        # (needles must not be substrings of the public description)
        blob = open(self.vault._blob_path, "rb").read()
        meta = open(self.vault._meta_path, "rb").read()
        for needle in (b"sk-multi", b"line2", b"whk_a91fz"):
            self.assertNotIn(needle, meta)
            self.assertNotIn(needle, blob)

    def test_secret_and_kind_validation(self):
        self.vault.create(PASS)
        with self.assertRaises(pv.VaultError):
            self.vault.add_entry("x", None, kind="secret", secret="")
        with self.assertRaises(pv.VaultError):
            self.vault.add_entry("x", None, kind="totp", secret="y")
        with self.assertRaises(pv.VaultError):
            self.vault.add_entry("x", None, username="u")  # login: no password
        with self.assertRaises(pv.VaultError):
            self.vault.add_entry("x", [], "u", "p")  # login: no origins

    def test_update_kind_rules(self):
        self.vault.create(PASS)
        sid = self.vault.add_entry("API key", None, kind="secret", secret="k1")
        lid = self.vault.add_entry("Site", ["https://example.com"], "u", "p")
        with self.assertRaises(pv.VaultError):
            self.vault.update_entry(sid, username="x")
        with self.assertRaises(pv.VaultError):
            self.vault.update_entry(lid, secret="x")
        with self.assertRaises(pv.VaultError):
            self.vault.update_entry(sid, secret="")
        with self.assertRaises(pv.VaultError):
            self.vault.update_entry(lid, origins=[])
        # rejected updates leave the vault intact
        self.assertEqual(self.vault.get_secret(lid)["username"], "u")
        self.assertTrue(self.vault.update_entry(sid, secret="k2"))
        self.assertEqual(self.vault.get_secret(sid)["secret"], "k2")
        self.assertTrue(self.vault.update_entry(sid, origins=[]))
        self.assertEqual(self.vault.list_entries()[0]["origins"], [])

    def _write_v1_vault(self, sub: str, passphrase: str, blob: dict) -> str:
        import base64 as _b64
        vdir = os.path.join(self.dir, sub)
        os.makedirs(vdir, exist_ok=True)
        kdf = pv._new_kdf()
        key = pv._derive_key(passphrase, _b64.b64decode(kdf["salt"]),
                             kdf["n"], kdf["r"], kdf["p"], kdf["dklen"])
        pv._atomic_write(os.path.join(vdir, "vault.bin"),
                         pv._serialize_vault(kdf, key, blob))
        return vdir

    def test_v1_vault_migrates_on_unlock(self):
        vdir = self._write_v1_vault("old", "old-pass", {
            "format": 1,
            "entries": {"cred_aabbccddeeff": {
                "description": "Legacy",
                "origins": ["https://old.example.com"],
                "username": "lu", "password": "lp", "notes": "",
                "created": "2026-09-01T00:00:00+00:00",
                "updated": "2026-09-01T00:00:00+00:00"}}})
        vault = pv.PasswordVault(vdir)
        self.assertTrue(vault.unlock("old-pass"))
        entries = vault.list_entries()
        self.assertEqual(entries[0]["kind"], "login")
        self.assertEqual(vault.get_secret("cred_aabbccddeeff")["password"], "lp")
        # the persisted file is now v2: a fresh instance sees no migration
        again = pv.PasswordVault(vdir)
        self.assertTrue(again.unlock("old-pass"))
        kdf2, nonce2, ct2 = pv._split_vault_file(
            open(os.path.join(vdir, "vault.bin"), "rb").read())
        blob2, migrated2 = pv._migrate_blob(json.loads(
            pv.AESGCM(again._key).decrypt(nonce2, ct2, pv._AAD).decode()))
        self.assertEqual(blob2["format"], 2)
        self.assertFalse(migrated2)

    def test_bad_v1_entry_rejected(self):
        vdir = self._write_v1_vault("bad", "bad-pass", {
            "format": 1,
            "entries": {"cred_aabbccddeeff": {"description": "x"}}})
        vault = pv.PasswordVault(vdir)
        with self.assertRaises(pv.VaultError):
            vault.unlock("bad-pass")


class TestModelFacingTools(unittest.TestCase):

    def setUp(self):
        self.registry = tools.get_registry()
        self.list_tool = self.registry.get("password.list")
        self.fill_tool = self.registry.get("password.fill")
        self.assertIsNotNone(self.list_tool)
        self.assertIsNotNone(self.fill_tool)
        self.home = tempfile.mkdtemp(prefix="vault-tool-home-")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_list_without_vault_reports_no_vault(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            result = self.list_tool.invoke({}, tools.ToolCtx())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_vault")

    def test_list_returns_public_metadata_only(self):
        vdir = os.path.join(self.home, ".laintas", "password-vault")
        vault = pv.PasswordVault(vdir)
        vault.create(PASS)
        vault.add_entry("Work account", ["https://example.com"],
                        "secret-user", "secret-password")
        vault.lock()
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            result = self.list_tool.invoke({}, tools.ToolCtx())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["entries"]), 1)
        entry = result["entries"][0]
        self.assertEqual(entry["description"], "Work account")
        self.assertEqual(entry["origins"], ["https://example.com"])
        body = json.dumps(result)
        self.assertNotIn("secret-user", body)
        self.assertNotIn("secret-password", body)

    def test_fill_fails_closed_without_broker(self):
        result = self.fill_tool.invoke(
            {"entry_id": "cred_000000000000", "tab": "tab_x"},
            tools.ToolCtx())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "broker_unavailable")

    def test_list_failure_never_exports_exception_contents(self):
        for error in (pv.VaultError("dummy-sensitive-value"),
                      OSError("dummy-sensitive-value")):
            with mock.patch.object(pv.PasswordVault, "list_entries", side_effect=error):
                result = self.list_tool.invoke({}, tools.ToolCtx())
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("dummy-sensitive-value", json.dumps(result))

    def test_fill_schema_accepts_no_scripts_or_secrets(self):
        schema = self.fill_tool.schema
        self.assertIs(schema.get("additionalProperties"), False)
        self.assertEqual(sorted(schema["properties"]), ["entry_id", "tab"])
        self.assertEqual(schema.get("required"), ["entry_id", "tab"])


class TestReveal(unittest.TestCase):
    """The (v)iew command must reach the user's screen without passing
    through sys.stdout — the CLI's stdout is teed to repl_mirror, and a
    revealed secret must never become a mirror event."""

    def test_write_private_bypasses_stdout(self):
        import io as _io
        import password_vault_ui as ui
        read_fd, write_fd = os.pipe()
        real_open = os.open

        def fake_open(path, flags, *args, **kwargs):
            if path == "/dev/tty":
                return write_fd
            return real_open(path, flags, *args, **kwargs)

        captured = _io.StringIO()
        with mock.patch.object(ui.os, "open", side_effect=fake_open), \
                mock.patch("sys.stdout", new=captured):
            ui._write_private("REVEALED-KEY-551")
        # _write_private already closed the fd it was given (write_fd) in
        # its finally block; closing it again would be an EBADF.
        payload = b""
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            payload += chunk
        os.close(read_fd)
        # reaches the raw /dev/tty channel (the user's screen)...
        self.assertIn(b"REVEALED-KEY-551", payload)
        # ...and never the stdout/mirror channel
        self.assertNotIn("REVEALED-KEY-551", captured.getvalue())


class TestEntryBoundaries(unittest.TestCase):

    def test_ui_refuses_arguments(self):
        import password_vault_ui as ui
        self.assertFalse(ui.handle_command(["secret-as-arg"]))

    def test_ui_refuses_without_controlling_tty(self):
        """In a session detached from any controlling terminal the UI must
        refuse rather than fall back to a less trusted input path."""
        script = (
            "import sys; sys.path.insert(0, '.'); "
            "import password_vault_ui as ui; "
            "print('refused=' + str(ui.handle_command([]) is False))"
        )
        proc = subprocess.run(
            ["setsid", sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("refused=True", proc.stdout)

    def test_dispatch_layer_rejects_password_arguments(self):
        """`/password <anything>` must be refused by the zero-arg rule before
        the UI ever runs, so secrets can never ride in as command text."""
        import laintas_cli as lc
        with self.assertRaises(lc.SlashCommandUsageError):
            lc._validate_slash_args("/password", ["whatever"])
        lc._validate_slash_args("/password", [])  # bare form is accepted

    def test_injected_line_cannot_open_password(self):
        """Helpwo/extension injected lines must not open the vault UI: the
        hidden passphrase prompt would appear on a screen the remote
        requester cannot see. Pins the guard in the REPL main loop."""
        source_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "laintas_cli.py")
        source = open(source_path, encoding="utf-8").read()
        self.assertIn("_is_injected_line", source)
        self.assertRegex(source, r"_is_injected_line\s+and _is_password_command\(user_input\)")
        import laintas_cli as lc
        for command in ("/password", "/PASSWORD", " /Password  "):
            self.assertTrue(lc._is_password_command(command))

    def test_secret_input_never_falls_back_when_tty_fails(self):
        import password_vault_ui as ui
        with mock.patch.object(ui.os, "open", side_effect=OSError), \
                mock.patch("builtins.input") as fallback:
            with self.assertRaises(pv.VaultError):
                ui._secret("Hidden: ")
        fallback.assert_not_called()

    def test_secret_input_never_reads_when_echo_cannot_be_disabled(self):
        import password_vault_ui as ui
        with mock.patch.object(ui.os, "open", return_value=42), \
                mock.patch.object(ui.os, "close") as close, \
                mock.patch.object(ui.termios, "tcgetattr", side_effect=ui.termios.error), \
                mock.patch.object(ui.os, "fdopen") as reader:
            with self.assertRaises(pv.VaultError):
                ui._secret("Hidden: ")
        reader.assert_not_called()
        close.assert_called_once_with(42)

    def test_ui_exception_locks_and_unregisters_exit_handler(self):
        import password_vault_ui as ui
        vault = mock.Mock()
        with mock.patch.object(pv, "PasswordVault", return_value=vault), \
                mock.patch.object(ui, "_run_session", side_effect=OSError), \
                mock.patch.object(ui.atexit, "register"), \
                mock.patch.object(ui.atexit, "unregister") as unregister:
            with self.assertRaises(OSError):
                ui._run()
        vault.lock.assert_called_once_with()
        unregister.assert_called_once_with(vault.lock)

    def test_cancelling_edit_does_not_save_partial_fields(self):
        import password_vault_ui as ui
        vault = mock.Mock()
        vault.list_entries.return_value = [{"id": "cred_aabbccddeeff", "kind": "login",
                                           "description": "old", "origins": ["https://example.com"]}]
        with mock.patch.object(ui, "_choose_entry", return_value="cred_aabbccddeeff"), \
                mock.patch.object(ui, "_ask", side_effect=["new description", ""]), \
                mock.patch.object(ui, "_secret", return_value=None):
            ui._edit_flow(vault)
        vault.update_entry.assert_not_called()

    def test_password_arguments_are_not_saved_in_prompt_history(self):
        import laintas_cli as lc
        with tempfile.TemporaryDirectory(prefix="vault-history-") as directory:
            path = os.path.join(directory, "history")
            history = lc._PrivateCommandHistory(path)
            history.append_string("ordinary prompt")
            history.append_string("/PASSWORD dummy-sensitive-value")
            history.append_string(" /password dummy-sensitive-value")
            self.assertEqual(history.get_strings(), ["ordinary prompt"])
            with open(path) as stream:
                self.assertNotIn("dummy-sensitive-value", stream.read())

    def test_dispatch_does_not_log_vault_exception_contents(self):
        import laintas_cli as lc
        with mock.patch.object(lc, "_handle_meta_command_impl",
                               side_effect=RuntimeError("dummy-sensitive-value")), \
                mock.patch.object(lc, "add_debug_log") as debug, \
                mock.patch.object(lc.console, "print") as output:
            self.assertFalse(lc.handle_meta_command("/password", None, {}))
        debug.assert_not_called()
        self.assertNotIn("dummy-sensitive-value", str(output.call_args_list))

    def test_distribution_includes_vault_and_crypto_dependency(self):
        from pathlib import Path
        manifest = json.loads((Path(__file__).resolve().parents[1] / "package_manifest.json").read_text())
        self.assertTrue({"password_vault", "password_vault_ui"} <= set(manifest["modules"]))
        self.assertIn("cryptography>=41.0", manifest["core_requires"])


class TestInteractiveUiPty(unittest.TestCase):
    """Drives the real UI through a PTY and asserts the core promise:
    secrets typed at hidden prompts are never echoed, so they cannot be
    captured by terminal mirrors or chat history."""

    def _spawn_pty(self, home):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        driver = "import password_vault_ui as ui; ui.handle_command([])"
        master, slave = pty.openpty()
        # select_dialog is a prompt_toolkit app: give the PTY a real size
        # (0x0 breaks full-screen layout) — 80x24 like a plain terminal.
        import fcntl as _fcntl
        import struct as _struct
        import termios as _termios
        _fcntl.ioctl(slave, _termios.TIOCSWINSZ,
                     _struct.pack("HHHH", 24, 80, 0, 0))

        def preexec():
            os.setsid()
            import fcntl
            import termios
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            [sys.executable, "-c", driver], stdin=slave, stdout=slave,
            stderr=slave, cwd=root,
            env=dict(os.environ, HOME=home), preexec_fn=preexec)
        os.close(slave)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            os.close(master)
        self.addCleanup(cleanup)
        state = {"transcript": b""}

        def expect(pattern, timeout=30, count=1):
            # count>1 handles repeated identical prompts (multi-line secret
            # input): waiting for the Nth occurrence avoids matching the
            # previous one and racing getpass's TCSAFLUSH input discard.
            deadline = time.time() + timeout
            while time.time() < deadline:
                if len(re.findall(pattern, state["transcript"])) >= count:
                    return
                ready, _, _ = select.select([master], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    state["transcript"] += chunk
            self.fail(f"prompt {pattern!r} x{count} missing; tail: "
                      f"{state['transcript'][-300:]!r}")

        def send(line):
            os.write(master, line.encode() + b"\n")

        def key(k, wait=0.5):
            # Dialog keys are raw keypresses (no newline). select_dialog
            # flushes typeahead when it opens and ignores affirmative keys
            # for 0.25s after open: the caller's expect() proves the dialog
            # rendered, and this sleep carries the key past both gates.
            time.sleep(wait)
            os.write(master, k.encode())

        return proc, expect, send, key, state

    def test_pty_login_flow_never_echoes_secrets(self):
        home = tempfile.mkdtemp(prefix="vault-e2e-")
        try:
            proc, expect, send, key, state = self._spawn_pty(home)
            pw = "e2e-secret-pw-99"
            try:
                expect(rb"Create a password vault now\?")
                key("y")                          # confirm dialog (y = Yes)
                expect(rb"New vault passphrase")
                send("e2e-passphrase-42")
                expect(rb"Repeat passphrase")
                send("e2e-passphrase-42")
                expect(rb"Vault created and unlocked")
                expect(rb"lock & quit")            # main menu rendered
                key("a")                          # Add entry
                expect(rb"Description \(visible")
                send("E2E test site")
                expect(rb"Entry kind")
                key("l")                          # login
                expect(rb"Approved HTTPS origin")
                send("https://e2e.example.com")
                expect(rb"Username \(hidden\)")
                send("e2e-user")
                expect(rb"Password \(hidden, empty cancels\)")
                send(pw)
                expect(rb"Repeat password")
                send(pw)
                expect(rb"Notes \(hidden")
                send("")
                expect(rb"added cred_[0-9a-f]{12} \(login\)")
                key("s")                          # Show entries
                expect(rb"cred_[0-9a-f]{12}  \[login\]  E2E test site")
                key("l")                          # Lock vault
                expect(rb"vault locked")
                key("q")                          # Quit
                expect(rb"Vault closed")
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)

            text = state["transcript"].decode("utf-8", "replace")
            for needle in (pw, "e2e-user", "e2e-passphrase-42"):
                self.assertNotIn(needle, text)
            self.assertIn("E2E test site", text)       # public by design
            self.assertIn("https://e2e.example.com", text)

            vdir = os.path.join(home, ".laintas", "password-vault")
            blob = open(os.path.join(vdir, "vault.bin"), "rb").read()
            meta = open(os.path.join(vdir, "meta.json"), "rb").read()
            for needle in (pw, "e2e-user"):
                self.assertNotIn(needle.encode(), blob)
                self.assertNotIn(needle.encode(), meta)

            vault = pv.PasswordVault(vdir)
            self.assertTrue(vault.unlock("e2e-passphrase-42"))
            entries = vault.list_entries()
            self.assertEqual(len(entries), 1)
            secret = vault.get_secret(entries[0]["id"])
            self.assertEqual(secret["kind"], "login")
            self.assertEqual(secret["username"], "e2e-user")
            self.assertEqual(secret["password"], pw)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_pty_secret_flow_never_echoes_secret(self):
        home = tempfile.mkdtemp(prefix="vault-e2e2-")
        try:
            proc, expect, send, key, state = self._spawn_pty(home)
            line1 = "sk-e2e-first-line-77"
            line2 = "sk-e2e-second-line-88"
            try:
                expect(rb"Create a password vault now\?")
                key("y")
                expect(rb"New vault passphrase")
                send("e2e-passphrase-42")
                expect(rb"Repeat passphrase")
                send("e2e-passphrase-42")
                expect(rb"Vault created and unlocked")
                expect(rb"lock & quit")
                key("a")                          # Add entry
                expect(rb"Description \(visible")
                send("Deploy API key")
                expect(rb"Entry kind")
                key("s")                          # secret
                expect(rb"Related HTTPS origin")
                send("")                       # optional: skip
                expect(rb"Secret value \(hidden, multi-line")
                # A pasted multiline key arrives all at once. Reopening
                # getpass for each line used to discard the buffered tail.
                send(line1 + "\n" + line2 + "\n")
                expect(rb"\.\.\. \(hidden", count=1)   # 1st continuation
                expect(rb"\.\.\. \(hidden", count=2)   # 2nd continuation
                expect(rb"Notes \(hidden")
                send("")
                expect(rb"added cred_[0-9a-f]{12} \(secret\)")
                key("s")                          # Show entries
                expect(rb"cred_[0-9a-f]{12}  \[secret\]  Deploy API key"
                       rb"  \[no origin\]")
                key("q")                          # Quit
                expect(rb"Vault closed")
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)

            text = state["transcript"].decode("utf-8", "replace")
            for needle in (line1, line2, "e2e-passphrase-42"):
                self.assertNotIn(needle, text)
            self.assertIn("Deploy API key", text)      # public by design

            vdir = os.path.join(home, ".laintas", "password-vault")
            blob = open(os.path.join(vdir, "vault.bin"), "rb").read()
            meta = open(os.path.join(vdir, "meta.json"), "rb").read()
            for needle in (line1, line2):
                self.assertNotIn(needle.encode(), blob)
                self.assertNotIn(needle.encode(), meta)

            vault = pv.PasswordVault(vdir)
            self.assertTrue(vault.unlock("e2e-passphrase-42"))
            entries = vault.list_entries()
            self.assertEqual(entries[0]["kind"], "secret")
            self.assertEqual(entries[0]["origins"], [])
            self.assertEqual(
                vault.get_secret(entries[0]["id"]),
                {"kind": "secret", "secret": f"{line1}\n{line2}", "notes": ""})
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_pty_view_shows_secret_on_screen_then_clears(self):
        """End-to-end reveal: the secret appears on the user's screen (the
        PTY master sees it — that is the point of view), Enter wipes the
        screen with an ANSI erase, and the session closes cleanly."""
        home = tempfile.mkdtemp(prefix="vault-e2e3-")
        try:
            vdir = os.path.join(home, ".laintas", "password-vault")
            vault = pv.PasswordVault(vdir)
            vault.create("e2e-passphrase-42")
            vault.add_entry("View test", ["https://view.example.com"],
                           "view-user", "view-pw-77")
            vault.lock()

            proc, expect, send, key, state = self._spawn_pty(home)
            try:
                expect(rb"lock & quit")            # main menu (vault exists)
                key("u")                          # Unlock vault
                expect(rb"Vault passphrase")
                send("e2e-passphrase-42")
                expect(rb"vault unlocked")
                key("v")                          # View entry
                expect(rb"View which entry\?")
                key("\r")                         # Enter selects first row
                expect(rb"Show the secret on this screen")
                key("y")                          # confirm reveal
                expect(rb"username: view-user")
                expect(rb"password: view-pw-77")   # on the screen, by design
                expect(rb"Press Enter to clear")
                send("")
                expect(rb"\x1b\[2J")               # ANSI screen erase sent
                expect(rb"screen cleared")
                key("q")                          # Quit
                expect(rb"Vault closed")
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()

            text = state["transcript"].decode("utf-8", "replace")
            # the secret reached the screen exactly through the reveal,
            # and the passphrase never did (it was hidden input)
            self.assertIn("view-pw-77", text)
            self.assertNotIn("e2e-passphrase-42", text)
            # nothing was written to disk in plaintext
            blob = open(os.path.join(vdir, "vault.bin"), "rb").read()
            self.assertNotIn(b"view-pw-77", blob)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
