from django import forms

from .models import OpenPlay


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
