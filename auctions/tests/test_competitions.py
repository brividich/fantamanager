from decimal import Decimal
from django.test import TestCase
from auctions.models import Competition, Fixture, Formation, Giornata, GiornataScore, League, Participant, Player, PlayerPerformance, Season
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

    def test_get_competition_matchdays_and_app_view(self):
        from auctions.services.competitions import get_competition_matchdays
        comp = Competition.objects.create(
            season=self.season,
            name="Campionato 1vs1",
            kind=Competition.Type.ROUND_ROBIN,
        )
        setup_round_robin_competition(comp)

        # Mark Giornata 1 as scored and create GiornataScores
        g1 = self.season.giornate.get(number=1)
        g1.status = Giornata.Status.SCORED
        g1.save()
        GiornataScore.objects.create(giornata=g1, participant=self.teams[0], total=Decimal("78.0"), goals=2)
        GiornataScore.objects.create(giornata=g1, participant=self.teams[1], total=Decimal("71.5"), goals=1)

        # Verify get_competition_matchdays returns all 38 matchdays
        matchdays = get_competition_matchdays(comp, participant_id=self.teams[0].id)
        self.assertEqual(len(matchdays), 38)
        self.assertEqual(matchdays[0]["giornata"].number, 1)
        self.assertTrue(matchdays[0]["has_user_match"])
        self.assertTrue(matchdays[0]["is_scored"])

        # Test app_lega view rendering with competitions and tab=giornate
        user_participant = self.teams[0]
        # Login participant via session
        session = self.client.session
        session["participant_id"] = user_participant.id
        session.save()

        from django.urls import reverse
        resp = self.client.get(reverse("app_lega") + f"?comp={comp.id}&tab=giornate")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Tutte le Giornate")
        self.assertContains(resp, "Giornata 1")
        self.assertContains(resp, "La tua sfida")

    def test_setup_groups_knockout_competition(self):
        from auctions.services.competitions import setup_groups_knockout_competition
        comp = Competition.objects.create(
            season=self.season,
            name="Coppa a Gironi Test",
            kind=Competition.Type.GROUPS_KNOCKOUT,
        )
        fixtures = setup_groups_knockout_competition(comp, start_giornata=1)
        self.assertGreater(len(fixtures), 0)
        self.assertTrue(any("Girone A" in f.stage for f in fixtures))
        self.assertTrue(any("Girone B" in f.stage for f in fixtures))

    def test_app_competition_wizard_and_creation(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        owner = User.objects.create_user("league_boss", "boss@x.local", "secret")
        self.league.owner = owner
        self.league.save()
        self.client.force_login(owner)

        # GET app_lega with open_comp_wizard=1
        resp = self.client.get(reverse("app_lega") + f"?league={self.league.id}&open_comp_wizard=1")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "comp-wizard-modal")
        self.assertContains(resp, "Nuova Competizione")

        # GET app_regia
        regia = self.client.get(reverse("app_regia") + f"?league={self.league.id}")
        self.assertEqual(regia.status_code, 200)
        self.assertContains(regia, "Nuova Competizione")

        # POST admin_competition_create with from=app
        create_url = reverse("admin_competition_create")
        post_resp = self.client.post(create_url, {
            "league_id": self.league.id,
            "from": "app",
            "name": "Champions League App",
            "kind": Competition.Type.GROUPS_KNOCKOUT,
            "start_giornata": 1,
            "win_points": 3,
            "draw_points": 1,
            "loss_points": 0,
            "goal_threshold": 66.0,
            "goal_step": 6.0,
            "home_bonus": 1.0,
        })
        self.assertEqual(post_resp.status_code, 302)
        new_comp = Competition.objects.filter(season=self.season, name="Champions League App").first()
        self.assertIsNotNone(new_comp)
        self.assertIn(f"?comp={new_comp.id}&tab=competizioni", post_resp["Location"])
        self.assertEqual(new_comp.settings["home_bonus"], 1.0)
        self.assertGreater(new_comp.fixtures.count(), 0)

    def test_new_formats_standings_computation(self):
        f1_comp = Competition.objects.create(
            season=self.season, name="GP F1", kind=Competition.Type.FORMULA_1
        )
        survival_comp = Competition.objects.create(
            season=self.season, name="Survival", kind=Competition.Type.SURVIVAL
        )
        swiss_comp = Competition.objects.create(
            season=self.season, name="Swiss", kind=Competition.Type.SWISS_LEAGUE
        )
        davis_comp = Competition.objects.create(
            season=self.season, name="Davis", kind=Competition.Type.FANTA_DAVIS
        )

        # Compute standings for each
        f1_data = compute_competition_standings(f1_comp)
        self.assertEqual(f1_data["kind"], "formula_1")
        self.assertEqual(len(f1_data["standings"]), 8)

        survival_data = compute_competition_standings(survival_comp)
        self.assertEqual(survival_data["kind"], "survival")
        self.assertEqual(survival_data["alive_count"], 8)

        swiss_data = compute_competition_standings(swiss_comp)
        self.assertEqual(swiss_data["kind"], "swiss_league")

        davis_data = compute_competition_standings(davis_comp)
        self.assertEqual(davis_data["kind"], "fanta_davis")
        self.assertGreaterEqual(len(davis_data["pairs"]), 4)

    def test_fixture_details_and_api(self):
        comp = Competition.objects.create(
            season=self.season,
            name="Campionato Dettagli",
            kind=Competition.Type.ROUND_ROBIN,
        )
        setup_round_robin_competition(comp)
        g1 = self.season.giornate.get(number=1)
        fx = comp.fixtures.filter(giornata=g1).first()
        self.assertIsNotNone(fx)

        # Create a player and performance
        p1 = Player.objects.create(name="Lautaro", role="A", team="Inter", initial_price=30, owner=fx.home)
        Formation.objects.create(participant=fx.home, module="4-3-3", starter_ids=[p1.id])
        PlayerPerformance.objects.create(giornata=g1, player=p1, vote=Decimal("7.5"), goals=2)

        # Score participant
        from auctions.services.scoring import compute_giornata
        compute_giornata(g1, mark_scored=True)
        fx.refresh_from_db()

        # Check get_fixture_details
        from auctions.services.competitions import get_fixture_details, get_competition_matchdays
        details = get_fixture_details(fx)
        self.assertEqual(details["fixture_id"], fx.id)
        self.assertEqual(details["home"]["name"], fx.home.display_name)
        self.assertGreater(details["home"]["total"], 0)

        # Check get_competition_matchdays has scorers and mod
        matchdays = get_competition_matchdays(comp, participant_id=fx.home_id)
        self.assertEqual(len(matchdays), 38)
        first_m = matchdays[0]
        self.assertTrue(first_m["has_user_match"])
        matched_fx = next(f for f in first_m["fixtures"] if f.id == fx.id)
        self.assertIsNotNone(matched_fx.home_score)

        # Test API endpoint: lineups and votes are the league's own business.
        self.assertEqual(self.client.get(f"/app/fixture/{fx.id}/detail/").status_code, 404)
        outsider = Participant.objects.create(display_name="Fuori", league=League.objects.create(name="Altra"))
        session = self.client.session
        session["participant_id"] = outsider.id
        session.save()
        self.assertEqual(self.client.get(f"/app/fixture/{fx.id}/detail/").status_code, 404)
        session["participant_id"] = fx.away_id
        session.save()
        resp = self.client.get(f"/app/fixture/{fx.id}/detail/")
        self.assertEqual(resp.status_code, 200)
        json_data = resp.json()
        self.assertTrue(json_data["success"])
        self.assertEqual(json_data["fixture"]["fixture_id"], fx.id)
        self.assertEqual(json_data["fixture"]["home"]["name"], fx.home.display_name)


class CompetitionWizardParityTests(TestCase):
    """Web console and mobile app offer the very same «Nuova Competizione» wizard."""

    def setUp(self):
        import re
        from django.contrib.auth.models import User
        self.re = re
        self.owner = User.objects.create_user("owner_cwz", password="pw")
        self.league = League.objects.create(name="Lega Coppe", owner=self.owner)
        self.season = Season.objects.create(league=self.league, name="2026/27", matchdays=38, is_current=True)
        for n in range(1, 5):
            Giornata.objects.create(season=self.season, number=n)
        self.team = Participant.objects.create(display_name="Owner FC", league=self.league, credits=500, user=self.owner)
        for i in range(3):
            Participant.objects.create(display_name=f"Squadra {i}", league=self.league, credits=500)
        self.client.force_login(self.owner)
        s = self.client.session
        s["participant_id"] = self.team.id
        s.save()

    def _wizard(self, resp):
        html = resp.content.decode()
        start = html.index('id="comp-wizard-modal"')
        wizard = html[start:html.index("</form>", start)]
        return self.re.sub(r'name="(from|csrfmiddlewaretoken)" value="[^"]*"', "", wizard)

    def test_console_and_app_offer_the_same_wizard(self):
        from django.urls import reverse
        console = self.client.get(reverse("admin_competitions") + f"?league={self.league.id}")
        app = self.client.get(reverse("app_lega") + f"?league={self.league.id}")
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        web_wz, app_wz = self._wizard(console), self._wizard(app)
        for kind, _ in Competition.Type.choices:
            self.assertIn(f"selectCompKind('{kind}'", web_wz, kind)
        for field in ("name", "description", "start_giornata", "end_giornata", "two_legged",
                      "home_id", "away_id", "win_points", "draw_points", "loss_points", "home_bonus", "notify_teams"):
            self.assertIn(f'name="{field}"', web_wz, field)
        self.assertEqual(web_wz, app_wz, "il wizard competizioni differisce tra web e app")

    def test_console_wizard_creates_in_the_league_with_its_rules(self):
        from django.urls import reverse
        resp = self.client.post(reverse("admin_competition_create"), {
            "league_id": self.league.id, "from": "console", "kind": Competition.Type.ROUND_ROBIN,
            "name": "Campionato Web", "start_giornata": 1, "win_points": 2, "home_bonus": "1.0",
        })
        self.assertEqual(resp.status_code, 302)
        comp = Competition.objects.get(name="Campionato Web")
        self.assertEqual(comp.season.league, self.league)
        self.assertEqual(comp.settings["win_points"], 2)
        self.assertEqual(comp.settings["home_bonus"], 1.0)


class CompetitionRulesAppliedTests(TestCase):
    """What the wizard lets the admin choose is what the matches are played
    with: the home bonus and the points for a win, a draw and a defeat."""

    def setUp(self):
        self.league = League.objects.create(name="Lega Regole")
        self.season = Season.objects.create(league=self.league, name="2026/27", matchdays=2)
        self.g1 = Giornata.objects.create(season=self.season, number=1)
        self.home = Participant.objects.create(display_name="Casa", league=self.league)
        self.away = Participant.objects.create(display_name="Ospite", league=self.league)
        self.comp = Competition.objects.create(
            season=self.season, name="Campionato", kind=Competition.Type.ROUND_ROBIN,
            settings={"home_bonus": 2.0, "win_points": 2, "draw_points": 1, "loss_points": 0},
        )
        self.fx = Fixture.objects.create(giornata=self.g1, competition=self.comp,
                                         home=self.home, away=self.away)

    def _play(self, home_total, away_total):
        from auctions.services.scoring import set_manual_scores

        set_manual_scores(self.g1, {self.home: (Decimal(home_total), None),
                                    self.away: (Decimal(away_total), None)})
        self.fx.refresh_from_db()

    def test_the_home_bonus_turns_a_draw_into_a_win(self):
        self._play("64", "65")             # 64 + 2 = 66: one goal; 65: none
        self.assertEqual((self.fx.home_goals, self.fx.away_goals), (1, 0))
        self.assertEqual(self.fx.home_total, Decimal("66"))
        self.assertEqual((self.fx.home_points, self.fx.away_points), (2, 0))   # win worth 2 here

    def test_without_a_bonus_the_totals_decide_alone(self):
        self.comp.settings = {}
        self.comp.save()
        self._play("64", "65")
        self.assertEqual((self.fx.home_goals, self.fx.away_goals), (0, 0))
        self.assertEqual((self.fx.home_points, self.fx.away_points), (1, 1))

    def test_typed_goals_stay_as_typed(self):
        from auctions.services.scoring import set_manual_scores

        set_manual_scores(self.g1, {self.home: (Decimal("64"), 0), self.away: (Decimal("65"), 0)})
        self.fx.refresh_from_db()
        self.assertEqual((self.fx.home_goals, self.fx.away_goals), (0, 0))

    def test_the_match_sheet_shows_the_bonus(self):
        from auctions.services.competitions import get_fixture_details

        self._play("64", "65")
        details = get_fixture_details(self.fx)
        self.assertEqual(details["home"]["total"], 66.0)
        self.assertEqual(details["home"]["goals"], 1)
        self.assertEqual(details["home"]["home_bonus"], 2.0)


class NewSeasonTests(TestCase):
    """«Nuova stagione» chiude l'anno: giornate e classifiche restano nella
    vecchia stagione (lo storico), la nuova riparte da zero con le stesse
    competizioni e le stesse regole."""

    def setUp(self):
        self.league = League.objects.create(name="Lega Dinastia")
        self.teams = [Participant.objects.create(display_name=f"T{i}", league=self.league) for i in range(4)]
        self.old = Season.objects.create(league=self.league, name="Stagione 2026/27", matchdays=6,
                                         rules={"conv_base": 60})
        for n in range(1, 7):
            Giornata.objects.create(season=self.old, number=n)
        self.comp = Competition.objects.create(season=self.old, name="Campionato", kind=Competition.Type.ROUND_ROBIN,
                                               settings={"home_bonus": 1.0, "start_giornata": 1})
        setup_round_robin_competition(self.comp)
        g1 = self.old.giornate.get(number=1)
        from auctions.services.scoring import set_manual_scores
        set_manual_scores(g1, {t: (Decimal("70"), None) for t in self.teams})
        self.old_scores = GiornataScore.objects.filter(giornata__season=self.old).count()

    def test_the_year_is_kept_and_a_new_one_starts(self):
        from auctions.services import season as season_service
        season_service.start_new_season(self.league.id, final_order=[t.id for t in self.teams])
        self.old.refresh_from_db()
        self.assertFalse(self.old.is_current)
        new = Season.objects.get(league=self.league, is_current=True)
        self.assertEqual(new.name, "Stagione 2027/28")
        self.assertEqual(new.rules, {"conv_base": 60})
        self.assertEqual(new.giornate.count(), 6)
        self.assertFalse(new.giornate.exclude(status=Giornata.Status.SCHEDULED).exists())
        clone = new.competitions.get()
        self.assertEqual((clone.name, clone.kind, clone.settings["home_bonus"]), ("Campionato", "ROUND_ROBIN", 1.0))
        self.assertGreater(clone.fixtures.count(), 0)
        self.assertFalse(clone.fixtures.filter(computed=True).exists())
        # Last year's results are still there.
        self.assertEqual(GiornataScore.objects.filter(giornata__season=self.old).count(), self.old_scores)
        self.assertTrue(self.comp.fixtures.filter(computed=True).exists())


class KnockoutAdvanceTests(TestCase):
    """Il tabellone avanza da solo: a fine turno si sorteggia il successivo,
    la finale nomina la vincitrice."""

    def setUp(self):
        self.league = League.objects.create(name="Lega Coppa")
        self.season = Season.objects.create(league=self.league, name="2026/27", matchdays=10)
        for n in range(1, 11):
            Giornata.objects.create(season=self.season, number=n)
        self.teams = [Participant.objects.create(display_name=f"T{i}", league=self.league) for i in range(4)]

    def _cup(self, **settings):
        comp = Competition.objects.create(season=self.season, name="Coppa", kind=Competition.Type.KNOCKOUT,
                                          settings={"start_giornata": 1, **settings})
        setup_knockout_competition(comp, team_ids=[t.id for t in self.teams], start_giornata=1,
                                   two_legged=bool(settings.get("two_legged")))
        return comp

    def _play(self, number, totals):
        from auctions.services.scoring import set_manual_scores
        g = self.season.giornate.get(number=number)
        set_manual_scores(g, {t: (Decimal(str(totals.get(t.id, 60))), None) for t in self.teams})

    def test_semifinals_then_final_then_winner(self):
        comp = self._cup()
        t0, t1, t2, t3 = (t.id for t in self.teams)
        self.assertEqual(sorted((f.home_id, f.away_id) for f in comp.fixtures.all()), [(t0, t3), (t1, t2)])
        self._play(1, {t0: 72, t3: 60, t1: 60, t2: 78})          # T0 and T2 go through
        final = comp.fixtures.get(giornata__number=2)
        self.assertEqual((final.stage, final.home_id, final.away_id), ("Finale", t0, t2))
        self._play(2, {t0: 60, t2: 66})
        comp.refresh_from_db()
        self.assertEqual(comp.settings["winner_id"], t2)

    def test_a_draw_goes_to_the_fantapunti_or_to_the_seed(self):
        comp = self._cup()
        t0, t1, t2, t3 = (t.id for t in self.teams)
        self._play(1, {t0: 60, t3: 64, t1: 70, t2: 68})          # 0-0 and 1-1: decided on fantapunti
        final = comp.fixtures.get(giornata__number=2)
        self.assertEqual({final.home_id, final.away_id}, {t3, t1})

    def test_the_seed_rule(self):
        comp = self._cup(knockout_tiebreak="casa")
        t0, t1, t2, t3 = (t.id for t in self.teams)
        self._play(1, {t0: 60, t3: 64, t1: 70, t2: 68})
        final = comp.fixtures.get(giornata__number=2)
        self.assertEqual({final.home_id, final.away_id}, {t0, t1})

    def test_two_legs_add_up_and_a_corrected_result_redraws(self):
        comp = self._cup(two_legged=True)
        t0, t1, t2, t3 = (t.id for t in self.teams)
        self._play(1, {t0: 66, t3: 60, t1: 60, t2: 60})          # andata: T0 1-0, T1-T2 0-0
        self.assertFalse(comp.fixtures.filter(stage__startswith="Finale").exists())
        self._play(2, {t0: 60, t3: 72, t1: 60, t2: 66})          # ritorno: T3 2-0 (agg. 1-2), T2 1-0
        finals = comp.fixtures.filter(stage__startswith="Finale").order_by("giornata__number")
        self.assertEqual([f.giornata.number for f in finals], [3, 4])
        self.assertEqual({finals[0].home_id, finals[0].away_id}, {t3, t2})
        # The admin corrects the ritorno: T3 doesn't score, T0 goes through.
        self._play(2, {t0: 60, t3: 60, t1: 60, t2: 66})
        finals = comp.fixtures.filter(stage__startswith="Finale")
        self.assertEqual({finals[0].home_id, finals[0].away_id}, {t0, t2})

    def test_groups_then_semifinals(self):
        from auctions.services.competitions import setup_groups_knockout_competition
        comp = Competition.objects.create(season=self.season, name="Champions", kind=Competition.Type.GROUPS_KNOCKOUT,
                                          settings={"start_giornata": 1, "end_giornata": 2})
        setup_groups_knockout_competition(comp, team_ids=[t.id for t in self.teams], start_giornata=1, end_giornata=2)
        t0, t1, t2, t3 = (t.id for t in self.teams)              # Girone A: T0, T1 · Girone B: T2, T3
        self._play(1, {t0: 72, t2: 72})
        self.assertFalse(comp.fixtures.exclude(stage__startswith="Girone").exists())
        self._play(2, {t0: 72, t2: 72})
        semis = comp.fixtures.exclude(stage__startswith="Girone")
        self.assertEqual(sorted((f.home_id, f.away_id) for f in semis), [(t0, t3), (t2, t1)])
        self.assertTrue(all(f.giornata.number == 3 and f.stage == "Semifinale" for f in semis))


class HistoryPageTests(TestCase):
    """Storico: albo d'oro per stagione e classifica di sempre, uguale in
    console e app (_season_history.html)."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.owner = User.objects.create_user("presidente_storico", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lega Storica", owner=self.owner)
        self.teams = [Participant.objects.create(display_name=f"Club {i}", league=self.league) for i in range(4)]
        self.old = Season.objects.create(league=self.league, name="Stagione 2025/26", matchdays=3, is_current=False)
        for n in range(1, 4):
            Giornata.objects.create(season=self.old, number=n)
        comp = Competition.objects.create(season=self.old, name="Campionato", kind=Competition.Type.ROUND_ROBIN)
        setup_round_robin_competition(comp)
        from auctions.services.scoring import set_manual_scores
        for n in range(1, 4):
            set_manual_scores(self.old.giornate.get(number=n),
                              {t: (Decimal("80") if t == self.teams[2] else Decimal("60"), None) for t in self.teams})
        Season.objects.create(league=self.league, name="Stagione 2026/27", is_current=True)

    def _section(self, html):
        import re
        return re.sub(r"\s+", " ", html.split("<!-- season-history:start -->", 1)[1]
                      .split("<!-- season-history:end -->", 1)[0])

    def test_console_and_app_show_the_same_history(self):
        from django.urls import reverse
        self.client.force_login(self.owner)
        console = self.client.get(reverse("admin_storico"), {"league": self.league.id})
        self.assertContains(console, "Stagione 2025/26")
        self.assertContains(console, "Club 2")
        session = self.client.session
        session["participant_id"] = self.teams[0].id
        session.save()
        app = self.client.get(reverse("app_storico"))
        self.assertEqual(app.status_code, 200)
        self.assertEqual(self._section(console.content.decode()), self._section(app.content.decode()))

    def test_the_champion_gets_a_title(self):
        from auctions.services.history import league_history
        history = league_history(self.league)
        top = history["alltime"][0]
        self.assertEqual((top["team"], top["titles"]), (self.teams[2], 1))
        self.assertEqual(history["seasons"][0]["season"].name, "Stagione 2026/27")     # current first
