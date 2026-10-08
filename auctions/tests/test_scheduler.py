"""The scheduler service: what must happen on time with nobody on the page."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .. import services
from ..models import (Auction, Giornata, League, MarketSession, MatchdayFormation, Participant,
                      Player, Season)
from ..services import scheduler
from .common import make_live_auction


class AuctionTickTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega", budget=Decimal("500"))
        self.team = Participant.objects.create(display_name="Eve", league=self.league, credits=Decimal("500"))
        self.player = Player.objects.create(name="Kvara", role="A", league=self.league,
                                            initial_price=Decimal("10"))
        self.auction = make_live_auction(league=self.league, player=self.player, current_price=Decimal("10"),
                                         min_increment=Decimal("1"), quick_increments="1,5,10")

    def test_an_expired_lot_closes_and_the_winner_pays_with_nobody_connected(self):
        services.place_bid(self.auction.id, self.team.id, 5)
        Auction.objects.filter(pk=self.auction.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        sent = []
        summary = scheduler.run_once(broadcast=sent.append)
        self.assertEqual(summary["auctions"], {self.auction.id: ["closed"]})
        self.assertEqual(sent, [self.auction.id])
        self.player.refresh_from_db()
        self.assertEqual(self.player.owner_id, self.team.id)
        # A second pass (or the room's own ticker) changes nothing more.
        self.team.refresh_from_db()
        spent = self.team.spent_credits
        scheduler.run_once()
        self.team.refresh_from_db()
        self.assertEqual(self.team.spent_credits, spent)

    def test_a_running_lot_is_left_alone(self):
        self.assertEqual(scheduler.run_once()["auctions"], {})
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.LIVE)

    def test_the_command_runs_one_pass(self):
        out = StringIO()
        call_command("run_scheduler", "--once", stdout=out)
        self.assertIn("auctions", out.getvalue())


class MarketAndLineupTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega")
        self.team = Participant.objects.create(display_name="Squadra", league=self.league)
        self.season = Season.objects.create(league=self.league, name="2026/27")
        self.g1 = Giornata.objects.create(season=self.season, number=1, status="OPEN")
        self.g2 = Giornata.objects.create(season=self.season, number=2)

    def test_market_sessions_open_and_close_on_their_dates(self):
        now = timezone.now()
        due = MarketSession.objects.create(league=self.league, title="Buste", opens_at=now - timedelta(minutes=1),
                                           closes_at=now + timedelta(days=1))
        scheduler.run_once()
        due.refresh_from_db()
        self.assertEqual(due.status, MarketSession.Status.OPEN)

    def test_the_deadline_locks_the_lineups(self):
        self.g1.starts_at = timezone.now() - timedelta(minutes=1)
        self.g1.save()
        self.g2.starts_at = timezone.now() + timedelta(days=7)
        self.g2.save()
        summary = scheduler.run_once()
        self.assertEqual(summary["giornate"], [self.g1.id])
        self.g1.refresh_from_db()
        self.g2.refresh_from_db()
        self.assertEqual(self.g1.status, Giornata.Status.LOCKED)
        self.assertTrue(MatchdayFormation.objects.filter(giornata=self.g1, participant=self.team).exists())
        self.assertEqual(self.g2.status, Giornata.Status.SCHEDULED)

    def test_without_the_scheduler_the_deadline_still_holds(self):
        """Nobody ran the service: the next look at the formation page locks
        the giornata whose deadline passed and moves on to the next one."""
        self.g1.starts_at = timezone.now() - timedelta(minutes=1)
        self.g1.save()
        self.assertEqual(services.target_giornata(self.league), self.g2)
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.status, Giornata.Status.LOCKED)

    def test_deadlines_come_from_the_serie_a_calendar(self):
        import os
        from unittest import mock

        rows = [{"league": {"round": "Regular Season - 1"}, "fixture": {"date": "2026-08-23T18:45:00+00:00"}},
                {"league": {"round": "Regular Season - 1"}, "fixture": {"date": "2026-08-22T16:30:00+00:00"}},
                {"league": {"round": "Regular Season - 2"}, "fixture": {"date": "2026-08-30T16:30:00+00:00"}}]

        class Resp:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"response": rows, "errors": []}

        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "k"}):
            n = scheduler.deadlines_from_calendar(self.season, get=lambda *a, **k: Resp())
        self.assertEqual(n, 2)
        self.g1.refresh_from_db()
        self.assertEqual(self.g1.starts_at.isoformat(), "2026-08-22T16:30:00+00:00")


class DeadlineFormTests(TestCase):
    """The deadline is set from the Giornate page, the same in console and app."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.owner = User.objects.create_user("presidente", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lega", owner=self.owner)
        self.season = Season.objects.create(league=self.league, name="2026/27", matchdays=2)
        self.g1 = Giornata.objects.create(season=self.season, number=1)
        Giornata.objects.create(season=self.season, number=2)
        self.client.force_login(self.owner)

    def test_admin_sets_and_clears_the_deadline(self):
        from django.urls import reverse
        for url in (reverse("admin_giornate"), reverse("app_giornate")):
            page = self.client.get(url, {"league": self.league.id, "giornata": 1})
            self.assertContains(page, 'name="starts_at"')
        self.client.post(reverse("admin_giornate") + f"?league={self.league.id}",
                         {"giornata": 1, "serie_a_matchday": 1, "starts_at": "2026-08-22T18:30"})
        self.g1.refresh_from_db()
        self.assertEqual(timezone.localtime(self.g1.starts_at).strftime("%d/%m %H:%M"), "22/08 18:30")
        self.client.post(reverse("admin_giornate") + f"?league={self.league.id}",
                         {"giornata": 1, "serie_a_matchday": 1, "starts_at": ""})
        self.g1.refresh_from_db()
        self.assertIsNone(self.g1.starts_at)
