from django.urls import path

from . import player_views as views


app_name = "player"

urlpatterns = [
    path("", views.home, name="home"),
    path("openplays/", views.openplays, name="openplays"),
    path("openplays/<int:openplay_id>/register/", views.register, name="register"),
    path("openplays/<int:openplay_id>/cancel/", views.cancel, name="cancel"),
    path("attendance/", views.attendance, name="attendance"),
    path("attendance/submit/", views.submit_attendance, name="attendance_submit"),
    path("rewards/", views.rewards, name="rewards"),
    path("rewards/<int:issuance_id>/use/", views.reward_use, name="reward_use"),
    path("rewards/requests/<int:request_id>/cancel/", views.reward_request_cancel, name="reward_request_cancel"),
    path("profile/", views.profile, name="profile"),
]
