"""Member pages. All data is scoped to request.user.player."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .calendar_grid import local_date, month_bounds, month_context, parse_month
from .loyalty import (
    cancel_redemption_request, get_loyalty_summary, get_upcoming_milestones, is_session_bound,
    request_reward_redemption,
)
from .models import (
    Attendance, AttendanceSubmission, OpenPlay, Registration, RewardIssuance, RewardRedemptionRequest,
)
from .player_forms import AttendanceSubmissionForm, PlayerProfileForm, RewardUseForm
from .progression import get_badges, get_reward_progress, get_streaks
from .services import (
    cancel_registration, counted_openplay_starts, create_linked_player, openplays_attended, register_player,
    submit_member_attendance, update_own_profile,
)


def _player(request):
    return getattr(request.user, "player", None)


def _required_player(request):
    player = _player(request)
    if player is None:
        messages.info(request, "Set up your player profile to use the club pages.")
    return player


@login_required
@require_GET
def home(request):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    summary = get_loyalty_summary(player)
    streaks = get_streaks(player)
    now = timezone.now()
    available = [reward for reward in RewardIssuance.objects.filter(
        player=player, status=RewardIssuance.Status.AVAILABLE,
    ).order_by("earned_at", "pk") if not reward.is_expired(now)]
    next_registration = Registration.objects.filter(
        player=player, status=Registration.Status.ACTIVE,
        openplay__status=OpenPlay.Status.SCHEDULED, openplay__ends_at__gte=timezone.now(),
    ).select_related("openplay", "openplay__venue").order_by("openplay__starts_at", "openplay_id").first()
    return render(request, "player/home.html", {
        "player": player, "summary": summary,
        "progress": get_reward_progress(summary), "streaks": streaks,
        "badges": get_badges(completed_attendances=summary.completed_attendances, streaks=streaks),
        "available_rewards": available[:3], "available_count": len(available),
        "next_session": next_registration.openplay if next_registration else None,
    })


@login_required
@require_GET
def openplays(request):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    now = timezone.now()
    sessions_page = Paginator(OpenPlay.objects.filter(
        status=OpenPlay.Status.SCHEDULED, ends_at__gte=now,
    ).select_related("venue").order_by("starts_at", "pk"), 20).get_page(request.GET.get("page"))
    sessions = list(sessions_page)
    registrations = {
        registration.openplay_id: registration.status
        for registration in Registration.objects.filter(
            player=player, openplay_id__in=[session.pk for session in sessions],
        )
    }
    for session in sessions:
        session.member_registration_status = registrations.get(session.pk)
        session.member_can_register = now < session.starts_at
    first = parse_month(request.GET.get("month"), timezone.localdate())
    start, end = month_bounds(first)
    month_sessions = list(OpenPlay.objects.filter(
        status=OpenPlay.Status.SCHEDULED, ends_at__gte=now, starts_at__gte=start, starts_at__lt=end,
    ).values_list("pk", "starts_at"))
    registered_ids = set(Registration.objects.filter(
        player=player, status=Registration.Status.ACTIVE, openplay_id__in=[pk for pk, _ in month_sessions],
    ).values_list("openplay_id", flat=True)) if month_sessions else set()
    marked = {}
    for pk, starts_at in month_sessions:
        day = local_date(starts_at)
        marked[day] = "registered" if pk in registered_ids or marked.get(day) == "registered" else "session"
    return render(request, "player/openplays.html", {
        "sessions": sessions, "page": sessions_page,
        "calendar": month_context(first, marked, timezone.localdate()),
    })


def _change_registration(request, openplay_id, operation, success):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    try:
        operation(openplay_id=openplay_id, player_id=player.pk, actor=request.user)
    except (ValidationError, Registration.DoesNotExist, OpenPlay.DoesNotExist) as error:
        messages.error(request, "; ".join(error.messages) if isinstance(error, ValidationError) else "OpenPlay or registration not found.")
    else:
        messages.success(request, success)
    return redirect("player:openplays")


@login_required
@require_POST
def register(request, openplay_id):
    return _change_registration(request, openplay_id, register_player, "Registration confirmed.")


@login_required
@require_POST
def cancel(request, openplay_id):
    return _change_registration(request, openplay_id, cancel_registration, "Registration cancelled.")


@login_required
@require_GET
def attendance(request):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    rows = Attendance.objects.filter(player=player).select_related("openplay", "openplay__venue").order_by(
        "-openplay__starts_at", "-pk"
    )
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    first = parse_month(request.GET.get("month"), timezone.localdate())
    start, end = month_bounds(first)
    month_starts = counted_openplay_starts(player, start, end)
    return render(request, "player/attendance.html", {
        "page": page, "attended_total": openplays_attended(player), "attended_in_month": len(month_starts),
        "calendar": month_context(first, {local_date(moment): "attended" for moment in month_starts}, timezone.localdate()),
        "submissions": AttendanceSubmission.objects.filter(player=player).select_related(
            "openplay", "openplay__venue",
        ).order_by("-submitted_at", "-pk")[:25],
    })


@login_required
@require_http_methods(["GET", "POST"])
def submit_attendance(request):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    form = AttendanceSubmissionForm(request.POST or None, initial={"openplay": request.GET.get("openplay")})
    if request.method == "POST" and form.is_valid():
        try:
            submission = submit_member_attendance(openplay_id=form.cleaned_data["openplay"].pk, actor=request.user)
        except ValidationError as error:
            form.add_error(None, "; ".join(error.messages))
        else:
            messages.success(request, "Attendance submitted for approval." if submission.status == "PENDING"
                             else "Attendance already approved.")
            return redirect("player:attendance")
    return render(request, "player/attendance_submit.html", {"form": form, "player": player})


@login_required
@require_http_methods(["GET", "POST"])
def reward_use(request, issuance_id):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    issuance = get_object_or_404(RewardIssuance, pk=issuance_id, player=player)
    form = RewardUseForm(request.POST if request.method == "POST" else None, issuance=issuance)
    if request.method == "POST" and form.is_valid():
        try:
            request_reward_redemption(
                issuance_id=issuance.pk, openplay_id=form.cleaned_data["openplay"].pk, actor=request.user,
            )
        except ValidationError as error:
            form.add_error(None, "; ".join(error.messages))
        else:
            messages.success(request, "Reward requested. An admin will confirm it for that OpenPlay.")
            return redirect("player:rewards")
    return render(request, "player/reward_use.html", {"form": form, "issuance": issuance})


@login_required
@require_POST
def reward_request_cancel(request, request_id):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    reward_request = get_object_or_404(RewardRedemptionRequest, pk=request_id, player=player)
    try:
        cancel_redemption_request(request_id=reward_request.pk, actor=request.user)
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, "Reward request cancelled. The reward is available again.")
    return redirect("player:rewards")


@login_required
@require_GET
def rewards(request):
    player = _required_player(request)
    if player is None:
        return redirect("player:profile")
    issuances = list(RewardIssuance.objects.filter(player=player).select_related(
        "redemption", "redemption__openplay", "redemption__openplay__venue",
    ).order_by("-earned_at", "-pk"))
    now = timezone.now()
    open_requests = {
        item.issuance_id: item for item in RewardRedemptionRequest.objects.filter(
            player=player, status=RewardRedemptionRequest.Status.PENDING,
        ).select_related("openplay", "openplay__venue")
    }
    for reward in issuances:
        reward.open_request = open_requests.get(reward.pk)
        reward.can_request = (
            reward.status == RewardIssuance.Status.AVAILABLE and not reward.is_expired(now)
            and is_session_bound(reward) and reward.open_request is None
        )
    return render(request, "player/rewards.html", {
        "available": [r for r in issuances if r.status == RewardIssuance.Status.AVAILABLE and not r.is_expired(now)],
        "expired": [r for r in issuances if r.is_expired(now)],
        "redeemed": [r for r in issuances if r.status == RewardIssuance.Status.REDEEMED],
        "revoked": [r for r in issuances if r.status == RewardIssuance.Status.REVOKED],
        "upcoming": get_upcoming_milestones(player),
    })


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    player = _player(request)
    form = PlayerProfileForm(request.POST, player=player) if request.method == "POST" else PlayerProfileForm(player=player)
    if request.method == "POST" and form.is_valid():
        try:
            if player is None:
                create_linked_player(
                    user_id=request.user.pk, display_name=form.cleaned_data["display_name"],
                    phone=form.cleaned_data["phone"], actor=request.user,
                    reclub_name=form.cleaned_data["reclub_name"],
                    reclub_profile_url=form.cleaned_data["reclub_profile_url"],
                )
            else:
                update_own_profile(
                    player_id=player.pk, display_name=form.cleaned_data["display_name"],
                    phone=form.cleaned_data["phone"], actor=request.user,
                    reclub_name=form.cleaned_data["reclub_name"],
                    reclub_profile_url=form.cleaned_data["reclub_profile_url"],
                )
        except (ValidationError, IntegrityError) as error:
            form.add_error(None, "; ".join(error.messages) if isinstance(error, ValidationError) else "Profile could not be saved.")
        else:
            messages.success(request, "Profile saved.")
            return redirect("player:profile")
    return render(request, "player/profile.html", {"player": player, "form": form})
