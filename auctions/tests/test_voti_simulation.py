"""La simulazione dei voti live: solo per le prove, e solo con la lega giusta.

Scrive voti a caso: in produzione un presidente non deve poterla lanciare
sulla sua lega al posto dei voti veri, e i giocatori che sceglie non vengono
mai da altre leghe.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from ..models import Giornata, League, Player, PlayerPerformance, Season
from ..services.voti_live import fetch_simulation_live


class VotiSimulationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pw-presidente")
        self.league = League.objects.create(name="Mia", owner=self.owner, budget=Decimal("500"))
        Season.objects.create(name="2026/27", league=self.league, is_current=True)
        Player.objects.create(name="Mio", role="A", team="Inter", league=self.league)
        other = League.objects.create(name="Altrui", budget=Decimal("500"))
        Player.objects.create(name="Altrui", role="A", team="Milan", league=other)

    def test_only_the_requested_league(self):
        names = {r["name"] for r in fetch_simulation_live(1, leagues=[self.league])}
        self.assertEqual(names, {"Mio"})

    @override_settings(DEBUG=False)
    def test_a_league_admin_cannot_run_it_in_production(self):
        self.client.force_login(self.owner)
        resp = self.client.post("/dashboard/giornate/live-sync/", {
            "giornata_number": "1", "provider": "simulation", "league_id": self.league.id}, follow=True)
        self.assertContains(resp, "solo per le prove")
        self.assertFalse(PlayerPerformance.objects.exists())

    @override_settings(DEBUG=False)
    def test_the_superuser_can(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.it", "pw-root-123"))
        resp = self.client.post("/dashboard/giornate/live-sync/", {
            "giornata_number": "1", "provider": "simulation", "league_id": self.league.id}, follow=True)
        self.assertNotContains(resp, "solo per le prove")
        self.assertTrue(Giornata.objects.filter(season__league=self.league, number=1).exists())
