from django.core.exceptions import PermissionDenied


ORGANIZER = "ORGANIZER"
ADMIN = "ADMIN"


def is_player(user):
    """Player access is own-record access for any active authenticated account."""
    return bool(user and user.is_authenticated and user.is_active)


def is_admin(user):
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and (user.is_superuser or user.groups.filter(name=ADMIN).exists())
    )


def is_organizer(user):
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and (is_admin(user) or user.groups.filter(name=ORGANIZER).exists())
    )


def require_admin(user):
    if not is_admin(user):
        raise PermissionDenied("Administrator access is required")


def require_organizer(user):
    if not is_organizer(user):
        raise PermissionDenied("Organizer access is required")
