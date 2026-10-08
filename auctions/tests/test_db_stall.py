"""Il database che si ferma a metà asta (services/stall.py, consumers.py,
backup.repair_at_startup).

Sul PC tutte le chiamate al database passano da una coda sola: un import
pesante o l'antivirus che blocca il file la fermano. Il lotto non deve
scadere per quei secondi, le offerte arrivate in tempo valgono, il telefono
non perde la connessione e un file rovinato non ferma la serata.
"""
import os
import sqlite3
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.db import OperationalError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .. import backup, health, services
from ..models import Auction, League, Participant, Player
from ..routing import websocket_urlpatterns
from ..services import stall
from .common import make_live_auction


def _forget_stalls():
    stall._MARKS.clear()
    health._STALLS.clear()


@override_settings(BID_MIN_INTERVAL_MS=0)
class StallTests(TestCase):
    def setUp(self):
        _forget_stalls()
        self.addCleanup(_forget_stalls)
        self.a = Participant.objects.create(display_name="Alfa", credits=Decimal("1000"))
        self.b = Participant.objects.create(display_name="Beta", credits=Decimal("1000"))

    def _auction(self, ends_in, **kwargs):
        kwargs.setdefault("antisnipe_seconds", 5)
        return make_live_auction(ends_at=timezone.now() + timedelta(seconds=ends_in), **kwargs)

    def test_an_offer_that_arrived_in_time_counts(self):
        """Arrivata prima della fine, scritta un attimo dopo: vale, e gli
        altri hanno i secondi dell'anti-snipe per rispondere."""
        auction = self._auction(-0.5)
        r = services.place_bid(auction.id, self.a.id, 10,
                               received_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(r.accepted, r.reason)
        auction.refresh_from_db()
        self.assertGreater(auction.ends_at, timezone.now() + timedelta(seconds=4))

    def test_an_offer_that_arrived_late_does_not(self):
        auction = self._auction(-2)
        r = services.place_bid(auction.id, self.a.id, 10,
                               received_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.EXPIRED)

    def test_a_long_stop_gives_the_seconds_back_once(self):
        """Fermo di 10 s con 3 s da giocare: il lotto riparte da 3 s, e chi era
        in coda dietro non li restituisce un'altra volta."""
        now = timezone.now()
        auction = self._auction(-7, antisnipe_seconds=0)
        r = services.place_bid(auction.id, self.a.id, 10, received_at=now - timedelta(seconds=10))
        self.assertTrue(r.accepted, r.reason)
        auction.refresh_from_db()
        left = (auction.ends_at - timezone.now()).total_seconds()
        self.assertTrue(2 < left <= 3.1, left)
        r = services.place_bid(auction.id, self.b.id, 10, received_at=now - timedelta(seconds=9))
        self.assertTrue(r.accepted, r.reason)
        auction.refresh_from_db()
        self.assertTrue(2 < (auction.ends_at - timezone.now()).total_seconds() <= 3.1)
        self.assertIsNotNone(health.recent_stall(auction.id))

    def test_the_ticker_does_not_close_on_time_lost_in_the_queue(self):
        auction = self._auction(-0.5)
        self.assertIsNone(services.close_if_expired(auction.id, as_of=timezone.now() - timedelta(seconds=1)))
        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.LIVE)
        self.assertIsNotNone(services.close_if_expired(auction.id, as_of=timezone.now()))
        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.CLOSED)

    def test_a_long_stop_seen_by_the_ticker_moves_the_timer(self):
        auction = self._auction(-1)
        self.assertIsNone(services.close_if_expired(auction.id, as_of=timezone.now() - timedelta(seconds=5)))
        auction.refresh_from_db()
        self.assertEqual(auction.status, Auction.Status.LIVE)
        self.assertGreater(auction.ends_at, timezone.now() + timedelta(seconds=3))
        state = services.serialize_state(auction)
        self.assertIn("Il database è rimasto fermo", state["ticker_warning"])

    def test_a_short_wait_is_not_a_stop(self):
        auction = self._auction(10)
        before = auction.ends_at
        services.close_if_expired(auction.id, as_of=timezone.now() - timedelta(seconds=0.5))
        auction.refresh_from_db()
        self.assertEqual(auction.ends_at, before)
        self.assertIsNone(health.recent_stall(auction.id))


@override_settings(BID_MIN_INTERVAL_MS=0)
class SealedStallTests(TestCase):
    def setUp(self):
        _forget_stalls()
        self.addCleanup(_forget_stalls)
        league = League.objects.create(name="Lega", budget=Decimal("1000"))
        player = Player.objects.create(name="Bomber", role="A", league=league, initial_price=Decimal("1"))
        self.a = Participant.objects.create(display_name="Alfa", league=league, credits=Decimal("1000"))
        self.auction = make_live_auction(league=league, player=player, sealed_bids=True,
                                         starting_price=Decimal("1"), current_price=Decimal("1"),
                                         enforce_limits=False)
        services.open_sealed_now(self.auction.id)

    def test_an_envelope_that_arrived_in_time_counts(self):
        Auction.objects.filter(pk=self.auction.id).update(
            sealed_ends_at=timezone.now() - timedelta(seconds=0.5))
        r = services.place_sealed_bid(self.auction.id, self.a.id, "20",
                                      received_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(r.accepted, r.reason)

    def test_the_ticker_gives_the_envelopes_their_seconds_back(self):
        Auction.objects.filter(pk=self.auction.id).update(
            sealed_ends_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(services.sealed_tick(self.auction.id, as_of=timezone.now() - timedelta(seconds=5)))
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.sealed_open)
        self.assertGreater(self.auction.sealed_ends_at, timezone.now() + timedelta(seconds=3))


class BusyDatabaseSocketTests(TransactionTestCase):
    """Il database non risponde: la squadra sa che deve rilanciare e il
    telefono resta collegato."""

    async def test_the_phone_stays_connected(self):
        auction = await Auction.objects.acreate(
            title="WS", starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
        )
        team = await Participant.objects.acreate(display_name="Eve")
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/auction/{auction.id}/")
        comm.scope["session"] = {"participant_id": team.id}
        self.assertTrue((await comm.connect())[0])

        async def next_of(kind):
            for _ in range(5):   # all'ingresso arrivano anche lo stato e le buste
                msg = await comm.receive_json_from()
                if msg.get("type") == kind:
                    return msg
            self.fail(f"nessun messaggio {kind}")

        with mock.patch.object(services, "place_bid", side_effect=OperationalError("database is locked")):
            await comm.send_json_to({"action": "bid", "increment": 10})
            msg = await next_of("bid_rejected")
        self.assertEqual(msg["reason"], services.Reject.SERVER_BUSY)
        self.assertIn(services.Reject.SERVER_BUSY, services.ERROR_LABELS)
        await comm.send_json_to({"action": "sync", "sync_id": 7})
        state = await next_of("state")
        while state.get("sync_id") != 7:
            state = await next_of("state")
        await comm.disconnect()


class BusyPhoneTests(TransactionTestCase):
    """Mentre l'offerta di un telefono aspetta il database (tante offerte
    insieme), quel telefono continua a ricevere quello che succede in sala."""

    async def test_the_room_keeps_flowing_while_an_offer_waits(self):
        import asyncio
        import threading
        from channels.layers import get_channel_layer
        auction = await Auction.objects.acreate(
            title="WS", starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"), duration_seconds=60,
            status=Auction.Status.LIVE, starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(seconds=60),
        )
        team = await Participant.objects.acreate(display_name="Eve", credits=Decimal("1000"))
        comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/auction/{auction.id}/")
        comm.scope["session"] = {"participant_id": team.id}
        self.assertTrue((await comm.connect())[0])
        while (await comm.receive_json_from()).get("type") != "state":
            pass
        release = threading.Event()
        real = services.place_bid

        def slow_bid(*args, **kwargs):
            release.wait(5)        # il database è in coda
            return real(*args, **kwargs)

        with mock.patch.object(services, "place_bid", side_effect=slow_bid):
            await comm.send_json_to({"action": "bid", "increment": 10})
            # Uno in corso, uno in attesa: il terzo tocco torna subito indietro.
            await comm.send_json_to({"action": "bid", "increment": 10})
            await comm.send_json_to({"action": "bid", "increment": 10})
            await asyncio.sleep(0.1)
            await get_channel_layer().group_send(
                f"auction_{auction.id}", {"type": "announcement", "text": "Pausa caffè"})
            seen = []
            while not seen or seen[-1].get("type") != "announcement":
                seen.append(await comm.receive_json_from(timeout=2))
            self.assertNotIn("bid_accepted", [m.get("type") for m in seen])
            self.assertIn({"type": "bid_rejected", "reason": services.Reject.BID_PENDING}, seen)
            release.set()
            while (await comm.receive_json_from(timeout=5)).get("type") != "bid_accepted":
                pass
        await comm.disconnect()


class RepairAtStartupTests(TestCase):
    """All'avvio dell'app del PC: database rovinato → l'ultima copia integra."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "db.sqlite3"
        (self.dir / "backups").mkdir()
        patcher = mock.patch.object(backup, "_db_path", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make(self, path, rows=1):
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT)")
        con.executemany("INSERT INTO django_migrations (app) VALUES (?)", [("auctions",)] * rows)
        con.commit()
        con.close()

    def _ruin(self, path):
        with open(path, "r+b") as f:
            f.write(b"\x00" * 100)   # l'intestazione: non è più un database

    def test_intact_database_is_left_alone(self):
        self._make(self.db)
        self.assertIsNone(backup.repair_at_startup())

    def test_ruined_database_gets_the_last_intact_copy(self):
        self._make(self.db)
        self._make(self.dir / "backups" / "db-20261001-200000.sqlite3", rows=3)
        ruined_copy = self.dir / "backups" / "db-20261002-200000.sqlite3"
        self._make(ruined_copy)
        self._ruin(ruined_copy)
        os.utime(ruined_copy, (time.time() + 60, time.time() + 60))   # la più recente, ma rovinata
        self._ruin(self.db)
        msg = backup.repair_at_startup()
        self.assertIn("Ho rimesso l'ultima copia integra", msg)
        con = sqlite3.connect(str(self.db))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM django_migrations").fetchone()[0], 3)
        con.close()
        self.assertEqual(len(list((self.dir / "backups").glob("danneggiato-*.sqlite3"))), 1)

    def test_without_an_intact_copy_nothing_is_touched(self):
        self._make(self.db)
        self._ruin(self.db)
        msg = backup.repair_at_startup()
        self.assertIn("non c'è una copia integra", msg)
        self.assertTrue(self.db.exists())
        self.assertEqual(list((self.dir / "backups").glob("danneggiato-*")), [])
