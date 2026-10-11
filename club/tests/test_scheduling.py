from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.permissions import ADMIN, ORGANIZER
from club.models import AuditEvent, OpenPlay, Venue
from club.services import create_venue


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class SchedulingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com", "Securepass123")
        self.venue = Venue.objects.create(name="Main Court")
        self.venue_url = reverse("organizer:venue_new")
        self.openplay_url = reverse("organizer:openplay_new")

    def post_openplay(self, starts, ends, venue=None, **extra):
        return self.client.post(self.openplay_url, {
            "venue": (venue or self.venue).pk, "name": "Test session", "starts_at": starts, "ends_at": ends,
        }, **extra)

    def test_anonymous_is_sent_to_login_and_nothing_is_created(self):
        for url in (self.venue_url, self.openplay_url):
            self.assertIn("/login/", self.client.get(url)["Location"])
            self.assertIn("/login/", self.client.post(url, {"name": "X"})["Location"])
        self.assertEqual(Venue.objects.count(), 1)
        self.assertFalse(OpenPlay.objects.exists())

    def test_player_is_forbidden_and_nothing_is_created(self):
        self.client.force_login(self.member)
        for url in (self.venue_url, self.openplay_url):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self.venue_url, {"name": "Hacked"}).status_code, 403)
        self.assertEqual(self.post_openplay("2026-12-01T18:00", "2026-12-01T20:00").status_code, 403)
        self.assertEqual(Venue.objects.count(), 1)
        self.assertFalse(OpenPlay.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action__in=["VENUE_CREATED", "OPENPLAY_CREATED"]).exists())

    def test_organizer_and_admin_can_open_both_forms(self):
        for user in (self.organizer, self.admin):
            self.client.force_login(user)
            self.assertContains(self.client.get(self.venue_url), "Venue name")
            self.assertContains(self.client.get(self.openplay_url), "Main Court")

    def test_get_never_mutates_and_csrf_is_enforced(self):
        self.client.force_login(self.organizer)
        self.client.get(self.venue_url)
        self.client.get(self.openplay_url)
        self.assertEqual(Venue.objects.count(), 1)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.organizer)
        self.assertEqual(strict.post(self.venue_url, {"name": "No token"}).status_code, 403)
        self.assertEqual(strict.post(self.openplay_url, {
            "venue": self.venue.pk, "starts_at": "2026-12-01T18:00", "ends_at": "2026-12-01T20:00",
        }).status_code, 403)
        self.assertEqual(Venue.objects.count(), 1)
        self.assertFalse(OpenPlay.objects.exists())

    def test_home_links_to_scheduling_forms(self):
        self.client.force_login(self.organizer)
        page = self.client.get(reverse("organizer:home"))
        self.assertContains(page, self.openplay_url)
        self.assertContains(page, self.venue_url)

    def test_venue_creation_is_audited_with_actor(self):
        self.client.force_login(self.organizer)
        response = self.client.post(self.venue_url, {"name": "  Side Court  "})
        self.assertRedirects(response, self.openplay_url)
        venue = Venue.objects.get(name="Side Court")
        event = AuditEvent.objects.get(action="VENUE_CREATED", target_id=venue.pk)
        self.assertEqual((event.actor, event.target_type), (self.organizer, "venue"))

    def test_blank_venue_name_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.venue_url, {"name": "   "})
        self.assertContains(response, "required", status_code=400)
        self.assertEqual(Venue.objects.count(), 1)

    @override_settings(TIME_ZONE="Asia/Manila")
    def test_openplay_times_are_interpreted_in_club_timezone(self):
        self.client.force_login(self.admin)
        response = self.post_openplay("2026-11-10T18:00", "2026-11-10T20:30")
        openplay = OpenPlay.objects.get()
        self.assertRedirects(response, reverse("organizer:session", args=[openplay.pk]))
        # Manila is UTC+8 with no DST.
        self.assertEqual(openplay.starts_at, utc(2026, 11, 10, 10, 0))
        self.assertEqual(openplay.ends_at, utc(2026, 11, 10, 12, 30))
        self.assertEqual(openplay.venue, self.venue)
        event = AuditEvent.objects.get(action="OPENPLAY_CREATED", target_id=openplay.pk)
        self.assertEqual(event.actor, self.admin)
        self.assertContains(self.client.get(self.openplay_url), "Asia/Manila")

    @override_settings(TIME_ZONE="Asia/Manila")
    def test_early_morning_manila_session_lands_on_the_local_organizer_day(self):
        # 06:00 Manila is 22:00 UTC the previous day; the organizer home must still list it today.
        self.client.force_login(self.organizer)
        from django.utils import timezone
        today = timezone.localdate()
        self.post_openplay(f"{today.isoformat()}T06:00", f"{today.isoformat()}T08:00")
        self.assertEqual(OpenPlay.objects.get().starts_at.hour, 22)
        self.assertContains(self.client.get(reverse("organizer:home")), "Main Court · 6:00 AM")

    def test_end_before_or_equal_to_start_is_rejected(self):
        self.client.force_login(self.organizer)
        for ends in ("2026-12-01T17:00", "2026-12-01T18:00"):
            response = self.post_openplay("2026-12-01T18:00", ends)
            self.assertContains(response, "The end must be after the start.", status_code=400)
        self.assertFalse(OpenPlay.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action="OPENPLAY_CREATED").exists())

    def test_inactive_venue_is_not_schedulable(self):
        self.venue.is_active = False
        self.venue.save()
        self.client.force_login(self.organizer)
        page = self.client.get(self.openplay_url)
        self.assertNotContains(page, "Main Court")
        self.assertContains(page, "No active venue yet")
        response = self.post_openplay("2026-12-01T18:00", "2026-12-01T20:00")
        self.assertContains(response, "Select a valid choice", status_code=400)
        self.assertFalse(OpenPlay.objects.exists())

    def test_service_validation_error_is_shown_not_raised(self):
        # Covers a venue deactivated between form validation and the service call.
        self.client.force_login(self.organizer)
        with patch("club.organizer_views.create_openplay",
                   side_effect=ValidationError("Cannot schedule at an inactive venue")):
            response = self.post_openplay("2026-12-01T18:00", "2026-12-01T20:00")
        self.assertContains(response, "Cannot schedule at an inactive venue", status_code=400)

    def test_malformed_or_missing_times_are_rejected(self):
        self.client.force_login(self.organizer)
        for starts, ends in (("not-a-date", "2026-12-01T20:00"), ("", ""), ("2026-12-01T18:00", "")):
            self.assertEqual(self.post_openplay(starts, ends).status_code, 400)
        self.assertFalse(OpenPlay.objects.exists())


@override_settings(TIME_ZONE="America/New_York")
class SchedulingDstTests(TestCase):
    def setUp(self):
        self.organizer = get_user_model().objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.venue = Venue.objects.create(name="DST Court")
        self.client.force_login(self.organizer)
        self.url = reverse("organizer:openplay_new")

    def post(self, starts, ends):
        return self.client.post(self.url, {"venue": self.venue.pk, "name": "DST test", "starts_at": starts, "ends_at": ends})

    def test_nonexistent_spring_forward_time_is_rejected(self):
        response = self.post("2026-03-08T02:30", "2026-03-08T04:00")
        self.assertContains(response, "ambiguous or it may not exist", status_code=400)
        self.assertFalse(OpenPlay.objects.exists())

    def test_ambiguous_fall_back_time_is_rejected(self):
        response = self.post("2026-11-01T01:30", "2026-11-01T03:00")
        self.assertContains(response, "ambiguous or it may not exist", status_code=400)
        self.assertFalse(OpenPlay.objects.exists())

    def test_session_spanning_fall_back_has_real_elapsed_duration(self):
        # 00:30 EDT (04:30 UTC) to 03:00 EST (08:00 UTC): 3.5 real hours across a 1:30 that repeats.
        self.post("2026-11-01T00:30", "2026-11-01T03:00")
        openplay = OpenPlay.objects.get()
        self.assertEqual((openplay.starts_at, openplay.ends_at), (utc(2026, 11, 1, 4, 30), utc(2026, 11, 1, 8, 0)))

    def test_session_spanning_spring_forward_has_real_elapsed_duration(self):
        # 01:30 EST (06:30 UTC) to 03:30 EDT (07:30 UTC): one real hour.
        self.post("2026-03-08T01:30", "2026-03-08T03:30")
        openplay = OpenPlay.objects.get()
        self.assertEqual((openplay.starts_at, openplay.ends_at), (utc(2026, 3, 8, 6, 30), utc(2026, 3, 8, 7, 30)))

    def test_end_before_start_is_rejected_across_fall_back(self):
        self.assertContains(self.post("2026-11-01T03:00", "2026-11-01T00:30"),
                            "The end must be after the start.", status_code=400)
        self.assertFalse(OpenPlay.objects.exists())


class VenueUniquenessTests(TestCase):
    def setUp(self):
        self.organizer = get_user_model().objects.create_user("organizer@example.com", "Securepass123")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.venue = create_venue(name="Main Court", actor=self.organizer)
        self.url = reverse("organizer:venue_new")

    def assert_single_venue(self):
        self.assertEqual(Venue.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="VENUE_CREATED").count(), 1)

    def test_service_rejects_case_and_whitespace_duplicates_without_a_row_or_audit(self):
        for name in ("main court", " Main Court ", "MAIN COURT"):
            with self.assertRaisesMessage(ValidationError, "A venue with this name already exists."):
                create_venue(name=name, actor=self.organizer)
        self.assert_single_venue()

    def test_database_rejects_duplicate_when_validation_is_bypassed(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Venue.objects.create(name="MAIN court")
        self.assertEqual(Venue.objects.count(), 1)

    def test_distinct_names_are_still_allowed(self):
        create_venue(name="Side Court", actor=self.organizer)
        self.assertEqual(Venue.objects.count(), 2)

    def test_post_shows_friendly_error_for_duplicate(self):
        self.client.force_login(self.organizer)
        for name in ("main court", " Main Court "):
            response = self.client.post(self.url, {"name": name})
            self.assertContains(response, "A venue with this name already exists.", status_code=400)
        self.assert_single_venue()

    def test_simultaneous_submit_integrity_error_becomes_form_error_not_500(self):
        self.client.force_login(self.organizer)
        with patch("club.organizer_views.create_venue", side_effect=IntegrityError("duplicate key")):
            response = self.client.post(self.url, {"name": "Main Court"})
        self.assertContains(response, "A venue with this name already exists.", status_code=400)
        self.assert_single_venue()
