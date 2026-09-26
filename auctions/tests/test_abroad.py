"""Giocatori usciti dalla Serie A (5.05 – 5.09) e integrazioni esterne."""
import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import ContractEvent, League, Participant, Player
from ..providers import apifootball, uefa
from ..providers.importers import sync_players
from ..services import abroad, contracts


class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class CompensationTableTests(TestCase):
    def test_uefa_and_fifa_brackets(self):
        self.assertEqual(abroad.compensation("A", "uefa", 3), 500)
        self.assertEqual(abroad.compensation("C", "uefa", 12), 200)
        self.assertEqual(abroad.compensation("D", "uefa", 77), 20)
        self.assertEqual(abroad.compensation("A", "uefa", 150), 20)   # oltre le prime 100: soglia minima
        self.assertEqual(abroad.compensation("A", "uefa", None), 20)
        self.assertEqual(abroad.compensation("P", "fifa", 11), 20)    # es. MLS = Stati Uniti
        self.assertEqual(abroad.compensation("A", "free", 1), 0)


class DepartureFlowTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="L", contracts_enabled=True, salary_cap_enabled=True)
        self.team = Participant.objects.create(display_name="A", league=self.league, credits=Decimal("1000"))
        self.p = Player.objects.create(name="Vlahovic", role="A", team="JUV", league=self.league,
                                       owner=self.team, cost=Decimal("80"), contract_years=2)

    def _listone(self, names):
        return [{"name": n, "role": "A", "team": "JUV", "price": 10} for n in names]

    def test_listone_import_flags_missing_owned_players(self):
        Player.objects.create(name="Kean", role="A", team="FIO", league=self.league, owner=self.team)
        report = sync_players(self._listone(["Kean"]), league=self.league)
        self.assertEqual(report["flagged_left_serie_a"], 1)
        self.p.refresh_from_db()
        self.assertIsNotNone(self.p.left_serie_a_at)

    def test_sold_to_uefa_club_pays_and_counts_as_lost(self):
        abroad.store_uefa_ranking([("Real Madrid", 1, "ESP"), ("Galatasaray", 25, "TUR")])
        res = abroad.resolve(self.p.id, "uefa", club="Galatasaray SK")
        self.assertEqual(res["amount"], 150)
        self.p.refresh_from_db()
        self.team.refresh_from_db()
        self.assertIsNone(self.p.owner)
        self.assertEqual(self.team.credits, 1150)
        self.assertTrue(ContractEvent.objects.filter(kind="left", participant=self.team).exists())

    def test_retired_gets_nothing(self):
        self.assertEqual(abroad.resolve(self.p.id, "free")["amount"], 0)

    def test_list_keeps_player_out_of_slots_until_contract_ends(self):
        from ..services.state import roster_plan
        abroad.resolve(self.p.id, "uefa", club="X", position=3)   # venduto: +500
        q = Player.objects.create(name="Lukaku", role="A", league=self.league, owner=self.team, contract_years=1)
        res = abroad.resolve(q.id, "list", club="Al Hilal", position=None)
        self.assertEqual(res["deferred"], 20)
        self.assertEqual(roster_plan(self.team)["owned"], 0)
        self.assertEqual(contracts.expiring(self.team), [])
        contracts.new_season(self.league.id)   # contratto a 0: perso con compenso
        q.refresh_from_db()
        self.team.refresh_from_db()
        self.assertIsNone(q.owner)
        self.assertEqual(self.team.credits, 1000 + 500 + 20)

    def test_list_is_limited_to_three(self):
        for i in range(3):
            pl = Player.objects.create(name=f"L{i}", role="C", league=self.league, owner=self.team)
            self.assertTrue(abroad.resolve(pl.id, "list")["ok"])
        self.assertFalse(abroad.resolve(self.p.id, "list")["ok"])

    def test_detect_with_api_and_uefa_ranking(self):
        abroad.store_uefa_ranking([("Fenerbahce", 40, "TUR")])
        res = abroad.detect(self.p.id, finder=lambda name, team: {"club": "Fenerbahçe SK", "date": "2026-08-01"})
        self.assertEqual(res["position"], 40)
        self.p.refresh_from_db()
        self.assertEqual((self.p.left_rank_kind, self.p.left_rank_pos), ("uefa", 40))

    def test_admin_actions(self):
        admin = User.objects.create_user("adm", password="pw")
        self.league.owner = admin
        self.league.save()
        self.client.force_login(admin)
        Player.objects.filter(pk=self.p.pk).update(left_serie_a_at="2026-09-01T00:00Z")
        page = self.client.get(reverse("admin_contracts") + f"?league={self.league.id}")
        self.assertContains(page, "Vlahovic")
        self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "uefa_paste", "ranking": "1;Real Madrid\n33;Benfica"})
        self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "left_resolve", "player_id": self.p.id,
            "outcome": "uefa", "club": "Benfica"})
        self.team.refresh_from_db()
        self.assertEqual(self.team.credits, 1080)   # 31°-50° attaccante = 80

    def test_app_list_release_only_during_auction(self):
        abroad.resolve(self.p.id, "list", club="X", position=1)
        s = self.client.session
        s["participant_id"] = self.team.id
        s.save()
        resp = self.client.post(reverse("app_list_release", args=[self.p.id]), follow=True)
        self.assertContains(resp, "solo in sede d")
        self.p.refresh_from_db()
        self.assertTrue(self.p.abroad_list)


class ProviderTests(TestCase):
    def test_apifootball_finds_last_transfer(self):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params))
            if url.endswith("/players"):
                return Resp({"response": [{"player": {"id": 7}, "statistics": [{"team": {"name": "Juventus"}}]}]})
            return Resp({"response": [{"transfers": [
                {"date": "2024-07-01", "teams": {"in": {"name": "Juventus"}}},
                {"date": "2026-08-20", "teams": {"in": {"name": "Galatasaray"}}}]}]})

        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test"}):
            found = apifootball.find_destination("Dusan Vlahovic", "JUV", get=fake_get)
        self.assertEqual(found["club"], "Galatasaray")
        self.assertEqual(calls[0][1]["league"], 135)

    def test_apifootball_without_key(self):
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": ""}):
            self.assertIsNone(apifootball.find_destination("Vlahovic", get=lambda *a, **k: 1 / 0))

    def test_uefa_json_walk_and_paste(self):
        data = {"data": {"members": [
            {"position": 2, "member": {"displayName": "Bayern München", "countryName": "Germany"}},
            {"position": 1, "member": {"displayName": "Real Madrid", "countryName": "Spain"}}]}}
        rows = uefa.fetch_club_ranking(get=lambda *a, **k: Resp(data))
        self.assertEqual(rows[0][:2], ("Real Madrid", 1))
        self.assertEqual(uefa.parse_pasted("3;Inter\nBenfica 12\n"), [("Inter", 3, ""), ("Benfica", 12, "")])
