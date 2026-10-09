"""Privacy e GDPR (Fase A): informativa e termini, consenso alla registrazione,
verifica dell'email, «Il mio account» (export, cambio email, eliminazione),
conservazione, disiscrizione dalle email della lega, registro delle azioni
sensibili, nessuna risorsa di terzi caricata dal browser."""
import json
import logging
import re
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core import mail as outbox
from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .. import legal
from ..models import (
    AccountPrivacy,
    Auction,
    AuditLog,
    Bid,
    LegalAcceptance,
    League,
    ManagedAccount,
    Participant,
)
from ..services import privacy
from ..views import images

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
HTML = {"HTTP_ACCEPT": "text/html,application/xhtml+xml"}


def _register(client, **extra):
    data = {"username": "nuovo_utente", "email": "nuovo@esempio.it", "password": "Ombrello-Verde-77",
            "password_confirm": "Ombrello-Verde-77", "accept_terms": "1", "accept_age": "1"}
    data.update(extra)
    return client.post(reverse("register"), {k: v for k, v in data.items() if v is not None})


def _accept_current(user):
    privacy.record_acceptance(user, "127.0.0.1")


# --- A.5 Terze parti -----------------------------------------------------------

class NoThirdPartyResourcesTests(TestCase):
    def test_no_google_fonts_in_templates(self):
        for path in TEMPLATES_DIR.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("fonts.googleapis.com", text, path)
            self.assertNotIn("gstatic", text, path)

    def test_pages_load_the_local_fonts(self):
        html = self.client.get(reverse("login")).content.decode()
        self.assertRegex(html, r'href="/static/fonts/fonts\.[0-9a-f]+\.css"')
        self.assertNotIn("googleapis", html)

    def test_no_external_scripts_styles_or_images_in_templates(self):
        """Nessun <script src>, <link href> o <img src> verso un altro sito: i
        link cliccabili (<a href>) sì."""
        pattern = re.compile(r"<(script|link|img|iframe)\b[^>]*\b(src|href)=[\"'](https?:)?//", re.I)
        for path in TEMPLATES_DIR.rglob("*.html"):
            if "/email/" in str(path):
                continue                  # le email non sono pagine del sito
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), f"{path}: {pattern.search(text)}")


class RemoteImageTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)

    def test_external_photos_pass_through_the_site(self):
        url = "https://media.api-sports.io/football/players/9.png"
        local = images.local_url(url)
        self.assertTrue(local.startswith("/img/"))
        self.assertNotIn("api-sports", local.split("/img/")[0])
        resp_obj = mock.Mock(status_code=200, headers={"Content-Type": "image/png"}, content=b"\x89PNGfake")
        resp_obj.raise_for_status = lambda: None
        with mock.patch("auctions.providers.standings._public_host", return_value=True), \
                mock.patch.object(images.requests, "get", return_value=resp_obj) as get:
            first = self.client.get(local)
            second = self.client.get(local)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(b"".join(second.streaming_content), b"\x89PNGfake")
        self.assertEqual(get.call_count, 1)          # la seconda volta dalla cache

    def test_only_signed_addresses(self):
        self.assertEqual(self.client.get("/img/qualcosa-di-inventato/").status_code, 404)

    def test_private_addresses_are_refused(self):
        local = images.local_url("http://192.168.1.1/router.png")
        with mock.patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.1", 0))]), \
                mock.patch.object(images.requests, "get") as get:
            self.assertEqual(self.client.get(local).status_code, 404)
        get.assert_not_called()


# --- A.1 Informativa e termini -------------------------------------------------

class LegalPagesTests(TestCase):
    @override_settings(FM_PRIVACY_OWNER="Associazione Fanta Test", FM_PRIVACY_EMAIL="privacy@test.it")
    def test_privacy_page_shows_owner_version_and_draft(self):
        resp = self.client.get(reverse("privacy"))
        self.assertContains(resp, "Associazione Fanta Test")
        self.assertContains(resp, "privacy@test.it")
        self.assertContains(resp, legal.PRIVACY_VERSION)
        self.assertContains(resp, "BOZZA")
        self.assertContains(resp, "14 anni")

    def test_without_settings_the_page_says_so(self):
        self.assertContains(self.client.get(reverse("privacy")), "non ancora indicato")

    def test_terms_page(self):
        resp = self.client.get(reverse("terms"))
        self.assertContains(resp, legal.TERMS_VERSION)
        self.assertContains(resp, "BOZZA")

    def test_links_at_the_bottom_registration_and_app_login(self):
        for name in ("login", "register", "app_login", "password_reset", "privacy"):
            with self.subTest(page=name):
                resp = self.client.get(reverse(name))
                self.assertContains(resp, f'href="{reverse("privacy")}"')
                self.assertContains(resp, f'href="{reverse("terms")}"')


# --- A.2 Consenso e verifica dell'email ---------------------------------------

@override_settings(EMAIL_HOST="smtp.env.local", FM_SITE_URL="http://testserver", FM_LEGAL_REQUIRED=True)
class RegistrationConsentTests(TestCase):
    def test_refused_without_each_box_or_email(self):
        for missing in ("accept_terms", "accept_age", "email"):
            with self.subTest(missing=missing):
                resp = _register(self.client, **{missing: None})
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(User.objects.filter(username="nuovo_utente").exists())

    def test_acceptance_saved_with_version_and_email_to_verify(self):
        resp = _register(self.client)
        self.assertRedirects(resp, reverse("onboarding"), fetch_redirect_response=False)
        user = User.objects.get(username="nuovo_utente")
        docs = dict(LegalAcceptance.objects.filter(user=user).values_list("doc", "version"))
        self.assertEqual(docs, {"privacy": legal.PRIVACY_VERSION, "terms": legal.TERMS_VERSION,
                                "age": str(legal.MIN_AGE)})
        self.assertTrue(user.privacy.self_registered)
        self.assertIsNone(user.privacy.email_verified_at)
        self.assertEqual(len(outbox.outbox), 1)
        self.assertIn("/account/verifica/", outbox.outbox[0].body)
        self.assertIn("nuovo@esempio.it", outbox.outbox[0].to)

    def test_the_form_has_the_unticked_boxes_and_links(self):
        html = self.client.get(reverse("register")).content.decode()
        for name in ("accept_terms", "accept_age"):
            box = re.search(rf'<input type="checkbox" name="{name}"[^>]*>', html).group(0)
            self.assertNotIn("checked", box)
            self.assertIn("required", box)
        self.assertIn(reverse("privacy"), html)

    @override_settings(FM_LEGAL_REQUIRED=False)
    def test_desktop_app_keeps_the_short_form(self):
        resp = _register(self.client, email=None, accept_terms=None, accept_age=None)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AccountPrivacy.objects.exists())


@override_settings(FM_LEGAL_REQUIRED=True)
class ReacceptanceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("vecchio", password="pw")
        self.client.force_login(self.user)

    def test_new_version_asks_again(self):
        LegalAcceptance.objects.create(user=self.user, doc="privacy", version="0.9")
        LegalAcceptance.objects.create(user=self.user, doc="terms", version=legal.TERMS_VERSION)
        resp = self.client.get(reverse("account"), **HTML)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("legal_reaccept")))
        # Le chiamate dei pulsanti non si fermano.
        self.assertEqual(self.client.get(reverse("account")).status_code, 200)
        # Senza le caselle no; con le caselle sì, e si torna alla pagina.
        self.client.post(reverse("legal_reaccept"), {"next": reverse("account")})
        self.assertEqual(privacy.acceptance_state(User.objects.get(pk=self.user.pk)), "outdated")
        resp = self.client.post(reverse("legal_reaccept"), {"next": reverse("account"),
                                                           "accept_terms": "1", "accept_age": "1"})
        self.assertRedirects(resp, reverse("account"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("account"), **HTML).status_code, 200)

    def test_accounts_from_before_get_a_notice_not_a_wall(self):
        resp = self.client.get(reverse("account"), **HTML)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Leggi e accetta")
        _accept_current(self.user)
        self.assertNotContains(self.client.get(reverse("account"), **HTML), "fm-privacy-notice")


@override_settings(EMAIL_HOST="smtp.env.local", FM_SITE_URL="http://testserver", FM_LEGAL_REQUIRED=True)
class EmailVerificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("verifica", email="io@esempio.it", password="pw")
        AccountPrivacy.objects.create(user=self.user, self_registered=True)
        _accept_current(self.user)

    def _link(self):
        return reverse("account_verify", args=[privacy.verify_token(self.user, self.user.email)])

    def test_valid_link_confirms(self):
        resp = self.client.get(self._link())
        self.assertContains(resp, "Email confermata")
        self.assertIsNotNone(AccountPrivacy.objects.get(user=self.user).email_verified_at)

    def test_expired_link(self):
        link = self._link()
        later = timezone.now().timestamp() + privacy.VERIFY_MAX_AGE + 60
        with mock.patch("django.core.signing.time.time", return_value=later):
            resp = self.client.get(link)
        self.assertContains(resp, "scaduto")
        self.assertIsNone(AccountPrivacy.objects.get(user=self.user).email_verified_at)

    def test_tampered_link(self):
        token = privacy.verify_token(self.user, self.user.email)
        resp = self.client.get(reverse("account_verify", args=[token[:-2] + ("AA" if token[-2:] != "AA" else "BB")]))
        self.assertContains(resp, "non è valido")
        self.assertIsNone(AccountPrivacy.objects.get(user=self.user).email_verified_at)

    def test_the_link_does_not_carry_the_address(self):
        token = privacy.verify_token(self.user, self.user.email)
        self.assertNotIn("esempio", signing.b64_decode(token.split(":")[0].encode()).decode(errors="ignore"))

    def test_until_verified_no_reset_and_no_league_emails(self):
        league = League.objects.create(name="Lega V")
        team = Participant.objects.create(league=league, display_name="Verdi", user=self.user)
        self.assertEqual(team.contact_email, "")
        self.client.post(reverse("password_reset"), {"identifier": "verifica"})
        self.assertEqual(len(outbox.outbox), 0)
        self.client.get(self._link())
        team = Participant.objects.get(pk=team.pk)
        self.assertEqual(team.contact_email, "io@esempio.it")
        self.client.post(reverse("password_reset"), {"identifier": "verifica"})
        self.assertEqual(len(outbox.outbox), 1)

    def test_banner_and_resend(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("account"), **HTML)
        self.assertContains(page, "Conferma la tua email")
        resp = self.client.post(reverse("account_resend"), {"next": reverse("account")})
        self.assertRedirects(resp, reverse("account"), fetch_redirect_response=False)
        self.assertEqual(len(outbox.outbox), 1)

    def test_accounts_from_before_count_as_verified(self):
        old = User.objects.create_user("storico", email="storico@esempio.it", password="pw")
        self.assertFalse(privacy.email_unverified(old))
        self.client.post(reverse("password_reset"), {"identifier": "storico"})
        self.assertEqual(len(outbox.outbox), 1)


class ManagedAccountEmailTests(TestCase):
    def test_admin_creates_a_team_account_without_email(self):
        owner = User.objects.create_user("pres_ma", password="pw")
        league = League.objects.create(name="Lega MA", owner=owner)
        team = Participant.objects.create(league=league, display_name="Gialli")
        self.client.force_login(owner)
        self.client.post(reverse("admin_participant_account", args=[team.id]),
                         {"action": "create", "username": "allenatore_gialli", "password": ""})
        account = User.objects.get(username="allenatore_gialli")
        self.assertEqual(account.email, "")
        self.assertTrue(ManagedAccount.objects.filter(user=account).exists())


# --- A.3 Il mio account ------------------------------------------------------

class AccountFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mario", email="mario@esempio.it", password="Password-Mario-1")
        _accept_current(self.user)
        self.other_user = User.objects.create_user("luigi", email="luigi@segreto.it", password="pw")
        self.owner = User.objects.create_user("presidente", email="pres@esempio.it", password="pw")
        self.league = League.objects.create(name="Lega Export", owner=self.owner)
        self.mine = Participant.objects.create(league=self.league, display_name="Mario FC", user=self.user,
                                               email="mario@esempio.it", credits=Decimal("500"))
        self.theirs = Participant.objects.create(league=self.league, display_name="Luigi United",
                                                 user=self.other_user, email="luigi@segreto.it")
        self.auction = Auction.objects.create(league=self.league, title="Asta di prova")
        self.my_bid = Bid.objects.create(auction=self.auction, participant=self.mine, amount=Decimal("12"),
                                         increment=Decimal("1"), accepted=True, ip_address="10.1.1.1",
                                         user_agent="Telefono di Mario")
        self.their_bid = Bid.objects.create(auction=self.auction, participant=self.theirs, amount=Decimal("13"),
                                            increment=Decimal("1"), accepted=True, ip_address="10.9.9.9",
                                            user_agent="Telefono di Luigi")


class AccountExportTests(AccountFixture):
    def test_export_has_my_data_and_nothing_of_others(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("account_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        body = resp.content.decode()
        data = json.loads(body)
        self.assertEqual(data["account"]["username"], "mario")
        self.assertEqual(data["squadre"][0]["nome"], "Mario FC")
        self.assertEqual(len(data["offerte_asta"]), 1)
        self.assertEqual(data["offerte_asta"][0]["ip"], "10.1.1.1")
        self.assertEqual(len(data["accettazioni_legali"]), 3)
        for leak in ("luigi@segreto.it", "10.9.9.9", "Telefono di Luigi", "pres@esempio.it"):
            self.assertNotIn(leak, body)
        self.assertTrue(AuditLog.objects.filter(action="export", target_user=self.user).exists())

    def test_export_needs_login_and_post(self):
        self.assertEqual(self.client.post(reverse("account_export")).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_export")).status_code, 405)


class AccountDeleteTests(AccountFixture):
    def _delete(self, user, password, username=None):
        self.client.force_login(user)
        return self.client.post(reverse("account_delete"), {
            "confirm_username": username or user.username, "password": password})

    def test_bids_stay_without_personal_data(self):
        AuditLog.objects.create(actor=self.owner, actor_name="presidente", action="view_as",
                                target_user=self.user, target_name="mario")
        resp = self._delete(self.user, "Password-Mario-1")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="mario").exists())
        team = Participant.objects.get(pk=self.mine.pk)
        self.assertIsNone(team.user)
        self.assertEqual(team.email, "")
        bid = Bid.objects.get(pk=self.my_bid.pk)
        self.assertEqual((bid.ip_address, bid.user_agent, bid.amount), (None, "", Decimal("12")))
        other = Bid.objects.get(pk=self.their_bid.pk)
        self.assertEqual(other.ip_address, "10.9.9.9")        # gli altri non si toccano
        self.assertFalse(AuditLog.objects.filter(target_name="mario").exists())
        self.assertFalse(LegalAcceptance.objects.filter(user_id=self.user.pk).exists())
        # Disconnesso.
        self.assertEqual(self.client.get(reverse("account")).status_code, 302)

    def test_wrong_password_or_name_refused(self):
        self._delete(self.user, "sbagliata")
        self._delete(self.user, "Password-Mario-1", username="Mario")
        self.assertTrue(User.objects.filter(username="mario").exists())

    def test_sole_owner_of_an_active_league_is_refused(self):
        self.owner.set_password("Pres-Password-9")
        self.owner.save()
        self._delete(self.owner, "Pres-Password-9")
        self.assertTrue(User.objects.filter(username="presidente").exists())
        page = self.client.get(reverse("account"))
        self.assertContains(page, "Passa le tue leghe")
        self.assertContains(page, f'{reverse("admin_participants")}?league={self.league.id}')


@override_settings(EMAIL_HOST="smtp.env.local", FM_SITE_URL="http://testserver")
class AccountEmailChangeTests(AccountFixture):
    def test_new_email_counts_after_the_link(self):
        self.client.force_login(self.user)
        self.client.post(reverse("account_email"), {"email": "nuova@esempio.it"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "mario@esempio.it")
        self.assertEqual(len(outbox.outbox), 1)
        self.assertEqual(outbox.outbox[0].to, ["nuova@esempio.it"])
        link = re.search(r"http://testserver(/account/verifica/\S+/)", outbox.outbox[0].body).group(1)
        self.client.get(link)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "nuova@esempio.it")
        self.assertEqual(Participant.objects.get(pk=self.mine.pk).email, "nuova@esempio.it")

    def test_someone_elses_email_refused(self):
        self.client.force_login(self.user)
        self.client.post(reverse("account_email"), {"email": "LUIGI@segreto.it"})
        self.assertEqual(len(outbox.outbox), 0)
        self.assertEqual(AccountPrivacy.objects.filter(user=self.user, pending_email__gt="").count(), 0)


class PresidentRemovesTeamEmailTests(AccountFixture):
    def test_remove_email_and_unlink_are_logged(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_participant_email", args=[self.mine.id]), {"email": ""})
        self.assertEqual(Participant.objects.get(pk=self.mine.pk).email, "")
        self.client.post(reverse("admin_participant_account", args=[self.mine.id]), {"action": "unlink"})
        self.assertIsNone(Participant.objects.get(pk=self.mine.pk).user)
        actions = set(AuditLog.objects.filter(target_user=self.user).values_list("action", flat=True))
        self.assertEqual(actions, {"email_removed", "unlink"})


# --- A.4 Conservazione ---------------------------------------------------------

@override_settings(FM_RETENTION_BID_IP_DAYS=90, FM_RETENTION_UNVERIFIED_DAYS=30)
class CleanupTests(AccountFixture):
    def test_old_bid_data_cleared_recent_kept(self):
        old = timezone.now() - timedelta(days=91)
        Bid.objects.filter(pk=self.my_bid.pk).update(server_received_at=old)
        busta = Bid.objects.create(auction=self.auction, participant=self.mine, amount=Decimal("1"),
                                   increment=Decimal("1"), user_agent="busta", server_received_at=old)
        report = privacy.cleanup()
        self.assertEqual(report["bids"], 1)
        mine = Bid.objects.get(pk=self.my_bid.pk)
        self.assertEqual((mine.ip_address, mine.user_agent), (None, ""))
        self.assertEqual(Bid.objects.get(pk=self.their_bid.pk).ip_address, "10.9.9.9")
        self.assertEqual(Bid.objects.get(pk=busta.pk).user_agent, "busta")

    def test_only_unverified_unused_self_registered_accounts_go(self):
        old = timezone.now() - timedelta(days=31)

        def account(name, verified=False, self_registered=True, when=old):
            u = User.objects.create_user(name, email=f"{name}@x.it", password="pw")
            row = AccountPrivacy.objects.create(user=u, self_registered=self_registered,
                                                email_verified_at=timezone.now() if verified else None)
            AccountPrivacy.objects.filter(pk=row.pk).update(created_at=when)
            return u

        doomed = account("dimenticato")
        with_team = account("con_squadra")
        Participant.objects.create(league=self.league, display_name="Squadra", user=with_team)
        with_league = account("con_lega")
        League.objects.create(name="Sua", owner=with_league)
        co_admin = account("co_admin")
        self.league.admins.add(co_admin)
        account("verificato", verified=True)
        account("recente", when=timezone.now())
        account("gestito", self_registered=False)
        User.objects.create_user("di_prima", password="pw")          # nessuna riga: mai toccato
        self.assertEqual(privacy.cleanup(dry_run=True)["accounts"], 1)
        self.assertTrue(User.objects.filter(pk=doomed.pk).exists())  # dry-run non cancella
        self.assertEqual(privacy.cleanup()["accounts"], 1)
        self.assertFalse(User.objects.filter(pk=doomed.pk).exists())
        for name in ("con_squadra", "con_lega", "co_admin", "verificato", "recente", "gestito", "di_prima"):
            self.assertTrue(User.objects.filter(username=name).exists(), name)

    def test_expired_sessions_deleted(self):
        Session.objects.create(session_key="vecchia", session_data="x", expire_date=timezone.now() - timedelta(days=1))
        Session.objects.create(session_key="nuova", session_data="x", expire_date=timezone.now() + timedelta(days=1))
        privacy.cleanup()
        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), ["nuova"])

    def test_command(self):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("privacy_cleanup", "--dry-run", stdout=out)
        self.assertIn("sessioni scadute", out.getvalue())


class LogsTests(TestCase):
    def test_login_log_has_no_password(self):
        User.objects.create_user("logtest", email="logtest@esempio.it", password="Segretissima-Pw-42")
        with self.assertLogs("auctions", level="DEBUG") as logs:
            logging.getLogger("auctions").debug("inizio")
            self.client.post(reverse("login"), {"identifier": "logtest@esempio.it",
                                                "password": "Segretissima-Pw-42"})
            self.client.post(reverse("login"), {"identifier": "logtest", "password": "Sbagliata-Pw-42"})
        text = "\n".join(logs.output)
        self.assertNotIn("Segretissima-Pw-42", text)
        self.assertNotIn("Sbagliata-Pw-42", text)
        self.assertNotIn("logtest@esempio.it", text)

    @override_settings(EMAIL_HOST="smtp.env.local")
    def test_mail_log_has_no_address(self):
        from ..services import mail
        with self.assertLogs("auctions.mail", level="INFO") as logs:
            mail.send("Prova", "destinatario@esempio.it", "testo")
        self.assertNotIn("destinatario@esempio.it", "\n".join(logs.output))


# --- A.6 Email della lega -------------------------------------------------------

@override_settings(EMAIL_HOST="smtp.env.local", FM_SITE_URL="http://testserver")
class LeagueEmailTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente_m", first_name="Paolo", password="pw")
        self.league = League.objects.create(name="Lega Email", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Blu", email="blu@esempio.it")
        self.client.force_login(self.owner)

    def _invite(self):
        self.client.post(reverse("admin_invite_teams"), {"league_id": self.league.id})
        return outbox.outbox[-1]

    def test_invite_says_who_why_and_how_to_stop(self):
        msg = self._invite()
        self.assertIn("Paolo", msg.body)
        self.assertIn("Lega Email", msg.body)
        self.assertIn("perché", msg.body)
        self.assertIn("/email/disiscrivi/", msg.body)
        self.assertIn("/privacy/", msg.body)
        html = msg.alternatives[0][0]
        self.assertIn("Non voglio più ricevere email da questa lega", html)

    def test_unsubscribe_is_signed_and_tells_the_president(self):
        msg = self._invite()
        link = re.search(r"http://testserver(/email/disiscrivi/\S+/)", msg.body).group(1)
        self.client.logout()
        self.assertContains(self.client.get(link), "Non voglio più email da questa lega")
        self.assertEqual(Participant.objects.get(pk=self.team.pk).email, "blu@esempio.it")  # GET non cambia
        self.client.post(link)
        team = Participant.objects.get(pk=self.team.pk)
        self.assertEqual(team.email, "")
        self.assertIsNotNone(team.email_opt_out_at)
        # Un link inventato non fa niente.
        self.assertEqual(self.client.post(link[:-3] + "xx/").status_code, 404)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("admin_participants") + f"?league={self.league.id}")
        self.assertContains(page, "Ha chiesto di non ricevere più email")
        self.assertTrue(AuditLog.objects.filter(action="unsubscribe", league=self.league).exists())

    def test_opt_out_also_silences_the_linked_account(self):
        coach = User.objects.create_user("coach_blu", email="coach@esempio.it", password="pw")
        self.team.user = coach
        self.team.email = ""
        self.team.save()
        self.assertEqual(self.team.contact_email, "coach@esempio.it")
        privacy.unsubscribe(self.team)
        self.assertEqual(Participant.objects.get(pk=self.team.pk).contact_email, "")
        # Il presidente scrive un indirizzo nuovo: si riparte.
        self.client.post(reverse("admin_participant_email", args=[self.team.id]), {"email": "nuovo@esempio.it"})
        team = Participant.objects.get(pk=self.team.pk)
        self.assertIsNone(team.email_opt_out_at)
        self.assertEqual(team.contact_email, "nuovo@esempio.it")


class NoMultiTeamCredentialsTests(TestCase):
    """Nessuna pagina mette i link d'accesso di più squadre in un solo testo
    da copiare o condividere: ogni squadra riceve solo il suo."""

    def test_dashboard_and_teams_pages(self):
        owner = User.objects.create_user("pres_cred", password="pw")
        league = League.objects.create(name="Lega Credenziali", owner=owner)
        teams = [Participant.objects.create(league=league, display_name=f"Squadra {i}") for i in range(4)]
        tokens = [t.public_token for t in teams]
        self.client.force_login(owner)
        pages = [reverse("dashboard_league", args=[league.id]),
                 reverse("admin_participants") + f"?league={league.id}",
                 reverse("app_regia_teams") + f"?league={league.id}"]
        chunk = re.compile(r'(?:href|onclick|value|data-[a-z-]+)="([^"]*)"|<textarea[^>]*>(.*?)</textarea>', re.S)
        for url in pages:
            html = self.client.get(url).content.decode()
            self.assertNotIn("Riepilogo Credenziali", html)
            self.assertNotIn("Lista Completa", html)
            for m in chunk.finditer(html):
                text = m.group(1) or m.group(2) or ""
                found = [t for t in tokens if t in text]
                self.assertLessEqual(len(found), 1, f"{url}: {text[:120]}")


# --- A.7 Registro ----------------------------------------------------------------

class AuditTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root_a", "root@x.it", "pw")
        self.coach = User.objects.create_user("coach_a", password="pw")
        self.owner = User.objects.create_user("pres_a", password="pw")
        self.league = League.objects.create(name="Lega Audit", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Rossi", user=self.coach)
        _accept_current(self.coach)

    def test_impersonation_and_view_as_are_logged_and_visible(self):
        self.client.force_login(self.root)
        self.client.post(reverse("supervisor_dashboard"), {"action": "impersonate_user", "user_id": self.coach.id})
        self.client.force_login(self.owner)
        self.client.post(reverse("app_view_as", args=[self.team.id]))
        actions = list(AuditLog.objects.filter(target_user=self.coach).values_list("action", flat=True))
        self.assertCountEqual(actions, ["impersonate", "view_as"])
        # Il superuser vede tutto, l'interessato le sue righe.
        self.client.force_login(self.root)
        self.assertContains(self.client.get(reverse("supervisor_dashboard") + "?tab=audit"), "Vedi come")
        self.client.force_login(self.coach)
        page = self.client.get(reverse("account"))
        self.assertContains(page, "Vedi come (squadra)")
        self.assertContains(page, "pres_a")

    def test_password_set_by_the_president_is_logged(self):
        ManagedAccount.objects.create(user=self.coach, created_by=self.owner)
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_participant_account", args=[self.team.id]),
                         {"action": "password", "password": ""})
        self.assertTrue(AuditLog.objects.filter(action="credentials", target_user=self.coach,
                                                actor=self.owner).exists())


class AccountPageParityTests(TestCase):
    def test_console_and_app_same_page(self):
        from .test_app_pages import AppPagesParityTests

        user = User.objects.create_user("solo_manager", password="pw")
        league = League.objects.create(name="Lega P")
        Participant.objects.create(league=league, display_name="Mia", user=user)
        self.client.force_login(user)
        console = self.client.get(reverse("account"))
        app = self.client.get(reverse("app_account"))
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        self.assertIn("app-nav", app.content.decode())
        self.assertNotIn("fm-topbar", console.content.decode())       # chi non gestisce leghe: niente console
        page = AppPagesParityTests._page
        self.assertEqual(page(console.content.decode()), page(app.content.decode()))
        self.assertContains(app, "Cosa fare in questa pagina")
