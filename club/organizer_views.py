"""Server-rendered organizer workflows; domain mutations stay in services."""

from functools import wraps
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.permissions import is_admin, require_admin, require_organizer

from .forms import (
    AuditFilterForm, CorrectionForm, IdentityLinkForm, IdentityLookupForm, OpenPlayForm, ReasonForm,
    RedemptionForm, VenueForm,
)
from .loyalty import get_loyalty_summary, redeem_reward
from .models import Attendance, AttendanceSubmission, AuditEvent, OpenPlay, Player, Registration, RewardIssuance, Venue
from .services import (
    add_historical_attendance,
    cancel_openplay,
    complete_openplay,
    create_openplay,
    create_venue,
    find_players,
    link_player_account,
    openplays_attended,
    review_attendance_submission,
    restore_attendance,
    submit_staff_attendance,
    void_attendance,
)


def organizer_view(view):
    @login_required
    @wraps(view)
    def guarded(request, *args, **kwargs):
        require_organizer(request.user)
        return view(request, *args, **kwargs)

    return guarded


def admin_view(view):
    @login_required
    @wraps(view)
    def guarded(request, *args, **kwargs):
        require_admin(request.user)
        return view(request, *args, **kwargs)

    return guarded


def _error_text(error):
    return "; ".join(error.messages)


def _session_url(openplay_id, query=""):
    url = reverse("organizer:session", args=[openplay_id])
    return f"{url}?{urlencode({'q': query})}" if query else url


@organizer_view
@require_GET
def home(request):
    today = timezone.localdate()
    sessions = (
        OpenPlay.objects.select_related("venue")
        .annotate(
            registered_count=Count(
                "registrations", filter=Q(registrations__status=Registration.Status.ACTIVE), distinct=True
            ),
            checked_in_count=Count(
                "attendances", filter=Q(attendances__status=Attendance.Status.VALID), distinct=True
            ),
        )
    )
    return render(request, "organizer/home.html", {
        "today": today,
        "today_sessions": sessions.filter(starts_at__date=today).order_by("starts_at"),
        "upcoming_sessions": sessions.filter(
            starts_at__date__gt=today,
            starts_at__date__lte=today + timedelta(days=7),
            status=OpenPlay.Status.SCHEDULED,
        ).order_by("starts_at"),
        "pending_attendance_count": AttendanceSubmission.objects.filter(
            status=AttendanceSubmission.Status.PENDING,
        ).count() if is_admin(request.user) else None,
    })


@admin_view
@require_GET
def attendance_submissions(request):
    statuses = {"pending": AttendanceSubmission.Status.PENDING,
                "approved": AttendanceSubmission.Status.APPROVED,
                "declined": AttendanceSubmission.Status.DECLINED}
    selected = request.GET.get("status", "pending").lower()
    if selected not in {"all", *statuses}:
        selected = "pending"
    rows = AttendanceSubmission.objects.select_related(
        "player", "player__user", "openplay", "openplay__venue", "reviewed_by",
    ).order_by("-submitted_at", "-pk")
    if selected != "all":
        rows = rows.filter(status=statuses[selected])
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    counts = {"all": AttendanceSubmission.objects.count()}
    counts.update({key: AttendanceSubmission.objects.filter(status=value).count()
                   for key, value in statuses.items()})
    return render(request, "organizer/attendance_submissions.html", {
        "page": page, "selected": selected, "counts": counts,
    })


@admin_view
@require_POST
def attendance_submission_review(request, submission_id):
    get_object_or_404(AttendanceSubmission, pk=submission_id)
    action = request.POST.get("action")
    if action not in {"approve", "decline"}:
        return HttpResponseBadRequest("Choose approve or decline.")
    try:
        review_attendance_submission(
            submission_id=submission_id, actor=request.user,
            approve=action == "approve", reason=request.POST.get("reason", ""),
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        messages.success(request, "Attendance approved." if action == "approve" else "Attendance declined.")
    selected = request.POST.get("status", "pending")
    return redirect(f"{reverse('organizer:attendance_submissions')}?{urlencode({'status': selected})}")


def _session_context(request, openplay, query):
    registrations = list(
        Registration.objects.filter(openplay=openplay, status=Registration.Status.ACTIVE)
        .select_related("player", "player__user")
        .order_by("player__display_name", "pk")
    )
    attendances = list(
        Attendance.objects.filter(openplay=openplay).select_related("player", "player__user")
    )
    by_player = {attendance.player_id: attendance for attendance in attendances}
    submissions = list(AttendanceSubmission.objects.filter(openplay=openplay).select_related("player"))
    by_submission = {submission.player_id: submission for submission in submissions}
    roster = [{"player": registration.player, "attendance": by_player.get(registration.player_id),
               "submission": by_submission.get(registration.player_id)}
              for registration in registrations]
    registered_ids = {registration.player_id for registration in registrations}
    outside_roster_attendances = [attendance for attendance in attendances
                                  if attendance.player_id not in registered_ids
                                  and attendance.status == Attendance.Status.VALID]
    outside_roster_submissions = [submission for submission in submissions
                                  if submission.player_id not in registered_ids
                                  and submission.status == AttendanceSubmission.Status.PENDING]
    checked_in = sum(attendance.status == Attendance.Status.VALID for attendance in attendances)
    pending_count = sum(submission.status == AttendanceSubmission.Status.PENDING for submission in submissions)
    unchecked = sum(row["attendance"] is None or row["attendance"].status != Attendance.Status.VALID
                    for row in roster)
    query = query.strip()[:100]
    search_results = list(
        find_players(term=query, actor=request.user).select_related("user").order_by("display_name", "pk")[:20]
    ) if len(query) >= 2 else []
    return {
        "openplay": openplay,
        "roster": roster,
        "outside_roster_attendances": outside_roster_attendances,
        "outside_roster_submissions": outside_roster_submissions,
        "registered_count": len(registrations),
        "checked_in_count": checked_in,
        "pending_count": pending_count,
        "unchecked_count": unchecked,
        "query": query,
        "search_results": [{"player": player, "registered": player.pk in registered_ids,
                            "attendance": by_player.get(player.pk),
                            "submission": by_submission.get(player.pk)} for player in search_results],
        "can_complete": openplay.status == OpenPlay.Status.SCHEDULED and timezone.now() >= openplay.ends_at,
        "can_cancel": openplay.status == OpenPlay.Status.SCHEDULED or (
            openplay.status == OpenPlay.Status.COMPLETED and is_admin(request.user)
        ),
        "can_add_historical": openplay.status == OpenPlay.Status.COMPLETED and is_admin(request.user),
    }


@organizer_view
@require_GET
def session_detail(request, openplay_id):
    openplay = get_object_or_404(OpenPlay.objects.select_related("venue"), pk=openplay_id)
    return render(request, "organizer/session.html", _session_context(request, openplay, request.GET.get("q", "")))


@organizer_view
@require_POST
def check_in_player(request, openplay_id, player_id):
    get_object_or_404(OpenPlay, pk=openplay_id)
    get_object_or_404(Player, pk=player_id)
    try:
        submission = submit_staff_attendance(openplay_id=openplay_id, player_id=player_id, actor=request.user)
        messages.success(request, "Attendance submitted for admin approval." if submission.status == "PENDING"
                         else "Attendance was already reviewed.")
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return redirect(_session_url(openplay_id, request.POST.get("q", "")[:100]))


@organizer_view
@require_http_methods(["GET", "POST"])
def complete_session(request, openplay_id):
    openplay = get_object_or_404(OpenPlay.objects.select_related("venue"), pk=openplay_id)
    context = _session_context(request, openplay, "")
    if request.method == "POST":
        try:
            complete_openplay(openplay_id=openplay_id, actor=request.user)
        except ValidationError as error:
            context["error"] = _error_text(error)
            return render(request, "organizer/confirm_complete.html", context, status=409)
        messages.success(request, "OpenPlay completed. Loyalty eligibility has been reconciled.")
        return redirect("organizer:session", openplay_id=openplay_id)
    return render(request, "organizer/confirm_complete.html", context)


@organizer_view
@require_http_methods(["GET", "POST"])
def cancel_session(request, openplay_id):
    openplay = get_object_or_404(OpenPlay.objects.select_related("venue"), pk=openplay_id)
    if openplay.status == OpenPlay.Status.COMPLETED:
        require_admin(request.user)
    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            cancel_openplay(openplay_id=openplay_id, actor=request.user, reason=form.cleaned_data["reason"])
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            messages.success(request, "OpenPlay cancelled. Attendance history was preserved.")
            return redirect("organizer:session", openplay_id=openplay_id)
    return render(request, "organizer/confirm_cancel.html", {"openplay": openplay, "form": form},
                  status=400 if request.method == "POST" else 200)


@organizer_view
@require_http_methods(["GET", "POST"])
def venue_new(request):
    form = VenueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            venue = create_venue(name=form.cleaned_data["name"], actor=request.user)
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        except IntegrityError:
            # A simultaneous submit passed validation; the database constraint still held.
            form.add_error(None, "A venue with this name already exists.")
        else:
            messages.success(request, f"Venue “{venue.name}” created. Schedule an OpenPlay there.")
            return redirect("organizer:openplay_new")
    return render(request, "organizer/schedule_form.html", {
        "form": form, "title": "Add a venue", "submit_label": "Create venue",
    }, status=400 if request.method == "POST" else 200)


@organizer_view
@require_http_methods(["GET", "POST"])
def openplay_new(request):
    form = OpenPlayForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            openplay = create_openplay(
                venue_id=data["venue"].pk, starts_at=data["starts_at"], ends_at=data["ends_at"],
                actor=request.user,
            )
        except (ValidationError, Venue.DoesNotExist) as error:
            form.add_error(None, _error_text(error) if isinstance(error, ValidationError) else "Venue not found.")
        else:
            messages.success(request, "OpenPlay scheduled.")
            return redirect("organizer:session", openplay_id=openplay.pk)
    return render(request, "organizer/schedule_form.html", {
        "form": form, "title": "Schedule an OpenPlay", "submit_label": "Schedule OpenPlay",
        "show_venue_link": not form.fields["venue"].queryset.exists(),
        "time_zone": timezone.get_current_timezone_name(),
    }, status=400 if request.method == "POST" else 200)


@organizer_view
@require_GET
def player_search(request):
    query = request.GET.get("q", "").strip()[:100]
    players = list(find_players(term=query, actor=request.user).select_related("user")
                   .order_by("display_name", "pk")[:30]) if len(query) >= 2 else []
    return render(request, "organizer/players.html", {"query": query, "players": players})


@organizer_view
@require_GET
def player_detail(request, player_id):
    player = get_object_or_404(Player.objects.select_related("user"), pk=player_id)
    rewards = list(RewardIssuance.objects.filter(player=player).select_related("milestone")
                   .order_by("milestone__threshold"))
    history = list(Attendance.objects.filter(player=player).select_related("openplay", "openplay__venue")
                   .order_by("-openplay__starts_at", "-pk")[:20])
    return render(request, "organizer/player.html", {
        "player": player, "summary": get_loyalty_summary(player), "rewards": rewards,
        "history": history, "is_admin": is_admin(request.user),
    })


@organizer_view
@require_http_methods(["GET", "POST"])
def redeem(request, player_id, issuance_id):
    player = get_object_or_404(Player, pk=player_id)
    issuance = get_object_or_404(
        RewardIssuance.objects.select_related("milestone"), pk=issuance_id, player=player
    )
    form = RedemptionForm(request.POST or None)
    error = None
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                redeem_reward(
                    issuance_id=issuance_id, player_id=player_id, actor=request.user,
                    openplay_id=form.cleaned_data["openplay"].pk,
                )
            except ValidationError as exception:
                error = _error_text(exception)
                status = 409
            else:
                messages.success(request, "Reward redemption confirmed and recorded.")
                return redirect("organizer:player", player_id=player_id)
        else:
            status = 400
    elif issuance.status != RewardIssuance.Status.AVAILABLE:
        error = "Only an available reward can be redeemed."
        status = 409
    return render(request, "organizer/redeem.html", {
        "player": player, "issuance": issuance, "form": form, "error": error,
    }, status=status)


@admin_view
@require_GET
def identity_search(request):
    query = request.GET.get("q", "").strip()[:100]
    players = list(find_players(term=query, actor=request.user).filter(user__isnull=True)
                   .order_by("display_name", "pk")[:30]) if len(query) >= 2 else []
    return render(request, "organizer/identity_search.html", {"query": query, "players": players})


@admin_view
@require_http_methods(["GET", "POST"])
def identity_link(request, player_id):
    """The admin names the Player and the account; nothing is matched or linked automatically."""
    User = get_user_model()
    player = get_object_or_404(Player.objects.select_related("user"), pk=player_id)
    lookup_form = IdentityLookupForm(request.GET or None)
    link_form = None
    account = None
    status = 200
    if request.method == "POST":
        link_form = IdentityLinkForm(request.POST)
        if link_form.is_valid():
            try:
                link_player_account(
                    player_id=player.pk, user_id=link_form.cleaned_data["user_id"], actor=request.user,
                )
            except ValidationError as error:
                link_form.add_error(None, _error_text(error))
                status = 409
            except User.DoesNotExist:
                link_form.add_error(None, "Account not found.")
                status = 409
            else:
                messages.success(request, "Account linked. The player's history and rewards are unchanged.")
                return redirect("organizer:player", player_id=player.pk)
        else:
            status = 400
        raw_id = request.POST.get("user_id", "")
        account = User.objects.filter(pk=raw_id).first() if raw_id.isdigit() else None
    elif request.GET.get("email") and lookup_form.is_valid():
        account = User.objects.filter(email__iexact=lookup_form.cleaned_data["email"]).first()
        if account is None:
            lookup_form.add_error("email", "No account has that email.")
        else:
            link_form = IdentityLinkForm(initial={"user_id": account.pk})
    return render(request, "organizer/identity_link.html", {
        "player": player, "attended": openplays_attended(player),
        "lookup_form": lookup_form, "link_form": link_form, "account": account,
        "account_player": Player.objects.filter(user=account).first() if account else None,
    }, status=status)


@admin_view
@require_GET
def historical_search(request, openplay_id):
    openplay = get_object_or_404(OpenPlay.objects.select_related("venue"), pk=openplay_id)
    query = request.GET.get("q", "").strip()[:100]
    players = []
    if openplay.status == OpenPlay.Status.COMPLETED and len(query) >= 2:
        players = list(find_players(term=query, actor=request.user).select_related("user")
                       .order_by("display_name", "pk")[:30])
    attendance_by_player = {
        attendance.player_id: attendance
        for attendance in Attendance.objects.filter(openplay=openplay, player__in=players)
    }
    return render(request, "organizer/historical_search.html", {
        "openplay": openplay, "query": query,
        "results": [{"player": player, "attendance": attendance_by_player.get(player.pk)} for player in players],
    })


@admin_view
@require_http_methods(["GET", "POST"])
def historical_add(request, openplay_id, player_id):
    openplay = get_object_or_404(OpenPlay.objects.select_related("venue"), pk=openplay_id)
    player = get_object_or_404(Player.objects.select_related("user"), pk=player_id)
    form = ReasonForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                add_historical_attendance(
                    openplay_id=openplay.pk, player_id=player.pk, actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as error:
                form.add_error(None, _error_text(error))
                status = 409
            else:
                messages.success(request, "Historical attendance added. Loyalty has been reconciled.")
                return redirect("organizer:player", player_id=player.pk)
        else:
            status = 400
    existing = Attendance.objects.filter(openplay=openplay, player=player).first()
    return render(request, "organizer/historical_add.html", {
        "openplay": openplay, "player": player, "attended": openplays_attended(player),
        "form": form, "existing": existing,
        "can_add": openplay.status == OpenPlay.Status.COMPLETED and existing is None,
    }, status=status)


@admin_view
@require_GET
def audit_log(request):
    """Read-only review of AuditEvent; only valid filters are applied."""
    form = AuditFilterForm(request.GET)
    form.is_valid()
    filters = form.cleaned_data
    events = AuditEvent.objects.select_related("actor")
    if filters.get("action"):
        events = events.filter(action=filters["action"])
    if filters.get("target_type"):
        events = events.filter(target_type=filters["target_type"])
    if filters.get("target_id") is not None:
        events = events.filter(target_id=filters["target_id"])
    if filters.get("actor"):
        events = events.filter(actor__email__iexact=filters["actor"])
    page = Paginator(events.order_by("-occurred_at", "-pk"), 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "organizer/audit.html", {"form": form, "page": page, "query_string": params.urlencode()})


@admin_view
@require_http_methods(["GET", "POST"])
def correct_attendance(request, attendance_id):
    attendance = get_object_or_404(
        Attendance.objects.select_related("player", "openplay", "openplay__venue"), pk=attendance_id
    )
    form = CorrectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        operation = void_attendance if form.cleaned_data["action"] == "VOID" else restore_attendance
        try:
            operation(attendance_id=attendance_id, actor=request.user, reason=form.cleaned_data["reason"])
        except ValidationError as error:
            form.add_error(None, _error_text(error))
        else:
            messages.success(request, "Attendance correction recorded and loyalty reconciled.")
            return redirect("organizer:player", player_id=attendance.player_id)
    return render(request, "organizer/correct.html", {"attendance": attendance, "form": form},
                  status=400 if request.method == "POST" else 200)
