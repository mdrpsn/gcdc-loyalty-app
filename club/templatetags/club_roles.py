"""Template-side role check; authorization is still enforced by the views and services."""

from django import template

from accounts.permissions import is_admin


register = template.Library()


@register.filter
def is_club_admin(user):
    return is_admin(user)
