"""Exercise pg_dump/pg_restore using two newly created local scratch databases.

Run with the development database environment and optional PG_BIN directory.
No existing database is read, modified, or dropped.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("DB_HOST", "")
PORT = os.environ.get("DB_PORT", "")
USER = os.environ.get("DB_USER", "")
PASSWORD = os.environ.get("DB_PASSWORD", "")
if HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("Restore check is restricted to local PostgreSQL")
if not all((PORT, USER, PASSWORD)):
    raise SystemExit("DB_HOST, DB_PORT, DB_USER, and DB_PASSWORD are required")
if os.environ.get("DJANGO_SETTINGS_MODULE") == "config.settings.production":
    raise SystemExit("Restore check cannot use production settings")

pg_bin = Path(os.environ.get("PG_BIN", "")) if os.environ.get("PG_BIN") else None
if pg_bin is None and os.name == "nt":
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "gcdc-postgres/pgsql/bin"
    if (candidate / "pg_dump.exe").is_file():
        pg_bin = candidate


def pg_tool(name):
    if pg_bin:
        path = pg_bin / (name + (".exe" if os.name == "nt" else ""))
        if path.is_file():
            return str(path)
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} is required on PATH or in PG_BIN")
    return found


tools = {name: pg_tool(name) for name in ("createdb", "dropdb", "pg_dump", "pg_restore")}
pg_env = {**os.environ, "PGPASSWORD": PASSWORD}
suffix = uuid.uuid4().hex[:12]
source = f"gcdc_restore_source_{suffix}"
restored = f"gcdc_restore_target_{suffix}"
created = []


def run(args, *, env=pg_env):
    result = subprocess.run(args, env=env, cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr[-1200:]}")


def pg_args(name, *extra):
    return [tools[name], "-h", HOST, "-p", PORT, "-U", USER, *extra]


def counts(database):
    import psycopg

    with psycopg.connect(dbname=database, host=HOST, port=PORT, user=USER, password=PASSWORD) as conn:
        with conn.cursor() as cursor:
            return {
                table: cursor.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "accounts_user", "club_player", "club_attendance", "club_rewardissuance",
                    "club_rewardredemption", "club_auditevent",
                )
            }


try:
    for database in (source, restored):
        run(pg_args("createdb", "-O", USER, database))
        created.append(database)
    source_env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.development", "DB_NAME": source}
    run([sys.executable, "manage.py", "migrate", "--noinput"], env=source_env)
    os.environ.update(source_env)
    sys.path.insert(0, str(ROOT))
    import django

    django.setup()
    from datetime import timedelta

    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.db import connections
    from django.utils import timezone

    from accounts.permissions import ORGANIZER
    from club.loyalty import redeem_reward
    from club.models import Attendance, Player, RewardIssuance
    from club.services import complete_openplay, create_openplay, create_venue

    actor = get_user_model().objects.create_user("restore-operator@example.invalid", "not-a-real-secret")
    actor.groups.add(Group.objects.get(name=ORGANIZER))
    player = Player.objects.create(display_name="Restore Test", phone="000")
    venue = create_venue(name="Restore Court", actor=actor)
    for days_ago in range(10, 0, -1):
        start = timezone.now() - timedelta(days=days_ago)
        session = create_openplay(
            venue_id=venue.pk, starts_at=start, ends_at=start + timedelta(hours=2), actor=actor,
        )
        Attendance.objects.create(
            player=player, openplay=session,
            source=Attendance.Source.HISTORICAL, recorded_by=actor,
        )
        complete_openplay(openplay_id=session.pk, actor=actor, now=session.ends_at + timedelta(minutes=1))
    issuance = RewardIssuance.objects.get(player=player, milestone__threshold=10)
    future = timezone.now() + timedelta(days=2)
    target = create_openplay(
        venue_id=venue.pk, starts_at=future, ends_at=future + timedelta(hours=2), actor=actor,
    )
    redeem_reward(issuance_id=issuance.pk, player_id=player.pk, openplay_id=target.pk, actor=actor)
    connections.close_all()

    with tempfile.TemporaryDirectory(prefix="gcdc_restore_check_") as directory:
        dump = str(Path(directory) / "synthetic.dump")
        run(pg_args("pg_dump", "-Fc", "-f", dump, source))
        run(pg_args("pg_restore", "--no-owner", "--no-acl", "--exit-on-error", "-d", restored, dump))
        before, after = counts(source), counts(restored)
        if before != after or before["club_rewardredemption"] != 1:
            raise RuntimeError(f"Restored ledger counts differ: {before} vs {after}")
        restored_env = {**source_env, "DB_NAME": restored}
        run([sys.executable, "manage.py", "check"], env=restored_env)
        run([sys.executable, "manage.py", "migrate", "--check"], env=restored_env)
        run([
            sys.executable, "manage.py", "shell", "-c",
            "from django.test import Client; assert Client().get('/health/', HTTP_HOST='127.0.0.1').status_code == 200",
        ], env=restored_env)
        print(f"Synthetic local restore verified: {after}")
finally:
    if "django" in sys.modules:
        from django.db import connections

        connections.close_all()
    for database in reversed(created):
        # Only names generated in this process are eligible for cleanup.
        if database in {source, restored}:
            run(pg_args("dropdb", database))
