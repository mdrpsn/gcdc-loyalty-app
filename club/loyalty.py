"""Derived loyalty queries and transactional milestone entitlements."""

from dataclasses import dataclass
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.permissions import require_organizer

from .models import (
    Attendance,
    AuditEvent,
    LoyaltyLevel,
    LoyaltyMilestone,
    OpenPlay,
    Player,
    RewardIssuance,
    RewardRedemption,
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
        earned_milestones=tuple(
            LoyaltyMilestone.objects.filter(issuances__player=player).order_by("threshold")
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
        issuance.milestone_id: issuance
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
        if milestone.pk in existing:
            continue
        issuance = RewardIssuance.objects.create(
            player=player,
            milestone=milestone,
            terms_snapshot={
                "threshold": milestone.threshold,
                "name": milestone.name,
                "benefits": deepcopy(milestone.benefits),
            },
            earned_at=now,
        )
        existing[milestone.pk] = issuance
        _audit_reward("REWARD_ISSUED", issuance, actor, completed_attendances=completed)
    return tuple(existing.values())


@transaction.atomic
def redeem_reward(*, issuance_id, player_id, actor, openplay_id=None, details=None):
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
    redemption = RewardRedemption.objects.create(
        issuance=issuance,
        player_id=player_id,
        openplay=openplay,
        redeemed_by=actor,
        redeemed_at=now,
        details=details or {},
    )
    issuance.status = RewardIssuance.Status.REDEEMED
    issuance.save(update_fields=["status", "updated_at"])
    _audit_reward("REWARD_REDEEMED", issuance, actor, redemption_id=redemption.pk, openplay_id=openplay_id)
    return redemption
