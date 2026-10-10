from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.loyalty import reconcile_player_loyalty, redeem_reward
from club.models import Attendance, AttendanceSubmission, AuditEvent, OpenPlay, Player, Registration, RewardIssuance, RewardRedemption, Venue
from club.services import restore_attendance, review_attendance_submission, void_attendance


class OrganizerRequestTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        self.player = Player.objects.create(
            user=self.member, display_name="Jane Player", phone="09171234567",
            reclub_name="Jane ReClub", reclub_profile_url="https://example.com/reclub/jane",
        )
        self.other = Player.objects.create(
            display_name="Other Player", phone="09170000000",
            reclub_name="Other ReClub", reclub_profile_url="https://example.com/reclub/other",
        )
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        self.current = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now - timedelta(minutes=15),
            ends_at=self.now + timedelta(hours=1),
        )
        self.past = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now - timedelta(hours=3),
            ends_at=self.now - timedelta(hours=1),
        )
        self.future = OpenPlay.objects.create(
            venue=self.venue, starts_at=self.now + timedelta(days=1),
            ends_at=self.now + timedelta(days=1, hours=2),
        )
        Registration.objects.create(player=self.player, openplay=self.current)
        self.client.force_login(self.organizer)

    def url(self, name, *args):
        return reverse(f"organizer:{name}", args=args)

    def history(self, count):
        sessions = OpenPlay.objects.bulk_create([
            OpenPlay(
                venue=self.venue, starts_at=self.now - timedelta(days=i + 2),
                ends_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
                status=OpenPlay.Status.COMPLETED,
                completed_at=self.now - timedelta(days=i + 2) + timedelta(hours=2),
            )
            for i in range(count)
        ])
        Attendance.objects.bulk_create([
            Attendance(player=self.player, openplay=session, source=Attendance.Source.HISTORICAL,
                       recorded_by=self.admin, checked_in_at=session.starts_at)
            for session in sessions
        ])
        return sessions

    def test_login_logout_and_role_guards(self):
        self.client.logout()
        response = self.client.get(self.url("home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        response = self.client.post(reverse("login"), {
            "username": "organizer@example.com", "password": "Securepass123"
        })
        self.assertRedirects(response, self.url("home"))
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)
        self.client.post(reverse("logout"))
        self.client.force_login(self.member)
        for url in [self.url("home"), self.url("session", self.current.pk),
                    self.url("players"), self.url("player", self.player.pk),
                    self.url("complete", self.current.pk), self.url("cancel", self.current.pk)]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self.url("check_in", self.current.pk, self.player.pk)).status_code, 403)
        self.assertEqual(self.client.post(self.url("complete", self.past.pk)).status_code, 403)
        self.assertEqual(self.client.post(self.url("cancel", self.current.pk), {"reason": "Rain"}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)
        self.assertEqual(self.client.get(self.url("session", self.current.pk)).status_code, 200)
        Player.objects.create(user=self.organizer, display_name="Organizer who plays")
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(self.url("home")).status_code, 200)

    def test_is_staff_without_club_group_cannot_operate(self):
        staff_only = get_user_model().objects.create_user(
            "django-staff@example.com", "Securepass123", is_staff=True,
        )
        self.client.force_login(staff_only)
        self.assertEqual(self.client.get(self.url("home")).status_code, 403)
        self.assertEqual(self.client.get(self.url("session", self.current.pk)).status_code, 403)
        self.assertEqual(self.client.get(self.url("player", self.player.pk)).status_code, 403)
        self.assertEqual(self.client.post(self.url("check_in", self.current.pk, self.player.pk)).status_code, 403)
        self.assertEqual(self.client.get(self.url("correct", 1)).status_code, 403)

    def test_mutations_require_post_and_csrf(self):
        self.assertEqual(self.client.get(self.url("check_in", self.current.pk, self.player.pk)).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.organizer)
        self.assertEqual(csrf_client.post(self.url("check_in", self.current.pk, self.player.pk)).status_code, 403)
        self.assertFalse(Attendance.objects.filter(player=self.player, openplay=self.current).exists())

    def test_home_today_counts_and_empty_state(self):
        # A session starting just before midnight belongs to yesterday, even
        # though check-in is active now. Keep this fixture on today's date.
        self.current.starts_at = self.now
        self.current.save(update_fields=["starts_at", "updated_at"])
        self.past.starts_at = self.now - timedelta(days=2)
        self.past.ends_at = self.past.starts_at + timedelta(hours=2)
        self.past.save(update_fields=["starts_at", "ends_at", "updated_at"])
        response = self.client.get(self.url("home"))
        self.assertContains(response, "Today's OpenPlays")
        self.assertContains(response, "Main Court")
        self.assertContains(response, "1 registered")
        self.assertContains(response, "0")
        self.current.starts_at += timedelta(days=3)
        self.current.ends_at += timedelta(days=3)
        self.current.save()
        self.assertContains(self.client.get(self.url("home")), "No OpenPlays today")

    def test_roster_search_and_check_in_confirmation(self):
        response = self.client.get(self.url("session", self.current.pk))
        self.assertContains(response, "Jane Player")
        self.assertContains(response, "No attendance submitted")
        self.assertContains(response, "Registered, awaiting approval")
        response = self.client.get(self.url("session", self.current.pk), {"q": "091712"})
        self.assertContains(response, "Registered for this OpenPlay")
        response = self.client.get(self.url("session", self.current.pk), {"q": "Other"})
        self.assertContains(response, "Existing Player · not registered")
        response = self.client.post(
            self.url("check_in", self.current.pk, self.player.pk), {"q": "Jane"}, follow=True
        )
        self.assertContains(response, "pending admin approval")
        self.assertContains(response, "Pending admin approval")
        self.assertFalse(Attendance.objects.filter(player=self.player, openplay=self.current).exists())
        submission = AttendanceSubmission.objects.get(player=self.player, openplay=self.current)
        response = self.client.post(self.url("check_in", self.current.pk, self.player.pk), follow=True)
        self.assertContains(response, "pending admin approval")
        self.client.force_login(self.admin)
        review_attendance_submission(submission_id=submission.pk, actor=self.admin, approve=True)
        attendance = Attendance.objects.get(player=self.player, openplay=self.current)
        self.assertEqual(attendance.source, Attendance.Source.CLAIM)
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_APPROVED", target_id=submission.pk).exists())
        self.assertEqual(Attendance.objects.filter(player=self.player, openplay=self.current).count(), 1)

    def test_void_cancelled_and_outside_window_check_ins(self):
        self.client.post(self.url("check_in", self.current.pk, self.player.pk))
        submission = AttendanceSubmission.objects.get(player=self.player, openplay=self.current)
        review_attendance_submission(submission_id=submission.pk, actor=self.admin, approve=True)
        attendance = Attendance.objects.get(player=self.player, openplay=self.current)
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Wrong player")
        response = self.client.post(self.url("check_in", self.current.pk, self.player.pk), follow=True)
        self.assertContains(response, "requires an ADMIN restoration")
        self.assertContains(response, "Void attendance")
        attendance.refresh_from_db()
        self.assertEqual(attendance.status, Attendance.Status.VOID)
        self.assertFalse(Attendance.objects.filter(player=self.other, openplay=self.current).exists())
        response = self.client.post(self.url("check_in", self.future.pk, self.other.pk), follow=True)
        self.assertContains(response, "has started")
        self.current.status = OpenPlay.Status.CANCELLED
        self.current.cancelled_at = timezone.now()
        self.current.cancellation_reason = "Rain"
        self.current.save()
        response = self.client.post(self.url("check_in", self.current.pk, self.other.pk), follow=True)
        self.assertContains(response, "was not cancelled")
        self.assertFalse(Attendance.objects.filter(player=self.other).exists())

    def test_submit_button_appears_only_once_the_openplay_has_started(self):
        Registration.objects.create(player=self.player, openplay=self.future)
        upcoming = self.client.get(self.url("session", self.future.pk), {"q": "Jane"})
        self.assertNotContains(upcoming, "Submit attendance for Jane Player")
        self.assertContains(upcoming, "Attendance opens when the OpenPlay starts")
        self.assertFalse(upcoming.context["session_started"])
        started = self.client.get(self.url("session", self.current.pk), {"q": "Jane"})
        self.assertContains(started, "Submit attendance for Jane Player")
        self.assertNotContains(started, "Attendance opens when the OpenPlay starts")
        self.assertTrue(started.context["session_started"])

    def test_new_player_must_sign_up_before_staff_can_submit(self):
        response = self.client.get(self.url("session", self.current.pk), {"q": "New Walk In"})
        self.assertContains(response, "New players must create an account")
        self.assertNotContains(response, "Create a new walk-in")
        response = self.client.get(self.url("session", self.current.pk), {"q": "Other"})
        self.assertContains(response, "Account required before attendance submission")
        self.assertNotContains(response, "Submit attendance for Other Player")
        self.assertEqual(self.client.post(f"/organizer/openplays/{self.current.pk}/walk-in/").status_code, 404)
        self.assertEqual(Player.objects.count(), 2)

    def test_100_player_roster_query_budget(self):
        players = Player.objects.bulk_create([
            Player(display_name=f"Roster Player {i:03}", phone=f"09{i:09}") for i in range(100)
        ])
        Registration.objects.bulk_create([
            Registration(player=player, openplay=self.current) for player in players
        ])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url("session", self.current.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Roster Player 099")
        self.assertContains(response, "101 players")
        self.assertLess(len(queries), 20)

    def test_completion_confirmation_and_loyalty_timing(self):
        self.history(9)
        Attendance.objects.create(
            player=self.player, openplay=self.past, source=Attendance.Source.HISTORICAL,
            recorded_by=self.admin,
        )
        Registration.objects.create(player=self.other, openplay=self.past)
        response = self.client.get(self.url("complete", self.past.pk))
        self.assertContains(response, "Registered, unchecked")
        self.assertContains(response, "remain unchecked")
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())
        response = self.client.post(self.url("complete", self.past.pk), follow=True)
        self.assertContains(response, "Loyalty eligibility has been reconciled")
        self.past.refresh_from_db()
        self.assertEqual(self.past.status, OpenPlay.Status.COMPLETED)
        self.assertEqual(RewardIssuance.objects.get(player=self.player).status, RewardIssuance.Status.AVAILABLE)
        self.assertEqual(self.client.post(self.url("complete", self.past.pk)).status_code, 409)
        self.assertEqual(self.client.post(self.url("complete", self.current.pk)).status_code, 409)

    def test_cancellation_reason_and_completed_session_admin_guard(self):
        url = self.url("cancel", self.current.pk)
        self.assertContains(self.client.get(url), "Cancellation reason")
        self.assertEqual(self.client.post(url, {"reason": ""}).status_code, 400)
        self.assertEqual(OpenPlay.objects.get(pk=self.current.pk).status, OpenPlay.Status.SCHEDULED)
        response = self.client.post(url, {"reason": "Rain"}, follow=True)
        self.assertContains(response, "OpenPlay cancelled")
        self.assertEqual(OpenPlay.objects.get(pk=self.current.pk).status, OpenPlay.Status.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(action="OPENPLAY_CANCELLED", target_id=self.current.pk).exists())
        self.past.status = OpenPlay.Status.COMPLETED
        self.past.completed_at = timezone.now()
        self.past.save()
        self.assertEqual(self.client.get(self.url("cancel", self.past.pk)).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url("cancel", self.past.pk)).status_code, 200)

    def test_player_lookup_pending_reward_and_redemption(self):
        self.history(9)
        self.client.post(self.url("check_in", self.current.pk, self.player.pk))
        submission = AttendanceSubmission.objects.get(player=self.player, openplay=self.current)
        review_attendance_submission(submission_id=submission.pk, actor=self.admin, approve=True)
        response = self.client.get(self.url("player", self.player.pk))
        self.assertContains(response, "OpenPlays Attended")
        self.assertContains(response, "New rewards become available after")
        self.assertNotContains(response, "Dinker")
        self.history(1)
        reconcile_player_loyalty(self.player, actor=self.organizer)
        response = self.client.get(self.url("players"), {"q": "member@example.com"})
        self.assertContains(response, "Jane Player")
        response = self.client.get(self.url("player", self.player.pk))
        self.assertContains(response, "Dinker")
        issuance = RewardIssuance.objects.get(player=self.player)
        redeem_url = self.url("redeem", self.player.pk, issuance.pk)
        self.assertContains(self.client.get(redeem_url), "Target OpenPlay")
        response = self.client.post(redeem_url, {"openplay": self.future.pk}, follow=True)
        self.assertContains(response, "Reward redemption confirmed")
        self.assertContains(response, "Redeemed")
        self.assertEqual(RewardRedemption.objects.get(issuance=issuance).openplay_id, self.future.pk)
        response = self.client.post(redeem_url, {"openplay": self.future.pk})
        self.assertContains(response, "Only an available reward", status_code=409)
        self.assertEqual(self.client.get(self.url("redeem", self.other.pk, issuance.pk)).status_code, 404)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(redeem_url).status_code, 403)
        self.assertEqual(self.client.post(redeem_url, {"openplay": self.future.pk}).status_code, 403)

    def test_revoked_reward_is_not_offered_for_redemption(self):
        self.history(10)
        reconcile_player_loyalty(self.player, actor=self.admin)
        attendance = Attendance.objects.filter(player=self.player).first()
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Incorrect check-in")
        issuance = RewardIssuance.objects.get(player=self.player)
        response = self.client.get(self.url("player", self.player.pk))
        self.assertContains(response, "Revoked")
        self.assertNotContains(response, ">Redeem</a>")
        response = self.client.get(self.url("redeem", self.player.pk, issuance.pk))
        self.assertContains(response, "Only an available reward", status_code=409)

    def test_redeemed_reward_discrepancy_is_visible_to_staff_until_resolved(self):
        self.history(10)
        reconcile_player_loyalty(self.player, actor=self.admin)
        issuance = RewardIssuance.objects.get(player=self.player)
        redeem_reward(issuance_id=issuance.pk, player_id=self.player.pk, actor=self.organizer)
        detail = self.url("player", self.player.pk)
        self.assertNotContains(self.client.get(detail), "later corrections left this player below")
        attendance = Attendance.objects.filter(player=self.player).first()
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Incorrect check-in")
        self.assertContains(self.client.get(detail), "later corrections left this player below")
        restore_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Verified")
        self.assertNotContains(self.client.get(detail), "later corrections left this player below")

    def test_admin_correction_requires_reason_and_reconciles(self):
        self.history(10)
        reconcile_player_loyalty(self.player, actor=self.admin)
        attendance = Attendance.objects.filter(player=self.player).first()
        url = self.url("correct", attendance.pk)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"action": "VOID", "reason": "Mistake"}).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(url), "Correct attendance")
        self.assertEqual(self.client.post(url, {"action": "VOID", "reason": ""}).status_code, 400)
        response = self.client.post(url, {"action": "VOID", "reason": "Wrong check-in"}, follow=True)
        self.assertContains(response, "Attendance correction recorded")
        attendance.refresh_from_db()
        self.assertEqual(attendance.status, Attendance.Status.VOID)
        self.assertEqual(RewardIssuance.objects.get(player=self.player).status, RewardIssuance.Status.REVOKED)
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_VOIDED", target_id=attendance.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="REWARD_REVOKED").exists())
        self.client.post(url, {"action": "RESTORE", "reason": "Verified from roster"})
        attendance.refresh_from_db()
        self.assertEqual(attendance.status, Attendance.Status.VALID)
        self.assertEqual(RewardIssuance.objects.get(player=self.player).status, RewardIssuance.Status.AVAILABLE)
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_RESTORED", target_id=attendance.pk).exists())

    def test_full_staff_walkthrough_with_20_player_roster(self):
        extra = Player.objects.bulk_create([
            Player(display_name=f"Club Player {i:02}", phone=f"0918{i:07}") for i in range(19)
        ])
        Registration.objects.bulk_create([
            Registration(player=player, openplay=self.current) for player in extra
        ])
        self.history(9)
        self.client.logout()
        self.assertRedirects(self.client.post(reverse("login"), {
            "username": "organizer@example.com", "password": "Securepass123",
        }), self.url("home"))
        self.assertContains(self.client.get(self.url("home")), "20 registered")
        self.assertContains(self.client.get(self.url("session", self.current.pk), {"q": "Jane"}), "Jane Player")
        self.assertContains(self.client.post(
            self.url("check_in", self.current.pk, self.player.pk), follow=True
        ), "pending admin approval")
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())
        self.assertContains(self.client.post(
            self.url("check_in", self.current.pk, self.player.pk), follow=True
        ), "pending admin approval")
        self.assertContains(self.client.get(self.url("session", self.current.pk), {"q": "New Walk In"}),
                            "New players must create an account")
        self.assertContains(self.client.get(self.url("session", self.current.pk)), "Registered, awaiting approval")
        submission = AttendanceSubmission.objects.get(player=self.player, openplay=self.current)
        review_attendance_submission(submission_id=submission.pk, actor=self.admin, approve=True)
        self.current.ends_at = timezone.now() - timedelta(minutes=1)
        self.current.save(update_fields=["ends_at", "updated_at"])
        self.assertContains(self.client.get(self.url("complete", self.current.pk)), "19 registered players remain unchecked")
        self.assertContains(self.client.post(
            self.url("complete", self.current.pk), follow=True
        ), "Loyalty eligibility has been reconciled")
        issuance = RewardIssuance.objects.get(player=self.player)
        self.assertEqual(issuance.status, RewardIssuance.Status.AVAILABLE)
        self.assertContains(self.client.get(self.url("player", self.player.pk)), "Dinker")
        redeem_url = self.url("redeem", self.player.pk, issuance.pk)
        self.assertContains(self.client.post(redeem_url, {"openplay": self.future.pk}, follow=True),
                            "Reward redemption confirmed")
        self.assertContains(self.client.post(redeem_url, {"openplay": self.future.pk}),
                            "Only an available reward", status_code=409)
