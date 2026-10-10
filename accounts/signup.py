"""Email ownership check for public member signup."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from club.services import create_member_account

from .models import SignupEmailChallenge, SignupRateLimit


SIGNUP_LINK_LIFETIME = timedelta(minutes=30)
EMAIL_RESEND_DELAY = timedelta(minutes=2)
SEND_WINDOW = timedelta(hours=1)
EMAIL_SEND_LIMIT = 5
IP_SEND_LIMIT = 100  # Allow for a shared proxy or venue network.


def _token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def request_signup_link(*, email, request):
    """Send a fresh link within shared DB limits; keep the response generic."""
    now = timezone.now()
    email = get_user_model().objects.normalize_login_email(email)
    if get_user_model().objects.filter(email__iexact=email).exists():
        return

    # REMOTE_ADDR is supplied by the web server. Do not trust a client-sent
    # X-Forwarded-For value for a security limit.
    ip_key = salted_hmac("signup-email-ip", request.META.get("REMOTE_ADDR", "unknown")).hexdigest()
    budget, _ = SignupRateLimit.objects.get_or_create(
        key=ip_key, defaults={"window_started_at": now}
    )
    budget = SignupRateLimit.objects.select_for_update().get(pk=budget.pk)
    if now - budget.window_started_at >= SEND_WINDOW:
        budget.window_started_at = now
        budget.sends_in_window = 0
    if budget.sends_in_window >= IP_SEND_LIMIT:
        return

    token = secrets.token_urlsafe(32)
    challenge, created = SignupEmailChallenge.objects.get_or_create(
        email=email,
        defaults={
            "token_digest": _token_digest(token), "expires_at": now + SIGNUP_LINK_LIFETIME,
            "last_sent_at": now, "window_started_at": now, "sends_in_window": 0,
        },
    )
    if not created:
        challenge = SignupEmailChallenge.objects.select_for_update().get(pk=challenge.pk)
        if now - challenge.window_started_at >= SEND_WINDOW:
            challenge.window_started_at = now
            challenge.sends_in_window = 0
        if (now - challenge.last_sent_at < EMAIL_RESEND_DELAY
                or challenge.sends_in_window >= EMAIL_SEND_LIMIT):
            return
        challenge.token_digest = _token_digest(token)
        challenge.expires_at = now + SIGNUP_LINK_LIFETIME
        challenge.last_sent_at = now
    challenge.sends_in_window += 1
    challenge.save()
    budget.sends_in_window += 1
    budget.save()

    url = request.build_absolute_uri(reverse("signup_verify", args=[token]))
    send_mail(
        "Verify your Got Cha Dink Club email",
        "Use this link to continue creating your Got Cha Dink Club account:\n\n"
        f"{url}\n\nThis link expires in 30 minutes. If you did not request it, ignore this email.",
        settings.DEFAULT_FROM_EMAIL,
        [email],
    )


def verified_challenge(token):
    if len(token) > 128:
        return None
    return SignupEmailChallenge.objects.filter(
        token_digest=_token_digest(token), expires_at__gt=timezone.now()
    ).first()


@transaction.atomic
def complete_verified_signup(*, token, full_name, phone, reclub_name, reclub_profile_url, password):
    if len(token) > 128:
        raise ValidationError("This signup link is invalid or expired. Request a new link.")
    challenge = SignupEmailChallenge.objects.select_for_update().filter(
        token_digest=_token_digest(token), expires_at__gt=timezone.now()
    ).first()
    if challenge is None:
        raise ValidationError("This signup link is invalid or expired. Request a new link.")
    if get_user_model().objects.filter(email__iexact=challenge.email).exists():
        raise ValidationError("An account already uses this email. Sign in or reset your password.")
    user = create_member_account(
        email=challenge.email, password=password, full_name=full_name, phone=phone,
        reclub_name=reclub_name, reclub_profile_url=reclub_profile_url,
    )
    challenge.delete()
    return user
