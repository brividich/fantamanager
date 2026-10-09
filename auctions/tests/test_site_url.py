"""I link nelle email non si costruiscono dall'Host della richiesta.

Con ``ALLOWED_HOSTS = ["*"]`` chiunque chiede il reset della password di un
altro con ``Host: evil.tld``: l'email vera arriva alla vittima, ma il link (col
token) porta a ``evil.tld``. I link nelle email vengono da ``FM_SITE_URL``; senza,
dalla richiesta solo se ``ALLOWED_HOSTS`` elenca i domini; altrimenti l'email
non parte e l'admin legge cosa impostare.
"""
import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail as django_mail
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from ..models import League, MailSettings, Participant
from ..services import mail

SITE = "https://fanta.example.it"


def _locmem(cfg=None):
    return django_mail.get_connection()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailLinksTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("mario", email="mario@example.com", password="Vecchia-2026")
        cfg = MailSettings.get()
        cfg.enabled, cfg.provider, cfg.host, cfg.from_email = True, "smtp", "smtp.example.com", "noreply@example.com"
        cfg.save()
        patcher = mock.patch("auctions.services.mail.connection", _locmem)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        cache.clear()

    def _reset(self, host):
        return self.client.post(reverse("password_reset"), {"identifier": "mario"}, HTTP_HOST=host)

    def _links(self):
        return [u for m in django_mail.outbox for u in re.findall(r"https?://[^\s\"'<>]+", m.body)]

    @override_settings(FM_SITE_URL=SITE, ALLOWED_HOSTS=["*"])
    def test_reset_link_uses_fm_site_url_never_the_host(self):
        resp = self._reset("evil.tld")
        self.assertContains(resp, "ti abbiamo scritto")
        self.assertEqual(len(django_mail.outbox), 1)
        links = self._links()
        self.assertTrue(links)
        for link in links:
            self.assertTrue(link.startswith(SITE + "/"), link)
        self.assertNotIn("evil.tld", django_mail.outbox[0].body)

    @override_settings(FM_SITE_URL="", ALLOWED_HOSTS=["*"])
    def test_no_site_url_and_any_host_sends_no_link(self):
        resp = self._reset("evil.tld")
        self.assertEqual(len(django_mail.outbox), 0)
        self.assertContains(resp, "FM_SITE_URL")       # l'admin sa cosa impostare

    @override_settings(FM_SITE_URL="", ALLOWED_HOSTS=["fanta.example.it", "testserver"])
    def test_without_site_url_a_listed_host_is_used(self):
        self._reset("fanta.example.it")
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertTrue(all(link.startswith("http://fanta.example.it/") for link in self._links()))

    @override_settings(FM_SITE_URL=SITE, ALLOWED_HOSTS=["*"])
    def test_invites_use_fm_site_url(self):
        owner = User.objects.create_user("presidente", password="pw-presidente")
        league = League.objects.create(name="Lega", owner=owner)
        Participant.objects.create(league=league, display_name="Alfa", email="alfa@example.com",
                                   credits=Decimal("500"))
        self.client.force_login(owner)
        self.client.post(reverse("admin_invite_teams"), {"league_id": league.id}, HTTP_HOST="evil.tld")
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertNotIn("evil.tld", django_mail.outbox[0].body)
        self.assertTrue(all(link.startswith(SITE + "/") for link in self._links()))

    @override_settings(FM_SITE_URL="", ALLOWED_HOSTS=["*"])
    def test_invites_without_a_trusted_address_are_not_sent(self):
        owner = User.objects.create_user("presidente", password="pw-presidente")
        league = League.objects.create(name="Lega", owner=owner)
        Participant.objects.create(league=league, display_name="Alfa", email="alfa@example.com",
                                   credits=Decimal("500"))
        self.client.force_login(owner)
        resp = self.client.post(reverse("admin_invite_teams"), {"league_id": league.id},
                                HTTP_HOST="evil.tld", follow=True)
        self.assertEqual(len(django_mail.outbox), 0)
        self.assertContains(resp, "FM_SITE_URL")


class DesktopLinkBaseTests(SimpleTestCase):
    """L'app del PC: i link usano l'indirizzo wifi della macchina, mai un Host qualsiasi."""

    def _request(self, host):
        from django.test import RequestFactory
        return RequestFactory().get("/", HTTP_HOST=host)

    @override_settings(FM_SITE_URL="", ALLOWED_HOSTS=["*"], DESKTOP_APP=True)
    def test_own_wifi_address_yes_any_other_host_no(self):
        with mock.patch("auctions.remote.lan_ip", return_value="192.168.1.50"):
            self.assertEqual(mail.link_base(self._request("localhost:8000")), "http://192.168.1.50:8000")
            self.assertEqual(mail.link_base(self._request("192.168.1.50:8000")), "http://192.168.1.50:8000")
            # L'indirizzo lo decide la macchina: un Host inventato non entra nel link.
            self.assertNotIn("evil", mail.link_base(self._request("evil.tld")))
        with mock.patch("auctions.remote.lan_ip", return_value=""):
            self.assertEqual(mail.link_base(self._request("evil.tld")), "")


class MailPageWarnsTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.it", "pw-root-123"))

    @override_settings(FM_SITE_URL="", ALLOWED_HOSTS=["*"])
    def test_the_mail_page_says_what_to_set(self):
        resp = self.client.get(reverse("admin_mail_settings"))
        self.assertContains(resp, "Le email con un link non partono")
        self.assertContains(resp, "FM_SITE_URL=https://")

    @override_settings(FM_SITE_URL=SITE, ALLOWED_HOSTS=["*"])
    def test_the_mail_page_shows_where_links_point(self):
        self.assertContains(self.client.get(reverse("admin_mail_settings")), SITE)


class SiteUrlSettingTests(SimpleTestCase):
    def test_link_base_prefers_fm_site_url(self):
        with self.settings(FM_SITE_URL=SITE):
            self.assertEqual(mail.link_base(None), SITE)

    def test_bad_values_are_refused(self):
        from liveauction.settings import _site_url
        self.assertEqual(_site_url("https://fanta.example.it/"), SITE)
        for bad in ("fanta.example.it", "ftp://x.it", "https://x.it/percorso", "https://"):
            with self.subTest(bad=bad), self.assertRaises(ImproperlyConfigured):
                _site_url(bad)

    def test_the_server_profile_requires_it(self):
        import importlib
        import os
        import sys

        env = {"DJANGO_DEBUG": "False", "DJANGO_ALLOWED_HOSTS": "fanta.example.it",
               "POSTGRES_DB": "x", "POSTGRES_PASSWORD": "una-password-vera", "FM_SITE_URL": ""}
        with mock.patch.dict(os.environ, env):
            for mod in ("liveauction.settings_server", "liveauction.settings"):
                sys.modules.pop(mod, None)
            try:
                with self.assertRaisesRegex(ImproperlyConfigured, "FM_SITE_URL"):
                    importlib.import_module("liveauction.settings_server")
            finally:
                for mod in ("liveauction.settings_server", "liveauction.settings"):
                    sys.modules.pop(mod, None)
                importlib.import_module("liveauction.settings")
