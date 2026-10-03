"""Production settings for a trusted HTTPS reverse proxy."""

import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
TIME_ZONE = required_env("DJANGO_TIME_ZONE")
try:
    ZoneInfo(TIME_ZONE)
except (ZoneInfoNotFoundError, ValueError) as error:
    raise ImproperlyConfigured("DJANGO_TIME_ZONE must be a valid IANA timezone") from error

ALLOWED_HOSTS = [host.strip() for host in required_env("DJANGO_ALLOWED_HOSTS").split(",") if host.strip()]
if not ALLOWED_HOSTS or any("*" in host or host.startswith(".") or "://" in host or "/" in host
                            for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must contain explicit hostnames")
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in required_env("DJANGO_CSRF_TRUSTED_ORIGINS").split(",")
    if origin.strip()
]
if not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must contain HTTPS origins")
for origin in CSRF_TRUSTED_ORIGINS:
    parsed = urlsplit(origin)
    try:
        valid_port = parsed.port is None or parsed.port > 0
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not parsed.hostname or "*" in parsed.hostname
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password
            or not valid_port):
        raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must contain explicit HTTPS origins")

db_sslmode = required_env("DB_SSLMODE").lower()
if db_sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
    raise ImproperlyConfigured("DB_SSLMODE must be a PostgreSQL SSL mode")
if DATABASES["default"]["HOST"].lower() not in {"localhost", "127.0.0.1", "::1"} and db_sslmode not in {
    "require", "verify-ca", "verify-full"
}:
    raise ImproperlyConfigured("Remote PostgreSQL requires DB_SSLMODE=require or stronger")
DATABASES["default"]["OPTIONS"]["sslmode"] = db_sslmode

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
# Render probes its private HTTP port; a redirect would count as healthy without
# reaching the database check. Public HTTP is redirected by Render's HTTPS edge.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", False)
if env_bool("DJANGO_TRUST_PROXY_SSL", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if os.environ.get("DJANGO_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
    raise ImproperlyConfigured("DEBUG cannot be enabled in production")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = required_env("EMAIL_HOST")
EMAIL_PORT = int(required_env("EMAIL_PORT"))
EMAIL_HOST_USER = required_env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = required_env("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
if EMAIL_USE_TLS == EMAIL_USE_SSL:
    raise ImproperlyConfigured("Exactly one of EMAIL_USE_TLS or EMAIL_USE_SSL must be enabled")
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
if not 1 <= EMAIL_PORT <= 65535 or EMAIL_TIMEOUT <= 0:
    raise ImproperlyConfigured("EMAIL_PORT and EMAIL_TIMEOUT must be positive valid values")
DEFAULT_FROM_EMAIL = required_env("DEFAULT_FROM_EMAIL")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"safe_json": {"()": "config.logging.SafeJsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe_json"}},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "config.health": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
