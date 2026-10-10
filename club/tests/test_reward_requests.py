from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import (
    cancel_redemption_request,
    reconcile_player_loyalty,
    redeem_reward,
    request_reward_redemption,
    review_redemption_request,
)
from club.models import (
    Attendance,
    AuditEvent,
    OpenPlay,
    Player,
    RewardIssuance,
    RewardRedemption,
    RewardRedemptionRequest,
    Venue,
)
from club.services import cancel_openplay

PENDING = RewardRedemptionRequest.Status.PENDING


class RewardRequestTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin@example.com")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.organizer = User.objects.create_user("organizer@example.com")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.member = User.objects.create_user("member@example.com")
        self.player = Player.objects.create(user=self.member, display_name="Member")
        self.other_user = User.objects.create_user("other@example.com")
        self.other = Player.objects.create(user=self.other_user, display_name="Other")
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        past = OpenPlay.objects.bulk_create([
            OpenPlay(venue=self.venue, starts_at=self.now - timedelta(days=i + 2),
                     ends_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
                     status=OpenPlay.Status.COMPLETED,
                     completed_at=self.now - timedelta(days=i + 2) + timedelta(hours=2))
            for i in range(75)
        ])
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.admin, checked_in_at=session.starts_at)
            for session in past
        ])
        reconcile_player_loyalty(self.player, actor=self.admin)
        self.discount = RewardIssuance.objects.get(player=self.player, benefit_key="discount-50")
        self.free = RewardIssuance.objects.get(player=self.player, milestone__threshold=50, benefit_key="free-openplay")
        self.gear = RewardIssuance.objects.get(player=self.player, benefit_key="overgrip")
        self.soon = self.upcoming(1)
        self.later = self.upcoming(3)

    def upcoming(self, days):
        return OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now + timedelta(days=days),
            ends_at=self.now + timedelta(days=days, hours=2),
        )

    def request(self, issuance=None, session=None, actor=None):
        return request_reward_redemption(
            issuance_id=(issuance or self.discount).pk, openplay_id=(session or self.soon).pk,
            actor=actor or self.member,
        )

    def open_requests(self):
        return RewardRedemptionRequest.objects.filter(status=PENDING)

    def audit(self, action):
        return AuditEvent.objects.filter(action=action).count()

    def test_request_is_recorded_and_leaves_the_reward_available(self):
        request = self.request()
        self.assertEqual((request.status, request.player, request.openplay), (PENDING, self.player, self.soon))
        self.discount.refresh_from_db()
        self.assertEqual(self.discount.status, RewardIssuance.Status.AVAILABLE)
        self.assertEqual(self.audit("REWARD_REDEMPTION_REQUESTED"), 1)
        self.assertFalse(RewardRedemption.objects.filter(issuance=self.discount).exists())

    def test_only_valid_requests_are_accepted(self):
        with self.assertRaises(PermissionDenied):  # an organizer is not the owning player
            self.request(actor=self.organizer)
        with self.assertRaisesMessage(ValidationError, "another player"):
            self.request(actor=self.other_user)
        with self.assertRaisesMessage(ValidationError, "handed over by the club"):
            self.request(issuance=self.gear)
        started = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now - timedelta(minutes=5), ends_at=self.now + timedelta(hours=1),
        )
        with self.assertRaisesMessage(ValidationError, "upcoming"):
            self.request(session=started)
        cancelled = self.upcoming(5)
        cancel_openplay(openplay_id=cancelled.pk, actor=self.organizer, reason="Rain")
        with self.assertRaisesMessage(ValidationError, "upcoming"):
            self.request(session=OpenPlay.objects.get(pk=cancelled.pk))
        RewardIssuance.objects.filter(pk=self.discount.pk).update(
            earned_at=self.now - timedelta(days=1), expires_at=self.now + timedelta(days=2),
        )
        with self.assertRaisesMessage(ValidationError, "expires before"):
            self.request(session=self.later)  # starts after the reward expires
        RewardIssuance.objects.filter(pk=self.discount.pk).update(
            earned_at=self.now - timedelta(days=40), expires_at=self.now - timedelta(days=10),
        )
        with self.assertRaisesMessage(ValidationError, "expired"):
            self.request()
        self.assertEqual(RewardRedemptionRequest.objects.count(), 0)

    def test_one_open_request_per_reward_and_one_reward_per_session(self):
        self.request()
        with self.assertRaisesMessage(ValidationError, "already has an open request"):
            self.request(session=self.later)
        with self.assertRaisesMessage(ValidationError, "already have a reward requested"):
            self.request(issuance=self.free)  # a second reward for the same OpenPlay
        self.request(issuance=self.free, session=self.later)  # a different session is fine
        self.assertEqual(self.open_requests().count(), 2)

    def test_database_blocks_duplicate_open_requests(self):
        self.request()
        with self.assertRaises(IntegrityError), transaction.atomic():
            RewardRedemptionRequest.objects.create(
                issuance=self.discount, player=self.player, openplay=self.later, requested_by=self.member,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():  # a declined row needs a reason
            RewardRedemptionRequest.objects.create(
                issuance=self.free, player=self.player, openplay=self.later, requested_by=self.member,
                status="DECLINED", reviewed_by=self.admin, reviewed_at=self.now,
            )

    def test_player_cancels_and_may_request_again(self):
        request = self.request()
        with self.assertRaises(PermissionDenied):  # another player cannot cancel it
            cancel_redemption_request(request_id=request.pk, actor=self.other_user)
        cancel_redemption_request(request_id=request.pk, actor=self.member)
        request.refresh_from_db()
        self.assertEqual(request.status, RewardRedemptionRequest.Status.CANCELLED)
        with self.assertRaisesMessage(ValidationError, "open request"):
            cancel_redemption_request(request_id=request.pk, actor=self.member)
        self.assertEqual(self.audit("REWARD_REDEMPTION_REQUEST_CANCELLED"), 1)
        self.request(session=self.later)
        self.assertEqual(self.open_requests().count(), 1)

    def test_admin_can_release_an_open_request(self):
        request = self.request()
        cancel_redemption_request(request_id=request.pk, actor=self.admin)
        request.refresh_from_db()
        self.assertEqual(request.status, RewardRedemptionRequest.Status.RELEASED)
        self.assertEqual(self.audit("REWARD_REDEMPTION_REQUEST_RELEASED"), 1)

    def test_admin_approval_redeems_for_the_requested_session(self):
        request = self.request()
        with self.assertRaises(PermissionDenied):
            review_redemption_request(request_id=request.pk, actor=self.organizer, approve=True)
        redemption = review_redemption_request(request_id=request.pk, actor=self.admin, approve=True)
        request.refresh_from_db()
        self.discount.refresh_from_db()
        self.assertEqual(self.discount.status, RewardIssuance.Status.REDEEMED)
        self.assertEqual(redemption.openplay, self.soon)
        self.assertEqual((request.status, request.redemption, request.reviewed_by),
                         (RewardRedemptionRequest.Status.APPROVED, redemption, self.admin))
        self.assertEqual(self.audit("REWARD_REDEEMED"), 1)
        self.assertEqual(self.audit("REWARD_REDEMPTION_REQUEST_APPROVED"), 1)
        with self.assertRaisesMessage(ValidationError, "open request"):
            review_redemption_request(request_id=request.pk, actor=self.admin, approve=True)

    def test_decline_needs_a_reason_and_frees_the_reward(self):
        request = self.request()
        with self.assertRaisesMessage(ValidationError, "reason"):
            review_redemption_request(request_id=request.pk, actor=self.admin, approve=False, reason=" ")
        review_redemption_request(request_id=request.pk, actor=self.admin, approve=False, reason="No payment seen")
        request.refresh_from_db()
        self.discount.refresh_from_db()
        self.assertEqual((request.status, request.decline_reason),
                         (RewardRedemptionRequest.Status.DECLINED, "No payment seen"))
        self.assertEqual(self.discount.status, RewardIssuance.Status.AVAILABLE)
        self.request()  # the player can try again
        self.assertEqual(self.audit("REWARD_REDEMPTION_REQUEST_DECLINED"), 1)

    def test_direct_redemption_closes_the_matching_request_and_releases_others(self):
        same = self.request()
        redeem_reward(issuance_id=self.discount.pk, player_id=self.player.pk, actor=self.organizer,
                      openplay_id=self.soon.pk)
        same.refresh_from_db()
        self.assertEqual(same.status, RewardRedemptionRequest.Status.APPROVED)
        self.assertEqual(same.reviewed_by, self.organizer)
        elsewhere = self.request(issuance=self.free, session=self.later)
        redeem_reward(issuance_id=self.free.pk, player_id=self.player.pk, actor=self.organizer,
                      openplay_id=self.soon.pk)
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.status, RewardRedemptionRequest.Status.RELEASED)
        self.assertEqual(self.open_requests().count(), 0)

    def test_expired_reward_cannot_be_approved_and_request_stays_open(self):
        request = self.request()
        RewardIssuance.objects.filter(pk=self.discount.pk).update(
            earned_at=self.now - timedelta(days=40), expires_at=self.now - timedelta(days=10),
        )
        with self.assertRaisesMessage(ValidationError, "expired"):
            review_redemption_request(request_id=request.pk, actor=self.admin, approve=True)
        request.refresh_from_db()
        self.assertEqual(request.status, PENDING)
        self.assertFalse(RewardRedemption.objects.filter(issuance=self.discount).exists())

    def test_a_request_for_an_ended_session_is_released_when_the_player_asks_again(self):
        stale = self.request()
        OpenPlay.objects.filter(pk=self.soon.pk).update(
            starts_at=self.now - timedelta(hours=5), ends_at=self.now - timedelta(hours=3),
        )
        fresh = self.request(session=self.later)
        stale.refresh_from_db()
        self.assertEqual(stale.status, RewardRedemptionRequest.Status.RELEASED)
        self.assertEqual(fresh.status, PENDING)
        self.assertEqual(self.open_requests().count(), 1)
