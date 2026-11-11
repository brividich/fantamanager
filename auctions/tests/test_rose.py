"""Pagina Rose: le rose di tutte le squadre.

Stesso partial (_rose.html) per la console (/dashboard/rose/), la Regia
dell'app (/app/regia/rose/, stessa view) e l'app del manager (/app/rose/,
sola lettura). Le azioni (aggiungi, modifica, svincola, crediti) tornano alla
pagina da cui partono. Più il contratto scritto a mano dalla pagina Contratti.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import League, Participant, Player


class RosePageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente_ro", password="pw")
        self.stranger = User.objects.create_user("altro_ro", password="pw")
        self.league = League.objects.create(name="Lega Rose", owner=self.owner, contracts_enabled=True)
        self.other = League.objects.create(name="Lega Altrui", owner=self.stranger)
        self.alfa = Participant.objects.create(league=self.league, display_name="Alfa Real", credits=Decimal("500"),
                                               spent_credits=Decimal("40"))
        self.beta = Participant.objects.create(league=self.league, display_name="Beta United",
                                               credits=Decimal("500"))
        self.lautaro = Player.objects.create(name="Lautaro", role="A", team="Inter", league=self.league,
                                             owner=self.alfa, cost=Decimal("40"), initial_price=Decimal("30"),
                                             contract_years=2)
        self.barella = Player.objects.create(name="Barella", role="C", team="Inter", league=self.league,
                                             initial_price=Decimal("18"))
        self.foreign = Player.objects.create(name="Pulisic", role="C", team="Milan", league=self.other,
                                             initial_price=Decimal("20"))
        self.q = f"?league={self.league.id}"

    def _as_manager(self, team):
        session = self.client.session
        session["participant_id"] = team.id
        session.save()

    # --- vedere ---------------------------------------------------------------

    def test_console_and_app_show_every_roster(self):
        self.client.force_login(self.owner)
        for name in ("admin_rose", "app_regia_rose"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name) + self.q).content.decode()
                self.assertIn("Cosa fare in questa pagina", html)
                self.assertIn("Alfa Real", html)
                self.assertIn("Beta United", html)
                self.assertIn("Lautaro", html)

    def test_team_view_has_the_admin_tools(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("app_regia_rose") + self.q + f"&team={self.alfa.id}").content.decode()
        self.assertIn("Aggiungi un giocatore", html)
        self.assertIn('value="edit"', html)
        self.assertIn('value="release"', html)
        self.assertIn('value="credits"', html)
        self.assertIn('name="contract"', html)

    def test_search_lists_only_free_players_of_the_league(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("admin_rose") + self.q + f"&team={self.beta.id}&cerca=bar").content.decode()
        self.assertIn("Barella", html)
        html = self.client.get(reverse("admin_rose") + self.q + f"&team={self.beta.id}&cerca=pul").content.decode()
        self.assertNotIn("Pulisic", html)
        self.assertIn("Nessun giocatore libero", html)

    def test_manager_sees_all_rosters_read_only(self):
        self._as_manager(self.beta)
        resp = self.client.get(reverse("app_rose"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Lautaro", html)
        self.assertIn("Beta United (tu)", html)
        self.assertNotIn(reverse("admin_rose_action"), html)
        # Una rosa sola.
        html = self.client.get(reverse("app_rose") + f"?team={self.alfa.id}").content.decode()
        self.assertIn("Lautaro", html)
        self.assertNotIn('value="edit"', html)

    def test_rosa_links_to_all_rosters(self):
        self._as_manager(self.beta)
        self.assertContains(self.client.get(reverse("app_rosa")), reverse("app_rose"))

    def test_only_your_leagues(self):
        self.client.force_login(self.stranger)
        for name in ("admin_rose", "app_regia_rose"):
            with self.subTest(page=name):
                resp = self.client.get(reverse(name) + self.q)
                self.assertNotContains(resp, "Lautaro", status_code=resp.status_code)
        resp = self.client.post(reverse("admin_rose_action"), {
            "action": "release", "player_id": self.lautaro.id})
        self.assertEqual(resp.status_code, 403)
        self.lautaro.refresh_from_db()
        self.assertEqual(self.lautaro.owner_id, self.alfa.id)

    # --- gestire --------------------------------------------------------------

    def test_assign_free_player_and_back_to_the_app(self):
        self.client.force_login(self.owner)
        back = reverse("app_regia_rose") + self.q + f"&team={self.beta.id}"
        resp = self.client.post(reverse("admin_rose_action"), {
            "action": "assign", "participant_id": self.beta.id, "player_id": self.barella.id,
            "price": "25", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.barella.refresh_from_db()
        self.beta.refresh_from_db()
        self.assertEqual(self.barella.owner_id, self.beta.id)
        self.assertEqual(self.barella.cost, Decimal("25"))
        self.assertEqual(self.beta.spent_credits, Decimal("25"))
        page = self.client.get(back).content.decode()
        self.assertEqual(page.count("Barella aggiunto a"), 1)   # il messaggio una volta sola

    def test_no_player_from_another_league(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("admin_rose_action"), {
            "action": "assign", "participant_id": self.beta.id, "player_id": self.foreign.id})
        self.assertEqual(resp.status_code, 403)
        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.owner_id)

    def test_edit_price_team_and_contract(self):
        self.client.force_login(self.owner)
        # Costo e contratto, stessa squadra.
        self.client.post(reverse("admin_rose_action"), {
            "action": "edit", "player_id": self.lautaro.id, "participant_id": self.alfa.id,
            "price": "50", "contract": "3"})
        self.lautaro.refresh_from_db()
        self.alfa.refresh_from_db()
        self.assertEqual(self.lautaro.cost, Decimal("50"))
        self.assertEqual(self.lautaro.contract_years, 3)
        self.assertEqual(self.alfa.spent_credits, Decimal("50"))
        # Un'altra squadra: la vecchia riavrà il costo, il contratto resta.
        self.client.post(reverse("admin_rose_action"), {
            "action": "edit", "player_id": self.lautaro.id, "participant_id": self.beta.id,
            "price": "50", "contract": "3"})
        self.lautaro.refresh_from_db()
        self.alfa.refresh_from_db()
        self.beta.refresh_from_db()
        self.assertEqual(self.lautaro.owner_id, self.beta.id)
        self.assertEqual(self.lautaro.contract_years, 3)
        self.assertEqual(self.alfa.spent_credits, Decimal("0"))
        self.assertEqual(self.beta.spent_credits, Decimal("50"))
        # Contratto rimesso da tirare.
        self.client.post(reverse("admin_rose_action"), {
            "action": "edit", "player_id": self.lautaro.id, "participant_id": self.beta.id,
            "price": "50", "contract": ""})
        self.lautaro.refresh_from_db()
        self.assertIsNone(self.lautaro.contract_years)

    def test_release_with_the_chosen_refund(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_rose_action"), {
            "action": "release", "player_id": self.lautaro.id, "refund": "none"})
        self.lautaro.refresh_from_db()
        self.alfa.refresh_from_db()
        self.assertIsNone(self.lautaro.owner_id)
        self.assertEqual(self.alfa.spent_credits, Decimal("40"))

    def test_release_refunds_the_cost_by_default(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_rose_action"), {"action": "release", "player_id": self.lautaro.id})
        self.alfa.refresh_from_db()
        self.assertEqual(self.alfa.spent_credits, Decimal("0"))

    def test_credits(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_rose_action"), {
            "action": "credits", "participant_id": self.beta.id, "mode": "add", "amount": "15"})
        self.beta.refresh_from_db()
        self.assertEqual(self.beta.credits, Decimal("515"))

    # --- tutto registrato -----------------------------------------------------

    def test_every_change_is_logged_with_who_did_it(self):
        from ..models import ContractEvent, RosterLog
        self.client.force_login(self.owner)
        url = reverse("admin_rose_action")
        self.client.post(url, {"action": "assign", "participant_id": self.beta.id,
                               "player_id": self.barella.id, "price": "20"})
        self.client.post(url, {"action": "edit", "player_id": self.lautaro.id, "participant_id": self.alfa.id,
                               "price": "45", "contract": "1"})
        self.client.post(url, {"action": "credits", "participant_id": self.beta.id, "mode": "sub", "amount": "5"})
        self.client.post(url, {"action": "release", "player_id": self.lautaro.id})
        logs = list(RosterLog.objects.filter(participant__league=self.league).order_by("id"))
        self.assertEqual([r.action for r in logs], [
            RosterLog.Action.ADMIN_ASSIGN, RosterLog.Action.EDIT, RosterLog.Action.CREDITS,
            RosterLog.Action.ADMIN_RELEASE])
        for row in logs:
            self.assertIn("presidente_ro", row.note)
        self.assertIn("500 → 495", logs[2].note)
        event = ContractEvent.objects.get(player=self.lautaro, kind=ContractEvent.Kind.SET)
        self.assertEqual(event.years, 1)
        self.assertIn("presidente_ro", event.note)

    def test_moves_show_credits_and_refunds(self):
        self.client.force_login(self.owner)
        url = reverse("admin_rose_action")
        self.client.post(url, {"action": "credits", "participant_id": self.beta.id, "mode": "add", "amount": "15"})
        self.client.post(url, {"action": "release", "player_id": self.lautaro.id})
        html = self.client.get(reverse("admin_market_moves") + self.q).content.decode()
        self.assertIn("Bonus: budget 500 → 515 FM", html)
        self.assertIn("+15 FM", html)
        self.assertIn("+40 FM", html)      # il rimborso dello svincolo, non una spesa
        self.assertNotIn("−40 FM", html)

    def test_dashboard_credits_are_logged_too(self):
        from ..models import RosterLog
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_adjust_team_credits", args=[self.alfa.id]), {"mode": "set", "amount": "600"})
        row = RosterLog.objects.get(action=RosterLog.Action.CREDITS)
        self.assertEqual(row.participant_id, self.alfa.id)
        self.assertIn("Budget impostato: budget 500 → 600 FM", row.note)


class ContractsByHandTests(TestCase):
    """Contratti: «Imposta» c'è per ogni giocatore, anche da tirare; vuoto = da tirare."""

    def setUp(self):
        self.owner = User.objects.create_user("presidente_ct", password="pw")
        self.league = League.objects.create(name="Lega Contratti", owner=self.owner, contracts_enabled=True)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.player = Player.objects.create(name="Dimarco", role="D", team="Inter", league=self.league,
                                            owner=self.team, cost=Decimal("12"))   # da tirare

    def test_set_by_hand_also_when_pending(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("admin_contracts") + f"?league={self.league.id}").content.decode()
        self.assertIn('value="set"><input type="hidden" name="player_id" value="%d"' % self.player.id, html)
        url = reverse("admin_contracts_action")
        self.client.post(url, {"league_id": self.league.id, "action": "set", "player_id": self.player.id, "years": "4"})
        self.player.refresh_from_db()
        self.assertEqual(self.player.contract_years, 4)
        self.client.post(url, {"league_id": self.league.id, "action": "set", "player_id": self.player.id, "years": ""})
        self.player.refresh_from_db()
        self.assertIsNone(self.player.contract_years)

    def test_too_long_is_refused(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "set", "player_id": self.player.id, "years": "9"})
        self.player.refresh_from_db()
        self.assertIsNone(self.player.contract_years)
