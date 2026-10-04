from django.contrib.auth.views import LoginView, PasswordResetConfirmView
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from accounts.permissions import is_organizer
from accounts.forms import MemberSignupForm, SignupEmailForm
from accounts.signup import complete_verified_signup, request_signup_link, verified_challenge


class ClubLoginView(LoginView):
    template_name = "registration/login.html"

    def get_default_redirect_url(self):
        if is_organizer(self.request.user):
            return reverse("organizer:home")
        return reverse("player:home")


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("player:home")
    form = SignupEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        request_signup_link(email=form.cleaned_data["email"], request=request)
        return redirect("signup_done")
    return render(request, "registration/signup.html", {"form": form})


@require_http_methods(["GET"])
def signup_done(request):
    return render(request, "registration/signup_done.html")


@sensitive_post_parameters("password1", "password2")
@never_cache
@require_http_methods(["GET", "POST"])
def signup_verify(request, token):
    if request.user.is_authenticated:
        return redirect("player:home")
    challenge = verified_challenge(token)
    if challenge is None:
        response = render(request, "registration/signup_invalid.html", status=400)
    else:
        form = MemberSignupForm(request.POST or None, verified_email=challenge.email)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            try:
                user = complete_verified_signup(
                    token=token, full_name=data["full_name"], phone=data["phone"],
                    reclub_name=data["reclub_name"],
                    reclub_profile_url=data["reclub_profile_url"], password=data["password1"],
                )
            except (ValidationError, IntegrityError) as error:
                form.add_error(None, "; ".join(error.messages) if isinstance(error, ValidationError)
                               else "Account could not be created. Check your details and try again.")
            else:
                login(request, user)
                return redirect("player:home")
        response = render(request, "registration/signup_profile.html", {
            "form": form, "email": challenge.email,
        })
    response["Referrer-Policy"] = "no-referrer"
    return response


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
