"""Economia (comma 3): tetto salariale, extra cap, Decreto, passaggi di stagione."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import (CapEntry, CapPhase, ContractEvent, DecreeAward, League, LeagueRanking,
                      MarketSession, Participant, Player, RosterLog)
from ..providers.standings import parse_ranking
from ..services import salary, season
from ..services.market import place_market_bid, resolve_market_session
from .common import make_live_auction


def _league(**kw):
    opts = dict(name="Lugnanese", salary_cap_enabled=True, contracts_enabled=True, season_number=2)
    opts.update(kw)
    return League.objects.create(**opts)


class CapTests(TestCase):
    def setUp(self):
        self.league = _league()
        self.teams = [Participant.objects.create(display_name=f"T{i}", league=self.league, credits=Decimal("3000"))
                      for i in range(10)]
        self.prev = salary.save_ranking(self.league, 1, LeagueRanking.Kind.FINAL, [t.id for t in self.teams])

    def test_base_by_previous_ranking(self):
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, self.prev)
        caps = [salary.cap_status(t)["cap"] for t in self.teams]
        self.assertEqual(caps, [1000, 1200, 1200, 1200, 1300, 1300, 1400, 1500, 1800, 1800])

    def test_lost_players_points_and_non_renewals(self):
        t = self.teams[0]
        # 2 attaccanti (2 pt) + 1 centrocampista (0,75) = 2,75 → +100; 2 non rinnovati → +2×50
        for name, role, kind in (("A1", "A", "not_renewed"), ("A2", "A", "rescinded"), ("C1", "C", "left")):
            p = Player.objects.create(name=name, role=role, league=self.league)
            ContractEvent.objects.create(league=self.league, player=p, participant=t, kind=kind)
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, self.prev)
        kinds = {e.kind: e.amount for e in CapEntry.objects.filter(participant=t)}
        self.assertEqual(kinds[CapEntry.Kind.LOST], 100)
        self.assertEqual(kinds[CapEntry.Kind.RENEWALS], 100)
        self.assertEqual(salary.cap_status(t)["cap"], 1200)

    def test_winter_adds_and_yearly_max(self):
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, self.prev)
        mid = salary.save_ranking(self.league, 2, LeagueRanking.Kind.MIDSEASON, [t.id for t in reversed(self.teams)])
        salary.open_phase(self.league, CapPhase.Kind.WINTER, mid)
        # Ultimo l'anno scorso (1800) e primo a metà (+100) = 1900.
        self.assertEqual(salary.cap_status(self.teams[-1])["cap"], 1900)
        salary.adjust_cap(self.league, self.teams[-1], 900, "prova")
        self.assertEqual(salary.cap_status(self.teams[-1])["cap"], 2500)  # massimo annuale

    def test_cap_blocks_live_bids_and_envelopes(self):
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, self.prev)
        t = self.teams[0]  # tetto 1000
        RosterLog.objects.create(participant=t, participant_name=t.display_name, player_name="x",
                                 action=RosterLog.Action.ASSIGN, credits_delta=Decimal("950"))
        self.assertEqual(salary.cap_status(t)["left"], 50)
        target = Player.objects.create(name="Bomber", role="A", league=self.league, initial_price=Decimal("1"))
        from .. import services
        with override_settings(BID_MIN_INTERVAL_MS=0):
            auction = make_live_auction(league=self.league, player=target, starting_price=Decimal("40"),
                                        current_price=Decimal("40"), min_increment=Decimal("1"),
                                        quick_increments="1,20", enforce_limits=False)
            self.assertTrue(services.place_bid(auction.id, t.id, 1).accepted)       # 41 ≤ 50
            r = services.place_bid(auction.id, self.teams[1].id, 20)                 # altro team ok
            self.assertTrue(r.accepted)
            r = services.place_bid(auction.id, t.id, 20)                             # 81 > 50
            self.assertEqual(r.reason, "salary_cap")
        session = MarketSession.objects.create(league=self.league, status=MarketSession.Status.OPEN)
        free = Player.objects.create(name="Free", role="C", league=self.league)
        self.assertEqual(place_market_bid(session.id, t.id, free.id, 60)["error"], "salary_cap")

    def test_net_spend_mode(self):
        self.league.salary_cap_spend = League.CapSpend.NET
        self.league.save()
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, self.prev)
        t = self.teams[0]
        for action, delta in ((RosterLog.Action.ASSIGN, 300), (RosterLog.Action.RELEASE, 100)):
            RosterLog.objects.create(participant=t, participant_name="T", player_name="x", action=action,
                                     credits_delta=Decimal(delta))
        self.assertEqual(salary.cap_status(t)["spent"], 200)

    def test_extra_salary_cap(self):
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, self.prev)
        t = self.teams[0]
        res = salary.convert_budget(t.id, 2)
        self.assertEqual((res["cost"], res["gain"]), (800, 200))
        t.refresh_from_db()
        self.assertEqual(t.credits, 2200)
        self.assertEqual(salary.cap_status(t)["cap"], 1200)
        make_live_auction(league=self.league, starting_price=Decimal("1"), current_price=Decimal("1"),
                          min_increment=Decimal("1"), quick_increments="1")
        self.assertFalse(salary.convert_budget(t.id, 1)["ok"])  # asta iniziata


class DecreeAndSeasonTests(TestCase):
    def setUp(self):
        self.league = _league(season_number=1)
        self.teams = [Participant.objects.create(display_name=f"T{i}", league=self.league, credits=Decimal("1000"))
                      for i in range(10)]

    def test_decree_burns_over_budget_max(self):
        rich = self.teams[-1]
        rich.credits = Decimal("4000")
        rich.save()
        ranking = salary.save_ranking(self.league, 1, LeagueRanking.Kind.FINAL, [t.id for t in self.teams])
        award = salary.award_decree(self.league, LeagueRanking.Kind.FINAL, ranking)
        rich.refresh_from_db()
        self.assertEqual(rich.credits, 4500)  # +800 ma massimo 4500
        first = next(d for d in award.details if d["position"] == 1)
        self.assertEqual((first["credits"], first["euro"]), (300, 350))
        with self.assertRaises(ValueError):
            salary.award_decree(self.league, LeagueRanking.Kind.FINAL, ranking)

    def test_full_season_flow(self):
        owner = self.teams[0]
        expiring = Player.objects.create(name="Scade", role="A", league=self.league, owner=owner, contract_years=1)
        order = [t.id for t in self.teams]
        salary.save_ranking(self.league, 0, LeagueRanking.Kind.FINAL, order)

        report = season.start_new_season(self.league.id, final_order=order)
        self.assertTrue(any("Decreto" in s for s in report["steps"]))
        self.league.refresh_from_db()
        self.assertEqual(self.league.season_number, 2)
        self.assertTrue(self.league.renewals_open)
        self.assertFalse(CapPhase.objects.exists())  # la fase estiva aspetta i rinnovi

        from ..services import contracts
        contracts.declare_renewals(owner.id, [])  # non rinnova: bonus
        season.close_renewals(self.league.id)
        entries = {e.kind: e.amount for e in CapEntry.objects.filter(participant=owner)}
        self.assertEqual(entries[CapEntry.Kind.BASE], 1000)
        self.assertEqual(entries[CapEntry.Kind.RENEWALS], 50)
        expiring.refresh_from_db()
        self.assertIsNone(expiring.owner)

        report = season.midseason(self.league.id, mid_order=list(reversed(order)))
        self.assertTrue(report["ok"])
        self.assertEqual(DecreeAward.objects.count(), 2)
        self.assertEqual(CapPhase.objects.filter(kind=CapPhase.Kind.WINTER).count(), 1)

    def test_admin_season_page(self):
        admin = User.objects.create_user("adm", password="pw")
        self.league.owner = admin
        self.league.save()
        self.client.force_login(admin)
        page = self.client.get(reverse("admin_season") + f"?league={self.league.id}")
        self.assertContains(page, "Nuova stagione")
        post = {"league_id": self.league.id, "action": "new_season"}
        post.update({f"final_{t.id}": str(i + 1) for i, t in enumerate(self.teams)})
        self.client.post(reverse("admin_season_action"), post)
        self.assertTrue(LeagueRanking.objects.filter(league=self.league, season=1, kind="final").exists())
        self.league.refresh_from_db()
        self.assertEqual(self.league.season_number, 2)

    def test_foreign_admin_forbidden(self):
        self.client.force_login(User.objects.create_user("x", password="pw"))
        self.league.owner = User.objects.create_user("owner", password="pw")
        self.league.save()
        resp = self.client.post(reverse("admin_season_action"), {"league_id": self.league.id, "action": "new_season"})
        self.assertEqual(resp.status_code, 403)


class RemoteStandingsTests(TestCase):
    def test_parse_table_with_team_names(self):
        html = """<table><tr><th>Pos</th><th>Squadra</th><th>Pt</th></tr>
            <tr><td>1</td><td>Dinamo Viaritta</td><td>40</td></tr>
            <tr><td>2</td><td>Poggio Saint-Germain</td><td>38</td></tr>
            <tr><td>3</td><td>Real Seravezza</td><td>30</td></tr></table>"""
        teams = {1: "Real Seravezza", 2: "Dinamo Viaritta", 3: "Poggio Saint Germain"}
        self.assertEqual(parse_ranking(html, teams), [2, 3, 1])

    def test_parse_div_based_list(self):
        html = """<div class="classifica"><div class="riga"><span>1</span><a>Real Seravezza</a><b>30</b></div>
            <div class="riga"><span>2</span><a>Dinamo Viaritta</a><b>28</b></div></div>"""
        self.assertEqual(parse_ranking(html, {5: "Dinamo Viaritta", 7: "Real Seravezza"}), [7, 5])

    def test_unknown_page_returns_none(self):
        self.assertIsNone(parse_ranking("<table><tr><td>Altro</td></tr></table>", {1: "Real Seravezza"}))


class AppCapTests(TestCase):
    def test_mercato_shows_cap_and_converts_budget(self):
        league = _league()
        teams = [Participant.objects.create(display_name=f"T{i}", league=league, credits=Decimal("3000")) for i in range(3)]
        prev = salary.save_ranking(league, 1, LeagueRanking.Kind.FINAL, [t.id for t in teams])
        salary.open_phase(league, CapPhase.Kind.SUMMER, prev)
        s = self.client.session
        s["participant_id"] = teams[0].id
        s.save()
        page = self.client.get(reverse("app_mercato"))
        self.assertContains(page, "Tetto")
        self.assertContains(page, "Extra salary cap")
        resp = self.client.post(reverse("app_extra_cap"), {"blocks": "1"}, follow=True)
        self.assertContains(resp, "Convertiti 400 FM")
        self.assertEqual(salary.cap_status(teams[0])["cap"], 1100)
