"""Production-risk tests across timezones, service boundaries, and PostgreSQL locks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connections
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import get_loyalty_summary, redeem_reward
from club.models import Attendance, AuditEvent, OpenPlay, Player, Registration, RewardIssuance, Venue
from club.progression import get_badges, get_streaks
from club.services import (
    check_in, check_in_walk_in, complete_openplay,
    create_openplay, create_venue, link_player_account, openplays_attended,
    register_player, update_own_profile, void_attendance,
)


@override_settings(TIME_ZONE="America/New_York")
class TimezoneHardeningTests(TestCase):
    def setUp(self):
        self.organizer = get_user_model().objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.user = get_user_model().objects.create_user("player@example.com", "Securepass123")
        self.player = Player.objects.create(user=self.user, display_name="DST Player")
        self.venue = Venue.objects.create(name="DST Court")

    def test_dst_fall_back_distinguishes_two_same_clock_sessions(self):
        # 01:30 happens twice locally: once in EDT, then in EST.
        starts = [datetime(2026, 11, 1, hour, 30, tzinfo=dt_timezone.utc) for hour in (5, 6)]
        sessions = [create_openplay(
            venue_id=self.venue.pk, starts_at=start, ends_at=start + timedelta(minutes=45), actor=self.organizer,
        ) for start in starts]
        with timezone.override("America/New_York"):
            self.client.force_login(self.user)
            page = self.client.get(reverse("player:openplays"))
            self.assertContains(page, "1:30 AM EDT")
            self.assertContains(page, "1:30 AM EST")
            self.assertContains(page, "Times shown in America/New_York")
            for session in sessions:
                Attendance.objects.create(player=self.player, openplay=session,
                                          source=Attendance.Source.WALK_IN, recorded_by=self.organizer)
                session.status = OpenPlay.Status.COMPLETED
                session.completed_at = session.ends_at
                session.save()
            self.assertEqual(openplays_attended(self.player), 2)
            self.assertEqual(get_streaks(self.player).current, 2)

    def test_schedule_and_time_boundaries_require_aware_instants(self):
        with self.assertRaises(ValidationError):
            create_openplay(venue_id=self.venue.pk, starts_at=datetime(2026, 11, 1, 1, 30),
                            ends_at=datetime(2026, 11, 1, 3, 0), actor=self.organizer)
        start = timezone.now() + timedelta(days=1)
        session = create_openplay(venue_id=self.venue.pk, starts_at=start,
                                  ends_at=start + timedelta(hours=2), actor=self.organizer)
        with self.assertRaises(ValidationError):
            check_in(openplay_id=session.pk, player_id=self.player.pk, actor=self.organizer,
                     now=start - timedelta(minutes=31))
        check_in(openplay_id=session.pk, player_id=self.player.pk, actor=self.organizer,
                 now=start - timedelta(minutes=30))
        with self.assertRaises(ValidationError):
            complete_openplay(openplay_id=session.pk, actor=self.organizer,
                              now=session.ends_at - timedelta(seconds=1))
        complete_openplay(openplay_id=session.pk, actor=self.organizer, now=session.ends_at)
        self.assertEqual(get_loyalty_summary(self.player).completed_attendances, 1)


class ClubLifecycleTests(TestCase):
    def test_registered_player_walk_in_reward_redemption_no_show_and_correction(self):
        User = get_user_model()
        user_a = User.objects.create_user("a@example.com", "Securepass123")
        organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        organizer.groups.add(Group.objects.get(name=ORGANIZER))
        admin = User.objects.create_user("admin@example.com", "Securepass123")
        admin.groups.add(Group.objects.get(name=ADMIN))
        player_a = Player.objects.create(user=user_a, display_name="Player A")
        venue = create_venue(name="Club Court", actor=organizer)
        start = timezone.now() + timedelta(days=1)
        sessions = []
        rows = []
        for number in range(10):
            begins = start + timedelta(days=number)
            session = create_openplay(venue_id=venue.pk, starts_at=begins,
                                      ends_at=begins + timedelta(hours=2), actor=organizer)
            sessions.append(session)
            register_player(openplay_id=session.pk, player_id=player_a.pk, actor=user_a)
            row, _ = check_in(openplay_id=session.pk, player_id=player_a.pk,
                              actor=organizer, now=begins + timedelta(minutes=10))
            rows.append(row)
            if number == 0:
                player_b, walk_in = check_in_walk_in(
                    openplay_id=session.pk, display_name="Player B", phone="555-0100",
                    actor=organizer, now=begins + timedelta(minutes=12),
                )
                self.assertFalse(Registration.objects.filter(player=player_b, openplay=session).exists())
                self.assertEqual(walk_in.source, Attendance.Source.WALK_IN)
                self.assertEqual(openplays_attended(player_a), 1)
                self.assertEqual(openplays_attended(player_b), 1)
            self.assertFalse(RewardIssuance.objects.filter(player=player_a).exists())
            complete_openplay(openplay_id=session.pk, actor=organizer,
                              now=session.ends_at + timedelta(minutes=1))
        self.assertEqual(openplays_attended(player_a), 10)
        self.assertEqual(get_streaks(player_a).best, 10)
        issuance = RewardIssuance.objects.get(player=player_a, milestone__threshold=10)
        self.assertEqual(issuance.status, RewardIssuance.Status.AVAILABLE)
        target = create_openplay(venue_id=venue.pk, starts_at=start + timedelta(days=11),
                                 ends_at=start + timedelta(days=11, hours=2), actor=organizer)
        redemption = redeem_reward(issuance_id=issuance.pk, player_id=player_a.pk,
                                   openplay_id=target.pk, actor=organizer)
        self.assertEqual(redemption.openplay_id, target.pk)
        register_player(openplay_id=target.pk, player_id=player_a.pk, actor=user_a)
        complete_openplay(openplay_id=target.pk, actor=organizer,
                          now=target.ends_at + timedelta(minutes=1))
        self.assertEqual(get_streaks(player_a).current, 0)
        for index in (2, 5, 8):
            void_attendance(attendance_id=rows[index].pk, actor=admin, reason="Verified error")
        issuance.refresh_from_db()
        self.assertEqual(issuance.status, RewardIssuance.Status.REDEEMED)
        self.assertIsNotNone(issuance.eligibility_discrepancy_at)
        self.assertEqual(openplays_attended(player_a), 7)
        self.assertEqual([badge.name for badge in get_badges(
            completed_attendances=get_loyalty_summary(player_a).completed_attendances,
            streaks=get_streaks(player_a),
        )], ["First Rally"])
        self.client.force_login(user_a)
        self.assertContains(self.client.get(reverse("player:rewards")), "Redeemed")
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_ISSUED", target_id=issuance.pk).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REWARD_REDEEMED", target_id=issuance.pk).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REDEEMED_REWARD_DISCREPANCY", target_id=issuance.pk).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="ATTENDANCE_VOIDED").count(), 3)

    def test_profile_and_identity_audit_failures_roll_back(self):
        User = get_user_model()
        member = User.objects.create_user("member@example.com")
        admin = User.objects.create_user("admin@example.com")
        admin.groups.add(Group.objects.get(name=ADMIN))
        linked = Player.objects.create(user=member, display_name="Before")
        walk_in = Player.objects.create(display_name="Walk In", contact_email="walk@example.com")
        second_account = User.objects.create_user("walk@example.com")
        with patch("club.services.AuditEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                update_own_profile(player_id=linked.pk, display_name="After", phone="123", actor=member)
            with self.assertRaises(RuntimeError):
                link_player_account(player_id=walk_in.pk, user_id=second_account.pk, actor=admin)
        linked.refresh_from_db()
        walk_in.refresh_from_db()
        self.assertEqual(linked.display_name, "Before")
        self.assertIsNone(walk_in.user_id)
        self.assertEqual(walk_in.contact_email, "walk@example.com")

    def test_database_rejects_invalid_states_and_second_redemption(self):
        from club.models import LoyaltyMilestone, RewardRedemption

        User = get_user_model()
        actor = User.objects.create_user("actor@example.com")
        player = Player.objects.create(display_name="Walk In", phone="123")
        venue = Venue.objects.create(name="Court")
        starts = timezone.now() + timedelta(days=1)
        session = OpenPlay.objects.create(venue=venue, starts_at=starts, ends_at=starts + timedelta(hours=2))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OpenPlay.objects.bulk_create([OpenPlay(venue=venue, starts_at=starts,
                    ends_at=starts + timedelta(hours=2), status="UNKNOWN")])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Registration.objects.bulk_create([Registration(player=player, openplay=session, status="UNKNOWN")])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Attendance.objects.bulk_create([Attendance(player=player, openplay=session,
                    recorded_by=actor, source=Attendance.Source.WALK_IN, status="BAD")])
        milestone = LoyaltyMilestone.objects.get(threshold=10)
        issuance = RewardIssuance.objects.create(
            player=player, milestone=milestone,
            terms_snapshot={"threshold": 10, "name": "Dinker", "benefits": milestone.benefits},
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RewardIssuance.objects.filter(pk=issuance.pk).update(status="UNKNOWN")
        RewardRedemption.objects.create(issuance=issuance, player=player, redeemed_by=actor)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RewardRedemption.objects.bulk_create([
                    RewardRedemption(issuance=issuance, player=player, redeemed_by=actor)
                ])


class ConcurrentIdentityAndRegistrationTests(TransactionTestCase):
    def setUp(self):
        self.organizer = get_user_model().objects.create_user("organizer@example.com")
        self.organizer.groups.add(Group.objects.get_or_create(name=ORGANIZER)[0])
        self.admin = get_user_model().objects.create_user("admin@example.com")
        self.admin.groups.add(Group.objects.get_or_create(name=ADMIN)[0])
        self.user = get_user_model().objects.create_user("member@example.com")
        self.venue = Venue.objects.create(name="Main Court")
        start = timezone.now() + timedelta(days=1)
        self.session = OpenPlay.objects.create(venue=self.venue, starts_at=start,
                                               ends_at=start + timedelta(hours=2))

    def parallel(self, operations):
        barrier = Barrier(2)

        def attempt(operation):
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(attempt, operations))

    def test_concurrent_registration_creates_one_row_and_audit(self):
        player = Player.objects.create(user=self.user, display_name="Member")
        results = self.parallel([
            lambda: register_player(openplay_id=self.session.pk, player_id=player.pk, actor=self.user).pk,
            lambda: register_player(openplay_id=self.session.pk, player_id=player.pk, actor=self.user).pk,
        ])
        self.assertEqual(results[0], results[1])
        self.assertEqual(Registration.objects.filter(player=player, openplay=self.session).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REGISTRATION_CREATED").count(), 1)

    def test_concurrent_account_linking_cannot_claim_two_players(self):
        players = [Player.objects.create(display_name=f"Walk-in {i}", phone=f"555-{i}") for i in range(2)]

        def link(player):
            try:
                return link_player_account(player_id=player.pk, user_id=self.user.pk, actor=self.admin).pk
            except ValidationError:
                return None

        results = self.parallel([lambda: link(players[0]), lambda: link(players[1])])
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(Player.objects.filter(user=self.user).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="PLAYER_LINKED").count(), 1)
