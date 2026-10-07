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


class GiornatePageTests(TestCase):
    """Giornate & Voti: lo stesso contenuto in console e app, le azioni tornano
    alla pagina da cui partono, e le formazioni si bloccano anche a mano."""

    def setUp(self):
        import re as _re
        from django.contrib.auth.models import User
        from ..models import Participant
        self._re = _re
        self.owner = User.objects.create_user("owner_gv", password="pw")
        self.league = League.objects.create(name="Lega Giornate", owner=self.owner)
        self.team = Participant.objects.create(display_name="Owner FC", league=self.league, user=self.owner)
        Participant.objects.create(display_name="Rivali", league=self.league)
        self.client.force_login(self.owner)

    def _part(self, resp):
        html = resp.content.decode()
        part = html[html.index("<!-- giornate-manage:start -->"):html.index("<!-- giornate-manage:end -->")]
        part = part.replace("/app/giornate/", "/dashboard/giornate/")
        return self._re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)

    def test_same_screen_in_console_and_app(self):
        from django.urls import reverse
        q = f"?league={self.league.id}"
        console = self.client.get(reverse("admin_giornate") + q)
        app = self.client.get(reverse("app_giornate") + q)
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        self.assertTemplateUsed(console, "auctions/admin_giornate.html")
        self.assertTemplateUsed(app, "auctions/app_giornate.html")
        self.assertContains(console, "Blocca formazioni")
        self.assertEqual(self._part(console), self._part(app))

    def test_season_is_named_after_the_football_year(self):
        import datetime
        from ..views.admin_voti import _season_name
        self.assertEqual(_season_name(datetime.date(2026, 10, 7)), "Stagione 2026/27")
        self.assertEqual(_season_name(datetime.date(2027, 3, 1)), "Stagione 2026/27")
        self.assertEqual(_season_name(datetime.date(2099, 7, 1)), "Stagione 2099/00")

    def test_lock_freezes_lineups_and_returns_to_the_console(self):
        from django.urls import reverse
        from ..models import MatchdayFormation, Season
        self.client.get(reverse("admin_giornate") + f"?league={self.league.id}")   # creates the season
        season = Season.objects.get(league=self.league, is_current=True)
        back = reverse("admin_giornate") + f"?league={self.league.id}&giornata=1"
        resp = self.client.post(reverse("app_giornata_lock"), {
            "league_id": self.league.id, "giornata_number": 1, "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        g1 = season.giornate.get(number=1)
        self.assertEqual(g1.status, "LOCKED")
        self.assertEqual(MatchdayFormation.objects.filter(giornata=g1).count(), 2)

    def test_lock_refused_for_another_league(self):
        from django.contrib.auth.models import User
        from django.urls import reverse
        other = League.objects.create(name="Altra", owner=User.objects.create_user("x_gv", password="pw"))
        resp = self.client.post(reverse("admin_giornata_lock"), {"league_id": other.id, "giornata_number": 1})
        self.assertEqual(resp.status_code, 403)


class AdminFormationEditorTests(TestCase):
    """L'admin gestisce le formazioni di ogni giornata dalla pagina Giornate:
    anche quelle bloccate o calcolate (correzione, con ricalcolo)."""

    def setUp(self):
        import re as _re
        from django.contrib.auth.models import User
        from ..models import Giornata, Participant, Player, Season
        from .. import services
        self._re = _re
        self.services = services
        self.owner = User.objects.create_user("owner_fe", password="pw")
        self.league = League.objects.create(name="Lega Editor", owner=self.owner)
        self.team = Participant.objects.create(display_name="Squadra A", league=self.league)
        Participant.objects.create(display_name="Squadra B", league=self.league)
        self.players = {}
        for role, n in (("P", 1), ("D", 5), ("C", 3), ("A", 3)):
            self.players[role] = [Player.objects.create(name=f"{role}{i}", role=role, team="X", league=self.league,
                                                        owner=self.team, cost=Decimal("1")) for i in range(n)]
        P, D, C, A = (self.players[r] for r in "PDCA")
        self.xi = [P[0], *D[:4], *C, *A]
        season = Season.objects.create(league=self.league, name="Stagione 2026/27")
        self.g1 = Giornata.objects.create(season=season, number=1, status="OPEN")
        self.g2 = Giornata.objects.create(season=season, number=2, status="SCHEDULED")
        self.client.force_login(self.owner)

    def _url(self, name, giornata):
        from django.urls import reverse
        return f"{reverse(name, args=[self.team.id])}?giornata={giornata.id}"

    def test_giornate_page_lists_every_team_with_an_edit_link(self):
        from django.urls import reverse
        resp = self.client.get(reverse("admin_giornate") + f"?league={self.league.id}&giornata=2")
        self.assertContains(resp, "Formazioni Giornata 2")
        self.assertContains(resp, "Squadra B")
        self.assertContains(resp, self._url("admin_formation_edit", self.g2))

    def test_admin_sets_a_future_giornata_without_touching_the_managers_lineup(self):
        from ..models import Formation, MatchdayFormation
        self.services.save_formation(self.team, "4-3-3", [str(p.id) for p in self.xi], giornata=self.g1)
        url = self._url("admin_formation_edit", self.g2)
        resp = self.client.post(url, {"giornata": self.g2.id, "module": "3-4-3", "save": "1",
                                      "starter": [str(p.id) for p in self.xi[:4]], "next": url})
        self.assertRedirects(resp, url, fetch_redirect_response=False)
        mf = MatchdayFormation.objects.get(giornata=self.g2, participant=self.team)
        self.assertEqual(mf.module, "3-4-3")
        self.assertEqual(Formation.objects.get(participant=self.team).module, "4-3-3")

    def test_correcting_a_scored_giornata_recomputes_it(self):
        from ..models import GiornataScore, PlayerPerformance
        self.services.save_formation(self.team, "4-3-3", [str(p.id) for p in self.xi], giornata=self.g1)
        d5 = self.players["D"][4]
        for group in self.players.values():
            for pl in group:
                PlayerPerformance.objects.create(giornata=self.g1, player=pl,
                                                 vote=Decimal("10") if pl == d5 else Decimal("6"))
        self.services.compute_giornata(self.g1)
        before = GiornataScore.objects.get(giornata=self.g1, participant=self.team).total
        # The admin puts D5 (vote 10) in place of D0 (vote 6) for that giornata.
        ids = [str(p.id) for p in self.xi]
        ids[1] = str(d5.id)
        self.client.post(self._url("admin_formation_edit", self.g1),
                         {"giornata": self.g1.id, "module": "4-3-3", "save": "1", "starter": ids})
        after = GiornataScore.objects.get(giornata=self.g1, participant=self.team).total
        self.assertEqual(after - before, Decimal("4"))
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.status, "SCORED")

    def test_same_editor_in_console_and_app(self):
        console = self.client.get(self._url("admin_formation_edit", self.g1))
        app = self.client.get(self._url("app_formation_edit", self.g1))
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        for tag in ("formation-admin", "formation-pitch"):
            with self.subTest(tag):
                self.assertEqual(self._part(console, tag), self._part(app, tag))

    def _part(self, resp, tag):
        html = resp.content.decode()
        part = html[html.index(f"<!-- {tag}:start -->"):html.index(f"<!-- {tag}:end -->")]
        part = part.replace("/app/giornate/", "/dashboard/giornate/")
        return self._re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)

    def test_other_leagues_admin_is_refused(self):
        from django.contrib.auth.models import User
        stranger = User.objects.create_user("stranger_fe", password="pw")
        League.objects.create(name="Sua", owner=stranger)
        self.client.force_login(stranger)
        resp = self.client.get(self._url("admin_formation_edit", self.g1))
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(self._url("admin_formation_edit", self.g1),
                                {"giornata": self.g1.id, "module": "4-3-3", "starter": []})
        self.assertEqual(resp.status_code, 403)


class CoAdminFormationTests(AdminFormationEditorTests):
    """Lo stesso, entrando come co-admin della lega (league.admins), non come proprietario."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        co = User.objects.create_user("coadmin_fe", password="pw")
        self.league.admins.add(co)
        self.client.force_login(co)


class VotiFileLayoutTests(TestCase):
    """File dei voti a blocchi per squadra: nome del club su una riga, intestazione
    ripetuta per ogni blocco, autoreti nella colonna «Au»."""

    HEAD = ["Cod.", "Ruolo", "Nome", "Voto", "Gf", "Gs", "Rp", "Rs", "Rf", "Au", "Amm", "Esp", "Ass"]

    def _xlsx(self, rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _per_club(self):
        return self._xlsx([
            ["Voti Giornata 5"], [],
            ["INTER"], self.HEAD,
            [1, "A", "Martinez L.", 7, 1, 0, 0, 0, 0, 0, 0, 0, 0],
            [2, "D", "Bastoni", 5.5, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            ["GENOA"], self.HEAD,
            [3, "A", "Martinez L.", 5, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ])

    def test_per_club_blocks(self):
        rows = parse_voti_file(self._per_club(), "voti.xlsx")
        self.assertEqual([r["name"] for r in rows], ["Martinez L.", "Bastoni", "Martinez L."])
        self.assertEqual([r["team"] for r in rows], ["INTER", "INTER", "GENOA"])
        bastoni = rows[1]
        self.assertEqual(bastoni["own_goals"], 1)
        self.assertTrue(bastoni["yellow"])
        self.assertEqual(rows[0]["goals"], 1)

    def test_semicolon_csv(self):
        text = "Nome;Squadra;Voto;Gf;Au\nBastoni;Inter;6,5;0;1\n"
        rows = parse_voti_file(text.encode("utf-8"), "voti.csv")
        self.assertEqual(rows[0]["vote"], Decimal("6.5"))
        self.assertEqual(rows[0]["own_goals"], 1)
        self.assertEqual(rows[0]["team"], "Inter")

    def test_same_name_goes_to_the_right_club(self):
        from ..models import Giornata, PlayerPerformance, Season
        league = League.objects.create(name="Omonimi")
        inter = Player.objects.create(league=league, name="Martinez L.", role="A", team="Inter")
        genoa = Player.objects.create(league=league, name="Martinez L.", role="A", team="Genoa")
        g = Giornata.objects.create(season=Season.objects.create(league=league, name="S"), number=5)
        import_voti_giornata(parse_voti_file(self._per_club(), "voti.xlsx"), g, league=league, recompute=False)
        self.assertEqual(PlayerPerformance.objects.get(giornata=g, player=inter).vote, Decimal("7"))
        self.assertEqual(PlayerPerformance.objects.get(giornata=g, player=genoa).vote, Decimal("5"))

    def test_old_excel_xls_is_read(self):
        from unittest import mock
        sheet = mock.Mock(nrows=3)
        sheet.row_values.side_effect = [["NAPOLI"], ["Ruolo", "Nome", "Voto", "Au"], ["D", "Rrahmani", 6.0, 1.0]][:].__getitem__
        book = mock.Mock(**{"sheet_by_index.return_value": sheet})
        with mock.patch("xlrd.open_workbook", return_value=book) as opened:
            rows = parse_voti_file(b"xls-bytes", "Voti.XLS")
        opened.assert_called_once_with(file_contents=b"xls-bytes")
        self.assertEqual(rows[0]["name"], "Rrahmani")
        self.assertEqual(rows[0]["team"], "NAPOLI")
        self.assertEqual(rows[0]["own_goals"], 1)


class ManualScoresTests(TestCase):
    """Punteggi a mano: il totale di ogni squadra com'è su un altro sito
    (Fantapazz esporta solo un'immagine), da cui gol, risultati e classifica."""

    def setUp(self):
        from django.contrib.auth.models import User
        from ..models import Competition, Fixture, Giornata, Participant, Season
        self.owner = User.objects.create_user("owner_ms", password="pw")
        self.league = League.objects.create(name="Lega Fantapazz", owner=self.owner)
        self.home = Participant.objects.create(display_name="Dinamo Viaritta", league=self.league)
        self.away = Participant.objects.create(display_name="Deportivo Zozzfanti", league=self.league)
        season = Season.objects.create(league=self.league, name="Stagione 2026/27")
        self.g = Giornata.objects.create(season=season, number=1, status="OPEN")
        comp = Competition.objects.create(season=season, name="Campionato")
        self.fx = Fixture.objects.create(competition=comp, giornata=self.g, home=self.home, away=self.away)
        self.client.force_login(self.owner)

    def _post(self, data, name="admin_giornata_manual_scores"):
        from django.urls import reverse
        payload = {"league_id": self.league.id, "giornata_number": 1}
        payload.update(data)
        return self.client.post(reverse(name), payload)

    def test_totals_give_goals_result_and_scored_giornata(self):
        from ..models import GiornataScore
        resp = self._post({f"score_{self.home.id}": "85,5", f"score_{self.away.id}": "69.5"})
        self.assertEqual(resp.status_code, 302)
        home = GiornataScore.objects.get(giornata=self.g, participant=self.home)
        self.assertEqual((home.total, home.goals), (Decimal("85.5"), 4))
        self.assertEqual(GiornataScore.objects.get(giornata=self.g, participant=self.away).goals, 1)
        self.fx.refresh_from_db()
        self.assertEqual((self.fx.home_goals, self.fx.away_goals, self.fx.home_points, self.fx.away_points), (4, 1, 3, 0))
        self.g.refresh_from_db()
        self.assertEqual(self.g.status, "SCORED")

    def test_typed_goals_win_over_the_thresholds(self):
        from ..models import GiornataScore
        self._post({f"score_{self.home.id}": "70", f"goals_{self.home.id}": "2", f"score_{self.away.id}": "70"})
        self.assertEqual(GiornataScore.objects.get(giornata=self.g, participant=self.home).goals, 2)
        self.fx.refresh_from_db()
        self.assertEqual((self.fx.home_points, self.fx.away_points), (3, 0))

    def test_bad_value_changes_nothing(self):
        from ..models import GiornataScore
        self._post({f"score_{self.home.id}": "tanti", f"score_{self.away.id}": "70"})
        self.assertFalse(GiornataScore.objects.filter(giornata=self.g).exists())

    def test_back_to_the_app_and_other_league_refused(self):
        from django.contrib.auth.models import User
        from django.urls import reverse
        back = reverse("app_giornate") + f"?league={self.league.id}&giornata=1"
        resp = self._post({f"score_{self.home.id}": "66", "next": back}, name="app_giornata_manual_scores")
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.client.force_login(User.objects.create_user("x_ms", password="pw"))
        self.assertEqual(self._post({f"score_{self.home.id}": "90"}).status_code, 403)

    def test_formation_correction_keeps_typed_totals(self):
        from ..models import GiornataScore
        from .. import services
        self._post({f"score_{self.home.id}": "85,5", f"score_{self.away.id}": "69,5"})
        services.admin_save_matchday_formation(self.home, self.g, "4-3-3", [])
        self.assertEqual(GiornataScore.objects.get(giornata=self.g, participant=self.home).total, Decimal("85.5"))

    def test_live_page_shows_typed_totals(self):
        self._post({f"score_{self.home.id}": "85,5", f"score_{self.away.id}": "69,5"})
        s = self.client.session
        s["participant_id"] = self.home.id
        s.save()
        resp = self.client.get("/app/live/?giornata=1")
        self.assertContains(resp, "inserito a mano")
        board = {row["participant"].id: row["total"] for row in resp.context["leaderboard"]}
        self.assertEqual(board[self.home.id], Decimal("85.5"))
