# Got Cha Dink Club

Production preparation and recovery procedures are in [Launch readiness](docs/LAUNCH_READINESS.md), [Render deployment](docs/RENDER_DEPLOYMENT.md), and [Operator runbook](docs/OPERATOR_RUNBOOK.md). These documents do not mean that hosting, SMTP, backups, or monitoring have been provisioned.

Server-rendered Django loyalty application for Got Cha Dink Club OpenPlay sessions. Members sign up, manage their profile and registrations, submit attendance for admin review, and view loyalty progress, rewards, streaks, and badges. Staff can run rosters, assist signed-up members, complete or cancel sessions, and redeem rewards. Admins approve or decline submitted attendance. The attendance ledger and loyalty engine remain the source of truth.

Phase 6 adds password recovery, timezone safeguards, role-change audit in Django admin, production configuration checks, and an operator runbook. It does not provision hosting, SMTP, monitoring, or backups; see the launch document before using real club data.

## Stack and prerequisites

- Python 3.14
- Django 6.1 and psycopg 3, resolved exactly in `uv.lock`
- PostgreSQL 18 for development and production-compatible database behavior
- [uv](https://docs.astral.sh/uv/) for the locked virtual environment

The project was verified on Windows 10 with Python 3.14.7, Django 6.1.1, psycopg 3.3.6, and PostgreSQL 18.6. Use `uv sync --locked` to reproduce the Python dependencies. PostgreSQL runs separately and is never replaced with SQLite.

## Local PostgreSQL on Windows

Install PostgreSQL 18 or download the Windows binary archive linked from the [PostgreSQL Windows downloads page](https://www.postgresql.org/download/windows/). A user-owned archive extraction works without a system service. The commands below assume its `pgsql` folder is at `%LOCALAPPDATA%\gcdc-postgres\pgsql`. Run these initialization commands **once** in PowerShell; `initdb` prompts for the local postgres password:

```powershell
$pgRoot = Join-Path $env:LOCALAPPDATA 'gcdc-postgres'
& "$pgRoot\pgsql\bin\initdb.exe" -D "$pgRoot\data" -U postgres -A scram-sha-256 -W -E UTF8
& "$pgRoot\pgsql\bin\pg_ctl.exe" -D "$pgRoot\data" -l "$pgRoot\server.log" -o '-h 127.0.0.1 -p 55432' start
& "$pgRoot\pgsql\bin\createuser.exe" -h 127.0.0.1 -p 55432 -U postgres --pwprompt --createdb gcdc_app
& "$pgRoot\pgsql\bin\createdb.exe" -h 127.0.0.1 -p 55432 -U postgres -O gcdc_app gcdc_dev
```

The development role needs `CREATEDB` because Django creates an isolated `test_gcdc_dev` database for tests. A production application role should not have that privilege. On later local sessions, start the server with the `pg_ctl ... start` command; stop it with `pg_ctl -D "$pgRoot\data" stop`. An existing PostgreSQL installation is equally suitable: create a role and database, then put its connection details in `.env`.

## Python environment and configuration

From the repository root:

```powershell
uv sync --locked
Copy-Item .env.example .env
```

Fill in `.env` with local values. Generate `DJANGO_SECRET_KEY` rather than copying a shared value:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Required development variables are `DJANGO_SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`. For the PostgreSQL example above, use database `gcdc_dev`, user `gcdc_app`, host `127.0.0.1`, and port `55432`, with the password chosen during `createuser`. `DB_SSLMODE=disable` is suitable for that loopback-only server. Development defaults to UTC if `DJANGO_TIME_ZONE` is unset; set it to the owner-confirmed club timezone, `Asia/Manila`, when checking club-local dates and times. The `.env` file is ignored by Git; `.env.example` contains names only.

The application reads process environment variables. `uv run --env-file .env` loads the local file without an extra application dependency. Direct `python manage.py` commands work if the same variables are already present in the shell.

## Initialize and run

```powershell
uv run --locked --env-file .env python manage.py check
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env python manage.py createsuperuser
uv run --locked --env-file .env python manage.py runserver
```

`createsuperuser` asks for an email and password. There is no username field. The Django admin at `/admin/` remains for User and Group administration. Assign staff to the `ORGANIZER` or `ADMIN` Group there; `is_staff` alone does not grant organizer access. Members begin at `/signup/` with their email, open the 30-minute verification link, then enter full name, contact number, ReClub name and profile link, and a password. Passwords need at least six letters or numbers, including at least one of each; symbols are allowed, while common and email-similar passwords remain blocked. No account is created until that form is completed. Local development writes verification email to ignored `.local-emails/`; production needs working SMTP. Members sign in at `/login/`. Staff use `/organizer/`; members use `/player/`. Signup never claims an existing walk-in record automatically; an admin must verify identity and link it. Existing sessions can be scheduled at `/organizer/schedule/openplays/new/`.

The health endpoint is `GET /health/`. It returns HTTP 200 with `{"status":"ok"}` when Django can query PostgreSQL and HTTP 503 with `{"status":"unhealthy"}` when the database query fails. It does not expose database connection details.

## Tests and migration checks

```powershell
uv run --locked --env-file .env python manage.py test --settings=config.settings.test
uv run --locked --env-file .env python manage.py makemigrations --check --dry-run
uv run --locked --env-file .env python manage.py migrate --check
```

`config.settings.test` is development settings with a fast password hasher (the full suite then takes seconds instead of minutes); it is for tests only. The production-settings smoke test must still run on production settings so the real PBKDF2 login path stays covered (see the launch checklist). Tests create a temporary PostgreSQL database; the test suite asserts that it is connected to PostgreSQL. Project migrations create the custom User, Phase 2 domain models, and Phase 3 loyalty models. Data migrations seed the ORGANIZER and ADMIN groups and the initial loyalty configuration. Django's built-in applications supply their own migrations.

The [CI release check](.github/workflows/ci.yml) runs on pull requests, pushes to `main`, and manual dispatch. It uses Python from `.python-version`, PostgreSQL 18, and synthetic settings to check `uv.lock`, install locked dependencies, verify and apply migrations, run the full suite, collect static assets, and run the production-settings smoke tests. The workflow has read-only repository permission and contains no deployment step. Its command sequence and YAML lint passed locally; the first GitHub-hosted run remains to be verified after an authorized push.

## Current domain operations

The `club.services` module is the supported entry point for creating Players and sessions, registrations, attendance submissions and reviews, session completion/cancellation, and ADMIN corrections. `find_players()` searches candidates but never links identities automatically. `link_player_account()` is an explicit ADMIN operation. An active account may access its own Player record; ORGANIZER and ADMIN are Django Groups seeded by migration.

Players submit attendance after an OpenPlay starts. The session choice shows its club-local date, Morning/Afternoon/Evening period, exact time, and venue so multiple sessions on one day remain distinct. A submission is PENDING until an ADMIN approves it; approval creates VALID Attendance. Declined submissions may be corrected and resubmitted. The current **OpenPlays Attended** count comes from `openplays_attended(player)`: it counts VALID Attendance linked to a non-CANCELLED OpenPlay. Registration and pending submissions never count. Historical rows are retained, and important service mutations write AuditEvent records in the same transaction.

## Loyalty engine

`club.loyalty` provides `get_openplays_attended`, `get_loyalty_level`, `get_eligible_milestones`, `get_loyalty_summary`, `reconcile_player_loyalty`, and `redeem_reward`. Levels use the OpenPlays Attended count after attendance approval. **Reward eligibility uses only valid attendance on COMPLETED sessions.** Approval on a completed session reconciles rewards immediately; approval before completion updates attendance progress, then completion evaluates rewards.

The seeded levels begin at 0/5/10/20/30. The seeded milestones are Dinker at 10 (₱50 discount), Dink Addict at 20 (₱100 discount), Court Regular at 30 (free OpenPlay), Got Cha Loyalist at 50 (free OpenPlay and recognition), Club Veteran at 75 (free OpenPlay and club perk), and Century Club at 100 (recognition and premium reward). Milestone benefits are configuration data; issuance captures a terms snapshot. Each milestone creates one bundled entitlement per Player. Redemption consumes that entitlement once; staff apply any discount, club item, or recognition operationally.

Completion, cancellation, and ADMIN attendance additions/voids/restorations reconcile affected Players in their transactions. Available rewards become REVOKED if completed attendance falls below the snapshotted threshold and return to AVAILABLE when it is restored. REDEEMED rewards remain redeemed; an eligibility discrepancy timestamp and audit event record a later shortfall. `redeem_reward` requires an ORGANIZER or ADMIN and records the OpenPlay selected by staff. No payment processing exists.

## Organizer workflow

At `/organizer/`, open today's session. The roster shows registrations, pending submissions, and approved attendance. Staff may submit for an existing player only after the player has a linked account. Ask a new player to sign up at `/signup/` first; a legacy unlinked walk-in needs admin identity verification and account linking. Submissions remain pending. ADMIN reviews them at `/organizer/attendance-submissions/` using All, Pending approval, Approved, and Declined views; verify actual attendance before approving. A declined claim needs a reason. VOID attendance still needs an ADMIN correction.

After the scheduled end, review counts on the completion confirmation page and explicitly complete the session. This calls the existing transactional domain service and reconciles rewards. Session cancellation requires a reason. The player lookup shows derived attendance, level, reward status, and recent history. Staff redeem an AVAILABLE reward against a selected OpenPlay; ADMIN users can void or restore historical attendance with an audit reason.

Set `DJANGO_TIME_ZONE=Asia/Manila` before using the organizer screens: today's list, displayed session periods, and submission times depend on it. Development still defaults to UTC when this variable is unset. The interface is server-rendered with responsive CSS and works without JavaScript.

## Player experience

Members use `/player/` for OpenPlays Attended, next configured milestone, current level, available rewards, current and best streak, and the First Rally and On a Roll badges. `/player/openplays/` lists upcoming sessions and provides register/cancel actions before the scheduled start. A new player must sign up before submitting attendance. At `/player/attendance/submit/`, signed-in members choose a session; their saved ReClub details are included without reentry. `/player/attendance/` shows pending, approved, declined, void, and cancelled history. `/player/profile/` permits full name, phone, and ReClub edits; the login email stays on User.

Approval increases OpenPlays Attended; milestone rewards become available only after both approval and session completion. Current and best streak are derived from completed sessions: approved attendance extends the run, an active registration without valid attendance breaks it, and cancelled registrations, unregistered sessions, cancelled sessions, and incomplete sessions are neutral. Approved walk-in attendance counts. The two badges are derived from completed attendance and best streak, with no stored badge or revocation state; a historical correction can change current qualification.

## Production configuration concepts

Set `DJANGO_SETTINGS_MODULE=config.settings.production` in the hosting environment. Production requires explicit `DJANGO_SECRET_KEY`, database credentials and `DB_SSLMODE`, `DJANGO_ALLOWED_HOSTS`, HTTPS `DJANGO_CSRF_TRUSTED_ORIGINS`, a valid IANA `DJANGO_TIME_ZONE`, and SMTP settings. It rejects missing values and `DJANGO_DEBUG=true`; it forces HTTPS redirect and secure session/CSRF cookies. Set `DJANGO_TRUST_PROXY_SSL=true` only after verifying that the trusted proxy overwrites `X-Forwarded-Proto`. HSTS remains off until the real domain and every relevant subdomain have verified HTTPS. See the authoritative [launch checklist](docs/LAUNCH_READINESS.md#production-checklist) and HSTS sequence.

The approved target is Render: one Python 3.14 WSGI web service and one paid Render PostgreSQL instance per environment, with separate staging and production resources in Singapore. Gunicorn and WhiteNoise are locked dependencies. Render's build collects files into `staticfiles/`; WhiteNoise serves `/static/` from the WSGI service. The exact build, pre-deploy migration, start, health, and environment settings are in the [Render deployment guide](docs/RENDER_DEPLOYMENT.md). Exact paid plans, production domain, staging sender address, and monitoring alert recipient still require explicit decisions. The approved club timezone is `Asia/Manila`, but it has not been configured on a hosted service. No Render service has been provisioned.

Local reset emails are written to ignored `.local-emails/`; production uses Resend SMTP after sender verification. A repeatable local restore check is `uv run --locked --env-file .env python scripts/verify_local_restore.py`. Render PITR, offsite logical backups, Better Stack alerts, and real SMTP delivery are not configured yet.
