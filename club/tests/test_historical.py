from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.models import Attendance, AuditEvent, OpenPlay, Player, RewardIssuance, Venue
from club.services import openplays_attended


class HistoricalAttendanceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()
        self.session = self.make_session(days_ago=1, status=OpenPlay.Status.COMPLETED)
        self.player = Player.objects.create(display_name="Missed Marco", phone="09170000001")
        self.valid_player = Player.objects.create(display_name="Valid Vera", phone="09170000002")
        self.void_player = Player.objects.create(display_name="Void Vic", phone="09170000003")
        self.valid = Attendance.objects.create(
            player=self.valid_player, openplay=self.session, source=Attendance.Source.ROSTER, recorded_by=self.organizer)
        self.void = Attendance.objects.create(
            player=self.void_player, openplay=self.session, source=Attendance.Source.ROSTER, recorded_by=self.organizer,
            status=Attendance.Status.VOID, voided_at=self.now, voided_by=self.admin, void_reason="Mistake")
        self.search_url = reverse("organizer:historical_search", args=[self.session.pk])

    def make_session(self, *, days_ago, status):
        start = self.now - timedelta(days=days_ago)
        fields = {"status": status}
        if status == OpenPlay.Status.COMPLETED:
            fields["completed_at"] = start + timedelta(hours=2)
        elif status == OpenPlay.Status.CANCELLED:
            fields.update(cancelled_at=self.now, cancellation_reason="Rain")
        return OpenPlay.objects.create(
            venue=self.venue, starts_at=start, ends_at=start + timedelta(hours=2), **fields)

    def add_url(self, player=None, session=None):
        return reverse("organizer:historical_add", args=[(session or self.session).pk, (player or self.player).pk])

    def post_add(self, player=None, session=None, reason="Paper roster showed attendance", **extra):
        return self.client.post(self.add_url(player, session), {"reason": reason}, **extra)

    def give_history(self, player, count):
        """Completed, valid attendances on other sessions so a milestone threshold is one away."""
        for index in range(count):
            old = self.make_session(days_ago=10 + index, status=OpenPlay.Status.COMPLETED)
            Attendance.objects.create(
                player=player, openplay=old, source=Attendance.Source.HISTORICAL, recorded_by=self.admin)

    def assert_no_new_attendance(self, player, session=None):
        self.assertFalse(Attendance.objects.filter(player=player, openplay=session or self.session)
                         .exclude(pk__in=[self.valid.pk, self.void.pk]).exists())
        self.assertFalse(AuditEvent.objects.filter(action="ATTENDANCE_ADDED").exists())

    # --- authorization ---

    def test_anonymous_is_sent_to_login(self):
        for response in (self.client.get(self.search_url), self.client.get(self.add_url()), self.post_add()):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response["Location"])
        self.assert_no_new_attendance(self.player)

    def test_player_and_organizer_are_forbidden_everywhere(self):
        for user in (self.member, self.organizer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.search_url).status_code, 403)
            self.assertEqual(self.client.get(self.add_url()).status_code, 403)
            self.assertEqual(self.post_add().status_code, 403)
        self.assert_no_new_attendance(self.player)

    def test_admin_can_open_both_pages(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.search_url), "Add historical attendance")
        self.assertContains(self.client.get(self.add_url()), "Reason for audit")

    def test_entry_point_is_admin_only_and_completed_only(self):
        scheduled = self.make_session(days_ago=-2, status=OpenPlay.Status.SCHEDULED)
        cancelled = self.make_session(days_ago=3, status=OpenPlay.Status.CANCELLED)
        page = lambda session: reverse("organizer:session", args=[session.pk])  # noqa: E731
        link = lambda session: reverse("organizer:historical_search", args=[session.pk])  # noqa: E731
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(page(self.session)), link(self.session))
        self.assertNotContains(self.client.get(page(scheduled)), link(scheduled))
        self.assertNotContains(self.client.get(page(cancelled)), link(cancelled))
        self.client.force_login(self.organizer)
        self.assertNotContains(self.client.get(page(self.session)), link(self.session))

    # --- search and confirm pages ---

    def test_search_shows_existing_state_and_only_offers_add_when_none(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.search_url, {"q": "09170000"})
        self.assertContains(page, "Missed Marco")
        self.assertContains(page, self.add_url(self.player))
        self.assertContains(page, "Already recorded: Valid")
        self.assertContains(page, "Already recorded: Void")
        self.assertEqual(self.client.get(self.search_url, {"q": "M"}).context["results"], [])

    def test_search_on_non_completed_session_explains_and_lists_nothing(self):
        scheduled = self.make_session(days_ago=-2, status=OpenPlay.Status.SCHEDULED)
        self.client.force_login(self.admin)
        page = self.client.get(reverse("organizer:historical_search", args=[scheduled.pk]), {"q": "Marco"})
        self.assertContains(page, "only to a completed OpenPlay")
        self.assertNotContains(page, "Missed Marco")

    def test_confirm_page_shows_session_player_and_current_count_without_mutating(self):
        self.give_history(self.player, 4)
        self.client.force_login(self.admin)
        page = self.client.get(self.add_url())
        self.assertContains(page, "Missed Marco")
        self.assertContains(page, "Main Court")
        self.assertContains(page, "OpenPlays Attended now: <strong>4</strong>")
        self.assertEqual(Attendance.objects.filter(player=self.player, openplay=self.session).count(), 0)
        self.assertEqual(openplays_attended(self.player), 4)
        self.assertEqual(AuditEvent.objects.filter(action="ATTENDANCE_ADDED").count(), 0)

    def test_unknown_ids_are_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("organizer:historical_search", args=[999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("organizer:historical_add", args=[self.session.pk, 999999])).status_code, 404)

    # --- the mutation ---

    def test_successful_add_audits_counts_and_issues_reward_at_milestone(self):
        self.give_history(self.player, 9)
        self.client.force_login(self.admin)
        self.assertEqual(openplays_attended(self.player), 9)
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())
        response = self.post_add(reason="Paper roster showed attendance")
        self.assertRedirects(response, reverse("organizer:player", args=[self.player.pk]), fetch_redirect_response=False)
        attendance = Attendance.objects.get(player=self.player, openplay=self.session)
        self.assertEqual(attendance.status, Attendance.Status.VALID)
        self.assertEqual(attendance.source, Attendance.Source.HISTORICAL)
        self.assertEqual(attendance.recorded_by, self.admin)
        self.assertEqual(attendance.checked_in_at, self.session.starts_at)
        self.assertEqual(openplays_attended(self.player), 10)
        added = AuditEvent.objects.get(action="ATTENDANCE_ADDED", target_id=attendance.pk)
        self.assertEqual((added.actor, added.details["reason"]), (self.admin, "Paper roster showed attendance"))
        issuance = RewardIssuance.objects.get(player=self.player)
        self.assertEqual((issuance.status, issuance.terms_snapshot["threshold"]), ("AVAILABLE", 10))
        self.assertTrue(AuditEvent.objects.filter(action="REWARD_ISSUED", target_id=issuance.pk).exists())
        followed = self.client.get(response["Location"])
        self.assertContains(followed, "Loyalty has been reconciled")
        self.assertContains(followed, "Dinker")

    def test_add_below_milestone_issues_no_reward(self):
        self.client.force_login(self.admin)
        self.post_add()
        self.assertEqual(openplays_attended(self.player), 1)
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())

    def test_existing_valid_attendance_is_rejected_and_unchanged(self):
        self.client.force_login(self.admin)
        page = self.client.get(self.add_url(self.valid_player))
        self.assertContains(page, "already recorded")
        self.assertNotContains(page, "Reason for audit")
        response = self.post_add(self.valid_player)
        self.assertContains(response, "Attendance already exists", status_code=409)
        self.valid.refresh_from_db()
        self.assertEqual(self.valid.status, Attendance.Status.VALID)
        self.assertEqual(Attendance.objects.filter(player=self.valid_player, openplay=self.session).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action="ATTENDANCE_ADDED").exists())

    def test_existing_void_attendance_is_rejected_and_points_to_restoration(self):
        self.client.force_login(self.admin)
        correct = reverse("organizer:correct", args=[self.void.pk])
        self.assertContains(self.client.get(self.add_url(self.void_player)), correct)
        response = self.post_add(self.void_player)
        self.assertContains(response, "use restoration for a void record", status_code=409)
        self.assertContains(response, correct, status_code=409)
        self.void.refresh_from_db()
        self.assertEqual(self.void.status, Attendance.Status.VOID)
        self.assertFalse(AuditEvent.objects.filter(action="ATTENDANCE_ADDED").exists())

    def test_scheduled_and_cancelled_sessions_are_rejected(self):
        scheduled = self.make_session(days_ago=-2, status=OpenPlay.Status.SCHEDULED)
        cancelled = self.make_session(days_ago=3, status=OpenPlay.Status.CANCELLED)
        self.client.force_login(self.admin)
        for session in (scheduled, cancelled):
            page = self.client.get(self.add_url(session=session))
            self.assertNotContains(page, "Reason for audit")
            response = self.post_add(session=session)
            self.assertContains(response, "requires a completed OpenPlay", status_code=409)
            self.assertFalse(Attendance.objects.filter(player=self.player, openplay=session).exists())
        self.assertFalse(AuditEvent.objects.filter(action="ATTENDANCE_ADDED").exists())

    def test_reason_is_required(self):
        self.client.force_login(self.admin)
        for reason in ("", "   ", None):
            data = {} if reason is None else {"reason": reason}
            response = self.client.post(self.add_url(), data)
            self.assertContains(response, "This field is required", status_code=400)
        self.assert_no_new_attendance(self.player)

    def test_csrf_is_enforced_and_get_never_mutates(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(self.add_url(), {"reason": "No token"}).status_code, 403)
        self.client.force_login(self.admin)
        self.client.get(self.add_url(), {"reason": "In the query string"})
        self.client.get(self.search_url, {"q": "Marco", "reason": "x"})
        self.assert_no_new_attendance(self.player)

    def test_backfill_workflow_schedule_past_session_complete_then_add_attendance(self):
        """The runbook's recovery path: an organizer records a session that already happened, an ADMIN adds attendance."""
        start = timezone.localtime(self.now - timedelta(days=5)).replace(microsecond=0, second=0)
        end = start + timedelta(hours=2)
        fmt = "%Y-%m-%dT%H:%M"
        self.client.force_login(self.organizer)
        created = self.client.post(reverse("organizer:openplay_new"), {
            "venue": self.venue.pk, "name": "Past session", "starts_at": start.strftime(fmt), "ends_at": end.strftime(fmt)})
        past = OpenPlay.objects.exclude(pk=self.session.pk).get()
        self.assertEqual(created.status_code, 302)
        self.assertEqual(self.client.post(reverse("organizer:complete", args=[past.pk])).status_code, 302)
        past.refresh_from_db()
        self.assertEqual(past.status, OpenPlay.Status.COMPLETED)
        self.client.force_login(self.admin)
        self.assertEqual(self.post_add(session=past).status_code, 302)
        self.assertTrue(Attendance.objects.filter(
            player=self.player, openplay=past, status=Attendance.Status.VALID).exists())
