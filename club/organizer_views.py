"""Server-rendered organizer workflows; domain mutations stay in services."""

from functools import wraps
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
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
    RedemptionForm, VenueForm, WalkInForm,
)
from .loyalty import get_loyalty_summary, redeem_reward
from .models import Attendance, AuditEvent, OpenPlay, Player, Registration, RewardIssuance, Venue
from .services import (
    cancel_openplay,
    check_in,
    check_in_walk_in,
    complete_openplay,
    create_openplay,
    create_venue,
    find_players,
    find_walk_in_matches,
    link_player_account,
    openplays_attended,
    restore_attendance,
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
    })


def _session_context(request, openplay, query, walk_in_form=None, matches=None):
    registrations = list(
        Registration.objects.filter(openplay=openplay, status=Registration.Status.ACTIVE)
        .select_related("player", "player__user")
        .order_by("player__display_name", "pk")
    )
    attendances = list(
        Attendance.objects.filter(openplay=openplay).select_related("player", "player__user")
    )
    by_player = {attendance.player_id: attendance for attendance in attendances}
    roster = [{"player": registration.player, "attendance": by_player.get(registration.player_id)}
              for registration in registrations]
    registered_ids = {registration.player_id for registration in registrations}
    walk_ins = [attendance for attendance in attendances
                if attendance.player_id not in registered_ids and attendance.status == Attendance.Status.VALID]
    checked_in = sum(attendance.status == Attendance.Status.VALID for attendance in attendances)
    unchecked = sum(row["attendance"] is None or row["attendance"].status != Attendance.Status.VALID
                    for row in roster)
    query = query.strip()[:100]
    search_results = list(
        find_players(term=query, actor=request.user).select_related("user").order_by("display_name", "pk")[:20]
    ) if len(query) >= 2 else []
    if matches is not None:
        search_results = matches
    token = signing.dumps({"openplay_id": openplay.pk, "query": query}, salt="club.walk-in") if len(query) >= 2 else ""
    if walk_in_form is None and token:
        walk_in_form = WalkInForm(initial={"search_token": token})
    return {
        "openplay": openplay,
        "roster": roster,
        "walk_ins": walk_ins,
        "registered_count": len(registrations),
        "checked_in_count": checked_in,
        "unchecked_count": unchecked,
        "query": query,
        "search_results": [{"player": player, "registered": player.pk in registered_ids,
                            "attendance": by_player.get(player.pk)} for player in search_results],
        "walk_in_form": walk_in_form,
        "can_complete": openplay.status == OpenPlay.Status.SCHEDULED and timezone.now() >= openplay.ends_at,
        "can_cancel": openplay.status == OpenPlay.Status.SCHEDULED or (
            openplay.status == OpenPlay.Status.COMPLETED and is_admin(request.user)
        ),
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
        _, created = check_in(openplay_id=openplay_id, player_id=player_id, actor=request.user)
        messages.success(request, "Check-in confirmed." if created else "Already checked in; no duplicate created.")
    except ValidationError as error:
        messages.error(request, _error_text(error))
    return redirect(_session_url(openplay_id, request.POST.get("q", "")[:100]))


@organizer_view
@require_POST
def walk_in(request, openplay_id):
    openplay = get_object_or_404(OpenPlay.objects.select_related("venue"), pk=openplay_id)
    form = WalkInForm(request.POST)
    query = ""
    try:
        token_data = signing.loads(request.POST.get("search_token", ""), salt="club.walk-in", max_age=900)
        if token_data.get("openplay_id") != openplay_id or len(token_data.get("query", "").strip()) < 2:
            raise signing.BadSignature("Search does not match this OpenPlay")
        query = token_data["query"]
    except signing.BadSignature:
        return HttpResponseBadRequest("Search for an existing player before creating a walk-in.")
    if not form.is_valid():
        return render(request, "organizer/session.html", _session_context(request, openplay, query, form), status=400)
    data = form.cleaned_data
    matches = list(find_walk_in_matches(
        display_name=data["display_name"], phone=data["phone"],
        contact_email=data["contact_email"], actor=request.user,
    ))
    if matches and not data["confirm_distinct"]:
        form.add_error(None, "Matching players found. Select the existing Player, or confirm this is a different person.")
        return render(request, "organizer/session.html", _session_context(request, openplay, query, form, matches), status=409)
    try:
        player, _ = check_in_walk_in(
            openplay_id=openplay_id, display_name=data["display_name"], phone=data["phone"],
            contact_email=data["contact_email"], actor=request.user,
        )
    except ValidationError as error:
        form.add_error(None, _error_text(error))
        return render(request, "organizer/session.html", _session_context(request, openplay, query, form), status=409)
    messages.success(request, f"{player.display_name} created and checked in.")
    return redirect(_session_url(openplay_id, player.display_name))


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
