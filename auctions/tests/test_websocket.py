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
class WebSocketFlowTests(TransactionTestCase):
    """End-to-end test of the realtime bid path through the consumer."""

    async def _connect(self, auction_id, participant_id=None):
        app = URLRouter(websocket_urlpatterns)
        communicator = WebsocketCommunicator(app, f"/ws/auction/{auction_id}/")
        # The consumer reads the bidder identity from the server-side session,
        # never from the client payload — inject it like the auth middleware would.
        communicator.scope["session"] = {"participant_id": participant_id}
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_bid_over_websocket_is_accepted_and_broadcast(self):
        auction = await Auction.objects.acreate(
            title="WS", starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
        )
        participant = await Participant.objects.acreate(display_name="Eve")

        comm = await self._connect(auction.id, participant.id)
        # First frame on connect is the current state.
        state = await comm.receive_json_from()
        self.assertEqual(state["type"], "state")
        self.assertEqual(state["current_price"], "100.00")

        await comm.send_json_to({"action": "bid", "increment": 50})

        # The sender receives a bid_new broadcast, a state update and an ack
        # (order may vary); collect a few frames and assert the outcome.
        types = {}
        for _ in range(3):
            msg = await comm.receive_json_from()
            types[msg["type"]] = msg
        self.assertIn("bid_accepted", types)
        self.assertEqual(types["bid_accepted"]["bid"]["amount"], "150.00")

        auction = await Auction.objects.aget(pk=auction.id)
        self.assertEqual(auction.current_price, Decimal("150"))
        await comm.disconnect()

    async def test_a_burst_of_bids_is_cut_before_the_database(self):
        from ..consumers import BID_BURST
        auction = await self._live_auction("Raffica")
        await Auction.objects.filter(pk=auction.id).aupdate(block_leader_rebid=False)
        p = await Participant.objects.acreate(display_name="Tap", credits=Decimal("100000"))
        comm = await self._connect(auction.id, p.id)
        await comm.receive_json_from()  # initial state
        for _ in range(BID_BURST + 6):
            await comm.send_json_to({"action": "bid", "increment": 10})
        rejected = 0
        while not await comm.receive_nothing(timeout=0.5):
            msg = await comm.receive_json_from()
            if msg["type"] == "bid_rejected" and msg["reason"] == services.Reject.RATE_LIMITED:
                rejected += 1
        self.assertGreaterEqual(rejected, 6)
        self.assertLessEqual(await Bid.objects.filter(participant=p).acount(), BID_BURST)
        await comm.disconnect()

    async def test_junk_frames_do_not_drop_the_connection(self):
        auction = await self._live_auction("Junk")
        p = await Participant.objects.acreate(display_name="Junk")
        comm = await self._connect(auction.id, p.id)
        await comm.receive_json_from()  # initial state
        await comm.send_to(text_data="[1, 2, 3]")
        await comm.send_to(text_data="42")
        await comm.send_to(text_data="non json")
        await comm.send_json_to({"action": "latency_warning", "ping": "tanto"})
        await comm.send_json_to({"action": "latency_warning", "ping": None})
        await comm.send_json_to({"action": "sync"})
        state = await self._await_type(comm, "state")
        self.assertEqual(state["current_price"], "100.00")
        await comm.disconnect()

    async def test_bid_without_session_is_rejected(self):
        auction = await Auction.objects.acreate(
            title="WS2", starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
        )
        comm = await self._connect(auction.id, participant_id=None)
        await comm.receive_json_from()  # initial state
        await comm.send_json_to({"action": "bid", "increment": 50})
        msg = await comm.receive_json_from()
        self.assertEqual(msg["type"], "bid_rejected")
        self.assertEqual(msg["reason"], "no_session")
        await comm.disconnect()

    async def _await_type(self, comm, wanted, tries=6):
        for _ in range(tries):
            msg = await comm.receive_json_from()
            if msg.get("type") == wanted:
                return msg
        self.fail(f"no {wanted!r} frame received")

    async def _live_auction(self, title):
        return await Auction.objects.acreate(
            title=title, starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
        )

    async def test_sealed_envelope_over_the_socket(self):
        """La busta passa dalla stessa socket dei rilanci, ma la risposta
        e' personale: la cifra torna solo a chi l'ha scritta, mentre alla
        stanza arriva soltanto quante buste sono state consegnate."""
        auction = await self._live_auction("Buste")
        p = await Participant.objects.acreate(display_name="Ann", credits=Decimal("1000"))
        await Auction.objects.filter(pk=auction.id).aupdate(
            sealed_bids=True, sealed_round=1, sealed_floor=Decimal("151"),
            sealed_ends_at=timezone.now() + timedelta(seconds=30), ends_at=None,
        )
        bidder = await self._connect(auction.id, p.id)
        watcher = await self._connect(auction.id, None)
        await self._await_type(bidder, "state")

        await bidder.send_json_to({"action": "sealed_bid", "amount": "200"})
        ack = await self._await_type(bidder, "sealed_accepted")
        self.assertEqual(ack["amount"], "200")
        mine = await self._await_type(bidder, "sealed_me")
        self.assertEqual(mine["your_amount"], "200")

        state = await self._await_type(watcher, "state", tries=8)
        while state["sealed"]["submitted"] == 0:
            state = await self._await_type(watcher, "state", tries=8)
        self.assertEqual(state["sealed"]["submitted"], 1)
        self.assertEqual(state["sealed"]["reveal"], [])
        self.assertNotIn("200", json.dumps(state["sealed"]))

        await bidder.disconnect()
        await watcher.disconnect()

    async def test_sealed_envelope_below_the_floor_is_refused(self):
        auction = await self._live_auction("Buste2")
        p = await Participant.objects.acreate(display_name="Bob", credits=Decimal("1000"))
        await Auction.objects.filter(pk=auction.id).aupdate(
            sealed_bids=True, sealed_round=1, sealed_floor=Decimal("151"),
            sealed_ends_at=timezone.now() + timedelta(seconds=30), ends_at=None,
        )
        comm = await self._connect(auction.id, p.id)
        await self._await_type(comm, "state")
        await comm.send_json_to({"action": "sealed_bid", "amount": "100"})
        msg = await self._await_type(comm, "sealed_rejected")
        self.assertEqual(msg["reason"], services.Reject.SEALED_TOO_LOW)
        await comm.disconnect()

    async def test_reaction_broadcasts_to_room(self):
        auction = await self._live_auction("React")
        p = await Participant.objects.acreate(display_name="Ann")
        sender = await self._connect(auction.id, p.id)
        watcher = await self._connect(auction.id, None)
        await sender.receive_json_from()   # state
        await watcher.receive_json_from()  # state

        await sender.send_json_to({"action": "reaction", "emoji": "🔥"})
        msg = await self._await_type(watcher, "reaction")
        self.assertEqual(msg["emoji"], "🔥")
        self.assertEqual(msg["participant"], "Ann")
        await sender.disconnect()
        await watcher.disconnect()

    async def test_reaction_rejects_unknown_emoji(self):
        auction = await self._live_auction("React2")
        comm = await self._connect(auction.id, None)
        await comm.receive_json_from()  # state
        await comm.send_json_to({"action": "reaction", "emoji": "💀"})  # not allow-listed
        # Nothing should come back before the next 2s ticker frame.
        self.assertTrue(await comm.receive_nothing(timeout=0.5))
        await comm.disconnect()

    async def test_announcement_reaches_room(self):
        from django.test import AsyncClient
        auction = await self._live_auction("Ann")
        watcher = await self._connect(auction.id, None)
        await watcher.receive_json_from()  # state

        admin = await User.objects.acreate(username="admin", is_staff=True, is_superuser=True)
        client = AsyncClient()
        await client.aforce_login(admin)
        resp = await client.post(
            f"/admin-auction/{auction.id}/announce/",
            {"text": "Ultima chiamata!", "level": "call"},
        )
        self.assertEqual(resp.status_code, 200)
        msg = await self._await_type(watcher, "announcement")
        self.assertEqual(msg["text"], "Ultima chiamata!")
        self.assertEqual(msg["level"], "call")
        await watcher.disconnect()


@override_settings(TIMER_SYNC_INTERVAL_SECONDS=0.05, BID_MIN_INTERVAL_MS=0)
class TickerRecoveryTests(TransactionTestCase):
    """Exercises the real background ticker inside the consumer (not the
    service functions called directly) — the control-flow layer where the
    "buy a player and it never gets assigned" bug actually lived: a client
    disconnecting during the post-close pause used to kill the one task that
    would ever charge the winner and assign the player."""

    async def _connect(self, auction_id, participant_id=None):
        app = URLRouter(websocket_urlpatterns)
        communicator = WebsocketCommunicator(app, f"/ws/auction/{auction_id}/")
        communicator.scope["session"] = {"participant_id": participant_id}
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_ticker_finalizes_expired_lot_and_charges_winner(self):
        """The plain path: one connection's own ticker closes the lot, charges
        the winner and assigns the player, with no disconnect involved."""
        player = await Player.objects.acreate(name="Falcone", role="P")
        auction = await Auction.objects.acreate(
            title="Tick", starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
            cycle_break_seconds=0, player=player,
            flow_mode=Auction.FlowMode.CALL,
        )
        winner = await Participant.objects.acreate(display_name="Bea", credits=Decimal("1000"))

        comm = await self._connect(auction.id, winner.id)
        await comm.receive_json_from()  # initial state
        await comm.send_json_to({"action": "bid", "increment": 50})
        for _ in range(3):
            msg = await comm.receive_json_from()
            if msg["type"] == "bid_accepted":
                break

        # Force the lot into the past so the connection's own ticker closes
        # it naturally on its next tick.
        await Auction.objects.filter(pk=auction.id).aupdate(
            ends_at=timezone.now() - timedelta(seconds=1)
        )
        await asyncio.sleep(0.3)  # a few 0.05s ticks

        winner_db = await Participant.objects.aget(pk=winner.id)
        player_db = await Player.objects.aget(pk=player.id)
        auction_db = await Auction.objects.aget(pk=auction.id)
        self.assertEqual(winner_db.spent_credits, Decimal("150"))
        self.assertEqual(player_db.owner_id, winner.id)
        self.assertEqual(auction_db.status, Auction.Status.LIVE)  # advanced past the pause
        await comm.disconnect()

    async def test_stuck_lot_recovered_after_disconnect_during_pause(self):
        """The bug scenario: the connection whose ticker closes the lot
        disconnects mid-pause (cycle_break_seconds) — a second, still-
        connected client must recover the lot once the pause has genuinely
        elapsed, without re-charging the winner."""
        player = await Player.objects.acreate(name="Totti", role="A")
        auction = await Auction.objects.acreate(
            title="Stuck", starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
            cycle_break_seconds=0.3, player=player,
            flow_mode=Auction.FlowMode.CALL,
        )
        winner = await Participant.objects.acreate(display_name="Gio", credits=Decimal("1000"))

        closer = await self._connect(auction.id, winner.id)  # will close the lot, then vanish
        await closer.receive_json_from()   # initial state

        await closer.send_json_to({"action": "bid", "increment": 50})
        for _ in range(3):
            msg = await closer.receive_json_from()
            if msg["type"] == "bid_accepted":
                break

        await Auction.objects.filter(pk=auction.id).aupdate(
            ends_at=timezone.now() - timedelta(seconds=1)
        )

        # Give closer's ticker time to close + finalize (charge/assign) and
        # enter its own cycle_break_seconds pause — then cut it off, exactly
        # like a winner closing their tab right after winning.
        await asyncio.sleep(0.15)
        await closer.disconnect()

        winner_db = await Participant.objects.aget(pk=winner.id)
        player_db = await Player.objects.aget(pk=player.id)
        auction_db = await Auction.objects.aget(pk=auction.id)
        self.assertEqual(winner_db.spent_credits, Decimal("150"))  # charged already
        self.assertEqual(player_db.owner_id, winner.id)            # assigned already
        self.assertEqual(auction_db.status, Auction.Status.CLOSED)  # advance step abandoned

        # Now watcher connects to the abandoned room mid-pause
        watcher = await self._connect(auction.id, None)       # connects to recover it
        await watcher.receive_json_from()  # initial state

        # Nothing must "fix" it before the pause has genuinely had time to
        # elapse (grace = cycle_break_seconds + 1s) — a second connection's
        # tick must not short-circuit a lot that is merely still pausing.
        await asyncio.sleep(0.3)
        auction_db = await Auction.objects.aget(pk=auction.id)
        self.assertEqual(auction_db.status, Auction.Status.CLOSED)

        # Once the pause has genuinely elapsed, the watcher's own ticker must
        # recover it — advance past the pause, no double charge.
        await asyncio.sleep(1.1)
        auction_db = await Auction.objects.aget(pk=auction.id)
        winner_db = await Participant.objects.aget(pk=winner.id)
        self.assertEqual(auction_db.status, Auction.Status.LIVE)
        self.assertIsNone(auction_db.player_id)
        self.assertEqual(winner_db.spent_credits, Decimal("150"))  # unchanged — no double charge

        await watcher.disconnect()


class TickerHealthTests(TestCase):
    """serialize_state's ticker_warning — the regia dashboard's "is the
    background ticker actually keeping this auction moving" indicator."""

    def setUp(self):
        from .. import health
        self.health = health

    def test_no_warning_by_default(self):
        auction = make_live_auction()
        self.assertIsNone(services.serialize_state(auction)["ticker_warning"])

    def test_warning_reflects_a_recorded_ticker_error(self):
        auction = make_live_auction()
        self.health.record_ticker_error(auction.id, "database is locked")
        self.addCleanup(self.health.clear_ticker_error, auction.id)
        warning = services.serialize_state(auction)["ticker_warning"]
        self.assertIsNotNone(warning)

    def test_warning_clears_once_the_recorded_error_is_cleared(self):
        auction = make_live_auction()
        self.health.record_ticker_error(auction.id, "boom")
        self.health.clear_ticker_error(auction.id)
        self.assertIsNone(services.serialize_state(auction)["ticker_warning"])

    def test_warning_flags_a_lot_stuck_well_past_its_pause(self):
        auction = make_live_auction(
            status=Auction.Status.CLOSED, ends_at=timezone.now() - timedelta(seconds=1),
            cycle_break_seconds=0,
        )
        Auction.objects.filter(pk=auction.id).update(
            updated_at=timezone.now() - timedelta(seconds=10)
        )
        auction.refresh_from_db()
        self.assertIsNotNone(services.serialize_state(auction)["ticker_warning"])

    def test_no_warning_for_a_lot_still_within_its_normal_pause(self):
        auction = make_live_auction(
            status=Auction.Status.CLOSED, ends_at=timezone.now() - timedelta(seconds=1),
            cycle_break_seconds=30,
        )
        self.assertIsNone(services.serialize_state(auction)["ticker_warning"])

