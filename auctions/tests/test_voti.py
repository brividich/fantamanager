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
