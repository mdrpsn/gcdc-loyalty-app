# Got Cha Dink Club operator runbook

## Daily session

1. Confirm today's OpenPlays and venue in `/organizer/`. Organizers and ADMINs schedule new sessions with **Schedule an OpenPlay** on that page, and add a venue first with **Add a venue** if none exists. Check the venue name before saving. There is no rename or deactivate screen; ask the technical operator to correct it. Enter start and end in the club timezone shown on the form. A clock time that is ambiguous or does not exist because of a daylight-saving change is rejected; choose an adjacent time. Check the session card afterward: there is no edit screen, so a wrong session is cancelled with a reason and rescheduled.
2. Open the session roster on a phone. Search before adding a walk-in. Select an existing Player when found; create an unlinked walk-in only with name plus phone or provisional email.
3. Tap **Check in** once for each attendee. A repeat tap leaves one Attendance; a VOID row requires ADMIN review. Registrations and unchecked players do not count.
4. After the scheduled end, review checked-in and unchecked counts, then explicitly complete the session. Completion evaluates reward eligibility. Do not mark absent registrants present.
5. For a reward, locate the Player, verify an AVAILABLE entitlement, choose the OpenPlay it applies to, and confirm redemption. Apply the actual club benefit outside this system; no payment is processed.

## Corrections and identity

- ADMIN may void or restore attendance only with a specific reason. The record remains in history. An unused reward can be revoked and later reinstated; a redeemed reward remains redeemed and may show an eligibility discrepancy for manual resolution.
- ADMIN reviews history at **Audit** in the organizer navigation. It is read-only, newest first, 50 per page, with times in the club timezone, and filters by action (exact, for example `REWARD_REDEEMED`), target type (for example `player`, `attendance`, `rewardissuance`), target id, and actor email. Invalid filters are ignored. **Audit events** on a player's record shows only events recorded against the player record itself (creation, linking, profile changes); attendance, registration, and reward events are found by target type, action, or the player id inside their details. Long details are shortened and credential-like keys are hidden; reasons are free text, so never type a password or token into one.
- A completed OpenPlay can be cancelled only by ADMIN with a reason. This removes its attendance from derived totals and reconciles entitlements without deleting records.
- Search by name and contact before creating a Player. To link an old walk-in, verify the person's identity and attendance history outside the application. Then an ADMIN opens **Link accounts** in the organizer navigation (or **Link to account** on an unlinked player's record), finds the unlinked player, enters the account's exact email, compares the two records side by side, ticks the identity-verified box, and confirms. Linking clears the provisional contact email and preserves history. Do not link by matching email alone. **Link before the member first signs in:** a member who saves their own profile gets a separate player record, and that account can then never be linked to a walk-in because there is no merge interface. Pause if two records could be the same person.
- A forgotten completion leaves checked-in progress visible but milestone rewards pending. Review the roster and complete the session after its scheduled end; do not manually issue a reward.

## Incidents

- **Database unavailable:** `/health/` returns 503. Stop check-in attempts, record attendance on a controlled paper roster, restore database service, then have ADMIN enter historical attendance with reasons after the session is completed. Avoid duplicate entries.
- **Application unavailable:** check host process and HTTPS proxy, then `/health/`, logs, and database connectivity. Use a controlled paper roster until recovery.
- **Duplicate check-in concern:** inspect the one Attendance row and its audit event. The database blocks a second row for the same Player/OpenPlay. Escalate identity duplicates to ADMIN; do not merge records during a live roster.
- **Reward discrepancy:** inspect attendance corrections, the issuance snapshot, redemption record, and audit history. Preserve redeemed history; resolve any club benefit dispute manually.
- **Forgotten password:** use the reset link on `/login/`. If email does not arrive, verify SMTP delivery and spam filtering; do not disclose whether a particular account exists.

## Restore verification

Follow the [backup and restore runbook](LAUNCH_READINESS.md#backup-and-restore-runbook) and [Render deployment procedure](RENDER_DEPLOYMENT.md#production-gate-and-recovery). Restore a Render PITR point or encrypted logical backup to a new isolated database, compare attendance/reward/audit row counts, run Django checks and migration check, then verify `/health/` and a controlled login. Do not overwrite the live database as a test. Rebuild static assets from the locked repository. Record who restored, when, backup timestamp, row-count comparison, and any data gap. The 24-hour RPO and 4-hour RTO are targets until a Render restore drill measures them.
