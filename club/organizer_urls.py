from django.urls import path

from . import organizer_views as views


app_name = "organizer"

urlpatterns = [
    path("", views.home, name="home"),
    path("openplays/<int:openplay_id>/", views.session_detail, name="session"),
    path("openplays/<int:openplay_id>/check-in/<int:player_id>/", views.check_in_player, name="check_in"),
    path("openplays/<int:openplay_id>/walk-in/", views.walk_in, name="walk_in"),
    path("openplays/<int:openplay_id>/complete/", views.complete_session, name="complete"),
    path("openplays/<int:openplay_id>/cancel/", views.cancel_session, name="cancel"),
    path("schedule/openplays/new/", views.openplay_new, name="openplay_new"),
    path("schedule/venues/new/", views.venue_new, name="venue_new"),
    path("players/", views.player_search, name="players"),
    path("players/<int:player_id>/", views.player_detail, name="player"),
    path("players/<int:player_id>/rewards/<int:issuance_id>/redeem/", views.redeem, name="redeem"),
    path("audit/", views.audit_log, name="audit"),
    path("identity/", views.identity_search, name="identity"),
    path("identity/<int:player_id>/link/", views.identity_link, name="identity_link"),
    path("attendances/<int:attendance_id>/correct/", views.correct_attendance, name="correct"),
]
