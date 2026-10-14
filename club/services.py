"""Phase 2 operations for sessions, bookings, and the attendance ledger."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.permissions import is_organizer, require_admin, require_organizer

from .models import Attendance, AttendanceSubmission, AuditEvent, OpenPlay, Player, Registration, Venue


def _audit(action, target, actor, **details):
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type=target._meta.model_name,
        target_id=target.pk,
        details=details,
    )


def _require_self_or_organizer(actor, player):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("Authentication is required")
    if actor.pk != player.user_id and not is_organizer(actor):
        raise PermissionDenied("This player record is not yours")


def _create_walk_in(display_name, phone, contact_email, actor, reclub_name="", reclub_profile_url=""):
    player = Player(
        display_name=display_name, phone=phone or "", contact_email=contact_email,
        reclub_name=reclub_name.strip(), reclub_profile_url=reclub_profile_url.strip(),
    )
    player.save()
    _audit("PLAYER_CREATED", player, actor, kind="walk_in")
    return player


@transaction.atomic
def create_walk_in_player(*, display_name, phone="", contact_email=None, actor,
                          reclub_name="", reclub_profile_url=""):
    require_organizer(actor)
    return _create_walk_in(display_name, phone, contact_email, actor, reclub_name, reclub_profile_url)


@transaction.atomic
def create_linked_player(*, user_id, display_name, actor, phone="", reclub_name="", reclub_profile_url=""):
    user = get_user_model().objects.select_for_update().get(pk=user_id)
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("Authentication is required")
    if actor.pk != user.pk:
        require_admin(actor)
    player = Player(
        user=user, display_name=display_name, phone=phone,
        reclub_name=reclub_name.strip(), reclub_profile_url=reclub_profile_url.strip(),
    )
    player.save()
    _audit("PLAYER_CREATED", player, actor, kind="account_linked")
    return player


@transaction.atomic
def update_own_profile(*, player_id, display_name, phone, actor,
                       reclub_name=None, reclub_profile_url=None):
    """Members may edit only non-identity profile fields on their linked Player."""
    player = Player.objects.select_for_update().get(pk=player_id, user=actor)
    if not actor.is_active:
        raise PermissionDenied("An active account is required")
    changed = []
    fields = [("display_name", display_name), ("phone", phone)]
    if reclub_name is not None:
        fields.append(("reclub_name", reclub_name.strip()))
    if reclub_profile_url is not None:
        fields.append(("reclub_profile_url", reclub_profile_url.strip()))
    for field, value in fields:
        if getattr(player, field) != value:
            setattr(player, field, value)
            changed.append(field)
    if changed:
        player.save(update_fields=[*changed, "updated_at"])
        _audit("PLAYER_PROFILE_UPDATED", player, actor, fields=changed)
    return player


def _validate_claim_identity(*, full_name, email, phone, reclub_name, reclub_profile_url):
    """Enforce required claim details even when a caller bypasses the HTML form."""
    if not all((full_name.strip(), email.strip(), phone.strip(), reclub_name.strip(),
                reclub_profile_url.strip())):
        raise ValidationError("Enter all contact and ReClub details")
    validate_email(email.strip())
    URLValidator(schemes=["http", "https"])(reclub_profile_url.strip())


@transaction.atomic
def create_member_account(*, email, password, full_name, phone, reclub_name, reclub_profile_url):
    """Create the login and its player profile together; never claim a walk-in implicitly."""
    User = get_user_model()
    _validate_claim_identity(
        full_name=full_name, email=email, phone=phone,
        reclub_name=reclub_name, reclub_profile_url=reclub_profile_url,
    )
    email = User.objects.normalize_login_email(email)
    if Player.objects.filter(reclub_profile_url__iexact=reclub_profile_url.strip()).exclude(
        reclub_profile_url=""
    ).exists():
        raise ValidationError("This ReClub profile is already in club records. Ask an admin to link your account.")
    user = User.objects.create_user(email=email, password=password)
    player = Player(
        user=user, display_name=full_name.strip(), phone=phone.strip(),
        reclub_name=reclub_name.strip(), reclub_profile_url=reclub_profile_url.strip(),
    )
    player.save()
    _audit("MEMBER_SIGNED_UP", user, user)
    _audit("PLAYER_CREATED", player, user, kind="account_linked")
    return user


@transaction.atomic
def link_player_account(*, player_id, user_id, actor):
    """An explicit ADMIN action; email matching never calls this automatically."""
    require_admin(actor)
    user = get_user_model().objects.select_for_update().get(pk=user_id)
    player = Player.objects.select_for_update().get(pk=player_id)
    if player.user_id is not None:
        raise ValidationError("Player is already linked to an account")
    if Player.objects.filter(user=user).exists():
        raise ValidationError("Account is already linked to a player")
    player.user = user
    player.contact_email = None
    player.save()
    _audit("PLAYER_LINKED", player, actor, user_id=user.pk)
    return player


def find_players(*, term, actor):
    """Search candidates; the caller must select a Player explicitly."""
    require_organizer(actor)
    term = term.strip()
    if not term:
        return Player.objects.none()
    return Player.objects.filter(
        Q(display_name__icontains=term)
        | Q(phone__icontains=term)
        | Q(contact_email__icontains=term)
        | Q(user__email__icontains=term)
        | Q(reclub_name__icontains=term)
    ).distinct()


def players_with_attended_counts(players):
    """Annotate `attended` on a Player queryset in one query. Same rule as `_counted_attendances`."""
    return players.annotate(attended=Count(
        "attendances",
        filter=Q(attendances__status=Attendance.Status.VALID)
        & ~Q(attendances__openplay__status=OpenPlay.Status.CANCELLED),
        distinct=True,
    ))


def find_walk_in_matches(*, display_name, phone, contact_email, actor, reclub_profile_url=""):
    """Exact candidate matches for the search-first UI; never claim an identity."""
    require_organizer(actor)
    criteria = Q(display_name__iexact=display_name.strip())
    if phone and phone.strip():
        criteria |= Q(phone=phone.strip())
    if contact_email and contact_email.strip():
        email = contact_email.strip()
        criteria |= Q(contact_email__iexact=email) | Q(user__email__iexact=email)
    if reclub_profile_url and reclub_profile_url.strip():
        criteria |= Q(reclub_profile_url__iexact=reclub_profile_url.strip())
    return Player.objects.filter(criteria).select_related("user").distinct().order_by("display_name", "pk")[:20]


@transaction.atomic
def create_venue(*, name, actor):
    require_organizer(actor)
    venue = Venue(name=name.strip())
    venue.full_clean()
    venue.save()
    _audit("VENUE_CREATED", venue, actor)
    return venue


@transaction.atomic
def create_openplay(*, venue_id, starts_at, ends_at, actor, name=""):
    require_organizer(actor)
    if not timezone.is_aware(starts_at) or not timezone.is_aware(ends_at):
        raise ValidationError("OpenPlay times must include a timezone")
    venue = Venue.objects.get(pk=venue_id)
    if not venue.is_active:
        raise ValidationError("Cannot schedule at an inactive venue")
    openplay = OpenPlay(venue=venue, starts_at=starts_at, ends_at=ends_at, name=name.strip())
    openplay.full_clean()
    openplay.save()
    _audit("OPENPLAY_CREATED", openplay, actor, name=openplay.name)
    return openplay


@transaction.atomic
def rename_openplay(*, openplay_id, name, actor):
    """Change only the label of an OpenPlay. A name is required; older unnamed sessions keep showing
    "OpenPlay at <venue>" until they are given one."""
    require_organizer(actor)
    name = name.strip()
    if not name:
        raise ValidationError("A session name is required")
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    if name == openplay.display_name:
        return openplay  # nothing changed (the editor opens with the current or default text)
    previous_name = openplay.name
    openplay.name = name
    openplay.full_clean(exclude=["venue", "starts_at", "ends_at"])
    openplay.save(update_fields=["name", "updated_at"])
    _audit("OPENPLAY_RENAMED", openplay, actor, previous_name=previous_name, name=name)
    return openplay


@transaction.atomic
def complete_openplay(*, openplay_id, actor, now=None):
    require_organizer(actor)
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    now = now or timezone.now()
    if not timezone.is_aware(now):
        raise ValidationError("Completion time must include a timezone")
    if openplay.status != OpenPlay.Status.SCHEDULED:
        raise ValidationError("Only a scheduled OpenPlay can be completed")
    if now < openplay.ends_at:
        raise ValidationError("An OpenPlay cannot be completed before its scheduled end")
    openplay.status = OpenPlay.Status.COMPLETED
    openplay.completed_at = now
    openplay.save(update_fields=["status", "completed_at", "updated_at"])
    _audit("OPENPLAY_COMPLETED", openplay, actor)
    _reconcile_attendees(openplay, actor)
    return openplay


@transaction.atomic
def cancel_openplay(*, openplay_id, actor, reason):
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    reason = reason.strip()
    if not reason:
        raise ValidationError("A cancellation reason is required")
    if openplay.status == OpenPlay.Status.COMPLETED:
        require_admin(actor)
    else:
        require_organizer(actor)
    if openplay.status == OpenPlay.Status.CANCELLED:
        raise ValidationError("OpenPlay is already cancelled")
    previous_status = openplay.status
    openplay.status = OpenPlay.Status.CANCELLED
    openplay.cancelled_at = timezone.now()
    openplay.cancellation_reason = reason
    openplay.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])
    _audit("OPENPLAY_CANCELLED", openplay, actor, previous_status=previous_status, reason=reason)
    _reconcile_attendees(openplay, actor)
    return openplay


def _reconcile_attendees(openplay, actor):
    from .loyalty import reconcile_player_loyalty

    player_ids = list(
        Attendance.objects.filter(openplay=openplay, status=Attendance.Status.VALID)
        .order_by("player_id")
        .values_list("player_id", flat=True)
        .distinct()
    )
    # Every completion/cancellation takes affected Player locks in the same order.
    players = list(Player.objects.select_for_update().filter(pk__in=player_ids).order_by("pk"))
    for player in players:
        reconcile_player_loyalty(player, actor=actor)


@transaction.atomic
def register_player(*, openplay_id, player_id, actor):
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    player = Player.objects.get(pk=player_id)
    _require_self_or_organizer(actor, player)
    if openplay.status != OpenPlay.Status.SCHEDULED or timezone.now() >= openplay.starts_at:
        raise ValidationError("Registration is closed")
    registration, created = Registration.objects.get_or_create(player=player, openplay=openplay)
    if created:
        _audit("REGISTRATION_CREATED", registration, actor)
    elif registration.status == Registration.Status.CANCELLED:
        registration.status = Registration.Status.ACTIVE
        registration.cancelled_at = None
        registration.save(update_fields=["status", "cancelled_at", "updated_at"])
        _audit("REGISTRATION_REACTIVATED", registration, actor)
    return registration


@transaction.atomic
def cancel_registration(*, openplay_id, player_id, actor):
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    player = Player.objects.get(pk=player_id)
    _require_self_or_organizer(actor, player)
    if openplay.status != OpenPlay.Status.SCHEDULED or timezone.now() >= openplay.starts_at:
        raise ValidationError("Registration cancellation is closed")
    registration = Registration.objects.select_for_update().get(player=player, openplay=openplay)
    if registration.status == Registration.Status.ACTIVE:
        registration.status = Registration.Status.CANCELLED
        registration.cancelled_at = timezone.now()
        registration.save(update_fields=["status", "cancelled_at", "updated_at"])
        _audit("REGISTRATION_CANCELLED", registration, actor)
    return registration


def _check_in_locked(*, openplay, player, actor, now):
    if not timezone.is_aware(now):
        raise ValidationError("Check-in time must include a timezone")
    if openplay.status != OpenPlay.Status.SCHEDULED:
        raise ValidationError("Only a scheduled OpenPlay accepts ordinary check-in")
    if now < openplay.starts_at - timedelta(minutes=30) or now > openplay.ends_at:
        raise ValidationError("Check-in is outside the OpenPlay window")
    has_registration = Registration.objects.filter(
        player=player, openplay=openplay, status=Registration.Status.ACTIVE
    ).exists()
    source = Attendance.Source.ROSTER if has_registration else Attendance.Source.WALK_IN
    attendance, created = Attendance.objects.get_or_create(
        player=player,
        openplay=openplay,
        defaults={"source": source, "recorded_by": actor, "checked_in_at": now},
    )
    if attendance.status == Attendance.Status.VOID:
        raise ValidationError("A void attendance requires an ADMIN restoration")
    if created:
        _audit("ATTENDANCE_CHECKED_IN", attendance, actor, source=source)
    return attendance, created


@transaction.atomic
def check_in(*, openplay_id, player_id, actor, now=None):
    require_organizer(actor)
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    player = Player.objects.get(pk=player_id)
    return _check_in_locked(openplay=openplay, player=player, actor=actor, now=now or timezone.now())


@transaction.atomic
def check_in_walk_in(*, openplay_id, display_name, actor, phone="", contact_email=None, now=None):
    require_organizer(actor)
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    now = now or timezone.now()
    if openplay.status != OpenPlay.Status.SCHEDULED:
        raise ValidationError("Only a scheduled OpenPlay accepts ordinary check-in")
    if now < openplay.starts_at - timedelta(minutes=30) or now > openplay.ends_at:
        raise ValidationError("Check-in is outside the OpenPlay window")
    player = _create_walk_in(display_name, phone, contact_email, actor)
    attendance, _ = _check_in_locked(openplay=openplay, player=player, actor=actor, now=now)
    return player, attendance


@transaction.atomic
def submit_member_attendance(*, openplay_id, actor, now=None):
    """A member's claim is inert until an ADMIN approves it."""
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("An active account is required")
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    player = Player.objects.select_for_update().get(user=actor)
    now = now or timezone.now()
    if openplay.status == OpenPlay.Status.CANCELLED or now < openplay.starts_at:
        raise ValidationError("Choose an OpenPlay that has started and was not cancelled")
    if not player.reclub_name or not player.reclub_profile_url:
        raise ValidationError("Complete your ReClub details in your profile before submitting attendance")
    return _submit_attendance_locked(
        openplay=openplay, player=player, actor=actor, now=now,
        full_name=player.display_name, email=actor.email, phone=player.phone,
        reclub_name=player.reclub_name, reclub_profile_url=player.reclub_profile_url,
    )


@transaction.atomic
def submit_staff_attendance(*, openplay_id, player_id, actor, now=None):
    """Staff may help an existing player submit, but approval remains an ADMIN action."""
    require_organizer(actor)
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    player = Player.objects.select_for_update().get(pk=player_id)
    now = now or timezone.now()
    if openplay.status == OpenPlay.Status.CANCELLED or now < openplay.starts_at:
        raise ValidationError("Choose an OpenPlay that has started and was not cancelled")
    if player.user_id is None:
        raise ValidationError("This player must sign up and have a linked account before submitting attendance")
    if not player.reclub_name or not player.reclub_profile_url:
        raise ValidationError("This player needs ReClub details before attendance can be submitted")
    return _submit_attendance_locked(
        openplay=openplay, player=player, actor=actor, now=now,
        full_name=player.display_name, email=player.user.email,
        phone=player.phone, reclub_name=player.reclub_name,
        reclub_profile_url=player.reclub_profile_url,
    )


def _submit_attendance_locked(*, openplay, player, actor, now, full_name, email, phone,
                              reclub_name, reclub_profile_url):
    if player.user_id is None:
        raise ValidationError("A linked account is required before submitting attendance")
    existing_attendance = Attendance.objects.filter(player=player, openplay=openplay).first()
    if existing_attendance and existing_attendance.status == Attendance.Status.VOID:
        raise ValidationError("A void attendance requires an ADMIN restoration")
    if existing_attendance:
        raise ValidationError("Attendance already exists for this OpenPlay")
    submission, created = AttendanceSubmission.objects.select_for_update().get_or_create(
        player=player, openplay=openplay,
        defaults={
            "claimed_full_name": full_name,
            "claimed_email": email,
            "claimed_phone": phone,
            "reclub_name": reclub_name,
            "reclub_profile_url": reclub_profile_url,
            "submitted_by": actor,
            "submitted_at": now,
        },
    )
    if created:
        _audit("ATTENDANCE_SUBMITTED", submission, actor, player_id=player.pk, openplay_id=openplay.pk)
    elif submission.status == AttendanceSubmission.Status.DECLINED:
        submission.status = AttendanceSubmission.Status.PENDING
        submission.claimed_full_name = full_name
        submission.claimed_email = email
        submission.claimed_phone = phone
        submission.reclub_name = reclub_name
        submission.reclub_profile_url = reclub_profile_url
        submission.submitted_by = actor
        submission.submitted_at = now
        submission.reviewed_by = None
        submission.reviewed_at = None
        submission.decline_reason = ""
        submission.save(update_fields=[
            "status", "claimed_full_name", "claimed_email", "claimed_phone", "reclub_name",
            "reclub_profile_url", "submitted_by", "submitted_at",
            "reviewed_by", "reviewed_at", "decline_reason", "updated_at",
        ])
        _audit("ATTENDANCE_RESUBMITTED", submission, actor, player_id=player.pk, openplay_id=openplay.pk)
    return submission


@transaction.atomic
def review_attendance_submission(*, submission_id, actor, approve, reason="", now=None):
    require_admin(actor)
    openplay_id = AttendanceSubmission.objects.values_list("openplay_id", flat=True).get(pk=submission_id)
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    player_id = AttendanceSubmission.objects.values_list("player_id", flat=True).get(pk=submission_id)
    player = Player.objects.select_for_update().get(pk=player_id)
    submission = AttendanceSubmission.objects.select_for_update().get(pk=submission_id)
    if submission.status != AttendanceSubmission.Status.PENDING:
        raise ValidationError("Only a pending submission can be reviewed")
    now = now or timezone.now()
    if approve:
        if player.user_id is None:
            raise ValidationError("Link this player to a signed-up account before approving attendance")
        if openplay.status == OpenPlay.Status.CANCELLED or now < openplay.starts_at:
            raise ValidationError("This OpenPlay cannot receive approved attendance")
        attendance, _ = Attendance.objects.get_or_create(
            player=player, openplay=openplay,
            defaults={
                "source": Attendance.Source.CLAIM,
                "checked_in_at": submission.submitted_at,
                "recorded_by": actor,
            },
        )
        if attendance.status == Attendance.Status.VOID:
            raise ValidationError("Void attendance requires an ADMIN restoration")
        submission.status = AttendanceSubmission.Status.APPROVED
        submission.attendance = attendance
        submission.reviewed_by = actor
        submission.reviewed_at = now
        submission.save(update_fields=["status", "attendance", "reviewed_by", "reviewed_at", "updated_at"])
        _audit("ATTENDANCE_APPROVED", submission, actor, player_id=player.pk, openplay_id=openplay.pk,
               attendance_id=attendance.pk)
        if openplay.status == OpenPlay.Status.COMPLETED:
            from .loyalty import reconcile_player_loyalty

            reconcile_player_loyalty(player, actor=actor)
    else:
        reason = reason.strip()
        if not reason:
            raise ValidationError("Give a reason for declining attendance")
        submission.status = AttendanceSubmission.Status.DECLINED
        submission.reviewed_by = actor
        submission.reviewed_at = now
        submission.decline_reason = reason
        submission.save(update_fields=["status", "reviewed_by", "reviewed_at", "decline_reason", "updated_at"])
        _audit("ATTENDANCE_DECLINED", submission, actor, player_id=player.pk, openplay_id=openplay.pk,
               reason=reason)
    return submission


@transaction.atomic
def add_historical_attendance(*, openplay_id, player_id, actor, reason):
    require_admin(actor)
    reason = reason.strip()
    if not reason:
        raise ValidationError("A correction reason is required")
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    if openplay.status != OpenPlay.Status.COMPLETED:
        raise ValidationError("Historical attendance requires a completed OpenPlay")
    player = Player.objects.get(pk=player_id)
    if Attendance.objects.filter(openplay=openplay, player=player).exists():
        raise ValidationError("Attendance already exists; use restoration for a void record")
    attendance = Attendance.objects.create(
        player=player,
        openplay=openplay,
        source=Attendance.Source.HISTORICAL,
        checked_in_at=openplay.starts_at,
        recorded_by=actor,
    )
    _audit("ATTENDANCE_ADDED", attendance, actor, reason=reason)
    from .loyalty import reconcile_player_loyalty

    reconcile_player_loyalty(player, actor=actor)
    return attendance


@transaction.atomic
def void_attendance(*, attendance_id, actor, reason):
    require_admin(actor)
    reason = reason.strip()
    if not reason:
        raise ValidationError("A correction reason is required")
    openplay_id = Attendance.objects.values_list("openplay_id", flat=True).get(pk=attendance_id)
    OpenPlay.objects.select_for_update().get(pk=openplay_id)
    attendance = Attendance.objects.select_for_update().get(pk=attendance_id)
    if attendance.status != Attendance.Status.VALID:
        raise ValidationError("Only valid attendance can be voided")
    attendance.status = Attendance.Status.VOID
    attendance.voided_at = timezone.now()
    attendance.voided_by = actor
    attendance.void_reason = reason
    attendance.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    _audit("ATTENDANCE_VOIDED", attendance, actor, reason=reason)
    from .loyalty import reconcile_player_loyalty

    reconcile_player_loyalty(attendance.player, actor=actor)
    return attendance


@transaction.atomic
def restore_attendance(*, attendance_id, actor, reason):
    require_admin(actor)
    reason = reason.strip()
    if not reason:
        raise ValidationError("A correction reason is required")
    openplay_id = Attendance.objects.values_list("openplay_id", flat=True).get(pk=attendance_id)
    OpenPlay.objects.select_for_update().get(pk=openplay_id)
    attendance = Attendance.objects.select_for_update().get(pk=attendance_id)
    if attendance.status != Attendance.Status.VOID:
        raise ValidationError("Only void attendance can be restored")
    attendance.status = Attendance.Status.VALID
    attendance.voided_at = None
    attendance.voided_by = None
    attendance.void_reason = ""
    attendance.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    _audit("ATTENDANCE_RESTORED", attendance, actor, reason=reason)
    from .loyalty import reconcile_player_loyalty

    reconcile_player_loyalty(attendance.player, actor=actor)
    return attendance


def _counted_attendances(player):
    """The one definition of what counts: VALID attendance at a non-cancelled OpenPlay."""
    return Attendance.objects.filter(player=player, status=Attendance.Status.VALID).exclude(
        openplay__status=OpenPlay.Status.CANCELLED
    )


def openplays_attended(player):
    """The current count is always derived from the attendance ledger."""
    return _counted_attendances(player).count()


def counted_openplay_starts(player, start, end):
    """Start times of the counted OpenPlays inside [start, end), for the attendance calendar."""
    return list(
        _counted_attendances(player)
        .filter(openplay__starts_at__gte=start, openplay__starts_at__lt=end)
        .values_list("openplay__starts_at", flat=True)
    )
