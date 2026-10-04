"""Password policy shared by signup, reset, and Django admin forms."""

import re

from django.core.exceptions import ValidationError


class AlphanumericPasswordValidator:
    message = "Password must contain at least 6 letters and numbers, including a letter and a number."

    def validate(self, password, user=None):
        if (len(re.findall(r"[A-Za-z0-9]", password)) < 6
                or not re.search(r"[A-Za-z]", password)
                or not re.search(r"[0-9]", password)):
            raise ValidationError(self.message, code="password_alphanumeric")

    def get_help_text(self):
        return "Use at least 6 letters and numbers, including a letter and a number. Symbols are allowed."
