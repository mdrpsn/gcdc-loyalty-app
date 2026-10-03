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


class OpenPlayForm(forms.Form):
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


class WalkInForm(forms.Form):
    display_name = forms.CharField(max_length=120, label="Player name")
    phone = forms.CharField(max_length=40, required=False)
    contact_email = forms.EmailField(required=False, label="Provisional contact email")
    search_token = forms.CharField(widget=forms.HiddenInput)
    confirm_distinct = forms.BooleanField(
        required=False, label="I checked the matches; this is a different player"
    )

    def clean(self):
        data = super().clean()
        if not data.get("phone") and not data.get("contact_email"):
            raise forms.ValidationError("Enter a phone number or contact email.")
        return data


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={"rows": 3}))


class CorrectionForm(ReasonForm):
    action = forms.ChoiceField(choices=[("VOID", "Void attendance"), ("RESTORE", "Restore attendance")])


class RedemptionForm(forms.Form):
    openplay = forms.ModelChoiceField(queryset=OpenPlay.objects.none(), label="Apply to OpenPlay")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.utils import timezone

        self.fields["openplay"].queryset = (
            OpenPlay.objects.filter(status=OpenPlay.Status.SCHEDULED, ends_at__gte=timezone.now())
            .select_related("venue")
            .order_by("starts_at")
        )
