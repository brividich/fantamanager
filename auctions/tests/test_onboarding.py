"""Onboarding (Fase B): una sola strada per creare la lega (il wizard, anche
nell'app), inviti che collegano la squadra all'account in un passo, stato
degli inviti e condivisione per squadra, co-admin, card «Prepara la lega»,
misure per il Supervisor e il percorso minimo «10 squadre invitate»."""
import json
import re
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core import mail as outbox
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import (
    AccountPrivacy,
    Auction,
    CoAdminInvite,
    Footballer,
    League,
    LegalAcceptance,
    Participant,
    Player,
)
from ..services import onboarding

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
MAIL = {"EMAIL_HOST": "smtp.env.local", "FM_SITE_URL": "http://testserver"}


# --- Parser delle squadre ------------------------------------------------------

class TeamParserTests(TestCase):
    def parse(self, text):
        return [(r["name"], r["email"]) for r in onboarding.parse_team_lines(text)]

    def test_separators_only_around_an_email(self):
        self.assertEqual(self.parse("Real Madrink; mario@x.it"), [("Real Madrink", "mario@x.it")])
        self.assertEqual(self.parse("Real Madrink, Mario@X.it"), [("Real Madrink", "mario@x.it")])
        self.assertEqual(self.parse("Real Madrink\tmario@x.it"), [("Real Madrink", "mario@x.it")])
        self.assertEqual(self.parse("Real Madrink - mario@x.it"), [("Real Madrink", "mario@x.it")])
        self.assertEqual(self.parse("mario@x.it; Real Madrink"), [("Real Madrink", "mario@x.it")])

    def test_comma_without_email_stays_in_the_name(self):
        self.assertEqual(self.parse("Nome, con virgola"), [("Nome, con virgola", "")])
        self.assertEqual(self.parse("Real - Madrid"), [("Real - Madrid", "")])

    def test_only_email_proposes_the_name(self):
        rows = onboarding.parse_team_lines("giulia.bianchi@x.it")
        self.assertEqual((rows[0]["name"], rows[0]["email"]), ("Giulia Bianchi", "giulia.bianchi@x.it"))
        self.assertTrue(rows[0]["name_from_email"])

    def test_blank_lines_and_duplicates(self):
        rows = onboarding.parse_team_lines("\nAlfa; a@x.it\n\n   \nalfa\nBeta; a@x.it\n")
        self.assertEqual([r["name"] for r in rows], ["Alfa", "alfa", "Beta"])
        self.assertEqual(rows[1]["warnings"], ["Nome già in elenco"])
        self.assertEqual(rows[2]["warnings"], ["Email già in elenco"])

    def test_the_wizard_uses_the_same_rules(self):
        """Il JavaScript del wizard porta le stesse espressioni del parser Python."""
        html = (TEMPLATES_DIR / "auctions" / "setup_wizard.html").read_text(encoding="utf-8")
        self.assertIn(onboarding.EMAIL_RE.pattern, html)


# --- Una sola strada: il wizard -------------------------------------------------

class WizardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("presidente", password="pw", email="p@x.it")
        self.client.force_login(self.user)
        Footballer.objects.create(api_id=1, name="Vlahovic", role="A", club_name="Juventus", in_serie_a=True)
        Footballer.objects.create(api_id=2, name="Maignan", role="P", club_name="Milan", in_serie_a=True)

    def _create(self, **extra):
        data = {"name": "Lega Senza File", "start_choice": "new", "budget": "500",
                "teams_text": "Alfa; alfa@x.it\nBeta, beta@x.it\nGamma", "create_auction": "0"}
        data.update(extra)
        return self.client.post(reverse("admin_setup_create"), data)

    def test_creates_the_league_without_a_file_with_the_general_listone(self):
        resp = self._create()
        league = League.objects.get(name="Lega Senza File")
        self.assertRedirects(resp, reverse("admin_setup_done", args=[league.id]), fetch_redirect_response=False)
        self.assertFalse(league.own_listone)
        self.assertEqual(set(Player.objects.filter(league=league).values_list("name", flat=True)),
                         {"Vlahovic", "Maignan"})
        teams = dict(Participant.objects.filter(league=league).values_list("display_name", "email"))
        self.assertEqual(teams, {"Alfa": "alfa@x.it", "Beta": "beta@x.it", "Gamma": ""})
        self.assertEqual(league.created_via, "wizard")
        self.assertTrue(league.owner == self.user)

    def test_own_listone_choice_needs_its_file(self):
        resp = self._create(start_choice="listone")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(League.objects.filter(name="Lega Senza File").exists())

    def test_presets_and_contracts(self):
        self._create(game_mode="MANTRA", slots_gk="3", slots_out="22", contracts_enabled="1")
        league = League.objects.get(name="Lega Senza File")
        self.assertTrue(league.is_mantra)
        self.assertTrue(league.contracts_enabled)

    def test_emails_of_teams_from_a_roster_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        listone = b"Id,Nome,R,Squadra,Qt.A\n7212,Lautaro,A,Inter,34\n"
        rose = "$,$,$\nDinamo Losca,7212,6\n".encode()
        self.client.post(reverse("admin_setup_create"), {
            "name": "Lega Rose", "start_choice": "rose", "import_choice": "leghe", "source_site": "leghe",
            "create_auction": "0", "detected_emails_json": json.dumps({"Dinamo Losca": "dinamo@x.it"}),
            # Un elenco scritto prima di scegliere le rose non crea squadre in più.
            "teams_text": "Squadra Avanzata; extra@x.it",
            "listone_file": SimpleUploadedFile("Quotazioni.csv", listone, content_type="text/csv"),
            "rose_file": SimpleUploadedFile("rose.csv", rose, content_type="text/csv"),
        })
        self.assertEqual(Participant.objects.get(display_name="Dinamo Losca").email, "dinamo@x.it")
        self.assertFalse(Participant.objects.filter(display_name="Squadra Avanzata").exists())

    def test_from_the_app_lands_on_the_app(self):
        resp = self._create(**{"from": "app"})
        league = League.objects.get(name="Lega Senza File")
        self.assertEqual(resp["Location"], reverse("app_regia_setup_done", args=[league.id]))
        self.assertEqual(league.created_via, "app")
        page = self.client.get(resp["Location"])
        self.assertContains(page, "app-nav")
        self.assertContains(page, "Prepara la lega")

    def test_wizard_in_console_and_app(self):
        from .test_app_pages import AppPagesParityTests
        console = self.client.get(reverse("admin_setup"))
        app = self.client.get(reverse("app_regia_setup"))
        self.assertEqual(self.client.get(reverse("app_setup")).status_code, 200)
        self.assertContains(app, "app-nav")
        self.assertNotContains(console, "app-nav")
        page = AppPagesParityTests._page
        self.assertEqual(page(console.content.decode()), page(app.content.decode()))
        self.assertContains(console, "Cosa fare in questa pagina")

    def test_every_way_in_leads_to_the_wizard(self):
        # Il vecchio form: un redirect, e nessun link ci porta più.
        self.assertRedirects(self.client.get(reverse("admin_create_league")), reverse("admin_setup"),
                             fetch_redirect_response=False)
        old = reverse("admin_create_league")
        for path in TEMPLATES_DIR.rglob("*.html"):
            if path.name == "league_form.html":
                continue
            self.assertNotIn("'admin_create_league'", path.read_text(encoding="utf-8"), path)
        # Onboarding, stato vuoto della dashboard e Supervisor portano al wizard.
        self.assertContains(self.client.get(reverse("onboarding")), reverse("admin_setup"))
        resp = self.client.post(reverse("onboarding"), {"action": "create_league", "name": "X"})
        self.assertRedirects(resp, reverse("admin_setup"), fetch_redirect_response=False)
        self.assertFalse(League.objects.filter(name="X").exists())
        # Chi non ha leghe dalla dashboard torna a «Cosa vuoi fare?»; lo stato
        # vuoto lo vede il superuser.
        self.assertRedirects(self.client.get(reverse("dashboard_hub")), reverse("onboarding"),
                             fetch_redirect_response=False)
        root = User.objects.create_superuser("root_w", "r@x.it", "pw")
        self.client.force_login(root)
        hub = self.client.get(reverse("dashboard_hub")).content.decode()
        self.assertIn(reverse("admin_setup"), hub)
        self.assertNotIn(f'href="{old}"', hub)
        sup = self.client.get(reverse("supervisor_dashboard") + "?tab=leagues").content.decode()
        self.assertIn(reverse("admin_setup"), sup)
        self.assertNotIn(f'href="{old}"', sup)


# --- L'invito: account e squadra in un passo -----------------------------------

@override_settings(FM_LEGAL_REQUIRED=True)
class InviteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_inv", first_name="Paolo", password="pw")
        self.league = League.objects.create(name="Lega Inviti", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Real Madrink",
                                               email="mario@x.it", credits=Decimal("500"))
        self.url = reverse("invite", args=[self.team.public_token])

    def test_page_says_who_where_and_as_whom(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Paolo")
        self.assertContains(page, "Lega Inviti")
        self.assertContains(page, "Real Madrink")
        self.assertContains(page, 'value="real_madrink"')        # username proposto
        self.assertContains(page, 'value="mario@x.it"')
        self.assertIsNotNone(Participant.objects.get(pk=self.team.pk).invite_opened_at)

    def test_create_account_and_link_the_team(self):
        resp = self.client.post(self.url, {"action": "signup", "username": "mario", "email": "mario@x.it",
                                           "password": "Ombrello-Verde-77", "accept_terms": "1", "accept_age": "1"})
        self.assertRedirects(resp, reverse("app_home"), fetch_redirect_response=False)
        user = User.objects.get(username="mario")
        team = Participant.objects.get(pk=self.team.pk)
        self.assertEqual(team.user, user)
        self.assertIsNotNone(team.invite_accepted_at)
        self.assertIsNotNone(AccountPrivacy.objects.get(user=user).email_verified_at)   # stessa email dell'invito
        self.assertEqual(LegalAcceptance.objects.filter(user=user).count(), 3)
        self.assertEqual(self.client.session["participant_id"], team.id)
        self.assertIsNotNone(League.objects.get(pk=self.league.pk).first_team_joined_at)

    def test_signup_needs_the_boxes(self):
        self.client.post(self.url, {"action": "signup", "username": "mario", "email": "mario@x.it",
                                    "password": "Ombrello-Verde-77"})
        self.assertFalse(User.objects.filter(username="mario").exists())

    def test_existing_account_links_through_the_same_rule(self):
        User.objects.create_user("gia_mio", password="Password-Lunga-9")
        self.client.post(self.url, {"action": "login", "identifier": "gia_mio", "password": "Password-Lunga-9"})
        self.assertEqual(Participant.objects.get(pk=self.team.pk).user.username, "gia_mio")
        # Un admin della lega che apre il link sta visitando: niente collegamento.
        other = Participant.objects.create(league=self.league, display_name="Atletico")
        self.client.force_login(self.owner)
        self.client.post(reverse("invite", args=[other.public_token]), {"action": "link"})
        self.assertIsNone(Participant.objects.get(pk=other.pk).user)

    def test_team_of_another_account_is_not_taken(self):
        first = User.objects.create_user("primo", password="pw")
        self.team.user = first
        self.team.save()
        intruder = User.objects.create_user("secondo", password="pw")
        self.client.force_login(intruder)
        page = self.client.post(self.url, {"action": "link"})
        self.assertContains(page, "già collegata a un altro account")
        self.assertEqual(Participant.objects.get(pk=self.team.pk).user, first)

    def test_guest_enters_without_account(self):
        resp = self.client.post(self.url, {"action": "guest"})
        self.assertRedirects(resp, reverse("app_home"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["participant_id"], self.team.id)
        self.assertIsNone(Participant.objects.get(pk=self.team.pk).user)
        # Con l'asta in corso si va dritti all'asta.
        auction = Auction.objects.create(league=self.league, title="Asta", status=Auction.Status.LIVE)
        resp = self.client.post(self.url, {"action": "guest"})
        self.assertRedirects(resp, reverse("bid", args=[auction.id]), fetch_redirect_response=False)

    def test_invalid_token_explains_what_to_do(self):
        resp = self.client.get(reverse("invite", args=["non-esiste-davvero-123"]))
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "Chiedi al presidente", status_code=404)
        self.assertContains(resp, reverse("app_login") + "?mode=code", status_code=404)

    def test_regenerated_link_stops_working(self):
        old = self.url
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_reset_team_pin", args=[self.team.id]),
                         {"pin": self.team.access_code or "", "regenerate_token": "1"})
        self.client.logout()
        self.assertEqual(self.client.get(old).status_code, 404)

    def test_old_links_still_work(self):
        # /app/login/?t= e /join/?t= di una squadra senza account: la pagina d'invito.
        resp = self.client.get(reverse("app_login") + f"?t={self.team.public_token}")
        self.assertTrue(resp["Location"].startswith(self.url))
        resp = self.client.get(reverse("join") + f"?t={self.team.public_token}")
        self.assertRedirects(resp, self.url, fetch_redirect_response=False)
        # Con l'asta in corso /join/ porta ancora nell'asta.
        auction = Auction.objects.create(league=self.league, title="Asta", status=Auction.Status.LIVE)
        resp = self.client.get(reverse("join") + f"?t={self.team.public_token}")
        self.assertRedirects(resp, reverse("bid", args=[auction.id]), fetch_redirect_response=False)
        # Squadra già collegata a un account: il vecchio link fa entrare come prima.
        self.team.user = User.objects.create_user("collegato", password="pw")
        self.team.save()
        resp = self.client.get(reverse("app_login") + f"?t={self.team.public_token}")
        self.assertEqual(resp["Location"], reverse("app_home"))

    def test_join_with_code_uses_the_same_link(self):
        user = User.objects.create_user("con_codice", password="pw")
        self.team.access_code = "REAL0101"
        self.team.save()
        self.client.force_login(user)
        self.client.post(reverse("onboarding"), {"action": "join_team", "access_code": "real0101"})
        self.assertEqual(Participant.objects.get(pk=self.team.pk).user, user)
        self.assertIsNotNone(Participant.objects.get(pk=self.team.pk).invite_accepted_at)
        # Incollare il link porta alla pagina d'invito.
        resp = self.client.post(reverse("onboarding"), {"action": "join_team",
                                                        "access_code": "http://testserver" + self.url})
        self.assertRedirects(resp, self.url, fetch_redirect_response=False)

    def test_portal_with_several_teams_asks_which(self):
        user = User.objects.create_user("due_squadre", password="pw")
        other_league = League.objects.create(name="Altra")
        Participant.objects.create(league=self.league, display_name="Uno", user=user)
        Participant.objects.create(league=other_league, display_name="Due", user=user)
        self.client.force_login(user)
        resp = self.client.get(reverse("home"))
        self.assertTrue(resp["Location"].startswith(reverse("app_login") + "?switch=1"))


# --- Stato degli inviti e condivisione -----------------------------------------

class InviteStatusTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_st", password="pw")
        self.league = League.objects.create(name="Lega Stato", owner=self.owner)
        self.a = Participant.objects.create(league=self.league, display_name="Alfa", email="a@x.it")
        self.b = Participant.objects.create(league=self.league, display_name="Beta", email="b@x.it")
        self.c = Participant.objects.create(league=self.league, display_name="Gamma")
        self.client.force_login(self.owner)

    @override_settings(**MAIL)
    def test_sent_opened_joined_and_resend_only_to_who_is_missing(self):
        self.client.post(reverse("admin_invite_teams"), {"league_id": self.league.id})
        self.assertEqual(len(outbox.outbox), 2)
        self.assertIn(reverse("invite", args=[self.a.public_token]), outbox.outbox[0].body + outbox.outbox[1].body)
        self.a.refresh_from_db()
        self.assertEqual(self.a.invite_last_channel, "email")
        self.assertEqual(onboarding.invite_status(self.a)[0], "sent")
        self.assertIsNotNone(League.objects.get(pk=self.league.pk).first_invite_at)
        self.client.logout()
        self.client.get(reverse("invite", args=[self.a.public_token]))
        self.a.refresh_from_db()
        self.assertEqual(onboarding.invite_status(self.a)[0], "open")
        onboarding.link_team(User.objects.create_user("alfa_user", password="pw"), self.a)
        self.a.refresh_from_db()
        self.assertEqual(onboarding.invite_status(self.a), ("in", "Entrata ✓"))
        outbox.outbox.clear()
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_invite_missing", args=[self.league.id]))
        self.assertEqual([m.to for m in outbox.outbox], [["b@x.it"]])

    def test_share_marks_the_channel(self):
        resp = self.client.post(reverse("admin_invite_mark", args=[self.c.id]), {"channel": "whatsapp"})
        self.assertEqual(resp.json()["status"], "sent")
        self.c.refresh_from_db()
        self.assertEqual(self.c.invite_last_channel, "whatsapp")
        stranger = User.objects.create_user("estraneo", password="pw", is_staff=True)
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(reverse("admin_invite_mark", args=[self.c.id]),
                                          {"channel": "copy"}).status_code, 403)

    def test_shared_text_has_only_that_team_link(self):
        page = self.client.get(reverse("admin_participants") + f"?league={self.league.id}").content.decode()
        tokens = [self.a.public_token, self.b.public_token, self.c.public_token]
        texts = re.findall(r'data-text="([^"]*)"', page)
        self.assertEqual(len(texts), 3)
        for text in texts:
            self.assertEqual(sum(1 for t in tokens if t in text), 1, text)
        self.assertIn("Da invitare", page)

    def test_add_team_from_the_shared_page(self):
        back = reverse("app_regia_teams") + f"?league={self.league.id}"
        resp = self.client.post(reverse("admin_create_participant"), {
            "league_id": self.league.id, "display_name": "Delta", "email": "d@x.it", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.assertEqual(Participant.objects.get(display_name="Delta").email, "d@x.it")
        page = self.client.get(back).content.decode()
        self.assertIn(reverse("app_regia_players"), page)       # «Importa rose» resta nell'app

    def test_mail_off_message_for_president_and_superuser(self):
        page = self.client.get(reverse("admin_participants") + f"?league={self.league.id}")
        self.assertContains(page, "invio email non è attivo su questo sito")
        self.assertNotContains(page, "Impostazioni → Posta")
        root = User.objects.create_superuser("root_m", "r@x.it", "pw")
        self.client.force_login(root)
        page = self.client.get(reverse("admin_participants") + f"?league={self.league.id}")
        self.assertContains(page, "Impostazioni → Posta")


# --- Co-admin ---------------------------------------------------------------------

@override_settings(FM_LEGAL_REQUIRED=True)
class CoAdminTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_ca", password="pw")
        self.league = League.objects.create(name="Lega Co", owner=self.owner)
        self.other = League.objects.create(name="Lega Altri", owner=User.objects.create_user("altro_pres", password="pw"))

    def _invite(self, email=""):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_coadmin", args=[self.league.id]), {"action": "invite", "email": email})
        return CoAdminInvite.objects.filter(league=self.league).latest("created_at")

    def test_invite_accept_remove(self):
        inv = self._invite("vice@x.it")
        self.client.logout()
        resp = self.client.post(reverse("coadmin_invite", args=[inv.token]), {
            "action": "signup", "username": "vice", "email": "vice@x.it", "password": "Ombrello-Verde-77",
            "accept_terms": "1", "accept_age": "1"})
        self.assertEqual(resp.status_code, 302)
        vice = User.objects.get(username="vice")
        self.assertIn(vice, self.league.admins.all())
        self.assertNotIn(vice, self.other.admins.all())
        # Il link vale una volta.
        self.assertEqual(self.client.get(reverse("coadmin_invite", args=[inv.token])).status_code, 404)
        # Il co-admin gestisce questa lega, non l'altra.
        self.assertEqual(self.client.get(reverse("admin_participants") + f"?league={self.other.id}").status_code, 403)
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_coadmin", args=[self.league.id]), {"action": "remove", "user_id": vice.id})
        self.assertNotIn(vice, self.league.admins.all())

    def test_owner_is_not_removable_and_strangers_get_nothing(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_coadmin", args=[self.league.id]), {"action": "remove", "user_id": self.owner.id})
        self.assertEqual(League.objects.get(pk=self.league.pk).owner, self.owner)
        stranger = User.objects.create_user("intruso", password="pw", is_staff=True)
        self.client.force_login(stranger)
        resp = self.client.post(reverse("admin_coadmin", args=[self.league.id]), {"action": "invite", "email": "x@x.it"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(CoAdminInvite.objects.exists())

    def test_existing_account_accepts(self):
        inv = self._invite()
        user = User.objects.create_user("esistente", password="pw")
        self.client.force_login(user)
        self.client.post(reverse("coadmin_invite", args=[inv.token]), {"action": "accept"})
        self.assertIn(user, self.league.admins.all())

    def test_revoked_invite_opens_nothing(self):
        inv = self._invite()
        self.client.post(reverse("admin_coadmin", args=[self.league.id]), {"action": "revoke", "invite_id": inv.id})
        self.client.logout()
        self.assertEqual(self.client.get(reverse("coadmin_invite", args=[inv.token])).status_code, 404)


# --- «Prepara la lega» e misure --------------------------------------------------

class SetupCardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_card", password="pw")
        self.league = League.objects.create(name="Lega Card", owner=self.owner)
        for n in ("Alfa", "Beta"):
            Participant.objects.create(league=self.league, display_name=n)
        self.client.force_login(self.owner)

    def test_card_in_dashboard_and_regia_until_ready(self):
        for url in (reverse("dashboard_league", args=[self.league.id]), reverse("app_regia") + f"?league={self.league.id}"):
            self.assertContains(self.client.get(url), "Prepara la lega")
        steps = {s["key"]: s for s in onboarding.setup_steps(self.league)}
        self.assertTrue(steps["teams"]["done"])
        self.assertTrue(steps["invites"]["now"])
        self.client.post(reverse("admin_setup_card", args=[self.league.id]), {"action": "rules_checked"})
        self.assertTrue(League.objects.get(pk=self.league.pk).setup_state["rules_checked"])
        self.client.post(reverse("admin_setup_card", args=[self.league.id]), {"action": "hide"})
        self.assertNotContains(self.client.get(reverse("dashboard_league", args=[self.league.id])), 'id="prepara-lega"')

    def test_ready_when_all_done(self):
        users = [User.objects.create_user(f"u{i}", password="pw") for i in range(2)]
        for t, u in zip(Participant.objects.filter(league=self.league), users):
            onboarding.link_team(u, t)
        onboarding.set_setup_flag(self.league, "rules_checked")
        Auction.objects.create(league=self.league, title="Asta")
        self.assertIsNone(onboarding.setup_card(League.objects.get(pk=self.league.pk)))
        self.assertIsNotNone(League.objects.get(pk=self.league.pk).ready_at)

    def test_funnel_in_the_supervisor(self):
        root = User.objects.create_superuser("root_f", "r@x.it", "pw")
        self.client.force_login(root)
        page = self.client.get(reverse("supervisor_dashboard") + "?tab=leagues")
        self.assertContains(page, "Leghe create")
        self.assertEqual(onboarding.funnel()["total"], 1)


# --- Primo avvio dell'app del PC -------------------------------------------------

@override_settings(DESKTOP_APP=True)
class DesktopFirstRunTests(TestCase):
    def test_first_screen_creates_the_pc_admin_then_asks_what_to_do(self):
        resp = self.client.get(reverse("home"))
        self.assertRedirects(resp, reverse("register"), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("register")), "Crea l'amministratore di questo PC")
        resp = self.client.post(reverse("register"), {"username": "admin_pc", "password": "Ombrello-Verde-77",
                                                      "password_confirm": "Ombrello-Verde-77"})
        self.assertRedirects(resp, reverse("onboarding"), fetch_redirect_response=False)
        page = self.client.get(reverse("onboarding"))
        self.assertContains(page, "Scarico una lega dal sito")
        self.assertContains(page, "Apro un backup")
        self.assertTrue(User.objects.get(username="admin_pc").is_superuser)


# --- Il percorso minimo --------------------------------------------------------------

@override_settings(FM_LEGAL_REQUIRED=True, **MAIL)
class MinimalPathTests(TestCase):
    def test_ten_teams_with_email_and_invites_in_at_most_eight_actions(self):
        posts = []
        original = self.client.post

        def counted(*a, **kw):
            posts.append(a[0])
            return original(*a, **kw)

        self.client.post(reverse("register"), {
            "username": "nuovo_presidente", "email": "pres@x.it", "password": "Ombrello-Verde-77",
            "password_confirm": "Ombrello-Verde-77", "accept_terms": "1", "accept_age": "1"})
        outbox.outbox.clear()
        self.client.post = counted
        # Dopo la registrazione: «Cosa vuoi fare?» → Creo una lega → wizard.
        page = self.client.get(reverse("onboarding")).content.decode()
        self.assertIn(reverse("admin_setup"), page)
        self.assertEqual(self.client.get(reverse("admin_setup")).status_code, 200)
        lines = "\n".join(f"Squadra {i}; allenatore{i}@x.it" for i in range(1, 11))
        resp = self.client.post(reverse("admin_setup_create"), {
            "name": "Lega dei Dieci", "start_choice": "new", "teams_text": lines,
            "send_invites": "1", "create_auction": "0", "from": "console"})
        league = League.objects.get(name="Lega dei Dieci")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Participant.objects.filter(league=league).count(), 10)
        self.assertEqual(len(outbox.outbox), 10)
        self.assertLessEqual(len(posts), 8)
        self.assertTrue(all(p.invite_sent_at for p in Participant.objects.filter(league=league)))
        # L'allenatore: un clic sul link e un modulo.
        link = re.search(r"http://testserver(/invito/[^/\s]+/)", outbox.outbox[0].body).group(1)
        self.client.logout()
        self.client.post = original
        self.assertEqual(self.client.get(link).status_code, 200)
        self.client.post(link, {"action": "signup", "username": "allenatore1", "email": "allenatore1@x.it",
                                "password": "Ombrello-Verde-77", "accept_terms": "1", "accept_age": "1"})
        team = Participant.objects.get(league=league, email="allenatore1@x.it")
        self.assertEqual(team.user.username, "allenatore1")
