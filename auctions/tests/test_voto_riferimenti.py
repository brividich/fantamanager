"""Voti di riferimento (caricati a mano dal superuser) e taratura automatica
del voto algoritmico: parsing, abbinamento, confronto, feature, ridge,
endpoint, permessi e isolamento dalle pagine di lega."""
import io
import json
import random
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .. import voto_algoritmico as va
from .. import voto_taratura as vt
from ..models import AlgoReference, AlgoSample, PlayerPerformance
from ..services import voto_algo
from ..services.voti import _normalize_vote_row, parse_voti_file
from ..voto_algoritmico import effective_algo_rules, player_vote


def csv_file(text, name="voti.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def synthetic(matches=150, per_giornata=550, true=None, noise=0.15, seed=1):
    """Righe simulate su più giornate e voti di riferimento generati da
    parametri noti più rumore."""
    rows = voto_algo.simulated_rows(matches=matches)
    for k, r in enumerate(rows):
        r["_g"] = k // per_giornata
    rnd = random.Random(seed)
    ref = {}
    for i, r in enumerate(rows):
        res = player_vote(r, rules=true)
        if res["raw"] is not None:
            ref[i] = res["raw"] + rnd.gauss(0, noise)
    return rows, ref


# Parametri «veri» dei dati sintetici. Gol subiti e scarto sono scelti perché
# il loro tetto scatti con questi valori e non con quelli di partenza
# (-0,45 × 3 gol < -1; 0,2 × 2 gol di scarto > 0,25).
TRUE = {"win": 0.5, "loss": -0.4, "goal": 0.8, "assist": 0.4, "clean_sheet_P": 0.8,
        "yellow": -0.4, "base": 6.1, "perf_weights": {"A": {"shots_on": 0.2}},
        "conceded_P": -0.45, "conceded_D": -0.35, "margin_step": 0.2}


# --------------------------------------------------------------------------
# Parsing del file
# --------------------------------------------------------------------------

class ParsingTests(SimpleTestCase):
    def test_voto_puro_vince_su_fantavoto(self):
        # Nessuna colonna intitolata esattamente «Voto»: prima vinceva «Fantavoto»
        # (la prima che contiene «voto»), ora la colonna del voto.
        rows = parse_voti_file(b"Nome;Squadra;Ruolo;Fantavoto;Voto Gazzetta\nBarella;Inter;C;9,5;6,5\n", "v.csv")
        self.assertEqual(rows[0]["vote"], Decimal("6.5"))
        row = _normalize_vote_row({"nome": "Barella", "fantavoto": "9.5", "voto gazzetta": "6.5"})
        self.assertEqual(row["vote"], Decimal("6.5"))

    def test_intestazione_esatta_come_prima(self):
        row = _normalize_vote_row({"nome": "Barella", "fantavoto": "9.5", "voto": "6"})
        self.assertEqual(row["vote"], Decimal("6"))
        row = _normalize_vote_row({"nome": "Barella", "voto puro": "7"})
        self.assertEqual(row["vote"], Decimal("7"))

    def test_voti_d_ufficio_e_senza_voto(self):
        text = "Nome,Squadra,Voto\nA,Inter,6*\nB,Inter,s.v.\nC,Inter,sv\nD,Inter,-\nE,Inter,\nF,Inter,5.5\n"
        rows = {r["name"]: r["vote"] for r in parse_voti_file(text.encode(), "v.csv")}
        self.assertEqual(rows["A"], Decimal("6"))
        self.assertTrue(all(rows[k] is None for k in "BCDE"))
        self.assertEqual(rows["F"], Decimal("5.5"))

    def test_righe_normalizzate(self):
        rows = voto_algo.parse_reference_file(csv_file("Nome,Squadra,Ruolo,Voto\nMartinez L.,Inter,A,7\nX,Inter,Por,s.v.\n"))
        self.assertEqual(rows, [{"name": "Martinez L.", "team": "Inter", "role": "A", "vote": "7"},
                                {"name": "X", "team": "Inter", "role": "", "vote": None}])

    def test_limiti(self):
        with self.assertRaisesMessage(ValueError, ".xlsx, .xls o .csv"):
            voto_algo.parse_reference_file(SimpleUploadedFile("voti.pdf", b"x"))
        too_many = "Nome,Voto\n" + "".join(f"G{i},6\n" for i in range(voto_algo.REF_MAX_ROWS + 1))
        with self.assertRaisesMessage(ValueError, "troppe righe"):
            voto_algo.parse_reference_file(csv_file(too_many))
        big = SimpleUploadedFile("voti.csv", b"Nome,Voto\n" + b"x" * (voto_algo.REF_MAX_BYTES + 1))
        with self.assertRaisesMessage(ValueError, "5 MB"):
            voto_algo.parse_reference_file(big)
        with self.assertRaisesMessage(ValueError, "nessuna riga"):
            voto_algo.parse_reference_file(csv_file("a,b\n1,2\n"))


# --------------------------------------------------------------------------
# Abbinamento
# --------------------------------------------------------------------------

SAMPLE = [
    {"name": "L. Martínez", "team": "Inter", "role": "A", "minutes": 90},
    {"name": "J. Martínez", "team": "Inter", "role": "P", "minutes": 0},
    {"name": "Martinez", "team": "Torino", "role": "D", "minutes": 90},
    {"name": "F. Esposito", "team": "Empoli", "role": "A", "minutes": 70},
    {"name": "S. Esposito", "team": "Empoli", "role": "A", "minutes": 20},
    {"name": "Barella", "team": "Inter", "role": "C", "minutes": 90},
]


class MatchingTests(SimpleTestCase):
    def ref(self, name, team, role="", vote="6"):
        return {"name": name, "team": team, "role": role, "vote": vote}

    def test_iniziale_dopo_e_accenti(self):
        m = voto_algo.match_reference(SAMPLE, [self.ref("Martinez L.", "Inter", "A", "7")])
        self.assertEqual(m["votes"], {"0": "7"})

    def test_omonimi_in_squadre_diverse(self):
        m = voto_algo.match_reference(SAMPLE, [self.ref("Martinez", "Torino", "D", "5.5")])
        self.assertEqual(m["votes"], {"2": "5.5"})

    def test_ambiguo(self):
        m = voto_algo.match_reference(SAMPLE, [self.ref("Esposito", "Empoli", "A")])
        self.assertEqual(m["votes"], {})
        self.assertEqual(m["ambiguous"][0]["name"], "Esposito")
        self.assertEqual(set(m["ambiguous"][0]["candidates"]), {"F. Esposito", "S. Esposito"})

    def test_riga_del_campione_usata_una_volta(self):
        m = voto_algo.match_reference(SAMPLE, [self.ref("Barella", "Inter", "C", "6"),
                                               self.ref("Barella N.", "Inter", "C", "8")])
        self.assertEqual(m["votes"], {"5": "6"})
        self.assertEqual(m["unmatched_file"][0]["name"], "Barella N.")
        self.assertIn("già abbinato", m["unmatched_file"][0]["reason"])

    def test_report(self):
        m = voto_algo.match_reference(SAMPLE, [self.ref("Sconosciuto", "Inter"), self.ref("Martinez L.", "INT")])
        self.assertEqual(m["unmatched_file"], [{"name": "Sconosciuto", "team": "Inter"}])
        self.assertEqual(m["votes"], {"0": "6"})                  # sigla del club
        # non abbinati del campione: solo chi ha giocato
        names = {u["name"] for u in m["unmatched_sample"]}
        self.assertNotIn("J. Martínez", names)
        self.assertIn("Barella", names)

    def test_name_match_score_come_prima(self):
        from ..services.footballers import name_match_score
        self.assertEqual(name_match_score("Martinez L.", "A", "Lautaro Martínez", "A"), 3 + 1 + 2)
        self.assertIsNone(name_match_score("Martinez L.", "A", "Josep Martínez", "P"))
        self.assertEqual(name_match_score("Barella", "C", "barella", ""), 100)


# --------------------------------------------------------------------------
# Confronto
# --------------------------------------------------------------------------

class ComparisonTests(SimpleTestCase):
    def test_metriche_calcolate_a_mano(self):
        rows = [{"name": f"G{i}", "team": "Inter", "role": "C", "minutes": 90,
                 "team_goals_for": 1, "team_goals_against": 0} for i in range(14)]
        bd = {"breakdown": {"base": 6.0}}
        before = [(6.5, bd)] * 12 + [(None, bd), (6.0, bd)]
        after = [(6.0, bd)] * 11 + [(7.0, bd)] + [(None, bd), (6.0, bd)]
        ref = {i: 6.0 for i in range(12)}
        ref[12] = 6.0          # algoritmo s.v., riferimento con voto
        ref[13] = None         # algoritmo con voto, riferimento s.v.
        c = voto_algo.compare_reference(rows, before, after, ref)
        self.assertEqual(c["matched"], 14)
        self.assertEqual((c["before"]["mae"], c["before"]["bias"], c["before"]["exact_pct"],
                          c["before"]["within_pct"]), (0.5, 0.5, 0.0, 100.0))
        # bozza: 11 esatti e uno sbagliato di 1
        self.assertEqual(c["after"]["mae"], round(1 / 12, 3))
        self.assertEqual(c["after"]["bias"], round(1 / 12, 3))
        self.assertEqual(c["after"]["exact_pct"], round(100 * 11 / 12, 1))
        self.assertEqual(c["after"]["within_pct"], round(100 * 11 / 12, 1))
        self.assertEqual(c["after"]["rmse"], round((1 / 12) ** 0.5, 3))
        self.assertEqual(c["sv_after"], {"both": 12, "alg_only": 1, "ref_only": 1, "none": 0})
        groups = {(g["dim"], g["group"]): g for g in c["groups"]}
        self.assertEqual(set(groups), {("Risultato della squadra", "Vittoria"), ("Minuti giocati", "90'"),
                                       ("Eventi", "nessun evento")})
        self.assertEqual(groups[("Minuti giocati", "90'")]["before"]["bias"], 0.5)
        self.assertEqual(groups[("Minuti giocati", "90'")]["after"]["n"], 12)
        self.assertEqual(c["heatmap_after"][4][4], 11)       # 6,0 × 6,0 sulla diagonale
        self.assertEqual(c["heatmap_after"][6][4], 1)        # 7,0 × 6,0
        self.assertEqual(c["disagreements"][0]["after"], 7.0)
        json.dumps(c)

    def test_gruppi_piccoli_nascosti(self):
        rows = [{"role": "D", "minutes": 20, "goals": 1, "team_goals_for": 0, "team_goals_against": 0}] * 5
        bd = {"breakdown": {}}
        c = voto_algo.compare_reference(rows, [(6.0, bd)] * 5, [(6.0, bd)] * 5, {i: 6.0 for i in range(5)})
        self.assertEqual(c["groups"], [])

    def test_effetto_dei_parametri_come_il_ricalcolo_completo(self):
        """L'anteprima ricalcola, per ogni parametro lineare, solo le righe in
        cui conta: il risultato è identico al ricalcolo di tutte le righe."""
        rows = voto_algo.simulated_rows(matches=40)
        rules = {k: v for k, v in effective_algo_rules({}).items() if not k.startswith("_")}
        draft = json.loads(json.dumps(rules))
        draft.update({"win": 0.6, "draw": 0.1, "margin_step": 0.3, "clean_sheet_D": 0.6, "conceded_P": -0.5,
                      "goal": 0.9, "yellow": -0.5, "base": 6.2, "perf_cap": 0.5, "min_minutes": 15})
        draft["perf_weights"]["D"]["tackles"] = 0.2
        draft["perf_malus"]["fouls"] = -0.1
        fast = voto_algo.preview(rows, rules, draft)["per_field"]
        with mock.patch.object(vt, "group_of", return_value=None):
            full = voto_algo.preview(rows, rules, draft)["per_field"]
        self.assertEqual(len(fast), 12)
        self.assertEqual(fast, full)

    def test_media_dei_riferimenti_e_fonte_singola(self):
        refs = [{"id": 1, "label": "Uno", "matched": {0: "6", 1: None, 2: None}},
                {"id": 2, "label": "Due", "matched": {0: "7", 1: "5", 2: None}}]
        self.assertEqual(voto_algo.reference_votes(refs, "media"), {0: 6.5, 1: 5.0, 2: None})
        self.assertEqual(voto_algo.reference_votes(refs, "uno"), {0: 6.0, 1: None, 2: None})

    def test_anteprima_con_riferimenti(self):
        rows = [{"name": f"G{i}", "team": "Inter", "role": "A", "minutes": 90, "goals": 1,
                 "team_goals_for": 1, "team_goals_against": 0} for i in range(20)]
        refs = [{"id": 1, "label": "Fonte", "matched": {i: "7.5" for i in range(20)}}]
        res = voto_algo.preview(rows, {}, {"goal": 1.0}, references=refs, reference_mode="Fonte")
        block = res["references"]
        self.assertEqual(block["mode"], "Fonte")
        self.assertLess(block["after"]["mae"], block["before"]["mae"])
        self.assertIsNone(voto_algo.preview(rows, {}, {})["references"])
        json.dumps(res)


# --------------------------------------------------------------------------
# Feature e taratura
# --------------------------------------------------------------------------

def _raw_unrounded(row, role, rules):
    """Il grezzo di ``player_vote`` prima dell'arrotondamento a 3 decimali."""
    return (rules["base"] + va._result_part(row, rules) + va._defence_part(row, role, rules)
            + va._events_part(row, role, rules) + va._perf_part(row, role, rules))


class FeatureTests(SimpleTestCase):
    def check(self, rows, rules):
        checked = 0
        for row in rows:
            role = row["role"]
            res = player_vote(row, role, rules)
            if res["raw"] is None or vt.cap_active(row, role, rules):
                continue
            x = vt.feature_row(row, role, rules)
            pred = rules["base"] + sum(vt.get_value(rules, p) * v for p, v in x.items())
            self.assertAlmostEqual(pred, _raw_unrounded(row, role, rules), delta=1e-9)
            # ``raw`` di player_vote è arrotondato a 3 decimali
            self.assertAlmostEqual(pred, res["raw"], delta=5e-4 + 1e-9)
            checked += 1
        return checked

    def test_identita_senza_tetti_tutti_i_ruoli(self):
        rules = effective_algo_rules({})
        rows = voto_algo.simulated_rows(matches=40)
        self.assertGreater(self.check(rows, rules), 500)
        self.assertEqual({r["role"] for r in rows}, {"P", "D", "C", "A"})

    def test_identita_senza_statistiche_avanzate(self):
        rules = effective_algo_rules({"win": 0.4, "goal_D": 1.1})
        plain = [{k: v for k, v in r.items() if k not in va.STAT_KEYS} for r in voto_algo.simulated_rows(matches=30)]
        self.assertGreater(self.check(plain, rules), 400)
        self.assertTrue(all(not k.startswith("perf_") for r in plain for k in vt.feature_row(r, r["role"], rules)))

    def test_tetti_riconosciuti(self):
        rules = effective_algo_rules({})
        big_win = {"role": "C", "minutes": 90, "team_goals_for": 5, "team_goals_against": 0}
        self.assertTrue(vt.cap_active(big_win, "C", rules))         # scarto oltre il tetto
        self.assertFalse(vt.cap_active({**big_win, "team_goals_for": 2}, "C", rules))

    def test_gauss(self):
        sol = vt.solve([[2, 1, -1], [-3, -1, 2], [-2, 1, 2]], [8, -11, -3])
        for got, want in zip(sol, [2, 3, -1]):
            self.assertAlmostEqual(got, want, places=9)


class TuningTests(SimpleTestCase):
    def test_recupera_parametri_noti(self):
        rows, ref = synthetic(true=TRUE)
        out = vt.tune(rows, ref, effective_algo_rules({}), lam=0.01, ranges=voto_algo.tuning_ranges(effective_algo_rules({})))
        got = {p["path"]: p["proposed"] for p in out["params"]}
        for path, want in (("win", 0.5), ("loss", -0.4), ("goal", 0.8), ("assist", 0.4), ("clean_sheet_P", 0.8),
                           ("yellow", -0.4), ("base", 6.1), ("perf_weights.A.shots_on", 0.2),
                           # i parametri con un tetto: prima della stima ripetuta restavano
                           # attenuati (-0,36, -0,32, 0,15), ora come gli altri
                           ("conceded_P", -0.45), ("conceded_D", -0.35), ("margin_step", 0.2)):
            self.assertAlmostEqual(got[path], want, delta=0.06, msg=path)
        self.assertGreater(out["iterations"], 1)
        self.assertEqual(out["iterations_stop"], "stabile")
        self.assertEqual(out["validation"]["mode"], "giornate")
        self.assertGreater(out["validation"]["gain"], vt.MIN_GAIN)
        self.assertIsNone(out["warning"])
        self.assertIsNotNone(out["calibration"])
        json.dumps(out)

    def test_prudenza_alta_resta_vicina(self):
        rows, ref = synthetic(true=TRUE)
        rules = effective_algo_rules({})
        low = vt.tune(rows, ref, rules, lam=0.01)
        high = vt.tune(rows, ref, rules, lam=20)
        spread = lambda out: sum(abs(p["delta"]) for p in out["params"])
        self.assertLess(spread(high), spread(low) / 4)
        self.assertLess(max(abs(p["delta"]) for p in high["params"]), 0.1)

    def test_vincoli_di_range_spostamento_e_segno(self):
        rows = voto_algo.simulated_rows(matches=120)
        for k, r in enumerate(rows):
            r["_g"] = k // 600
        rules = effective_algo_rules({})
        ref = {}
        for i, r in enumerate(rows):
            res = player_vote(r, rules=rules)
            if res["raw"] is None:
                continue
            # chi segna vale 3 punti in più, l'ammonito 2 in più (il malus vorrebbe diventare un bonus)
            ref[i] = res["raw"] + 3 * (r.get("goals") or 0) + (2 if r.get("yellow") and not r.get("red") else 0)
        ranges = voto_algo.tuning_ranges(rules)
        out = vt.tune(rows, ref, rules, lam=0.01, ranges=ranges)
        params = {p["path"]: p for p in out["params"]}
        for p in out["params"]:
            move = vt.MOVE_PERF if p["path"].startswith("perf_") else vt.MOVE_FLAT
            self.assertLessEqual(abs(p["delta"]), move + 1e-9, p["path"])
            lo, hi = ranges.get(p["path"], (-99, 99))
            self.assertTrue(lo - 1e-9 <= p["proposed"] <= hi + 1e-9, p["path"])
        self.assertAlmostEqual(params["goal"]["delta"], vt.MOVE_FLAT)
        self.assertIn("spostamento", params["goal"]["flags"])
        self.assertLessEqual(params["yellow"]["proposed"], 0)
        self.assertIn("segno", params["yellow"]["flags"])

    def test_pochi_giocatori(self):
        rows, ref = synthetic(matches=10, true=TRUE)
        with self.assertRaisesMessage(ValueError, "almeno 300"):
            vt.tune(rows, ref, effective_algo_rules({}))

    def test_verifica_su_giornate_mai_viste(self):
        rows, ref = synthetic(true=TRUE, per_giornata=600)
        giornate = {r["_g"] for r in rows}
        calls = []
        original = vt._fit

        def spy(cands, *args, **kwargs):
            calls.append({rows[c[0]]["_g"] for c in cands} if cands else set())
            calls[-1] = (len(cands), calls[-1])
            return original(cands, *args, **kwargs)

        with mock.patch.object(vt, "_fit", side_effect=spy):
            out = vt.tune(rows, ref, effective_algo_rules({}), lam=0.5)
        self.assertEqual(out["validation"]["folds"], len(giornate))
        self.assertEqual(calls[0], (out["rows_both"], giornate))          # proposta: tutte le righe
        # ogni verifica si tara senza la giornata su cui si misura
        held_out = [giornate - seen for _n, seen in calls[1:]]
        self.assertEqual(sorted(len(h) for h in held_out), [1] * len(giornate))
        self.assertEqual(set().union(*held_out), giornate)
        per_g = {g: sum(1 for c in vt._candidates(rows, ref, vt._neutral(effective_algo_rules({})))
                        if rows[c[0]]["_g"] == g) for g in giornate}
        for (n, _seen), (g,) in zip(calls[1:], held_out):
            self.assertEqual(n, out["rows_both"] - per_g[g])

    def test_una_giornata_divide_per_partita(self):
        rows, ref = synthetic(true=TRUE, per_giornata=10 ** 6)
        out = vt.tune(rows, ref, effective_algo_rules({}), lam=0.5)
        self.assertEqual((out["validation"]["mode"], out["validation"]["folds"]), ("partite", 1))
        keys = vt.match_keys(rows)
        # le due squadre della stessa partita hanno la stessa chiave
        self.assertEqual(keys[0], keys[11])
        self.assertNotEqual(keys[0], keys[22])

    def test_seconda_iterazione_esclude_i_tetti_dei_valori_stimati(self):
        """Portieri con 3 gol subiti: con -0,25 per gol il tetto (-1) non
        scatta, con i valori veri (-0,45) sì. La prima stima li tiene, la
        seconda li esclude."""
        rows, ref = synthetic(true=TRUE)
        anchor = vt._neutral(effective_algo_rules({}))
        cands = vt._candidates(rows, ref, anchor)
        gk3 = {c[0] for c in cands if c[2] == "P" and va._i(rows[c[0]].get("goals_conceded")) == 3}
        self.assertTrue(gk3)
        fit = vt._fit(cands, anchor, vt.tunable_paths(anchor), 0.01,
                      voto_algo.tuning_ranges(effective_algo_rules({})))
        self.assertFalse(gk3 & fit["trace"][0])            # prima stima: dentro
        self.assertTrue(gk3 <= fit["trace"][1])            # seconda: fuori
        self.assertTrue(gk3 <= fit["excluded"])
        self.assertLess(fit["values"]["conceded_P"], -0.4)

    def test_iterazioni_limitate_e_risultato_deterministico(self):
        rules = effective_algo_rules({})
        # Valore vero sul filo del tetto (0,25 = margin_cap): l'insieme delle
        # righe al tetto cambia a ogni stima e non si ferma da solo.
        edge = {**TRUE, "margin_step": 0.25}
        for true in (TRUE, edge):
            rows, ref = synthetic(true=true)
            one = vt.tune(rows, ref, rules, lam=0.01)
            two = vt.tune(rows, ref, rules, lam=0.01)
            self.assertLessEqual(one["iterations"], vt.MAX_ITERATIONS)
            self.assertEqual(one["proposal"], two["proposal"])
            self.assertEqual((one["iterations"], one["rows_capped"]), (two["iterations"], two["rows_capped"]))
        self.assertIn(one["iterations_stop"], ("limite", "oscilla"))
        self.assertAlmostEqual(one["proposal"].get("margin_step", 0.125), 0.25, delta=0.06)

    def test_oscillazione_tiene_la_proposta_migliore(self):
        """Se l'insieme escluso torna uguale a uno già visto, la stima si
        ferma e tiene la proposta con l'errore di taratura più basso."""
        rows, ref = synthetic(true=TRUE)
        anchor = vt._neutral(effective_algo_rules({}))
        cands = vt._candidates(rows, ref, anchor)
        a, b = frozenset({cands[0][0]}), frozenset({cands[1][0]})
        real = vt._analyze

        def fake(row, role, rules):
            x, caps = real(row, role, rules)
            if rules is anchor:
                return x, ([] if row is not cands[0][1] else ["finto"])
            return x, (["finto"] if row is cands[0][1] or (row is cands[1][1] and fake.flip) else [])
        fake.flip = False

        estimates = []

        def estimate(entries, *args, **kwargs):
            estimates.append(len(entries))
            fake.flip = not fake.flip
            return ({"win": 0.4 if fake.flip else 0.3}, {})

        with mock.patch.object(vt, "_analyze", side_effect=fake), \
                mock.patch.object(vt, "_estimate", side_effect=estimate):
            fit = vt._fit([(i, row, role, r, x, row is cands[0][1]) for i, row, role, r, x, _c in cands],
                          anchor, ["win"], 0.5, {})
        self.assertEqual(fit["stopped"], "oscilla")
        self.assertEqual(fit["trace"], [a, a | b])
        mae = {v: vt._mae(vt._vote_pairs([c[1] for c in cands], {n: c[3] for n, c in enumerate(cands)},
                                          vt._apply(anchor, {"win": v}))) for v in (0.3, 0.4)}
        self.assertEqual(fit["values"]["win"], min(mae, key=mae.get))

    def test_gruppi_esclusi_restano_invariati(self):
        rows, ref = synthetic(true=TRUE)
        out = vt.tune(rows, ref, effective_algo_rules({}), groups=["eventi"], lam=0.1)
        self.assertEqual({p["group"] for p in out["params"]}, {"eventi"})
        self.assertNotIn("win", out["proposal"])


# --------------------------------------------------------------------------
# Endpoint, permessi, isolamento
# --------------------------------------------------------------------------

def make_sample(name, matches=60, seed=3, true=TRUE):
    """Un campione «API-Football» e il CSV dei voti di riferimento abbinabili."""
    rows = voto_algo.simulated_rows(matches=matches, seed=seed)
    sample = AlgoSample.objects.create(name=name, rows=rows)
    lines = ["Nome,Squadra,Ruolo,Voto"]
    rnd = random.Random(seed)
    for r in rows:
        res = player_vote(r, rules=true)
        vote = "s.v." if res["vote"] is None else f"{res['raw'] + rnd.gauss(0, 0.1):.2f}"
        lines.append(f"{r['name']},{r['team']},{r['role']},{vote}")
    return sample, "\n".join(lines) + "\n"


class EndpointTests(TestCase):
    def setUp(self):
        self.su = User.objects.create_superuser("su_ref", "su@x.it", "pw")
        self.staff = User.objects.create_user("staff_ref", password="pw", is_staff=True)
        self.page = reverse("supervisor_algo")

    def _json(self, name, body):
        return self.client.post(reverse(name), data=json.dumps(body), content_type="application/json")

    def _upload(self, sample, text, label="Fonte A"):
        return self.client.post(reverse("supervisor_algo_reference_upload"),
                                {"sample": sample.pk, "label": label, "file": csv_file(text)})

    def test_permessi(self):
        sample = AlgoSample.objects.create(name="s", rows=[])
        ref = AlgoReference.objects.create(sample=sample, label="x")
        posts = [
            ("supervisor_algo_reference_upload", [], {"sample": sample.pk, "label": "x"}),
            ("supervisor_algo_reference_delete", [ref.pk], {}),
            ("supervisor_algo_fit", [], {}),
            ("supervisor_algo_preview", [], {}),
        ]
        for name, args, data in posts:
            url = reverse(name, args=args)
            self.assertEqual(self.client.post(url, data).status_code, 302, name)      # anonimo: al login
        self.client.force_login(self.staff)
        for name, args, data in posts:
            self.assertEqual(self.client.post(reverse(name, args=args), data).status_code, 403, name)
        self.assertTrue(AlgoReference.objects.filter(pk=ref.pk).exists())

    def test_carica_confronta_tara_ed_elimina(self):
        self.client.force_login(self.su)
        samples = []
        for k in range(3):
            sample, text = make_sample(f"Serie A, giornata {k + 1}", seed=10 + k)
            resp = self._upload(sample, text)
            self.assertRedirects(resp, f"{self.page}?campione={sample.pk}", fetch_redirect_response=False)
            samples.append(sample)
        ref = AlgoReference.objects.get(sample=samples[0])
        self.assertEqual(len(ref.matched["votes"]), len(samples[0].rows))
        self.assertEqual(ref.matched["unmatched_file"], [])

        page = self.client.get(f"{self.page}?campione={samples[0].pk}").content.decode()
        self.assertIn("Fonte A", page)
        self.assertIn("Materiale di taratura: eliminalo quando non serve più", page)
        self.assertIn('id="va-compare"', page)
        self.assertNotIn("|safe", page)

        ids = [s.pk for s in samples]
        resp = self._json("supervisor_algo_preview", {"rules": {}, "samples": ids, "reference_mode": "media"})
        data = resp.json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["references"]["matched"], sum(len(s.rows) for s in samples))
        self.assertEqual(data["references"]["sources"], ["Fonte A"])

        resp = self._json("supervisor_algo_fit", {"rules": {}, "samples": ids, "reference_mode": "Fonte A",
                                                  "groups": ["risultato", "eventi"], "lambda": 0.5})
        fit = resp.json()
        self.assertTrue(fit["ok"], fit)
        self.assertEqual(fit["validation"]["folds"], 3)
        self.assertTrue(all(p["group"] in ("risultato", "eventi") for p in fit["params"]))
        self.assertTrue(all(p["label"] for p in fit["params"]))
        self.assertFalse(AlgoSettingsVersionProbe.saved())               # niente si salva da solo

        # eliminato: sparisce dal confronto
        self.client.post(reverse("supervisor_algo_reference_delete", args=[ref.pk]))
        data = self._json("supervisor_algo_preview", {"rules": {}, "samples": [samples[0].pk]}).json()
        self.assertIsNone(data["references"])
        # eliminato il campione, i suoi riferimenti vanno con lui
        self.client.post(reverse("supervisor_algo_sample_delete", args=[samples[1].pk]))
        self.assertFalse(AlgoReference.objects.filter(sample_id=samples[1].pk).exists())

    def test_errori_chiari(self):
        self.client.force_login(self.su)
        sample, text = make_sample("Serie A, giornata 9", matches=10)
        self._upload(sample, text)
        resp = self._json("supervisor_algo_fit", {"rules": {}, "samples": [sample.pk]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("almeno 300", resp.json()["error"])
        self.assertEqual(self._json("supervisor_algo_preview", {"rules": {}, "samples": [999999]}).status_code, 400)
        self.assertEqual(self._json("supervisor_algo_fit", {"rules": {}, "samples": [sample.pk],
                                                            "groups": ["perf_baseline"]}).status_code, 400)
        self.assertEqual(self._json("supervisor_algo_fit", {"rules": {}, "samples": [sample.pk],
                                                            "lambda": 999}).status_code, 400)
        self.assertEqual(self._json("supervisor_algo_preview", {"rules": {}, "samples": [sample.pk],
                                                                "reference_ids": [424242]}).status_code, 400)
        # etichetta mancante, file sbagliato: nessun riferimento creato
        before = AlgoReference.objects.count()
        self.client.post(reverse("supervisor_algo_reference_upload"),
                         {"sample": sample.pk, "label": "", "file": csv_file(text)})
        self.client.post(reverse("supervisor_algo_reference_upload"),
                         {"sample": sample.pk, "label": "x", "file": SimpleUploadedFile("v.pdf", b"x")})
        self.assertEqual(AlgoReference.objects.count(), before)

    def test_tempi(self):
        """Anteprima con confronto sotto 1 s e taratura sotto 3 s su 3 campioni
        da circa 550 righe (margine largo: la CI è più lenta)."""
        import time
        self.client.force_login(self.su)
        ids = []
        for k in range(3):
            sample, text = make_sample(f"G{k}", matches=25, seed=20 + k)       # 25 × 22 = 550 righe
            self._upload(sample, text)
            ids.append(sample.pk)
        t = time.perf_counter()
        draft = {**{k: v for k, v in effective_algo_rules({}).items() if not k.startswith("_")}, "win": 0.4, "goal": 0.7}
        self.assertTrue(self._json("supervisor_algo_preview", {"rules": draft, "samples": ids}).json()["ok"])
        preview_s = time.perf_counter() - t
        t = time.perf_counter()
        self.assertTrue(self._json("supervisor_algo_fit", {"rules": {}, "samples": ids}).json()["ok"])
        fit_s = time.perf_counter() - t
        self.assertLess(preview_s, 2.0)
        self.assertLess(fit_s, 6.0)


class AlgoSettingsVersionProbe:
    @staticmethod
    def saved():
        from ..models import AlgoSettingsVersion
        return AlgoSettingsVersion.objects.exists()


class IsolationTests(TestCase):
    """I voti di riferimento non escono dalla pagina del Supervisor."""
    MARK = "FonteRiconoscibileXYZ"

    def test_non_compaiono_altrove(self):
        from ..models import Giornata, League, Participant, Season
        owner = User.objects.create_user("owner_iso", password="pw", is_staff=True)
        league = League.objects.create(name="Lega Iso", owner=owner)
        season = Season.objects.create(league=league, name="2026/27", is_current=True)
        Giornata.objects.create(season=season, number=1)
        team = Participant.objects.create(display_name="Iso FC", league=league, user=owner)
        sample = AlgoSample.objects.create(name="Serie A, giornata 1",
                                           rows=[{"name": "Lautaro", "team": "Inter", "role": "A", "minutes": 90}])
        AlgoReference.objects.create(sample=sample, label=self.MARK, filename=f"{self.MARK}.csv",
                                     rows=[{"name": "Lautaro", "team": "Inter", "role": "A", "vote": "9.75"}],
                                     matched={"votes": {"0": "9.75"}})
        self.client.force_login(owner)
        session = self.client.session
        session["participant_id"] = team.id
        session.save()
        q = f"?league={league.id}"
        urls = ["/", reverse("dashboard") + q, reverse("admin_giornate") + q, reverse("app_giornate") + q,
                reverse("admin_competitions") + q, reverse("app_lega") + q, reverse("app_live"),
                reverse("app_home") if _has("app_home") else "/app/", reverse("admin_export") + q,
                reverse("admin_export_csv") + q, reverse("admin_footballers"), "/api/version/"]
        for url in urls:
            resp = self.client.get(url, follow=True)
            body = resp.content.decode(errors="ignore")
            self.assertNotIn(self.MARK, body, url)
            self.assertNotIn("9.75", body, url)
            self.assertNotIn("9,75", body, url)
        self.assertFalse(PlayerPerformance.objects.exists())

    def test_solo_la_pagina_del_supervisor_li_usa(self):
        """Nessun modello di lega punta ai riferimenti e il codice che li legge
        sta solo nel servizio e nelle view del Supervisor."""
        from ..models import AlgoReference as Ref
        for rel in Ref._meta.related_objects:
            self.fail(f"{rel.related_model.__name__} punta ai voti di riferimento")
        root = Path(settings.BASE_DIR, "auctions")
        users = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                       if "AlgoReference" in p.read_text(encoding="utf-8") and "tests" not in p.parts
                       and "migrations" not in p.parts)
        self.assertEqual(users, ["models/__init__.py", "models/algo.py", "services/voto_algo.py",
                                 "views/algo_settings.py"])


def _has(name):
    from django.urls import NoReverseMatch
    try:
        reverse(name)
        return True
    except NoReverseMatch:
        return False
