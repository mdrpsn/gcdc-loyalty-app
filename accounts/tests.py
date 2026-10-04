from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
import re
from datetime import datetime, timedelta
from unittest.mock import patch

from club.models import AuditEvent

from .forms import UserCreationForm
from .models import SignupEmailChallenge, SignupRateLimit


User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailFirstSignupTests(TestCase):
    def _request_link(self, email="new@example.com"):
        response = self.client.post(reverse("signup"), {"email": email})
        self.assertRedirects(response, reverse("signup_done"))
        return re.search(r"https?://testserver(/signup/verify/[^\s]+)", mail.outbox[-1].body).group(1)

    def test_email_must_be_verified_before_account_or_attendance_access(self):
        path = self._request_link()
        self.assertFalse(User.objects.filter(email="new@example.com").exists())
        self.assertEqual(self.client.get(reverse("player:attendance_submit")).status_code, 302)
        response = self.client.get(path)
        self.assertContains(response, "Verified email: <strong>new@example.com</strong>")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        response = self.client.post(path, {
            "email": "forged@example.com", "full_name": "New Player", "phone": "09175550101",
            "reclub_name": "New on ReClub", "reclub_profile_url": "https://example.com/reclub/new",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        self.assertRedirects(response, reverse("player:home"))
        self.assertTrue(User.objects.filter(email="new@example.com").exists())
        self.assertFalse(User.objects.filter(email="forged@example.com").exists())
        self.client.logout()
        self.assertEqual(self.client.get(path).status_code, 400)

    def test_signup_rejects_password_similar_to_verified_email(self):
        path = self._request_link()
        response = self.client.post(path, {
            "full_name": "New Player", "phone": "09175550101",
            "reclub_name": "New on ReClub", "reclub_profile_url": "https://example.com/reclub/new",
            "password1": "new@example.com", "password2": "new@example.com",
        })
        self.assertContains(response, "too similar")
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_signup_accepts_six_character_alphanumeric_password(self):
        path = self._request_link()
        response = self.client.post(path, {
            "full_name": "New Player", "phone": "09175550101",
            "reclub_name": "New on ReClub", "reclub_profile_url": "https://example.com/reclub/new",
            "password1": "Qz7mP2", "password2": "Qz7mP2",
        })
        self.assertRedirects(response, reverse("player:home"))
        self.assertTrue(User.objects.get(email="new@example.com").check_password("Qz7mP2"))

    def test_signup_requires_six_alphanumeric_characters_with_letters_and_numbers(self):
        path = self._request_link()
        for password in ("Abc12", "abcdef", "123456", "Ab1!!!"):
            with self.subTest(password=password):
                response = self.client.post(path, {
                    "full_name": "New Player", "phone": "09175550101",
                    "reclub_name": "New on ReClub", "reclub_profile_url": "https://example.com/reclub/new",
                    "password1": password, "password2": password,
                })
                self.assertContains(response, "Password must contain at least 6 letters and numbers")
                self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_resend_replaces_old_link_and_expiry_blocks_signup(self):
        first = self._request_link()
        self.client.post(reverse("signup"), {"email": "new@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        challenge = SignupEmailChallenge.objects.get(email="new@example.com")
        challenge.last_sent_at -= timedelta(minutes=3)
        challenge.save(update_fields=["last_sent_at"])
        second = self._request_link()
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get(first).status_code, 400)
        challenge.refresh_from_db()
        challenge.expires_at = challenge.last_sent_at - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])
        self.assertEqual(self.client.get(second).status_code, 400)
        self.assertEqual(self.client.post(second, {"full_name": "New Player"}).status_code, 400)
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_email_send_limit_blocks_repeated_resends(self):
        self._request_link()
        for _ in range(5):
            challenge = SignupEmailChallenge.objects.get(email="new@example.com")
            challenge.last_sent_at -= timedelta(minutes=3)
            challenge.save(update_fields=["last_sent_at"])
            self.client.post(reverse("signup"), {"email": "new@example.com"})
        self.assertEqual(len(mail.outbox), 5)
        self.assertEqual(SignupEmailChallenge.objects.get(email="new@example.com").sends_in_window, 5)

    def test_existing_email_has_generic_response_and_reset_route(self):
        User.objects.create_user("new@example.com", "StrongPass123!")
        response = self.client.post(reverse("signup"), {"email": "NEW@example.com"}, follow=True)
        self.assertContains(response, "If that address can be used for a new account")
        self.assertContains(response, reverse("password_reset"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(SignupEmailChallenge.objects.exists())

    def test_email_send_failure_does_not_leave_pending_link(self):
        with patch("accounts.signup.send_mail", side_effect=OSError("SMTP unavailable")):
            with self.assertRaises(OSError):
                self.client.post(reverse("signup"), {"email": "new@example.com"})
        self.assertFalse(SignupEmailChallenge.objects.exists())
        self.assertFalse(SignupRateLimit.objects.exists())

    def test_ip_send_limit_covers_distinct_addresses(self):
        for index in range(101):
            self.client.post(reverse("signup"), {"email": f"new{index}@example.com"})
        self.assertEqual(len(mail.outbox), 100)
        self.assertEqual(SignupEmailChallenge.objects.count(), 100)


class UserTests(TestCase):
    def test_create_user_normalizes_email_and_hashes_password(self):
        user = User.objects.create_user("  Member@Example.com  ", "test-password-123")

        self.assertEqual(user.email, "member@example.com")
        self.assertNotEqual(user.password, "test-password-123")
        self.assertTrue(user.check_password("test-password-123"))
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)

    def test_authenticate_with_email_ignores_case(self):
        user = User.objects.create_user("member@example.com", "test-password-123")

        self.assertEqual(
            authenticate(email="MEMBER@EXAMPLE.COM", password="test-password-123"),
            user,
        )

    def test_database_rejects_case_only_duplicate_even_when_save_is_bypassed(self):
        User.objects.create_user("member@example.com", "test-password-123")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.bulk_create([User(email="MEMBER@example.com", password="!")])

    def test_email_is_required(self):
        with self.assertRaises(ValueError):
            User.objects.create_user("", "test-password-123")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.bulk_create([User(email="", password="!")])

    def test_inactive_user_cannot_authenticate(self):
        user = User.objects.create_user("member@example.com", "test-password-123")
        user.is_active = False
        user.save()

        self.assertIsNone(authenticate(email=user.email, password="test-password-123"))

    def test_user_can_belong_to_a_group(self):
        user = User.objects.create_user("member@example.com", "test-password-123")
        group, _ = Group.objects.get_or_create(name="ORGANIZER")
        user.groups.add(group)

        self.assertTrue(user.groups.filter(pk=group.pk).exists())

    def test_create_superuser_sets_admin_flags(self):
        user = User.objects.create_superuser("Admin@Example.com", "test-password-123")

        self.assertEqual(user.email, "admin@example.com")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_admin_creation_form_hashes_password(self):
        form = UserCreationForm(
            data={
                "email": "Member@Example.com",
                "password1": "test-password-123",
                "password2": "test-password-123",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, "member@example.com")
        self.assertTrue(user.check_password("test-password-123"))

    def test_superuser_can_open_user_admin(self):
        admin_user = User.objects.create_superuser("admin@example.com", "test-password-123")
        self.client.force_login(admin_user)

        response = self.client.get(reverse("admin:accounts_user_add"))

        self.assertEqual(response.status_code, 200)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_password_reset_recovers_active_account_without_email_enumeration(self):
        user = User.objects.create_user("member@example.com", "old-password-123")
        response = self.client.post(reverse("password_reset"), {"email": "MEMBER@example.com"}, secure=True)
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"https://testserver(/password-reset/[^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        response = self.client.get(match.group(1))
        self.assertEqual(response.status_code, 302)
        reset_form_url = response["Location"]
        response = self.client.post(reset_form_url, {
            "new_password1": "Abc12", "new_password2": "Abc12",
        })
        self.assertContains(response, "Password must contain at least 6 letters and numbers")
        response = self.client.post(reset_form_url, {
            "new_password1": "Qz7mP2", "new_password2": "Qz7mP2",
        })
        self.assertRedirects(response, reverse("password_reset_complete"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("Qz7mP2"))
        self.assertFalse(user.check_password("old-password-123"))
        self.assertEqual(AuditEvent.objects.filter(action="PASSWORD_RESET_COMPLETED", target_id=user.pk).count(), 1)
        self.client.post(reverse("password_reset"), {"email": "unknown@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        user.is_active = False
        user.save()
        self.client.post(reverse("password_reset"), {"email": user.email})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 86400)

    def test_admin_group_and_account_change_are_audited(self):
        admin_user = User.objects.create_superuser("admin@example.com", "Securepass123")
        member = User.objects.create_user("member@example.com", "Securepass123")
        group = Group.objects.get(name="ORGANIZER")
        self.client.force_login(admin_user)
        response = self.client.post(reverse("admin:accounts_user_change", args=[member.pk]), {
            "email": member.email, "password": member.password,
            "is_active": "on", "is_staff": "on", "groups": [group.pk],
            "date_joined_0": member.date_joined.date().isoformat(),
            "date_joined_1": member.date_joined.time().strftime("%H:%M:%S"),
            "_save": "Save",
        })
        self.assertEqual(response.status_code, 302, response.content[:300])
        member.refresh_from_db()
        self.assertTrue(member.is_staff)
        self.assertTrue(member.groups.filter(pk=group.pk).exists())
        self.assertEqual(AuditEvent.objects.filter(action="USER_ACCOUNT_CHANGED", target_id=member.pk).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="USER_ROLES_CHANGED", target_id=member.pk).count(), 1)

    def test_admin_account_change_rolls_back_if_audit_fails(self):
        admin_user = User.objects.create_superuser("admin@example.com", "Securepass123")
        member = User.objects.create_user("member@example.com", "Securepass123")
        self.client.force_login(admin_user)
        with patch("club.models.AuditEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("admin:accounts_user_change", args=[member.pk]), {
                    "email": member.email, "password": member.password,
                    "is_active": "on", "is_staff": "on", "_save": "Save",
                    "date_joined_0": member.date_joined.date().isoformat(),
                    "date_joined_1": member.date_joined.time().strftime("%H:%M:%S"),
                })
        member.refresh_from_db()
        self.assertFalse(member.is_staff)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_password_reset_rolls_back_if_audit_fails(self):
        user = User.objects.create_user("member@example.com", "old-password-123")
        self.client.post(reverse("password_reset"), {"email": user.email})
        match = re.search(r"http://testserver(/password-reset/[^\s]+)", mail.outbox[0].body)
        response = self.client.get(match.group(1))
        with patch("club.models.AuditEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(response["Location"], {
                    "new_password1": "new-secure-password-456",
                    "new_password2": "new-secure-password-456",
                })
        user.refresh_from_db()
        self.assertTrue(user.check_password("old-password-123"))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_password_reset_token_is_single_use_and_audit_has_no_secrets(self):
        user = User.objects.create_user("member@example.com", "old-password-123")
        self.client.post(reverse("password_reset"), {"email": user.email})
        path = re.search(r"http://testserver(/password-reset/[^\s]+)", mail.outbox[0].body).group(1)
        redirect = self.client.get(path)
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(self.client.post(redirect["Location"], {
            "new_password1": "new-secure-password-456", "new_password2": "new-secure-password-456",
        }).status_code, 302)
        self.assertContains(self.client.get(path), "invalid or expired")
        event = AuditEvent.objects.get(action="PASSWORD_RESET_COMPLETED", target_id=user.pk)
        self.assertEqual(event.details, {})
        self.assertNotIn("secret", str(event.__dict__))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", PASSWORD_RESET_TIMEOUT=1)
    def test_password_reset_token_expires(self):
        user = User.objects.create_user("member@example.com", "old-password-123")
        issued_at = datetime(2026, 1, 1, 12, 0)
        with patch.object(default_token_generator, "_now", return_value=issued_at):
            self.client.post(reverse("password_reset"), {"email": user.email})
        path = re.search(r"http://testserver(/password-reset/[^\s]+)", mail.outbox[0].body).group(1)
        with patch.object(default_token_generator, "_now", return_value=issued_at + timedelta(seconds=2)):
            self.assertContains(self.client.get(path), "invalid or expired")
        self.assertTrue(user.check_password("old-password-123"))
