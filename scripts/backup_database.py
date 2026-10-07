"""Create an encrypted logical PostgreSQL backup (pg_dump custom format).

Connection details come from the same DB_* variables as the application; the
password is passed to pg_dump only through the child environment. The dump is
encrypted with Fernet (AES-128-CBC plus HMAC-SHA256, authenticated) using
BACKUP_ENCRYPTION_KEY, so no plaintext dump is written to disk. The whole dump
is held in memory, which is fine at club scale but not for very large databases.

Usage: uv run --locked --env-file .env python scripts/backup_database.py [output_dir]
Output: <output_dir>/gcdc_<DB_NAME>_<UTC YYYYMMDDTHHMMSSZ>.dump.enc (default ./backups).
An existing file is never overwritten. Optional PG_BIN names a directory with pg_dump.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parents[1]
DUMP_MAGIC = b"PGDMP"
REQUIRED = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT")


class BackupError(Exception):
    """Raised with a message that is safe to print (no credentials or key)."""


def find_pg_tool(name, env=None):
    env = os.environ if env is None else env
    if env.get("PG_BIN"):
        path = Path(env["PG_BIN"]) / (name + (".exe" if os.name == "nt" else ""))
        if path.is_file():
            return str(path)
    if os.name == "nt":
        candidate = Path(env.get("LOCALAPPDATA", "")) / "gcdc-postgres/pgsql/bin" / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise BackupError(f"{name} is required on PATH or in PG_BIN")
    return found


def load_key(env):
    raw = env.get("BACKUP_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise BackupError("BACKUP_ENCRYPTION_KEY is required")
    try:
        Fernet(raw.encode())
    except (ValueError, TypeError):
        raise BackupError("BACKUP_ENCRYPTION_KEY is not a valid Fernet key") from None
    return raw.encode()


def backup_filename(db_name, now):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", db_name)
    return f"gcdc_{safe}_{now.strftime('%Y%m%dT%H%M%SZ')}.dump.enc"


def _redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def create_backup(env, output_dir, *, now=None, runner=subprocess.run):
    """Dump, encrypt, and publish a new backup file; return its Path."""
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise BackupError("Missing required variables: " + ", ".join(missing))
    key = load_key(env)
    now = now or datetime.now(timezone.utc)
    output_dir = Path(output_dir)
    final = output_dir / backup_filename(env["DB_NAME"], now)
    if final.exists():
        raise BackupError(f"Refusing to overwrite existing backup {final.name}")

    child_env = {**os.environ, "PGPASSWORD": env["DB_PASSWORD"]}
    if env.get("DB_SSLMODE"):
        child_env["PGSSLMODE"] = env["DB_SSLMODE"]
    command = [
        find_pg_tool("pg_dump", env), "-h", env["DB_HOST"], "-p", env["DB_PORT"],
        "-U", env["DB_USER"], "-Fc", env["DB_NAME"],
    ]
    result = runner(command, env=child_env, capture_output=True, check=False)
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-600:]
        raise BackupError("pg_dump failed: " + _redact(detail, (env["DB_PASSWORD"],)))
    if not result.stdout.startswith(DUMP_MAGIC):
        raise BackupError("pg_dump produced no valid custom-format dump")

    token = Fernet(key).encrypt(result.stdout)
    output_dir.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + ".partial")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(partial, final)  # fails if final exists: never overwrites
        except FileExistsError:
            raise BackupError(f"Refusing to overwrite existing backup {final.name}") from None
    finally:
        partial.unlink(missing_ok=True)
    return final


def decrypt_backup(path, key):
    """Return the plaintext pg_dump bytes, or raise BackupError."""
    try:
        data = Fernet(key).decrypt(Path(path).read_bytes())
    except InvalidToken:
        raise BackupError("Backup cannot be decrypted: wrong key or corrupted file") from None
    if not data.startswith(DUMP_MAGIC):
        raise BackupError("Decrypted data is not a pg_dump custom-format archive")
    return data


def main(argv):
    output_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "backups"
    try:
        path = create_backup(os.environ, output_dir)
    except BackupError as error:
        print(f"Backup failed: {error}", file=sys.stderr)
        return 1
    print(f"Backup written: {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
