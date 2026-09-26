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

    def test_manual_flag_visible_with_contracts_disabled(self):
        admin = User.objects.create_user("adm", password="pw")
        self.league.owner = admin
        self.league.contracts_enabled = False
        self.league.save()
        self.client.force_login(admin)
        url = reverse("admin_contracts") + f"?league={self.league.id}"
        page = self.client.get(url)
        self.assertContains(page, "Segnala uscita")
        self.assertContains(page, "API-Football")
        self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "left_flag", "player_id": self.p.id})
        self.p.refresh_from_db()
        self.assertIsNotNone(self.p.left_serie_a_at)
        self.assertContains(self.client.get(url), "Rileva")

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


class UefaRankingTests(TestCase):
    """Download del ranking UEFA: forme della risposta, errori, incolla a mano."""

    # La forma usata da uefa.com: la posizione sta in "overallRanking", e le
    # classifiche delle singole stagioni hanno posizioni loro che non contano.
    UEFA_SHAPE = {"data": {"members": [
        {"member": {"displayName": "Bayern München", "countryName": "Germany"},
         "overallRanking": {"position": 2, "totalValue": 120.5},
         "seasonRankings": [{"seasonYear": 2025, "position": 7}]},
        {"member": {"internationalName": "Real Madrid", "countryCode": "ESP"},
         "overallRanking": {"position": 1, "totalValue": 130.0},
         "seasonRankings": [{"seasonYear": 2025, "position": 1}]},
    ]}}

    def test_position_nested_in_overall_ranking(self):
        rows, reason = uefa.fetch(get=lambda *a, **k: Resp(self.UEFA_SHAPE), year=2027)
        self.assertEqual(reason, "")
        self.assertEqual([r[:2] for r in rows], [("Real Madrid", 1), ("Bayern München", 2)])
        self.assertEqual(rows[1][2], "Germany")

    def test_asks_like_a_browser_from_uefa_com(self):
        seen = {}

        def get(url, **kw):
            seen.update(kw)
            return Resp(self.UEFA_SHAPE)
        uefa.fetch(get=get, year=2027)
        self.assertIn("Mozilla", seen["headers"]["User-Agent"])
        self.assertEqual(seen["headers"]["Referer"], "https://www.uefa.com/")
        self.assertEqual(seen["params"]["seasonYear"], 2027)

    def test_refused_request_explains_why(self):
        import requests

        class Refused(Resp):
            status_code = 403

            def raise_for_status(self):
                raise requests.HTTPError("403", response=self)
        rows, reason = uefa.fetch(get=lambda *a, **k: Refused({}))
        self.assertEqual(rows, [])
        self.assertIn("rifiutato", reason)

    def test_unreachable_site_explains_why(self):
        import requests

        def get(*a, **k):
            raise requests.ConnectionError("no route")
        rows, reason = uefa.fetch(get=get)
        self.assertEqual(rows, [])
        self.assertIn("non è raggiungibile", reason)

    def test_falls_back_to_previous_season_when_current_is_empty(self):
        years = []

        def get(url, params=None, **kw):
            years.append(params["seasonYear"])
            return Resp({"data": {"members": []}} if len(years) == 1 else self.UEFA_SHAPE)
        with mock.patch.object(uefa, "_season_year", return_value=2027):
            rows, reason = uefa.fetch(get=get)
        self.assertEqual(years, [2027, 2026])
        self.assertEqual(len(rows), 2)

    def test_pages_until_the_ranking_ends(self):
        def page(n, count):
            start = (n - 1) * uefa.PAGE_SIZE
            return {"data": {"members": [
                {"member": {"displayName": f"Club {start + i}"}, "overallRanking": {"position": start + i + 1}}
                for i in range(count)]}}
        pages = []

        def get(url, params=None, **kw):
            pages.append(params["page"])
            return Resp(page(params["page"], uefa.PAGE_SIZE if params["page"] == 1 else 30))
        rows, _ = uefa.fetch(get=get, year=2027)
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(rows), uefa.PAGE_SIZE + 30)
        self.assertEqual(rows[-1][1], uefa.PAGE_SIZE + 30)

    def test_paste_the_table_copied_from_uefa_com(self):
        tabbed = "Pos\tClub\tAssociation\tPoints\n1\tReal Madrid\tESP\t143.500\n2\tBayern München\tGER\t136.250\n"
        self.assertEqual(uefa.parse_pasted(tabbed), [("Real Madrid", 1, ""), ("Bayern München", 2, "")])
        one_cell_per_line = "1\nReal Madrid\nESP\n143.500\n2\nInter\nITA\n116.250\n"
        self.assertEqual(uefa.parse_pasted(one_cell_per_line), [("Real Madrid", 1, ""), ("Inter", 2, "")])
        spaced = "3. Manchester City ENG 120.000\n12° Benfica POR 80,500\n"
        self.assertEqual(uefa.parse_pasted(spaced), [("Manchester City", 3, ""), ("Benfica", 12, "")])

    def test_failed_download_message_names_the_reason(self):
        league = League.objects.create(name="Lega", budget=Decimal("500"))
        admin = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.client.force_login(admin)
        with mock.patch.object(uefa, "fetch", return_value=([], "uefa.com ha rifiutato la richiesta (403)")):
            resp = self.client.post(reverse("admin_contracts_action"),
                                    {"league_id": league.id, "action": "uefa_fetch"}, follow=True)
        body = resp.content.decode()
        self.assertIn("rifiutato la richiesta (403)", body)
        self.assertIn(uefa.RANKING_PAGE, body)


class ApiFootballLookupTests(TestCase):
    """Ricerca della destinazione: stagione in Serie A, ripiego sull'anagrafica, errori."""

    def setUp(self):
        apifootball._refused_seasons.clear()
        self.addCleanup(apifootball._refused_seasons.clear)
        patcher = mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test", "APIFOOTBALL_SEASON": "2026"})
        patcher.start()
        self.addCleanup(patcher.stop)

    GALATASARAY = {"response": [{"transfers": [
        {"date": "2022-07-01", "teams": {"in": {"name": "Juventus"}, "out": {"name": "Fiorentina"}}},
        {"date": "2026-08-20", "teams": {"in": {"name": "Galatasaray"}, "out": {"name": "Juventus"}}}]}]}

    def test_search_term_from_listone_names(self):
        self.assertEqual(apifootball._search_term("Martinez L."), ("Martinez", "L"))
        self.assertEqual(apifootball._search_term("De Ketelaere"), ("Ketelaere", ""))
        self.assertEqual(apifootball._search_term("Esposito F.P."), ("Esposito", "F"))
        self.assertEqual(apifootball._search_term("Dusan Vlahovic"), ("Vlahovic", ""))
        self.assertEqual(apifootball._search_term("Pio")[0], "")

    def test_profiles_check_the_club_in_the_transfers(self):
        def get(url, params=None, **kw):
            if url.endswith("/players"):
                return Resp({"response": []})
            if url.endswith("/profiles"):
                return Resp({"response": [
                    {"player": {"id": 2, "firstname": "Marko", "lastname": "Vlahovic", "position": "Attacker"}},
                    {"player": {"id": 9, "firstname": "Dusan", "lastname": "Vlahovic", "position": "Attacker"}}]})
            if params == {"player": 2}:
                return Resp({"response": [{"transfers": [
                    {"date": "2025-07-01", "teams": {"in": {"name": "Partizan"}, "out": {"name": "OFK"}}}]}]})
            return Resp(self.GALATASARAY)

        # Senza iniziale si prova prima Marko: i suoi trasferimenti non passano dalla Juventus.
        found, _ = apifootball.lookup("Vlahovic", "Juventus", "A", get=get)
        self.assertEqual(found["club"], "Galatasaray")
        found, reason = apifootball.lookup("Vlahovic", "Roma", "A", get=get)
        self.assertIsNone(found)
        self.assertIn("non trovato", reason)

    def test_same_club_by_name_or_code(self):
        self.assertTrue(apifootball.same_club("AC Milan", "Milan"))
        self.assertTrue(apifootball.same_club("Hellas Verona", "Verona"))
        self.assertTrue(apifootball.same_club("Juventus", "JUV"))
        self.assertFalse(apifootball.same_club("Internacional", "Inter"))

    def test_free_plan_falls_back_to_player_profiles(self):
        calls = []

        def get(url, params=None, **kw):
            calls.append((url.rsplit("/", 1)[-1], dict(params)))
            if url.endswith("/players"):
                return Resp({"errors": {"plan": "Free plans do not have access to this season, try from 2021 to 2023."}})
            if url.endswith("/profiles"):
                return Resp({"response": [
                    {"player": {"id": 1, "name": "A. Vlahovic", "firstname": "Andrea", "lastname": "Vlahovic",
                                "position": "Defender"}},
                    {"player": {"id": 2, "name": "M. Vlahovic", "firstname": "Marko", "lastname": "Vlahovic",
                                "position": "Attacker"}},
                    {"player": {"id": 9, "name": "D. Vlahović", "firstname": "Dušan", "lastname": "Vlahović",
                                "position": "Attacker"}}]})
            if params == {"player": 2}:
                return Resp({"response": [{"transfers": [
                    {"date": "2025-07-01", "teams": {"in": {"name": "Partizan"}, "out": {"name": "OFK"}}}]}]})
            return Resp(self.GALATASARAY)

        found, reason = apifootball.lookup("Vlahovic D.", "Juventus", "A", get=get)
        self.assertEqual((found["club"], reason), ("Galatasaray", ""))
        # Il difensore omonimo è scartato dal ruolo; si prova per primo chi ha l'iniziale giusta.
        self.assertEqual([p for u, p in calls if u == "transfers"], [{"player": 9}])

        # Le stagioni rifiutate non si richiedono più.
        calls.clear()
        apifootball.lookup("Vlahovic D.", "Juventus", "A", get=get)
        self.assertNotIn("players", [u for u, _ in calls])

    def test_player_still_at_his_club_is_not_a_departure(self):
        def get(url, params=None, **kw):
            if url.endswith("/players"):
                return Resp({"response": [{"player": {"id": 7}, "statistics": [{"team": {"name": "Juventus"}}]}]})
            return Resp({"response": [{"transfers": [
                {"date": "2024-07-01", "teams": {"in": {"name": "Juventus"}, "out": {"name": "Fiorentina"}}}]}]})

        found, reason = apifootball.lookup("Vlahovic", "Juventus", "A", get=get)
        self.assertIsNone(found)
        self.assertIn("Juventus", reason)

    def test_bad_key_and_quota_stop_everything(self):
        for errors in ({"token": "Error/Missing application key."}, {"requests": "You have reached the limit."}):
            with self.assertRaises(apifootball.ApiFootballError):
                apifootball.lookup("Vlahovic", "JUV", get=lambda *a, **k: Resp({"errors": errors}))

    def test_unreachable_api_stops_everything(self):
        import requests

        def get(*a, **k):
            raise requests.ConnectionError("no route")
        with self.assertRaises(apifootball.ApiFootballError):
            apifootball.lookup("Vlahovic", "JUV", get=get)
        self.assertIsNone(apifootball.find_destination("Vlahovic", "JUV", get=get))


class AutoDetectTests(TestCase):
    """Dopo l'import del listone la destinazione si cerca da sola."""

    def setUp(self):
        self.admin = User.objects.create_user("adm", password="pw")
        self.league = League.objects.create(name="L", owner=self.admin)
        self.team = Participant.objects.create(display_name="A", league=self.league, credits=Decimal("1000"))
        self.gone = Player.objects.create(name="Vlahovic", role="A", team="JUV", league=self.league,
                                          owner=self.team, cost=Decimal("80"))
        self.kept = Player.objects.create(name="Kean", role="A", team="FIO", league=self.league, owner=self.team)
        patcher = mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ranking(self):
        return [("Real Madrid", 1, "ESP"), ("Galatasaray", 25, "TUR")], ""

    def test_detect_all_fills_club_and_downloads_the_ranking(self):
        abroad.flag_player(self.gone.id)
        report = abroad.detect_all(self.league, finder=lambda n, t: {"club": "Galatasaray SK"},
                                   fetcher=self._ranking)
        self.assertEqual([r["player_name"] for r in report["found"]], ["Vlahovic"])
        self.gone.refresh_from_db()
        self.assertEqual((self.gone.left_club, self.gone.left_rank_kind, self.gone.left_rank_pos),
                         ("Galatasaray SK", "uefa", 25))
        self.assertIn("ranking UEFA 25", abroad.detect_summary(report))

    def test_detect_all_stops_when_the_api_is_down(self):
        from ..providers.apifootball import ApiFootballError
        abroad.flag_player(self.gone.id)
        abroad.flag_player(self.kept.id)
        calls = []

        def finder(name, team):
            calls.append(name)
            raise ApiFootballError("limite di richieste API-Football raggiunto")
        report = abroad.detect_all(self.league, finder=finder, fetcher=self._ranking)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["left"], 2)
        self.assertIn("limite", abroad.detect_summary(report))

    def test_without_key_nothing_is_called(self):
        abroad.flag_player(self.gone.id)
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": ""}):
            report = abroad.detect_all(self.league, fetcher=lambda: 1 / 0)
        self.assertIn("APIFOOTBALL_KEY", report["error"])

    def test_listone_import_detects_departures(self):
        import io
        self.client.force_login(self.admin)
        csv = io.BytesIO("Nome;Ruolo;Squadra;Quotazione\nKean;A;Fiorentina;20\n".encode())
        csv.name = "listone.csv"
        with mock.patch("auctions.providers.apifootball.lookup",
                        return_value=({"club": "Galatasaray", "date": "2026-08-20"}, "")), \
                mock.patch("auctions.providers.uefa.fetch", side_effect=lambda: self._ranking()):
            resp = self.client.post(reverse("admin_import_players"),
                                    {"league_id": self.league.id, "csv_file": csv, "prune": "0"})
        data = resp.json()
        self.assertEqual(data["left_serie_a"]["flagged"], 1)
        self.assertEqual(data["left_serie_a"]["found"], 1)
        self.assertIn(reverse("admin_contracts"), data["left_serie_a"]["url"])
        self.gone.refresh_from_db()
        self.assertEqual((self.gone.left_club, self.gone.left_rank_pos), ("Galatasaray", 25))

    def test_contracts_page_detect_all_and_regia_todo(self):
        from ..views.app_admin import league_admin_digest
        abroad.flag_player(self.gone.id)
        self.assertTrue(any("fuori dal listone" in t["title"] for t in league_admin_digest(self.league)))
        self.client.force_login(self.admin)
        url = reverse("admin_contracts") + f"?league={self.league.id}"
        self.assertContains(self.client.get(url), "Rileva tutti (1)")
        with mock.patch("auctions.providers.apifootball.lookup", return_value=(None, "giocatore non trovato")):
            resp = self.client.post(reverse("admin_contracts_action"),
                                    {"league_id": self.league.id, "action": "left_detect_all"}, follow=True)
        self.assertContains(resp, "non trovati: Vlahovic")
