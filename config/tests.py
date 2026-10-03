from io import StringIO
import json
import logging
import os
import subprocess
import sys
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .logging import SafeJsonFormatter


class FoundationTests(TestCase):
    def test_django_system_checks_pass(self):
        call_command("check", stdout=StringIO())

    def test_database_is_postgresql_and_connection_works(self):
        self.assertEqual(connection.vendor, "postgresql")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
        self.assertTrue(database_name.startswith("test_"), database_name)

    def test_health_reports_database_available(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_health_reports_database_unavailable(self):
        with patch("config.views.connection.cursor", side_effect=OperationalError):
            with self.assertLogs("config.health", level="ERROR") as captured:
                response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertJSONEqual(response.content, {"status": "unhealthy"})
        self.assertIn("database_unavailable", captured.output[0])


class ProductionConfigurationTests(SimpleTestCase):
    def test_safe_formatter_never_emits_messages_secrets_or_raw_paths(self):
        request = type("Request", (), {
            "method": "POST", "path": "/password-reset/secret-token/",
            "resolver_match": type("Match", (), {"view_name": "password_reset_confirm"})(),
        })()
        record = logging.LogRecord(
            "django.request", logging.ERROR, "/password-reset/secret-token/", 1,
            "password=secret-value token=secret-token", (), None,
        )
        record.request = request
        record.status_code = 500
        try:
            raise ValueError("secret-value")
        except ValueError:
            record.exc_info = sys.exc_info()
        data = json.loads(SafeJsonFormatter().format(record))
        self.assertEqual(data["route"], "password_reset_confirm")
        self.assertEqual(data["status_code"], 500)
        self.assertEqual(data["exception_type"], "ValueError")
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("password=", json.dumps(data))

    def test_production_requires_explicit_security_configuration(self):
        env = {**os.environ, "DJANGO_SECRET_KEY": "x" * 60, "DB_NAME": "unused",
               "DB_USER": "unused", "DB_PASSWORD": "unused", "DB_HOST": "127.0.0.1",
               "DB_PORT": "5432", "DB_SSLMODE": "disable",
               "DJANGO_TIME_ZONE": "America/New_York", "DJANGO_ALLOWED_HOSTS": "club.example.invalid",
               "DJANGO_CSRF_TRUSTED_ORIGINS": "https://club.example.invalid",
               "EMAIL_HOST": "smtp.example.invalid", "EMAIL_PORT": "587",
               "EMAIL_HOST_USER": "test@example.invalid", "EMAIL_HOST_PASSWORD": "not-real",
               "DEFAULT_FROM_EMAIL": "test@example.invalid"}
        for key, expected in (
            ("DJANGO_SECRET_KEY", "DJANGO_SECRET_KEY must be set"),
            ("DJANGO_TIME_ZONE", "DJANGO_TIME_ZONE must be set"),
            ("DJANGO_CSRF_TRUSTED_ORIGINS", "DJANGO_CSRF_TRUSTED_ORIGINS must be set"),
            ("DB_SSLMODE", "DB_SSLMODE must be set"),
        ):
            missing = {**env, key: ""}
            result = subprocess.run(
                [sys.executable, "-c", "import config.settings.production"],
                env=missing, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)
        invalid = {**env, "DJANGO_TIME_ZONE": "Not/A_Real_Zone"}
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings.production"],
            env=invalid, capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("valid IANA timezone", result.stderr)
        for changes, message in (
            ({"DJANGO_DEBUG": "true"}, "DEBUG cannot be enabled"),
            ({"EMAIL_USE_TLS": "false"}, "Exactly one"),
            ({"DJANGO_CSRF_TRUSTED_ORIGINS": "http://club.example.invalid"}, "explicit HTTPS origins"),
            ({"DB_HOST": "db.example.invalid"}, "Remote PostgreSQL requires DB_SSLMODE=require or stronger"),
        ):
            result = subprocess.run(
                [sys.executable, "-c", "import config.settings.production"],
                env={**env, **changes}, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)
        encrypted_remote = {**env, "DB_HOST": "db.example.invalid", "DB_SSLMODE": "require"}
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings.production"],
            env=encrypted_remote, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_password_hashing_is_pbkdf2_and_never_the_test_hasher(self):
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.production",
               "DJANGO_SECRET_KEY": "x" * 60, "DB_NAME": "unused", "DB_USER": "unused",
               "DB_PASSWORD": "unused", "DB_HOST": "127.0.0.1", "DB_PORT": "5432", "DB_SSLMODE": "disable",
               "DJANGO_TIME_ZONE": "America/New_York", "DJANGO_ALLOWED_HOSTS": "club.example.invalid",
               "DJANGO_CSRF_TRUSTED_ORIGINS": "https://club.example.invalid",
               "EMAIL_HOST": "smtp.example.invalid", "EMAIL_PORT": "587",
               "EMAIL_HOST_USER": "test@example.invalid", "EMAIL_HOST_PASSWORD": "not-real",
               "DEFAULT_FROM_EMAIL": "test@example.invalid"}
        code = (
            "import json, sys; from django.conf import settings; "
            "print(json.dumps({'hashers': settings.PASSWORD_HASHERS, "
            "'test_settings_loaded': 'config.settings.test' in sys.modules}))"
        )
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["hashers"][0], "django.contrib.auth.hashers.PBKDF2PasswordHasher")
        self.assertFalse([hasher for hasher in data["hashers"] if "MD5" in hasher])
        self.assertFalse(data["test_settings_loaded"])
