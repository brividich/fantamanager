import asyncio
import json
import io
import re
import sqlite3
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.conf import settings
from django.test import (RequestFactory, TestCase, TransactionTestCase,
                         override_settings)
from django.utils import timezone

from .. import mantra, services
from ..providers import importers
from ..views import participant_join_url
from ..routing import websocket_urlpatterns
from ..models import (Auction, AuctionCycleResult, AuctionQueueItem, Bid, Formation,
                     League, Participant, Player, RosterLog, SealedBid, Watch)
from .common import make_live_auction

class FormationTests(TestCase):
    """Formazione — lineup builder (module + starters, capped per role)."""

    def setUp(self):
        self.league = League.objects.create(name="L", budget=Decimal("500"),
                                             slots_p=3, slots_d=8, slots_c=8, slots_a=6)
        self.p = Participant.objects.create(display_name="Mister", league=self.league,
                                            credits=Decimal("500"))
        self.players = {}
        for role, n in (("P", 2), ("D", 5), ("C", 5), ("A", 3)):
            for i in range(n):
                pl = Player.objects.create(name=f"{role}{i}", role=role, team="Inter",
                                           league=self.league, owner=self.p, cost=Decimal("10"))
                self.players.setdefault(role, []).append(pl)

    def _login(self):
        s = self.client.session
        s["participant_id"] = self.p.id
        s.save()

    def test_page_redirects_without_session(self):
        self.assertIn("/app/login/", self.client.get("/app/formazione/")["Location"])

    def test_default_module_renders_pitch(self):
        self._login()
        resp = self.client.get("/app/formazione/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="pitch"')
        self.assertContains(resp, "Modulo 4-3-3")

    def test_save_lineup_persists_and_caps_per_role(self):
        self._login()
        # 4-3-3 → 1P,4D,3C,3A. Post more D than allowed; extra must be dropped.
        starters = ([self.players["P"][0].id]
                    + [d.id for d in self.players["D"]]          # 5 D → capped to 4
                    + [c.id for c in self.players["C"][:3]]
                    + [a.id for a in self.players["A"][:3]])
        resp = self.client.post("/app/formazione/",
                                {"module": "4-3-3", "starter": [str(x) for x in starters]})
        self.assertEqual(resp.status_code, 302)
        f = self.p.formation
        self.assertEqual(f.module, "4-3-3")
        # 1 + 4 + 3 + 3 = 11 starters, D capped at 4
        self.assertEqual(len(f.starter_ids), 11)
        d_ids = {d.id for d in self.players["D"]}
        self.assertEqual(len([x for x in f.starter_ids if x in d_ids]), 4)

    def test_module_change_recaps_and_bench_reflects(self):
        self._login()
        # Fill a 3-5-2 (needs 5 C) then switch to 4-3-3 (only 3 C) → 2 C benched.
        services = __import__("auctions.services", fromlist=["save_formation"])
        ids = ([self.players["P"][0].id]
               + [d.id for d in self.players["D"][:3]]
               + [c.id for c in self.players["C"]]        # 5 C
               + [a.id for a in self.players["A"][:2]])
        services.save_formation(self.p, "3-5-2", [str(x) for x in ids])
        self.assertEqual(len(self.p.formation.starter_ids), 11)
        state = services.formation_state(self.p)
        self.assertEqual(state["starters_target"], 11)
        # Switch module with same players; C capped to 3 now.
        resp = self.client.post("/app/formazione/",
                                {"module": "4-3-3", "starter": [str(x) for x in ids]})
        self.assertEqual(resp.status_code, 302)
        f = Formation.objects.get(participant=self.p)   # re-query (reverse accessor is cached)
        c_ids = {c.id for c in self.players["C"]}
        self.assertEqual(f.module, "4-3-3")
        self.assertEqual(len([x for x in f.starter_ids if x in c_ids]), 3)

    def test_foreign_players_ignored(self):
        self._login()
        other = Participant.objects.create(display_name="X", league=self.league)
        stranger = Player.objects.create(name="Z", role="A", team="Roma",
                                         league=self.league, owner=other, cost=Decimal("1"))
        resp = self.client.post("/app/formazione/",
                                {"module": "4-3-3", "starter": [str(stranger.id)]})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(stranger.id, self.p.formation.starter_ids)

    def test_rosa_links_to_formazione(self):
        self._login()
        self.assertContains(self.client.get("/app/rosa/"), "/app/formazione/")


class ScoringEngineTests(TestCase):
    """Pure fantacalcio scoring engine (auctions/scoring.py) — no DB."""

    def test_fantavoto_bonuses(self):
        from auctions import scoring
        r = scoring.DEFAULTS
        # striker: 6 base + 2 gol (+6) + 1 assist (+1) + ammonizione (-0.5) = 12.5
        fv, has = scoring.player_fantavoto(
            {"vote": 6, "goals": 2, "assists": 1, "yellow": True}, "A", r)
        self.assertTrue(has)
        self.assertEqual(fv, Decimal("12.5"))

    def test_goalkeeper_clean_sheet_and_conceded(self):
        from auctions import scoring
        r = scoring.DEFAULTS
        self.assertEqual(scoring.player_fantavoto({"vote": 6, "goals_conceded": 0}, "P", r)[0], Decimal("7"))
        self.assertEqual(scoring.player_fantavoto({"vote": 6, "goals_conceded": 2}, "P", r)[0], Decimal("4"))

    def test_pen_saved_bonus_is_goalkeeper_only(self):
        """A saved penalty is a goalkeeper-only event by the rules of the
        game — an outfield player's stat line can't legitimately carry one,
        but a bad row from a stats provider shouldn't score it if it does."""
        from auctions import scoring
        r = scoring.DEFAULTS
        # goals_conceded=1 (no clean sheet) isolates the pen_saved bonus alone.
        gk, _ = scoring.player_fantavoto(
            {"vote": 6, "pen_saved": 1, "goals_conceded": 1}, "P", r)
        self.assertEqual(gk, Decimal("6") + r["pen_saved"] + r["goal_conceded"])
        striker, _ = scoring.player_fantavoto({"vote": 6, "pen_saved": 1}, "A", r)
        self.assertEqual(striker, Decimal("6"))  # no bonus for a non-keeper

    def test_senza_voto_returns_no_vote(self):
        from auctions import scoring
        self.assertEqual(scoring.player_fantavoto({"vote": None}, "A", scoring.DEFAULTS), (None, False))

    def test_goal_conversion_ladder(self):
        from auctions import scoring
        r = scoring.DEFAULTS
        self.assertEqual(scoring.goals_from_total(65, r), 0)
        self.assertEqual(scoring.goals_from_total(66, r), 1)
        self.assertEqual(scoring.goals_from_total(71, r), 1)
        self.assertEqual(scoring.goals_from_total(72, r), 2)
        self.assertEqual(scoring.goals_from_total(78, r), 3)

    def test_fixture_outcome_points(self):
        from auctions import scoring
        self.assertEqual(scoring.fixture_outcome(2, 1), (3, 0))
        self.assertEqual(scoring.fixture_outcome(0, 2), (0, 3))
        self.assertEqual(scoring.fixture_outcome(1, 1), (1, 1))

    def _lineup_433(self):
        # starters (4-3-3): 2 s.v. midfielder + striker to trigger subs
        starters = (
            [{"id": 1, "role": "P"}]
            + [{"id": i, "role": "D"} for i in (2, 3, 4, 5)]
            + [{"id": i, "role": "C"} for i in (6, 7, 8)]
            + [{"id": i, "role": "A"} for i in (9, 10, 11)]
        )
        bench = [{"id": 12, "role": "C"}, {"id": 13, "role": "A"}, {"id": 14, "role": "D"}]
        perf = {
            1: {"vote": 6, "goals_conceded": 0},                 # +1 clean sheet -> 7
            2: {"vote": 6}, 3: {"vote": 6}, 4: {"vote": 5}, 5: {"vote": 7},  # 24
            6: {"vote": 6, "assists": 1}, 7: {"vote": 7}, 8: {"vote": None}, # 7 + 7 + (sub)
            9: {"vote": 7, "goals": 1}, 10: {"vote": 6}, 11: {"vote": None}, # 10 + 6 + (sub)
            12: {"vote": 6}, 13: {"vote": 7}, 14: {"vote": 6},   # bench: C=6, A=7
        }
        return starters, bench, perf

    def test_score_lineup_with_substitutions(self):
        from auctions import scoring
        starters, bench, perf = self._lineup_433()
        res = scoring.score_lineup(starters, bench, perf)
        # 7 + 24 + (7+7+6) + (10+6+7) = 74
        self.assertEqual(res["total"], Decimal("74"))
        self.assertEqual(res["goals"], 2)
        self.assertEqual(res["subs"], 2)   # C8->C12, A11->A13

    def test_sv_without_bench_contributes_zero(self):
        from auctions import scoring
        starters = [{"id": 1, "role": "A"}, {"id": 2, "role": "A"}]
        res = scoring.score_lineup(starters, [], {1: {"vote": 6}, 2: {"vote": None}})
        self.assertEqual(res["total"], Decimal("6"))
        self.assertEqual(res["subs"], 0)

    def test_max_three_substitutions(self):
        from auctions import scoring
        starters = [{"id": i, "role": "A"} for i in range(1, 6)]       # 5 strikers, all s.v.
        bench = [{"id": i, "role": "A"} for i in range(10, 15)]        # 5 bench strikers, all 6
        perf = {i: {"vote": None} for i in range(1, 6)}
        perf.update({i: {"vote": 6} for i in range(10, 15)})
        res = scoring.score_lineup(starters, bench, perf)
        self.assertEqual(res["subs"], 3)              # capped
        self.assertEqual(res["total"], Decimal("18"))  # only 3 came on

    def test_modificatore_difesa_optional(self):
        from auctions import scoring
        lines_perf = {
            1: {"vote": 7}, 2: {"vote": 7}, 3: {"vote": 6}, 4: {"vote": 7}, 5: {"vote": 6},
        }
        starters = [{"id": 1, "role": "P"}] + [{"id": i, "role": "D"} for i in (2, 3, 4, 5)]
        base = scoring.score_lineup(starters, [], lines_perf)
        withmod = scoring.score_lineup(starters, [], lines_perf, {"modificatore_difesa": True})
        # avg of GK(7) + best 3 D (7,7,6)=27/4=6.75 -> +3 from default table
        self.assertEqual(withmod["total"] - base["total"], Decimal("3"))


class SeasonScoringTests(TestCase):
    """DB bridge: compute a giornata from saved lineups + performances."""

    def setUp(self):
        self.league = League.objects.create(name="L", budget=Decimal("500"),
                                             slots_p=3, slots_d=8, slots_c=8, slots_a=6)
        self.p = Participant.objects.create(display_name="Mister", league=self.league,
                                            credits=Decimal("500"))
        self.players = {}
        for role, n in (("P", 1), ("D", 4), ("C", 3), ("A", 3)):
            self.players[role] = [
                Player.objects.create(name=f"{role}{i}", role=role, team="Inter",
                                      league=self.league, owner=self.p, cost=Decimal("10"))
                for i in range(n)
            ]
        allp = [pl for group in self.players.values() for pl in group]
        services.save_formation(self.p, "4-3-3", [str(pl.id) for pl in allp])
        from ..models import Season, Giornata
        self.season = Season.objects.create(league=self.league, name="2025/26")
        self.g = Giornata.objects.create(season=self.season, number=1, status="LOCKED")
        from ..models import PlayerPerformance
        for pl in allp:
            PlayerPerformance.objects.create(giornata=self.g, player=pl, vote=Decimal("6"))

    def test_compute_giornata_stores_scores_and_marks_scored(self):
        from ..models import GiornataScore, Giornata
        results = services.compute_giornata(self.g)
        self.assertEqual(len(results), 1)
        gs = GiornataScore.objects.get(giornata=self.g, participant=self.p)
        # 11 players all vote 6; GK clean sheet default 0 conceded -> +1 => 6*11 + 1 = 67
        self.assertEqual(gs.total, Decimal("67"))
        self.assertEqual(gs.goals, 1)
        self.assertEqual(len(gs.breakdown["lines"]), 11)
        self.assertEqual(Giornata.objects.get(pk=self.g.pk).status, "SCORED")


class CalendarStandingsTests(TestCase):
    """Round-robin calendar, head-to-head fixtures, and the league table."""

    def setUp(self):
        from ..models import Season
        self.league = League.objects.create(name="L", budget=Decimal("500"),
                                             slots_p=3, slots_d=8, slots_c=8, slots_a=6)
        self.season = Season.objects.create(league=self.league, name="25/26", matchdays=6)
        self.teams = []
        for t in range(4):
            p = Participant.objects.create(display_name=f"T{t}", league=self.league, credits=Decimal("500"))
            self.teams.append(p)

    def _fill_roster(self, p, vote):
        allp = []
        for role, n in (("P", 1), ("D", 4), ("C", 3), ("A", 3)):
            for i in range(n):
                allp.append(Player.objects.create(name=f"{p.id}-{role}{i}", role=role, team="X",
                                                  league=self.league, owner=p, cost=Decimal("10")))
        services.save_formation(p, "4-3-3", [str(x.id) for x in allp])
        return allp, vote

    def test_round_robin_everyone_plays_everyone_once(self):
        rounds = services._round_robin([1, 2, 3, 4])
        self.assertEqual(len(rounds), 3)
        seen = set()
        for rnd in rounds:
            self.assertEqual(len(rnd), 2)
            for h, a in rnd:
                seen.add(frozenset((h, a)))
        self.assertEqual(len(seen), 6)   # C(4,2)

    def test_generate_calendar_creates_giornate_and_fixtures(self):
        from ..models import Giornata, Fixture
        made = services.generate_calendar(self.season, teams=self.teams)
        self.assertEqual(len(made), 6)
        self.assertEqual(Giornata.objects.filter(season=self.season).count(), 6)
        self.assertEqual(Giornata.objects.get(season=self.season, number=1).status, "OPEN")
        for g in made:
            self.assertEqual(Fixture.objects.filter(giornata=g).count(), 2)

    def test_compute_giornata_resolves_fixtures_and_standings(self):
        from ..models import Giornata, Fixture, PlayerPerformance
        services.generate_calendar(self.season, teams=self.teams)
        # Give every team a roster; strengths differ so results aren't all draws.
        strengths = {self.teams[0].id: 7, self.teams[1].id: 6, self.teams[2].id: 7, self.teams[3].id: 6}
        for p in self.teams:
            allp, vote = self._fill_roster(p, strengths[p.id])
        g1 = Giornata.objects.get(season=self.season, number=1)
        for p in self.teams:
            for pl in Player.objects.filter(owner=p):
                PlayerPerformance.objects.create(giornata=g1, player=pl, vote=Decimal(strengths[p.id]))
        services.compute_giornata(g1)
        # every fixture in g1 is computed
        for fx in Fixture.objects.filter(giornata=g1):
            self.assertTrue(fx.computed)
        table = services.standings(self.season)
        self.assertEqual(len(table), 4)
        self.assertEqual(sum(r["played"] for r in table), 4)   # 2 fixtures * 2 teams
        self.assertEqual(table[0]["rank"], 1)
        # points awarded (someone won or all draw) — total points is 4 or 6
        self.assertIn(sum(r["points"] for r in table), (4, 6))

