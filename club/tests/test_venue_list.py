from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ORGANIZER
from club.models import OpenPlay, Venue
from club.services import cancel_openplay, create_openplay


class VenueListTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.url = reverse("organizer:venue_new")
        self.start = timezone.now() + timedelta(days=2)

    def schedule(self, venue, days=0):
        return create_openplay(
            venue_id=venue.pk, starts_at=self.start + timedelta(days=days),
            ends_at=self.start + timedelta(days=days, hours=2), actor=self.organizer,
        )

    def test_the_add_venue_page_says_so_when_there_are_no_venues_yet(self):
        self.client.force_login(self.organizer)
        page = self.client.get(self.url)
        self.assertContains(page, "Venues")
        self.assertContains(page, "No venues yet")

    def test_existing_venues_are_listed_alphabetically_with_their_session_counts(self):
        pickle = Venue.objects.create(name="Pickle Star")
        arena = Venue.objects.create(name="Arena Court")
        self.schedule(pickle, 0)
        self.schedule(pickle, 1)
        cancelled = self.schedule(pickle, 2)
        cancel_openplay(openplay_id=cancelled.pk, actor=self.organizer, reason="Rain")
        self.client.force_login(self.organizer)
        page = self.client.get(self.url)
        html = page.content.decode()
        self.assertLess(html.index("Arena Court"), html.index("Pickle Star"))
        self.assertContains(page, "2 OpenPlays")  # the cancelled one is not counted
        self.assertContains(page, "No OpenPlays yet")  # Arena Court has none
        self.assertNotContains(page, "No venues yet")

    def test_an_inactive_venue_is_listed_and_marked_because_its_name_is_still_taken(self):
        Venue.objects.create(name="Old Gym", is_active=False)
        self.client.force_login(self.organizer)
        page = self.client.get(self.url)
        self.assertContains(page, "Old Gym")
        self.assertContains(page, "Inactive")

    def test_the_list_is_still_shown_when_a_duplicate_name_is_rejected(self):
        Venue.objects.create(name="Pickle Star")
        self.client.force_login(self.organizer)
        response = self.client.post(self.url, {"name": "pickle star"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "already exists", status_code=400)
        self.assertContains(response, "Pickle Star", status_code=400)
        self.assertEqual(Venue.objects.count(), 1)

    def test_the_venue_list_only_appears_on_the_add_venue_page(self):
        Venue.objects.create(name="Pickle Star")
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:openplay_new"))
        self.assertNotContains(page, "No OpenPlays yet")
        self.assertFalse(OpenPlay.objects.exists())
