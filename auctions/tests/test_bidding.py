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

@override_settings(BID_MIN_INTERVAL_MS=0)
class BidServiceTests(TestCase):
    def setUp(self):
        self.participant = Participant.objects.create(display_name="Alice")

    def test_place_bid_runs_in_its_own_transaction(self):
        # select_for_update senza transazione: su PostgreSQL ogni rilancio va in errore.
        from django.db import connection
        from ..services import bidding
        auction = make_live_auction()
        outside = len(connection.atomic_blocks)
        depth = []
        real_now = timezone.now
        with mock.patch.object(bidding.timezone, "now",
                               side_effect=lambda: depth.append(len(connection.atomic_blocks)) or real_now()):
            services.place_bid(auction.id, self.participant.id, 50)
        self.assertGreater(depth[0], outside)

    def test_accepted_while_live(self):
        auction = make_live_auction()
        result = services.place_bid(auction.id, self.participant.id, 50)
        self.assertTrue(result.accepted)
        self.assertEqual(result.bid.amount, Decimal("150"))
        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("150"))
        self.assertEqual(auction.best_bid_id, result.bid.id)

    def test_rejected_when_closed(self):
        auction = make_live_auction(status=Auction.Status.CLOSED)
        result = services.place_bid(auction.id, self.participant.id, 50)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, services.Reject.NOT_LIVE)
        # Even a rejected bid is logged.
        self.assertEqual(Bid.objects.filter(accepted=False).count(), 1)

    def test_rejected_when_timer_expired(self):
        now = timezone.now()
        auction = make_live_auction(
            starts_at=now - timedelta(seconds=120),
            ends_at=now - timedelta(seconds=1),
        )
        result = services.place_bid(auction.id, self.participant.id, 50)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, services.Reject.EXPIRED)

    def test_rejected_when_participant_inactive(self):
        auction = make_live_auction()
        self.participant.is_active = False
        self.participant.save()
        result = services.place_bid(auction.id, self.participant.id, 50)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, services.Reject.PARTICIPANT_INACTIVE)

    def test_rejected_when_increment_not_allowed(self):
        auction = make_live_auction()
        # 7 is neither a quick button nor >= min_increment (10).
        result = services.place_bid(auction.id, self.participant.id, 7)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, services.Reject.BAD_INCREMENT)

    def test_client_amount_is_ignored(self):
        """The server computes amount; only the increment matters."""
        auction = make_live_auction()
        # Even if a malicious client tried to pass amount, place_bid only takes
        # increment. We verify the computed amount is current_price + increment.
        result = services.place_bid(auction.id, self.participant.id, 100)
        self.assertEqual(result.bid.amount, Decimal("200"))

    def test_sequential_bids_build_on_current_price(self):
        """Two near-simultaneous bids must not both win at the same price.

        The atomic + select_for_update transaction serialises them, so the
        second bid is computed from the updated current_price.
        """
        auction = make_live_auction()
        bob = Participant.objects.create(display_name="Bob")
        r1 = services.place_bid(auction.id, self.participant.id, 50)
        r2 = services.place_bid(auction.id, bob.id, 50)
        self.assertTrue(r1.accepted and r2.accepted)
        self.assertEqual(r1.bid.amount, Decimal("150"))
        self.assertEqual(r2.bid.amount, Decimal("200"))  # built on 150, not 100
        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("200"))

    def test_rejected_bids_are_logged_with_reason(self):
        auction = make_live_auction(status=Auction.Status.PAUSED)
        services.place_bid(auction.id, self.participant.id, 50)
        bid = Bid.objects.get()
        self.assertFalse(bid.accepted)
        self.assertEqual(bid.rejection_reason, services.Reject.NOT_LIVE)


class RateLimitTests(TestCase):
    @override_settings(BID_MIN_INTERVAL_MS=1000)
    def test_second_rapid_bid_is_rate_limited(self):
        # block_leader_rebid off: this is about the spam guard, not about the
        # leader raising itself (covered by BlockLeaderRebidTests).
        auction = make_live_auction(block_leader_rebid=False)
        p = Participant.objects.create(display_name="Spammer")
        r1 = services.place_bid(auction.id, p.id, 50)
        r2 = services.place_bid(auction.id, p.id, 50)
        self.assertTrue(r1.accepted)
        self.assertFalse(r2.accepted)
        self.assertEqual(r2.reason, services.Reject.RATE_LIMITED)
        # Not written to the bid log: a burst of taps adds no rows.
        self.assertFalse(Bid.objects.filter(rejection_reason=services.Reject.RATE_LIMITED).exists())


@override_settings(BID_MIN_INTERVAL_MS=0)
class BlockLeaderRebidTests(TestCase):
    """Anti double-click: whoever is on top cannot bid against themselves."""

    def setUp(self):
        self.a = Participant.objects.create(display_name="Alfa", credits=Decimal("1000"))
        self.b = Participant.objects.create(display_name="Beta", credits=Decimal("1000"))

    def test_the_leader_is_refused_a_second_bid(self):
        auction = make_live_auction()
        self.assertTrue(services.place_bid(auction.id, self.a.id, 10).accepted)
        second = services.place_bid(auction.id, self.a.id, 10)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, services.Reject.ALREADY_LEADING)
        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("110"))   # price untouched

    def test_someone_else_can_still_outbid(self):
        auction = make_live_auction()
        services.place_bid(auction.id, self.a.id, 10)
        self.assertTrue(services.place_bid(auction.id, self.b.id, 10).accepted)
        # …and now Alfa, no longer leading, may raise again.
        self.assertTrue(services.place_bid(auction.id, self.a.id, 10).accepted)

    def test_the_rule_can_be_switched_off(self):
        auction = make_live_auction(block_leader_rebid=False)
        services.place_bid(auction.id, self.a.id, 10)
        self.assertTrue(services.place_bid(auction.id, self.a.id, 10).accepted)

    def test_the_rejection_is_logged_like_any_other(self):
        auction = make_live_auction()
        services.place_bid(auction.id, self.a.id, 10)
        services.place_bid(auction.id, self.a.id, 10)
        last = Bid.objects.order_by("-id").first()
        self.assertFalse(last.accepted)
        self.assertEqual(last.rejection_reason, services.Reject.ALREADY_LEADING)
        self.assertIn(services.Reject.ALREADY_LEADING, services.ERROR_LABELS)

    # --- precedence between the guards ------------------------------------

    def test_leading_beats_a_bad_increment(self):
        """Same situation, same message — whatever else is wrong with the bid."""
        auction = make_live_auction(quick_increments="10,50")
        services.place_bid(auction.id, self.a.id, 10)
        res = services.place_bid(auction.id, self.a.id, 7)      # 7 is not allowed
        self.assertEqual(res.reason, services.Reject.ALREADY_LEADING)

    @override_settings(BID_MIN_INTERVAL_MS=1000)
    def test_leading_beats_the_rate_limiter(self):
        auction = make_live_auction()
        services.place_bid(auction.id, self.a.id, 10)
        res = services.place_bid(auction.id, self.a.id, 10)
        self.assertEqual(res.reason, services.Reject.ALREADY_LEADING)

    @override_settings(BID_MIN_INTERVAL_MS=1000)
    def test_a_challenger_spamming_is_still_rate_limited(self):
        auction = make_live_auction()
        services.place_bid(auction.id, self.a.id, 10)           # Alfa leads
        self.assertTrue(services.place_bid(auction.id, self.b.id, 10).accepted)
        res = services.place_bid(auction.id, self.a.id, 10)     # Alfa, not leading
        self.assertEqual(res.reason, services.Reject.RATE_LIMITED)

    def test_a_paused_auction_answers_not_live_even_to_the_leader(self):
        auction = make_live_auction()
        services.place_bid(auction.id, self.a.id, 10)
        services.pause_auction(auction.id)
        res = services.place_bid(auction.id, self.a.id, 10)
        self.assertEqual(res.reason, services.Reject.NOT_LIVE)


@override_settings(BID_MIN_INTERVAL_MS=0)
class AssignmentTests(TestCase):
    """Cycle finalisation: idempotent charge + result + roster ownership."""

    def test_close_charges_winner_once_and_records_result(self):
        auction = make_live_auction()
        p = Participant.objects.create(display_name="Win", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)   # 100 + 50 = 150
        services.close_auction(auction.id)

        p.refresh_from_db()
        self.assertEqual(p.spent_credits, Decimal("150"))

        result = AuctionCycleResult.objects.get(auction=auction, cycle=1)
        self.assertTrue(result.assigned)
        self.assertEqual(result.amount, Decimal("150"))
        self.assertEqual(result.winner_id, p.id)
        self.assertEqual(result.assigned_by, "admin")

    def test_double_close_does_not_double_charge(self):
        """A second finalisation of the same cycle must be a no-op."""
        auction = make_live_auction()
        p = Participant.objects.create(display_name="Win", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)
        services.close_auction(auction.id)
        services.close_auction(auction.id)   # re-finalise same cycle

        p.refresh_from_db()
        self.assertEqual(p.spent_credits, Decimal("150"))   # charged once, not 300
        self.assertEqual(AuctionCycleResult.objects.filter(auction=auction, cycle=1).count(), 1)

    def test_assignment_transfers_ownership_and_logs(self):
        player  = Player.objects.create(name="Falcone", role="P", team="Lecce")
        auction = make_live_auction(player=player)
        p = Participant.objects.create(display_name="Owner", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)
        services.close_auction(auction.id)

        player.refresh_from_db()
        self.assertEqual(player.owner_id, p.id)
        self.assertEqual(player.cost, Decimal("150"))
        self.assertTrue(
            RosterLog.objects.filter(
                action=RosterLog.Action.ASSIGN, player_name="Falcone"
            ).exists()
        )

    def test_no_winner_no_charge(self):
        """Closing an auction with no accepted bid charges nobody."""
        auction = make_live_auction()
        services.close_auction(auction.id)
        self.assertFalse(AuctionCycleResult.objects.filter(auction=auction).exists())


@override_settings(BID_MIN_INTERVAL_MS=0)
class RosterLimitTests(TestCase):
    """Fase D — slot + budget-reserve enforcement during bidding.

    Enforcement is scoped to league bidders; legacy participants (no league)
    are intentionally unaffected, which the BidServiceTests above already cover.
    """

    def setUp(self):
        self.league = League.objects.create(
            name="Lega", budget=Decimal("100"),
            slots_p=1, slots_d=1, slots_c=1, slots_a=1,   # 4 total slots
        )

    def _bidder(self, credits):
        return Participant.objects.create(
            display_name="Bidder", league=self.league, credits=Decimal(credits)
        )

    def test_legacy_participant_without_league_is_unrestricted(self):
        auction = make_live_auction()
        p = Participant.objects.create(display_name="Solo", credits=Decimal("120"))
        # No league → reserve/slot rules skipped even though credits are tight.
        r = services.place_bid(auction.id, p.id, 100)  # 100 + 100 = 200 > credits
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.INSUFFICIENT_CREDITS)  # not BUDGET_RESERVE

    def test_slot_full_rejects_bid_for_that_role(self):
        keeper = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(
            player=keeper, starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5",
        )
        p = self._bidder("100")
        # Already owns a goalkeeper → P slot (1) is full.
        Player.objects.create(name="Other GK", role="P", owner=p)
        r = services.place_bid(auction.id, p.id, 5)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.ROSTER_SLOT_FULL)

    def test_free_slot_allows_bid(self):
        keeper = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(
            player=keeper, starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5",
        )
        p = self._bidder("100")
        r = services.place_bid(auction.id, p.id, 5)  # owns nothing yet
        self.assertTrue(r.accepted)

    def test_budget_reserve_blocks_overspend(self):
        # 4 slots, owns 0. After winning, 3 slots remain → must keep >=3 credits.
        auction = make_live_auction(
            starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5,100",
        )
        p = self._bidder("100")
        # Bidding 100 → amount 101 > 100 credits anyway, use a tighter case:
        # remaining 100, reserve 3 → max bid 97. A bid to 98 must be rejected.
        # current 1 + 97 = 98.
        r = services.place_bid(auction.id, p.id, 97)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.BUDGET_RESERVE)
        # A bid that respects the reserve (to 97) is accepted.
        r2 = services.place_bid(auction.id, p.id, 96)  # 1 + 96 = 97
        self.assertTrue(r2.accepted)

    def test_enforce_limits_off_disables_checks(self):
        keeper = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(
            player=keeper, enforce_limits=False,
            starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5",
        )
        p = self._bidder("100")
        Player.objects.create(name="Other GK", role="P", owner=p)  # slot would be full
        r = services.place_bid(auction.id, p.id, 5)
        self.assertTrue(r.accepted)  # override: no slot/reserve enforcement


@override_settings(BID_MIN_INTERVAL_MS=0)
class RosterReserveTests(TestCase):
    """The budget reserve must scale with how many slots are already filled:
    the more of the rosa is complete, the fewer credits must be held back."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega", budget=Decimal("500"),
            slots_p=1, slots_d=1, slots_c=1, slots_a=1,   # 4 total slots
        )

    def test_reserve_scales_with_already_owned_slots(self):
        mid = Player.objects.create(name="Reg", role="C")
        auction = make_live_auction(
            player=mid, starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5",
        )
        p = Participant.objects.create(
            display_name="Bidder", league=self.league, credits=Decimal("100"),
        )
        # Owns 2 of 4 slots (P + D). Bidding on a C leaves only 1 empty slot
        # after winning → reserve is just 1 credit (vs 3 when owning none).
        Player.objects.create(name="GK", role="P", owner=p)
        Player.objects.create(name="Def", role="D", owner=p)

        rejected = services.place_bid(auction.id, p.id, 99)   # 1 + 99 = 100, keeps 0 < 1
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, services.Reject.BUDGET_RESERVE)

        accepted = services.place_bid(auction.id, p.id, 98)   # 1 + 98 = 99, keeps exactly 1
        self.assertTrue(accepted.accepted)


@override_settings(BID_MIN_INTERVAL_MS=0)
class NoSlotLimitTests(TestCase):
    """'Nessun limite slot': the rosa has no shape, only the budget bites."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega Libera", budget=Decimal("500"), slot_limits=False,
            slots_p=1, slots_d=1, slots_c=1, slots_a=1,   # kept, but not applied
        )
        self.p = Participant.objects.create(
            display_name="Bidder", league=self.league, credits=Decimal("100"),
        )

    def _auction_on(self, role="A"):
        player = Player.objects.create(name=f"Nuovo{role}", role=role)
        return make_live_auction(
            player=player, starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5",
        )

    def test_a_full_role_no_longer_blocks_a_bid(self):
        Player.objects.create(name="Att1", role="A", owner=self.p)   # role "full"
        res = services.place_bid(self._auction_on("A").id, self.p.id, 5)
        self.assertTrue(res.accepted)

    def test_no_empty_slot_reserve_is_held_back(self):
        # With slots the bidder would have to keep 3 credits for the empty ones;
        # here the whole budget is spendable on this single lot.
        res = services.place_bid(self._auction_on("C").id, self.p.id, 99)  # 1 + 99 = 100
        self.assertTrue(res.accepted)

    def test_the_planner_reports_an_unlimited_rosa(self):
        plan = services.roster_plan(self.p)
        self.assertFalse(plan["slot_limits"])
        self.assertEqual(plan["total_slots"], 0)
        self.assertEqual(plan["free_slots"], 0)
        self.assertEqual(plan["max_bid"], Decimal("100"))

    def test_a_saved_session_carries_the_choice_across(self):
        auction = make_live_auction(league=self.league)
        session = services.save_session(auction.id, created_by="admin")
        self.assertFalse(session.data["league"]["slot_limits"])
        revived = services.resume_session(session.id)
        self.assertFalse(revived.league.slot_limits)


@override_settings(BID_MIN_INTERVAL_MS=0)
class ReleasePlayerTests(TestCase):
    """Svincolo: release an owned player and refund per the auction policy."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")

    def _owned(self, *, cost, quotazione, spent):
        p = Participant.objects.create(
            display_name="Owner", credits=Decimal("500"), spent_credits=Decimal(spent)
        )
        player = Player.objects.create(
            name="Falcao", role="A", team="Roma",
            initial_price=Decimal(quotazione), cost=Decimal(cost), owner=p,
        )
        return p, player

    def test_refund_purchase_returns_cost(self):
        auction = make_live_auction(release_refund_mode=Auction.RefundMode.PURCHASE)
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        res = services.release_player(player.id, auction_id=auction.id, by_admin=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["refund"], "80.00")
        player.refresh_from_db(); p.refresh_from_db()
        self.assertIsNone(player.owner_id)
        self.assertEqual(p.spent_credits, Decimal("0"))

    def test_refund_current_uses_quotazione(self):
        auction = make_live_auction(release_refund_mode=Auction.RefundMode.CURRENT)
        p, player = self._owned(cost="80", quotazione="120", spent="200")
        res = services.release_player(player.id, auction_id=auction.id, by_admin=True)
        self.assertEqual(res["refund"], "120.00")
        p.refresh_from_db()
        self.assertEqual(p.spent_credits, Decimal("80"))   # 200 - 120

    def test_refund_none_returns_nothing(self):
        auction = make_live_auction(release_refund_mode=Auction.RefundMode.NONE)
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        res = services.release_player(player.id, auction_id=auction.id, by_admin=True)
        self.assertEqual(res["refund"], "0.00")
        p.refresh_from_db()
        self.assertEqual(p.spent_credits, Decimal("80"))

    def test_refund_never_makes_spent_negative(self):
        auction = make_live_auction(release_refund_mode=Auction.RefundMode.CURRENT)
        p, player = self._owned(cost="80", quotazione="120", spent="50")
        res = services.release_player(player.id, auction_id=auction.id, by_admin=True)
        self.assertEqual(res["refund"], "50.00")   # capped at what was spent
        p.refresh_from_db()
        self.assertEqual(p.spent_credits, Decimal("0"))

    def test_release_writes_rosterlog(self):
        auction = make_live_auction()
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        services.release_player(player.id, auction_id=auction.id, by_admin=True)
        log = RosterLog.objects.latest("id")
        self.assertEqual(log.action, RosterLog.Action.ADMIN_RELEASE)
        self.assertTrue(log.by_admin)
        self.assertEqual(log.player_name, "Falcao")

    def test_participant_cannot_release_others_player(self):
        auction = make_live_auction()
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        intruder = Participant.objects.create(display_name="Nosy")
        res = services.release_player(
            player.id, auction_id=auction.id, by_admin=False, participant_id=intruder.id
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "forbidden")
        player.refresh_from_db()
        self.assertEqual(player.owner_id, p.id)   # untouched

    def test_participant_can_release_own_player(self):
        auction = make_live_auction(release_refund_mode=Auction.RefundMode.PURCHASE)
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        res = services.release_player(
            player.id, auction_id=auction.id, by_admin=False, participant_id=p.id
        )
        self.assertTrue(res["ok"])
        log = RosterLog.objects.latest("id")
        self.assertEqual(log.action, RosterLog.Action.RELEASE)
        self.assertFalse(log.by_admin)

    def test_release_free_agent_fails(self):
        auction = make_live_auction()
        free = Player.objects.create(name="FreeMan", role="C")
        res = services.release_player(free.id, auction_id=auction.id, by_admin=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not_owned")

    def test_admin_release_view(self):
        auction = make_live_auction(release_refund_mode=Auction.RefundMode.PURCHASE)
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        self.client.force_login(self.user)
        resp = self.client.post(
            f"/admin-auction/players/{player.id}/release/", {"auction_id": auction.id}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        player.refresh_from_db()
        self.assertIsNone(player.owner_id)

    def test_participant_release_view_requires_session(self):
        auction = make_live_auction()
        p, player = self._owned(cost="80", quotazione="120", spent="80")
        resp = self.client.post(
            f"/release/{player.id}/", {"auction_id": auction.id}
        )
        self.assertEqual(resp.status_code, 403)

    def test_refund_mode_flows_through_wizard(self):
        self.client.force_login(self.user)
        league = League.objects.create(name="L", budget=Decimal("300"))
        Player.objects.create(name="Base", role="A", league=league,
                              initial_price=Decimal("1"), owner=None)
        self.client.post("/admin-auction/wizard/create/", {
            "league_id": str(league.id),
            "mode": "REPAIR_AUCTION",
            "min_increment": "1", "quick_increments": "1,2,5",
            "duration_seconds": "60", "antisnipe_seconds": "10",
            "release_refund_mode": "current",
        })
        auction = Auction.objects.latest("id")
        self.assertEqual(auction.release_refund_mode, Auction.RefundMode.CURRENT)


class AssignPlayerTests(TestCase):
    """Manual admin assign / re-assign / price-correction of a player."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.team_a = Participant.objects.create(display_name="A", spent_credits=Decimal("0"))
        self.team_b = Participant.objects.create(display_name="B", spent_credits=Decimal("0"))

    def test_assign_free_agent_charges_owner(self):
        player = Player.objects.create(name="Vlahovic", role="A", initial_price=Decimal("90"))
        res = services.assign_player(player.id, self.team_a.id, price="75")
        self.assertTrue(res["ok"])
        player.refresh_from_db(); self.team_a.refresh_from_db()
        self.assertEqual(player.owner_id, self.team_a.id)
        self.assertEqual(player.cost, Decimal("75"))
        self.assertEqual(self.team_a.spent_credits, Decimal("75"))
        log = RosterLog.objects.latest("id")
        self.assertEqual(log.action, RosterLog.Action.ADMIN_ASSIGN)

    def test_assign_defaults_price_to_quotazione(self):
        player = Player.objects.create(name="Osi", role="A", initial_price=Decimal("60"))
        res = services.assign_player(player.id, self.team_a.id)
        self.assertEqual(res["price"], "60.00")
        self.team_a.refresh_from_db()
        self.assertEqual(self.team_a.spent_credits, Decimal("60"))

    def test_reassign_refunds_previous_owner(self):
        player = Player.objects.create(
            name="Leao", role="A", initial_price=Decimal("100"),
            cost=Decimal("80"), owner=self.team_a,
        )
        self.team_a.spent_credits = Decimal("80"); self.team_a.save()
        res = services.assign_player(player.id, self.team_b.id, price="50")
        self.assertTrue(res["ok"])
        player.refresh_from_db(); self.team_a.refresh_from_db(); self.team_b.refresh_from_db()
        self.assertEqual(player.owner_id, self.team_b.id)
        self.assertEqual(self.team_a.spent_credits, Decimal("0"))   # refunded 80
        self.assertEqual(self.team_b.spent_credits, Decimal("50"))  # charged 50
        # both a release (for A) and an assign (for B) were logged
        actions = set(RosterLog.objects.values_list("action", flat=True))
        self.assertIn(RosterLog.Action.ADMIN_RELEASE, actions)
        self.assertIn(RosterLog.Action.ADMIN_ASSIGN, actions)

    def test_price_correction_same_owner_applies_delta(self):
        player = Player.objects.create(
            name="Dimarco", role="D", initial_price=Decimal("30"),
            cost=Decimal("40"), owner=self.team_a,
        )
        self.team_a.spent_credits = Decimal("40"); self.team_a.save()
        res = services.assign_player(player.id, self.team_a.id, price="25")
        self.assertTrue(res["corrected"])
        player.refresh_from_db(); self.team_a.refresh_from_db()
        self.assertEqual(player.cost, Decimal("25"))
        self.assertEqual(self.team_a.spent_credits, Decimal("25"))  # 40 - 15 delta
        self.assertEqual(player.owner_id, self.team_a.id)

    def test_assign_view(self):
        player = Player.objects.create(name="Thuram", role="A", initial_price=Decimal("50"))
        self.client.force_login(self.user)
        resp = self.client.post(
            f"/admin-auction/players/{player.id}/assign/",
            {"participant_id": self.team_a.id, "price": "45"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        player.refresh_from_db()
        self.assertEqual(player.owner_id, self.team_a.id)

    def test_assign_view_requires_team(self):
        player = Player.objects.create(name="X", role="A")
        self.client.force_login(self.user)
        resp = self.client.post(f"/admin-auction/players/{player.id}/assign/", {})
        self.assertEqual(resp.status_code, 400)

