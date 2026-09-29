"""The league admin inside the app: Regia tab, "Vedi come", console ⇄ app links."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Auction, League, MarketSession, Participant, Player, Trade
from ..views.common import SESSION_LEAGUE_KEY


class AppRegiaTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pwd12345")
        self.other_owner = User.objects.create_user("altro_pres", password="pwd12345")
        self.manager = User.objects.create_user("mister", password="pwd12345")
        self.league = League.objects.create(name="Lega Nostra", owner=self.owner, budget=Decimal("500"))
        self.foreign = League.objects.create(name="Lega Loro", owner=self.other_owner)
        self.alfa = Participant.objects.create(league=self.league, display_name="Alfa",
                                               access_code="ALFA1", credits=Decimal("500"),
                                               spent_credits=Decimal("40"))
        self.beta = Participant.objects.create(league=self.league, display_name="Beta",
                                               user=self.manager, credits=Decimal("500"))
        self.stranger = Participant.objects.create(league=self.foreign, display_name="Gamma",
                                                   access_code="GAM1")
        Player.objects.create(name="Bomber", role="A", league=self.league, owner=self.alfa,
                              cost=Decimal("40"), initial_price=Decimal("30"))
        Player.objects.create(name="Libero", role="C", league=self.league, initial_price=Decimal("5"))

    # --- landing -----------------------------------------------------------

    def test_admin_without_team_lands_on_regia_not_login(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_home"))
        self.assertRedirects(resp, reverse("app_regia"))
        resp = self.client.get(reverse("app_login"))
        self.assertRedirects(resp, reverse("app_regia"))

    def test_regia_shows_the_dashboard_picture(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_regia"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Lega Nostra", body)
        self.assertIn("Alfa", body)
        self.assertIn("Beta", body)
        self.assertIn("Bomber", body)                        # rosters by role
        self.assertNotIn("Gamma", body)                      # other owner's team
        self.assertEqual(resp.context["kpi"]["teams"], 2)
        self.assertEqual(resp.context["kpi"]["free"], 1)
        self.assertIn(reverse("dashboard_league", args=[self.league.id]), body)  # console door

    def test_account_login_for_admin_without_team_goes_to_regia(self):
        resp = self.client.post(reverse("app_login"), {
            "login_mode": "account", "identifier": "presidente", "password": "pwd12345",
        })
        self.assertRedirects(resp, reverse("app_regia"))
        self.assertIsNone(self.client.session.get("participant_id"))

    def test_team_page_for_admin_without_team_points_to_regia(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_rosa"))
        self.assertRedirects(resp, reverse("app_regia"))

    def test_lega_and_altro_work_without_a_team(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("app_lega")), "Alfa")
        self.assertContains(self.client.get(reverse("app_altro")), "Leghe gestite")

    def test_manager_cannot_open_regia(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("app_regia"))
        self.assertRedirects(resp, reverse("app_home"))
        home = self.client.get(reverse("app_home")).content.decode()
        self.assertNotIn(reverse("app_regia"), home)

    def test_anonymous_regia_goes_to_login(self):
        resp = self.client.get(reverse("app_regia"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("app_login"), resp["Location"])

    # --- digest -------------------------------------------------------------

    def test_digest_lists_what_needs_the_admin(self):
        trade = Trade.objects.create(league=self.league, proposer=self.alfa, receiver=self.beta,
                                     status=Trade.Status.ACCEPTED)
        MarketSession.objects.create(league=self.league, title="Riparazione",
                                     status=MarketSession.Status.CLOSED)
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_regia"))
        titles = [t["title"] for t in resp.context["todo"]]
        self.assertTrue(any("ratificare" in t for t in titles))
        self.assertTrue(any("Spoglio" in t for t in titles))
        self.assertTrue(any("prima asta" in t for t in titles))
        self.assertContains(resp, reverse("admin_trade_decide", args=[trade.id]))

    def test_digest_all_good(self):
        Auction.objects.create(league=self.league, title="Estiva", status=Auction.Status.CLOSED)
        self.beta.access_code = "BETA1"
        self.beta.save()
        from ..views.app_admin import league_admin_digest
        todo = league_admin_digest(self.league)
        self.assertEqual([t["level"] for t in todo], ["ok"])

    def test_ratify_from_regia_returns_to_regia(self):
        trade = Trade.objects.create(league=self.league, proposer=self.alfa, receiver=self.beta,
                                     status=Trade.Status.ACCEPTED)
        self.client.force_login(self.owner)
        nxt = reverse("app_regia") + f"?league={self.league.id}#scambi"
        resp = self.client.post(reverse("admin_trade_decide", args=[trade.id]),
                                {"action": "veto", "next": nxt})
        self.assertEqual(resp["Location"], nxt)
        trade.refresh_from_db()
        self.assertEqual(trade.status, Trade.Status.VETOED)

    def test_ratify_ignores_offsite_next(self):
        trade = Trade.objects.create(league=self.league, proposer=self.alfa, receiver=self.beta,
                                     status=Trade.Status.ACCEPTED)
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("admin_trade_decide", args=[trade.id]),
                                {"action": "veto", "next": "https://evil.example/"})
        self.assertNotIn("evil.example", resp["Location"])

    # --- vedi come -----------------------------------------------------------

    def test_view_as_opens_the_app_as_that_team(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("app_view_as", args=[self.alfa.id]))
        self.assertRedirects(resp, reverse("app_home"))
        home = self.client.get(reverse("app_home"))
        self.assertEqual(home.context["participant"].id, self.alfa.id)
        self.assertTrue(home.context["viewing_as"])
        self.assertContains(home, "Vista admin")
        self.assertContains(self.client.get(reverse("app_rosa")), "Bomber")

    def test_view_as_refuses_foreign_league(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("app_view_as", args=[self.stranger.id]))
        self.assertIsNone(self.client.session.get("participant_id"))

    def test_view_as_refused_to_managers(self):
        self.client.force_login(self.manager)
        self.client.get(reverse("app_home"))
        self.client.post(reverse("app_view_as", args=[self.alfa.id]))
        self.assertEqual(self.client.session.get("participant_id"), self.beta.id)

    def test_exit_view_as_returns_to_regia(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("app_view_as", args=[self.alfa.id]))
        resp = self.client.post(reverse("app_view_as_exit"))
        self.assertRedirects(resp, reverse("app_regia") + f"?league={self.league.id}")
        self.assertIsNone(self.client.session.get("participant_id"))

    # --- one league across console and app -----------------------------------

    def test_regia_follows_the_console_league_and_back(self):
        second = League.objects.create(name="Seconda", owner=self.owner)
        self.client.force_login(self.owner)
        # The console picks "Seconda" → the app's Regia opens on it.
        self.client.get(reverse("dashboard_league", args=[second.id]))
        resp = self.client.get(reverse("app_regia"))
        self.assertEqual(resp.context["app_league"].id, second.id)
        # The app's switcher picks the first league → the console follows.
        self.client.get(reverse("app_regia") + f"?league={self.league.id}")
        self.assertEqual(self.client.session.get(SESSION_LEAGUE_KEY), self.league.id)

    def test_admin_with_own_team_gets_regia_card_on_home(self):
        self.beta.user = self.owner
        self.beta.save(update_fields=["user"])
        self.client.force_login(self.owner)
        home = self.client.get(reverse("app_home"))
        self.assertEqual(home.status_code, 200)
        self.assertFalse(home.context["viewing_as"])
        self.assertContains(home, "Regia di Lega Nostra")
        self.assertContains(home, reverse("app_regia"))

    def test_console_links_to_the_app_on_the_same_league(self):
        self.client.force_login(self.owner)
        body = self.client.get(reverse("dashboard_league", args=[self.league.id])).content.decode()
        self.assertIn(reverse("app_regia") + f"?league={self.league.id}", body)


class ConfigScopeTests(TestCase):
    """Impostazioni: a league admin sees and edits only their leagues."""

    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pwd12345")
        self.other = User.objects.create_user("vicino", password="pwd12345")
        self.mine = League.objects.create(name="Mia", owner=self.owner)
        self.theirs = League.objects.create(name="Altrui", owner=self.other)
        self.legacy = League.objects.create(name="Vecchia")  # no owner
        self.client.force_login(self.owner)

    def _post(self, **payload):
        return self.client.post(reverse("admin_config_action"), payload, follow=True)

    def test_page_lists_only_manageable_leagues(self):
        resp = self.client.get(reverse("admin_config"))
        names = {r["league"].name for r in resp.context["rows"]}
        self.assertEqual(names, {"Mia"})
        self.assertNotContains(resp, "Altrui")
        # A league nobody owns is the superadmin's to assign, not up for grabs.
        self.assertNotContains(resp, "Vecchia")

    def test_cannot_edit_or_delete_someone_elses_league(self):
        self._post(action="update_league", league_id=self.theirs.id, name="Presa")
        self._post(action="delete_league", league_id=self.theirs.id)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.name, "Altrui")

    def test_legacy_league_reserved_to_superusers(self):
        # Anyone can sign up: "any logged-in account" can no longer mean "may
        # edit every league that has no owner".
        self._post(action="update_league", league_id=self.legacy.id, name="Rinnovata")
        self._post(action="delete_league", league_id=self.legacy.id)
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.name, "Vecchia")
        self._post(action="clean_empty")
        self.assertTrue(League.objects.filter(pk=self.legacy.id).exists())
        self.assertFalse(League.objects.filter(pk=self.mine.id).exists())  # own empty league goes

    def test_rules_switches_saved_only_when_the_form_carries_them(self):
        self._post(action="update_league", league_id=self.mine.id, name="Mia",
                   rules_present="1", trades_enabled="1", contracts_enabled="1",
                   game_mode="MANTRA", slots_gk="3", slots_out="20")
        lg = League.objects.get(pk=self.mine.id)
        self.assertTrue(lg.trades_enabled)
        self.assertFalse(lg.trades_need_approval)
        self.assertTrue(lg.contracts_enabled)
        self.assertFalse(lg.salary_cap_enabled)
        self.assertTrue(lg.is_mantra)
        self.assertEqual(lg.total_slots, 23)
        # The legacy rename form (no rules_present) leaves the switches alone.
        self._post(action="rename_league", league_id=self.mine.id, name="Mia 2")
        lg.refresh_from_db()
        self.assertEqual(lg.name, "Mia 2")
        self.assertTrue(lg.contracts_enabled)

    def test_cannot_delete_auction_of_someone_elses_league(self):
        auction = Auction.objects.create(league=self.theirs, title="Loro")
        self._post(action="delete_auction", auction_id=auction.id)
        self.assertTrue(Auction.objects.filter(pk=auction.id).exists())


class SetupWizardSmartsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.client.force_login(self.user)

    def test_duplicate_team_names_make_one_team(self):
        import json
        from django.core.files.uploadedfile import SimpleUploadedFile
        resp = self.client.post(reverse("admin_setup_create"), {
            "name": "Lega Doppi", "budget": "300", "import_choice": "none",
            "participants_json": json.dumps([{"name": "Alfa", "credits": ""},
                                             {"name": "alfa ", "credits": "10"},
                                             {"name": "Beta", "credits": "-5"}]),
            "mode": "NEW_FROM_ZERO", "flow_mode": "call",
            "listone_file": SimpleUploadedFile("Q.csv", b"Nome,R,Squadra,Qt.A\nVlahovic,A,Juventus,30\n"),
        })
        self.assertEqual(resp.status_code, 302)
        league = League.objects.get(name="Lega Doppi")
        teams = {p.display_name: p.credits for p in Participant.objects.filter(league=league)}
        self.assertEqual(set(teams), {"Alfa", "Beta"})
        self.assertEqual(teams["Beta"], Decimal("0"))

    def test_analyze_flags_mantra_listone(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = b"Nome,R,RM,Squadra,Qt.A\nVlahovic,A,Pc,Juventus,30\nBarella,C,M;C,Inter,20\n"
        resp = self.client.post(reverse("admin_setup_analyze"),
                                {"listone_file": SimpleUploadedFile("Q.csv", csv)})
        self.assertTrue(resp.json()["mantra"])
        csv = b"Nome,R,Squadra,Qt.A\nVlahovic,A,Juventus,30\n"
        resp = self.client.post(reverse("admin_setup_analyze"),
                                {"listone_file": SimpleUploadedFile("Q.csv", csv)})
        self.assertFalse(resp.json()["mantra"])

    def test_wizard_page_knows_existing_names(self):
        League.objects.create(name="Già Qui", owner=self.user)
        resp = self.client.get(reverse("admin_setup"))
        self.assertIn("Già Qui", resp.context["existing_names"])
        self.assertContains(resp, 'id="existing-names"')


class SupervisorUserManagementTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_superuser("root", "root@x.local", "pass_root")
        self.user = User.objects.create_user("testuser", email="old@x.local", password="pass_old")
        self.client.force_login(self.superadmin)

    def test_supervisor_edit_user_success(self):
        resp = self.client.post(reverse("supervisor_dashboard"), {
            "action": "edit_user",
            "user_id": self.user.id,
            "username": "testuser_renamed",
            "email": "new@x.local",
            "first_name": "Mario",
            "last_name": "Rossi",
            "password": "new_secret_pwd",
            "role": "staff",
            "is_active": "1",
        })
        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "testuser_renamed")
        self.assertEqual(self.user.email, "new@x.local")
        self.assertEqual(self.user.first_name, "Mario")
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertTrue(self.user.check_password("new_secret_pwd"))

    def test_supervisor_cannot_demote_self(self):
        resp = self.client.post(reverse("supervisor_dashboard"), {
            "action": "edit_user",
            "user_id": self.superadmin.id,
            "username": "root",
            "email": "root@x.local",
            "role": "user",
        })
        self.assertEqual(resp.status_code, 302)
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.is_superuser)
        self.assertTrue(self.superadmin.is_active)

