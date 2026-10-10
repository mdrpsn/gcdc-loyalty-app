"""Encrypted logical backup uploaded to an S3-compatible bucket (Cloudflare R2).

Runs backup_database.create_backup (pg_dump -Fc, Fernet-encrypted), uploads the encrypted file
under daily/ (and monthly/ on the 1st), verifies each object's size by reading it back, then pings
an optional heartbeat URL. Only the encrypted file ever leaves the host. Requests are signed with
AWS Signature V4 using the standard library, so no extra dependency ships to production.

Usage (environment variables only, no secrets on the command line):
  python scripts/offsite_backup.py backup
  python scripts/offsite_backup.py fetch <object-key> <destination-file>

Required: DB_* (see backup_database.py), BACKUP_ENCRYPTION_KEY, R2_ACCOUNT_ID, R2_BUCKET,
R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY. Optional: R2_ENDPOINT (override, used by tests),
BACKUP_HEARTBEAT_URL (GET after a successful backup), BACKUP_FORCE_MONTHLY=1.
Retention is set on the bucket with lifecycle rules (see the README), not by this script.
"""

import hashlib
import hmac
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import backup_database as backups

REQUIRED = ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class OffsiteError(Exception):
    """Raised with a message that is safe to print (no keys, no credentials)."""


def _hmac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sign_v4(*, method, path, query, headers, payload_hash, access_key, secret_key, region, service, amz_date):
    """Return the Authorization header value for an AWS Signature V4 request.

    `headers` must already contain every header to be signed (including host and x-amz-date).
    """
    names = sorted(name.lower() for name in headers)
    lowered = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([method, path, query, canonical_headers, signed_headers, payload_hash])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key = _hmac(("AWS4" + secret_key).encode(), amz_date[:8])
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"


class ObjectStore:
    """Path-style S3 client for one bucket: put, head, and get."""

    def __init__(self, *, endpoint, bucket, access_key, secret_key, region="auto", clock=None, timeout=60):
        self.endpoint = endpoint.rstrip("/")
        self.host = urllib.parse.urlsplit(self.endpoint).netloc
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout = timeout

    def _request(self, method, key, body=b""):
        path = "/" + urllib.parse.quote(f"{self.bucket}/{key}" if key else self.bucket, safe="/-_.~")
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        amz_date = self.clock().strftime("%Y%m%dT%H%M%SZ")
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        headers["Authorization"] = sign_v4(
            method=method, path=path, query="", headers=headers, payload_hash=payload_hash,
            access_key=self.access_key, secret_key=self.secret_key, region=self.region,
            service="s3", amz_date=amz_date,
        )
        request = urllib.request.Request(
            self.endpoint + path, data=body if method == "PUT" else None, method=method,
            headers={k: v for k, v in headers.items() if k != "host"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            code = re.search(rb"<Code>([A-Za-z0-9]+)</Code>", error.read() or b"")
            raise OffsiteError(
                f"{method} {key or '(bucket)'} failed: HTTP {error.code}"
                + (f" {code.group(1).decode()}" if code else "")
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OffsiteError(f"{method} {key or '(bucket)'} failed: {type(error).__name__}") from None

    def put(self, key, data):
        self._request("PUT", key, data)

    def head(self, key):
        """Return the object's size in bytes."""
        _, headers, _ = self._request("HEAD", key)
        size = {name.lower(): value for name, value in headers.items()}.get("content-length")
        if size is None:
            raise OffsiteError(f"HEAD {key} returned no size")
        return int(size)

    def get(self, key):
        return self._request("GET", key)[2]


def store_from_env(env):
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise OffsiteError("Missing required variables: " + ", ".join(missing))
    endpoint = env.get("R2_ENDPOINT") or f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    return ObjectStore(
        endpoint=endpoint, bucket=env["R2_BUCKET"],
        access_key=env["R2_ACCESS_KEY_ID"], secret_key=env["R2_SECRET_ACCESS_KEY"],
    )


def ping_heartbeat(url, opener=urllib.request.urlopen):
    """Best effort: a missed ping is what raises the alert, so a failed ping never fails the backup."""
    try:
        with opener(url, timeout=10):
            return True
    except Exception as error:  # noqa: BLE001
        print(f"Heartbeat ping failed: {type(error).__name__}", file=sys.stderr)
        return False


def run_backup(env, *, store=None, now=None, runner=None, heartbeat=ping_heartbeat):
    """Create, upload, verify, and report; return the uploaded object keys."""
    store = store or store_from_env(env)
    now = now or datetime.now(timezone.utc)
    kwargs = {"runner": runner} if runner is not None else {}
    with tempfile.TemporaryDirectory(prefix="gcdc_backup_") as directory:
        path = backups.create_backup(env, directory, now=now, **kwargs)
        data = path.read_bytes()
        name = path.name
    prefixes = ["daily"]
    if now.day == 1 or env.get("BACKUP_FORCE_MONTHLY") == "1":
        prefixes.append("monthly")
    keys = [f"{prefix}/{name}" for prefix in prefixes]
    for key in keys:
        store.put(key, data)
        size = store.head(key)
        if size != len(data):
            raise OffsiteError(f"Uploaded {key} is {size} bytes, expected {len(data)}")
    if env.get("BACKUP_HEARTBEAT_URL"):
        heartbeat(env["BACKUP_HEARTBEAT_URL"])
    return keys


def fetch(env, key, destination):
    """Download one backup object for a restore drill; never overwrites an existing file."""
    if not key or key.endswith("/") or not key.startswith(("daily/", "monthly/")):
        raise OffsiteError("Give the full object key, for example daily/<file>.dump.enc")
    destination = Path(destination)
    if destination.exists():
        raise OffsiteError(f"Refusing to overwrite existing file {destination.name}")
    data = store_from_env(env).get(key)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return destination


def main(argv):
    try:
        if len(argv) == 2 and argv[1] == "backup":
            for key in run_backup(os.environ):
                print(f"Uploaded: {key}")
            return 0
        if len(argv) == 4 and argv[1] == "fetch":
            print(f"Downloaded: {fetch(os.environ, argv[2], argv[3])}")
            return 0
        print(__doc__, file=sys.stderr)
        return 2
    except (OffsiteError, backups.BackupError) as error:
        print(f"Off-site backup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
