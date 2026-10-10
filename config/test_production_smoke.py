"""HTTPS/CSRF application smoke, runnable with production settings and isolated PostgreSQL."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.contrib.staticfiles.storage import staticfiles_storage
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER
from club.models import Attendance, AttendanceSubmission, OpenPlay, Player, RewardIssuance, RewardRedemption, Venue


class ProductionWorkflowSmokeTests(TestCase):
    def setUp(self):
        self.host = settings.ALLOWED_HOSTS[0]
        self.member_client = Client(enforce_csrf_checks=True)
        self.staff_client = Client(enforce_csrf_checks=True)
        self.admin_client = Client(enforce_csrf_checks=True)
        User = get_user_model()
        self.member = User.objects.create_user("smoke-member@example.invalid", "Securepass123")
        self.staff = User.objects.create_user("smoke-staff@example.invalid", "Securepass123")
        self.staff.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("smoke-admin@example.invalid", "Securepass123")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.player = Player.objects.create(
            user=self.member, display_name="Smoke Member", phone="09175550101",
            reclub_name="Smoke ReClub", reclub_profile_url="https://example.com/reclub/smoke",
        )
        venue = Venue.objects.create(name="Smoke Court")
        now = timezone.now()
        for days_ago in range(2, 11):
            start = now - timedelta(days=days_ago)
            session = OpenPlay.objects.create(
                venue=venue, starts_at=start, ends_at=start + timedelta(hours=2),
                status=OpenPlay.Status.COMPLETED, completed_at=start + timedelta(hours=2),
            )
            Attendance.objects.create(
                player=self.player, openplay=session,
                source=Attendance.Source.HISTORICAL, recorded_by=self.staff,
            )
        self.check_in_session = OpenPlay.objects.create(
            venue=venue, starts_at=now - timedelta(minutes=10), ends_at=now + timedelta(hours=1),
        )
        self.completion_session = OpenPlay.objects.create(
            venue=venue, starts_at=now - timedelta(hours=3), ends_at=now - timedelta(hours=1),
        )
        self.reward_target = OpenPlay.objects.create(
            venue=venue, starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, hours=2),
        )

    def get(self, client, url):
        return client.get(url, secure=True, HTTP_HOST=self.host)

    def post(self, client, url, data=None):
        payload = {"csrfmiddlewaretoken": client.cookies["csrftoken"].value, **(data or {})}
        return client.post(
            url, payload, secure=True, HTTP_HOST=self.host,
            HTTP_REFERER=f"https://{self.host}{url}",
        )

    def login(self, client, email):
        self.assertEqual(self.get(client, reverse("login")).status_code, 200)
        response = self.post(client, reverse("login"), {"username": email, "password": "Securepass123"})
        self.assertEqual(response.status_code, 302)

    def test_private_http_health_probe_reaches_database_check(self):
        if "whitenoise.middleware.WhiteNoiseMiddleware" not in settings.MIDDLEWARE:
            self.skipTest("Production HTTPS settings required")
        self.assertEqual(
            self.member_client.get(reverse("health"), HTTP_HOST=self.host).status_code, 200,
        )
        with patch("config.views.connection.cursor", side_effect=OperationalError):
            response = self.member_client.get(reverse("health"), HTTP_HOST=self.host)
        self.assertEqual(response.status_code, 503)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_https_csrf_email_first_signup(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.get(client, reverse("signup")).status_code, 200)
        response = self.post(client, reverse("signup"), {"email": "NEW-SMOKE@example.invalid"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"https://{self.host}/signup/verify/", mail.outbox[0].body)
        path = re.search(r"https://[^/]+(/signup/verify/[^\s]+)", mail.outbox[0].body).group(1)
        self.assertFalse(get_user_model().objects.filter(email="new-smoke@example.invalid").exists())
        self.assertEqual(self.get(client, path).status_code, 200)
        response = self.post(client, path, {
            "full_name": "New Smoke", "phone": "09175550109",
            "reclub_name": "New Smoke on ReClub",
            "reclub_profile_url": "https://example.com/reclub/new-smoke",
            "password1": "Securepass123", "password2": "Securepass123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("player:home"))
        self.assertTrue(get_user_model().objects.filter(email="new-smoke@example.invalid").exists())
        self.assertEqual(self.get(client, reverse("player:attendance_submit")).status_code, 200)

    def test_https_csrf_club_lifecycle_and_static_collection(self):
        self.assertEqual(self.get(self.member_client, reverse("health")).status_code, 200)
        if settings.SECURE_SSL_REDIRECT:
            # Render's internal HTTP probe must reach the DB check, not pass via 301.
            self.assertEqual(
                self.member_client.get(reverse("health"), HTTP_HOST=self.host).status_code, 200,
            )
            response = self.member_client.get(reverse("login"), HTTP_HOST=self.host)
            self.assertEqual(response.status_code, 301)
            self.assertTrue(response["Location"].startswith("https://"))
        self.login(self.member_client, self.member.email)
        self.login(self.staff_client, self.staff.email)
        self.login(self.admin_client, self.admin.email)

        self.assertEqual(self.get(self.member_client, reverse("player:home")).status_code, 200)
        self.assertEqual(self.get(self.staff_client, reverse("organizer:home")).status_code, 200)
        self.assertEqual(self.get(
            self.staff_client, reverse("organizer:session", args=[self.check_in_session.pk]),
        ).status_code, 200)
        registration_url = reverse("player:register", args=[self.reward_target.pk])
        self.assertEqual(self.post(self.member_client, registration_url).status_code, 302)
        check_in_url = reverse("organizer:check_in", args=[self.check_in_session.pk, self.player.pk])
        self.assertEqual(self.post(self.staff_client, check_in_url).status_code, 302)
        self.assertEqual(Attendance.objects.filter(player=self.player, openplay=self.check_in_session).count(), 0)
        submission = AttendanceSubmission.objects.get(player=self.player, openplay=self.check_in_session)
        self.assertEqual(self.get(self.admin_client, reverse("organizer:attendance_submissions")).status_code, 200)
        approval_url = reverse("organizer:attendance_submission_review", args=[submission.pk])
        self.assertEqual(self.post(self.admin_client, approval_url, {"action": "approve"}).status_code, 302)
        self.assertEqual(Attendance.objects.filter(player=self.player, openplay=self.check_in_session).count(), 1)
        self.assertFalse(RewardIssuance.objects.filter(player=self.player).exists())

        Attendance.objects.create(
            player=self.player, openplay=self.completion_session,
            source=Attendance.Source.HISTORICAL, recorded_by=self.staff,
        )
        complete_url = reverse("organizer:complete", args=[self.completion_session.pk])
        self.assertEqual(self.get(self.staff_client, complete_url).status_code, 200)
        self.assertEqual(self.post(self.staff_client, complete_url).status_code, 302)
        issuance = RewardIssuance.objects.get(player=self.player, milestone__threshold=10)
        self.assertEqual(issuance.status, RewardIssuance.Status.AVAILABLE)
        self.assertContains(self.get(self.member_client, reverse("player:rewards")), "Dinker")
        redeem_url = reverse("organizer:redeem", args=[self.player.pk, issuance.pk])
        self.assertEqual(self.get(self.staff_client, redeem_url).status_code, 200)
        self.assertEqual(self.post(self.staff_client, redeem_url, {
            "openplay": self.reward_target.pk,
        }).status_code, 302)
        self.assertEqual(RewardRedemption.objects.filter(issuance=issuance).count(), 1)
        self.assertEqual(self.post(self.member_client, reverse("logout")).status_code, 302)
        self.assertEqual(self.post(self.staff_client, reverse("logout")).status_code, 302)
        self.assertEqual(self.post(self.admin_client, reverse("logout")).status_code, 302)

        with TemporaryDirectory() as directory, override_settings(STATIC_ROOT=Path(directory)):
            call_command("collectstatic", interactive=False, verbosity=0)
            self.assertTrue((Path(directory) / "club/css/player.css").is_file())
            self.assertTrue((Path(directory) / "club/css/organizer.css").is_file())
            if "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE:
                static_url = staticfiles_storage.url("club/css/player.css")
                response = Client().get(static_url, secure=True, HTTP_HOST=self.host)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"font", b"".join(response.streaming_content).lower())
