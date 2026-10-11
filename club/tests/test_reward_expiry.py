from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import extend_reward_expiry, get_loyalty_summary, reconcile_player_loyalty, redeem_reward
from club.models import Attendance, AuditEvent, LoyaltyMilestone, OpenPlay, Player, RewardIssuance, RewardRedemption, Venue
from club.services import restore_attendance, void_attendance


class RewardExpiryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin@example.com")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.organizer = User.objects.create_user("organizer@example.com")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.player = Player.objects.create(user=User.objects.create_user("member@example.com"), display_name="Member")
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        self.sessions = OpenPlay.objects.bulk_create([
            OpenPlay(
                venue=self.venue, starts_at=self.now - timedelta(days=i + 2),
                ends_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
                status=OpenPlay.Status.COMPLETED, completed_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
            )
            for i in range(100)
        ])
        self.used = 0

    def add_history(self, count):
        chosen = self.sessions[self.used:self.used + count]
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.admin, checked_in_at=session.starts_at)
            for session in chosen
        ])
        self.used += count
        reconcile_player_loyalty(self.player, actor=self.admin)

    def discount(self):
        self.add_history(10)
        return RewardIssuance.objects.get(player=self.player, milestone__threshold=10)

    def make_expired(self, issuance):
        RewardIssuance.objects.filter(pk=issuance.pk).update(
            earned_at=self.now - timedelta(days=40), expires_at=self.now - timedelta(days=10),
        )
        issuance.refresh_from_db()
        self.assertTrue(issuance.is_expired())
        return issuance

    def test_expiry_counts_from_the_earned_date_per_benefit(self):
        self.add_history(75)
        windows = {
            (reward.milestone.threshold, reward.benefit_key): reward.expires_at - reward.earned_at
            for reward in RewardIssuance.objects.filter(player=self.player).select_related("milestone")
        }
        self.assertEqual(windows, {
            (10, "discount-50"): timedelta(days=30),
            (20, "discount-100"): timedelta(days=30),
            (30, "free-openplay"): timedelta(days=30),
            (50, "free-openplay"): timedelta(days=30),
            (75, "free-openplay"): timedelta(days=30),
            (75, "club-perk"): timedelta(days=60),
        })

    def test_recognition_is_status_without_a_reward_row(self):
        # The 100 milestone is "special recognition" (status) plus the premium reward: only the reward is a row.
        self.add_history(100)
        at_100 = RewardIssuance.objects.filter(player=self.player, milestone__threshold=100)
        self.assertEqual(list(at_100.values_list("benefit_key", flat=True)), ["premium-reward"])
        earned = [milestone.threshold for milestone in get_loyalty_summary(self.player).earned_milestones]
        self.assertEqual(earned, [10, 20, 30, 50, 75, 100])

    def test_expired_reward_needs_an_admin_override_with_a_reason(self):
        issuance = self.make_expired(self.discount())
        with self.assertRaisesMessage(ValidationError, "expired"):
            redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        with self.assertRaises(PermissionDenied):  # an organizer cannot override
            redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer,
                          expired_override_reason="Goodwill")
        with self.assertRaises(ValidationError):  # an admin still needs a reason
            redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.admin,
                          expired_override_reason="   ")
        self.assertFalse(RewardRedemption.objects.filter(issuance=issuance).exists())
        original = issuance.expires_at.isoformat()
        redemption = redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.admin,
                                   expired_override_reason="Organizer-approved goodwill exception")
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.REDEEMED)
        self.assertEqual(issuance.expires_at.isoformat(), original)  # the record still shows it had expired
        for details in (redemption.details,
                        AuditEvent.objects.get(action="REWARD_REDEEMED", target_id=issuance.pk).details):
            self.assertTrue(details["expired_override"])
            self.assertEqual(details["override_reason"], "Organizer-approved goodwill exception")
            self.assertEqual(details["original_expires_at"], original)

    def test_reason_is_ignored_when_the_reward_has_not_expired(self):
        issuance = self.discount()
        redemption = redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer,
                                   expired_override_reason="Not needed")
        self.assertNotIn("expired_override", redemption.details)
        self.assertNotIn("expired_override", AuditEvent.objects.get(action="REWARD_REDEEMED").details)

    def test_expired_is_derived_only_for_available_rewards_and_only_after_the_moment(self):
        issuance = self.discount()
        self.assertFalse(issuance.is_expired(now=issuance.expires_at))
        self.assertTrue(issuance.is_expired(now=issuance.expires_at + timedelta(seconds=1)))
        issuance.status = RewardIssuance.Status.REDEEMED
        self.assertFalse(issuance.is_expired(now=issuance.expires_at + timedelta(days=9)))

    def test_admin_can_extend_with_a_reason_and_it_is_audited(self):
        issuance = self.make_expired(self.discount())
        previous = issuance.expires_at
        later = self.now + timedelta(days=5)
        with self.assertRaises(PermissionDenied):
            extend_reward_expiry(issuance_id=issuance.pk, new_expires_at=later, reason="Outage", actor=self.organizer)
        with self.assertRaises(ValidationError):
            extend_reward_expiry(issuance_id=issuance.pk, new_expires_at=later, reason=" ", actor=self.admin)
        with self.assertRaises(ValidationError):
            extend_reward_expiry(issuance_id=issuance.pk, new_expires_at=previous - timedelta(days=1),
                                 reason="Earlier", actor=self.admin)
        with self.assertRaises(ValidationError):
            extend_reward_expiry(issuance_id=issuance.pk, new_expires_at=later.replace(tzinfo=None),
                                 reason="Naive", actor=self.admin)
        extend_reward_expiry(issuance_id=issuance.pk, new_expires_at=later, reason="App outage", actor=self.admin)
        issuance.refresh_from_db()
        self.assertEqual(issuance.expires_at, later)
        self.assertFalse(issuance.is_expired())
        details = AuditEvent.objects.get(action="REWARD_EXPIRY_EXTENDED", target_id=issuance.pk).details
        self.assertEqual(details["reason"], "App outage")
        self.assertEqual(details["previous_expires_at"], previous.isoformat())
        self.assertEqual(details["new_expires_at"], later.isoformat())
        # An extended reward is now redeemable without any override.
        redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)

    def test_redeemed_and_non_expiring_rewards_cannot_be_extended(self):
        issuance = self.discount()
        redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        with self.assertRaisesMessage(ValidationError, "redeemed"):
            extend_reward_expiry(issuance_id=issuance.pk, new_expires_at=self.now + timedelta(days=90),
                                 reason="Late", actor=self.admin)
        self.add_history(40)  # reaches 50 as well
        free = RewardIssuance.objects.get(player=self.player, milestone__threshold=50)
        RewardIssuance.objects.filter(pk=free.pk).update(expires_at=None)
        with self.assertRaisesMessage(ValidationError, "no expiry"):
            extend_reward_expiry(issuance_id=free.pk, new_expires_at=self.now + timedelta(days=90),
                                 reason="None set", actor=self.admin)

    def test_restoring_a_revoked_reward_keeps_its_original_expiry(self):
        issuance = self.discount()
        original = issuance.expires_at
        attendance = Attendance.objects.filter(player=self.player).first()
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Wrong player")
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.REVOKED)
        restore_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Verified")
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.AVAILABLE)
        self.assertEqual(issuance.expires_at, original)

    def test_a_legacy_whole_milestone_row_is_never_reissued_per_benefit(self):
        milestone = LoyaltyMilestone.objects.get(threshold=10)
        RewardIssuance.objects.create(
            player=self.player, milestone=milestone, benefit_key="",
            terms_snapshot={"threshold": 10, "name": milestone.name, "benefits": milestone.benefits},
        )
        self.add_history(10)
        self.assertEqual(RewardIssuance.objects.filter(player=self.player).count(), 1)

    def test_database_rejects_an_expiry_that_is_not_after_the_earned_date(self):
        milestone = LoyaltyMilestone.objects.get(threshold=10)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RewardIssuance.objects.create(
                    player=self.player, milestone=milestone, benefit_key="x", earned_at=self.now,
                    expires_at=self.now, terms_snapshot={"threshold": 10, "name": "x", "benefits": []},
                )

    def test_milestone_configuration_validates_keys_and_validity(self):
        def clean(benefits):
            LoyaltyMilestone(threshold=999, name="Test", benefits=benefits).clean()

        perk = {"kind": "PERK", "key": "gift", "description": "Gift", "valid_days": 30}
        clean([perk, {"kind": "RECOGNITION", "description": "Status"}])  # valid
        bad = [
            [{**perk, "valid_days": 0}],
            [{**perk, "valid_days": "30"}],
            [{"kind": "RECOGNITION", "description": "Status", "valid_days": 30}],  # status never expires
            [perk, {**perk, "description": "Other"}],  # duplicate key
            [{**perk, "key": "Bad Key"}],
        ]
        for benefits in bad:
            with self.subTest(benefits=benefits), self.assertRaises(ValidationError):
                clean(benefits)
