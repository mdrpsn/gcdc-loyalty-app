from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
            models.CheckConstraint(condition=~models.Q(email=""), name="accounts_user_email_not_blank"),
        ]

    def clean(self):
        super().clean()
        try:
            self.email = UserManager.normalize_login_email(self.email)
        except ValueError as exc:
            raise ValidationError({"email": str(exc)}) from exc

    def save(self, *args, **kwargs):
        self.email = UserManager.normalize_login_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email
