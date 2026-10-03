from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .forms import UserChangeForm, UserCreationForm
from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    ordering = ("email",)
    list_display = ("email", "is_staff", "is_active", "is_superuser")
    list_filter = ("is_staff", "is_active", "is_superuser", "groups")
    search_fields = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),
    )

    def save_model(self, request, obj, form, change):
        from club.models import AuditEvent

        previous = None
        if change:
            previous = type(obj).objects.values(
                "email", "is_active", "is_staff", "is_superuser"
            ).get(pk=obj.pk)
        super().save_model(request, obj, form, change)
        current = {field: getattr(obj, field) for field in ("email", "is_active", "is_staff", "is_superuser")}
        changed = {field: {"before": previous[field], "after": value}
                   for field, value in current.items() if previous is not None and previous[field] != value}
        if previous is None or changed:
            AuditEvent.objects.create(
                actor=request.user, action="USER_ACCOUNT_CREATED" if previous is None else "USER_ACCOUNT_CHANGED",
                target_type="user", target_id=obj.pk, details={"changes": changed},
            )

    def save_related(self, request, form, formsets, change):
        from club.models import AuditEvent

        user = form.instance
        before_groups = sorted(user.groups.values_list("name", flat=True))
        before_permissions = sorted(user.user_permissions.values_list("codename", flat=True))
        super().save_related(request, form, formsets, change)
        after_groups = sorted(user.groups.values_list("name", flat=True))
        after_permissions = sorted(user.user_permissions.values_list("codename", flat=True))
        if before_groups != after_groups or before_permissions != after_permissions:
            AuditEvent.objects.create(
                actor=request.user, action="USER_ROLES_CHANGED", target_type="user", target_id=user.pk,
                details={
                    "groups_before": before_groups, "groups_after": after_groups,
                    "permissions_before": before_permissions, "permissions_after": after_permissions,
                },
            )
