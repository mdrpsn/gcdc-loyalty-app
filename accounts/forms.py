from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import BaseUserCreationForm, UserChangeForm as DjangoUserChangeForm

from .models import User


class UserCreationForm(BaseUserCreationForm):
    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ("email",)


class UserChangeForm(DjangoUserChangeForm):
    class Meta(DjangoUserChangeForm.Meta):
        model = User
        fields = "__all__"


class SignupEmailForm(forms.Form):
    email = forms.EmailField(label="Email address")

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class MemberSignupForm(forms.Form):
    full_name = forms.CharField(max_length=120, label="Full name")
    phone = forms.CharField(max_length=40, label="Contact number")
    reclub_name = forms.CharField(max_length=120, label="ReClub name")
    reclub_profile_url = forms.URLField(max_length=500, label="ReClub profile link")
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput)

    def __init__(self, *args, verified_email, **kwargs):
        super().__init__(*args, **kwargs)
        self.verified_email = verified_email

    def clean(self):
        data = super().clean()
        for field in ("full_name", "phone", "reclub_name", "reclub_profile_url"):
            if data.get(field):
                data[field] = data[field].strip()
        if data.get("password1") and data.get("password2"):
            if data["password1"] != data["password2"]:
                self.add_error("password2", "Passwords do not match.")
            else:
                password_validation.validate_password(
                    data["password1"], user=User(email=self.verified_email)
                )
        return data

