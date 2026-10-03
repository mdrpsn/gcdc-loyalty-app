import logging

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


logger = logging.getLogger("config.health")


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.error("database_unavailable", extra={"event": "database_unavailable"})
        response = JsonResponse({"status": "unhealthy"}, status=503)
    else:
        response = JsonResponse({"status": "ok"})
    response["Cache-Control"] = "no-store"
    return response
