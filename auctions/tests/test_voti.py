"""Unit tests for the Voti import engine, scoring and Coppa Italia Battle Royale."""
import io
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User

from ..models import (
    League,
    Participant,
    Player,
    Season,
    Giornata,
    GiornataScore,
    PlayerPerformance,
)
from ..services.voti import (
    parse_voti_file,
    import_voti_giornata,
    compute_coppa_italia_battle_royale,
)
from ..scoring import player_fantavoto, DEFAULTS


class VotiServicesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="password")
        self.league = League.objects.create(name="Fantalugnano", budget=Decimal("600"), game_mode="mantra")
        self.season = Season.objects.create(name="2026/2027", league=self.league, is_current=True)
        self.team_a = Participant.objects.create(display_name="Squadra Alfa", league=self.league, credits=Decimal("600"))
        self.team_b = Participant.objects.create(display_name="Squadra Beta", league=self.league, credits=Decimal("600"))
        self.team_c = Participant.objects.create(display_name="Squadra Gamma", league=self.league, credits=Decimal("600"))

        # Create players
        self.p1 = Player.objects.create(name="Lautaro Martinez", role="A", mantra_roles="Pc", team="Inter", league=self.league, owner=self.team_a)
        self.p2 = Player.objects.create(name="Barella", role="C", mantra_roles="C,T", team="Inter", league=self.league, owner=self.team_b)
        self.p3 = Player.objects.create(name="Sommer", role="P", mantra_roles="Por", team="Inter", league=self.league, owner=self.team_c)

    def test_parse_voti_csv_tolerant(self):
        csv_content = (
            "Nome,Ruolo,Squadra,Voto,Gol,Assist,Ammonizione,Espulsione,Rigore Parato,Rigore Sbagliato,Gol Subiti\n"
            "Lautaro Martinez,A,Inter,7.5,2,0,0,0,0,0,0\n"
            "Barella,C,Inter,6.5,0,1,1,0,0,0,0\n"
            "Sommer,P,Inter,6.0,0,0,0,0,1,0,1\n"
        )
        bio = io.BytesIO(csv_content.encode("utf-8"))
        rows = parse_voti_file(bio, "voti_giornata_1.csv")

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["name"], "Lautaro Martinez")
        self.assertEqual(rows[0]["vote"], Decimal("7.5"))
        self.assertEqual(rows[0]["goals"], 2)
        self.assertTrue(rows[1]["yellow"])
        self.assertEqual(rows[2]["pen_saved"], 1)

    def test_import_voti_giornata_and_scoring(self):
        csv_content = (
            "Nome,Squadra,Voto,Gol,Assist\n"
            "Lautaro Martinez,Inter,7.5,1,1\n"
            "Barella,Inter,6.5,0,0\n"
        )
        bio = io.BytesIO(csv_content.encode("utf-8"))
        rows = parse_voti_file(bio, "voti.csv")

        giornata = Giornata.objects.create(season=self.season, number=1)
        res = import_voti_giornata(rows, giornata, league=self.league, recompute=False)

        self.assertEqual(res["total_imported"], 2)

        perf1 = PlayerPerformance.objects.filter(player=self.p1, giornata=giornata).first()
        self.assertIsNotNone(perf1)
        self.assertEqual(perf1.vote, Decimal("7.5"))
        self.assertEqual(perf1.goals, 1)
        self.assertEqual(perf1.assists, 1)

        # Fantavoto: 7.5 + 3 (gol) + 1 (assist) = 11.5
        fv, has_vote = player_fantavoto(perf1.as_perf(), self.p1.role, DEFAULTS)
        self.assertTrue(has_vote)
        self.assertEqual(fv, Decimal("11.5"))

    def test_capitano_bonus_malus(self):
        # 7.0 >= 6.5 -> +0.5 bonus = 7.5
        perf_high = {"vote": Decimal("7.0"), "is_captain": True}
        fv_high, _ = player_fantavoto(perf_high, "A", DEFAULTS)
        self.assertEqual(fv_high, Decimal("7.5"))

        # 5.0 <= 5.5 -> -0.5 malus = 4.5
        perf_low = {"vote": Decimal("5.0"), "is_captain": True}
        fv_low, _ = player_fantavoto(perf_low, "A", DEFAULTS)
        self.assertEqual(fv_low, Decimal("4.5"))

        # 6.0 in between -> no bonus/malus = 6.0
        perf_mid = {"vote": Decimal("6.0"), "is_captain": True}
        fv_mid, _ = player_fantavoto(perf_mid, "A", DEFAULTS)
        self.assertEqual(fv_mid, Decimal("6.0"))

    def test_coppa_italia_battle_royale_matrix(self):
        giornata = Giornata.objects.create(season=self.season, number=1)

        # Squadra Alfa: 78.0 pt -> 3 gol
        # Squadra Beta: 72.0 pt -> 2 gol
        # Squadra Gamma: 72.0 pt -> 2 gol
        GiornataScore.objects.create(giornata=giornata, participant=self.team_a, total=Decimal("78.0"), goals=3)
        GiornataScore.objects.create(giornata=giornata, participant=self.team_b, total=Decimal("72.0"), goals=2)
        GiornataScore.objects.create(giornata=giornata, participant=self.team_c, total=Decimal("72.0"), goals=2)

        standings = compute_coppa_italia_battle_royale(giornata)
        # Alfa batte Beta (3pt) e Gamma (3pt) -> 6 pt
        # Beta perde con Alfa (0pt), pareggia con Gamma (1pt) -> 1 pt
        # Gamma perde con Alfa (0pt), pareggia con Beta (1pt) -> 1 pt

        self.assertEqual(len(standings), 3)
        self.assertEqual(standings[0]["participant"].id, self.team_a.id)
        self.assertEqual(standings[0]["battle_points"], 6)
        self.assertEqual(standings[0]["record"], "2V-0P-0S")

        self.assertEqual(standings[1]["battle_points"], 1)
        self.assertEqual(standings[1]["record"], "0V-1P-1S")
        self.assertEqual(standings[2]["battle_points"], 1)
        self.assertEqual(standings[2]["record"], "0V-1P-1S")

    def test_round_live_vote(self):
        from ..services.voti_live import round_live_vote
        # User rule verification: 7.2 -> 7.0, 6.8 -> 7.0
        self.assertEqual(round_live_vote(Decimal("7.2")), Decimal("7.0"))
        self.assertEqual(round_live_vote(Decimal("6.8")), Decimal("7.0"))
        self.assertEqual(round_live_vote("7.2"), Decimal("7.0"))
        self.assertEqual(round_live_vote("6.8"), Decimal("7.0"))
        self.assertEqual(round_live_vote(6.8), Decimal("7.0"))
        self.assertEqual(round_live_vote(7.2), Decimal("7.0"))
        # 0.5 fantacalcio step rounding
        self.assertEqual(round_live_vote("6.4"), Decimal("6.5"))
        self.assertEqual(round_live_vote("6.6"), Decimal("6.5"))
        self.assertEqual(round_live_vote("6.1"), Decimal("6.0"))
        self.assertEqual(round_live_vote("7.3"), Decimal("7.5"))
        self.assertIsNone(round_live_vote(None))

    def test_live_sync_manager_simulation_and_consolidation(self):
        from ..services.voti_live import LiveSyncManager
        mgr = LiveSyncManager.get_instance()
        mgr.provider = "simulation"

        giornata = Giornata.objects.create(season=self.season, number=2)

        # 1. Sync live ratings (simulation mode)
        res = mgr.sync_now(giornata_num=2, is_provisional=True)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["giornata"], 2)

        giornata.refresh_from_db()
        self.assertEqual(giornata.status, Giornata.Status.LIVE)

        # Check performances have is_live=True
        perfs = PlayerPerformance.objects.filter(giornata=giornata)
        self.assertTrue(perfs.exists())
        self.assertTrue(all(p.is_live for p in perfs))

        # Check status reporting
        status = mgr.get_status()
        self.assertEqual(status["last_status"], "SUCCESS")
        self.assertEqual(status["active_giornata_num"], 2)

        # 2. Consolidate into official ratings
        res_cons = mgr.consolidate_official(giornata_num=2)
        self.assertEqual(res_cons["status"], "CONSOLIDATED")

        giornata.refresh_from_db()
        self.assertEqual(giornata.status, Giornata.Status.SCORED)
        perfs = PlayerPerformance.objects.filter(giornata=giornata)
        self.assertTrue(all(not p.is_live for p in perfs))

    def test_live_sync_background_toggle(self):
        from ..services.voti_live import LiveSyncManager
        mgr = LiveSyncManager.get_instance()
        mgr.start_background(interval=30, provider="simulation")
        self.assertTrue(mgr.is_enabled)
        self.assertEqual(mgr.interval_seconds, 30)

        mgr.stop_background()
        self.assertFalse(mgr.is_enabled)

    def test_admin_live_voti_endpoints(self):
        self.client.force_login(self.user)
        # Add permission
        self.user.is_staff = True
        self.user.save()
        self.league.owner = self.user
        self.league.save()

        # Trigger sync view
        resp = self.client.post("/dashboard/giornate/live-sync/", {
            "giornata_number": "1",
            "provider": "simulation",
        })
        self.assertEqual(resp.status_code, 302)

        # Trigger consolidate view
        resp_cons = self.client.post("/dashboard/giornate/live-consolidate/", {
            "giornata_number": "1",
        })
        self.assertEqual(resp_cons.status_code, 302)

    def test_live_buttons_act_on_the_admins_league_only(self):
        """Giornata 1 of another league is not this league admin's to close."""
        self.league.owner = self.user
        self.league.save()
        other = League.objects.create(name="Altra", budget=Decimal("600"))
        other_season = Season.objects.create(name="2026/2027", league=other, is_current=True)
        mine = Giornata.objects.create(season=self.season, number=1, status=Giornata.Status.LIVE)
        theirs = Giornata.objects.create(season=other_season, number=1, status=Giornata.Status.LIVE)
        self.client.force_login(self.user)
        self.client.post("/dashboard/giornate/live-consolidate/", {
            "giornata_number": "1", "league_id": str(self.league.id)})
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, Giornata.Status.SCORED)
        self.assertEqual(theirs.status, Giornata.Status.LIVE)

    def test_live_buttons_refused_to_a_user_managing_no_league(self):
        outsider = User.objects.create_user("outsider", password="pw")
        g = Giornata.objects.create(season=self.season, number=1, status=Giornata.Status.LIVE)
        self.client.force_login(outsider)
        for url in ("/dashboard/giornate/live-sync/", "/dashboard/giornate/live-consolidate/"):
            with self.subTest(url=url):
                r = self.client.post(url, {"giornata_number": "1", "provider": "simulation"})
                self.assertEqual(r.status_code, 403)
        g.refresh_from_db()
        self.assertEqual(g.status, Giornata.Status.LIVE)

    def test_junk_giornata_number_is_not_a_500(self):
        self.league.owner = self.user
        self.league.save()
        self.client.force_login(self.user)
        r = self.client.get("/dashboard/giornate/?giornata=boh")
        self.assertNotEqual(r.status_code, 500)
        r = self.client.post("/dashboard/giornate/live-consolidate/", {"giornata_number": "x"})
        self.assertEqual(r.status_code, 302)

    def test_supervisor_live_sync_actions(self):
        superadmin = User.objects.create_superuser("super_live", password="pw")
        self.client.force_login(superadmin)

        # Start
        resp = self.client.post("/supervisor/", {
            "action": "start_live_sync",
            "interval_seconds": "60",
            "provider": "simulation",
            "target_giornata": "1",
        })
        self.assertEqual(resp.status_code, 302)

        # Trigger now
        resp_now = self.client.post("/supervisor/", {
            "action": "trigger_live_sync",
            "target_giornata": "1",
            "provider": "simulation",
        })
        self.assertEqual(resp_now.status_code, 302)

        # Stop
        resp_stop = self.client.post("/supervisor/", {
            "action": "stop_live_sync",
        })
        self.assertEqual(resp_stop.status_code, 302)


class ApiFootballLiveTests(TestCase):
    """Voti live da API-Football: nessuna pagina di siti di fantacalcio."""

    FIXTURES = [
        {"fixture": {"id": 11, "status": {"short": "2H"}}},
        {"fixture": {"id": 12, "status": {"short": "NS"}}},
    ]
    PLAYERS = [
        {"team": {"name": "Inter"}, "players": [
            {"player": {"id": 501, "name": "L. Martínez"},
             "statistics": [{"games": {"minutes": 70, "rating": "7.3", "position": "F"},
                             "goals": {"total": 1, "conceded": 0, "assists": 1},
                             "cards": {"yellow": 1, "red": 0},
                             "penalty": {"scored": 1, "missed": 0, "saved": None}}]},
            {"player": {"id": 502, "name": "Y. Sommer"},
             "statistics": [{"games": {"minutes": 90, "rating": "6.1", "position": "G"},
                             "goals": {"total": None, "conceded": 2, "assists": None},
                             "cards": {"yellow": 0, "red": 0},
                             "penalty": {"scored": 0, "missed": 0, "saved": 1}}]},
            {"player": {"id": 503, "name": "Panchinaro"},
             "statistics": [{"games": {"minutes": None, "rating": None, "position": "D"},
                             "goals": {}, "cards": {}, "penalty": {}}]},
        ]},
    ]
    EVENTS = [
        {"type": "Goal", "detail": "Own Goal", "player": {"id": 502}},
        {"type": "Goal", "detail": "Normal Goal", "player": {"id": 501}},
    ]

    def _fake_get(self):
        calls = []
        payloads = {"/fixtures": self.FIXTURES, "/fixtures/players": self.PLAYERS,
                    "/fixtures/events": self.EVENTS}

        class Resp:
            headers = {}

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return {"response": self._data, "errors": []}

        def get(url, params=None, headers=None, timeout=None):
            path = url.split("v3.football.api-sports.io", 1)[1]
            calls.append((path, params))
            return Resp(payloads[path])
        return get, calls

    def test_rows_from_started_fixtures_only(self):
        from unittest import mock
        import os
        from ..providers import apifootball
        get, calls = self._fake_get()
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "k"}):
            rows = apifootball.matchday_live_rows(5, season=2026, get=get)
        self.assertEqual(calls[0], ("/fixtures", {"league": 135, "season": 2026,
                                                  "round": "Regular Season - 5"}))
        # Partita non iniziata (12): nessuna richiesta di statistiche.
        self.assertFalse(any(p and p.get("fixture") == 12 for _, p in calls[1:]))
        by_id = {r["api_id"]: r for r in rows}
        lautaro = by_id[501]
        self.assertEqual((lautaro["vote"], lautaro["role"], lautaro["goals"], lautaro["assists"],
                          lautaro["pen_scored"], lautaro["yellow"], lautaro["own_goals"]),
                         ("7.3", "A", 1, 1, 1, True, 0))
        sommer = by_id[502]
        self.assertEqual((sommer["role"], sommer["goals_conceded"], sommer["pen_saved"],
                          sommer["own_goals"]), ("P", 2, 1, 1))
        self.assertIsNone(by_id[503]["vote"])

    def test_sync_matches_by_registry_id_then_by_name(self):
        from unittest import mock
        from ..models import Footballer
        from ..services.voti_live import LiveSyncManager
        league = League.objects.create(name="Lega Live", budget=Decimal("500"))
        season = Season.objects.create(name="2026/2027", league=league, is_current=True)
        Giornata.objects.create(season=season, number=5)
        f = Footballer.objects.create(api_id=501, name="Lautaro Martínez")
        lautaro = Player.objects.create(league=league, name="Lautaro", role="A", team="Inter",
                                        initial_price=1, footballer=f)
        sommer = Player.objects.create(league=league, name="Sommer", role="P", team="Inter",
                                       initial_price=1)
        rows = [
            {"api_id": 501, "name": "L. Martínez", "team": "Inter", "role": "A", "vote": "7.3",
             "goals": 1, "assists": 0, "own_goals": 0, "pen_scored": 0, "pen_missed": 0,
             "pen_saved": 0, "goals_conceded": 0, "yellow": False, "red": False},
            {"api_id": 502, "name": "Sommer", "team": "Inter", "role": "P", "vote": "6.1",
             "goals": 0, "assists": 0, "own_goals": 0, "pen_scored": 0, "pen_missed": 0,
             "pen_saved": 0, "goals_conceded": 2, "yellow": False, "red": False},
        ]
        mgr = LiveSyncManager.get_instance()
        mgr.provider = "apifootball"
        with mock.patch("auctions.providers.apifootball.matchday_live_rows", return_value=rows):
            res = mgr.sync_now(giornata_num=5, is_provisional=True, leagues=[league])
        self.assertEqual(res["status"], "SUCCESS")
        perf = PlayerPerformance.objects.get(player=lautaro)
        self.assertEqual(perf.vote, Decimal("7.5"))
        self.assertEqual(perf.live_source, "apifootball")
        self.assertEqual(PlayerPerformance.objects.get(player=sommer).goals_conceded, 2)

    def test_missing_key_is_reported_not_silent(self):
        from unittest import mock
        import os
        from ..services.voti_live import LiveSyncManager
        mgr = LiveSyncManager.get_instance()
        mgr.provider = "apifootball"
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": ""}):
            res = mgr.sync_now(giornata_num=3, is_provisional=True)
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("APIFOOTBALL_KEY", res["message"])
        self.assertEqual(mgr.get_status()["last_status"], "ERROR")

    def test_old_provider_value_falls_back_to_apifootball(self):
        from ..services.voti_live import normalize_provider
        self.assertEqual(normalize_provider("fantacalcio_web"), "apifootball")
        self.assertEqual(normalize_provider(None), "apifootball")
        self.assertEqual(normalize_provider("simulation"), "simulation")

    def test_no_third_party_fantasy_site_is_read(self):
        from pathlib import Path
        from ..services import voti_live
        source = Path(voti_live.__file__).read_text(encoding="utf-8")
        self.assertNotIn("fantacalcio.it", source)
        self.assertNotIn("BeautifulSoup", source)
