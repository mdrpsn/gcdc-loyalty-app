from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from club.calendar_grid import month_bounds, month_context, month_grid, parse_month, shift_month
from club.loyalty import get_upcoming_milestones
from club.models import Attendance, LoyaltyMilestone, OpenPlay, Player, Registration, RewardIssuance, Venue
from club.services import counted_openplay_starts, openplays_attended
from club.templatetags.club_roles import avatar_initials


class CalendarGridTests(SimpleTestCase):
    today = date(2026, 10, 4)

    def test_parse_month_accepts_valid_and_falls_back_for_anything_else(self):
        self.assertEqual(parse_month("2026-09", self.today), date(2026, 9, 1))
        for bad in (None, "", "abc", "2026", "2026-13", "2026-00", "2026-9-1", "99999-01", "0000-01", "2026/09"):
            self.assertEqual(parse_month(bad, self.today), date(2026, 10, 1), bad)
        self.assertEqual(parse_month("2040-01", self.today), date(2026, 10, 1))  # too far away

    def test_shift_month_crosses_year_boundaries(self):
        self.assertEqual(shift_month(date(2026, 1, 1), -1), date(2025, 12, 1))
        self.assertEqual(shift_month(date(2026, 12, 1), 1), date(2027, 1, 1))
        self.assertEqual(shift_month(date(2026, 10, 1), 0), date(2026, 10, 1))

    def test_grid_is_sunday_first_with_blank_cells_outside_the_month(self):
        grid = month_grid(date(2026, 10, 1), {date(2026, 10, 5): "session"}, self.today)
        self.assertEqual(len(grid[0]), 7)
        self.assertEqual([cell is None for cell in grid[0]], [True, True, True, True, False, False, False])  # 1 Oct 2026 is Thursday
        flat = [cell for week in grid for cell in week if cell]
        self.assertEqual([cell["day"] for cell in flat], list(range(1, 32)))
        self.assertEqual([cell["day"] for cell in flat if cell["mark"]], [5])
        self.assertEqual([cell["day"] for cell in flat if cell["today"]], [4])

    def test_context_labels_and_navigation(self):
        context = month_context(date(2026, 1, 1), {}, self.today)
        self.assertEqual((context["label"], context["previous"], context["next"]), ("January 2026", "2025-12", "2026-02"))
        self.assertFalse(context["is_current"])
        self.assertTrue(month_context(date(2026, 10, 1), {}, self.today)["is_current"])

    @override_settings(TIME_ZONE="Asia/Manila")
    def test_month_bounds_use_the_club_timezone(self):
        start, end = month_bounds(date(2026, 10, 1))
        self.assertEqual(start, datetime(2026, 9, 30, 16, 0, tzinfo=dt_timezone.utc))  # Manila midnight is 16:00 UTC
        self.assertEqual(end, datetime(2026, 10, 31, 16, 0, tzinfo=dt_timezone.utc))


class PlayerUiTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("member@example.com")
        self.other_user = User.objects.create_user("other@example.com")
        self.player = Player.objects.create(user=self.user, display_name="Maya Santos", phone="111")
        self.other = Player.objects.create(user=self.other_user, display_name="Private Member", phone="222")
        self.venue = Venue.objects.create(name="Court Green")
        self.now = timezone.now()
        self.client.force_login(self.user)

    def session(self, start, *, status=OpenPlay.Status.SCHEDULED, hours=2):
        fields = {"status": status}
        if status == OpenPlay.Status.COMPLETED:
            fields["completed_at"] = start + timedelta(hours=hours)
        if status == OpenPlay.Status.CANCELLED:
            fields.update(cancelled_at=self.now, cancellation_reason="Rain")
        return OpenPlay.objects.create(venue=self.venue, starts_at=start, ends_at=start + timedelta(hours=hours), **fields)

    def attend(self, player, session, **fields):
        return Attendance.objects.create(player=player, openplay=session, source=Attendance.Source.WALK_IN,
                                         recorded_by=self.user, **fields)

    @staticmethod
    def marks(response):
        return {cell["day"]: cell["mark"] for week in response.context["calendar"]["weeks"]
                for cell in week if cell and cell["mark"]}

    @staticmethod
    def month_param(moment):
        return timezone.localtime(moment).strftime("%Y-%m")


class SharedAttendanceDefinitionTests(PlayerUiTestCase):
    def test_counted_starts_match_what_openplays_attended_counts(self):
        good = self.session(self.now - timedelta(days=3), status=OpenPlay.Status.COMPLETED)
        voided = self.session(self.now - timedelta(days=4), status=OpenPlay.Status.COMPLETED)
        cancelled = self.session(self.now - timedelta(days=5), status=OpenPlay.Status.CANCELLED)
        pending = self.session(self.now + timedelta(days=1))
        self.attend(self.player, good)
        self.attend(self.player, voided, status=Attendance.Status.VOID, voided_at=self.now,
                    voided_by=self.user, void_reason="Mistake")
        self.attend(self.player, cancelled)
        self.attend(self.player, pending)
        self.attend(self.other, good)
        wide = (self.now - timedelta(days=30), self.now + timedelta(days=30))
        starts = counted_openplay_starts(self.player, *wide)
        self.assertEqual(sorted(starts), sorted([good.starts_at, pending.starts_at]))
        self.assertEqual(len(starts), openplays_attended(self.player))

    def test_range_is_half_open(self):
        start = self.now.replace(microsecond=0) - timedelta(days=2)
        session = self.session(start, status=OpenPlay.Status.COMPLETED)
        self.attend(self.player, session)
        self.assertEqual(counted_openplay_starts(self.player, start, start + timedelta(seconds=1)), [start])
        self.assertEqual(counted_openplay_starts(self.player, start - timedelta(days=1), start), [])


class UpcomingMilestoneTests(PlayerUiTestCase):
    def complete(self, count):
        for index in range(count):
            self.attend(self.player, self.session(self.now - timedelta(days=10 + index), status=OpenPlay.Status.COMPLETED))

    def test_lists_unreached_active_milestones_nearest_first_with_progress(self):
        self.complete(4)
        upcoming = get_upcoming_milestones(self.player)
        self.assertEqual([item.milestone.threshold for item in upcoming], [10, 25, 50, 75, 100])
        self.assertEqual((upcoming[0].remaining, upcoming[0].percent), (6, 40))
        self.assertEqual(upcoming[1].remaining, 21)

    def test_reached_inactive_and_already_held_milestones_are_not_upcoming(self):
        self.complete(12)
        LoyaltyMilestone.objects.filter(threshold=75).update(is_active=False)
        held = LoyaltyMilestone.objects.get(threshold=50)
        RewardIssuance.objects.create(player=self.player, milestone=held, terms_snapshot={
            "threshold": 50, "name": held.name, "benefits": held.benefits})
        thresholds = [item.milestone.threshold for item in get_upcoming_milestones(self.player)]
        self.assertEqual(thresholds, [25, 100])  # 10 reached, 50 already held, 75 inactive

    def test_uses_completed_attendance_not_pending_check_ins(self):
        self.attend(self.player, self.session(self.now + timedelta(days=1)))  # checked in, not completed
        self.assertEqual(get_upcoming_milestones(self.player)[0].remaining, 10)


class PlayerShellTests(PlayerUiTestCase):
    def test_tab_bar_marks_only_the_current_page(self):
        for name, current in (("home", "home"), ("openplays", "openplays"), ("attendance", "attendance"),
                              ("rewards", "rewards"), ("profile", "profile")):
            page = self.client.get(reverse(f"player:{name}"))
            self.assertContains(page, f'href="{reverse("player:" + current)}" aria-current="page"')
            self.assertEqual(page.content.decode().count('aria-current="page"'), 1, name)

    def test_user_menu_shows_account_and_posts_sign_out(self):
        page = self.client.get(reverse("player:home"))
        self.assertContains(page, "member@example.com")
        self.assertContains(page, f'action="{reverse("logout")}"')
        self.assertContains(page, ">MS<")  # initials from "Maya Santos"

    def test_avatar_initials(self):
        User = get_user_model()
        self.assertEqual(avatar_initials(self.user), "MS")
        solo = User.objects.create_user("solo@example.com")
        Player.objects.create(user=solo, display_name="Cher", phone="9")
        self.assertEqual(avatar_initials(User.objects.get(pk=solo.pk)), "C")
        self.assertEqual(avatar_initials(User.objects.create_user("zed@example.com")), "Z")


class PlayerHomeUiTests(PlayerUiTestCase):
    def test_upcoming_card_shows_only_own_next_registered_session(self):
        later = self.session(self.now + timedelta(days=3))
        sooner = self.session(self.now + timedelta(days=1))
        theirs = self.session(self.now + timedelta(hours=5))
        Registration.objects.create(player=self.player, openplay=later)
        Registration.objects.create(player=self.player, openplay=sooner)
        Registration.objects.create(player=self.other, openplay=theirs)
        response = self.client.get(reverse("player:home"))
        self.assertEqual(response.context["next_session"], sooner)
        self.assertContains(response, "View Session")

    def test_cancelled_registration_and_ended_or_cancelled_sessions_are_not_upcoming(self):
        cancelled_reg = self.session(self.now + timedelta(days=1))
        Registration.objects.create(player=self.player, openplay=cancelled_reg, status=Registration.Status.CANCELLED,
                                    cancelled_at=self.now)
        cancelled_session = self.session(self.now + timedelta(days=2), status=OpenPlay.Status.CANCELLED)
        Registration.objects.create(player=self.player, openplay=cancelled_session)
        ended = self.session(self.now - timedelta(days=1))
        Registration.objects.create(player=self.player, openplay=ended)
        response = self.client.get(reverse("player:home"))
        self.assertIsNone(response.context["next_session"])
        self.assertContains(response, "Find your next OpenPlay")

    def test_rewards_ready_counts_only_available_rewards(self):
        milestones = list(LoyaltyMilestone.objects.order_by("threshold")[:3])
        for milestone, status in zip(milestones, ("AVAILABLE", "AVAILABLE", "REVOKED")):
            fields = {"revoked_at": self.now, "revoked_reason": "Correction"} if status == "REVOKED" else {}
            RewardIssuance.objects.create(player=self.player, milestone=milestone, status=status, terms_snapshot={
                "threshold": milestone.threshold, "name": milestone.name, "benefits": milestone.benefits}, **fields)
        response = self.client.get(reverse("player:home"))
        self.assertEqual(response.context["available_count"], 2)
        self.assertContains(response, "<strong>2</strong><span>Rewards ready</span>")

    def test_home_stays_within_its_query_budget(self):
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(reverse("player:home")).status_code, 200)
        self.assertLessEqual(len(captured), 16)


class OpenPlaysCalendarTests(PlayerUiTestCase):
    @override_settings(TIME_ZONE="Asia/Manila")
    def test_marks_session_days_in_club_local_time_and_registered_days_differently(self):
        base = timezone.now() + timedelta(days=2)
        local_midnight = timezone.localtime(base).replace(hour=0, minute=30, second=0, microsecond=0)
        # 00:30 local on a given day is the previous day in UTC; the calendar must use the local day.
        registered = self.session(local_midnight)
        open_one = self.session(local_midnight + timedelta(days=1, hours=8))
        Registration.objects.create(player=self.player, openplay=registered)
        response = self.client.get(reverse("player:openplays"), {"month": self.month_param(registered.starts_at)})
        marks = self.marks(response)
        self.assertEqual(marks[timezone.localtime(registered.starts_at).day], "registered")
        if timezone.localtime(open_one.starts_at).month == timezone.localtime(registered.starts_at).month:
            self.assertEqual(marks[timezone.localtime(open_one.starts_at).day], "session")
        self.assertNotIn(timezone.localtime(registered.starts_at).day - 1, marks)

    def test_ignores_cancelled_completed_and_other_months(self):
        start = self.now + timedelta(days=1)
        self.session(start, status=OpenPlay.Status.CANCELLED)
        self.session(self.now - timedelta(days=1), status=OpenPlay.Status.COMPLETED)
        response = self.client.get(reverse("player:openplays"), {"month": self.month_param(start)})
        self.assertEqual(self.marks(response), {})
        far = self.client.get(reverse("player:openplays"), {"month": self.month_param(start + timedelta(days=200))})
        self.assertEqual(self.marks(far), {})

    def test_invalid_month_values_never_error(self):
        for value in ("garbage", "2026-99", "", "1900-01", "2026-1-1"):
            response = self.client.get(reverse("player:openplays"), {"month": value})
            self.assertEqual(response.status_code, 200, value)
            self.assertTrue(response.context["calendar"]["is_current"], value)

    def test_navigation_links_point_at_adjacent_months(self):
        response = self.client.get(reverse("player:openplays"), {"month": "2026-01"})
        self.assertContains(response, 'href="?month=2025-12"')
        self.assertContains(response, 'href="?month=2026-02"')

    def test_another_players_registration_is_not_shown_as_yours(self):
        start = self.now + timedelta(days=1)
        theirs = self.session(start)
        Registration.objects.create(player=self.other, openplay=theirs)
        response = self.client.get(reverse("player:openplays"), {"month": self.month_param(start)})
        self.assertEqual(self.marks(response)[timezone.localtime(start).day], "session")


class AttendanceCalendarTests(PlayerUiTestCase):
    def test_marks_only_counted_attendance_in_the_displayed_month(self):
        counted = self.session(self.now - timedelta(days=3), status=OpenPlay.Status.COMPLETED)
        voided = self.session(self.now - timedelta(days=5), status=OpenPlay.Status.COMPLETED)
        cancelled = self.session(self.now - timedelta(days=7), status=OpenPlay.Status.CANCELLED)
        self.attend(self.player, counted)
        self.attend(self.player, voided, status=Attendance.Status.VOID, voided_at=self.now,
                    voided_by=self.user, void_reason="Mistake")
        self.attend(self.player, cancelled)
        self.attend(self.other, self.session(self.now - timedelta(days=9), status=OpenPlay.Status.COMPLETED))
        month = self.month_param(counted.starts_at)
        response = self.client.get(reverse("player:attendance"), {"month": month})
        in_month = [moment for moment in counted_openplay_starts(
            self.player, *month_bounds(parse_month(month, timezone.localdate())))]
        self.assertEqual(response.context["attended_in_month"], len(in_month))
        self.assertEqual(set(self.marks(response).values()), {"attended"})
        self.assertEqual(set(self.marks(response)), {timezone.localtime(moment).day for moment in in_month})
        self.assertEqual(response.context["attended_total"], openplays_attended(self.player))
        marked_days = set(self.marks(response))
        self.assertIn(timezone.localtime(counted.starts_at).day, marked_days)
        for excluded in (voided, cancelled):  # voided attendance and cancelled sessions never mark a day
            local = timezone.localtime(excluded.starts_at)
            if self.month_param(excluded.starts_at) == month:
                self.assertNotIn(local.day, marked_days)
        self.assertNotContains(response, "Private Member")

    def test_summary_total_matches_the_dashboard_count(self):
        for days in (2, 4, 6):
            self.attend(self.player, self.session(self.now - timedelta(days=days), status=OpenPlay.Status.COMPLETED))
        attendance = self.client.get(reverse("player:attendance"))
        home = self.client.get(reverse("player:home"))
        self.assertEqual(attendance.context["attended_total"], 3)
        self.assertEqual(attendance.context["attended_total"], home.context["summary"].openplays_attended)

    def test_invalid_month_never_errors(self):
        for value in ("nope", "2026-00", "9999-12"):
            self.assertEqual(self.client.get(reverse("player:attendance"), {"month": value}).status_code, 200)


class RewardsUpcomingTests(PlayerUiTestCase):
    def test_locked_milestones_show_progress_and_never_leak_other_players_data(self):
        for index in range(4):
            self.attend(self.player, self.session(self.now - timedelta(days=10 + index), status=OpenPlay.Status.COMPLETED))
        response = self.client.get(reverse("player:rewards"))
        self.assertContains(response, "Upcoming milestone rewards")
        self.assertContains(response, "6 OpenPlays to unlock")
        self.assertContains(response, "Locked")
        self.assertContains(response, "Next milestone")
        self.assertNotContains(response, "Private Member")
        self.assertEqual(response.context["upcoming"][0].milestone.threshold, 10)

    def test_no_upcoming_section_when_every_milestone_is_reached_or_held(self):
        LoyaltyMilestone.objects.update(is_active=False)
        response = self.client.get(reverse("player:rewards"))
        self.assertNotContains(response, "Upcoming milestone rewards")
        self.assertNotContains(response, "Next milestone")

    def test_rewards_stays_within_its_query_budget(self):
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(reverse("player:rewards")).status_code, 200)
        self.assertLessEqual(len(captured), 8)
