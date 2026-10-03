"""Test-only settings: development settings with a fast password hasher.

Run the suite with `manage.py test --settings=config.settings.test`. Django's default
PBKDF2 costs about a second per hash on a laptop and made up roughly 90% of test time.

Test-only. Never import this module from production settings or from base.py, and never
use it to run the application. The production smoke test must still run on production
settings so the real PBKDF2 login path stays covered.
"""

from .development import *  # noqa: F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
