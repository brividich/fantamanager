"""Lo scheduler non ruba le offerte al ticker della sala.

Il ticker del processo web chiude un lotto solo se era scaduto quando ha
chiesto (``as_of``), dietro alle offerte arrivate prima in coda. Lo scheduler
gira in un altro processo: se chiudesse da solo mentre un'offerta arrivata in
tempo aspetta il database, quell'offerta tornerebbe ``NOT_LIVE``. Quindi il
ticker vivo scrive un battito (``Auction.ticker_seen_at``) e lo scheduler
lascia stare le aste col battito recente; senza ticker chiude, ma con un
margine (``CLOSE_GRACE``) per le offerte ancora in viaggio.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone

from .. import services
from ..models import Auction, League, Participant, Player
from ..services import scheduler
from .common import make_live_auction


class SchedulerAndTickerTests(TransactionTestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega", budget=Decimal("500"))
        self.team = Participant.objects.create(display_name="Eve", league=self.league, credits=Decimal("500"))
        self.player = Player.objects.create(name="Kvara", role="A", league=self.league,
                                            initial_price=Decimal("10"))
        self.auction = make_live_auction(league=self.league, player=self.player, current_price=Decimal("10"),
                                         min_increment=Decimal("1"), quick_increments="1,5,10")

    def _expired(self, seconds_ago, beat_ago=None):
        now = timezone.now()
        Auction.objects.filter(pk=self.auction.id).update(
            ends_at=now - timedelta(seconds=seconds_ago),
            ticker_seen_at=None if beat_ago is None else now - timedelta(seconds=beat_ago))

    def _status(self):
        return Auction.objects.get(pk=self.auction.id).status

    def test_a_live_ticker_keeps_the_scheduler_away(self):
        self._expired(seconds_ago=30, beat_ago=1)
        self.assertEqual(scheduler.run_once()["auctions"], {})
        self.assertEqual(self._status(), Auction.Status.LIVE)

    def test_a_dead_ticker_hands_over_after_the_grace(self):
        self._expired(seconds_ago=1, beat_ago=60)          # scaduto da poco: c'è ancora il margine
        self.assertEqual(scheduler.run_once()["auctions"], {})
        self.assertEqual(self._status(), Auction.Status.LIVE)
        self._expired(seconds_ago=scheduler.CLOSE_GRACE.total_seconds() + 1, beat_ago=60)
        self.assertEqual(scheduler.run_once()["auctions"], {self.auction.id: ["closed"]})
        self.assertEqual(self._status(), Auction.Status.CLOSED)

    def test_a_bid_that_arrived_in_time_wins_over_the_scheduler(self):
        services.place_bid(self.auction.id, self.team.id, 1)
        # Il lotto scade; un'offerta arrivata mezzo secondo prima è ancora in coda.
        self._expired(seconds_ago=1, beat_ago=None)
        ends_at = Auction.objects.get(pk=self.auction.id).ends_at
        scheduler.run_once()                                # passa dello scheduler intanto
        rival = Participant.objects.create(display_name="Bob", league=self.league, credits=Decimal("500"))
        res = services.place_bid(self.auction.id, rival.id, 5,
                                 received_at=ends_at - timedelta(milliseconds=500))
        self.assertTrue(res.accepted, res.reason)
        self.assertEqual(self._status(), Auction.Status.LIVE)

    def test_the_ticker_beats_at_most_every_two_seconds(self):
        now = timezone.now()
        self.assertTrue(scheduler.ticker_heartbeat(self.auction.id, last=None, now=now))
        self.assertFalse(scheduler.ticker_heartbeat(self.auction.id, last=now, now=now + timedelta(seconds=1)))
        self.assertTrue(scheduler.ticker_heartbeat(self.auction.id, last=now, now=now + timedelta(seconds=2)))
        self.assertEqual(Auction.objects.get(pk=self.auction.id).ticker_seen_at, now + timedelta(seconds=2))


class RoomTickerBeatsTests(TransactionTestCase):
    def test_the_room_ticker_writes_its_heartbeat(self):
        from asgiref.sync import async_to_sync
        from django.test import override_settings

        from ..consumers import RoomTicker

        league = League.objects.create(name="Lega", budget=Decimal("500"))
        player = Player.objects.create(name="Kvara", role="A", league=league, initial_price=Decimal("10"))
        auction = make_live_auction(league=league, player=player, current_price=Decimal("10"),
                                    min_increment=Decimal("1"), quick_increments="1,5,10")
        ticker = RoomTicker(auction.id, channel_layer=None, group_name="g")
        with override_settings(FM_TICKER_HEARTBEAT=True):
            async_to_sync(ticker._ticker_tick)()
        self.assertIsNotNone(Auction.objects.get(pk=auction.id).ticker_seen_at)
        # Scaduto, ma col ticker vivo: lo scheduler non lo considera.
        Auction.objects.filter(pk=auction.id).update(ends_at=timezone.now() - timedelta(seconds=30))
        self.assertNotIn(auction.id, scheduler.due_auction_ids())


class SyncAfterSchedulerCloseTests(TransactionTestCase):
    """Il channel layer in memoria non attraversa i processi: dopo una chiusura
    fatta dallo scheduler, il telefono lo sa al primo sync (ogni 4 s) o
    ricollegandosi, perché entrambi leggono lo stato dal database."""

    async def test_sync_and_reconnect_see_the_closed_lot(self):
        from channels.routing import URLRouter
        from channels.testing import WebsocketCommunicator

        from ..routing import websocket_urlpatterns

        league = await League.objects.acreate(name="Lega", budget=Decimal("500"))
        auction = await Auction.objects.acreate(
            title="WS", league=league, starting_price=Decimal("10"), current_price=Decimal("10"),
            min_increment=Decimal("1"), duration_seconds=60, status=Auction.Status.LIVE,
            starts_at=timezone.now(), ends_at=timezone.now() + timedelta(seconds=60))

        async def connect():
            comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/auction/{auction.id}/")
            comm.scope["session"] = {}
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            first = await comm.receive_json_from()
            return comm, first

        comm, first = await connect()
        self.assertEqual(first["status"], "LIVE")
        # Lo scheduler, da un altro processo, chiude il lotto scaduto senza ticker.
        await Auction.objects.filter(pk=auction.id).aupdate(
            ends_at=timezone.now() - timedelta(seconds=30), ticker_seen_at=None)
        from asgiref.sync import sync_to_async
        await sync_to_async(scheduler.auction_tick)(auction.id)
        await comm.send_json_to({"action": "sync", "sync_id": 3})
        while True:
            msg = await comm.receive_json_from(timeout=5)
            if msg.get("sync_id") == 3:
                break
        self.assertEqual(msg["status"], "CLOSED")
        await comm.disconnect()
        comm, first = await connect()
        self.assertEqual(first["status"], "CLOSED")
        await comm.disconnect()
