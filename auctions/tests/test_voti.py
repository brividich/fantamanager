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
