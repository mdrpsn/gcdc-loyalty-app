from django.contrib import admin
from django.urls import include, path
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView

from .views import health
from .auth_views import ClubLoginView, ClubPasswordResetConfirmView, signup, signup_done, signup_verify

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="player:home", permanent=False), name="home"),
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("login/", ClubLoginView.as_view(), name="login"),
    path("signup/", signup, name="signup"),
    path("signup/check-email/", signup_done, name="signup_done"),
    path("signup/verify/<str:token>/", signup_verify, name="signup_verify"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="registration/password_reset_form.html",
        email_template_name="registration/password_reset_email.txt",
        subject_template_name="registration/password_reset_subject.txt",
    ), name="password_reset"),
    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(
        template_name="registration/password_reset_done.html",
    ), name="password_reset_done"),
    path("password-reset/<uidb64>/<token>/", ClubPasswordResetConfirmView.as_view(),
         name="password_reset_confirm"),
    path("password-reset/complete/", auth_views.PasswordResetCompleteView.as_view(
        template_name="registration/password_reset_complete.html",
    ), name="password_reset_complete"),
    path("organizer/", include("club.organizer_urls")),
    path("player/", include("club.player_urls")),
]
