from decimal import Decimal
from django.test import TestCase
from auctions.models import Competition, Fixture, Giornata, GiornataScore, League, Participant, Season
from auctions.services.competitions import (
    generate_round_robin_schedule,
    setup_round_robin_competition,
    setup_knockout_competition,
    setup_supercoppa,
    compute_competition_standings,
)


class CompetitionsEngineTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Serie A Test")
        self.season = Season.objects.create(league=self.league, name="2026/27", matchdays=38)
        self.teams = [
            Participant.objects.create(display_name=f"Squadra {i}", league=self.league, credits=500)
            for i in range(1, 9)
        ]
        # Prepopulate matchdays
        for n in range(1, 39):
            Giornata.objects.create(season=self.season, number=n)

    def test_round_robin_schedule_algorithm(self):
        team_ids = [t.id for t in self.teams]
        rounds = generate_round_robin_schedule(team_ids)
        # 8 teams -> 7 rounds in a single cycle
        self.assertEqual(len(rounds), 7)
        for r in rounds:
            # 4 matches per round
            self.assertEqual(len(r), 4)

    def test_setup_round_robin_competition(self):
        comp = Competition.objects.create(
            season=self.season,
            name="Campionato 1vs1",
            kind=Competition.Type.ROUND_ROBIN,
        )
        fixtures = setup_round_robin_competition(comp)
        self.assertGreater(len(fixtures), 0)
        # 38 matchdays * 4 matches = 152 fixtures
        self.assertEqual(len(fixtures), 38 * 4)

        standings_data = compute_competition_standings(comp)
        self.assertEqual(standings_data["kind"], "table")
        self.assertEqual(len(standings_data["standings"]), 8)

    def test_total_points_gran_premio(self):
        comp = Competition.objects.create(
            season=self.season,
            name="Gran Premio Totale",
            kind=Competition.Type.TOTAL_POINTS,
        )
        g1 = self.season.giornate.get(number=1)
        # Give Team 1 75.5 points and Team 2 70.0 points
        GiornataScore.objects.create(giornata=g1, participant=self.teams[0], total=Decimal("75.5"), goals=2)
        GiornataScore.objects.create(giornata=g1, participant=self.teams[1], total=Decimal("70.0"), goals=1)

        res = compute_competition_standings(comp)
        self.assertEqual(res["kind"], "points")
        self.assertEqual(res["standings"][0]["team"], self.teams[0])
        self.assertEqual(res["standings"][0]["total"], Decimal("75.5"))

    def test_knockout_bracket_setup(self):
        comp = Competition.objects.create(
            season=self.season,
            name="Coppa di Lega",
            kind=Competition.Type.KNOCKOUT,
        )
        # 8 teams -> Quarti di finale
        fixtures = setup_knockout_competition(comp, start_giornata=10)
        self.assertEqual(len(fixtures), 4)
        for f in fixtures:
            self.assertEqual(f.giornata.number, 10)
            self.assertIn("Quarti", f.stage)

    def test_supercoppa_setup(self):
        comp = Competition.objects.create(
            season=self.season,
            name="Supercoppa",
            kind=Competition.Type.SUPERCOPPA,
        )
        fixtures = setup_supercoppa(comp, self.teams[0].id, self.teams[1].id, giornata_num=1)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0].stage, "Finale Secca")
        self.assertEqual(fixtures[0].home_id, self.teams[0].id)
        self.assertEqual(fixtures[0].away_id, self.teams[1].id)

    def test_ensure_league_season_and_competitions(self):
        from auctions.services.competitions import ensure_league_season_and_competitions
        new_league = League.objects.create(name="Nuova Lega")
        Participant.objects.create(display_name="Team A", league=new_league, credits=500)
        Participant.objects.create(display_name="Team B", league=new_league, credits=500)

        season, comps = ensure_league_season_and_competitions(new_league)
        self.assertIsNotNone(season)
        self.assertEqual(season.giornate.count(), 38)
        self.assertEqual(len(comps), 3)
        self.assertTrue(any(c.kind == Competition.Type.ROUND_ROBIN for c in comps))
        self.assertTrue(any(c.kind == Competition.Type.BATTLE_ROYALE for c in comps))
        self.assertTrue(any(c.kind == Competition.Type.TOTAL_POINTS for c in comps))

    def test_admin_competitions_views(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        user = User.objects.create_superuser("admin_comp", "admin@example.com", "pass123")
        self.client.force_login(user)

        # GET console view
        url = reverse("admin_competitions") + f"?league={self.league.id}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Competizioni")

        # POST create knockout cup
        create_url = reverse("admin_competition_create")
        resp = self.client.post(create_url, {
            "league_id": self.league.id,
            "name": "Coppa Italia Eliminazione",
            "kind": Competition.Type.KNOCKOUT,
            "start_giornata": 5,
        })
        self.assertEqual(resp.status_code, 302)
        new_comp = Competition.objects.filter(season=self.season, name="Coppa Italia Eliminazione").first()
        self.assertIsNotNone(new_comp)
        self.assertEqual(new_comp.fixtures.count(), 4)

        # POST regenerate
        regen_url = reverse("admin_competition_regenerate", args=[new_comp.id])
        resp = self.client.post(regen_url, {"start_giornata": 6})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(new_comp.fixtures.first().giornata.number, 6)

        # POST delete
        del_url = reverse("admin_competition_delete", args=[new_comp.id])
        resp = self.client.post(del_url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Competition.objects.filter(id=new_comp.id).exists())
