"""Ripristino di una copia dal Supervisor e aste rimaste a metà."""
import sqlite3
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import django.db
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .. import backup, services
from ..models import Auction, Participant, Player
from ..services.recovery import pending_recovery, recover_auction
from .common import make_live_auction


def _sqlite_file(path, value):
    con = sqlite3.connect(str(path))
    con.execute("create table django_migrations(id integer)")
    con.execute("create table t(x)")
    con.execute("insert into t values (?)", (value,))
    con.commit()
    con.close()


def _value(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("select x from t").fetchone()[0]
    finally:
        con.close()


class RestoreSqliteTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.live = self.dir / "db.sqlite3"
        _sqlite_file(self.live, "oggi")
        (self.dir / "backups").mkdir()
        self.snap = self.dir / "backups" / "db-20261001-200000.sqlite3"
        _sqlite_file(self.snap, "ieri")
        for target, kwargs in (
            ("auctions.backup._db_path", {"return_value": self.live}),
            ("auctions.backup._is_postgres", {"return_value": False}),
            ("django.core.management.call_command", {}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The test database must stay open: closing it would drop it.
        patcher = mock.patch.object(django.db.connections, "close_all")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_restore_puts_the_snapshot_back_and_keeps_the_current_state(self):
        safety = backup.restore_sqlite(self.snap.name)
        self.assertEqual(_value(self.live), "ieri")
        self.assertEqual(_value(safety), "oggi")
        self.assertTrue(safety.name.startswith("db-"))

    def test_corrupt_file_is_refused_and_nothing_changes(self):
        bad = self.dir / "backups" / "db-20261002-200000.sqlite3"
        bad.write_bytes(b"non un database")
        with self.assertRaises(backup.RestoreError):
            backup.restore_sqlite(bad.name)
        self.assertEqual(_value(self.live), "oggi")

    def test_only_a_snapshot_name_inside_the_folder(self):
        for name in ("../db.sqlite3", "db.sqlite3", "/etc/passwd", "", "db-x.sqlite3"):
            with self.subTest(name=name), self.assertRaises(backup.RestoreError):
                backup.restore_sqlite(name)
        self.assertEqual(_value(self.live), "oggi")


class SupervisorRestoreViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("sup", "s@x.it", "pass12345")
        self.client.force_login(self.admin)

    def test_refused_while_an_auction_is_live(self):
        make_live_auction()
        with mock.patch("auctions.backup.restore_sqlite") as restore:
            r = self.client.post("/supervisor/", {"action": "restore_backup",
                                                  "file": "db-20261001-200000.sqlite3"}, follow=True)
        restore.assert_not_called()
        self.assertContains(r, "aste in corso")

    def test_restore_reports_the_safety_copy(self):
        with mock.patch("auctions.backup.restore_sqlite",
                        return_value=Path("db-20261006-210000.sqlite3")) as restore:
            r = self.client.post("/supervisor/", {"action": "restore_backup",
                                                  "file": "db-20261001-200000.sqlite3"}, follow=True)
        restore.assert_called_once_with("db-20261001-200000.sqlite3")
        self.assertContains(r, "db-20261006-210000.sqlite3")

    def test_restore_button_offered_on_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "backups").mkdir()
            _sqlite_file(Path(tmp) / "backups" / "db-20261001-200000.sqlite3", "x")
            with mock.patch("auctions.backup._is_postgres", return_value=False), \
                    mock.patch("auctions.backup._db_path", return_value=Path(tmp) / "db.sqlite3"):
                r = self.client.get("/supervisor/?tab=reports")
        self.assertContains(r, 'value="restore_backup"')


@override_settings(BID_MIN_INTERVAL_MS=0)
class StalledAuctionTests(TestCase):
    def setUp(self):
        self.player = Player.objects.create(name="Osimhen", role="A")
        self.team = Participant.objects.create(display_name="Alfa", credits=Decimal("1000"))
        self.auction = make_live_auction(player=self.player, flow_mode=Auction.FlowMode.CALL,
                                         cycle_break_seconds=0)
        services.place_bid(self.auction.id, self.team.id, 50)
        # Server stopped mid-lot: the lot expired a minute ago, nobody closed it.
        Auction.objects.filter(pk=self.auction.id).update(
            ends_at=timezone.now() - timedelta(minutes=1))

    def test_an_expired_lot_nobody_closed_is_listed(self):
        found = pending_recovery()
        self.assertEqual([(a.id, label) for a, label in found],
                         [(self.auction.id, "Lotto scaduto e mai chiuso")])

    def test_an_auction_a_ticker_is_running_is_not_listed(self):
        self.assertEqual(pending_recovery(exclude_ids=[self.auction.id]), [])

    def test_a_lot_still_running_is_not_listed(self):
        Auction.objects.filter(pk=self.auction.id).update(
            ends_at=timezone.now() + timedelta(seconds=30))
        self.assertEqual(pending_recovery(), [])

    def test_recover_closes_charges_and_moves_on(self):
        auction = recover_auction(self.auction.id)
        self.player.refresh_from_db()
        self.team.refresh_from_db()
        self.assertEqual(self.player.owner_id, self.team.id)
        self.assertEqual(self.team.spent_credits, Decimal("150"))
        self.assertEqual(auction.status, Auction.Status.LIVE)
        self.assertIsNone(auction.player_id)
        self.assertEqual(pending_recovery(), [])
        # Running it again charges nobody twice.
        recover_auction(self.auction.id)
        self.team.refresh_from_db()
        self.assertEqual(self.team.spent_credits, Decimal("150"))

    def test_expired_envelopes_are_opened(self):
        Auction.objects.filter(pk=self.auction.id).update(
            sealed_bids=True, sealed_round=1, sealed_floor=Decimal("151"),
            sealed_ends_at=timezone.now() - timedelta(minutes=1), ends_at=None)
        found = pending_recovery()
        self.assertEqual(found[0][1], "Buste scadute da aprire")
        recover_auction(self.auction.id)
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.sealed_open)

    def test_supervisor_lists_and_recovers(self):
        admin = User.objects.create_superuser("sup2", "s2@x.it", "pass12345")
        self.client.force_login(admin)
        r = self.client.get("/supervisor/?tab=health")
        self.assertContains(r, "Aste rimaste a metà")
        self.assertContains(r, "Lotto scaduto e mai chiuso")
        r = self.client.post("/supervisor/", {"action": "recover_auction",
                                              "auction_id": self.auction.id})
        self.assertEqual(r.status_code, 302)
        self.player.refresh_from_db()
        self.assertEqual(self.player.owner_id, self.team.id)
