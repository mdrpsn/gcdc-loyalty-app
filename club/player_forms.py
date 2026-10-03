from django import forms

from .models import Player


class PlayerProfileForm(forms.Form):
    display_name = forms.CharField(max_length=120, label="Display name")
    phone = forms.CharField(max_length=40, required=False, label="Phone (optional)")

    def __init__(self, *args, player: Player | None = None, **kwargs):
        if player is not None and not args and "initial" not in kwargs:
            kwargs["initial"] = {"display_name": player.display_name, "phone": player.phone}
        super().__init__(*args, **kwargs)

    def clean_display_name(self):
        value = self.cleaned_data["display_name"].strip()
        if not value:
            raise forms.ValidationError("Enter a display name.")
        return value

    def clean_phone(self):
        return self.cleaned_data["phone"].strip()
