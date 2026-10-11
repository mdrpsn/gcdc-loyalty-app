from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ORGANIZER
from club.models import OpenPlay, Player, Venue
from club.services import create_openplay


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class DurationLabelTests(SimpleTestCase):
    def label(self, minutes):
        start = utc(2026, 12, 1, 8, 0)
        return OpenPlay(starts_at=start, ends_at=start + timedelta(minutes=minutes)).duration_label

    def test_whole_hours_use_singular_and_plural(self):
        self.assertEqual(self.label(60), "1 hr")
        self.assertEqual(self.label(120), "2 hrs")
        self.assertEqual(self.label(240), "4 hrs")

    def test_part_hours_show_the_minutes(self):
        self.assertEqual(self.label(90), "1 hr 30 min")
        self.assertEqual(self.label(150), "2 hrs 30 min")
        self.assertEqual(self.label(45), "45 min")

    def test_seconds_are_ignored(self):
        start = utc(2026, 12, 1, 8, 0)
        self.assertEqual(OpenPlay(starts_at=start, ends_at=start + timedelta(hours=2, seconds=40)).duration_label, "2 hrs")


class SessionHoursOnScreensTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        Player.objects.create(user=self.member, display_name="Maya Santos", phone="111")
        self.venue = Venue.objects.create(name="Pickle Star")
        noon = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        self.today = create_openplay(venue_id=self.venue.pk, starts_at=noon, ends_at=noon + timedelta(hours=2),
                                     actor=self.organizer, name="Today session")
        later = noon + timedelta(days=2)
        self.upcoming = create_openplay(venue_id=self.venue.pk, starts_at=later, ends_at=later + timedelta(hours=4),
                                        actor=self.organizer, name="Upcoming session")

    def test_organizer_home_shows_the_length_on_today_and_upcoming_cards(self):
        self.client.force_login(self.organizer)
        html = self.client.get(reverse("organizer:home")).content.decode()
        today_card = html[html.index("Today session"):html.index("Coming up this week")]
        upcoming_card = html[html.index("Upcoming session"):]
        self.assertIn("2 hrs", today_card)
        self.assertIn("4 hrs", upcoming_card)

    def test_session_page_shows_the_length(self):
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:session", args=[self.upcoming.pk]))
        self.assertContains(page, "4 hrs")

    def test_players_see_the_length_on_upcoming_openplays(self):
        self.client.force_login(self.member)
        html = self.client.get(reverse("player:openplays")).content.decode()
        self.assertIn("2 hrs", html[html.index("Today session"):html.index("Upcoming session")])
        self.assertIn("4 hrs", html[html.index("Upcoming session"):])
