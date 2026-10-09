"""«Password dimenticata?»: the link by email, and nothing for a stranger."""
import re

from django.contrib.auth.models import User
from django.core import mail as django_mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import MailSettings


# I link delle email partono dall'indirizzo pubblico del sito (FM_SITE_URL).
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   FM_SITE_URL="http://testserver")
class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("mario", email="mario@example.com", password="Vecchia-2026")
        cfg = MailSettings.get()
        cfg.enabled, cfg.provider, cfg.host, cfg.from_email = True, "smtp", "smtp.example.com", "noreply@example.com"
        cfg.save()

    def tearDown(self):
        cache.clear()

    def _link(self):
        from unittest import mock

        with mock.patch("auctions.services.mail.connection",
                        lambda cfg=None: django_mail.get_connection()):
            resp = self.client.post(reverse("password_reset"), {"identifier": "MARIO@example.com"})
        self.assertContains(resp, "ti abbiamo scritto")
        self.assertEqual(len(django_mail.outbox), 1)
        return re.search(r"https?://\S+", django_mail.outbox[0].body).group(0)

    def test_the_link_sets_a_new_password_once(self):
        link = self._link()
        page = self.client.get(link)
        self.assertContains(page, "Nuova password")
        weak = self.client.post(link, {"password": "12345678", "password_confirm": "12345678"})
        self.assertEqual(weak.status_code, 200)               # refused, stays on the form
        resp = self.client.post(link, {"password": "Nuova-Lunga-77", "password_confirm": "Nuova-Lunga-77"})
        self.assertRedirects(resp, reverse("login"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Nuova-Lunga-77"))
        self.assertContains(self.client.get(link), "non è valido")     # used once

    def test_unknown_accounts_get_the_same_answer_and_no_email(self):
        resp = self.client.post(reverse("password_reset"), {"identifier": "nessuno"})
        self.assertContains(resp, "ti abbiamo scritto")
        self.assertEqual(len(django_mail.outbox), 0)

    def test_a_forged_link_opens_nothing(self):
        resp = self.client.get(reverse("password_reset_confirm", args=["MQ", "falso-token"]))
        self.assertContains(resp, "non è valido")

    def test_login_pages_offer_it(self):
        self.assertContains(self.client.get(reverse("login")), reverse("password_reset"))
        self.assertContains(self.client.get(reverse("app_login")), reverse("password_reset"))
