"""Conservative JSON logging for production application failures.

The formatter deliberately omits raw messages, exception values, request paths,
query strings, headers, and form data. Those can contain reset tokens or PII.
"""

import json
import logging
import re
from datetime import datetime, timezone


_EVENT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        event = getattr(record, "event", None)
        if not isinstance(event, str) or not _EVENT.fullmatch(event):
            event = "application_error"
        data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": event,
        }
        status = getattr(record, "status_code", None)
        if isinstance(status, int):
            data["status_code"] = status
        request = getattr(record, "request", None)
        if request is not None:
            data["method"] = request.method
            match = getattr(request, "resolver_match", None)
            if match is not None and match.view_name:
                data["route"] = match.view_name
        if record.exc_info and record.exc_info[0]:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, separators=(",", ":"))
