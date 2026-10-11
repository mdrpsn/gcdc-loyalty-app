from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import (
    get_eligible_milestones,
    get_loyalty_level,
    get_loyalty_summary,
    get_openplays_attended,
    reconcile_player_loyalty,
    redeem_reward,
)
from club.models import (
    Attendance,
    AuditEvent,
    LoyaltyLevel,
    LoyaltyMilestone,
    OpenPlay,
    Player,
    RewardIssuance,
    RewardRedemption,
    Venue,
)
from club.services import (
    add_historical_attendance,
    cancel_openplay,
    check_in,
    complete_openplay,
    restore_attendance,
    void_attendance,
)


class LoyaltyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin@example.com")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.organizer = User.objects.create_user("organizer@example.com")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.member = User.objects.create_user("member@example.com")
        self.player = Player.objects.create(user=self.member, display_name="Member")
        self.other = Player.objects.create(display_name="Other", phone="123")
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        self.sessions = OpenPlay.objects.bulk_create([
            OpenPlay(
                venue=self.venue,
                starts_at=self.now - timedelta(days=i + 2),
                ends_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
                status=OpenPlay.Status.COMPLETED,
                completed_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
            )
            for i in range(100)
        ])
        self.next_session = 0

    def add_history(self, count):
        selected = self.sessions[self.next_session:self.next_session + count]
        Attendance.objects.bulk_create([
            Attendance(
                player=self.player,
                openplay=session,
                source=Attendance.Source.HISTORICAL,
                recorded_by=self.admin,
                checked_in_at=session.starts_at,
            )
            for session in selected
        ])
        self.next_session += count
        return selected

    def test_initial_configuration_and_level_boundaries(self):
        self.assertEqual(
            list(LoyaltyLevel.objects.values_list("name", "minimum_attendances")),
            [("Rookie", 0), ("Regular", 5), ("Player", 10), ("Court Regular", 20), ("Legend", 30)],
        )
        self.assertEqual(
            list(LoyaltyMilestone.objects.filter(is_active=True).values_list("threshold", "name")),
            [(10, "Dinker"), (20, "Dink Addict"), (30, "Court Regular"),
             (50, "Got Cha Loyalist"), (75, "Club Veteran"), (100, "Century Club")],
        )
        # The interim 25 milestone is retired, never deleted.
        self.assertEqual(
            list(LoyaltyMilestone.objects.filter(is_active=False).values_list("threshold", flat=True)), [25],
        )
        previous = 0
        for count, level in [(0, "Rookie"), (4, "Rookie"), (5, "Regular"),
                             (9, "Regular"), (10, "Player"), (19, "Player"),
                             (20, "Court Regular"), (29, "Court Regular"),
                             (30, "Legend"), (100, "Legend")]:
            self.add_history(count - previous)
            with self.subTest(count=count):
                self.assertEqual(get_openplays_attended(self.player), count)
                self.assertEqual(get_loyalty_level(self.player).name, level)
            previous = count

    def test_seeded_milestones_have_all_agreed_benefits(self):
        terms = {
            milestone.threshold: milestone.benefits
            for milestone in LoyaltyMilestone.objects.filter(is_active=True)
        }
        self.assertEqual(
            {threshold: [benefit["kind"] for benefit in benefits] for threshold, benefits in terms.items()},
            {
                10: ["DISCOUNT"],
                20: ["DISCOUNT"],
                30: ["FREE_OPENPLAY"],
                50: ["FREE_OPENPLAY", "RECOGNITION"],
                75: ["FREE_OPENPLAY", "PERK"],
                100: ["RECOGNITION", "PREMIUM_REWARD"],
            },
        )
        self.assertEqual(terms[10][0]["amount_php"], 50)
        self.assertEqual(terms[20][0]["amount_php"], 100)
        self.assertEqual(terms[10][0]["currency"], "PHP")
        validity = {
            (threshold, benefit["key"]): benefit["valid_days"]
            for threshold, benefits in terms.items() for benefit in benefits if "valid_days" in benefit
        }
        self.assertEqual(validity, {
            (10, "discount-50"): 30, (20, "discount-100"): 30, (30, "free-openplay"): 30,
            (50, "free-openplay"): 30, (75, "free-openplay"): 30, (75, "club-perk"): 60,
            (100, "premium-reward"): 60,
        })
        for benefits in terms.values():  # recognition is status and never expires or becomes a reward row
            for benefit in benefits:
                if benefit["kind"] == "RECOGNITION":
                    self.assertNotIn("valid_days", benefit)
                    self.assertNotIn("key", benefit)

    def test_all_milestone_crossings_issue_once_and_reconciliation_is_idempotent(self):
        previous = 0
        # One reward row per redeemable benefit; recognition is status and has no row.
        rows = {10: 1, 20: 1, 30: 1, 50: 1, 75: 2, 100: 1}
        for before, threshold in [(9, 10), (19, 20), (29, 30), (49, 50), (74, 75), (99, 100)]:
            self.add_history(before - previous)
            reconcile_player_loyalty(self.player, actor=self.admin)
            self.assertFalse(RewardIssuance.objects.filter(player=self.player, milestone__threshold=threshold).exists())
            self.add_history(1)
            reconcile_player_loyalty(self.player, actor=self.admin)
            reconcile_player_loyalty(self.player, actor=self.admin)
            with self.subTest(threshold=threshold):
                self.assertEqual(
                    RewardIssuance.objects.filter(player=self.player, milestone__threshold=threshold).count(),
                    rows[threshold],
                )
            previous = threshold
        self.assertEqual(RewardIssuance.objects.filter(player=self.player).count(), 7)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_ISSUED").count(), 7)

    def test_multiple_milestones_crossed_and_terms_snapshot(self):
        self.add_history(8)
        reconcile_player_loyalty(self.player)
        self.add_history(18)
        self.assertEqual(list(get_eligible_milestones(self.player).values_list("threshold", flat=True)), [10, 20])
        reconcile_player_loyalty(self.player, actor=self.admin)
        self.assertEqual(list(RewardIssuance.objects.filter(player=self.player).order_by("milestone__threshold")
                              .values_list("milestone__threshold", flat=True)), [10, 20])
        issuance = RewardIssuance.objects.get(player=self.player, milestone__threshold=10)
        self.assertEqual(issuance.terms_snapshot["benefits"][0]["amount_php"], 50)
        milestone = issuance.milestone
        milestone.benefits = [{"kind": "DISCOUNT", "description": "₱75 OFF one OpenPlay", "amount_php": 75}]
        milestone.save()
        issuance.refresh_from_db()
        self.assertEqual(issuance.terms_snapshot["benefits"][0]["amount_php"], 50)

    def test_summary_distinguishes_check_ins_from_completed_eligibility(self):
        self.add_history(9)
        pending = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now - timedelta(minutes=15),
            ends_at=self.now + timedelta(hours=1),
        )
        check_in(openplay_id=pending.pk, player_id=self.player.pk, actor=self.organizer)
        summary = get_loyalty_summary(self.player)
        self.assertEqual(summary.openplays_attended, 10)
        self.assertEqual(summary.completed_attendances, 9)
        self.assertEqual(summary.current_level.name, "Player")
        self.assertEqual(summary.next_milestone.threshold, 10)
        self.assertEqual(summary.remaining_openplays, 1)
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())
        complete_openplay(openplay_id=pending.pk, actor=self.organizer, now=pending.ends_at + timedelta(minutes=1))
        self.assertEqual(RewardIssuance.objects.filter(player=self.player).count(), 1)
        self.assertEqual(get_loyalty_summary(self.player).remaining_openplays, 10)

    def test_cancelled_pending_session_never_issues_reward(self):
        self.add_history(9)
        pending = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now - timedelta(minutes=15),
            ends_at=self.now + timedelta(hours=1),
        )
        check_in(openplay_id=pending.pk, player_id=self.player.pk, actor=self.organizer)
        cancel_openplay(openplay_id=pending.pk, actor=self.organizer, reason="Rain")
        self.assertEqual(get_openplays_attended(self.player), 9)
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())

    def test_completion_failure_rolls_back_status_and_issuance(self):
        self.add_history(9)
        pending = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now - timedelta(hours=3),
            ends_at=self.now - timedelta(hours=1),
        )
        Attendance.objects.create(
            player=self.player, openplay=pending, source=Attendance.Source.HISTORICAL,
            recorded_by=self.admin,
        )
        with patch("club.loyalty._audit_reward", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                complete_openplay(openplay_id=pending.pk, actor=self.organizer)
        pending.refresh_from_db()
        self.assertEqual(pending.status, OpenPlay.Status.SCHEDULED)
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())

    def test_correction_revoke_and_restore_original_issuance(self):
        self.add_history(50)
        reconcile_player_loyalty(self.player, actor=self.admin)
        issuance = RewardIssuance.objects.get(player=self.player, milestone__threshold=50)
        attendance = Attendance.objects.filter(player=self.player).first()
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Wrong player")
        issuance.refresh_from_db()
        self.assertEqual(get_openplays_attended(self.player), 49)
        self.assertEqual(issuance.status, RewardIssuance.Status.REVOKED)
        restore_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Verified")
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.AVAILABLE)
        self.assertEqual(RewardIssuance.objects.filter(player=self.player, milestone__threshold=50).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_REVOKED", target_id=issuance.pk).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_REINSTATED", target_id=issuance.pk).count(), 1)

    def test_historical_addition_reconciles(self):
        self.add_history(9)
        reconcile_player_loyalty(self.player)
        add_historical_attendance(
            openplay_id=self.sessions[9].pk, player_id=self.player.pk,
            actor=self.admin, reason="Paper roster",
        )
        self.assertTrue(RewardIssuance.objects.filter(player=self.player, milestone__threshold=10).exists())

    def test_cancelled_openplay_reconciles_without_deleting_history(self):
        self.add_history(10)
        reconcile_player_loyalty(self.player, actor=self.admin)
        session = self.sessions[0]
        cancel_openplay(openplay_id=session.pk, actor=self.admin, reason="Session did not happen")
        issuance = RewardIssuance.objects.get(player=self.player, milestone__threshold=10)
        self.assertEqual(issuance.status, RewardIssuance.Status.REVOKED)
        self.assertEqual(get_openplays_attended(self.player), 9)
        self.assertTrue(Attendance.objects.filter(player=self.player, openplay=session).exists())

    def test_redeemed_history_survives_correction_and_discrepancy_is_audited_once(self):
        self.add_history(50)
        reconcile_player_loyalty(self.player, actor=self.admin)
        issuance = RewardIssuance.objects.get(player=self.player, milestone__threshold=50)
        redemption = redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        attendance = Attendance.objects.filter(player=self.player).first()
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Mistake")
        reconcile_player_loyalty(self.player, actor=self.admin)
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.REDEEMED)
        self.assertIsNotNone(issuance.eligibility_discrepancy_at)
        self.assertTrue(RewardRedemption.objects.filter(pk=redemption.pk).exists())
        self.assertEqual(AuditEvent.objects.filter(action="REDEEMED_REWARD_DISCREPANCY", target_id=issuance.pk).count(), 1)
        restore_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Verified")
        issuance.refresh_from_db()
        self.assertIsNone(issuance.eligibility_discrepancy_at)
        self.assertEqual(issuance.status, RewardIssuance.Status.REDEEMED)

    def test_redemption_ownership_state_and_audit(self):
        self.add_history(10)
        reconcile_player_loyalty(self.player, actor=self.admin)
        issuance = RewardIssuance.objects.get(player=self.player)
        with self.assertRaises(PermissionDenied):
            redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.member)
        with self.assertRaises(ValidationError):
            redeem_reward(issuance_id=issuance.pk, player_id=self.other.pk, actor=self.organizer)
        redemption = redeem_reward(
            issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer,
            openplay_id=self.sessions[0].pk,
        )
        self.assertEqual(redemption.player_id, self.player.pk)
        self.assertEqual(redemption.openplay_id, self.sessions[0].pk)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_REDEEMED", target_id=issuance.pk).count(), 1)
        with self.assertRaises(ValidationError):
            redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        self.assertEqual(RewardRedemption.objects.filter(issuance=issuance).count(), 1)

    def test_redemption_audit_failure_rolls_back_state_and_record(self):
        self.add_history(10)
        reconcile_player_loyalty(self.player)
        issuance = RewardIssuance.objects.get(player=self.player)
        with patch("club.loyalty._audit_reward", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.AVAILABLE)
        self.assertFalse(RewardRedemption.objects.filter(issuance=issuance).exists())

    def test_reward_cannot_be_applied_to_cancelled_openplay(self):
        self.add_history(10)
        reconcile_player_loyalty(self.player)
        issuance = RewardIssuance.objects.get(player=self.player)
        target = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now + timedelta(days=1),
            ends_at=self.now + timedelta(days=1, hours=2),
        )
        cancel_openplay(openplay_id=target.pk, actor=self.organizer, reason="Rain")
        with self.assertRaises(ValidationError):
            redeem_reward(
                issuance_id=issuance.pk, player_id=self.player.pk,
                actor=self.organizer, openplay_id=target.pk,
            )
        self.assertFalse(RewardRedemption.objects.filter(issuance=issuance).exists())

    def test_revoked_reward_cannot_redeem_and_constraints_protect_history(self):
        self.add_history(10)
        reconcile_player_loyalty(self.player)
        issuance = RewardIssuance.objects.get(player=self.player)
        void_attendance(attendance_id=Attendance.objects.filter(player=self.player).first().pk,
                        actor=self.admin, reason="Mistake")
        with self.assertRaises(ValidationError):
            redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RewardIssuance.objects.create(
                    player=self.player, milestone=issuance.milestone, terms_snapshot=issuance.terms_snapshot,
                    benefit_key=issuance.benefit_key,
                )
        with self.assertRaises(ValidationError):
            issuance.delete()
        with self.assertRaises(ValidationError):
            issuance.milestone.delete()

    def test_invalid_milestone_terms_and_threshold_are_rejected(self):
        with self.assertRaises(ValidationError):
            LoyaltyMilestone.objects.create(threshold=101, name="Invalid", benefits=[])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LoyaltyMilestone.objects.bulk_create([
                    LoyaltyMilestone(threshold=0, name="Invalid", benefits=[{"kind": "PERK", "description": "Item"}])
                ])
