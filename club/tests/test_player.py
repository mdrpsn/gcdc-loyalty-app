from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.conf import settings
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ORGANIZER
from club.loyalty import reconcile_player_loyalty
from club.models import Attendance, AuditEvent, OpenPlay, Player, Registration, RewardIssuance, RewardRedemption, Venue
from club.progression import get_badges, get_streaks


class PlayerRequestTests(TestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        User = get_user_model()
        self.user = User.objects.create_user("member@example.com", "Securepass123")
        self.other_user = User.objects.create_user("other@example.com", "Securepass123")
        self.staff = User.objects.create_user("staff@example.com", "Securepass123")
        self.staff.groups.add(Group.objects.get(name=ORGANIZER))
        self.player = Player.objects.create(user=self.user, display_name="Member One", phone="111")
        self.other = Player.objects.create(user=self.other_user, display_name="Private Member", phone="222")
        self.venue = Venue.objects.create(name="Court Green")
        self.now = timezone.now()
        self.future = self.session(1, completed=False)
        self.client.force_login(self.user)

    def url(self, name, *args):
        return reverse(f"player:{name}", args=args)

    def session(self, days_ago, *, completed=True, cancelled=False):
        starts = self.now - timedelta(days=days_ago) if completed or cancelled else self.now + timedelta(days=days_ago)
        ends = starts + timedelta(hours=2)
        status = OpenPlay.Status.CANCELLED if cancelled else OpenPlay.Status.COMPLETED if completed else OpenPlay.Status.SCHEDULED
        return OpenPlay.objects.create(
            venue=self.venue, starts_at=starts, ends_at=ends, status=status,
            completed_at=ends if completed and not cancelled else None,
            cancelled_at=ends if cancelled else None,
            cancellation_reason="Rain" if cancelled else "",
        )

    def attend(self, session, *, player=None, valid=True):
        return Attendance.objects.create(
            player=player or self.player, openplay=session, source=Attendance.Source.WALK_IN,
            recorded_by=self.staff, checked_in_at=session.starts_at,
            status=Attendance.Status.VALID if valid else Attendance.Status.VOID,
            voided_at=self.now if not valid else None,
            voided_by=self.staff if not valid else None,
            void_reason="Mistake" if not valid else "",
        )

    def history(self, count):
        for i in range(count):
            self.attend(self.session(i + 2))

    def test_login_logout_and_member_redirect(self):
        self.client.logout()
        for name in ("home", "openplays", "attendance", "rewards", "profile"):
            self.assertIn("/login/", self.client.get(self.url(name))["Location"])
        response = self.client.post(reverse("login"), {"username": self.user.email, "password": "Securepass123"})
        self.assertRedirects(response, self.url("home"))
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertIn("/login/", self.client.get(self.url("home"))["Location"])

    def test_own_scope_and_direct_url_tampering(self):
        secret_venue = Venue.objects.create(name="Court Secret")
        secret_session = self.session(3)
        secret_session.venue = secret_venue
        secret_session.save()
        self.attend(secret_session, player=self.other)
        self.client.force_login(self.other_user)
        self.assertContains(self.client.get(self.url("home")), "Private Member")
        self.client.force_login(self.user)
        for name in ("home", "attendance", "rewards", "profile"):
            response = self.client.get(self.url(name), {"player_id": self.other.pk})
            self.assertNotContains(response, "Private Member")
            self.assertNotContains(response, "other@example.com")
        self.assertEqual(self.client.get(f"/player/{self.other.pk}/").status_code, 404)
        self.assertNotContains(self.client.get(self.url("attendance")), "Court Secret")
        from club.models import LoyaltyMilestone
        milestone = LoyaltyMilestone.objects.get(threshold=10)
        RewardIssuance.objects.create(
            player=self.other, milestone=milestone,
            terms_snapshot={"name": "Private Reward", "threshold": 10, "benefits": []},
        )
        self.assertNotContains(self.client.get(self.url("rewards")), "Private Reward")
        self.assertEqual(self.client.post(reverse("organizer:redeem", args=[self.other.pk, 1])).status_code, 403)

    def test_upcoming_list_uses_own_registration_and_pages_results(self):
        Registration.objects.create(player=self.other, openplay=self.future)
        response = self.client.get(self.url("openplays"))
        self.assertContains(response, "Not registered")
        self.assertContains(response, f"Times shown in {settings.TIME_ZONE}")
        self.assertNotContains(response, "Private Member")
        for day in range(2, 23):
            self.session(day, completed=False)
        response = self.client.get(self.url("openplays"))
        self.assertEqual(len(response.context["sessions"]), 20)
        self.assertContains(response, "Page 1 of 2")
        self.assertEqual(len(self.client.get(self.url("openplays"), {"page": 2}).context["sessions"]), 2)

    def test_registration_and_cancellation_use_own_identity(self):
        response = self.client.post(self.url("register", self.future.pk), {"player_id": self.other.pk}, follow=True)
        self.assertContains(response, "Registration confirmed")
        self.assertTrue(Registration.objects.filter(player=self.player, openplay=self.future, status="ACTIVE").exists())
        self.assertFalse(Registration.objects.filter(player=self.other, openplay=self.future).exists())
        self.assertContains(self.client.get(self.url("openplays")), "Registered")
        response = self.client.post(self.url("cancel", self.future.pk), follow=True)
        self.assertContains(response, "Registration cancelled")
        self.assertEqual(Registration.objects.get(player=self.player, openplay=self.future).status, "CANCELLED")
        self.assertEqual(AuditEvent.objects.filter(action="REGISTRATION_CANCELLED").count(), 1)

    def test_invalid_registration_and_csrf(self):
        past = self.session(3)
        response = self.client.post(self.url("register", past.pk), follow=True)
        self.assertContains(response, "Registration is closed")
        self.assertFalse(Registration.objects.filter(player=self.player, openplay=past).exists())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url("register", self.future.pk)).status_code, 403)
        self.assertEqual(self.client.get(self.url("register", self.future.pk)).status_code, 405)
        response = self.client.post(self.url("cancel", self.future.pk), follow=True)
        self.assertContains(response, "OpenPlay or registration not found")

    def test_dashboard_count_level_and_pending_then_available(self):
        self.history(9)
        pending = self.session(0, completed=False)
        pending.starts_at = self.now - timedelta(minutes=10)
        pending.ends_at = self.now + timedelta(hours=1)
        pending.save()
        self.attend(pending)
        response = self.client.get(self.url("home"))
        self.assertContains(response, "OpenPlays Attended")
        self.assertEqual(response.context["summary"].openplays_attended, 10)
        self.assertEqual(response.context["summary"].current_level.name, "Player")
        self.assertEqual(response.context["summary"].next_milestone.threshold, 10)
        self.assertContains(response, "Reward pending session completion")
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())
        pending.status = OpenPlay.Status.COMPLETED
        pending.completed_at = self.now
        pending.save()
        reconcile_player_loyalty(self.player, actor=self.staff)
        response = self.client.get(self.url("home"))
        self.assertEqual(response.context["summary"].next_milestone.threshold, 20)
        self.assertContains(response, "Available reward")
        self.assertContains(response, "Dinker")
        self.assertNotContains(response, "Reward pending session completion")

    def test_highest_milestone_uses_configuration(self):
        self.history(100)
        response = self.client.get(self.url("home"))
        self.assertContains(response, "No further milestone configured")
        self.assertEqual(response.context["summary"].current_level.name, "Legend")

    def test_dashboard_query_count_does_not_grow_per_attendance(self):
        self.history(120)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.url("home"))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(captured), 16)
        with CaptureQueriesContext(connection) as captured:
            history = self.client.get(self.url("attendance"))
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.context["page"]), 25)
        self.assertLessEqual(len(captured), 8)

    def test_attendance_history_distinguishes_void_cancelled_and_pending(self):
        good = self.session(3)
        void = self.session(4)
        cancelled = self.session(5, cancelled=True)
        self.attend(good)
        self.attend(void, valid=False)
        self.attend(cancelled)
        response = self.client.get(self.url("attendance"))
        self.assertContains(response, "Approved — counts")
        self.assertContains(response, "Void — does not count")
        self.assertContains(response, "Cancelled session — does not count")
        self.assertEqual(self.client.get(self.url("home")).context["summary"].openplays_attended, 1)

    def test_reward_status_and_historical_snapshot(self):
        self.history(30)
        reconcile_player_loyalty(self.player, actor=self.staff)
        rewards = list(RewardIssuance.objects.filter(player=self.player).order_by("milestone__threshold"))
        self.assertEqual(len(rewards), 3)
        RewardRedemption.objects.create(issuance=rewards[0], player=self.player, redeemed_by=self.staff, openplay=self.future)
        rewards[0].status = RewardIssuance.Status.REDEEMED
        rewards[0].save()
        rewards[2].status = RewardIssuance.Status.REVOKED
        rewards[2].revoked_at = self.now
        rewards[2].revoked_reason = "Correction"
        rewards[2].save()
        rewards[1].milestone.benefits = [{"kind": "DISCOUNT", "description": "Changed later", "amount_php": 999}]
        rewards[1].milestone.save()
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.url("rewards"))
        self.assertContains(response, "Available")
        self.assertContains(response, "Redeemed")
        self.assertContains(response, "Revoked")
        self.assertContains(response, "₱100 OFF one OpenPlay")
        self.assertNotContains(response, "Changed later")
        self.assertContains(response, "for OpenPlay at Court Green")
        self.assertLessEqual(len(captured), 8)
        self.assertEqual(self.client.get("/player/rewards/1/redeem/").status_code, 404)

    def test_streaks_and_badges_derive_only_from_completed_sessions(self):
        for day in (9, 8, 7, 6):
            self.attend(self.session(day))  # Walk-in attendance, no registration.
        streaks = get_streaks(self.player)
        self.assertEqual((streaks.current, streaks.best), (4, 4))
        response = self.client.get(self.url("home"))
        self.assertContains(response, "Current Streak: 4 OpenPlays")
        self.assertContains(response, "Best Streak: 4 OpenPlays")
        self.assertContains(response, "First Rally")
        self.assertContains(response, "On a Roll")
        skipped = self.session(5)
        Registration.objects.create(player=self.player, openplay=skipped)
        neutral = self.session(4)
        Registration.objects.create(player=self.player, openplay=neutral, status="CANCELLED", cancelled_at=self.now)
        self.attend(self.session(3))
        future = self.future
        Registration.objects.create(player=self.player, openplay=future)
        self.attend(future)
        streaks = get_streaks(self.player)
        self.assertEqual((streaks.current, streaks.best), (1, 4))
        self.assertEqual(len(get_badges(completed_attendances=5, streaks=streaks)), 2)
        self.assertContains(self.client.get(self.url("home")), "Current Streak: 1 OpenPlays")

    def test_badges_wait_for_completion_and_recalculate_after_correction(self):
        self.attend(self.future)
        self.assertNotContains(self.client.get(self.url("home")), "First Rally")
        completed = self.session(2)
        row = self.attend(completed)
        self.assertContains(self.client.get(self.url("home")), "First Rally")
        row.status = Attendance.Status.VOID
        row.voided_at = self.now
        row.voided_by = self.staff
        row.void_reason = "Correction"
        row.save()
        self.assertNotContains(self.client.get(self.url("home")), "First Rally")

    def test_streak_cancelled_session_void_and_backdated_history(self):
        oldest = self.session(10)
        latest = self.session(2)
        self.attend(oldest)
        self.attend(latest)
        self.attend(self.session(5, cancelled=True))
        self.assertEqual(get_streaks(self.player).current, 2)
        middle = self.session(6)
        Registration.objects.create(player=self.player, openplay=middle)
        self.assertEqual(get_streaks(self.player).current, 1)
        corrected = self.attend(middle, valid=False)
        self.assertEqual(get_streaks(self.player).current, 1)
        corrected.status = Attendance.Status.VALID
        corrected.voided_at = None
        corrected.voided_by = None
        corrected.void_reason = ""
        corrected.save()
        self.assertEqual(get_streaks(self.player).current, 3)

    def test_profile_creation_edit_and_no_walk_in_claiming(self):
        walk_in = Player.objects.create(display_name="Member One", contact_email=self.user.email)
        self.player.user = None
        self.player.phone = "111"
        self.player.save()
        response = self.client.get(self.url("home"))
        self.assertRedirects(response, self.url("profile"))
        response = self.client.post(self.url("profile"), {
            "display_name": "Fresh Profile", "phone": "333", "reclub_name": "Fresh on ReClub",
            "reclub_profile_url": "https://example.com/reclub/fresh",
        }, follow=True)
        self.assertContains(response, "Profile saved")
        linked = Player.objects.get(user=self.user)
        self.assertNotEqual(linked.pk, walk_in.pk)
        self.assertEqual(linked.phone, "333")
        self.client.post(self.url("profile"), {
            "display_name": "Updated Member", "phone": "444", "reclub_name": "Fresh on ReClub",
            "reclub_profile_url": "https://example.com/reclub/fresh",
        })
        linked.refresh_from_db()
        self.assertEqual(linked.display_name, "Updated Member")
        self.assertEqual(linked.phone, "444")
        self.assertEqual(linked.contact_email, None)
        self.assertEqual(self.user.email, "member@example.com")
        self.assertTrue(AuditEvent.objects.filter(action="PLAYER_PROFILE_UPDATED", target_id=linked.pk).exists())

    def test_profile_cannot_claim_another_players_reclub_link(self):
        self.other.reclub_name = "Private on ReClub"
        self.other.reclub_profile_url = "https://example.com/reclub/private"
        self.other.save()

        response = self.client.post(self.url("profile"), {
            "display_name": "Member One", "phone": "111", "reclub_name": "Member on ReClub",
            "reclub_profile_url": "https://example.com/reclub/PRIVATE",
        })

        self.assertContains(response, "This ReClub profile is already in club records")
        self.player.refresh_from_db()
        self.assertEqual(self.player.reclub_profile_url, "")
