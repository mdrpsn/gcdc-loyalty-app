"""Off-site backup upload: signing, safety, and failure behavior (no real pg_dump or network needed)."""

import importlib.util
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from django.test import SimpleTestCase

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("offsite_backup", SCRIPTS / "offsite_backup.py")
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)

DUMP = b"PGDMP\x01\x0e\x00 synthetic custom-format payload"
SECRET = "super-secret-r2-key"


class FakeS3(BaseHTTPRequestHandler):
    objects = {}
    log = []
    fail_status = None
    wrong_size = False

    def log_message(self, *args):
        pass

    def _check(self):
        if self.fail_status:
            body = b"<Error><Code>AccessDenied</Code></Error>"
            self.send_response(self.fail_status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeS3.log.append((self.command, self.path, dict(self.headers), body))
        if self._check():
            FakeS3.objects[self.path] = body
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_HEAD(self):
        FakeS3.log.append((self.command, self.path, dict(self.headers), b""))
        if self._check():
            size = len(FakeS3.objects.get(self.path, b"")) + (1 if FakeS3.wrong_size else 0)
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()

    def do_GET(self):
        FakeS3.log.append((self.command, self.path, dict(self.headers), b""))
        if self._check():
            body = FakeS3.objects.get(self.path, b"")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class OffsiteBackupTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        FakeS3.objects, FakeS3.log, FakeS3.fail_status, FakeS3.wrong_size = {}, [], None, False
        self.key = Fernet.generate_key()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        pg_bin = Path(self.tmp.name)
        (pg_bin / ("pg_dump.exe" if os.name == "nt" else "pg_dump")).write_bytes(b"")
        self.env = {
            "DB_NAME": "gcdc", "DB_USER": "app", "DB_PASSWORD": "db-password-123", "DB_HOST": "db.internal",
            "DB_PORT": "5432", "BACKUP_ENCRYPTION_KEY": self.key.decode(), "PG_BIN": str(pg_bin),
            "R2_ACCOUNT_ID": "acct", "R2_BUCKET": "club-backups", "R2_ACCESS_KEY_ID": "AKIDEXAMPLE",
            "R2_SECRET_ACCESS_KEY": SECRET, "R2_ENDPOINT": self.endpoint,
        }
        self.pings = []

    def runner(self, stdout=DUMP, returncode=0):
        return lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, stdout, b"")

    def backup(self, day=10, **overrides):
        env = {**self.env, **overrides}
        now = datetime(2026, 10, day, 18, 0, 0, tzinfo=timezone.utc)
        # Sign requests with the same fixed time, not the machine's clock, so the tests give the
        # same result on any day (the signature scope contains the date).
        store = offsite.store_from_env(env)
        store.clock = lambda: now
        return offsite.run_backup(
            env, store=store, now=now, runner=self.runner(), heartbeat=lambda url: self.pings.append(url),
        )

    def test_signature_matches_the_published_aws_example(self):
        # "GET Object" example from the AWS Signature Version 4 documentation.
        header = offsite.sign_v4(
            method="GET", path="/test.txt", query="",
            headers={
                "host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9",
                "x-amz-content-sha256": offsite.EMPTY_SHA256, "x-amz-date": "20130524T000000Z",
            },
            payload_hash=offsite.EMPTY_SHA256, access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", region="us-east-1", service="s3",
            amz_date="20130524T000000Z",
        )
        self.assertEqual(
            header,
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        )

    def test_uploads_an_encrypted_daily_copy_only_and_verifies_it(self):
        keys = self.backup(day=10)
        self.assertEqual(keys, ["daily/gcdc_gcdc_20261010T180000Z.dump.enc"])
        stored = FakeS3.objects["/club-backups/" + keys[0]]
        self.assertNotIn(b"PGDMP", stored)  # only the encrypted file leaves the host
        self.assertEqual(Fernet(self.key).decrypt(stored), DUMP)
        self.assertEqual([entry[0] for entry in FakeS3.log], ["PUT", "HEAD"])

    def test_the_first_of_the_month_also_uploads_a_monthly_copy(self):
        keys = self.backup(day=1)
        self.assertEqual([key.split("/")[0] for key in keys], ["daily", "monthly"])
        self.assertEqual(len(FakeS3.objects), 2)
        self.assertEqual(len(self.backup(day=10, BACKUP_FORCE_MONTHLY="1")), 2)

    def test_every_request_is_signed_and_the_secret_is_never_sent(self):
        self.backup()
        for _, _, raw_headers, body in FakeS3.log:
            headers = {name.lower(): value for name, value in raw_headers.items()}  # HTTP names are case-insensitive
            auth = headers["authorization"]
            self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261010/auto/s3/aws4_request"))
            self.assertIn("SignedHeaders=host;x-amz-content-sha256;x-amz-date", auth)
            self.assertNotIn(SECRET, auth + "".join(headers.values()))
            if body:
                import hashlib
                self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(body).hexdigest())

    def test_heartbeat_is_pinged_only_after_a_successful_backup(self):
        self.backup(BACKUP_HEARTBEAT_URL="https://heartbeat.example/abc")
        self.assertEqual(self.pings, ["https://heartbeat.example/abc"])
        self.pings.clear()
        FakeS3.fail_status = 403
        with self.assertRaises(offsite.OffsiteError):
            self.backup(BACKUP_HEARTBEAT_URL="https://heartbeat.example/abc")
        self.assertEqual(self.pings, [])  # a missed ping is what raises the alert

    def test_a_failed_heartbeat_does_not_fail_the_backup(self):
        def broken(url, timeout):
            raise OSError("down")

        self.assertFalse(offsite.ping_heartbeat("https://heartbeat.example/abc", opener=broken))

    def test_storage_errors_raise_a_safe_message(self):
        FakeS3.fail_status = 403
        with self.assertRaises(offsite.OffsiteError) as raised:
            self.backup()
        message = str(raised.exception)
        self.assertIn("HTTP 403 AccessDenied", message)
        for secret in (SECRET, "db-password-123", self.key.decode()):
            self.assertNotIn(secret, message)

    def test_a_size_mismatch_after_upload_is_an_error(self):
        FakeS3.wrong_size = True
        with self.assertRaisesMessage(offsite.OffsiteError, "bytes, expected"):
            self.backup()

    def test_unreachable_storage_is_an_error_without_details_that_leak(self):
        with self.assertRaises(offsite.OffsiteError) as raised:
            self.backup(R2_ENDPOINT="http://127.0.0.1:9")  # nothing listens here
        self.assertNotIn(SECRET, str(raised.exception))

    def test_missing_storage_settings_fail_before_pg_dump_runs(self):
        calls = []
        env = {k: v for k, v in self.env.items() if k != "R2_SECRET_ACCESS_KEY"}
        with self.assertRaisesMessage(offsite.OffsiteError, "R2_SECRET_ACCESS_KEY"):
            offsite.run_backup(env, runner=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_a_failed_dump_uploads_nothing(self):
        with self.assertRaises(offsite.backups.BackupError):
            offsite.run_backup(self.env, runner=self.runner(stdout=b"", returncode=1))
        self.assertEqual(FakeS3.log, [])

    def test_fetch_requires_a_real_backup_object_key(self):
        destination = Path(self.tmp.name) / "restore.dump.enc"
        for bad in ("", "daily/", "somewhere/else.dump.enc", "../daily/x"):
            with self.subTest(key=bad), self.assertRaisesMessage(offsite.OffsiteError, "full object key"):
                offsite.fetch(self.env, bad, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(FakeS3.log, [])

    def test_fetch_round_trips_and_never_overwrites(self):
        key = self.backup()[0]
        destination = Path(self.tmp.name) / "restore.dump.enc"
        offsite.fetch(self.env, key, destination)
        self.assertEqual(Fernet(self.key).decrypt(destination.read_bytes()), DUMP)
        with self.assertRaisesMessage(offsite.OffsiteError, "Refusing to overwrite"):
            offsite.fetch(self.env, key, destination)
