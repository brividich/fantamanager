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

class DrawnLetterQueueTests(TestCase):
    """Regolamento §3.1 B — lettura del reparto dalla lettera estratta."""

    def setUp(self):
        self.league = League.objects.create(name="Lega")
        for name in ("Abate", "Cassano", "Materazzi", "Totti", "Zola"):
            Player.objects.create(
                name=name, role="P", league=self.league, initial_price=Decimal("1"))

    def _queue_names(self, auction):
        return [i.player.name for i in
                auction.queue_items.filter(done=False).order_by("order", "id")]

    def test_reading_starts_from_the_drawn_letter_and_wraps(self):
        auction = make_live_auction(
            league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS,
            call_order=Auction.CallOrder.PDCA,
            within_role_order=Auction.WithinRole.LETTER,
        )
        with mock.patch.object(services.random, "choice", return_value="M"):
            services.build_queue(auction)
        auction.refresh_from_db()
        self.assertEqual(auction.drawn_letters["P"], "M")
        self.assertEqual(
            self._queue_names(auction),
            ["Materazzi", "Totti", "Zola", "Abate", "Cassano"],
        )

    def test_the_drawn_letter_survives_a_queue_rebuild(self):
        auction = make_live_auction(
            league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS,
            call_order=Auction.CallOrder.PDCA,
            within_role_order=Auction.WithinRole.LETTER,
        )
        with mock.patch.object(services.random, "choice", return_value="T"):
            services.build_queue(auction)
        with mock.patch.object(services.random, "choice", return_value="A"):
            services.build_queue(auction)
        auction.refresh_from_db()
        self.assertEqual(auction.drawn_letters["P"], "T")
        self.assertEqual(self._queue_names(auction)[0], "Totti")


@override_settings(BID_MIN_INTERVAL_MS=0)
class AuctionQueueTests(TestCase):
    """Auction running order: call orders, role banding, within-role, churn."""

    def _free(self, name, role, price=1):
        return Player.objects.create(
            name=name, role=role, team="X",
            initial_price=Decimal(str(price)), owner=None,
        )

    def test_build_queue_alphabetical(self):
        self._free("Zaza", "A"); self._free("Abate", "D"); self._free("Mertens", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        n = services.build_queue(a)
        self.assertEqual(n, 3)
        names = list(a.queue_items.order_by("order").values_list("player__name", flat=True))
        self.assertEqual(names, ["Abate", "Mertens", "Zaza"])

    def test_build_queue_by_role_groups_pdca(self):
        self._free("Att", "A"); self._free("Por", "P")
        self._free("Cen", "C"); self._free("Dif", "D")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        roles = list(a.queue_items.order_by("order").values_list("role", flat=True))
        self.assertEqual(roles, ["P", "D", "C", "A"])

    def test_start_puts_first_player_on_block(self):
        self._free("Abate", "D"); self._free("Zaza", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertEqual(a.player.name, "Abate")
        self.assertEqual(a.queue_items.filter(done=False).count(), 1)  # Zaza left

    def test_reset_advances_then_closes_when_empty(self):
        p1 = self._free("Abate", "D", 5); p2 = self._free("Zaza", "A", 5)
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db(); self.assertEqual(a.player_id, p1.id)

        # First cycle: someone wins p1, timer expires -> advance to p2.
        bidder = Participant.objects.create(display_name="Bob", credits=Decimal("500"))
        services.place_bid(a.id, bidder.id, 10)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.close_if_expired(a.id)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.reset_if_closed(a.id)
        a.refresh_from_db()
        self.assertEqual(a.player_id, p2.id)
        p1.refresh_from_db(); self.assertEqual(p1.owner_id, bidder.id)  # p1 assigned

        # Second cycle: p2 is also won, queue now empty -> auction closes.
        services.place_bid(a.id, bidder.id, 10)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.close_if_expired(a.id)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.reset_if_closed(a.id)
        a.refresh_from_db()
        self.assertEqual(a.status, Auction.Status.CLOSED)
        p2.refresh_from_db(); self.assertEqual(p2.owner_id, bidder.id)

    # --- MANUAL with self-advance on ("scorri anche senza rilanci") ---------

    def _manual_auto(self, **kwargs):
        return make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                                 manual_auto_advance=True,
                                 call_order=Auction.CallOrder.ALPHA,
                                 status=Auction.Status.READY, **kwargs)

    def test_manual_auto_advance_arms_timer_at_start(self):
        """The opposite of plain manual: the first lot goes up already ticking."""
        self._free("Aaa", "D"); self._free("Bbb", "A")
        a = self._manual_auto()
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertIsNotNone(a.ends_at)

    def test_manual_auto_advance_rolls_on_with_no_bid_at_all(self):
        """Nobody bids: the lot expires, goes invenduto and the queue moves on."""
        first = self._free("Aaa", "D"); second = self._free("Bbb", "A")
        a = self._manual_auto()
        services.build_queue(a)
        services.start_auction(a.id)

        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.close_if_expired(a.id)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.reset_if_closed(a.id)

        a.refresh_from_db(); first.refresh_from_db()
        self.assertEqual(a.player_id, second.id)      # advanced by itself
        self.assertIsNone(first.owner_id)             # nobody bought it
        self.assertIsNotNone(a.ends_at)               # and the new lot is ticking
        # Unsold lots come back later in the run, as in continuous play.
        self.assertTrue(a.queue_items.filter(player=first, done=False).exists())

    def test_manual_without_auto_advance_never_rolls_on(self):
        """Default manual: no clock, so an un-bid lot just waits for the regia."""
        first = self._free("Aaa", "D"); self._free("Bbb", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertIsNone(a.ends_at)
        self.assertIsNone(services.close_if_expired(a.id))   # nothing to expire
        a.refresh_from_db()
        self.assertEqual(a.player_id, first.id)              # still on the block

    def test_manual_auto_advance_forward_step_arms_the_new_lot(self):
        first = self._free("Aaa", "D"); second = self._free("Bbb", "A")
        a = self._manual_auto()
        services.build_queue(a)
        services.start_auction(a.id)
        services.manual_step(a.id, "next")
        a.refresh_from_db()
        self.assertEqual(a.player_id, second.id)
        self.assertIsNotNone(a.ends_at)
        self.assertIsNone(first.__class__.objects.get(pk=first.id).owner_id)

    def test_set_auto_advance_arms_and_disarms_the_running_lot(self):
        """Flipping the regia switch applies to the player already on the block."""
        self._free("Aaa", "D"); self._free("Bbb", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db(); self.assertIsNone(a.ends_at)

        services.set_auto_advance(a.id, True)
        a.refresh_from_db()
        self.assertTrue(a.manual_auto_advance)
        self.assertIsNotNone(a.ends_at)      # the lot starts ticking right away

        services.set_auto_advance(a.id, False)
        a.refresh_from_db()
        self.assertFalse(a.manual_auto_advance)
        self.assertIsNone(a.ends_at)         # and stops again

    def test_set_auto_advance_off_keeps_a_clock_started_by_a_bid(self):
        """That countdown belongs to the bidding, not to the auto-advance."""
        self._free("Aaa", "D"); self._free("Bbb", "A")
        a = self._manual_auto()
        services.build_queue(a)
        services.start_auction(a.id)
        bidder = Participant.objects.create(display_name="Bob", credits=Decimal("500"))
        services.place_bid(a.id, bidder.id, 10)

        services.set_auto_advance(a.id, False)
        a.refresh_from_db()
        self.assertFalse(a.manual_auto_advance)
        self.assertIsNotNone(a.ends_at)

    def test_call_player_sets_block_and_rejects_owned(self):
        free = self._free("Libero", "C")
        owner = Participant.objects.create(display_name="Own")
        owned = Player.objects.create(name="Preso", role="C", owner=owner)
        a = make_live_auction(flow_mode=Auction.FlowMode.CALL,
                              status=Auction.Status.READY)
        self.assertIsNone(services.call_player(a.id, owned.id))   # owned -> rejected
        out = services.call_player(a.id, free.id)
        self.assertIsNotNone(out)
        a.refresh_from_db()
        self.assertEqual(a.player_id, free.id)
        self.assertEqual(a.status, Auction.Status.LIVE)

    def test_released_player_requeued_at_end_of_role(self):
        d1 = self._free("Dife1", "D"); d2 = self._free("Dife2", "D")
        att = self._free("Atta", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        # d1 has been auctioned (consumed from the queue) and bought.
        a.queue_items.filter(player=d1).update(done=True)
        owner = Participant.objects.create(display_name="Own", credits=Decimal("500"))
        d1.owner = owner; d1.cost = Decimal("10"); d1.save()
        # Release d1 -> rejoins at the END of the D band (after d2), before the A.
        services.release_player(d1.id, auction_id=a.id, by_admin=True)
        a.refresh_from_db()
        names = list(a.queue_items.filter(done=False).order_by("order")
                     .values_list("player__name", flat=True))
        self.assertEqual(names, ["Dife2", "Dife1", "Atta"])

    def test_state_exposes_current_player_and_next(self):
        self._free("Abate", "D"); self._free("Zaza", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        state = services.serialize_state(a)
        self.assertEqual(state["player"]["name"], "Abate")
        self.assertEqual(state["queue_next"]["name"], "Zaza")
        self.assertEqual(state["queue_pending"], 1)

    def test_build_queue_acdp_inverts_role_order(self):
        self._free("Att", "A"); self._free("Por", "P")
        self._free("Cen", "C"); self._free("Dif", "D")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.ACDP,
                              status=Auction.Status.READY)
        services.build_queue(a)
        roles = list(a.queue_items.order_by("order").values_list("role", flat=True))
        self.assertEqual(roles, ["A", "C", "D", "P"])

    def test_within_role_order_quota_high_to_low(self):
        self._free("Cheap", "D", 5)
        self._free("Pricey", "D", 30)
        self._free("Mid", "D", 12)
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              within_role_order=Auction.WithinRole.QUOTA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        names = list(a.queue_items.order_by("order").values_list("player__name", flat=True))
        self.assertEqual(names, ["Pricey", "Mid", "Cheap"])

    def test_continuous_arms_timer_on_start(self):
        self._free("Solo", "D")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertIsNotNone(a.ends_at)   # presentation timer is running at once

    def test_continuous_unsold_player_requeued_then_advances(self):
        d1 = self._free("Aaa", "D"); d2 = self._free("Bbb", "D")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db(); self.assertEqual(a.player_id, d1.id)

        # d1 receives no bids -> unsold, re-queued at end of its role; advance to d2.
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.close_if_expired(a.id)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.reset_if_closed(a.id)
        a.refresh_from_db()
        self.assertEqual(a.player_id, d2.id)
        pending = list(a.queue_items.filter(done=False).values_list("player_id", flat=True))
        self.assertIn(d1.id, pending)   # d1 awaits another turn

    def test_continuous_unsold_lots_stop_looping_and_the_run_ends(self):
        """Two unsold players in a role must not re-offer each other forever.

        Each is put back up once; after that the lots are parked so the running
        order reaches the next role and the auction can finish.
        """
        self._free("Aaa", "D"); self._free("Bbb", "D")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)

        seen = []
        for _ in range(12):
            Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
            services.close_if_expired(a.id)
            Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
            services.reset_if_closed(a.id)
            a.refresh_from_db()
            seen.append(a.player_id)
            if a.status == Auction.Status.CLOSED:
                break

        self.assertEqual(a.status, Auction.Status.CLOSED,
                         msg=f"non si è chiusa, sequenza lotti: {seen}")
        self.assertFalse(a.queue_items.filter(done=False).exists())

    def test_release_requeue_resets_the_unsold_counter(self):
        """A deliberate re-offer (svincolo) is a fresh chance, not a rebound."""
        d1 = self._free("Aaa", "D")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA)
        item = services.enqueue_released_player(a, d1, unsold=True)
        self.assertEqual(item.unsold_passes, 1)
        item.done = True
        item.save(update_fields=["done"])
        item = services.enqueue_released_player(a, d1)     # released by hand
        self.assertEqual(item.unsold_passes, 0)
        self.assertFalse(item.done)

    def test_continuous_skips_saturated_role_and_sets_notice(self):
        league = League.objects.create(
            name="L", budget=Decimal("500"),
            slots_p=1, slots_d=1, slots_c=1, slots_a=1,
        )
        manager = Participant.objects.create(
            display_name="Mgr", league=league, credits=Decimal("500"), is_active=True,
        )
        d1 = Player.objects.create(name="D1", role="D", league=league,
                                   initial_price=Decimal("1"), owner=None)
        d2 = Player.objects.create(name="D2", role="D", league=league,
                                   initial_price=Decimal("1"), owner=None)
        a1 = Player.objects.create(name="A1", role="A", league=league,
                                   initial_price=Decimal("1"), owner=None)
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              league=league, status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db(); self.assertEqual(a.player_id, d1.id)

        # Manager wins d1 -> the D role (1 slot) is now saturated.
        services.place_bid(a.id, manager.id, 10)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        services.close_if_expired(a.id)
        Auction.objects.filter(pk=a.id).update(ends_at=timezone.now() - timedelta(seconds=1))
        reset = services.reset_if_closed(a.id)

        # d2 (still D, now saturated) is skipped; a1 (A) goes on the block.
        self.assertEqual(reset.player_id, a1.id)
        d2.refresh_from_db()
        self.assertTrue(a.queue_items.get(player=d2).done)
        state = services.serialize_state(reset)
        self.assertEqual(state["notice"], {"from": "D", "to": "A"})

    def test_manual_step_next_finalises_and_prev_recalls(self):
        first = self._free("Aaa", "D"); second = self._free("Bbb", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertEqual(a.player_id, first.id)
        self.assertIsNone(a.ends_at)   # manual flow does not arm a timer

        bidder = Participant.objects.create(display_name="Bob", credits=Decimal("500"))
        services.place_bid(a.id, bidder.id, 10)
        services.manual_step(a.id, "next")
        a.refresh_from_db()
        self.assertEqual(a.player_id, second.id)
        first.refresh_from_db(); self.assertEqual(first.owner_id, bidder.id)

        # The previous lot was knocked down, so stepping back is refused until
        # the admin confirms undoing the sale.
        pending = services.manual_step(a.id, "prev")
        a.refresh_from_db()
        self.assertEqual(a.player_id, second.id)          # block untouched
        first.refresh_from_db()
        self.assertEqual(first.owner_id, bidder.id)       # still sold
        undo = getattr(pending, "needs_undo_confirm", None)
        self.assertIsNotNone(undo)
        self.assertEqual(undo["player_id"], first.id)
        self.assertEqual(undo["winner_name"], "Bob")

        services.manual_step(a.id, "prev", undo_sale=True)
        a.refresh_from_db()
        self.assertEqual(a.player_id, first.id)   # stepped back to the previous lot

    def test_manual_prev_undo_frees_player_and_refunds_exact_price(self):
        """Confirmed step-back reverses the knock-down instead of re-selling."""
        first = self._free("Aaa", "D"); self._free("Bbb", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        bidder = Participant.objects.create(display_name="Bob", credits=Decimal("500"))
        services.place_bid(a.id, bidder.id, 10)
        services.manual_step(a.id, "next")
        first.refresh_from_db(); bidder.refresh_from_db()
        price = first.cost
        self.assertEqual(bidder.spent_credits, price)

        services.manual_step(a.id, "prev", undo_sale=True)
        a.refresh_from_db(); first.refresh_from_db(); bidder.refresh_from_db()
        self.assertEqual(a.player_id, first.id)       # back on the block
        self.assertIsNone(first.owner_id)             # freed
        self.assertEqual(first.cost, Decimal("0"))
        self.assertEqual(bidder.spent_credits, Decimal("0"))   # exact refund
        result = AuctionCycleResult.objects.get(auction=a, player=first)
        self.assertFalse(result.assigned)             # no longer counts as a sale
        self.assertTrue(
            RosterLog.objects.filter(participant=bidder,
                                     note__contains="annullata").exists())

    def test_manual_prev_onto_unsold_lot_needs_no_confirmation(self):
        """A lot that went unsold steps back straight away — nothing to undo."""
        first = self._free("Aaa", "D"); second = self._free("Bbb", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                              call_order=Auction.CallOrder.ALPHA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        services.manual_step(a.id, "next")            # no bids on `first`
        a.refresh_from_db()
        self.assertEqual(a.player_id, second.id)

        stepped = services.manual_step(a.id, "prev")
        a.refresh_from_db()
        self.assertIsNone(getattr(stepped, "needs_undo_confirm", None))
        self.assertEqual(a.player_id, first.id)

    def test_manual_step_jumps_by_role(self):
        # Two P, then a D and an A — PDCA so the queue is P,P,D,A.
        p1 = self._free("Pp1", "P"); p2 = self._free("Pp2", "P")
        d1 = self._free("Dd1", "D"); a1 = self._free("Aa1", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.MANUAL,
                              call_order=Auction.CallOrder.PDCA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertEqual(a.player_id, p1.id)

        # next_role skips the rest of the P band straight to the first D.
        services.manual_step(a.id, "next_role")
        a.refresh_from_db()
        self.assertEqual(a.player_id, d1.id)
        self.assertFalse(a.queue_items.get(player=p2).done)  # skipped, still pending

        # next_role again lands on the A.
        services.manual_step(a.id, "next_role")
        a.refresh_from_db()
        self.assertEqual(a.player_id, a1.id)

        # prev_role recalls the previous role (D).
        services.manual_step(a.id, "prev_role")
        a.refresh_from_db()
        self.assertEqual(a.player_id, d1.id)

    def test_manual_step_controls_continuous_queue(self):
        first = self._free("Aaa", "D"); second = self._free("Bbb", "A")
        a = make_live_auction(flow_mode=Auction.FlowMode.CONTINUOUS,
                              call_order=Auction.CallOrder.PDCA,
                              status=Auction.Status.READY)
        services.build_queue(a)
        services.start_auction(a.id)
        a.refresh_from_db()
        self.assertEqual(a.player_id, first.id)

        # A forward step in CONTINUOUS advances the queue AND arms the timer.
        services.manual_step(a.id, "next")
        a.refresh_from_db()
        self.assertEqual(a.player_id, second.id)
        self.assertIsNotNone(a.ends_at)              # timer running on the new lot
        # The skipped lot had no bid → recorded invenduto in the storico.
        res = AuctionCycleResult.objects.get(auction=a, cycle=1)
        self.assertFalse(res.assigned)
        self.assertEqual(res.player_name, first.name)

        # A backward recall disarms the timer (waits for a bid).
        services.manual_step(a.id, "prev")
        a.refresh_from_db()
        self.assertEqual(a.player_id, first.id)
        self.assertIsNone(a.ends_at)

    def test_expired_unsold_lot_recorded_in_storico(self):
        p = self._free("Zzz", "P")
        a = make_live_auction(flow_mode=Auction.FlowMode.CALL,
                              status=Auction.Status.READY)
        services.call_player(a.id, p.id)
        a.refresh_from_db()
        cycle = a.current_cycle
        # No bids: expire and run the cyclic reset.
        Auction.objects.filter(pk=a.id).update(
            status=Auction.Status.CLOSED, ends_at=timezone.now() - timedelta(seconds=1))
        services.reset_if_closed(a.id)
        res = AuctionCycleResult.objects.get(auction=a, cycle=cycle)
        self.assertFalse(res.assigned)
        self.assertEqual(res.player_name, p.name)


class QueueManagementFeatureTests(TestCase):
    """Tests for new queue controls (prioritize, postpone, exclude) and unsold policies."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client.force_login(self.user)
        self.league = League.objects.create(name="Test League")
        self.p1 = Player.objects.create(name="Donnarumma", role="P", team="PSG", league=self.league, initial_price=Decimal("20"))
        self.p2 = Player.objects.create(name="Sommer", role="P", team="Inter", league=self.league, initial_price=Decimal("15"))
        self.d1 = Player.objects.create(name="Bastoni", role="D", team="Inter", league=self.league, initial_price=Decimal("18"))
        self.a1 = Player.objects.create(name="Lautaro", role="A", team="Inter", league=self.league, initial_price=Decimal("50"))

    def test_get_queue_preview(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.PDCA)
        services.build_queue(auction)
        preview = services.get_queue_preview(auction, limit=10)
        self.assertTrue(len(preview) >= 3)
        self.assertEqual(preview[0]["name"], "Donnarumma")
        self.assertIn("is_recovery", preview[0])

    def test_get_queue_preview_random_hidden(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.RANDOM)
        services.build_queue(auction)
        preview = services.get_queue_preview(auction, limit=10)
        self.assertEqual(preview, [])

    def test_prioritize_queue_item(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.PDCA)
        services.build_queue(auction)
        # Initially Donnarumma is first, Sommer is second
        next_p = services._next_pending(auction)
        self.assertEqual(next_p.player_id, self.p1.id)

        # Prioritize Lautaro (an Attaccante at the end of the queue)
        services.prioritize_queue_item(auction, self.a1.id)
        next_p = services._next_pending(auction)
        self.assertEqual(next_p.player_id, self.a1.id)

    def test_postpone_pending_queue_item(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.PDCA)
        services.build_queue(auction)
        # Donnarumma is first among P
        self.assertEqual(services._next_pending(auction).player_id, self.p1.id)

        # Postpone Donnarumma -> Sommer should now be first P
        services.postpone_queue_item(auction, self.p1.id)
        next_p = services._next_pending(auction)
        self.assertEqual(next_p.player_id, self.p2.id)

    def test_postpone_player_on_block(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.MANUAL, call_order=Auction.CallOrder.PDCA)
        services.build_queue(auction)
        services.manual_step(auction.id, "next")
        auction.refresh_from_db()
        self.assertEqual(auction.player_id, self.p1.id)

        # Postpone Donnarumma while on block with no bids
        services.postpone_queue_item(auction, self.p1.id)
        auction.refresh_from_db()
        self.assertEqual(auction.player_id, self.p2.id)

    def test_exclude_queue_item(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.PDCA)
        services.build_queue(auction)
        # Exclude Donnarumma
        services.exclude_queue_item(auction, self.p1.id)
        self.assertTrue(auction.queue_items.get(player=self.p1).done)
        self.assertEqual(services._next_pending(auction).player_id, self.p2.id)

    def test_unsold_policy_role_end(self):
        auction = make_live_auction(
            league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS,
            call_order=Auction.CallOrder.PDCA, unsold_policy=Auction.UnsoldPolicy.ROLE_END,
        )
        services.build_queue(auction)
        auction.queue_items.filter(player=self.p1).update(done=True)
        item = services.enqueue_released_player(auction, self.p1, unsold=True)
        self.assertIsNotNone(item)
        # In role band P (band 0)
        self.assertLess(item.order, services._QUEUE_BAND)

    def test_unsold_policy_auction_end(self):
        auction = make_live_auction(
            league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS,
            call_order=Auction.CallOrder.PDCA, unsold_policy=Auction.UnsoldPolicy.AUCTION_END,
        )
        services.build_queue(auction)
        auction.queue_items.filter(player=self.p1).update(done=True)
        item = services.enqueue_released_player(auction, self.p1, unsold=True)
        self.assertIsNotNone(item)
        # In recovery band (_RECOVERY_BAND = 10_000_000)
        self.assertGreaterEqual(item.order, services._RECOVERY_BAND)

    def test_unsold_policy_discard(self):
        auction = make_live_auction(
            league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS,
            call_order=Auction.CallOrder.PDCA, unsold_policy=Auction.UnsoldPolicy.DISCARD,
        )
        services.build_queue(auction)
        auction.queue_items.filter(player=self.p1).update(done=True)
        item = services.enqueue_released_player(auction, self.p1, unsold=True)
        self.assertIsNone(item)
        self.assertTrue(auction.queue_items.get(player=self.p1).done)

    def test_queue_http_endpoints(self):
        auction = make_live_auction(league=self.league, flow_mode=Auction.FlowMode.CONTINUOUS, call_order=Auction.CallOrder.PDCA)
        services.build_queue(auction)

        # GET queue preview
        r = self.client.get(f"/admin-auction/{auction.id}/queue/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertTrue(len(data["items"]) >= 3)

        # POST prioritize
        r = self.client.post(f"/admin-auction/{auction.id}/queue/prioritize/", {"player_id": self.a1.id})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["items"][0]["player_id"], self.a1.id)

        # POST postpone
        r = self.client.post(f"/admin-auction/{auction.id}/queue/postpone/", {"player_id": self.a1.id})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        # POST exclude
        r = self.client.post(f"/admin-auction/{auction.id}/queue/exclude/", {"player_id": self.a1.id})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_edit_auction_saves_unsold_policy(self):
        auction = make_live_auction(league=self.league, unsold_policy=Auction.UnsoldPolicy.ROLE_END)
        r = self.client.post(f"/admin-auction/{auction.id}/edit/", {
            "title": auction.title,
            "unsold_policy": "auction_end",
        })
        self.assertEqual(r.status_code, 200)
        auction.refresh_from_db()
        self.assertEqual(auction.unsold_policy, Auction.UnsoldPolicy.AUCTION_END)

