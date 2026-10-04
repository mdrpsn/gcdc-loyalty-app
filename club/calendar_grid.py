"""Month grid for the player calendars. Pure date logic: callers supply the days to mark."""

import calendar
from datetime import date, datetime, time

from django.utils import timezone

MAX_YEARS_AWAY = 5


def parse_month(raw, today):
    """First day of the requested YYYY-MM month; anything invalid or far away falls back to today's month."""
    fallback = today.replace(day=1)
    try:
        year, month = (int(part) for part in str(raw).split("-"))
        first = date(year, month, 1)
    except (TypeError, ValueError):
        return fallback
    return first if abs(first.year - today.year) <= MAX_YEARS_AWAY else fallback


def shift_month(first, delta):
    index = first.year * 12 + (first.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def month_bounds(first):
    """Aware [start, end) instants of the month in the current (club) timezone."""
    zone = timezone.get_current_timezone()
    start = datetime.combine(first, time.min, tzinfo=zone)
    end = datetime.combine(shift_month(first, 1), time.min, tzinfo=zone)
    return start, end


def month_grid(first, marked, today):
    """Sunday-first weeks of day cells; days outside the month are None placeholders."""
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    return [
        [
            {"day": day.day, "mark": marked.get(day), "today": day == today} if day.month == first.month else None
            for day in week
        ]
        for week in weeks
    ]


def month_context(first, marked, today):
    return {
        "label": first.strftime("%B %Y"),
        "month_name": first.strftime("%B"),
        "weeks": month_grid(first, marked, today),
        "previous": shift_month(first, -1).strftime("%Y-%m"),
        "next": shift_month(first, 1).strftime("%Y-%m"),
        "is_current": first == today.replace(day=1),
        "weekdays": ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"),
    }


def local_date(moment):
    return timezone.localtime(moment).date()
