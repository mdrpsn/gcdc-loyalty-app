from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

    @staticmethod
    def normalize_login_email(email):
        if not email or not email.strip():
            raise ValueError("An email address is required")
        return email.strip().lower()

    def get_by_natural_key(self, email):
        return self.get(email__iexact=self.normalize_login_email(email))

    def create_user(self, email, password=None, **extra_fields):
        user = self.model(email=self.normalize_login_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields["is_staff"] is not True or extra_fields["is_superuser"] is not True:
            raise ValueError("A superuser must have is_staff and is_superuser set")
        return self.create_user(email, password, **extra_fields)

