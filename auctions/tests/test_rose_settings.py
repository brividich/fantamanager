"""Rose della lega e Impostazioni lega: le stesse in console e app."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import League, Participant, Player

User = get_user_model()


class _LeagueFixture(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente_rs", password="pw")
        self.stranger = User.objects.create_user("altro_rs", password="pw")
        self.league = League.objects.create(name="Lega Rose", owner=self.owner)
        self.other = League.objects.create(name="Lega Altrui", owner=self.stranger)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa Real", credits=Decimal("500"))
        self.team2 = Participant.objects.create(league=self.league, display_name="Beta FC", credits=Decimal("500"))
        self.lautaro = Player.objects.create(name="Lautaro", role="A", team="Inter", league=self.league,
                                             owner=self.team, cost=Decimal("40"))
        self.free = Player.objects.create(name="Dimarco", role="D", team="Inter", league=self.league,
                                          initial_price=Decimal("12"))
        Player.objects.create(name="Barella", role="C", team="Inter", league=self.other)


class RosePanelParityTests(_LeagueFixture):
    """La scheda Rose della console e quella della Regia dell'app sono lo
    stesso partial (_rose_manage.html), con le stesse azioni."""

    def test_same_panel_in_console_and_app(self):
        self.client.force_login(self.owner)
        console = self.client.get(reverse("dashboard_league", args=[self.league.id])).content.decode()
        app = self.client.get(reverse("app_regia") + f"?league={self.league.id}").content.decode()
        for html in (console, app):
            self.assertIn('id="rose-manage"', html)
            self.assertIn(f'data-rm-release="{self.lautaro.id}"', html)   # Svincola
            self.assertIn(f'data-rm-assign="{self.team.id}"', html)       # Assegna
            self.assertIn(f'data-rm-credits="{self.team.id}"', html)      # Crediti
            self.assertIn("Lautaro", html)
            self.assertNotIn("Barella", html)
        self.assertIn('data-asec-go="rose"', app)

    def test_team_param_opens_that_team(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("app_regia") + f"?league={self.league.id}&team={self.team2.id}").content.decode()
        self.assertRegex(html, rf'id="rm-team-{self.team2.id}"\s+aria-label')   # visibile
        self.assertRegex(html, rf'id="rm-team-{self.team.id}"\s+hidden')

    def test_assign_from_app_returns_to_app(self):
        self.client.force_login(self.owner)
        back = reverse("app_regia") + f"?league={self.league.id}&team={self.team.id}#rose"
        resp = self.client.post(reverse("admin_quick_assign_player"), {
            "participant_id": self.team.id, "player_id": self.free.id, "price": "5", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.free.refresh_from_db()
        self.assertEqual(self.free.owner_id, self.team.id)


class LeagueSettingsPageTests(_LeagueFixture):
    """Impostazioni lega: una pagina, console e app (stessa view, stesso
    template; la parità del contenuto la prova AppPagesParityTests)."""

    def test_save_from_app_stays_in_app(self):
        self.client.force_login(self.owner)
        page = reverse("app_regia_league_settings") + f"?league={self.league.id}"
        resp = self.client.post(reverse("admin_config_action"), {
            "action": "update_league", "league_id": self.league.id, "rules_present": "1",
            "name": "Lega Rose", "budget": "600", "game_mode": "CLASSIC", "slot_limits": "1",
            "slots_p": "3", "slots_d": "8", "slots_c": "8", "slots_a": "7", "gk_max_clubs": "0",
            "trades_enabled": "1", "next": page})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("app_regia_league_settings")))
        self.league.refresh_from_db()
        self.assertEqual((self.league.budget, self.league.slots_a, self.league.trades_need_approval),
                         (Decimal("600"), 7, False))

    def test_summary_and_links(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("app_regia_league_settings") + f"?league={self.league.id}").content.decode()
        self.assertIn("Cosa fare in questa pagina", html)
        self.assertIn('name="slots_a"', html)
        for name in ("app_regia_market", "app_regia_trades", "app_giornate", "app_regia_competitions",
                     "app_regia_teams", "app_footballers"):
            self.assertIn(f'href="{reverse(name)}?league={self.league.id}', html)

    def test_only_your_league(self):
        self.client.force_login(self.stranger)
        html = self.client.get(reverse("admin_league_settings") + f"?league={self.league.id}").content.decode()
        self.assertNotIn("Lega Rose", html)
        self.assertIn("Lega Altrui", html)

    def test_reachable_from_regia_and_console(self):
        self.client.force_login(self.owner)
        app = self.client.get(reverse("app_regia") + f"?league={self.league.id}").content.decode()
        self.assertIn(f'href="{reverse("app_regia_league_settings")}?league={self.league.id}"', app)
        console = self.client.get(reverse("dashboard_league", args=[self.league.id])).content.decode()
        self.assertIn(f'href="{reverse("admin_league_settings")}?league={self.league.id}"', console)
