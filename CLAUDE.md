# Got Cha Dink Club (GCDC) loyalty app

Loyalty tracker for pickleball **OpenPlay attendance**. One OpenPlay attended = one loyalty unit. Never reword this as games or matches played.

**OpenPlays Attended** is derived: count of `Attendance` with status VALID on OpenPlays that are not CANCELLED (`club.services.openplays_attended`). There are no stored counters and no Streak table. Level, milestones, streaks, and badges are all derived. Reward eligibility uses *completed* sessions only (`club/loyalty.py`).

## Architecture (keep it)

Django 6.1 monolith, server-rendered templates, PostgreSQL 18, psycopg, uv, Python 3.14. Apps: `accounts` (email-keyed User, role checks) and `club` (domain). Business operations live in `club/services.py` and `club/loyalty.py` (transactional, audited); views in `club/organizer_views.py` and `club/player_views.py` stay thin. Progression is in `club/progression.py`.

Explicitly **no** SPA, REST API, Redis, Celery, WebSockets, or extra services. Loyalty levels and milestones (thresholds, benefits, amounts) are database configuration, seeded by migration `0004`; do not hard-code them in logic.

## Commands

PostgreSQL must be running first (README: "Local PostgreSQL on Windows", port 55432). `.env` is git-ignored and holds local credentials; copy names from `.env.example`. Never print or commit its values.

```
uv run --locked --env-file .env python manage.py test            # full suite (~3 min)
uv run --locked --env-file .env python manage.py check
uv run --locked --env-file .env python manage.py makemigrations --check --dry-run
uv run --locked --env-file .env python manage.py migrate --check
uv lock --check
```

Tests create `test_<DB_NAME>`, so the DB role needs CREATEDB. The full suite reports 1 skipped test: the production smoke. Run it separately: set the production variables (synthetic test values only, see `docs/RENDER_DEPLOYMENT.md`), run `collectstatic --noinput` with `DJANGO_SETTINGS_MODULE=config.settings.production`, then `manage.py test config.test_production_smoke --settings=config.settings.production`. Gunicorn does not run on Windows; WSGI is only verifiable on Render/Linux. Production reads individual `DB_*` variables (not `DATABASE_URL`) and needs all `EMAIL_*` at boot.

## Roles (enforced server-side; hiding UI is not authorization)

- **PLAYER:** any active user; own dashboard, attendance, rewards, profile, own registration.
- **ORGANIZER** (group): sessions, rosters, check-in, walk-ins, lookup, completion, cancellation, redemption, scheduling.
- **ADMIN** (group or superuser): also attendance void/restore, exceptional cancellation of a completed session, identity linking, historical attendance addition to completed sessions, read-only audit review (`/organizer/audit/`). `is_staff` alone grants nothing.

## State machines

- **OpenPlay:** SCHEDULED to COMPLETED (only after its scheduled end), SCHEDULED to CANCELLED (reason required), COMPLETED to CANCELLED (ADMIN only).
- **Registration:** ACTIVE and CANCELLED are reversible only before start.
- **Attendance:** VALID and VOID by ADMIN correction with a reason. Ordinary check-in never restores VOID. Check-in window: 30 minutes before start until end.
- **Reward:** AVAILABLE to REDEEMED or REVOKED; REVOKED back to AVAILABLE if eligibility returns. REDEEMED is terminal and survives corrections (a discrepancy flag is set instead). Terms are snapshotted at issuance.

Identity: authentication is `User.email`. Walk-ins are unlinked Players; signup never claims one. ADMIN links explicitly; there is no merge. Link before a member first saves a profile.

## Conventions

- Mutations are `@transaction.atomic` and write their `AuditEvent` in the same transaction. Lock order: OpenPlay, then Player, then RewardIssuance.
- Historical records are never deleted; audit events are append-only.
- Times are stored in UTC; display and form input use `DJANGO_TIME_ZONE` (club is `Asia/Manila`).
- Tests are PostgreSQL-only; concurrency tests use `TransactionTestCase`.

## Working agreements

- Forensic mode: inspect the real code and trace the path before changing anything; distinguish real defects from theory.
- Smallest correct change plus a regression test that fails without it. Run the full suite before calling anything done.
- No scope expansion, no new infrastructure.
- No commits or pushes without explicit authorization. Staging only until told otherwise; never touch production.
- Never print or commit secrets.
- Treat documentation claims as unverified until you run the check.

## Status (as of 2026-10-04)

Run `git log origin/main..HEAD` to see unpushed work; push only when the owner authorizes it. The Render staging gate is parked pending the staging URL, the deploy logs, and the environment-variable checklist (names only); no staging evidence exists yet. The loyalty-configuration editor is the only remaining service-only V1 gap. Local development continues first.

## Known V1 gaps (service-only, no screen)

Loyalty-configuration editor (milestones and levels change only by migration or ORM edit, and existing rewards keep their snapshotted terms). Venue rename/deactivate and OpenPlay edit screens also do not exist (deliberately deferred; cancel and reschedule instead). See `docs/OPERATOR_RUNBOOK.md`.

Deployment: `docs/RENDER_DEPLOYMENT.md` and `docs/LAUNCH_READINESS.md`.
