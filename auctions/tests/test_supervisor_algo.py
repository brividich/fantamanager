"""Supervisor → Voto algoritmico: parametri di piattaforma con anteprima."""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ..models import AlgoSample, AlgoSettingsVersion
from ..services import voto_algo
from ..voto_algoritmico import ALGO_DEFAULTS, effective_algo_rules, player_vote


def full_rules(**changes):
    rules = {k: v for k, v in effective_algo_rules({}).items() if not k.startswith("_")}
    for path, value in changes.items():
        keys = path.split("__")
        target = rules
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return rules


class LivelliTests(SimpleTestCase):
    def test_diff_dai_default_solo_le_voci_cambiate(self):
        diff = voto_algo.diff_from_defaults(full_rules(win=0.5, perf_weights__A__shots_on=0.2))
        self.assertEqual(diff, {"win": 0.5, "perf_weights": {"A": {"shots_on": 0.2}}})

    def test_diff_vuoto_se_tutto_di_partenza(self):
        self.assertEqual(voto_algo.diff_from_defaults(full_rules()), {})

    def test_merge_fonde_le_tabelle_per_ruolo(self):
        merged = voto_algo.merge_overrides(
            {"win": 0.5, "perf_weights": {"A": {"shots_on": 0.2}}},
            {"loss": -0.5, "perf_weights": {"A": {"key_passes": 0.3}, "D": {"tackles": 0.1}}})
        self.assertEqual(merged["perf_weights"], {"A": {"shots_on": 0.2, "key_passes": 0.3},
                                                  "D": {"tackles": 0.1}})
        self.assertEqual((merged["win"], merged["loss"]), (0.5, -0.5))

    def test_validazione(self):
        _full, errors = voto_algo.clean_rules({"win": "abc", "min_vote": 8, "max_vote": 7,
                                               "step": 0.3, "perf_weights": {"A": {"shots_on": 3}}})
        self.assertIn("win", errors)
        self.assertIn("max_vote", errors)
        self.assertIn("step", errors)
        self.assertIn("perf_weights.A.shots_on", errors)

    def test_validazione_ignora_chiavi_sconosciute(self):
        full, errors = voto_algo.clean_rules({"hack": 1, "perf_weights": {"X": {"y": 1}}})
        self.assertEqual(errors, {})
        self.assertNotIn("hack", full)
        self.assertNotIn("X", full["perf_weights"])

    def test_campione_simulato_deterministico(self):
        self.assertEqual(voto_algo.simulated_rows()[:20], voto_algo.simulated_rows()[:20])

    def test_anteprima_senza_modifiche(self):
        rows = voto_algo.simulated_rows(matches=10)
        res = voto_algo.preview(rows, {}, {})
        self.assertEqual(res["summary"]["changed"], 0)
        self.assertEqual(res["per_field"], [])

    def test_anteprima_misura_lo_scostamento(self):
        rows = voto_algo.simulated_rows(matches=20)
        res = voto_algo.preview(rows, {}, {"win": 0.75})
        self.assertGreater(res["summary"]["changed"], 0)
        self.assertGreater(res["summary"]["mean_delta"], 0)
        self.assertEqual(res["summary"]["down"], 0)
        [field] = res["per_field"]
        self.assertEqual((field["path"], field["label"], field["to"]), ("win", "Vittoria", 0.75))
        self.assertEqual(field["changed"], res["summary"]["changed"])
        tot = res["by_role"][-1]
        self.assertGreater(tot["after"]["mean"], tot["before"]["mean"])
        json.dumps(res)   # serializzabile

    def test_anteprima_con_riferimento(self):
        rows = [{"role": "A", "minutes": 90, "team_goals_for": 1, "team_goals_against": 0,
                 "goals": 1, "ref_vote": 7.5} for _ in range(5)]
        res = voto_algo.preview(rows, {}, {"goal": 1.0})
        self.assertLess(res["reference"]["after"]["mae"], res["reference"]["before"]["mae"])


class PaginaTests(TestCase):
    def setUp(self):
        self.su = User.objects.create_superuser("su_algo", "su@x.it", "pw")
        self.staff = User.objects.create_user("staff_algo", password="pw", is_staff=True)
        self.url = reverse("supervisor_algo")

    def _post_json(self, name, body):
        return self.client.post(reverse(name), data=json.dumps(body), content_type="application/json")

    def test_solo_superuser(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self._post_json("supervisor_algo_preview", {"rules": {}}).status_code, 403)
        self.assertEqual(self.client.post(reverse("supervisor_algo_save"), {"rules": "{}"}).status_code, 403)
        self.assertFalse(AlgoSettingsVersion.objects.exists())

    def test_pagina(self):
        self.client.force_login(self.su)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for needle in ('id="va-saved"', 'id="va-defaults"', 'data-path="win"',
                       'data-path="perf_weights.A.shots_on"', "Stagione simulata"):
            self.assertIn(needle, html)
        self.assertNotIn("|safe", html)

    def test_anteprima(self):
        self.client.force_login(self.su)
        resp = self._post_json("supervisor_algo_preview", {"rules": full_rules(win=0.6), "sample": "sim"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertGreater(data["summary"]["changed"], 0)

    def test_anteprima_con_errori(self):
        self.client.force_login(self.su)
        resp = self._post_json("supervisor_algo_preview", {"rules": full_rules(min_vote=9)})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("max_vote", resp.json()["errors"])

    def test_anteprima_rifiuta_corpi_non_validi(self):
        self.client.force_login(self.su)
        resp = self.client.post(reverse("supervisor_algo_preview"), data="non json",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_salvataggio_e_uso(self):
        self.client.force_login(self.su)
        self.client.post(reverse("supervisor_algo_save"),
                         {"rules": json.dumps(full_rules(win=0.5)), "note": "più peso al risultato"})
        v = AlgoSettingsVersion.objects.get()
        self.assertEqual((v.rules, v.note, v.created_by), ({"win": 0.5}, "più peso al risultato", self.su))
        self.assertEqual(voto_algo.platform_rules()["win"], 0.5)
        # stessi valori: nessuna nuova versione
        self.client.post(reverse("supervisor_algo_save"), {"rules": json.dumps(full_rules(win=0.5))})
        self.assertEqual(AlgoSettingsVersion.objects.count(), 1)

    def test_salvataggio_rifiuta_valori_fuori_range(self):
        self.client.force_login(self.su)
        self.client.post(reverse("supervisor_algo_save"), {"rules": json.dumps(full_rules(base=12))})
        self.assertFalse(AlgoSettingsVersion.objects.exists())

    def test_la_lega_ritocca_sopra_la_piattaforma(self):
        AlgoSettingsVersion.objects.create(rules={"win": 0.5, "perf_weights": {"A": {"shots_on": 0.2}}})
        rules = voto_algo.rules_for_league({"loss": -0.5, "perf_weights": {"A": {"key_passes": 0.3}}})
        self.assertEqual((rules["win"], rules["loss"]), (0.5, -0.5))
        self.assertEqual(rules["perf_weights"]["A"]["shots_on"], 0.2)
        self.assertEqual(rules["perf_weights"]["A"]["key_passes"], 0.3)
        self.assertEqual(rules["perf_weights"]["D"], ALGO_DEFAULTS["perf_weights"]["D"])
        res = player_vote({"minutes": 90, "team_goals_for": 1, "team_goals_against": 0}, "C", rules)
        self.assertEqual(res["vote"], Decimal("6.5"))

    def test_ripristino_e_valori_di_partenza(self):
        self.client.force_login(self.su)
        old = AlgoSettingsVersion.objects.create(rules={"win": 0.5})
        AlgoSettingsVersion.objects.create(rules={"win": 0.75})
        self.client.post(reverse("supervisor_algo_restore", args=[old.pk]))
        self.assertEqual(voto_algo.platform_overrides(), {"win": 0.5})
        self.assertEqual(AlgoSettingsVersion.objects.count(), 3)
        self.client.post(reverse("supervisor_algo_defaults"))
        self.assertEqual(voto_algo.platform_overrides(), {})
        self.assertEqual(AlgoSettingsVersion.objects.count(), 4)

    def test_calibrazione(self):
        self.client.force_login(self.su)
        resp = self._post_json("supervisor_algo_calibrate",
                               {"rules": full_rules(), "sample": "sim", "mean": 6.0, "sd": 0.6})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(0.2 <= data["calib_scale"] <= 3)
        # applicata, la media sul campione va dove chiesto
        rows = voto_algo.simulated_rows()
        res = voto_algo.preview(rows, {}, {"calib_scale": data["calib_scale"],
                                           "calib_shift": data["calib_shift"]})
        self.assertAlmostEqual(res["by_role"][-1]["after"]["mean"], 6.0, delta=0.08)

    def test_calibrazione_rifiuta_obiettivi_assurdi(self):
        self.client.force_login(self.su)
        resp = self._post_json("supervisor_algo_calibrate", {"rules": full_rules(), "mean": 9, "sd": 0.6})
        self.assertEqual(resp.status_code, 400)

    def test_import_campione_da_api_football(self):
        self.client.force_login(self.su)
        rows = [{"api_id": 1, "name": "Lautaro", "team": "Inter", "role": "A", "vote": "7.3",
                 "minutes": 90, "goals": 1, "team_goals_for": 2, "team_goals_against": 0},
                {"api_id": 2, "name": "Panchina", "team": "Inter", "role": "D", "vote": None, "minutes": 0}]
        with mock.patch("auctions.providers.apifootball.matchday_live_rows", return_value=rows), \
                mock.patch("auctions.views.algo_settings.apifootball_configured", return_value=True):
            resp = self.client.post(reverse("supervisor_algo_sample_import"), {"giornata": 5})
        sample = AlgoSample.objects.get()
        self.assertEqual(sample.name, "Serie A, giornata 5")
        self.assertEqual(len(sample.rows), 1)                       # senza minuti: scartato
        self.assertEqual(sample.rows[0]["ref_vote"], 7.3)
        self.assertNotIn("vote", sample.rows[0])
        self.assertRedirects(resp, f"{self.url}?campione={sample.pk}", fetch_redirect_response=False)
        page = self.client.get(f"{self.url}?campione={sample.pk}").content.decode()
        self.assertIn("Serie A, giornata 5", page)

    def test_import_senza_chiave(self):
        self.client.force_login(self.su)
        with mock.patch("auctions.views.algo_settings.apifootball_configured", return_value=False):
            self.client.post(reverse("supervisor_algo_sample_import"), {"giornata": 5})
        self.assertFalse(AlgoSample.objects.exists())

    def test_elimina_campione(self):
        self.client.force_login(self.su)
        s = AlgoSample.objects.create(name="x", rows=[])
        self.client.post(reverse("supervisor_algo_sample_delete", args=[s.pk]))
        self.assertFalse(AlgoSample.objects.exists())

    def test_voce_nel_menu_della_console(self):
        self.client.force_login(self.su)
        html = self.client.get(reverse("admin_footballers")).content.decode()
        self.assertIn(self.url, html)
