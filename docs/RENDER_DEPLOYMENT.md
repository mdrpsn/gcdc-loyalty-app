# Render deployment package

This is a staging and production procedure, not evidence of a deployed service. The approved shape is one Render Python WSGI web service and one paid Render PostgreSQL database per environment, both in Singapore. Staging and production must have separate services, databases, secrets, and application users. The club operating timezone is confirmed as `Asia/Manila`. Exact paid plans, production domain, staging sender address, and alert recipient remain unspecified; select the plans before creating resources rather than accepting defaults. The reviewed initial commit is on the approved `origin/main`; later changes require a separate push authorization.

## Build, release, and start

Render's native Python runtime reads `.python-version` (`3.14.7`, the locally verified patch) and provides uv when `uv.lock` is present. Confirm that Render installs this exact patch in staging. Use these exact web-service settings for each environment:

| Render field | Value |
| --- | --- |
| Runtime | Python |
| Build command | `uv sync --locked --no-dev && uv run --locked --no-sync python manage.py collectstatic --noinput --settings=config.settings.production` |
| Pre-deploy command | `uv run --locked --no-sync python manage.py migrate --noinput --settings=config.settings.production` |
| Start command | `uv run --locked --no-sync gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 4` |
| HTTP health-check path | `/health/` |
| Auto-deploy | Off; deploy manually only after a verified pre-migration backup and review |

Use a **paid** web plan so the pre-deploy command is available, and a **paid** PostgreSQL plan so PITR is available. Record the selected plan and same region for both resources before creation. The web service must listen on Render's `PORT` (normally 10000). Gunicorn uses WSGI and its default disabled access log; do not enable raw request-path logging because reset URLs contain tokens. `collectstatic` runs during the build so its output is present in the deployed filesystem. Migrations run once in Render's pre-deploy phase, never during build or on each process start. If migration fails, the deploy fails; assess database recovery before retrying. A failed deploy does not automatically reverse a committed migration, so migration compatibility with the previous web version must be reviewed.

Production settings add WhiteNoise immediately after SecurityMiddleware and use its compressed manifest static storage. The WSGI service serves collected `/static/` assets without Django `runserver`, a separate static service, or persistent disk. CSS uses hashed URLs and should be checked over staging HTTPS after deployment. Uploaded files are not part of V1. WhiteNoise 6.12 documentation currently lists Django support through 6.0 while this project uses Django 6.1; the local production-settings smoke verifies the specific behavior used here, and the Render Linux build and static response require staging verification.

## Environment configuration

Set `DJANGO_SETTINGS_MODULE=config.settings.production` on **both** web services and their pre-deploy commands. Render service variables are per environment; never share staging and production secrets. The committed `.env.example` lists variable names only. Production startup deliberately fails when required settings are absent.

| Existing application key | Render source |
| --- | --- |
| `DJANGO_SECRET_KEY` | Unique secret generated and stored in that web service's Render environment |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Corresponding **individual** internal connection details on that environment's Render Postgres Info/Connect page. `DB_HOST` is the internal hostname; port is ordinarily `5432`. Do not paste the assembled URL into any of these fields. |
| `DB_SSLMODE` | `require` for Render's internal connection. Render's internal certificates are self-signed, so `verify-ca`/`verify-full` are not supported on that path. Never use `disable` for an external connection. |
| `DJANGO_ALLOWED_HOSTS` | Explicit Render staging hostname or approved production domain, and any additional host actually used for health checks. No wildcard. Update after a custom domain is verified. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Corresponding explicit `https://` origin(s). No wildcard or HTTP origin. |
| `DJANGO_TIME_ZONE` | Set the owner-confirmed `Asia/Manila` explicitly in each environment. Do not copy the development machine's timezone. |
| `DJANGO_TRUST_PROXY_SSL` | Set `true` only after staging confirms Render overwrites untrusted `X-Forwarded-Proto` and Django sees external HTTPS correctly. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | Resend SMTP host, port, fixed username, and a distinct secret API key per environment. Resend documents `smtp.resend.com`, username `resend`, and STARTTLS port `587`; use `EMAIL_USE_TLS=true`, `EMAIL_USE_SSL=false` for this choice. |
| `DEFAULT_FROM_EMAIL` | Owner-approved address at a verified Resend sender domain. |

Optional controls are `EMAIL_TIMEOUT` (default 10 seconds) and the `DJANGO_HSTS_*` settings. Keep HSTS at its default zero until the real domain and all required subdomains have verified HTTPS and redirects. Production forces `DEBUG=False`, secure session and CSRF cookies, and HTTPS redirect; do not set `DJANGO_DEBUG=true`. Render's edge redirects public HTTP to HTTPS. `/health/` alone is exempt from Django's redirect because Render's private HTTP probe would otherwise receive a 301 that Render considers healthy without running the database check. Confirm in staging that the probe receives 200 when PostgreSQL is available and 503 when it is unavailable; other HTTP application routes must redirect. Confirm the proxy's forwarded-header behavior before enabling `DJANGO_TRUST_PROXY_SSL` or using password-reset links.

The current Django settings do **not** read `DATABASE_URL`. Keep the individual `DB_*` variables; changing to a URL requires a separate, tested application change. Use the private same-region connection. Restrict external database access after confirming that backups and restore operations still work. A production app role must not have `CREATEDB`; tests run only against an isolated test-capable PostgreSQL role/database.

## Local package verification

On the current Windows development machine, `uv sync --locked --no-dev`, the full PostgreSQL test suite, Django checks, migration checks, and `collectstatic` can run locally. For the production-settings smoke, use the local test-capable PostgreSQL role, set `DJANGO_SETTINGS_MODULE=config.settings.production`, a **test-only** IANA timezone and HTTPS hostname/origin, test-only SMTP values, and `DB_SSLMODE=disable` solely for the loopback PostgreSQL connection. Run production `collectstatic` before `python manage.py test config.test_production_smoke --settings=config.settings.production`; manifest storage requires the collected files when rendering pages. These are isolated test values, never Render credentials. The smoke exercises HTTPS-aware requests, CSRF, the club lifecycle, CSS response, and the private HTTP health probe's database failure response.

Gunicorn uses Unix `fcntl` and cannot execute on this Windows host. A local standard-library WSGI server verified `config.wsgi` over HTTP with `/health/` returning 200, hashed player CSS returning 200 when a trusted proxy marks the request HTTPS, and `/login/` redirecting insecure HTTP. The **exact Gunicorn start command, Render build image, Render health probe, proxy headers, and static response remain staging checks**. There is no committed `render.yaml`: exact plans remain unselected, and an incomplete Blueprint could silently use an unapproved plan. Configure the fields in the Render Dashboard exactly as listed above after those decisions are made.

## Staging procedure

1. Select and record exact paid web and database plans, a staging hostname, and staging-only secrets. Place both resources in approved Singapore. Create a separate staging database and web service from the reviewed Git repository. Disable auto-deploy.
2. Configure every required environment key above. Verify `uv sync --locked` in a clean build, Python 3.14, the build command, collected CSS, and the pre-deploy migration command against staging only.
3. Confirm `/health/` over HTTPS and the internal Render health probe. Confirm HTTPS redirects, CSRF, secure cookies, login/logout, password-reset link scheme, player privacy, and organizer/ADMIN authorization.
4. Exercise registration, roster, duplicate-safe check-in, walk-in, session completion, reward issuance/redemption, and audit records with synthetic staging data. Verify club-local display times.
5. Verify a Resend reset email reaches a controlled mailbox and inspect delivery/bounce status. Do not send a real user's reset email as a smoke test.
6. Configure Better Stack to check external HTTPS `/health/` and alert on failure. Route repeated 5xx and safe JSON application errors from Render logs/metrics; monitor Render PostgreSQL availability, storage/resource pressure, Resend delivery failures, and backup-job failure or missed heartbeat. Restrict log access and redact reset-token paths, cookies, request bodies, and query strings in provider access logs.
7. Enable and inspect Render paid Postgres PITR. Configure **separate encrypted logical backups** to storage outside the database instance, daily with 30-day retention and monthly with 12-month retention. Alert on a failed or missing backup. Take and verify a pre-migration backup before every deployment that changes schema.
8. Restore a provider backup and a logical backup into separate isolated databases, compare account/attendance/reward/redemption/audit counts, run `check` and `migrate --check`, and exercise controlled login and `/health/`. Record backup age and measured restore duration. The 24-hour RPO and 4-hour RTO are targets until these drills demonstrate them.

## Production gate and recovery

Production creation and deployment are **not authorized** by this document. Before requesting approval, record the approved domain, IANA timezone, Resend sender, Better Stack alert recipient, plans/region, named operator and deputy, verified staging results, working backups and restore, and individual ADMIN and ORGANIZER accounts. Repeat non-destructive HTTPS, health, login/logout, static CSS, role, and monitoring checks on production; run full attendance/reward mutations in staging rather than creating false production history.

Before each production migration, verify a recent encrypted backup and record its timestamp. Render PITR on paid PostgreSQL can restore to a **new database** within the workspace plan's window (currently three days on Hobby, seven on Pro or higher). Render's logical exports are retained seven days, so they do not satisfy the separate 30-day/12-month policy. For an incident, pause mutations, restore PITR or the encrypted logical dump into a new isolated database, verify migrations, health, ledger and audit counts, then decide whether to cut over by updating the five `DB_*` settings. Never overwrite the live database as a rehearsal. The named operator and deputy must practice this procedure and record actual RPO/RTO.

## Source documentation

- [Render Python and uv versions](https://render.com/docs/python-version)
- [Render deploy steps and pre-deploy commands](https://render.com/docs/deploys)
- [Render web port and HTTPS behavior](https://render.com/docs/web-services)
- [Render health checks](https://render.com/docs/health-checks)
- [Render PostgreSQL connections and SSL modes](https://render.com/docs/postgresql-creating-connecting)
- [Render PostgreSQL PITR and exports](https://render.com/docs/postgresql-backups)
- [Resend SMTP settings](https://resend.com/docs/send-with-smtp)
