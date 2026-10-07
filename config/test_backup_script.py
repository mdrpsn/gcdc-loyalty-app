"""Safety and failure behavior of scripts/backup_database.py (no real pg_dump needed)."""

import importlib.util
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from django.test import SimpleTestCase

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_database.py"
spec = importlib.util.spec_from_file_location("backup_database", SCRIPT)
backups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backups)

PASSWORD = "s3cret-db-password"
DUMP = b"PGDMP\x01\x0e\x00 synthetic custom-format payload"
NOW = datetime(2026, 10, 8, 3, 4, 5, tzinfo=timezone.utc)


class BackupScriptTests(SimpleTestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "out"
        pg_bin = Path(self.tmp.name) / "bin"
        pg_bin.mkdir()
        (pg_bin / ("pg_dump.exe" if os.name == "nt" else "pg_dump")).write_bytes(b"")
        self.env = {
            "DB_NAME": "gcdc prod", "DB_USER": "app", "DB_PASSWORD": PASSWORD,
            "DB_HOST": "db.internal", "DB_PORT": "5432", "DB_SSLMODE": "require",
            "BACKUP_ENCRYPTION_KEY": self.key.decode(), "PG_BIN": str(pg_bin),
        }
        self.calls = []

    def runner(self, stdout=DUMP, returncode=0, stderr=b""):
        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)
        return run

    def backup(self, **kwargs):
        return backups.create_backup(self.env, self.output, now=NOW, runner=self.runner(**kwargs))

    def files(self):
        return sorted(p.name for p in self.output.iterdir()) if self.output.exists() else []

    def test_writes_encrypted_dated_file_without_plaintext(self):
        path = self.backup()
        self.assertEqual(path.name, "gcdc_gcdc_prod_20261008T030405Z.dump.enc")
        self.assertEqual(self.files(), [path.name])
        blob = path.read_bytes()
        self.assertNotIn(b"PGDMP", blob)
        self.assertNotIn(b"synthetic custom-format payload", blob)
        self.assertEqual(backups.decrypt_backup(path, self.key), DUMP)

    def test_password_goes_only_through_child_environment(self):
        self.backup()
        command, kwargs = self.calls[0]
        self.assertNotIn(PASSWORD, " ".join(command))
        self.assertEqual(kwargs["env"]["PGPASSWORD"], PASSWORD)
        self.assertEqual(kwargs["env"]["PGSSLMODE"], "require")
        self.assertIn("-Fc", command)

    def test_never_overwrites_an_existing_backup(self):
        first = self.backup()
        original = first.read_bytes()
        with self.assertRaisesMessage(backups.BackupError, "Refusing to overwrite"):
            self.backup(stdout=DUMP + b"changed")
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(self.files(), [first.name])

    def test_dump_failure_writes_nothing_and_redacts_password(self):
        with self.assertRaises(backups.BackupError) as raised:
            self.backup(returncode=1, stderr=f"connection failed for {PASSWORD}".encode())
        self.assertNotIn(PASSWORD, str(raised.exception))
        self.assertIn("pg_dump failed", str(raised.exception))
        self.assertEqual(self.files(), [])

    def test_invalid_dump_output_writes_nothing(self):
        for bad in (b"", b"not a dump"):
            with self.subTest(bad=bad), self.assertRaisesMessage(backups.BackupError, "no valid"):
                self.backup(stdout=bad)
        self.assertEqual(self.files(), [])

    def test_missing_or_invalid_key_fails_before_running_pg_dump(self):
        for value, message in (("", "is required"), ("not-a-key", "not a valid Fernet key")):
            with self.subTest(value=value):
                self.env["BACKUP_ENCRYPTION_KEY"] = value
                with self.assertRaises(backups.BackupError) as raised:
                    self.backup()
                self.assertIn(message, str(raised.exception))
                self.assertNotIn("not-a-key", str(raised.exception))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.files(), [])

    def test_missing_connection_variable_fails_before_running_pg_dump(self):
        del self.env["DB_PASSWORD"]
        with self.assertRaisesMessage(backups.BackupError, "DB_PASSWORD"):
            self.backup()
        self.assertEqual(self.calls, [])

    def test_wrong_key_and_tampered_file_cannot_be_decrypted(self):
        path = self.backup()
        with self.assertRaisesMessage(backups.BackupError, "wrong key or corrupted"):
            backups.decrypt_backup(path, Fernet.generate_key())
        blob = bytearray(path.read_bytes())
        blob[len(blob) // 2] ^= 0x01
        path.write_bytes(bytes(blob))
        with self.assertRaisesMessage(backups.BackupError, "wrong key or corrupted"):
            backups.decrypt_backup(path, self.key)

    def test_filename_contains_no_unsafe_characters(self):
        name = backups.backup_filename("a/b\\c:d..", NOW)
        self.assertRegex(name, r"^gcdc_[A-Za-z0-9_-]+_20261008T030405Z\.dump\.enc$")
