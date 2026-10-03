"""Readable, bounded rendering of AuditEvent.details for the ADMIN audit page.

No code path stores secrets in details. Redaction here is defense in depth, and
length is capped because details can hold free-text reasons.
"""

import json
import re

from django import template


register = template.Library()

_SECRET_KEY = re.compile(r"password|passwd|token|secret|hash|credential|api_?key|session", re.IGNORECASE)
MAX_VALUE_LENGTH = 120
MAX_DETAILS_LENGTH = 300


def _redact(value):
    if isinstance(value, dict):
        return {str(key): "[hidden]" if _SECRET_KEY.search(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@register.filter
def audit_details(details):
    if not details:
        return ""
    if not isinstance(details, dict):
        details = {"details": details}
    parts = []
    for key, value in sorted(_redact(details).items()):
        text = _text(value)
        if len(text) > MAX_VALUE_LENGTH:
            text = text[:MAX_VALUE_LENGTH] + "…"
        parts.append(f"{key}: {text}")
    rendered = "; ".join(parts)
    return rendered if len(rendered) <= MAX_DETAILS_LENGTH else rendered[:MAX_DETAILS_LENGTH] + "…"
