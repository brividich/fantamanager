"""liveauction.settings_server: a hosted server refuses home-only settings."""
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
PROBE = ("import django; django.setup(); from django.conf import settings as s; "
         "print(s.PUBLIC_TOKENS_REQUIRED, s.FM_REMOTE_TUNNEL, s.SESSION_COOKIE_SECURE, "
         "'file' in s.LOGGING['handlers'])")


def _run(**env):
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DJANGO_", "POSTGRES_", "FANTAMANAGER_", "FM_SITE_URL"))}
    base.update({"DJANGO_SETTINGS_MODULE": "liveauction.settings_server",
                 "DJANGO_SECRET_KEY": "test-only-" + "x" * 40}, **env)
    return subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=base,
                          capture_output=True, text=True, timeout=60)


class ServerProfileTests(SimpleTestCase):
    def test_home_defaults_are_refused(self):
        res = _run()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("DJANGO_ALLOWED_HOSTS", res.stderr)
        self.assertIn("PostgreSQL", res.stderr)

    def test_the_example_database_password_is_refused(self):
        res = _run(DJANGO_ALLOWED_HOSTS="fanta.example.com", POSTGRES_DB="fm",
                   POSTGRES_PASSWORD="fantamanager_secret_pass")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("POSTGRES_PASSWORD", res.stderr)

    def test_the_public_https_address_is_required(self):
        for site in ("", "http://fanta.example.com"):
            with self.subTest(site=site):
                res = _run(DJANGO_ALLOWED_HOSTS="fanta.example.com", POSTGRES_DB="fm",
                           POSTGRES_PASSWORD="una-password-vera", FM_SITE_URL=site)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("FM_SITE_URL", res.stderr)

    def test_a_proper_server_boots_locked_down(self):
        res = _run(DJANGO_ALLOWED_HOSTS="fanta.example.com", POSTGRES_DB="fm",
                   POSTGRES_PASSWORD="una-password-vera", FM_SITE_URL="https://fanta.example.com")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["True", "False", "True", "False"])
