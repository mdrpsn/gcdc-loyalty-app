from django.urls import path

from . import player_views as views


app_name = "player"

urlpatterns = [
    path("", views.home, name="home"),
    path("openplays/", views.openplays, name="openplays"),
    path("openplays/<int:openplay_id>/register/", views.register, name="register"),
    path("openplays/<int:openplay_id>/cancel/", views.cancel, name="cancel"),
    path("attendance/", views.attendance, name="attendance"),
    path("rewards/", views.rewards, name="rewards"),
    path("profile/", views.profile, name="profile"),
]
