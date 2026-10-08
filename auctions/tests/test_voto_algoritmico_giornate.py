"""Voto algoritmico nelle giornate: fonte del voto per lega, sync live,
consolidamento, ricalcolo senza API, form «Voto base», calibrazione e
«Perché questo voto»."""
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse

from .. import scoring as scoring_engine
from ..models import (AlgoSettingsVersion, Competition, Fixture, Footballer, Formation, Giornata, League,
                      Participant, Player, PlayerPerformance, Season)
from ..services import voto_algo
from ..services.scoring import compute_giornata, recompute_season
from ..services.voti_live import LiveSyncManager
from ..views.admin_voti import RULE_GROUPS

# Una partita vinta 2-0 dall'Inter: Lautaro segna, Sommer non subisce.
ROWS = [
    {"api_id": 501, "name": "L. Martínez", "team": "Inter", "role": "A", "vote": "7.3",
     "goals": 1, "assists": 0, "own_goals": 0, "pen_scored": 0, "pen_missed": 0, "pen_saved": 0,
     "goals_conceded": 0, "yellow": False, "red": False,
     "minutes": 90, "team_goals_for": 2, "team_goals_against": 0},
    {"api_id": 502, "name": "Y. Sommer", "team": "Inter", "role": "G", "vote": "6.1",
     "goals": 0, "assists": 0, "own_goals": 0, "pen_scored": 0, "pen_missed": 0, "pen_saved": 0,
     "goals_conceded": 0, "yellow": False, "red": False,
     "minutes": 90, "team_goals_for": 2, "team_goals_against": 0},
]


def _no_http(*args, **kwargs):
    raise AssertionError("chiamata HTTP non prevista")


class LeagueMixin:
    def make_league(self, name, source=None, algo=None):
        owner = User.objects.create_user(f"own_{name}", password="pw")
        league = League.objects.create(name=name, owner=owner)
        rules = {}
        if source:
            rules["vote_source"] = source
        if algo:
            rules["algo"] = algo
        season = Season.objects.create(league=league, name="2026/27", is_current=True, rules=rules)
        giornata = Giornata.objects.create(season=season, number=5)
        lautaro_f, _ = Footballer.objects.get_or_create(api_id=501, defaults={"name": "Lautaro Martínez"})
        sommer_f, _ = Footballer.objects.get_or_create(api_id=502, defaults={"name": "Yann Sommer"})
        team = Participant.objects.create(display_name=f"{name} FC", league=league, user=owner)
        # Una lega nuova riceve già i calciatori dell'anagrafica comune: si riusano.
        lautaro = self._player(league, lautaro_f, team, name="Lautaro", role="A")
        sommer = self._player(league, sommer_f, team, name="Sommer", role="P")
        Formation.objects.create(participant=team, module="4-3-3", starter_ids=[sommer.id, lautaro.id])
        return {"owner": owner, "league": league, "season": season, "giornata": giornata,
                "team": team, "lautaro": lautaro, "sommer": sommer}

    def _player(self, league, footballer, team, **fields):
        p = Player.objects.filter(league=league, footballer=footballer).first()
        p = p or Player(league=league, footballer=footballer, initial_price=1)
        for k, v in {**fields, "team": "Inter", "owner": team}.items():
            setattr(p, k, v)
        p.save()
        return p

    def sync(self, *leagues, rows=ROWS):
        mgr = LiveSyncManager.get_instance()
        mgr.provider = "apifootball"
        with mock.patch("auctions.providers.apifootball.matchday_live_rows", return_value=rows) as api:
            res = mgr.sync_now(giornata_num=5, is_provisional=True, leagues=[x["league"] for x in leagues])
        self.assertEqual(res["status"], "SUCCESS")
        return api

    def rules_form(self, league, **extra):
        """Il form delle regole con i valori classici, più ``extra``."""
        form = {"league_id": league.id}
        for _title, items in RULE_GROUPS:
            for key, _label, _hint, switchable, optional in items:
                value = scoring_engine.DEFAULTS.get(key)
                form[f"rule_{key}"] = "" if value is None else str(value)
                if switchable:
                    form[f"on_{key}"] = "1"
        form.update(extra)
        return form


class VoteSourceHelpersTests(LeagueMixin, TestCase):
    def test_default_and_levels(self):
        x = self.make_league("Default")
        self.assertEqual(voto_algo.vote_source_for(x["season"]), "rating")
        x["season"].rules = {"vote_source": "boh"}
        self.assertEqual(voto_algo.vote_source_for(x["season"]), "rating")
        # piattaforma sopra i default, la lega sopra la piattaforma
        AlgoSettingsVersion.objects.create(rules={"win": 1.0, "loss": -1.0})
        x["season"].rules = {"vote_source": "algoritmico", "algo": {"loss": -0.5}}
        rules = voto_algo.algo_rules_for(x["season"])
        self.assertEqual((rules["win"], rules["loss"]), (1.0, -0.5))


class SyncPerLeagueTests(LeagueMixin, TestCase):
    def test_same_api_call_different_votes(self):
        rating = self.make_league("Rating")
        algo = self.make_league("Algo", source="algoritmico")
        api = self.sync(rating, algo)
        self.assertEqual(api.call_count, 1)

        r_lau = PlayerPerformance.objects.get(player=rating["lautaro"])
        a_lau = PlayerPerformance.objects.get(player=algo["lautaro"])
        self.assertEqual(r_lau.vote, Decimal("7.5"))            # 7,3 arrotondato
        self.assertEqual(a_lau.vote, Decimal("7.0"))            # 6 + 0,375 + 0,5 = 6,875
        self.assertEqual((r_lau.live_source, a_lau.live_source), ("apifootball", "algoritmico"))
        self.assertIsNone(r_lau.vote_detail)
        self.assertEqual(a_lau.vote_detail["source"], "algoritmico")
        self.assertEqual(a_lau.vote_detail["raw"], 6.875)
        self.assertEqual(a_lau.vote_detail["input"]["minutes"], 90)
        self.assertEqual(a_lau.vote_detail["input"]["team_goals_for"], 2)
        self.assertEqual(a_lau.vote_detail["breakdown"]["eventi"], 0.5)
        # il ruolo è quello della lega (P), non quello dell'API (G)
        a_som = PlayerPerformance.objects.get(player=algo["sommer"])
        self.assertEqual(a_som.vote_detail["breakdown"]["reparto"], 0.5)
        self.assertEqual(a_som.vote, Decimal("7.0"))
        self.assertEqual(PlayerPerformance.objects.get(player=rating["sommer"]).vote, Decimal("6.0"))

    def test_simulation_feeds_the_algorithm(self):
        algo = self.make_league("Sim", source="algoritmico_provvisorio")
        mgr = LiveSyncManager.get_instance()
        mgr.provider = "simulation"
        with mock.patch("random.random", return_value=0.5):
            res = mgr.sync_now(giornata_num=5, is_provisional=True, leagues=[algo["league"]])
        self.assertEqual(res["status"], "SUCCESS")
        perf = PlayerPerformance.objects.get(player=algo["lautaro"])
        self.assertEqual(perf.live_source, "algoritmico")
        self.assertGreater(perf.vote_detail["input"]["minutes"], 0)
        self.assertIn("team_goals_against", perf.vote_detail["input"])
        self.assertIsNotNone(perf.vote)


class ConsolidateTests(LeagueMixin, TestCase):
    def _upload(self, x, content, **extra):
        self.client.force_login(x["owner"])
        f = SimpleUploadedFile("voti.csv", content.encode(), content_type="text/csv")
        return self.client.post(reverse("admin_voti_import"), {
            "league_id": x["league"].id, "giornata_number": 5, "voti_file": f, **extra})

    def test_algoritmico_closes_without_file(self):
        algo = self.make_league("Algo", source="algoritmico")
        self.sync(algo)
        LiveSyncManager.get_instance().consolidate_official(5, leagues=[algo["league"]])
        algo["giornata"].refresh_from_db()
        self.assertEqual(algo["giornata"].status, Giornata.Status.SCORED)
        perf = PlayerPerformance.objects.get(player=algo["lautaro"])
        self.assertFalse(perf.is_live)
        self.assertEqual(perf.live_source, "algoritmico")
        self.assertEqual(perf.vote, Decimal("7.0"))
        self.assertIsNotNone(perf.vote_detail)

    def test_provvisorio_replaced_by_the_file(self):
        prov = self.make_league("Prov", source="algoritmico_provvisorio")
        self.sync(prov)
        self.assertEqual(PlayerPerformance.objects.get(player=prov["lautaro"]).vote, Decimal("7.0"))
        self._upload(prov, "Nome,Squadra,Ruolo,Voto\nLautaro,Inter,A,5\nSommer,Inter,P,6\n")
        perf = PlayerPerformance.objects.get(player=prov["lautaro"])
        self.assertEqual(perf.vote, Decimal("5"))
        self.assertIsNone(perf.vote_detail)
        self.assertEqual(perf.live_source, "official_upload")
        self.assertFalse(perf.is_live)

    def test_algoritmico_file_only_when_asked(self):
        algo = self.make_league("Algo", source="algoritmico")
        self.sync(algo)
        csv = "Nome,Squadra,Ruolo,Voto\nLautaro,Inter,A,5\n"
        self._upload(algo, csv)
        self.assertEqual(PlayerPerformance.objects.get(player=algo["lautaro"]).vote, Decimal("7.0"))
        self._upload(algo, csv, replace_algo="1")
        self.assertEqual(PlayerPerformance.objects.get(player=algo["lautaro"]).vote, Decimal("5"))


class RecomputeTests(LeagueMixin, TestCase):
    def test_new_parameters_regenerate_votes_without_http(self):
        algo = self.make_league("Algo", source="algoritmico")
        self.sync(algo)
        LiveSyncManager.get_instance().consolidate_official(5, leagues=[algo["league"]])
        # Un voto venuto da un'altra fonte non ha la riga della partita.
        extra = Player.objects.create(league=algo["league"], name="Barella", role="C", team="Inter", initial_price=1)
        PlayerPerformance.objects.create(giornata=algo["giornata"], player=extra, vote=Decimal("6.5"))

        self.client.force_login(algo["owner"])
        with mock.patch("requests.get", side_effect=_no_http), \
                mock.patch("requests.Session.request", side_effect=_no_http), \
                mock.patch("auctions.providers.apifootball._get", side_effect=_no_http), \
                mock.patch("auctions.providers.apifootball.matchday_live_rows", side_effect=_no_http):
            resp = self.client.post(reverse("admin_scoring_rules"), self.rules_form(
                algo["league"], vote_base_form="1", vote_source="algoritmico", algo_goal="1,5"), follow=True)
        self.assertEqual(resp.status_code, 200)
        algo["season"].refresh_from_db()
        self.assertEqual(algo["season"].rules["algo"], {"goal": 1.5})
        perf = PlayerPerformance.objects.get(player=algo["lautaro"])
        self.assertEqual(perf.vote, Decimal("8.0"))             # 6 + 0,375 + 1,5 = 7,875
        self.assertEqual(perf.vote_detail["breakdown"]["eventi"], 1.5)
        self.assertEqual(PlayerPerformance.objects.get(player=extra).vote, Decimal("6.5"))
        self.assertContains(resp, "1 voti senza i dati della partita")

    def test_summary_counts_missing_rows(self):
        algo = self.make_league("Algo2", source="algoritmico_provvisorio")
        PlayerPerformance.objects.create(giornata=algo["giornata"], player=algo["lautaro"], vote=Decimal("6"))
        compute_giornata(algo["giornata"])
        summary = {}
        self.assertEqual(recompute_season(algo["season"], summary=summary), 1)
        self.assertEqual((summary["algo_regenerated"], summary["algo_missing"],
                          summary["algo_missing_giornate"]), (0, 1, [5]))
        self.assertEqual(PlayerPerformance.objects.get(player=algo["lautaro"]).vote, Decimal("6"))


class VoteBaseFormTests(LeagueMixin, TestCase):
    def _part(self, resp):
        html = resp.content.decode()
        part = html[html.index('<div class="sr-group">Voto base</div>'):html.index('<div class="sr-group">Modificatore difesa</div>')]
        return part

    def test_same_section_in_console_and_app(self):
        x = self.make_league("Parita", source="algoritmico", algo={"win": 0.5})
        self.client.force_login(x["owner"])
        q = f"?league={x['league'].id}"
        console = self.client.get(reverse("admin_giornate") + q)
        app = self.client.get(reverse("app_giornate") + q)
        self.assertEqual((console.status_code, app.status_code), (200, 200))
        self.assertEqual(self._part(console), self._part(app))
        for value, label, help_text in voto_algo.VOTE_SOURCES:
            self.assertContains(console, f'value="{value}"')
            self.assertContains(console, label)
        self.assertContains(console, 'name="algo_min_minutes"')
        self.assertContains(console, "usa quello della piattaforma")
        self.assertContains(console, 'name="algo_win" value="0,5"')
        self.assertNotContains(console, 'name="algo_perf_cap"')    # solo di piattaforma

    def test_out_of_range_refused(self):
        x = self.make_league("Valida", source="algoritmico")
        self.client.force_login(x["owner"])
        for bad in ({"algo_win": "5"}, {"algo_min_minutes": "abc"}, {"algo_goal": "-9"},
                    {"vote_source": "sofascore"}):
            form = self.rules_form(x["league"], vote_base_form="1", vote_source="algoritmico")
            form.update(bad)
            resp = self.client.post(reverse("admin_scoring_rules"), form, follow=True)
            self.assertContains(resp, "Valori non validi")
            x["season"].refresh_from_db()
            self.assertEqual(x["season"].rules, {"vote_source": "algoritmico"}, bad)

    def test_save_and_back_to_platform(self):
        x = self.make_league("Salva", algo={"calib_scale": 1.2})
        self.client.force_login(x["owner"])
        self.client.post(reverse("admin_scoring_rules"), self.rules_form(
            x["league"], vote_base_form="1", vote_source="algoritmico_provvisorio",
            algo_win="0,4", algo_min_minutes="30"))
        x["season"].refresh_from_db()
        self.assertEqual(x["season"].rules["vote_source"], "algoritmico_provvisorio")
        self.assertEqual(x["season"].rules["algo"], {"calib_scale": 1.2, "win": 0.4, "min_minutes": 30})
        self.client.post(reverse("admin_scoring_rules"), self.rules_form(
            x["league"], vote_base_form="1", vote_source="algoritmico_provvisorio",
            algo_win="0,4", algo_platform_win="1", algo_min_minutes="30"))
        x["season"].refresh_from_db()
        self.assertEqual(x["season"].rules["algo"], {"calib_scale": 1.2, "min_minutes": 30})
        # «Valori classici» tocca bonus e malus, non il voto base
        self.client.post(reverse("admin_scoring_rules"), {"league_id": x["league"].id, "reset": "1"})
        x["season"].refresh_from_db()
        self.assertEqual(x["season"].rules["vote_source"], "algoritmico_provvisorio")

    def test_other_league_refused(self):
        x = self.make_league("Mia")
        self.client.force_login(User.objects.create_user("intruso", password="pw"))
        resp = self.client.post(reverse("admin_scoring_rules"), self.rules_form(
            x["league"], vote_base_form="1", vote_source="algoritmico"))
        self.assertEqual(resp.status_code, 403)


class CalibrationCommandTests(LeagueMixin, TestCase):
    def _votes(self, x, n):
        rules = voto_algo.algo_rules_for(x["season"])
        players = [Player.objects.create(league=x["league"], name=f"G{i}", role="C", team="Inter", initial_price=1)
                   for i in range(10)]
        made = 0
        for g in range(1, 6):
            giornata, _ = Giornata.objects.get_or_create(season=x["season"], number=g)
            for i, p in enumerate(players):
                if made >= n:
                    return
                row = {"minutes": 90, "team_goals_for": (i + g) % 3, "team_goals_against": i % 2,
                       "goals": int(i == g), "yellow": i % 4 == 0}
                vote, detail = voto_algo.algo_vote(row, "C", rules)
                PlayerPerformance.objects.create(giornata=giornata, player=p, vote=vote, vote_detail=detail)
                made += 1

    def test_dry_run_and_apply(self):
        x = self.make_league("Cal", source="algoritmico")
        self._votes(x, 40)
        out = StringIO()
        call_command("calibra_voto_algoritmico", "--league", str(x["league"].id), stdout=out)
        self.assertIn("calib_scale", out.getvalue())
        self.assertIn("nulla è stato scritto", out.getvalue())
        x["season"].refresh_from_db()
        self.assertNotIn("algo", x["season"].rules)
        call_command("calibra_voto_algoritmico", "--league", str(x["league"].id),
                     "--mean", "6.0", "--sd", "0.5", "--apply", stdout=StringIO())
        x["season"].refresh_from_db()
        self.assertIn("calib_scale", x["season"].rules["algo"])
        self.assertIn("calib_shift", x["season"].rules["algo"])

    def test_too_few_votes(self):
        x = self.make_league("Pochi", source="algoritmico")
        self._votes(x, 12)
        with self.assertRaisesMessage(CommandError, "almeno 30"):
            call_command("calibra_voto_algoritmico", "--league", str(x["league"].id), stdout=StringIO())


class VoteWhyAndFixtureAccessTests(LeagueMixin, TestCase):
    def setUp(self):
        self.x = self.make_league("Why", source="algoritmico")
        rival = Participant.objects.create(display_name="Rivali", league=self.x["league"])
        comp = Competition.objects.create(season=self.x["season"], name="Campionato")
        self.fx = Fixture.objects.create(competition=comp, giornata=self.x["giornata"],
                                         home=self.x["team"], away=rival)
        self.sync(self.x)

    def _as_team(self):
        session = self.client.session
        session["participant_id"] = self.x["team"].id
        session.save()

    def test_detail_refused_to_anonymous_and_other_league(self):
        url = reverse("app_fixture_detail", args=[self.fx.id])
        self.assertEqual(self.client.get(url).status_code, 403)
        other = self.make_league("Altra")
        self.client.force_login(other["owner"])        # admin e squadra di un'altra lega
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin_fixture_detail", args=[self.fx.id])).status_code, 403)

    def test_detail_for_the_league_with_why(self):
        self.client.force_login(self.x["owner"])
        data = self.client.get(reverse("admin_fixture_detail", args=[self.fx.id])).json()
        lautaro = next(p for p in data["fixture"]["home"]["starters"] if p["name"] == "Lautaro")
        self.assertEqual(lautaro["vote"], 7.0)
        labels = [p["label"] for p in lautaro["why"]["parts"]]
        self.assertEqual(labels[:5], ["Base", "Risultato", "Reparto", "Eventi", "Rendimento"])
        self.assertEqual(round(sum(p["value"] for p in lautaro["why"]["parts"]), 2), 7.0)
        self.client.logout()
        self._as_team()
        self.assertEqual(self.client.get(reverse("app_fixture_detail", args=[self.fx.id])).status_code, 200)

    def test_live_page_and_detail_modals_share_the_panel(self):
        self._as_team()
        resp = self.client.get(reverse("app_live") + "?giornata=5")
        self.assertContains(resp, f'data-why-pid="{self.x["lautaro"].id}"')
        self.assertContains(resp, "Voto calcolato da FantaManager")
        self.assertContains(resp, 'id="fm-why-data"')
        self.client.force_login(self.x["owner"])
        for name in ("admin_competitions", "app_lega"):
            page = self.client.get(reverse(name) + f"?league={self.x['league'].id}")
            self.assertEqual(page.status_code, 200, name)
            self.assertContains(page, "Voto calcolato da FantaManager")
            self.assertContains(page, "fmVoteWhy.register")

    def test_rating_votes_have_no_why(self):
        rating = self.make_league("NoWhy")
        self.sync(rating)
        self.assertEqual(voto_algo.vote_why(None, Decimal("7")), None)
        perf = PlayerPerformance.objects.get(player=rating["lautaro"])
        self.assertIsNone(voto_algo.vote_why(perf.vote_detail, perf.vote))
