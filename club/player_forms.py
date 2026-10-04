from django import forms

from .models import OpenPlay, Player


class PlayerProfileForm(forms.Form):
    display_name = forms.CharField(max_length=120, label="Full name")
    phone = forms.CharField(max_length=40, label="Contact number")
    reclub_name = forms.CharField(max_length=120, label="ReClub name")
    reclub_profile_url = forms.URLField(max_length=500, label="ReClub profile link")

    def __init__(self, *args, player: Player | None = None, **kwargs):
        if player is not None and not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "display_name": player.display_name, "phone": player.phone,
                "reclub_name": player.reclub_name, "reclub_profile_url": player.reclub_profile_url,
            }
        super().__init__(*args, **kwargs)

    def clean_display_name(self):
        value = self.cleaned_data["display_name"].strip()
        if not value:
            raise forms.ValidationError("Enter a display name.")
        return value

    def clean_phone(self):
        return self.cleaned_data["phone"].strip()

    def clean(self):
        data = super().clean()
        if bool(data.get("reclub_name")) != bool(data.get("reclub_profile_url")):
            raise forms.ValidationError("Enter both ReClub fields, or leave both blank.")
        return data


class OpenPlayChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, openplay):
        from django.utils import timezone

        start = timezone.localtime(openplay.starts_at)
        return f"{start:%b %d, %Y} · {openplay.period} · {start:%I:%M %p} · {openplay.venue.name}"


class AttendanceSubmissionForm(forms.Form):
    openplay = OpenPlayChoiceField(queryset=OpenPlay.objects.none(), label="Date played / OpenPlay",
                                   empty_label="Choose the date, time of day, and venue")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.utils import timezone

        self.fields["openplay"].queryset = OpenPlay.objects.filter(
            starts_at__lte=timezone.now(),
        ).exclude(status=OpenPlay.Status.CANCELLED).select_related("venue").order_by("-starts_at", "-pk")
