"""Derived loyalty queries and transactional milestone entitlements."""

from dataclasses import dataclass
from copy import deepcopy
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.permissions import require_admin, require_organizer

from .models import (
    Attendance,
    AuditEvent,
    LoyaltyLevel,
    LoyaltyMilestone,
    OpenPlay,
    Player,
    RewardIssuance,
    RewardRedemption,
    RewardRedemptionRequest,
)
from .services import openplays_attended


def get_openplays_attended(player):
    return openplays_attended(player)


def _completed_attendances(player):
    """Only completed attendance can establish a redeemable entitlement."""
    return Attendance.objects.filter(
        player=player,
        status=Attendance.Status.VALID,
        openplay__status=OpenPlay.Status.COMPLETED,
    ).count()


def get_loyalty_level(player):
    count = get_openplays_attended(player)
    return LoyaltyLevel.objects.filter(is_active=True, minimum_attendances__lte=count).order_by(
        "-minimum_attendances"
    ).first()


def get_eligible_milestones(player):
    return LoyaltyMilestone.objects.filter(is_active=True, threshold__lte=_completed_attendances(player)).order_by(
        "threshold"
    )


@dataclass(frozen=True)
class LoyaltySummary:
    openplays_attended: int
    completed_attendances: int
    current_level: LoyaltyLevel | None
    next_milestone: LoyaltyMilestone | None
    remaining_openplays: int | None
    earned_milestones: tuple[LoyaltyMilestone, ...]


def get_loyalty_summary(player):
    completed = _completed_attendances(player)
    next_milestone = LoyaltyMilestone.objects.filter(is_active=True, threshold__gt=completed).order_by(
        "threshold"
    ).first()
    return LoyaltySummary(
        openplays_attended=get_openplays_attended(player),
        completed_attendances=completed,
        current_level=get_loyalty_level(player),
        next_milestone=next_milestone,
        remaining_openplays=next_milestone.threshold - completed if next_milestone else None,
        # Recognition-only milestones have no reward row, so earned status comes from the count.
        earned_milestones=tuple(
            LoyaltyMilestone.objects.filter(
                Q(issuances__player=player) | Q(is_active=True, threshold__lte=completed)
            ).distinct().order_by("threshold")
        ),
    )


@dataclass(frozen=True)
class UpcomingMilestone:
    milestone: LoyaltyMilestone
    remaining: int
    percent: int


def get_upcoming_milestones(player):
    """Active milestones not yet reached by completed attendance, nearest first, with progress."""
    completed = _completed_attendances(player)
    return tuple(
        UpcomingMilestone(
            milestone=milestone,
            remaining=milestone.threshold - completed,
            percent=completed * 100 // milestone.threshold,
        )
        for milestone in LoyaltyMilestone.objects.filter(is_active=True, threshold__gt=completed)
        .exclude(issuances__player=player).order_by("threshold")
    )


def _audit_reward(action, issuance, actor, **details):
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type="rewardissuance",
        target_id=issuance.pk,
        details={"player_id": issuance.player_id, "milestone_id": issuance.milestone_id, **details},
    )


@transaction.atomic
def reconcile_player_loyalty(player, *, actor=None):
    """Serialize by Player; issue from completed attendance and preserve redeemed history.

    The caller may already hold the Player lock. PostgreSQL row locks are reentrant
    within the same transaction. Existing issuance thresholds come from their
    snapshots, so later configuration edits cannot rewrite earned terms.
    """
    player = Player.objects.select_for_update().get(pk=player.pk)
    completed = _completed_attendances(player)
    now = timezone.now()

    existing = {
        (issuance.milestone_id, issuance.benefit_key): issuance
        for issuance in RewardIssuance.objects.select_for_update().filter(player=player).order_by("pk")
    }
    for issuance in existing.values():
        eligible = completed >= issuance.terms_snapshot["threshold"]
        if issuance.status == RewardIssuance.Status.AVAILABLE and not eligible:
            issuance.status = RewardIssuance.Status.REVOKED
            issuance.revoked_at = now
            issuance.revoked_reason = "Completed attendance fell below the earned threshold"
            issuance.save(update_fields=["status", "revoked_at", "revoked_reason", "updated_at"])
            _audit_reward("REWARD_REVOKED", issuance, actor, completed_attendances=completed)
        elif issuance.status == RewardIssuance.Status.REVOKED and eligible:
            issuance.status = RewardIssuance.Status.AVAILABLE
            issuance.revoked_at = None
            issuance.revoked_reason = ""
            issuance.save(update_fields=["status", "revoked_at", "revoked_reason", "updated_at"])
            _audit_reward("REWARD_REINSTATED", issuance, actor, completed_attendances=completed)
        elif issuance.status == RewardIssuance.Status.REDEEMED:
            if not eligible and issuance.eligibility_discrepancy_at is None:
                issuance.eligibility_discrepancy_at = now
                issuance.save(update_fields=["eligibility_discrepancy_at", "updated_at"])
                _audit_reward("REDEEMED_REWARD_DISCREPANCY", issuance, actor, completed_attendances=completed)
            elif eligible and issuance.eligibility_discrepancy_at is not None:
                issuance.eligibility_discrepancy_at = None
                issuance.save(update_fields=["eligibility_discrepancy_at", "updated_at"])
                _audit_reward("REDEEMED_REWARD_ELIGIBILITY_RESTORED", issuance, actor, completed_attendances=completed)

    for milestone in LoyaltyMilestone.objects.filter(is_active=True, threshold__lte=completed).order_by("threshold"):
        if (milestone.pk, "") in existing:
            continue  # a legacy row already covers this whole milestone
        for index, benefit in enumerate(milestone.benefits):
            if benefit["kind"] not in LoyaltyMilestone.REDEEMABLE_KINDS:
                continue
            key = benefit.get("key") or f"benefit-{index}"
            if (milestone.pk, key) in existing:
                continue
            valid_days = benefit.get("valid_days")
            issuance = RewardIssuance.objects.create(
                player=player,
                milestone=milestone,
                benefit_key=key,
                terms_snapshot={
                    "threshold": milestone.threshold,
                    "name": milestone.name,
                    "benefits": [deepcopy(benefit)],
                },
                earned_at=now,
                expires_at=now + timedelta(days=valid_days) if valid_days else None,
            )
            existing[(milestone.pk, key)] = issuance
            _audit_reward("REWARD_ISSUED", issuance, actor, completed_attendances=completed, benefit_key=key)
    return tuple(existing.values())


@transaction.atomic
def redeem_reward(*, issuance_id, player_id, actor, openplay_id=None, details=None, expired_override_reason=None):
    require_organizer(actor)
    # When supplied, lock the OpenPlay first, matching completion/cancellation.
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id) if openplay_id is not None else None
    if openplay is not None and openplay.status == OpenPlay.Status.CANCELLED:
        raise ValidationError("A reward cannot be applied to a cancelled OpenPlay")
    # Remaining lock order matches reconciliation: Player, then RewardIssuance.
    owner_id = RewardIssuance.objects.values_list("player_id", flat=True).get(pk=issuance_id)
    if owner_id != player_id:
        raise ValidationError("This reward belongs to another player")
    Player.objects.select_for_update().get(pk=player_id)
    issuance = RewardIssuance.objects.select_for_update().get(pk=issuance_id)
    if issuance.status != RewardIssuance.Status.AVAILABLE:
        raise ValidationError("Only an available reward can be redeemed")
    if details is not None and not isinstance(details, dict):
        raise ValidationError("Redemption details must be an object")
    now = timezone.now()
    redemption_details = dict(details or {})
    override = {}
    if issuance.is_expired(now):
        reason = (expired_override_reason or "").strip()
        if not reason:
            raise ValidationError("This reward has expired; an admin override with a reason is required")
        require_admin(actor)
        override = {"expired_override": True, "override_reason": reason,
                    "original_expires_at": issuance.expires_at.isoformat()}
        redemption_details.update(override)
    redemption = RewardRedemption.objects.create(
        issuance=issuance,
        player_id=player_id,
        openplay=openplay,
        redeemed_by=actor,
        redeemed_at=now,
        details=redemption_details,
    )
    issuance.status = RewardIssuance.Status.REDEEMED
    issuance.save(update_fields=["status", "updated_at"])
    _audit_reward(
        "REWARD_REDEEMED", issuance, actor, redemption_id=redemption.pk, openplay_id=openplay_id, **override,
    )
    _close_open_requests(issuance, redemption, actor, now)
    return redemption


def is_session_bound(issuance):
    """Discounts and free OpenPlays apply to a session; gear and keepsakes do not."""
    return any(benefit.get("kind") in {"DISCOUNT", "FREE_OPENPLAY"}
               for benefit in issuance.terms_snapshot.get("benefits", []))


def _audit_request(action, request, actor, **details):
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type="rewardredemptionrequest",
        target_id=request.pk,
        details={"player_id": request.player_id, "issuance_id": request.issuance_id,
                 "openplay_id": request.openplay_id, **details},
    )


def _close_open_requests(issuance, redemption, actor, now):
    """A redemption fulfils the open request for the same OpenPlay and releases any other."""
    for request in RewardRedemptionRequest.objects.select_for_update().filter(
        issuance=issuance, status=RewardRedemptionRequest.Status.PENDING,
    ):
        if request.openplay_id == redemption.openplay_id:
            request.status = RewardRedemptionRequest.Status.APPROVED
            request.reviewed_by = actor
            request.reviewed_at = now
            request.redemption = redemption
            request.save(update_fields=["status", "reviewed_by", "reviewed_at", "redemption", "updated_at"])
            _audit_request("REWARD_REDEMPTION_REQUEST_APPROVED", request, actor, redemption_id=redemption.pk)
        else:
            request.status = RewardRedemptionRequest.Status.RELEASED
            request.save(update_fields=["status", "updated_at"])
            _audit_request("REWARD_REDEMPTION_REQUEST_RELEASED", request, actor,
                           reason="Reward was redeemed for a different OpenPlay")


def _release_stale_requests(issuance, now, actor):
    """Open requests for a cancelled or finished OpenPlay no longer hold the reward."""
    for request in RewardRedemptionRequest.objects.select_for_update().select_related("openplay").filter(
        issuance=issuance, status=RewardRedemptionRequest.Status.PENDING,
    ):
        if request.openplay.status == OpenPlay.Status.CANCELLED or request.openplay.ends_at < now:
            request.status = RewardRedemptionRequest.Status.RELEASED
            request.save(update_fields=["status", "updated_at"])
            _audit_request("REWARD_REDEMPTION_REQUEST_RELEASED", request, actor,
                           reason="The requested OpenPlay ended or was cancelled")


@transaction.atomic
def request_reward_redemption(*, issuance_id, openplay_id, actor):
    """A player asks to use an available discount or free OpenPlay at an upcoming OpenPlay."""
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("An active account is required")
    player = Player.objects.filter(user=actor).first()
    if player is None:
        raise PermissionDenied("A player account is required")
    if RewardIssuance.objects.values_list("player_id", flat=True).get(pk=issuance_id) != player.pk:
        raise ValidationError("This reward belongs to another player")
    # Lock order matches completion and redemption: OpenPlay, Player, RewardIssuance.
    openplay = OpenPlay.objects.select_for_update().get(pk=openplay_id)
    Player.objects.select_for_update().get(pk=player.pk)
    issuance = RewardIssuance.objects.select_for_update().get(pk=issuance_id)
    now = timezone.now()
    if issuance.status != RewardIssuance.Status.AVAILABLE:
        raise ValidationError("Only an available reward can be used")
    if issuance.is_expired(now):
        raise ValidationError("This reward has expired")
    if not is_session_bound(issuance):
        raise ValidationError("This reward is handed over by the club and cannot be requested")
    if openplay.status != OpenPlay.Status.SCHEDULED or openplay.starts_at <= now:
        raise ValidationError("Choose an upcoming OpenPlay")
    if issuance.expires_at is not None and openplay.starts_at > issuance.expires_at:
        raise ValidationError("This reward expires before that OpenPlay")
    _release_stale_requests(issuance, now, actor)
    pending = RewardRedemptionRequest.Status.PENDING
    if RewardRedemptionRequest.objects.filter(issuance=issuance, status=pending).exists():
        raise ValidationError("This reward already has an open request")
    if RewardRedemptionRequest.objects.filter(player=player, openplay=openplay, status=pending).exists():
        raise ValidationError("You already have a reward requested for this OpenPlay")
    request = RewardRedemptionRequest.objects.create(
        issuance=issuance, player=player, openplay=openplay, requested_by=actor, requested_at=now,
    )
    _audit_request("REWARD_REDEMPTION_REQUESTED", request, actor)
    return request


def _lock_request(request_id):
    """Lock in the standard order (OpenPlay, Player, RewardIssuance) and return the request row."""
    ids = RewardRedemptionRequest.objects.values("openplay_id", "player_id", "issuance_id").get(pk=request_id)
    OpenPlay.objects.select_for_update().get(pk=ids["openplay_id"])
    Player.objects.select_for_update().get(pk=ids["player_id"])
    RewardIssuance.objects.select_for_update().get(pk=ids["issuance_id"])
    return RewardRedemptionRequest.objects.select_for_update().select_related("player").get(pk=request_id)


@transaction.atomic
def cancel_redemption_request(*, request_id, actor):
    """The requesting player cancels; an ADMIN may instead release a request they will not fulfil."""
    request = _lock_request(request_id)
    if request.status != RewardRedemptionRequest.Status.PENDING:
        raise ValidationError("Only an open request can be cancelled")
    if actor is not None and actor.is_authenticated and request.player.user_id == actor.pk:
        status, action = RewardRedemptionRequest.Status.CANCELLED, "REWARD_REDEMPTION_REQUEST_CANCELLED"
    else:
        require_admin(actor)
        status, action = RewardRedemptionRequest.Status.RELEASED, "REWARD_REDEMPTION_REQUEST_RELEASED"
    request.status = status
    request.save(update_fields=["status", "updated_at"])
    _audit_request(action, request, actor)
    return request


@transaction.atomic
def review_redemption_request(*, request_id, actor, approve, reason=""):
    """ADMIN verifies the request. Approving performs the real redemption for that OpenPlay."""
    require_admin(actor)
    request = _lock_request(request_id)
    if request.status != RewardRedemptionRequest.Status.PENDING:
        raise ValidationError("Only an open request can be reviewed")
    if approve:
        # redeem_reward marks this request approved and links the redemption.
        return redeem_reward(
            issuance_id=request.issuance_id, player_id=request.player_id, actor=actor,
            openplay_id=request.openplay_id,
        )
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A decline reason is required")
    request.status = RewardRedemptionRequest.Status.DECLINED
    request.reviewed_by = actor
    request.reviewed_at = timezone.now()
    request.decline_reason = reason
    request.save(update_fields=["status", "reviewed_by", "reviewed_at", "decline_reason", "updated_at"])
    _audit_request("REWARD_REDEMPTION_REQUEST_DECLINED", request, actor, reason=reason)
    return request


@transaction.atomic
def extend_reward_expiry(*, issuance_id, new_expires_at, reason, actor):
    """An explicit, audited ADMIN action; restoring a revoked reward never changes its expiry."""
    require_admin(actor)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("An extension reason is required")
    if not timezone.is_aware(new_expires_at):
        raise ValidationError("The new expiry must include a timezone")
    player_id = RewardIssuance.objects.values_list("player_id", flat=True).get(pk=issuance_id)
    Player.objects.select_for_update().get(pk=player_id)
    issuance = RewardIssuance.objects.select_for_update().get(pk=issuance_id)
    if issuance.status == RewardIssuance.Status.REDEEMED:
        raise ValidationError("A redeemed reward cannot be extended")
    if issuance.expires_at is None:
        raise ValidationError("This reward has no expiry")
    if new_expires_at <= issuance.expires_at:
        raise ValidationError("The new expiry must be later than the current expiry")
    previous = issuance.expires_at
    issuance.expires_at = new_expires_at
    issuance.save(update_fields=["expires_at", "updated_at"])
    _audit_reward(
        "REWARD_EXPIRY_EXTENDED", issuance, actor, reason=reason,
        previous_expires_at=previous.isoformat(), new_expires_at=new_expires_at.isoformat(),
    )
    return issuance
