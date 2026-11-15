"""Squadre doppie: si riconoscono, si uniscono (services/teams.py) e l'import
delle rose non le crea più per un nome scritto in un altro modo."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Fixture, Giornata, GiornataScore, League, Participant, Player, RosterLog, Season
from ..providers.importers import import_rose_data
from ..services import teams


class MergeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_merge", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lega Doppie", owner=self.owner)
        self.coach = User.objects.create_user("spartak_noce", password="pw")
        # Quella con l'account, vuota; e quella dell'import, con la rosa.
        self.keep = Participant.objects.create(display_name="SPARTAK NOCE", league=self.league,
                                               user=self.coach, credits=Decimal("500"))
        self.dup = Participant.objects.create(display_name="Spartak Noce", league=self.league,
                                              credits=Decimal("2900"), spent_credits=Decimal("840"))
        self.other = Participant.objects.create(display_name="GELSI UNITED", league=self.league)
        for i in range(3):
            Player.objects.create(name=f"Gioc {i}", role="C", league=self.league, owner=self.dup,
                                  cost=Decimal("280"), contract_years=2)
        RosterLog.objects.create(participant=self.dup, participant_name="Spartak Noce", player_name="Gioc 0",
                                 action=RosterLog.Action.ASSIGN, credits_delta=Decimal("280"))

    def test_duplicates_are_spotted(self):
        pairs = teams.likely_duplicates(Participant.objects.filter(league=self.league))
        self.assertEqual([{a.pk, b.pk} for a, b in pairs], [{self.keep.pk, self.dup.pk}])
        a = Participant(display_name="SLAVIA VIA FONDA")
        b = Participant(display_name="Slavia Viafonda")
        a.pk, b.pk = 1, 2
        self.assertEqual(len(teams.likely_duplicates([a, b])), 1)

    def test_merge_moves_roster_account_and_keeps_the_chosen_budget(self):
        info = teams.merge_teams(self.dup, self.keep, budget="dup")
        self.assertEqual(info["players"], 3)
        self.keep.refresh_from_db()
        self.assertFalse(Participant.objects.filter(pk=self.dup.pk).exists())
        self.assertEqual(Player.objects.filter(owner=self.keep).count(), 3)
        self.assertEqual(Player.objects.get(name="Gioc 1").contract_years, 2)
        self.assertEqual(self.keep.user, self.coach)
        self.assertEqual((self.keep.spent_credits, self.keep.remaining_credits), (Decimal("840"), Decimal("2060")))
        self.assertTrue(RosterLog.objects.filter(participant=self.keep, player_name="Gioc 0").exists())

    def test_the_account_of_the_duplicate_comes_along(self):
        # Al contrario: l'account sulla doppia, la rosa su quella che resta.
        self.keep.user = None
        self.keep.save()
        self.dup.user = self.coach
        self.dup.save()
        teams.merge_teams(self.dup, self.keep)
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.user, self.coach)
        self.assertEqual(self.keep.remaining_credits, Decimal("500"))     # budget di chi resta

    def test_a_team_that_already_played_is_not_merged(self):
        season = Season.objects.create(league=self.league, name="2027/28", is_current=True)
        g = Giornata.objects.create(season=season, number=1)
        GiornataScore.objects.create(giornata=g, participant=self.dup, total=Decimal("70"))
        with self.assertRaises(teams.TeamsNotMergeable):
            teams.merge_teams(self.dup, self.keep)
        self.assertTrue(Participant.objects.filter(pk=self.dup.pk).exists())

    def test_sealed_bids_in_the_same_round_block_the_merge(self):
        from ..models import SealedBid
        from .common import make_live_auction
        auction = make_live_auction(league=self.league)
        auction.status = auction.Status.CLOSED                    # asta finita: resta solo il conflitto delle buste
        auction.save(update_fields=["status"])
        for team in (self.keep, self.dup):
            SealedBid.objects.create(auction=auction, participant=team, cycle=1, round=1, amount=Decimal("10"))
        self.assertTrue(any("busta segreta" in p for p in teams.merge_problems(self.dup, self.keep)))
        with self.assertRaises(teams.TeamsNotMergeable):
            teams.merge_teams(self.dup, self.keep)

    def test_rankings_and_cap_follow_the_team_that_stays(self):
        from ..models import CapEntry, CapPhase, LeagueRanking
        from ..services import salary
        self.league.salary_cap_enabled = True
        self.league.save()
        prev = salary.save_ranking(self.league, self.league.season_number - 1, LeagueRanking.Kind.FINAL,
                                   [self.other.id, self.dup.id])           # la classifica conosce solo la doppia
        mid = salary.save_ranking(self.league, self.league.season_number, LeagueRanking.Kind.MIDSEASON,
                                  [self.keep.id, self.other.id, self.dup.id])   # tutte e due
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, prev)
        teams.merge_teams(self.dup, self.keep)
        prev.refresh_from_db(); mid.refresh_from_db()
        self.assertEqual(prev.order, [self.other.id, self.keep.id])
        self.assertEqual(mid.order, [self.keep.id, self.other.id])
        self.assertEqual(CapEntry.objects.filter(participant=self.keep, kind=CapEntry.Kind.BASE).count(), 1)

    def test_not_while_an_auction_is_running(self):
        from .common import make_live_auction
        make_live_auction(league=self.league)
        self.assertTrue(any("asta in corso" in p for p in teams.merge_problems(self.dup, self.keep)))

    def test_three_accounts_do_not_fit(self):
        self.keep.co_user = User.objects.create_user("vice1", password="pw")
        self.keep.save()
        self.dup.user = User.objects.create_user("altro", password="pw")
        self.dup.save()
        self.assertTrue(any("più di due account" in p for p in teams.merge_problems(self.dup, self.keep)))

    def test_from_the_teams_page(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("admin_participants"), {"league": self.league.id})
        self.assertContains(page, "doppia?")
        self.assertContains(page, "Unisci a un'altra squadra")
        app = self.client.get(reverse("app_regia_teams"), {"league": self.league.id})
        self.assertContains(app, "Unisci a un'altra squadra")
        url = reverse("admin_merge_participant", args=[self.dup.id])
        self.client.post(url, {"target_id": self.keep.id, "budget": "dup"})           # senza conferma
        self.assertTrue(Participant.objects.filter(pk=self.dup.pk).exists())
        self.client.post(url, {"target_id": self.keep.id, "budget": "dup", "confirm": "1"})
        self.assertFalse(Participant.objects.filter(pk=self.dup.pk).exists())
        page = self.client.get(reverse("admin_participants"), {"league": self.league.id})
        self.assertNotContains(page, "doppia?")

    def test_teams_of_another_league_are_refused(self):
        foreign = Participant.objects.create(display_name="Spartak Noce",
                                             league=League.objects.create(name="Altra"))
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_merge_participant", args=[self.dup.id]),
                         {"target_id": foreign.id, "confirm": "1"})
        self.assertTrue(Participant.objects.filter(pk=self.dup.pk).exists())


class RoseImportTeamNameTests(TestCase):
    def test_a_differently_spelled_team_is_the_same_team(self):
        league = League.objects.create(name="Lega Nomi")
        team = Participant.objects.create(display_name="SLAVIA VIA FONDA", league=league)
        result = import_rose_data([{"name": "Slavia Viafonda", "credits": 100, "players": [
            {"name": "Bomber", "role": "A", "club": "Inter", "cost": 50}]}], league=league)
        self.assertEqual(result["teams"], 0)
        self.assertEqual(Participant.objects.filter(league=league).count(), 1)
        self.assertEqual(Player.objects.get(league=league, name="Bomber").owner, team)


class LeagueSearchTests(TestCase):
    """La ricerca rapida (console) e il Cerca dell'app trovano i dati della lega."""

    def setUp(self):
        from ..models import Competition
        self.owner = User.objects.create_user("pres_search", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lega Cerca", owner=self.owner)
        self.team = Participant.objects.create(display_name="Dinamo Viaritta", league=self.league)
        Player.objects.create(name="Lautaro Martinez", role="A", team="Inter", league=self.league, owner=self.team)
        Player.objects.create(name="Lautaro Altro", role="C", team="Como", league=League.objects.create(name="Altra"))
        season = Season.objects.create(league=self.league, name="2027/28", is_current=True)
        Competition.objects.create(season=season, name="Coppa Lugnanese", kind=Competition.Type.KNOCKOUT)

    def groups(self, resp):
        return {g["label"]: g["items"] for g in resp.json()["groups"]}

    def test_console_finds_players_teams_and_competitions_of_the_league(self):
        self.client.force_login(self.owner)
        url = reverse("league_search")
        found = self.groups(self.client.get(url, {"q": "lautaro", "league": self.league.id}))
        self.assertEqual([i["title"] for i in found["Calciatori"]], ["Lautaro Martinez"])   # non quello dell'altra lega
        self.assertIn("Dinamo Viaritta", found["Calciatori"][0]["detail"])
        self.assertIn(reverse("admin_footballers"), found["Calciatori"][0]["url"])
        found = self.groups(self.client.get(url, {"q": "viaritta", "league": self.league.id}))
        self.assertEqual(found["Squadre"][0]["title"], "Dinamo Viaritta")
        found = self.groups(self.client.get(url, {"q": "coppa", "league": self.league.id}))
        self.assertEqual(found["Competizioni"][0]["title"], "Coppa Lugnanese")

    def test_the_app_searches_the_team_league_with_app_links(self):
        session = self.client.session
        session["participant_id"] = self.team.id
        session.save()
        found = self.groups(self.client.get(reverse("app_league_search"), {"q": "lautaro"}))
        self.assertIn(reverse("app_footballers"), found["Calciatori"][0]["url"])
        page = self.client.get(reverse("app_altro"))
        self.assertContains(page, "Cerca nella lega")

    def test_draft_markets_stay_hidden_in_the_app(self):
        from ..models import MarketSession
        MarketSession.objects.create(league=self.league, title="Mercato segreto", status=MarketSession.Status.DRAFT)
        session = self.client.session
        session["participant_id"] = self.team.id
        session.save()
        self.assertNotIn("Mercati", self.groups(self.client.get(reverse("app_league_search"), {"q": "segreto"})))
        self.client.force_login(self.owner)
        found = self.groups(self.client.get(reverse("league_search"), {"q": "segreto", "league": self.league.id}))
        self.assertEqual(found["Mercati"][0]["title"], "Mercato segreto")      # chi gestisce la vede in console

    def test_strangers_find_nothing(self):
        self.client.force_login(User.objects.create_user("estraneo", password="pw", is_staff=True))
        resp = self.client.get(reverse("league_search"), {"q": "lautaro", "league": self.league.id})
        self.assertEqual(resp.json()["groups"], [])
