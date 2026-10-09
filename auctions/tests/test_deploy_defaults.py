"""I default di Docker non aprono porte al proxy finto né domini altrui.

Con ``DJANGO_BEHIND_PROXY=True`` e la porta pubblicata su tutte le interfacce,
chiunque raggiunge il container direttamente e scrive ``X-Forwarded-For``:
nuovo indirizzo a ogni tentativo, limite dei tentativi aggirato (login, reset,
codici squadra) e HTTPS finto. E ``*.synology.me`` fra le origini CSRF fidava
anche i sottodomini di altri utenti, in chiaro.
"""
import importlib
import os
import re
import sys
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .. import throttle

ROOT = Path(settings.BASE_DIR)
COMPOSES = ("docker-compose.yml", "docker-compose.ghcr.yml")


def _service(name, service):
    """The text of one service of a compose file (PyYAML is not a dependency)."""
    src = (ROOT / name).read_text(encoding="utf-8")
    m = re.search(rf"^  {service}:\n(.*?)(?=^  \w[\w-]*:\n|^\S|\Z)", src, re.M | re.S)
    return m.group(1)


def _env(text):
    return dict(m.groups() for m in re.finditer(r"^\s+- ([A-Z_]+)=(.*)$", text, re.M))


class ComposeDefaultsTests(SimpleTestCase):
    def test_the_port_listens_on_localhost_by_default(self):
        for name in COMPOSES:
            with self.subTest(compose=name):
                ports = re.findall(r'^\s+- "([^"]*:8000)"$', _service(name, "app"), re.M)
                self.assertEqual(ports, ["${FM_BIND:-127.0.0.1}:${PORT:-8088}:8000"])

    def test_the_proxy_is_not_trusted_by_default(self):
        for name in COMPOSES:
            with self.subTest(compose=name):
                env = _env(_service(name, "app"))
                self.assertEqual(env["DJANGO_BEHIND_PROXY"], "${DJANGO_BEHIND_PROXY:-False}")

    def test_allowed_hosts_must_be_set(self):
        for name in COMPOSES:
            with self.subTest(compose=name):
                env = _env(_service(name, "app"))
                self.assertTrue(env["DJANGO_ALLOWED_HOSTS"].startswith("${DJANGO_ALLOWED_HOSTS:?"))
                self.assertIn("FM_SITE_URL", env)

    def test_no_wildcards_anywhere(self):
        for name in COMPOSES:
            with self.subTest(compose=name):
                src = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn(":-*}", src)
                self.assertNotIn("synology.me", src)
                self.assertIsNone(re.search(r"CSRF_TRUSTED_ORIGINS=\$\{[^}]*\*", src))

    def test_the_example_env_says_what_to_set(self):
        src = (ROOT / ".env.docker.example").read_text(encoding="utf-8")
        for line in ("DJANGO_ALLOWED_HOSTS=fantamanager.example.it", "FM_SITE_URL=https://",
                     "DJANGO_BEHIND_PROXY=True"):
            self.assertIn(line, src)
        self.assertNotIn("DJANGO_ALLOWED_HOSTS=*", src)


class CsrfOriginsTests(SimpleTestCase):
    def _settings_with(self, **env):
        base = {"DJANGO_CSRF_TRUSTED_ORIGINS": "", "FM_SITE_URL": ""}
        base.update(env)
        with mock.patch.dict(os.environ, base):
            sys.modules.pop("liveauction.settings", None)
            try:
                return importlib.import_module("liveauction.settings")
            finally:
                sys.modules.pop("liveauction.settings", None)
                importlib.import_module("liveauction.settings")

    def test_no_wildcard_origins_by_default(self):
        mod = self._settings_with()
        self.assertFalse([o for o in mod.CSRF_TRUSTED_ORIGINS if "*" in o])

    def test_the_site_url_is_a_trusted_origin(self):
        mod = self._settings_with(FM_SITE_URL="https://fanta.example.it",
                                  DJANGO_CSRF_TRUSTED_ORIGINS="http://localhost:8088")
        self.assertEqual(mod.CSRF_TRUSTED_ORIGINS, ["http://localhost:8088", "https://fanta.example.it"])


class ForwardedForTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("mario", password="giusta-123456")

    def tearDown(self):
        cache.clear()

    def _burn(self, forwarded):
        limit, _ = throttle.LIMITS["login"]
        for n in range(limit):
            self.client.post(reverse("login"), {"identifier": "mario", "password": "x"},
                             HTTP_X_FORWARDED_FOR=forwarded(n))

    @override_settings(TRUSTED_PROXY_HOPS=0)
    def test_without_the_proxy_a_changing_header_does_not_reset_the_count(self):
        self._burn(lambda n: f"203.0.113.{n}")
        r = self.client.post(reverse("login"), {"identifier": "mario", "password": "giusta-123456"},
                             HTTP_X_FORWARDED_FOR="198.51.100.99")
        self.assertContains(r, "Troppi tentativi")

    @override_settings(TRUSTED_PROXY_HOPS=1)
    def test_behind_the_proxy_the_last_hop_counts_not_what_the_client_wrote(self):
        # Il client scrive quello che vuole in testa; il proxy aggiunge in fondo l'indirizzo vero.
        self._burn(lambda n: f"10.0.0.{n}, 203.0.113.7")
        r = self.client.post(reverse("login"), {"identifier": "mario", "password": "giusta-123456"},
                             HTTP_X_FORWARDED_FOR="10.9.9.9, 203.0.113.7")
        self.assertContains(r, "Troppi tentativi")
        self.assertEqual(throttle.client_ip(type("R", (), {"META": {
            "HTTP_X_FORWARDED_FOR": "1.1.1.1, 2.2.2.2, 203.0.113.7", "REMOTE_ADDR": "172.17.0.1"}})()),
            "203.0.113.7")
