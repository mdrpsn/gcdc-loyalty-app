from datetime import timedelta
import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from club.models import Attendance, AttendanceSubmission, AuditEvent, OpenPlay, Player, Venue
from club.services import (
    create_member_account, openplays_attended, review_attendance_submission,
    submit_member_attendance, submit_staff_attendance,
)


class AttendanceSubmissionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.member = User.objects.create_user("member@example.com", "StrongPass123!")
        self.other = User.objects.create_user("other@example.com", "StrongPass123!")
        self.organizer = User.objects.create_user("organizer@example.com", "StrongPass123!")
        self.organizer.groups.add(Group.objects.get(name="ORGANIZER"))
        self.admin = User.objects.create_user("admin@example.com", "StrongPass123!")
        self.admin.groups.add(Group.objects.get(name="ADMIN"))
        self.player = Player.objects.create(
            user=self.member, display_name="Maya Santos", phone="09175550101",
            reclub_name="Maya on ReClub", reclub_profile_url="https://example.com/reclub/maya",
        )
        self.venue = Venue.objects.create(name="Court Green")
        now = timezone.now()
        self.session = OpenPlay.objects.create(
            venue=self.venue, starts_at=now - timedelta(hours=3), ends_at=now - timedelta(hours=1),
            status=OpenPlay.Status.COMPLETED, completed_at=now - timedelta(hours=1),
        )

    def test_member_submission_is_pending_until_admin_approves(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("player:attendance_submit"), {"openplay": self.session.pk})
        self.assertRedirects(response, reverse("player:attendance"))
        submission = AttendanceSubmission.objects.get(player=self.player, openplay=self.session)
        self.assertEqual(submission.status, AttendanceSubmission.Status.PENDING)
        self.assertEqual(submission.reclub_name, self.player.reclub_name)
        self.assertEqual(submission.reclub_profile_url, self.player.reclub_profile_url)
        self.assertEqual(openplays_attended(self.player), 0)
        self.assertFalse(Attendance.objects.filter(player=self.player, openplay=self.session).exists())
        self.assertContains(self.client.get(reverse("player:attendance")), "Pending approval")

        self.client.force_login(self.admin)
        dashboard = reverse("organizer:attendance_submissions")
        self.assertContains(self.client.get(dashboard), "Maya Santos")
        review = reverse("organizer:attendance_submission_review", args=[submission.pk])
        self.assertRedirects(self.client.post(review, {"action": "approve"}), f"{dashboard}?status=pending")
        submission.refresh_from_db()
        self.assertEqual(submission.status, AttendanceSubmission.Status.APPROVED)
        self.assertEqual(submission.attendance.source, Attendance.Source.CLAIM)
        self.assertEqual(openplays_attended(self.player), 1)
        self.assertContains(self.client.get(f"{dashboard}?status=approved"), "Maya Santos")
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_APPROVED", target_id=submission.pk).exists())

    def test_decline_requires_reason_and_member_can_resubmit(self):
        submission = submit_member_attendance(openplay_id=self.session.pk, actor=self.member)
        with self.assertRaises(ValidationError):
            review_attendance_submission(submission_id=submission.pk, actor=self.admin, approve=False)
        review_attendance_submission(submission_id=submission.pk, actor=self.admin,
                                     approve=False, reason="No attendance on roster")
        self.assertEqual(openplays_attended(self.player), 0)
        submission.refresh_from_db()
        self.assertEqual(submission.status, AttendanceSubmission.Status.DECLINED)
        self.assertEqual(submission.decline_reason, "No attendance on roster")
        again = submit_member_attendance(openplay_id=self.session.pk, actor=self.member)
        self.assertEqual(again.pk, submission.pk)
        self.assertEqual(again.status, AttendanceSubmission.Status.PENDING)
        self.assertEqual(again.decline_reason, "")

    def test_member_cannot_approve_and_cannot_submit_future_session(self):
        submission = submit_member_attendance(openplay_id=self.session.pk, actor=self.member)
        with self.assertRaises(PermissionDenied):
            review_attendance_submission(submission_id=submission.pk, actor=self.organizer, approve=True)
        future = OpenPlay.objects.create(
            venue=self.venue, starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
        )
        with self.assertRaises(ValidationError):
            submit_member_attendance(openplay_id=future.pk, actor=self.member)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("organizer:attendance_submissions")).status_code, 403)

    def test_duplicate_submission_does_not_create_duplicate_attendance(self):
        first = submit_member_attendance(openplay_id=self.session.pk, actor=self.member)
        second = submit_member_attendance(openplay_id=self.session.pk, actor=self.member)
        self.assertEqual(first.pk, second.pk)
        review_attendance_submission(submission_id=first.pk, actor=self.admin, approve=True)
        with self.assertRaises(ValidationError):
            submit_member_attendance(openplay_id=self.session.pk, actor=self.member)
        self.assertEqual(Attendance.objects.filter(player=self.player, openplay=self.session).count(), 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MemberSignupTests(TestCase):
    def _verification_path(self, email="MAYA@example.com"):
        response = self.client.post(reverse("signup"), {"email": email})
        self.assertRedirects(response, reverse("signup_done"))
        self.assertEqual(len(mail.outbox), 1)
        return re.search(r"https?://testserver(/signup/verify/[^\s]+)", mail.outbox[0].body).group(1)

    def test_service_rejects_missing_or_invalid_identity_details(self):
        details = {
            "email": "maya@example.com", "password": "StrongPass123!",
            "full_name": "Maya Santos", "phone": "09175550101",
            "reclub_name": "Maya on ReClub",
            "reclub_profile_url": "https://example.com/reclub/maya",
        }
        for changes in ({"phone": " "}, {"reclub_profile_url": "not-a-link"},
                        {"email": "invalid-email"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                create_member_account(**{**details, **changes})
        self.assertFalse(get_user_model().objects.filter(email="maya@example.com").exists())

    def test_signup_creates_account_and_player_with_reclub_details(self):
        path = self._verification_path()
        self.assertFalse(get_user_model().objects.filter(email="maya@example.com").exists())
        response = self.client.post(path, {
            "full_name": "Maya Santos", "phone": "09175550101",
            "reclub_name": "Maya on ReClub", "reclub_profile_url": "https://example.com/reclub/maya",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        self.assertRedirects(response, reverse("player:home"))
        user = get_user_model().objects.get(email="maya@example.com")
        self.assertTrue(user.check_password("StrongPass123!"))
        self.assertEqual(user.player.display_name, "Maya Santos")
        self.assertEqual(user.player.phone, "09175550101")
        self.assertEqual(user.player.reclub_profile_url, "https://example.com/reclub/maya")
        self.assertTrue(AuditEvent.objects.filter(action="MEMBER_SIGNED_UP", target_id=user.pk).exists())

    def test_signup_requires_all_profile_details_and_does_not_claim_walk_in(self):
        path = self._verification_path()
        walk_in = Player.objects.create(
            display_name="Maya Santos", phone="09175550101",
            reclub_name="Maya on ReClub", reclub_profile_url="https://example.com/reclub/maya",
        )
        data = {
            "full_name": "Maya Santos", "phone": "09175550101",
            "reclub_name": "Maya on ReClub", "reclub_profile_url": walk_in.reclub_profile_url,
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        }
        response = self.client.post(path, data)
        self.assertContains(response, "already in club records")
        self.assertFalse(get_user_model().objects.filter(email="maya@example.com").exists())
        data["reclub_profile_url"] = ""
        self.assertContains(self.client.post(path, data), "This field is required")


class SignupRequiredAttendanceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organizer = User.objects.create_user("organizer@example.com", "StrongPass123!")
        self.organizer.groups.add(Group.objects.get(name="ORGANIZER"))
        self.admin = User.objects.create_user("admin@example.com", "StrongPass123!")
        self.admin.groups.add(Group.objects.get(name="ADMIN"))
        self.venue = Venue.objects.create(name="Riverside Courts")
        now = timezone.now()
        self.session = OpenPlay.objects.create(
            venue=self.venue, starts_at=now - timedelta(hours=4), ends_at=now - timedelta(hours=2),
            status=OpenPlay.Status.COMPLETED, completed_at=now - timedelta(hours=2),
        )
        self.unlinked = Player.objects.create(
            display_name="Alex Cruz", contact_email="alex@example.com", phone="09175550103",
            reclub_name="Alex on ReClub", reclub_profile_url="https://example.com/reclub/alex",
        )

    def test_anonymous_player_must_sign_up_before_submitting(self):
        response = self.client.get(reverse("player:attendance_submit"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('player:attendance_submit')}")
        self.assertEqual(self.client.post("/player/attendance/guest/", {"openplay": self.session.pk}).status_code, 404)
        self.assertFalse(AttendanceSubmission.objects.exists())

    def test_staff_cannot_submit_for_unlinked_player(self):
        with self.assertRaisesMessage(ValidationError, "sign up"):
            submit_staff_attendance(
                openplay_id=self.session.pk, player_id=self.unlinked.pk, actor=self.organizer,
            )
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("organizer:check_in", args=[self.session.pk, self.unlinked.pk]),
                                    follow=True)
        self.assertContains(response, "must sign up")
        self.assertFalse(AttendanceSubmission.objects.exists())

    def test_admin_cannot_approve_old_unlinked_submission(self):
        submission = AttendanceSubmission.objects.create(
            player=self.unlinked, openplay=self.session,
            claimed_full_name=self.unlinked.display_name,
            claimed_email="alex@example.com", claimed_phone=self.unlinked.phone,
            reclub_name=self.unlinked.reclub_name,
            reclub_profile_url=self.unlinked.reclub_profile_url,
        )
        with self.assertRaisesMessage(ValidationError, "signed-up account"):
            review_attendance_submission(submission_id=submission.pk, actor=self.admin, approve=True)
        self.assertFalse(Attendance.objects.exists())
        self.client.force_login(self.admin)
        response = self.client.get(reverse("organizer:attendance_submissions"))
        self.assertContains(response, "linked account before attendance can be approved")
        self.assertNotContains(response, '<button class="button primary" type="submit">Approve</button>')
