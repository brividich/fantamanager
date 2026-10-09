"""Posta: provider settings, test email, team invites and buste notices."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail as outbox
from django.core.mail import get_connection
from django.test import TestCase, override_settings
from django.urls import reverse

from auctions.models import League, MailSettings, MarketSession, Participant
from auctions.services import mail


def _locmem(cfg=None):
    return get_connection("django.core.mail.backends.locmem.EmailBackend")


class MailSettingsPageTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.local", "pw")
        self.client.force_login(self.root)

    def test_only_superusers_reach_the_page(self):
        self.client.force_login(User.objects.create_user("staff", password="pw", is_staff=True))
        self.assertEqual(self.client.get(reverse("admin_mail_settings")).status_code, 403)

    def test_save_keeps_the_password_write_only(self):
        self.client.post(reverse("admin_mail_settings"), {
            "action": "save", "enabled": "1", "provider": "gmail", "host": "smtp.gmail.com",
            "port": "587", "security": "starttls", "username": "lega@gmail.com",
            "password": "segreta-123", "from_email": "lega@gmail.com", "from_name": "Lega",
        })
        cfg = MailSettings.get()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.password, "segreta-123")
        self.assertEqual(mail.status(cfg), (True, "db"))
        page = self.client.get(reverse("admin_mail_settings"))
        self.assertNotContains(page, "segreta-123")
        self.assertContains(page, "salvata")
        # An empty password field keeps the saved one; the checkbox clears it.
        self.client.post(reverse("admin_mail_settings"), {
            "action": "save", "enabled": "1", "provider": "gmail", "host": "smtp.gmail.com",
            "port": "587", "security": "starttls", "from_email": "lega@gmail.com", "password": "",
        })
        self.assertEqual(MailSettings.get().password, "segreta-123")
        self.client.post(reverse("admin_mail_settings"), {
            "action": "save", "provider": "gmail", "host": "smtp.gmail.com", "from_email": "lega@gmail.com",
            "clear_password": "1",
        })
        self.assertEqual(MailSettings.get().password, "")

    def test_enabled_without_host_or_sender_is_refused(self):
        resp = self.client.post(reverse("admin_mail_settings"), {
            "action": "save", "enabled": "1", "provider": "smtp", "host": "", "from_email": "nope",
        }, follow=True)
        self.assertContains(resp, "non è un indirizzo email valido")
        self.assertFalse(MailSettings.get().enabled)

    def test_test_email_goes_out_and_is_remembered(self):
        cfg = MailSettings.get()
        cfg.enabled, cfg.provider, cfg.host, cfg.from_email = True, "smtp", "smtp.x.it", "lega@x.it"
        cfg.save()
        with mock.patch.object(mail, "connection", _locmem):
            resp = self.client.post(reverse("admin_mail_settings"), {"action": "test", "test_to": "me@x.it"}, follow=True)
        self.assertContains(resp, "Email di prova inviata a me@x.it")
        self.assertEqual(len(outbox.outbox), 1)
        self.assertEqual(outbox.outbox[0].to, ["me@x.it"])
        self.assertIn("lega@x.it", outbox.outbox[0].from_email)
        self.assertTrue(MailSettings.get().last_test_ok)

    def test_failed_send_explains_why(self):
        import smtplib
        cfg = MailSettings.get()
        cfg.enabled, cfg.provider, cfg.host, cfg.from_email = True, "smtp", "smtp.x.it", "lega@x.it"
        cfg.save()

        class Boom:
            def send_messages(self, msgs):
                raise smtplib.SMTPAuthenticationError(535, b"no")
        with mock.patch.object(mail, "connection", lambda cfg=None: Boom()):
            ok, error = mail.send_test("me@x.it")
        self.assertFalse(ok)
        self.assertIn("Credenziali rifiutate", error)
        self.assertFalse(MailSettings.get().last_test_ok)

    def test_nothing_configured_means_not_ready(self):
        self.assertFalse(mail.is_ready())
        self.assertEqual(mail.send("x", "a@x.it", "y"), (False, "Posta non configurata."))


# I link delle email partono dall'indirizzo pubblico del sito (FM_SITE_URL).
@override_settings(EMAIL_HOST="smtp.env.local", FM_SITE_URL="http://testserver")
class LeagueEmailTests(TestCase):
    """With the env fallback the test runner's locmem backend receives the mail."""

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.local", "pw")
        self.client.force_login(self.root)
        self.league = League.objects.create(name="Lega Mail", owner=self.root)
        self.a = Participant.objects.create(league=self.league, display_name="Alfa", email="alfa@x.it",
                                            access_code="AL01", credits=Decimal("300"))
        self.b = Participant.objects.create(league=self.league, display_name="Beta")
        acc = User.objects.create_user("gamma", email="gamma@x.it", password="pw")
        self.c = Participant.objects.create(league=self.league, display_name="Gamma", user=acc)

    def test_recipients_use_team_email_then_account_email(self):
        self.assertEqual({p.contact_email for p in mail.league_recipients(self.league)}, {"alfa@x.it", "gamma@x.it"})

    def test_invite_all_sends_the_personal_link(self):
        resp = self.client.post(reverse("admin_invite_teams"), {"league_id": self.league.id}, follow=True)
        self.assertContains(resp, "2 email inviate")
        self.assertEqual(len(outbox.outbox), 2)
        msg = next(m for m in outbox.outbox if m.to == ["alfa@x.it"])
        self.assertIn(f"t={self.a.public_token}", msg.body)
        self.assertIn("AL01", msg.body)
        self.assertEqual(msg.subject, "Benvenuto in Lega Mail")

    def test_team_email_is_saved_and_invite_sent(self):
        url = reverse("admin_participant_email", args=[self.b.id])
        self.client.post(url, {"email": "non-valida"})
        self.b.refresh_from_db()
        self.assertEqual(self.b.email, "")
        self.client.post(url, {"email": "beta@x.it", "invite": "1"})
        self.b.refresh_from_db()
        self.assertEqual(self.b.email, "beta@x.it")
        self.assertEqual([m.to for m in outbox.outbox], [["beta@x.it"]])

    def test_foreign_admin_cannot_invite(self):
        self.client.force_login(User.objects.create_user("other", password="pw", is_staff=True))
        resp = self.client.post(reverse("admin_invite_teams"), {"league_id": self.league.id})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(outbox.outbox), 0)

    def test_new_session_can_notify_the_teams(self):
        self.client.post(reverse("admin_market_create"), {
            "league_id": self.league.id, "title": "Buste Novembre", "notify": "1",
        })
        self.assertEqual(len(outbox.outbox), 2)
        self.assertIn("Buste Novembre", outbox.outbox[0].subject)
        self.assertIn("300 FM", next(m for m in outbox.outbox if m.to == ["alfa@x.it"]).body)

    def test_notify_button_on_the_session(self):
        s = MarketSession.objects.create(league=self.league, title="S", status=MarketSession.Status.OPEN)
        page = self.client.get(reverse("admin_market_session", args=[s.id]))
        self.assertContains(page, "Avvisa via email")
        self.client.post(reverse("admin_market_notify", args=[s.id]))
        self.assertEqual(len(outbox.outbox), 2)
