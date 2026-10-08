"""Voto algoritmico: il voto in pagella calcolato dai fatti della partita."""
from decimal import Decimal

from django.test import SimpleTestCase

from .. import scoring
from ..voto_algoritmico import (
    ALGO_DEFAULTS, calibrate, effective_algo_rules, match_votes, player_vote,
)


def row(**kw):
    """Riga di 90' senza eventi in un pareggio 1-1."""
    base = {"minutes": 90, "team_goals_for": 1, "team_goals_against": 1}
    base.update(kw)
    return base


class DirittoAlVotoTests(SimpleTestCase):
    def test_non_entrato_senza_voto(self):
        self.assertIsNone(player_vote(row(minutes=0), "C")["vote"])

    def test_pochi_minuti_senza_voto(self):
        self.assertIsNone(player_vote(row(minutes=20), "C")["vote"])

    def test_pochi_minuti_con_gol_prende_il_voto(self):
        self.assertIsNotNone(player_vote(row(minutes=10, goals=1), "A")["vote"])

    def test_portiere_subentrato_che_subisce_prende_il_voto(self):
        res = player_vote(row(minutes=15, goals_conceded=1), "P")
        self.assertIsNotNone(res["vote"])

    def test_soglia_minuti_configurabile(self):
        self.assertIsNotNone(player_vote(row(minutes=20), "C", {"min_minutes": 15})["vote"])


class VotoTests(SimpleTestCase):
    def test_partita_anonima_vale_sei(self):
        self.assertEqual(player_vote(row(), "C")["vote"], Decimal("6.0"))

    def test_attaccante_doppietta_in_vittoria(self):
        # 6 + 0,25 vittoria + 2 × 0,5 gol = 7,25 → 7,5
        res = player_vote(row(goals=2, team_goals_for=2, team_goals_against=0), "A")
        self.assertEqual(res["vote"], Decimal("7.5"))
        self.assertEqual(res["breakdown"]["eventi"], 1.0)

    def test_portiere_imbattuto_con_parate(self):
        # 6 + 0,25 vittoria + 0,5 imbattuto + (5 − 2,5 parate medie) × 0,15 = 7,125 → 7,0
        res = player_vote(row(team_goals_for=1, team_goals_against=0, saves=5,
                              goals_conceded=0), "P")
        self.assertEqual(res["breakdown"]["rendimento"], 0.375)
        self.assertEqual(res["vote"], Decimal("7.0"))

    def test_rendimento_sotto_media_abbassa_il_voto(self):
        # difensore dato dalla fonte con zero contrasti, intercetti e duelli vinti
        res = player_vote(row(tackles=0, interceptions=0, duels_won=0, blocks=0), "D")
        self.assertLess(res["breakdown"]["rendimento"], 0)
        self.assertEqual(res["vote"], Decimal("5.5"))

    def test_statistica_assente_vale_nella_media(self):
        self.assertEqual(player_vote(row(), "D")["breakdown"]["rendimento"], 0.0)

    def test_portiere_disastro(self):
        # 6 − 0,25 sconfitta − 0,25 scarto − 3 × 0,25 subiti = 4,75 → 5,0
        res = player_vote(row(team_goals_for=0, team_goals_against=3,
                              goals_conceded=3), "P")
        self.assertEqual(res["vote"], Decimal("5.0"))

    def test_difensore_stima_gol_subiti_dai_minuti(self):
        # in campo 45' su 2 gol subiti dalla squadra → 1 gol "suo"
        res = player_vote(row(minutes=45, team_goals_for=2, team_goals_against=2), "D")
        self.assertEqual(res["breakdown"]["reparto"], -0.125)

    def test_senza_risultato_niente_porta_inviolata_per_i_difensori(self):
        res = player_vote({"minutes": 90}, "D")
        self.assertEqual(res["breakdown"]["reparto"], 0.0)
        self.assertEqual(res["vote"], Decimal("6.0"))

    def test_rosso_assorbe_il_giallo(self):
        res = player_vote(row(yellow=True, red=True), "C")
        self.assertEqual(res["breakdown"]["eventi"], -1.0)
        self.assertEqual(res["vote"], Decimal("5.0"))

    def test_voto_limitato_in_alto(self):
        res = player_vote(row(goals=4, team_goals_for=5, team_goals_against=0,
                              shots_on=8), "A")
        self.assertEqual(res["vote"], Decimal("8.5"))

    def test_rendimento_non_esplode_per_chi_entra_tardi(self):
        # 2 tiri in porta in 10': senza correzione varrebbero 18 tiri a partita
        res = player_vote(row(minutes=10, goals=1, shots_on=2), "A")
        self.assertLess(res["breakdown"]["rendimento"], 0.1)

    def test_arrotondamento_al_mezzo_punto(self):
        self.assertEqual(player_vote(row(assists=1), "C")["vote"], Decimal("6.5"))  # 6,25
        self.assertEqual(player_vote(row(key_passes=1), "C")["vote"], Decimal("6.0"))

    def test_ruolo_sconosciuto_trattato_da_centrocampista(self):
        self.assertEqual(player_vote(row(), "X")["vote"], Decimal("6.0"))


class RegoleTests(SimpleTestCase):
    def test_lega_cambia_un_solo_peso(self):
        rules = effective_algo_rules({"perf_weights": {"A": {"shots_on": 0.5}}})
        self.assertEqual(rules["perf_weights"]["A"]["shots_on"], 0.5)
        self.assertEqual(rules["perf_weights"]["A"]["key_passes"],
                         ALGO_DEFAULTS["perf_weights"]["A"]["key_passes"])
        self.assertEqual(rules["perf_weights"]["P"], ALGO_DEFAULTS["perf_weights"]["P"])

    def test_i_default_non_vengono_modificati(self):
        effective_algo_rules({"perf_weights": {"A": {"shots_on": 9}}})
        self.assertEqual(ALGO_DEFAULTS["perf_weights"]["A"]["shots_on"], 0.12)

    def test_calibrazione(self):
        raw = [5.0, 5.5, 6.0, 6.5, 7.0] * 10
        cal = calibrate(raw, target_mean=6.0, target_sd=0.35)
        self.assertAlmostEqual(cal["calib_scale"], 0.35 / 0.7071, places=3)
        self.assertAlmostEqual(cal["calib_shift"], 0.0, places=3)
        # applicata: un 7 grezzo diventa ~6,35 → 6,5
        res = player_vote(row(goals=2), "A", cal)
        self.assertEqual(res["vote"], Decimal("6.5"))
        self.assertIn("calibrazione", res["breakdown"])

    def test_calibrazione_rifiuta_campioni_piccoli(self):
        with self.assertRaises(ValueError):
            calibrate([6.0] * 10)


class IntegrazioneTests(SimpleTestCase):
    def test_match_votes_sostituisce_solo_il_voto(self):
        rows = [row(name="Rossi", role="A", vote="7.3", goals=1, team_goals_for=1,
                    team_goals_against=0)]
        out = match_votes(rows)
        # il 7.3 della fonte viene ignorato: 6 + 0,25 vittoria + 0,5 gol = 6,75 → 7,0
        self.assertEqual(out[0]["vote"], Decimal("7.0"))
        self.assertEqual(out[0]["goals"], 1)
        self.assertIn("breakdown", out[0]["algo"])

    def test_bonus_del_motore_si_sommano_al_voto_algoritmico(self):
        res = player_vote(row(goals=1, team_goals_for=1, team_goals_against=0), "A")
        perf = {"vote": res["vote"], "goals": 1}
        fv, has = scoring.player_fantavoto(perf, "A", scoring.effective_rules())
        self.assertTrue(has)
        self.assertEqual(fv, res["vote"] + 3)


class ApiFootballRigheTests(SimpleTestCase):
    def test_righe_con_minuti_statistiche_e_risultato(self):
        from ..providers import apifootball

        players = [
            {"team": {"name": "Inter"}, "players": [
                {"player": {"id": 1, "name": "Attaccante"},
                 "statistics": [{"games": {"minutes": 90, "rating": "7.0", "position": "F"},
                                 "goals": {"total": 2, "assists": 0},
                                 "shots": {"on": 3}, "passes": {"key": 2},
                                 "penalty": {"won": 1, "commited": None}}]},
            ]},
            {"team": {"name": "Milan"}, "players": [
                {"player": {"id": 2, "name": "Difensore"},
                 "statistics": [{"games": {"minutes": 90, "rating": "5.5", "position": "D"},
                                 "goals": {"total": 0}, "tackles": {"total": 4, "interceptions": 2},
                                 "penalty": {"commited": 1}}]},
            ]},
        ]
        events = [{"type": "Goal", "detail": "Own Goal", "player": {"id": 2}}]

        def fake_get(path, params, get=None):
            return {"/fixtures/players": players, "/fixtures/events": events}[path]

        from unittest import mock
        with mock.patch.object(apifootball, "_get", side_effect=fake_get):
            rows = {r["api_id"]: r for r in apifootball.fixture_player_rows(99)}
        # Inter: 2 gol + 1 autogol del Milan = 3; Milan 0
        self.assertEqual((rows[1]["team_goals_for"], rows[1]["team_goals_against"]), (3, 0))
        self.assertEqual((rows[2]["team_goals_for"], rows[2]["team_goals_against"]), (0, 3))
        self.assertEqual((rows[1]["minutes"], rows[1]["shots_on"], rows[1]["key_passes"],
                          rows[1]["pen_won"]), (90, 3, 2, 1))
        self.assertEqual((rows[2]["tackles"], rows[2]["interceptions"], rows[2]["pen_committed"],
                          rows[2]["own_goals"]), (4, 2, 1, 1))
        votes = {r["api_id"]: r["vote"] for r in match_votes(rows.values())}
        self.assertGreater(votes[1], votes[2])
