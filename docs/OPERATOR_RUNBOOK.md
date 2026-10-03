# Got Cha Dink Club operator runbook

## Daily session

1. Confirm today's OpenPlays and venue in `/organizer/`. A technical operator must schedule new sessions through the existing `create_openplay` service; there is no scheduling screen.
2. Open the session roster on a phone. Search before adding a walk-in. Select an existing Player when found; create an unlinked walk-in only with name plus phone or provisional email.
3. Tap **Check in** once for each attendee. A repeat tap leaves one Attendance; a VOID row requires ADMIN review. Registrations and unchecked players do not count.
4. After the scheduled end, review checked-in and unchecked counts, then explicitly complete the session. Completion evaluates reward eligibility. Do not mark absent registrants present.
5. For a reward, locate the Player, verify an AVAILABLE entitlement, choose the OpenPlay it applies to, and confirm redemption. Apply the actual club benefit outside this system; no payment is processed.

## Corrections and identity

- ADMIN may void or restore attendance only with a specific reason. The record remains in history. An unused reward can be revoked and later reinstated; a redeemed reward remains redeemed and may show an eligibility discrepancy for manual resolution.
- A completed OpenPlay can be cancelled only by ADMIN with a reason. This removes its attendance from derived totals and reconciles entitlements without deleting records.
- Search by name and contact before creating a Player. To link an old walk-in, verify the person's identity and attendance history outside the application, then use the ADMIN-only `link_player_account` service with the correct Player and User IDs. It clears provisional contact email and preserves history. Do not link by matching email alone. Pause if two records could be the same person; there is no merge interface.
- A forgotten completion leaves checked-in progress visible but milestone rewards pending. Review the roster and complete the session after its scheduled end; do not manually issue a reward.

## Incidents

- **Database unavailable:** `/health/` returns 503. Stop check-in attempts, record attendance on a controlled paper roster, restore database service, then have ADMIN enter historical attendance with reasons after the session is completed. Avoid duplicate entries.
- **Application unavailable:** check host process and HTTPS proxy, then `/health/`, logs, and database connectivity. Use a controlled paper roster until recovery.
- **Duplicate check-in concern:** inspect the one Attendance row and its audit event. The database blocks a second row for the same Player/OpenPlay. Escalate identity duplicates to ADMIN; do not merge records during a live roster.
- **Reward discrepancy:** inspect attendance corrections, the issuance snapshot, redemption record, and audit history. Preserve redeemed history; resolve any club benefit dispute manually.
- **Forgotten password:** use the reset link on `/login/`. If email does not arrive, verify SMTP delivery and spam filtering; do not disclose whether a particular account exists.

## Restore verification

Follow the [backup and restore runbook](LAUNCH_READINESS.md#backup-and-restore-runbook) and [Render deployment procedure](RENDER_DEPLOYMENT.md#production-gate-and-recovery). Restore a Render PITR point or encrypted logical backup to a new isolated database, compare attendance/reward/audit row counts, run Django checks and migration check, then verify `/health/` and a controlled login. Do not overwrite the live database as a test. Rebuild static assets from the locked repository. Record who restored, when, backup timestamp, row-count comparison, and any data gap. The 24-hour RPO and 4-hour RTO are targets until a Render restore drill measures them.
