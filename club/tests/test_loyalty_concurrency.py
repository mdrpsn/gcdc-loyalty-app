from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import reconcile_player_loyalty, redeem_reward
from club.models import Attendance, AuditEvent, LoyaltyMilestone, OpenPlay, Player, RewardIssuance, RewardRedemption, Venue
from club.services import complete_openplay, void_attendance


class ConcurrentLoyaltyTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase flushes rows between tests, including data-migration seeds.
        LoyaltyMilestone.objects.get_or_create(
            threshold=10,
            defaults={"name": "Dinker", "benefits": [
                {"kind": "DISCOUNT", "description": "₱50 OFF one OpenPlay", "amount_php": 50}
            ]},
        )
        self.organizer = get_user_model().objects.create_user("organizer@example.com")
        self.organizer.groups.add(Group.objects.get_or_create(name=ORGANIZER)[0])
        self.admin = get_user_model().objects.create_user("admin@example.com")
        self.admin.groups.add(Group.objects.get_or_create(name=ADMIN)[0])
        self.player = Player.objects.create(display_name="Walk In", phone="123")
        self.venue = Venue.objects.create(name="Main Court")
        now = timezone.now()
        self.sessions = OpenPlay.objects.bulk_create([
            OpenPlay(
                venue=self.venue,
                starts_at=now - timedelta(days=i + 1),
                ends_at=now - timedelta(days=i + 1) + timedelta(hours=2),
                status=OpenPlay.Status.COMPLETED,
                completed_at=now - timedelta(days=i + 1) + timedelta(hours=2),
            )
            for i in range(10)
        ])
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.organizer)
            for session in self.sessions
        ])

    def _parallel(self, operation):
        barrier = Barrier(2)

        def attempt():
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(lambda _: attempt(), range(2)))

    def test_simultaneous_reconciliation_issues_once(self):
        self._parallel(lambda: reconcile_player_loyalty(self.player, actor=self.organizer))
        self.assertEqual(RewardIssuance.objects.filter(player=self.player).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_ISSUED").count(), 1)

    def test_simultaneous_redemption_allows_one(self):
        reconcile_player_loyalty(self.player, actor=self.organizer)
        issuance = RewardIssuance.objects.get(player=self.player)

        def redeem():
            try:
                redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
                return "redeemed"
            except ValidationError:
                return "rejected"

        self.assertCountEqual(self._parallel(redeem), ["redeemed", "rejected"])
        self.assertEqual(RewardRedemption.objects.filter(issuance=issuance).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_REDEEMED").count(), 1)

    def test_two_sessions_completed_concurrently_cross_one_threshold(self):
        attendance = Attendance.objects.filter(player=self.player).order_by("pk").first()
        # Existing ledger rows remain; use void state for the starting count.
        attendance.status = Attendance.Status.VOID
        attendance.voided_at = timezone.now()
        attendance.voided_by = self.organizer
        attendance.void_reason = "Test setup"
        attendance.save()
        now = timezone.now()
        pending = OpenPlay.objects.bulk_create([
            OpenPlay(venue=self.venue, starts_at=now - timedelta(hours=3), ends_at=now - timedelta(hours=1)),
            OpenPlay(venue=self.venue, starts_at=now - timedelta(hours=4), ends_at=now - timedelta(hours=2)),
        ])
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.organizer)
            for session in pending
        ])
        barrier = Barrier(2)

        def complete(session_id):
            try:
                barrier.wait(timeout=10)
                complete_openplay(openplay_id=session_id, actor=self.organizer)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(complete, [session.pk for session in pending]))
        self.assertEqual(RewardIssuance.objects.filter(player=self.player).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_ISSUED").count(), 1)

    def test_correction_racing_reconciliation_finishes_revoked(self):
        reconcile_player_loyalty(self.player, actor=self.organizer)
        attendance = Attendance.objects.filter(player=self.player).first()
        barrier = Barrier(2)

        def attempt(kind):
            try:
                barrier.wait(timeout=10)
                if kind == "correct":
                    void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Wrong check-in")
                else:
                    reconcile_player_loyalty(self.player, actor=self.organizer)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(attempt, ["correct", "reconcile"]))
        issuance = RewardIssuance.objects.get(player=self.player)
        self.assertEqual(issuance.status, RewardIssuance.Status.REVOKED)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_REVOKED").count(), 1)
