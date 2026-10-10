import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone


class HistoricalQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("Historical club records cannot be deleted")


class HistoricalModel(models.Model):
    objects = models.Manager.from_queryset(HistoricalQuerySet)()

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ValidationError("Historical club records cannot be deleted")


class AuditEventQuerySet(HistoricalQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit events are append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Audit events are append-only")


class Player(HistoricalModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="player",
    )
    display_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=40, blank=True, db_index=True)
    contact_email = models.EmailField(null=True, blank=True)
    reclub_name = models.CharField(max_length=120, blank=True)
    reclub_profile_url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(display_name=""), name="club_player_name_required"),
            models.CheckConstraint(
                condition=(Q(user__isnull=False, contact_email__isnull=True))
                | (Q(user__isnull=True) & (~Q(phone="") | Q(contact_email__isnull=False))),
                name="club_player_identity_required",
            ),
            models.CheckConstraint(
                condition=Q(contact_email__isnull=True) | ~Q(contact_email=""),
                name="club_player_contact_not_blank",
            ),
            models.UniqueConstraint(
                Lower("reclub_profile_url"),
                condition=~Q(reclub_profile_url=""),
                name="club_player_reclub_url_ci_uniq",
                violation_error_message="This ReClub profile is already in club records",
            ),
        ]
        indexes = [models.Index(Lower("contact_email"), name="club_player_email_idx")]

    def clean_fields(self, exclude=None):
        self.contact_email = (self.contact_email or "").strip().lower() or None
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self.display_name = (self.display_name or "").strip()
        self.phone = self.phone.strip()
        errors = {}
        if not self.display_name:
            errors["display_name"] = "A player name is required"
        if self.user_id and self.contact_email:
            errors["contact_email"] = "A linked player uses the User email"
        if not self.user_id and not (self.phone or self.contact_email):
            errors["contact_email"] = "A walk-in needs a phone or provisional email"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name


class Venue(HistoricalModel):
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="club_venue_name_ci_uniq",
                violation_error_message="A venue with this name already exists.",
            ),
        ]

    def __str__(self):
        return self.name


class OpenPlay(HistoricalModel):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="openplays")
    # Optional label chosen by the club; blank means "OpenPlay at <venue>".
    name = models.CharField(max_length=80, blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="club_openplay_times_valid"),
            models.CheckConstraint(
                condition=(Q(status="SCHEDULED", completed_at__isnull=True, cancelled_at__isnull=True, cancellation_reason=""))
                | (Q(status="COMPLETED", completed_at__isnull=False, cancelled_at__isnull=True, cancellation_reason=""))
                | (Q(status="CANCELLED", cancelled_at__isnull=False) & ~Q(cancellation_reason="")),
                name="club_openplay_state_valid",
            ),
        ]
        indexes = [models.Index(fields=["status", "starts_at"], name="club_openplay_status_start_idx")]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.name or f"OpenPlay at {self.venue}"

    @property
    def period(self):
        """Club-local part of day, using the scheduled start time."""
        hour = timezone.localtime(self.starts_at).hour
        return "Morning" if hour < 12 else "Afternoon" if hour < 18 else "Evening"


class Registration(HistoricalModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CANCELLED = "CANCELLED", "Cancelled"

    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="registrations")
    openplay = models.ForeignKey(OpenPlay, on_delete=models.PROTECT, related_name="registrations")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["player", "openplay"], name="club_registration_player_session_uniq"),
            models.CheckConstraint(
                condition=Q(status="ACTIVE", cancelled_at__isnull=True)
                | Q(status="CANCELLED", cancelled_at__isnull=False),
                name="club_registration_state_valid",
            ),
        ]
        indexes = [models.Index(fields=["openplay", "status"], name="club_registration_roster_idx")]


class Attendance(HistoricalModel):
    class Status(models.TextChoices):
        VALID = "VALID", "Valid"
        VOID = "VOID", "Void"

    class Source(models.TextChoices):
        ROSTER = "ROSTER", "Roster"
        WALK_IN = "WALK_IN", "Walk-in"
        HISTORICAL = "HISTORICAL", "Historical correction"
        CLAIM = "CLAIM", "Approved player submission"

    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="attendances")
    openplay = models.ForeignKey(OpenPlay, on_delete=models.PROTECT, related_name="attendances")
    status = models.CharField(max_length=5, choices=Status.choices, default=Status.VALID)
    source = models.CharField(max_length=10, choices=Source.choices)
    checked_in_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recorded_attendances"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="voided_attendances",
    )
    void_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["player", "openplay"], name="club_attendance_player_session_uniq"),
            models.CheckConstraint(
                condition=Q(status="VALID", voided_at__isnull=True, voided_by__isnull=True, void_reason="")
                | (Q(status="VOID", voided_at__isnull=False, voided_by__isnull=False) & ~Q(void_reason="")),
                name="club_attendance_state_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["player", "status"], name="club_attendance_player_idx"),
            models.Index(fields=["openplay", "status"], name="club_attendance_session_idx"),
        ]


class AttendanceSubmission(HistoricalModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"

    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="attendance_submissions")
    openplay = models.ForeignKey(OpenPlay, on_delete=models.PROTECT, related_name="attendance_submissions")
    claimed_full_name = models.CharField(max_length=120, blank=True)
    claimed_email = models.EmailField(blank=True)
    claimed_phone = models.CharField(max_length=40, blank=True)
    reclub_name = models.CharField(max_length=120)
    reclub_profile_url = models.URLField(max_length=500)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="submitted_attendances",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reviewed_attendances",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=300, blank=True)
    attendance = models.OneToOneField(
        Attendance, on_delete=models.PROTECT, null=True, blank=True, related_name="submission",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["player", "openplay"], name="club_submission_player_session_uniq"),
            models.CheckConstraint(
                condition=Q(status="PENDING", reviewed_by__isnull=True, reviewed_at__isnull=True,
                            decline_reason="", attendance__isnull=True)
                | Q(status="APPROVED", reviewed_by__isnull=False, reviewed_at__isnull=False,
                    decline_reason="", attendance__isnull=False)
                | (Q(status="DECLINED", reviewed_by__isnull=False, reviewed_at__isnull=False,
                     attendance__isnull=True) & ~Q(decline_reason="")),
                name="club_submission_state_valid",
            ),
        ]
        indexes = [models.Index(fields=["status", "submitted_at"], name="club_submission_queue_idx")]


class AuditEvent(HistoricalModel):
    objects = models.Manager.from_queryset(AuditEventQuerySet)()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="club_audit_events",
    )
    action = models.CharField(max_length=60)
    target_type = models.CharField(max_length=40)
    target_id = models.PositiveBigIntegerField()
    details = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["target_type", "target_id", "occurred_at"], name="club_audit_target_idx")]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Audit events are append-only")
        super().save(*args, **kwargs)


class LoyaltyLevel(HistoricalModel):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    minimum_attendances = models.PositiveIntegerField(unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["minimum_attendances"]

    def __str__(self):
        return self.name


class LoyaltyMilestone(HistoricalModel):
    class RewardKind(models.TextChoices):
        DISCOUNT = "DISCOUNT", "Discount"
        FREE_OPENPLAY = "FREE_OPENPLAY", "Free OpenPlay"
        RECOGNITION = "RECOGNITION", "Recognition"
        PERK = "PERK", "Perk"
        PREMIUM_REWARD = "PREMIUM_REWARD", "Premium reward"

    # RECOGNITION is status shown from the attendance count; the others become redeemable rewards.
    REDEEMABLE_KINDS = frozenset({
        RewardKind.DISCOUNT, RewardKind.FREE_OPENPLAY, RewardKind.PERK, RewardKind.PREMIUM_REWARD,
    })

    threshold = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    benefits = models.JSONField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["threshold"]
        constraints = [
            models.CheckConstraint(condition=Q(threshold__gt=0), name="club_milestone_positive_threshold"),
        ]

    def clean(self):
        super().clean()
        allowed = set(self.RewardKind.values)
        if not isinstance(self.benefits, list) or not self.benefits:
            raise ValidationError({"benefits": "At least one benefit is required"})
        for benefit in self.benefits:
            if not isinstance(benefit, dict) or benefit.get("kind") not in allowed:
                raise ValidationError({"benefits": "Each benefit needs a supported reward kind"})
            if not isinstance(benefit.get("description"), str) or not benefit["description"].strip():
                raise ValidationError({"benefits": "Each benefit needs a description"})
            if benefit["kind"] == self.RewardKind.DISCOUNT:
                amount = benefit.get("amount_php")
                if type(amount) is not int or amount <= 0:
                    raise ValidationError({"benefits": "Discounts need a positive whole-peso amount"})
            if "valid_days" in benefit:
                days = benefit["valid_days"]
                if benefit["kind"] not in self.REDEEMABLE_KINDS or type(days) is not int or days <= 0:
                    raise ValidationError({"benefits": "valid_days must be a positive whole number on a redeemable benefit"})
        keys = [benefit["key"] for benefit in self.benefits if "key" in benefit]
        if any(not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", key) for key in keys) \
                or len(keys) != len(set(keys)):
            raise ValidationError({"benefits": "Benefit keys must be unique lowercase slugs"})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.threshold}: {self.name}"


class RewardIssuance(HistoricalModel):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        REDEEMED = "REDEEMED", "Redeemed"
        REVOKED = "REVOKED", "Revoked"

    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="reward_issuances")
    milestone = models.ForeignKey(LoyaltyMilestone, on_delete=models.PROTECT, related_name="issuances")
    # One row per redeemable benefit. A blank key marks a legacy row that covers its whole milestone.
    benefit_key = models.CharField(max_length=40, blank=True, default="")
    terms_snapshot = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AVAILABLE)
    earned_at = models.DateTimeField(default=timezone.now)
    # Null means no expiry. "Expired" is derived (AVAILABLE past this moment), never stored.
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=300, blank=True)
    # A redeemed entitlement stays redeemed; this records a later eligibility discrepancy.
    eligibility_discrepancy_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["player", "milestone", "benefit_key"], name="club_reward_player_benefit_uniq"
            ),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gt=F("earned_at")),
                name="club_reward_expiry_after_earned",
            ),
            models.CheckConstraint(
                condition=(Q(status="AVAILABLE", revoked_at__isnull=True, revoked_reason="", eligibility_discrepancy_at__isnull=True))
                | (Q(status="REDEEMED", revoked_at__isnull=True, revoked_reason=""))
                | (Q(status="REVOKED", revoked_at__isnull=False, eligibility_discrepancy_at__isnull=True) & ~Q(revoked_reason="")),
                name="club_reward_state_valid",
            ),
        ]
        indexes = [models.Index(fields=["player", "status"], name="club_reward_player_status_idx")]

    def is_expired(self, now=None):
        return (
            self.status == self.Status.AVAILABLE
            and self.expires_at is not None
            and (now or timezone.now()) > self.expires_at
        )


class RewardRedemptionRequest(HistoricalModel):
    """A player's request to use a session-bound reward at a named OpenPlay.

    It never changes the reward's own status: "requested" means an open request exists, and the
    reward is consumed only when staff redeem it (which approves the matching request).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Requested"
        APPROVED = "APPROVED", "Redeemed"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"
        RELEASED = "RELEASED", "Released"

    issuance = models.ForeignKey(RewardIssuance, on_delete=models.PROTECT, related_name="redemption_requests")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="reward_redemption_requests")
    openplay = models.ForeignKey(OpenPlay, on_delete=models.PROTECT, related_name="reward_redemption_requests")
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reward_redemption_requests"
    )
    requested_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="reviewed_reward_redemption_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=300, blank=True)
    redemption = models.OneToOneField(
        "RewardRedemption", on_delete=models.PROTECT, null=True, blank=True, related_name="request",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["issuance"], condition=Q(status="PENDING"), name="club_redemption_request_one_open_per_reward",
            ),
            models.UniqueConstraint(
                fields=["player", "openplay"], condition=Q(status="PENDING"),
                name="club_redemption_request_one_open_per_session",
            ),
            models.CheckConstraint(
                condition=Q(status="PENDING", reviewed_by__isnull=True, reviewed_at__isnull=True,
                            decline_reason="", redemption__isnull=True)
                | Q(status="APPROVED", reviewed_by__isnull=False, reviewed_at__isnull=False,
                    decline_reason="", redemption__isnull=False)
                | (Q(status="DECLINED", reviewed_by__isnull=False, reviewed_at__isnull=False,
                     redemption__isnull=True) & ~Q(decline_reason=""))
                | Q(status__in=["CANCELLED", "RELEASED"], decline_reason="", redemption__isnull=True),
                name="club_redemption_request_state_valid",
            ),
        ]
        indexes = [models.Index(fields=["status", "requested_at"], name="club_redeem_request_queue_idx")]


class RewardRedemption(HistoricalModel):
    issuance = models.OneToOneField(RewardIssuance, on_delete=models.PROTECT, related_name="redemption")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="reward_redemptions")
    openplay = models.ForeignKey(
        OpenPlay, on_delete=models.PROTECT, null=True, blank=True, related_name="reward_redemptions"
    )
    redeemed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="club_reward_redemptions"
    )
    redeemed_at = models.DateTimeField(default=timezone.now)
    details = models.JSONField(default=dict)
