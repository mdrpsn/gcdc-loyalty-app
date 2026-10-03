from django.contrib.auth.views import LoginView, PasswordResetConfirmView
from django.db import transaction
from django.urls import reverse

from accounts.permissions import is_organizer


class ClubLoginView(LoginView):
    template_name = "registration/login.html"

    def get_default_redirect_url(self):
        if is_organizer(self.request.user):
            return reverse("organizer:home")
        return reverse("player:home")


class ClubPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = "/password-reset/complete/"

    @transaction.atomic
    def form_valid(self, form):
        from club.models import AuditEvent

        response = super().form_valid(form)
        AuditEvent.objects.create(
            action="PASSWORD_RESET_COMPLETED", target_type="user",
            target_id=self.user.pk, details={},
        )
        return response
