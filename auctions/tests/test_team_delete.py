"""Eliminare una squadra creata per sbaglio (services/teams.py), dalla Scheda
della pagina Squadre: la stessa in console e nell'app."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Giornata, GiornataScore, League, Participant, Player, RosterLog, Season


class TeamDeleteTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_del", password="pw")
        self.stranger = User.objects.create_user("altro_del", password="pw")
        self.league = League.objects.create(name="Lega Doppioni", owner=self.owner)
        self.keep = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.extra = Participant.objects.create(league=self.league, display_name="Alfa (doppia)",
                                                credits=Decimal("500"))
        self.player = Player.objects.create(name="Lautaro", role="A", team="Inter", league=self.league,
                                            owner=self.extra, cost=Decimal("40"))
        self.url = reverse("admin_delete_participant", args=[self.extra.id])

    def test_extra_team_goes_and_its_players_are_free(self):
        self.client.force_login(self.owner)
        resp = self.client.post(self.url, {"confirm": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Participant.objects.filter(pk=self.extra.id).exists())
        self.player.refresh_from_db()
        self.assertIsNone(self.player.owner_id)
        self.assertEqual(self.player.cost, 0)
        self.assertTrue(RosterLog.objects.filter(player_name="Lautaro", participant_name="Alfa (doppia)").exists())
        self.assertTrue(Participant.objects.filter(pk=self.keep.id).exists())

    def test_needs_the_confirmation(self):
        self.client.force_login(self.owner)
        self.client.post(self.url)
        self.assertTrue(Participant.objects.filter(pk=self.extra.id).exists())

    def test_team_that_played_stays(self):
        season = Season.objects.create(name="2026/27", league=self.league, is_current=True)
        giornata = Giornata.objects.create(season=season, number=1)
        GiornataScore.objects.create(giornata=giornata, participant=self.extra, total=Decimal("70"))
        self.client.force_login(self.owner)
        resp = self.client.post(self.url, {"confirm": "1"}, follow=True)
        self.assertTrue(Participant.objects.filter(pk=self.extra.id).exists())
        self.assertContains(resp, "non si può eliminare")
        page = self.client.get(reverse("admin_participants") + f"?league={self.league.id}").content.decode()
        self.assertIn("ha già giocato", page)

    def test_only_who_runs_the_league(self):
        self.client.force_login(self.stranger)
        self.client.post(self.url, {"confirm": "1"})
        self.assertTrue(Participant.objects.filter(pk=self.extra.id).exists())

    def test_same_button_in_console_and_app(self):
        self.client.force_login(self.owner)
        q = f"?league={self.league.id}"
        for name in ("admin_participants", "app_regia_teams"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name) + q).content.decode()
                self.assertIn(self.url, html)
                self.assertIn("Elimina squadra", html)
