from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ORGANIZER
from club.models import Attendance, OpenPlay, Player, Venue
from club.services import find_players, openplays_attended


class PlayerListTests(TestCase):
    """The Players page lists the club's players without a search, and filters by signed-up or walk-in."""

    def setUp(self):
        self.User = get_user_model()
        self.organizer = self.User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.url = reverse("organizer:players")
        self.venue = Venue.objects.create(name="Main Court")
        self.now = timezone.now()

    def member(self, name, reclub_name=""):
        user = self.User.objects.create_user(f"{name.lower().replace(' ', '.')}@example.com")
        return Player.objects.create(user=user, display_name=name, reclub_name=reclub_name)

    def walk_in(self, name, phone="0917-000-0001"):
        return Player.objects.create(display_name=name, phone=phone)

    def session(self, days_ago, status=OpenPlay.Status.COMPLETED):
        start = self.now - timedelta(days=days_ago)
        end = start + timedelta(hours=2)
        state = {"completed_at": end} if status == OpenPlay.Status.COMPLETED else {
            "cancelled_at": end, "cancellation_reason": "Rain"}
        return OpenPlay.objects.create(venue=self.venue, starts_at=start, ends_at=end, status=status, **state)

    def attend(self, player, openplay, status=Attendance.Status.VALID):
        voided = {}
        if status == Attendance.Status.VOID:
            voided = {"voided_at": self.now, "voided_by": self.organizer, "void_reason": "Test correction"}
        return Attendance.objects.create(
            player=player, openplay=openplay, status=status, source=Attendance.Source.HISTORICAL,
            recorded_by=self.organizer, checked_in_at=openplay.starts_at, **voided,
        )

    def get(self, **params):
        self.client.force_login(self.organizer)
        return self.client.get(self.url + (f"?{urlencode(params)}" if params else ""))

    def test_the_page_lists_players_before_any_search(self):
        self.member("Maya Santos")
        self.walk_in("Tomas Reyes")
        page = self.get()
        self.assertContains(page, "Maya Santos")
        self.assertContains(page, "Tomas Reyes")

    def test_only_organizers_can_open_it(self):
        member = self.member("Maya Santos")
        self.client.force_login(member.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_tabs_split_signed_up_players_from_walk_ins_and_show_counts(self):
        self.member("Maya Santos")
        self.member("Rico Cruz")
        self.walk_in("Tomas Reyes")
        everyone = self.get()
        self.assertContains(everyone, "All (3)")
        self.assertContains(everyone, "Signed up (2)")
        self.assertContains(everyone, "Walk-ins (1)")
        signed_up = self.get(status="signed_up")
        self.assertContains(signed_up, "Maya Santos")
        self.assertNotContains(signed_up, "Tomas Reyes")
        walk_ins = self.get(status="walk_in")
        self.assertContains(walk_ins, "Tomas Reyes")
        self.assertNotContains(walk_ins, "Maya Santos")

    def test_an_unknown_tab_falls_back_to_everyone(self):
        self.member("Maya Santos")
        self.walk_in("Tomas Reyes")
        page = self.get(status="nonsense")
        self.assertContains(page, "Maya Santos")
        self.assertContains(page, "Tomas Reyes")

    def test_the_attended_count_matches_the_loyalty_definition(self):
        player = self.member("Maya Santos")
        self.attend(player, self.session(10))
        self.attend(player, self.session(9), status=Attendance.Status.VOID)
        self.attend(player, self.session(8, status=OpenPlay.Status.CANCELLED))
        self.attend(player, self.session(7))
        row = next(p for p in self.get().context["page"] if p.pk == player.pk)
        self.assertEqual(row.attended, 2)
        self.assertEqual(row.attended, openplays_attended(player))

    def test_a_player_with_no_attendance_shows_zero(self):
        player = self.walk_in("Tomas Reyes")
        row = next(p for p in self.get().context["page"] if p.pk == player.pk)
        self.assertEqual(row.attended, 0)

    def test_search_finds_a_player_by_reclub_name(self):
        self.member("Maya Santos", reclub_name="mayadinks")
        self.member("Rico Cruz", reclub_name="ricoR")
        found = self.get(q="mayadinks")
        self.assertContains(found, "Maya Santos")
        self.assertNotContains(found, "Rico Cruz")
        # the session roster search shares find_players, so it finds by ReClub name too
        self.assertEqual(find_players(term="mayadinks", actor=self.organizer).count(), 1)

    def test_search_works_inside_a_tab(self):
        self.member("Maya Santos")
        self.walk_in("Maya Walkin")
        page = self.get(q="maya", status="walk_in")
        self.assertContains(page, "Maya Walkin")
        self.assertNotContains(page, "Maya Santos")

    def test_a_one_character_search_is_ignored_rather_than_shown_as_results(self):
        self.member("Maya Santos")
        self.walk_in("Tomas Reyes")
        page = self.get(q="a")
        self.assertContains(page, "Everyone")
        self.assertNotContains(page, "Results")
        self.assertContains(page, "All (2)")

    def test_a_search_with_no_match_says_so(self):
        self.member("Maya Santos")
        self.assertContains(self.get(q="zzzz"), "No matching players")

    def test_pages_hold_25_players_and_links_keep_the_filter_and_search(self):
        for number in range(30):
            self.member(f"Player {number:02d}")
        first = self.get(status="signed_up", q="player")
        self.assertEqual(len(first.context["page"]), 25)
        self.assertContains(first, "Page 1 of 2")
        self.assertContains(first, "status=signed_up")
        self.assertContains(first, "q=player")
        self.assertContains(first, "page=2")
        second = self.get(status="signed_up", q="player", page=2)
        self.assertEqual(len(second.context["page"]), 5)
        beyond = self.get(status="signed_up", q="player", page=999)
        self.assertEqual(beyond.status_code, 200)
        self.assertEqual(len(beyond.context["page"]), 5)

    def test_the_number_of_queries_does_not_grow_with_the_number_of_players(self):
        self.client.force_login(self.organizer)

        def queries():
            self.client.get(self.url)  # warm the session so only the page's own queries are counted
            with CaptureQueriesContext(connection) as captured:
                self.client.get(self.url)
            return len(captured)

        for number in range(3):
            player = self.member(f"First {number}")
            self.attend(player, self.session(number + 1))
        few = queries()
        for number in range(20):
            player = self.member(f"Second {number:02d}")
            self.attend(player, self.session(number + 5))
        self.assertEqual(queries(), few)
