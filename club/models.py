from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
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

    def __str__(self):
        return self.name


class OpenPlay(HistoricalModel):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="openplays")
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
        return f"OpenPlay {self.pk} at {self.venue}"


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
    terms_snapshot = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AVAILABLE)
    earned_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=300, blank=True)
    # A redeemed entitlement stays redeemed; this records a later eligibility discrepancy.
    eligibility_discrepancy_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["player", "milestone"], name="club_reward_player_milestone_uniq"),
            models.CheckConstraint(
                condition=(Q(status="AVAILABLE", revoked_at__isnull=True, revoked_reason="", eligibility_discrepancy_at__isnull=True))
                | (Q(status="REDEEMED", revoked_at__isnull=True, revoked_reason=""))
                | (Q(status="REVOKED", revoked_at__isnull=False, eligibility_discrepancy_at__isnull=True) & ~Q(revoked_reason="")),
                name="club_reward_state_valid",
            ),
        ]
        indexes = [models.Index(fields=["player", "status"], name="club_reward_player_status_idx")]


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
