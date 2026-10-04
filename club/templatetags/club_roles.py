"""Template-side role check; authorization is still enforced by the views and services."""

from django import template

from accounts.permissions import is_admin


register = template.Library()


@register.filter
def is_club_admin(user):
    return is_admin(user)


@register.filter
def avatar_initials(user):
    """Up to two initials for the player's display name, falling back to the account email."""
    player = getattr(user, "player", None)
    words = (player.display_name if player else "").split()
    if words:
        return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()
    return (getattr(user, "email", "") or "?")[0].upper()
