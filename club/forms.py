from django import forms

from .models import OpenPlay, Venue

LOCAL_DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"]


def _local_datetime_field(label):
    # Naive input is read in the current (club) timezone; ambiguous or
    # nonexistent DST clock times are rejected rather than guessed.
    return forms.DateTimeField(
        label=label,
        input_formats=LOCAL_DATETIME_FORMATS,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )


class VenueForm(forms.Form):
    name = forms.CharField(max_length=120, label="Venue name")

    def clean_name(self):
        value = self.cleaned_data["name"].strip()
        if not value:
            raise forms.ValidationError("Enter a venue name.")
        return value


class OpenPlayNameForm(forms.Form):
    name = forms.CharField(
        max_length=80, required=False, label="Session name (optional)",
        help_text="Players see this name. Leave blank to use “OpenPlay at” plus the venue.",
    )


class OpenPlayForm(OpenPlayNameForm):
    field_order = ["venue", "name", "starts_at", "ends_at"]
    venue = forms.ModelChoiceField(queryset=Venue.objects.none(), empty_label="Choose a venue")
    starts_at = _local_datetime_field("Starts")
    ends_at = _local_datetime_field("Ends")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["venue"].queryset = Venue.objects.filter(is_active=True).order_by("name", "pk")

    def clean(self):
        data = super().clean()
        starts_at, ends_at = data.get("starts_at"), data.get("ends_at")
        if starts_at and ends_at and ends_at <= starts_at:
            self.add_error("ends_at", "The end must be after the start.")
        return data


class IdentityLookupForm(forms.Form):
    email = forms.EmailField(label="Account email")


class IdentityLinkForm(forms.Form):
    user_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    verified = forms.BooleanField(label="I verified this person's identity outside the app")


class AuditFilterForm(forms.Form):
    """Every field is optional; an invalid field is dropped by the view, not applied."""

    action = forms.CharField(max_length=60, required=False, label="Action (exact, e.g. REWARD_ISSUED)")
    target_type = forms.CharField(max_length=40, required=False, label="Target type (e.g. player)")
    target_id = forms.IntegerField(min_value=1, max_value=2**63 - 1, required=False, label="Target id")
    actor = forms.EmailField(required=False, label="Actor email")

    def clean_action(self):
        return self.cleaned_data["action"].strip().upper()

    def clean_target_type(self):
        return self.cleaned_data["target_type"].strip().lower()


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={"rows": 3}))


class CorrectionForm(ReasonForm):
    action = forms.ChoiceField(choices=[("VOID", "Void attendance"), ("RESTORE", "Restore attendance")])


class RedemptionForm(forms.Form):
    openplay = forms.ModelChoiceField(queryset=OpenPlay.objects.none(), label="Apply to OpenPlay")
    expired_override_reason = forms.CharField(
        max_length=300, required=False, label="Reason for the admin override",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, openplay_required=True, override_required=False, **kwargs):
        super().__init__(*args, **kwargs)
        from django.utils import timezone

        self.override_required = override_required
        self.fields["openplay"].required = openplay_required
        if not openplay_required:
            self.fields["openplay"].label = "Apply to OpenPlay (optional)"
        self.fields["openplay"].queryset = (
            OpenPlay.objects.filter(status=OpenPlay.Status.SCHEDULED, ends_at__gte=timezone.now())
            .select_related("venue")
            .order_by("starts_at")
        )

    def clean(self):
        data = super().clean()
        if self.override_required and not (data.get("expired_override_reason") or "").strip():
            self.add_error("expired_override_reason", "Enter the reason for redeeming an expired reward.")
        return data


class ExtensionForm(forms.Form):
    new_expires_at = _local_datetime_field("New expiry")
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={"rows": 3}))
