# Launch readiness: authoritative checklist and runbook

**Current classification: READY PENDING EXTERNAL CONFIGURATION.** This document records local capability, not a public deployment. Render web hosting and Render PostgreSQL, Resend SMTP, and Better Stack monitoring are approved as providers; no accounts or services have been provisioned. The [Render deployment guide](RENDER_DEPLOYMENT.md) gives the exact packaging and staging procedure. Keep staging and production resources separate and the database private to its same-region web service. No worker, cache, or separate frontend is required.

Status meanings: **READY** = implemented and locally verified; **NOT CONFIGURED** = no real external resource or value exists; **REQUIRES EXTERNAL VERIFICATION** = code or procedure exists but must be tested against the selected service/domain/provider. No external dependency is marked READY based on a local test.

## Local hardening verification (2026-10-04)

**Locally verified:** `uv run --locked --env-file .env python manage.py test --verbosity 1` ran 133 PostgreSQL tests successfully (one skipped: the production-settings smoke test, run separately with production settings). `check`, `makemigrations --check --dry-run`, `migrate --check`, and `collectstatic --noinput --dry-run` passed. `uv lock --check` passed. With production settings and synthetic hostname/SMTP values, `collectstatic` and the two HTTPS-aware `config.test_production_smoke` tests passed; no email was sent. `check --deploy` reported only W004 because HSTS remains intentionally off until domain HTTPS is verified. The synthetic local dump/restore script passed and compared one user, one player, ten attendances, one issuance, one redemption, and 24 audit events. The PostgreSQL request suite includes a 101-player roster assertion of fewer than 20 queries.

**Requires Render/staging verification:** Linux Gunicorn startup, proxy headers, external HTTPS and static responses, real SMTP delivery, monitoring and alert delivery, provider backups and isolated restore, and staging performance. The local computer session exposed no browser surface, so desktop and 390/430px visual journeys were not verified here; request and production-smoke tests do not replace that review.

**Requires real-club pilot validation:** Organizer phone usability, operational identity checks, and the club's actual check-in and reward handling. These local checks are not a penetration test.

## Production checklist

| Requirement | Status | Action/evidence required before launch |
| --- | --- | --- |
| Locked Django/PostgreSQL build and WSGI entry point | READY | uv.lock, Python 3.14 pin, Gunicorn, and config.wsgi; install with uv sync --locked |
| Production domain and managed HTTPS | NOT CONFIGURED | Select domain/provider; provision certificate and verify external HTTPS |
| DJANGO_SECRET_KEY | NOT CONFIGURED | Generate a long random secret in the host secret manager; never use local .env |
| DJANGO_ALLOWED_HOSTS | NOT CONFIGURED | Set explicit production hostnames, no wildcard |
| DJANGO_CSRF_TRUSTED_ORIGINS | NOT CONFIGURED | Set explicit HTTPS origin(s) for the real domain |
| DJANGO_TIME_ZONE | NOT CONFIGURED | Set owner-confirmed `Asia/Manila` explicitly on each hosted service and verify local display |
| PostgreSQL credentials and DB_SSLMODE | NOT CONFIGURED | Provision managed database and restricted credentials; use provider-supported verified TLS mode |
| Proxy/HTTPS configuration | REQUIRES EXTERNAL VERIFICATION | Confirm proxy removes client-supplied X-Forwarded-Proto, sets its own value, then set DJANGO_TRUST_PROXY_SSL=true; verify redirect and secure cookies |
| SMTP credentials and DEFAULT_FROM_EMAIL | NOT CONFIGURED | Select provider and verify sender domain, TLS, delivery, bounces, and reset messages |
| Static files | REQUIRES EXTERNAL VERIFICATION | WhiteNoise serves collected /static/ from the WSGI service; verify CSS over Render HTTPS |
| Encrypted, off-site PostgreSQL backups | NOT CONFIGURED | Configure scheduled backups, access control, retention, alerts, and an isolated restore |
| Availability/error/log monitoring | NOT CONFIGURED | Route JSON process logs, HTTP 5xx, failed /health/, database/SMTP/backup/storage failures to an alert destination |
| Login/reset abuse limits | NOT CONFIGURED | At trusted edge, limit POST /login/, /password-reset/, and reset-confirmation attempts by IP and normalized account where possible; preserve non-enumerating responses |
| HSTS | REQUIRES EXTERNAL VERIFICATION | Keep DJANGO_HSTS_SECONDS=0 until domain and HTTPS checks below pass |
| Real-domain check --deploy | REQUIRES EXTERNAL VERIFICATION | Run with actual secrets/domain; review every warning, including W004/W005/W021 according to HSTS stage |
| External HTTPS smoke, login/logout, reset mail | REQUIRES EXTERNAL VERIFICATION | Exercise browser over real HTTPS with staging accounts and actual SMTP |
| Player privacy, organizer and ADMIN authorization | REQUIRES EXTERNAL VERIFICATION | Repeat role checks in staging using individual accounts; automated PostgreSQL tests pass locally |
| Check-in, duplicate check-in, completion, reward issuance/redemption | REQUIRES EXTERNAL VERIFICATION | Rehearse with synthetic staging sessions, never false production attendance |
| Backup, restore, audit trail, timezone/DST | REQUIRES EXTERNAL VERIFICATION | Restore a real backup into staging, compare ledger rows, check audit events, and verify local display times |

## Application configuration

Set DJANGO_SETTINGS_MODULE=config.settings.production for the hosted process and release commands. Production fails startup if DJANGO_SECRET_KEY, DJANGO_TIME_ZONE, DJANGO_ALLOWED_HOSTS, DJANGO_CSRF_TRUSTED_ORIGINS, DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_SSLMODE, EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, or DEFAULT_FROM_EMAIL is absent. The timezone must resolve as an IANA zone. Hosts and CSRF origins must be explicit; trusted origins must be HTTPS. Production rejects DJANGO_DEBUG=true and forces DEBUG=False, HTTPS redirect, secure session and CSRF cookies, a 12-hour session lifetime, and browser-close expiry. Base settings never supply a secret or database fallback.

For Render's same-region internal PostgreSQL hostname, set DB_SSLMODE=require: Render documents self-signed internal certificates, so verify-ca and verify-full are unavailable on that connection. Production settings reject unencrypted SSL modes for non-loopback database hosts; `disable` is accepted only for isolated local testing. Set DJANGO_TRUST_PROXY_SSL=true **only** after confirming that Render overwrites the X-Forwarded-Proto header. Otherwise an untrusted client could misrepresent request security. Render redirects public HTTP to HTTPS at its edge. Django redirects other insecure routes; /health/ alone is exempt so Render's private HTTP probe reaches the database check instead of passing on a 301.

Development writes password-reset messages to ignored .local-emails/ files. Treat those files as sensitive and remove them when no longer needed. Production uses Django SMTP and requires real credentials; no provider integration or delivery is verified. It requires exactly one of EMAIL_USE_TLS (default true for STARTTLS) and EMAIL_USE_SSL (default false for implicit TLS); EMAIL_TIMEOUT defaults to 10 seconds. Select the mode and port specified by the provider. Reset tokens expire after 24 hours, become unusable after the password changes, and are absent from AuditEvent details. Password changes and audit events commit together. Edge rate limiting is still mandatory: start with at most five login or reset POST attempts per 15 minutes per source IP and account identifier, review false positives for shared venue networks, and alert on sustained failure. The exact enforcement mechanism depends on the chosen proxy/provider; the application has no shared rate-limit store.

Production emits JSON error logs to standard error with timestamp, level, logger, event, HTTP status, method, named route, and exception class when available. It deliberately omits raw message, exception value, URL/path, query, request body, headers, tokens, passwords, and database credentials. Capture these logs at the host with restricted access and retention. Configure proxy access logs and error reporting to redact reset-token URL paths, query strings, cookies, and request bodies; application formatting does not control provider logs. The /health/ endpoint checks database connectivity and logs a safe database_unavailable event on failure; it returns 503 without database details. Monitoring and alert routing are **not configured**.

PostgreSQL stores aware UTC timestamps. DJANGO_TIME_ZONE drives club-local presentation and today's organizer list; the browser timezone does not choose displayed times. Session cards include timezone abbreviations to distinguish DST repeated clock times. There is no per-venue timezone. The owner confirmed `Asia/Manila`; it must still be set and verified in each hosted environment.

## HSTS and domain sequence

1. Select the real domain and confirm DNS ownership.
2. Verify the certificate and HTTPS response for the application and every required subdomain.
3. Verify HTTP redirects to HTTPS at the edge and through Django, including proxy headers.
4. Start with a short DJANGO_HSTS_SECONDS only after the preceding checks; increase deliberately after monitoring.
5. Enable DJANGO_HSTS_INCLUDE_SUBDOMAINS only after every relevant subdomain is HTTPS-ready.
6. Consider DJANGO_HSTS_PRELOAD only if the domain is permanently HTTPS-only and its operator accepts the long-lived browser commitment.

Local check --deploy with safe test values reports W004 while HSTS is zero. Enabling HSTS without subdomain/preload flags can report W005/W021. These are domain decisions, not warnings to suppress for a green check.

## Staging and release sequence

1. Use the approved Render web/PostgreSQL, Resend SMTP, and Better Stack direction. Use Singapore for both Render resources and `Asia/Manila` for the club timezone. Select the still-open exact plans, production domain, staging sender address, monitoring recipient, and named operator/deputy. Create a separate staging environment as described in [Render deployment](RENDER_DEPLOYMENT.md).
2. Configure production values through the host secret manager. Give the runtime database role only required application privileges; use a separate test role for Django tests, which create a test_ database.
3. Build from the repository with uv sync --locked. Run the full PostgreSQL suite, check, makemigrations --check --dry-run, migrate --check, and check --deploy against staging. The persistent HTTPS/CSRF workflow test is: uv run --locked --env-file .env python manage.py test config.test_production_smoke --settings=config.settings.production. Use only isolated test credentials and database rights for this command.
4. Take a verified pre-migration backup. Render collects static assets during build and runs `uv run --locked --no-sync python manage.py migrate --noinput --settings=config.settings.production` as a separate pre-deploy command. WhiteNoise serves STATIC_ROOT at /static/ from the WSGI service.
5. Start config.wsgi behind managed HTTPS. Create individual admin and organizer accounts, configure group membership, and protect /admin/ through the hosting network/access policy in addition to Django authentication.
6. Confirm /health/, external HTTPS login/logout, reset email delivery, player privacy, registration, roster, duplicate-safe check-in, completion, reward availability and redemption, audit events, and CSS. Exercise a DST boundary in staging if the club timezone observes DST.
7. Verify alert delivery for 5xx, health/database failure, SMTP delivery failure, backup failure, and storage failure. Record the release and monitor errors after launch.

Do not send live production reset emails or create false attendance merely to satisfy a smoke test. Use staging for full flows and non-mutating checks on live production.

## Backup and restore runbook

**Proposed targets, pending club/provider approval:** recovery point objective (RPO) at most 24 hours and recovery time objective (RTO) at most four hours. Daily encrypted off-site backups retained for 30 days, monthly copies for 12 months, and a verified pre-migration backup are the minimum proposed policy. If the club needs less than 24 hours of potential data loss, enable provider point-in-time recovery and revise the RPO. The designated club technical operator performs restores; a named deputy must have tested access. Neither person nor backup provider is assigned yet, so these are targets, not operational guarantees.

1. During an incident, stop mutations or place the service in maintenance mode, record the last good backup timestamp and expected data gap, and notify the operator/deputy.
2. Obtain an encrypted backup through the provider's restricted recovery access. Restore into a **new isolated PostgreSQL database**, never over the live database. For a custom-format dump use pg_restore --no-owner --no-acl --exit-on-error -d <isolated_database> <backup.dump> after provisioning the empty target and its credentials.
3. Point an isolated application instance at the restored database. Run python manage.py check and python manage.py migrate --check; compare accounts_user, club_player, club_attendance, club_rewardissuance, club_rewardredemption, and club_auditevent counts with the backup manifest/source. Verify /health/, controlled login, a sample player's ledger, reward history, and audit history. Run the Django suite only against a separate test-capable clone; tests create and destroy their own test_ database.
4. Record restore duration, backup age, row counts, any discrepancy, operator, and decision to cut over. Rotate/recover host secrets through its secret manager. Rebuild static assets from the locked repository.
5. For a failed migration, diagnose before retrying. Restore the pre-migration backup to a new database and verify it before redirecting traffic. Never assume a migration reversal is safe.

The local synthetic exercise is reproducible with uv run --locked --env-file .env python scripts/verify_local_restore.py. It creates two randomly named **local** scratch PostgreSQL databases, seeds attendance/reward/redemption/audit data, dumps, restores, compares row counts, runs checks, and removes only its own databases. It refuses a non-loopback database host and production settings. It does **not** prove provider backups, encryption, off-site retention, alerting, or the proposed RPO/RTO. Run a real provider restore rehearsal before launch and periodically thereafter, at least monthly initially.

## Operational boundaries

The supported domain mutations are in club.services and club.loyalty; important events are audited in the same transaction. Database-owner writes can bypass service audit, so restrict that credential. is_staff alone does not grant organizer business permissions. Organizers and ADMINs schedule sessions at /organizer/schedule/openplays/new/ and add venues at /organizer/schedule/venues/new/; both call the create_openplay() and create_venue() services, and times are read in the club timezone. There is no public signup or player merge UI. Explicit ADMIN account linking requires human identity verification. Redeemed rewards remain historical after a correction; an eligibility discrepancy is flagged for human resolution.
