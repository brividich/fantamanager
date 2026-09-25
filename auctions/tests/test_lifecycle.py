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

class LifecycleTests(TestCase):
    def test_pause_and_resume_preserve_remaining(self):
        auction = make_live_auction(duration_seconds=60)
        services.pause_auction(auction.id)
        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.PAUSED)
        self.assertIsNotNone(auction.remaining_seconds)
        services.resume_auction(auction.id)
        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.LIVE)
        self.assertIsNone(auction.remaining_seconds)

    def test_close_if_expired_runs_once(self):
        now = timezone.now()
        auction = make_live_auction(ends_at=now - timedelta(seconds=1))
        first = services.close_if_expired(auction.id)
        second = services.close_if_expired(auction.id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # already closed
        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.CLOSED)

    @override_settings(BID_MIN_INTERVAL_MS=0)
    def test_antisnipe_extends_timer_on_late_bid(self):
        now = timezone.now()
        # Only 3s left, anti-sniping window is 10s.
        auction = make_live_auction(
            antisnipe_seconds=10,
            ends_at=now + timedelta(seconds=3),
        )
        p = Participant.objects.create(display_name="Sniper")
        result = services.place_bid(auction.id, p.id, 50)
        self.assertTrue(result.accepted)
        self.assertTrue(result.extended)
        auction.refresh_from_db()
        # ends_at must now be ~10s in the future, not ~3s.
        self.assertGreater((auction.ends_at - now).total_seconds(), 9)

    @override_settings(BID_MIN_INTERVAL_MS=0)
    def test_antisnipe_does_not_extend_early_bid(self):
        now = timezone.now()
        auction = make_live_auction(
            antisnipe_seconds=10,
            ends_at=now + timedelta(seconds=55),  # plenty of time left
        )
        p = Participant.objects.create(display_name="Early")
        result = services.place_bid(auction.id, p.id, 50)
        self.assertTrue(result.accepted)
        self.assertFalse(result.extended)

    @override_settings(BID_MIN_INTERVAL_MS=0)
    def test_cancel_bid_recomputes_price(self):
        auction = make_live_auction()
        a = Participant.objects.create(display_name="Carol")
        b = Participant.objects.create(display_name="Dave")
        r1 = services.place_bid(auction.id, a.id, 50)   # 150
        r2 = services.place_bid(auction.id, b.id, 50)   # 200 — now leading
        result = services.cancel_bid(r2.bid.id)
        self.assertTrue(result["ok"])
        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("150"))
        self.assertEqual(auction.best_bid_id, r1.bid.id)

    @override_settings(BID_MIN_INTERVAL_MS=0)
    def test_cancel_bid_on_a_resolved_round_actually_undoes_the_sale(self):
        """Regression: cancelling the winning bid of an already-assigned
        round used to mark it "annullata" in the log while leaving the
        player sold and the credits spent — indistinguishable, from the
        regia's side, from a broken assignment. It must now really undo it:
        refund, free the player, un-assign the round."""
        player = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(player=player, flow_mode=Auction.FlowMode.CONTINUOUS)
        p = Participant.objects.create(display_name="Carol", credits=Decimal("1000"))
        result = services.place_bid(auction.id, p.id, 50)   # 150
        services.close_auction(auction.id)                  # resolves cycle 1

        cancel_result = services.cancel_bid(result.bid.id)
        self.assertTrue(cancel_result["ok"])

        player.refresh_from_db()
        p.refresh_from_db()
        self.assertIsNone(player.owner_id)
        self.assertEqual(p.spent_credits, Decimal("0"))
        cycle_result = AuctionCycleResult.objects.get(auction=auction, cycle=1)
        self.assertFalse(cycle_result.assigned)
        # A continuous/manual running order gets the freed player back —
        # otherwise it can never be called again.
        self.assertTrue(
            AuctionQueueItem.objects.filter(auction=auction, player=player, done=False).exists()
        )

    @override_settings(BID_MIN_INTERVAL_MS=0)
    def test_cancel_bid_refused_for_a_non_winning_bid_in_an_open_round(self):
        """Only the current leader can be cancelled — an earlier, now-outbid
        offer in the same still-open round carries no consequence of its
        own, so there is nothing to undo by cancelling it."""
        auction = make_live_auction()
        a = Participant.objects.create(display_name="Carol", credits=Decimal("1000"))
        b = Participant.objects.create(display_name="Dave", credits=Decimal("1000"))
        r1 = services.place_bid(auction.id, a.id, 50)   # 150 — outbid next
        services.place_bid(auction.id, b.id, 50)        # 200 — now leading

        result = services.cancel_bid(r1.bid.id)
        self.assertEqual(result, {"ok": False, "error": "not_winning_bid"})
        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("200"))  # untouched

    @override_settings(BID_MIN_INTERVAL_MS=0)
    def test_cancel_bid_on_an_old_unresolved_round_never_touches_the_live_price(self):
        """A stray cancel attempt on a *past* (never resolved into a sale)
        round's bid must be refused — it isn't the live leader — and must
        never resurrect that bid as the price/leader of whatever round is
        live now."""
        auction = make_live_auction(current_cycle=2)
        a = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        b = Participant.objects.create(display_name="B", credits=Decimal("1000"))

        old_bid = Bid.objects.create(
            auction=auction, participant=a, amount=Decimal("999"), increment=Decimal("899"),
            accepted=True, cycle=1, server_received_at=timezone.now(),
        )
        live_bid = Bid.objects.create(
            auction=auction, participant=b, amount=Decimal("110"), increment=Decimal("10"),
            accepted=True, cycle=2, server_received_at=timezone.now(),
        )
        auction.best_bid = live_bid
        auction.current_price = Decimal("110")
        auction.save()

        result = services.cancel_bid(old_bid.id)
        self.assertEqual(result, {"ok": False, "error": "not_winning_bid"})

        auction.refresh_from_db()
        self.assertEqual(auction.current_price, Decimal("110"))
        self.assertEqual(auction.best_bid_id, live_bid.id)


@override_settings(BID_MIN_INTERVAL_MS=0)
class AdminCancelBidViewTests(TestCase):
    """HTTP contract for the "storico offerte" panel's Annulla button."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.client.force_login(self.user)

    def test_cancel_on_a_resolved_round_frees_the_player(self):
        player = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(player=player)
        p = Participant.objects.create(display_name="Carol", credits=Decimal("1000"))
        result = services.place_bid(auction.id, p.id, 50)
        services.close_auction(auction.id)

        r = self.client.post(f"/admin-auction/bid/{result.bid.id}/cancel/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        player.refresh_from_db()
        p.refresh_from_db()
        self.assertIsNone(player.owner_id)
        self.assertEqual(p.spent_credits, Decimal("0"))

    def test_cancel_refused_for_a_non_winning_bid(self):
        auction = make_live_auction()
        a = Participant.objects.create(display_name="Carol", credits=Decimal("1000"))
        b = Participant.objects.create(display_name="Dave", credits=Decimal("1000"))
        r1 = services.place_bid(auction.id, a.id, 50)   # outbid next
        services.place_bid(auction.id, b.id, 50)

        r = self.client.post(f"/admin-auction/bid/{r1.bid.id}/cancel/")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "not_winning_bid")

    def test_cancel_succeeds_on_the_still_open_round(self):
        auction = make_live_auction()
        p = Participant.objects.create(display_name="Carol", credits=Decimal("1000"))
        result = services.place_bid(auction.id, p.id, 50)

        r = self.client.post(f"/admin-auction/bid/{result.bid.id}/cancel/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        auction.refresh_from_db()
        self.assertIsNone(auction.best_bid_id)


class AnnounceViewTests(TestCase):
    """HTTP contract for the regista announcement endpoint."""

    def _live(self):
        return Auction.objects.create(
            title="A", status=Auction.Status.LIVE, current_price=Decimal("1"),
            min_increment=Decimal("1"),
        )

    def test_empty_text_rejected(self):
        a = self._live()
        resp = self.client.post(f"/admin-auction/{a.id}/announce/", {"text": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_level_clamped_to_known_values(self):
        a = self._live()
        resp = self.client.post(f"/admin-auction/{a.id}/announce/",
                                {"text": "ciao", "level": "hacker"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["level"], "info")  # unknown → info

    def test_text_truncated_to_140(self):
        a = self._live()
        resp = self.client.post(f"/admin-auction/{a.id}/announce/", {"text": "x" * 300})
        self.assertEqual(len(resp.json()["text"]), 140)


@override_settings(BID_MIN_INTERVAL_MS=0)
class RegistaControlTests(TestCase):
    """Regista live controls: timer adjust + bid-on-behalf."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.league = League.objects.create(name="L", budget=Decimal("500"))
        self.p = Participant.objects.create(display_name="Eve", league=self.league,
                                            credits=Decimal("500"))
        self.player = Player.objects.create(name="Kvara", role="A", league=self.league,
                                            initial_price=Decimal("10"))
        self.auction = make_live_auction(
            league=self.league, player=self.player, current_price=Decimal("10"),
            min_increment=Decimal("1"), quick_increments="1,5,10",
        )

    def test_adjust_timer_extends(self):
        base = self.auction.ends_at
        a = services.adjust_timer(self.auction.id, 30)
        self.assertGreater(a.ends_at, base)

    def test_adjust_timer_floors_at_one_second(self):
        a = services.adjust_timer(self.auction.id, -100000)
        self.assertGreaterEqual((a.ends_at - timezone.now()).total_seconds(), 0.5)

    def test_adjust_timer_noop_without_clock(self):
        Auction.objects.filter(pk=self.auction.id).update(ends_at=None)
        a = services.adjust_timer(self.auction.id, 30)
        self.assertIsNone(a.ends_at)
        self.assertFalse(a.timer_changed)   # and it says so, rather than faking success

    def test_adjust_timer_reports_change(self):
        a = services.adjust_timer(self.auction.id, 30)
        self.assertTrue(a.timer_changed)

    def test_adjust_timer_view_refuses_when_clock_not_running(self):
        """The console must not flash '+30s' when there is no clock to move."""
        Auction.objects.filter(pk=self.auction.id).update(ends_at=None)
        r = self.client.post(f"/admin-auction/{self.auction.id}/timer/", {"delta": "30"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "timer_not_running")

    def test_adjust_timer_paused_shifts_remaining(self):
        Auction.objects.filter(pk=self.auction.id).update(
            status=Auction.Status.PAUSED, remaining_seconds=20, ends_at=None)
        a = services.adjust_timer(self.auction.id, 10)
        self.assertEqual(a.remaining_seconds, 30)

    def test_force_close_lot_moves_ends_at_to_now(self):
        a = services.force_close_lot(self.auction.id)
        self.assertIsNone(a.force_close_error)
        self.assertLessEqual((timezone.now() - a.ends_at).total_seconds(), 1)

    def test_force_close_lot_noop_without_a_running_clock(self):
        Auction.objects.filter(pk=self.auction.id).update(ends_at=None)
        a = services.force_close_lot(self.auction.id)
        self.assertEqual(a.force_close_error, "timer_not_running")

    def test_force_close_lot_noop_when_not_live(self):
        Auction.objects.filter(pk=self.auction.id).update(status=Auction.Status.PAUSED)
        a = services.force_close_lot(self.auction.id)
        self.assertEqual(a.force_close_error, "timer_not_running")

    def test_force_close_lot_refused_once_a_bid_is_standing(self):
        """Skipping is only for a lot nobody has offered on yet — cutting
        the clock short with a bid already in must not cheat every other
        team out of the time they still had left to raise it."""
        result = services.place_bid(self.auction.id, self.p.id, 5)   # 15
        self.assertTrue(result.accepted)
        before = self.auction.ends_at

        a = services.force_close_lot(self.auction.id)
        self.assertEqual(a.force_close_error, "bid_in_progress")
        a.refresh_from_db()
        self.assertEqual(a.ends_at, before)   # clock untouched

    def test_force_close_lot_reaches_the_same_closing_path_as_a_real_expiry(self):
        """The whole point: skipping an un-bid lot must log it invenduto and
        advance exactly like natural expiry would — reusing
        close_if_expired/reset_if_closed rather than a separate, untested
        code path."""
        AuctionQueueItem.objects.create(
            auction=self.auction, player=self.player, order=0, done=True,
        )
        nxt = Player.objects.create(name="Kean", role="A", league=self.league)
        AuctionQueueItem.objects.create(auction=self.auction, player=nxt, order=1)
        Auction.objects.filter(pk=self.auction.id).update(
            flow_mode=Auction.FlowMode.CONTINUOUS
        )

        a = services.force_close_lot(self.auction.id)
        self.assertIsNone(a.force_close_error)
        closed = services.close_if_expired(self.auction.id)
        self.assertIsNotNone(closed)
        reset = services.reset_if_closed(self.auction.id)

        self.player.refresh_from_db()
        self.assertIsNone(self.player.owner_id)              # invenduto, not sold
        self.assertEqual(reset.player_id, nxt.id)             # advanced to the next lot

    def test_force_close_lot_view_refuses_when_clock_not_running(self):
        Auction.objects.filter(pk=self.auction.id).update(ends_at=None)
        r = self.client.post(f"/admin-auction/{self.auction.id}/force-close/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "timer_not_running")

    def test_force_close_lot_view_refuses_with_a_bid_standing(self):
        services.place_bid(self.auction.id, self.p.id, 5)
        r = self.client.post(f"/admin-auction/{self.auction.id}/force-close/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "bid_in_progress")

    def test_force_close_lot_view_succeeds(self):
        r = self.client.post(f"/admin-auction/{self.auction.id}/force-close/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.auction.refresh_from_db()
        self.assertLessEqual((timezone.now() - self.auction.ends_at).total_seconds(), 1)

    def test_auto_advance_view_toggles_the_flag(self):
        self.client.force_login(self.user)
        Auction.objects.filter(pk=self.auction.id).update(
            flow_mode=Auction.FlowMode.MANUAL)
        r = self.client.post(f"/admin-auction/{self.auction.id}/auto-advance/", {"on": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["state"]["manual_auto_advance"])
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.manual_auto_advance)

        r = self.client.post(f"/admin-auction/{self.auction.id}/auto-advance/", {"on": "0"})
        self.assertFalse(r.json()["state"]["manual_auto_advance"])
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.manual_auto_advance)

    def test_adjust_timer_view_out_of_range(self):
        self.client.force_login(self.user)
        r = self.client.post(f"/admin-auction/{self.auction.id}/timer/", {"delta": "9999"})
        self.assertEqual(r.status_code, 400)

    def test_bid_for_places_bid_on_behalf(self):
        self.client.force_login(self.user)
        r = self.client.post(f"/admin-auction/{self.auction.id}/bid-for/",
                             {"participant_id": self.p.id, "increment": "5"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["bid"]["amount"], "15.00")
        self.assertEqual(r.json()["bid"]["participant"], "Eve")
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("15"))

    def test_bid_for_rejected_returns_400(self):
        self.client.force_login(self.user)
        Participant.objects.filter(pk=self.p.id).update(credits=Decimal("5"))
        r = self.client.post(f"/admin-auction/{self.auction.id}/bid-for/",
                             {"participant_id": self.p.id, "increment": "1"})
        self.assertEqual(r.status_code, 400)


# ══════════════════════════════════════════════════════════════════════════
# Fase G — added coverage for behaviours the earlier phases shipped but did
# not yet exercise end-to-end.
# ══════════════════════════════════════════════════════════════════════════


@override_settings(BID_MIN_INTERVAL_MS=0)
class CyclicAuctionTests(TestCase):
    """Repair/cyclic auctions: ``reset_if_closed`` advances the cycle and
    charges each cycle's winner exactly once on the natural-expiry path."""

    def _expire(self, auction):
        """Force a live auction past its deadline so it can be closed."""
        Auction.objects.filter(pk=auction.id).update(
            ends_at=timezone.now() - timedelta(seconds=1)
        )

    def test_cyclic_reset_advances_cycle_and_charges_each_winner_once(self):
        auction = make_live_auction(
            starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), ends_at=None,   # waiting for first bid
        )
        a = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        b = Participant.objects.create(display_name="B", credits=Decimal("1000"))

        # --- Cycle 1: A wins at 150 ---
        services.place_bid(auction.id, a.id, 50)   # first bid starts timer → 150
        self._expire(auction)
        self.assertIsNotNone(services.close_if_expired(auction.id))
        reset = services.reset_if_closed(auction.id)
        self.assertIsNotNone(reset)
        self.assertEqual(reset.current_cycle, 2)
        self.assertEqual(reset.status, Auction.Status.LIVE)
        self.assertIsNone(reset.ends_at)                       # back to waiting
        self.assertEqual(reset.current_price, Decimal("100"))  # price reset

        # --- Cycle 2: B wins at 120 ---
        services.place_bid(auction.id, b.id, 20)   # 100 + 20 = 120
        self._expire(auction)
        services.close_if_expired(auction.id)
        services.reset_if_closed(auction.id)

        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.spent_credits, Decimal("150"))
        self.assertEqual(b.spent_credits, Decimal("120"))
        results = list(AuctionCycleResult.objects.filter(auction=auction).order_by("cycle"))
        self.assertEqual([r.cycle for r in results], [1, 2])
        self.assertEqual(results[0].winner_id, a.id)
        self.assertEqual(results[1].winner_id, b.id)

    def test_finalize_expired_charges_and_assigns_without_reset(self):
        """A ticker that never reaches reset_if_closed (disconnect during the
        post-close pause) must still leave the winner charged and the player
        assigned — finalize_expired alone must be enough."""
        won_player = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(
            starting_price=Decimal("100"), current_price=Decimal("100"), ends_at=None,
            player=won_player,
        )
        a = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        services.place_bid(auction.id, a.id, 50)   # 150
        self._expire(auction)
        self.assertIsNotNone(services.close_if_expired(auction.id))

        # No reset_if_closed call here — simulates the winner's tab (and thus
        # the only live ticker) disconnecting right after close_if_expired.
        finalized = services.finalize_expired(auction.id)
        self.assertIsNotNone(finalized)

        a.refresh_from_db()
        won_player.refresh_from_db()
        self.assertEqual(a.spent_credits, Decimal("150"))
        self.assertEqual(won_player.owner_id, a.id)

        # Immediately after closing, the lot is just in its normal post-close
        # pause — not "stuck" yet, so another connection's tick right now
        # must not short-circuit it.
        self.assertIsNone(services.stuck_closed_lot(auction.id))

        # Once the pause has genuinely had time to elapse, a later ticker tick
        # (from any reconnecting client) must be able to detect the lot is
        # still parked and finish advancing it — and must not re-charge the
        # already-finalized cycle.
        Auction.objects.filter(pk=auction.id).update(
            updated_at=timezone.now() - timedelta(seconds=auction.cycle_break_seconds + 5)
        )
        stuck = services.stuck_closed_lot(auction.id)
        self.assertIsNotNone(stuck)
        reset = services.reset_if_closed(auction.id)
        self.assertIsNotNone(reset)
        a.refresh_from_db()
        self.assertEqual(a.spent_credits, Decimal("150"))  # unchanged, no double charge
        self.assertIsNone(services.stuck_closed_lot(auction.id))

    def test_reset_skipped_after_admin_close(self):
        """Admin close nulls ``ends_at`` → the cyclic auto-reset must not fire,
        and the winner stays charged exactly once."""
        auction = make_live_auction()
        p = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)
        services.close_auction(auction.id)                      # ends_at=None, charges once
        self.assertIsNone(services.reset_if_closed(auction.id))  # no auto-reset
        p.refresh_from_db()
        self.assertEqual(p.spent_credits, Decimal("150"))


class CycleBreakTests(TestCase):
    """The pause between one lot and the next is the auction's own setting."""

    def setUp(self):
        self.user = User.objects.create_user("regia2", password="x", is_staff=True,
                                             is_superuser=True)
        self.client.force_login(self.user)
        self.league = League.objects.create(name="Lega")
        self.auction = make_live_auction(league=self.league)

    def test_defaults_to_four_seconds(self):
        self.assertEqual(self.auction.cycle_break_seconds, 4)

    def test_accepts_a_fraction_of_a_second(self):
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title, "cycle_break_seconds": "0.5"})
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 0.5)

    def test_accepts_the_italian_decimal_comma(self):
        """The field renders "0,5" under it-it, so the form posts it back."""
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title, "cycle_break_seconds": "0,5"})
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 0.5)

    def test_junk_keeps_the_stored_value_instead_of_failing_the_save(self):
        self.auction.cycle_break_seconds = 3
        self.auction.save(update_fields=["cycle_break_seconds"])
        r = self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title, "cycle_break_seconds": "due secondi"})
        self.assertEqual(r.status_code, 200)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 3)

    def test_a_negative_pause_floors_at_zero(self):
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title, "cycle_break_seconds": "-5"})
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 0)

    def test_settings_form_stores_it(self):
        r = self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title,
            "duration_seconds": "60",
            "antisnipe_seconds": "10",
            "cycle_break_seconds": "12",
        })
        self.assertEqual(r.status_code, 200)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 12)

    def test_zero_means_go_straight_on(self):
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title, "cycle_break_seconds": "0"})
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 0)

    def test_an_absurd_pause_is_clamped_not_accepted(self):
        """A minute is already a long silence; 9999s would look like a freeze."""
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "title": self.auction.title, "cycle_break_seconds": "9999"})
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.cycle_break_seconds, 60)

    def test_state_carries_it_to_the_clients(self):
        self.auction.cycle_break_seconds = 7
        self.auction.save(update_fields=["cycle_break_seconds"])
        state = services.serialize_state(self.auction)
        self.assertEqual(state["cycle_break_seconds"], 7)


class InterludeTests(TestCase):
    """The gap between two lots, as the big screen counts it down."""

    def setUp(self):
        self.league = League.objects.create(name="Lega")
        self.a = make_live_auction(league=self.league,
                                   flow_mode=Auction.FlowMode.CONTINUOUS)
        self.p1 = Player.objects.create(name="Uno", role="A", league=self.league)
        self.p2 = Player.objects.create(name="Due", role="A", league=self.league)

    def _close_with_one_left(self, *, break_seconds=4, ago=0):
        """Put the auction in the state the ticker leaves it in mid-pause."""
        AuctionQueueItem.objects.create(auction=self.a, player=self.p2, order=2)
        self.a.player = self.p1
        self.a.cycle_break_seconds = break_seconds
        self.a.status = Auction.Status.CLOSED
        self.a.ends_at = timezone.now()
        self.a.save()
        # updated_at is auto_now: stamp it explicitly to simulate elapsed time.
        Auction.objects.filter(pk=self.a.pk).update(
            updated_at=timezone.now() - timedelta(seconds=ago))
        return Auction.objects.get(pk=self.a.pk)

    def test_counts_down_from_the_configured_pause(self):
        a = self._close_with_one_left(break_seconds=6)
        self.assertAlmostEqual(services.serialize_state(a)["interlude_seconds"],
                               6, delta=0.5)

    def test_shrinks_as_the_pause_runs_out(self):
        a = self._close_with_one_left(break_seconds=6, ago=4)
        self.assertAlmostEqual(services.serialize_state(a)["interlude_seconds"],
                               2, delta=0.5)

    def test_never_goes_negative(self):
        a = self._close_with_one_left(break_seconds=4, ago=30)
        self.assertEqual(services.serialize_state(a)["interlude_seconds"], 0)

    def test_absent_while_a_lot_is_running(self):
        self.assertIsNone(services.serialize_state(self.a)["interlude_seconds"])

    def test_absent_when_the_pause_is_off(self):
        a = self._close_with_one_left(break_seconds=0)
        self.assertIsNone(services.serialize_state(a)["interlude_seconds"])

    def test_the_last_lot_is_the_end_not_an_interlude(self):
        """Empty queue: nothing is coming up, so no countdown to a next player."""
        self.a.player = self.p1
        self.a.status = Auction.Status.CLOSED
        self.a.ends_at = timezone.now()
        self.a.save()
        self.assertIsNone(services.serialize_state(self.a)["interlude_seconds"])

    def test_absent_in_call_mode_where_the_admin_sets_the_pace(self):
        a = self._close_with_one_left()
        a.flow_mode = Auction.FlowMode.CALL
        a.save(update_fields=["flow_mode"])
        self.assertIsNone(services.serialize_state(a)["interlude_seconds"])

