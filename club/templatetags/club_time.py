"""Show the configured club timezone abbreviation, including DST folds."""

from django import template
from django.utils import dateformat, timezone


register = template.Library()


@register.filter
def club_time(value, format_string="g:i A"):
    if value is None:
        return ""
    local = timezone.localtime(value)
    return f"{dateformat.format(local, format_string)} {local.strftime('%Z')}"
