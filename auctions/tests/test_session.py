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

class SessionTests(TestCase):
    """Fase E — save/resume sessions (snapshot + non-destructive rebuild)."""

    def _league_with_team(self):
        league = League.objects.create(name="Lega", budget=Decimal("500"),
                                       slots_p=1, slots_d=1, slots_c=1, slots_a=1)
        p = Participant.objects.create(
            league=league, display_name="GELSI", external_team_id="1449",
            credits=Decimal("500"), spent_credits=Decimal("9"),
        )
        Player.objects.create(name="Falcone", role="P", team="Lecce",
                              cost=Decimal("9"), owner=p)
        auction = Auction.objects.create(
            league=league, title="Asta Lega", mode=Auction.Mode.REPAIR_AUCTION,
            min_increment=Decimal("2"), quick_increments="1,2,5",
            duration_seconds=45, enforce_limits=True, current_cycle=3,
            status=Auction.Status.LIVE,
        )
        return league, p, auction

    def test_save_session_snapshots_standings(self):
        league, p, auction = self._league_with_team()
        session = services.save_session(auction.id, name="Backup1", created_by="admin")

        self.assertEqual(session.source_auction_id, auction.id)
        self.assertEqual(session.league_id, league.id)
        self.assertEqual(session.current_cycle, 3)
        self.assertEqual(session.participant_count, 1)
        snap = session.data["participants"][0]
        self.assertEqual(snap["display_name"], "GELSI")
        self.assertEqual(snap["external_team_id"], "1449")
        self.assertEqual(snap["spent_credits"], "9.00")
        self.assertEqual(len(snap["roster"]), 1)
        self.assertEqual(snap["roster"][0]["role"], "P")
        self.assertEqual(session.data["auction"]["duration_seconds"], 45)

    def test_resume_session_rebuilds_fresh_playable_auction(self):
        _, _, auction = self._league_with_team()
        session = services.save_session(auction.id, name="Backup1", created_by="admin")

        new_auction = services.resume_session(session.id, created_by="admin")

        self.assertEqual(new_auction.mode, Auction.Mode.RESUME_SAVED)
        self.assertEqual(new_auction.resumed_from_session_id, session.id)
        self.assertEqual(new_auction.status, Auction.Status.READY)
        self.assertEqual(new_auction.current_cycle, 3)
        self.assertEqual(new_auction.duration_seconds, 45)

        # Fresh, separate league + participant rebuilt from the snapshot.
        self.assertNotEqual(new_auction.league_id, session.league_id)
        rebuilt = Participant.objects.get(league=new_auction.league, display_name="GELSI")
        self.assertEqual(rebuilt.credits, Decimal("500"))
        self.assertEqual(rebuilt.spent_credits, Decimal("9"))
        self.assertEqual(rebuilt.external_team_id, "1449")
        self.assertEqual(Player.objects.filter(owner=rebuilt, role="P").count(), 1)

    def test_resume_does_not_mutate_originals(self):
        league, p, auction = self._league_with_team()
        session = services.save_session(auction.id, created_by="admin")
        participants_before = Participant.objects.count()
        services.resume_session(session.id, created_by="admin")
        # Originals untouched; resume only adds new rows.
        p.refresh_from_db()
        self.assertEqual(p.league_id, league.id)
        self.assertEqual(Participant.objects.count(), participants_before + 1)

    def test_save_session_legacy_no_league(self):
        """An auction without a league snapshots all participants (legacy)."""
        Participant.objects.create(display_name="Solo", credits=Decimal("300"))
        auction = Auction.objects.create(title="Libera", status=Auction.Status.LIVE)
        session = services.save_session(auction.id)
        self.assertIsNone(session.league_id)
        self.assertEqual(session.participant_count, 1)


class SessionViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.client.force_login(self.user)

    def test_sessions_page_loads(self):
        resp = self.client.get("/admin-auction/sessions/")
        self.assertEqual(resp.status_code, 200)

    def test_save_and_resume_via_views(self):
        league = League.objects.create(name="Lega", budget=Decimal("400"))
        Participant.objects.create(league=league, display_name="Alfa", credits=Decimal("400"))
        auction = Auction.objects.create(league=league, title="Asta", status=Auction.Status.LIVE)

        resp = self.client.post(f"/admin-auction/{auction.id}/save-session/", {"name": "Snap"})
        self.assertEqual(resp.status_code, 302)
        from ..models import AuctionSession
        session = AuctionSession.objects.get(name="Snap")

        resp2 = self.client.post(f"/admin-auction/sessions/{session.id}/resume/")
        self.assertEqual(resp2.status_code, 302)
        self.assertTrue(
            Auction.objects.filter(mode=Auction.Mode.RESUME_SAVED, resumed_from_session=session).exists()
        )


class BackupTests(TestCase):
    """auctions/backup.py in isolation — a temp sqlite file stands in for the
    real database so these never touch the actual test-run connection."""

    def setUp(self):
        from .. import backup
        self.backup = backup
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.db_path = self.data_dir / "db.sqlite3"
        con = sqlite3.connect(str(self.db_path))
        con.execute("create table t(x)")
        con.execute("insert into t values (1)")
        con.commit()
        con.close()
        self._patch_db_path(self.db_path)
        # backup_database_async's throttle is module-global — never let one
        # test's timestamp bleed into the next.
        self.addCleanup(setattr, backup, "_last_bg_backup", 0.0)
        backup._last_bg_backup = 0.0

    def _patch_db_path(self, path):
        patcher = mock.patch.object(self.backup, "_db_path", return_value=path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backup_writes_a_consistent_snapshot(self):
        dst = self.backup.backup_database()
        self.assertIsNotNone(dst)
        self.assertTrue(dst.exists())
        con = sqlite3.connect(str(dst))
        self.assertEqual(con.execute("select * from t").fetchall(), [(1,)])
        con.close()

    def test_backup_rotates_to_keep_only_the_newest(self):
        backups_dir = self.data_dir / "backups"
        backups_dir.mkdir()
        # Pre-seed 12 fake backups with distinct mtimes, oldest first.
        import os
        import time as time_mod
        for i in range(12):
            f = backups_dir / f"db-fake{i:02d}.sqlite3"
            f.write_text("x")
            t = time_mod.time() - (12 - i)
            os.utime(f, (t, t))
        self.backup.backup_database(keep=10)
        remaining = sorted((backups_dir).glob("db-*.sqlite3"))
        self.assertEqual(len(remaining), 10)
        # The freshest of the fakes (fake11) must have survived the prune.
        self.assertTrue((backups_dir / "db-fake11.sqlite3").exists())
        self.assertFalse((backups_dir / "db-fake00.sqlite3").exists())

    def test_backup_is_a_noop_when_the_db_file_is_missing(self):
        self._patch_db_path(self.data_dir / "does-not-exist.sqlite3")
        self.assertIsNone(self.backup.backup_database())

    def test_backup_is_a_noop_for_a_non_sqlite_engine(self):
        with mock.patch.object(self.backup, "_db_path", return_value=None):
            self.assertIsNone(self.backup.backup_database())

    def test_async_throttles_bursts_and_coalesces_to_one_thread(self):
        started = []
        real_thread = threading.Thread

        def _spy(*a, **k):
            t = real_thread(*a, **k)
            started.append(t)
            return t

        with mock.patch("auctions.backup.threading.Thread", side_effect=_spy):
            self.backup.backup_database_async(min_interval=60)
            self.backup.backup_database_async(min_interval=60)  # too soon — coalesced
            self.backup.backup_database_async(min_interval=60)  # too soon — coalesced
        self.assertEqual(len(started), 1)
        started[0].join(timeout=2)

    def test_async_runs_again_once_the_interval_has_passed(self):
        with mock.patch("auctions.backup.threading.Thread") as MockThread:
            self.backup.backup_database_async(min_interval=0)
            self.backup.backup_database_async(min_interval=0)
        self.assertEqual(MockThread.call_count, 2)


@override_settings(BID_MIN_INTERVAL_MS=0)
class AuctionConclusionBackupTests(TestCase):
    """The three genuine "asta conclusa" moments must each trigger a backup
    — but only once the transaction actually commits, and never eagerly for
    every ordinary lot in between."""

    def setUp(self):
        patcher = mock.patch("auctions.services.backup_database_async")
        self.mock_backup = patcher.start()
        self.addCleanup(patcher.stop)

    def test_admin_close_auction_triggers_a_backup(self):
        auction = make_live_auction()
        p = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)
        with self.captureOnCommitCallbacks(execute=True):
            services.close_auction(auction.id)
        self.mock_backup.assert_called_once()

    def test_continuous_flow_running_out_of_players_triggers_a_backup(self):
        auction = make_live_auction(
            flow_mode=Auction.FlowMode.CONTINUOUS, ends_at=None,
        )
        # No queue items pending: the very first cyclic reset finds nothing
        # left to put up next and closes the whole auction.
        p = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)
        Auction.objects.filter(pk=auction.id).update(
            ends_at=timezone.now() - timedelta(seconds=1)
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.assertIsNotNone(services.close_if_expired(auction.id))
            reset = services.reset_if_closed(auction.id)
        self.assertEqual(reset.status, Auction.Status.CLOSED)
        self.mock_backup.assert_called_once()

    def test_manual_step_running_out_of_players_triggers_a_backup(self):
        auction = make_live_auction(flow_mode=Auction.FlowMode.MANUAL)
        with self.captureOnCommitCallbacks(execute=True):
            result = services.manual_step(auction.id, "next")
        self.assertEqual(result.status, Auction.Status.CLOSED)
        self.mock_backup.assert_called_once()

    def test_an_ordinary_lot_closing_does_not_trigger_a_backup(self):
        """Only the whole-auction-is-over moments back up here — the every-
        5-minutes safety net for everything in between lives in the ticker
        (auctions/consumers.py), not in the service layer."""
        player = Player.objects.create(name="Falcone", role="P")
        auction = make_live_auction(
            flow_mode=Auction.FlowMode.CALL, cycle_break_seconds=0, player=player,
        )
        p = Participant.objects.create(display_name="A", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)
        Auction.objects.filter(pk=auction.id).update(
            ends_at=timezone.now() - timedelta(seconds=1)
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.assertIsNotNone(services.close_if_expired(auction.id))
            services.reset_if_closed(auction.id)
        self.mock_backup.assert_not_called()


class ResumeKeepsTheListoneTests(TestCase):
    """A resumed session must come back with a playable listone.

    The snapshot used to carry only the teams' rosters, so the league it
    rebuilt had no free agents — and ``start_auction`` refuses to start without
    a pool. The auction was resumable but could never be run.
    """

    def setUp(self):
        self.league = League.objects.create(name="Lega", budget=Decimal("500"))
        self.team = Participant.objects.create(
            display_name="Alfa", league=self.league, credits=Decimal("500"),
            spent_credits=Decimal("30"),
        )
        Player.objects.create(name="Comprato", role="A", league=self.league,
                              initial_price=Decimal("20"), cost=Decimal("30"), owner=self.team)
        for n in ("Libero1", "Libero2", "Libero3"):
            Player.objects.create(name=n, role="C", league=self.league,
                                  initial_price=Decimal("5"), owner=None)
        self.auction = make_live_auction(league=self.league, status=Auction.Status.READY)

    def test_resume_restores_free_agents_and_rosters(self):
        session = services.save_session(self.auction.id, name="Serata 1")
        self.assertEqual(len(session.data["pool"]), 4)

        revived = services.resume_session(session.id)
        pool = Player.objects.filter(league=revived.league)
        self.assertEqual(pool.count(), 4)
        self.assertEqual(pool.filter(owner__isnull=True).count(), 3)
        owned = pool.get(name="Comprato")
        self.assertEqual(owned.owner.display_name, "Alfa")
        self.assertEqual(owned.cost, Decimal("30"))
        self.assertEqual(owned.initial_price, Decimal("20"))

    def test_a_resumed_auction_can_actually_start(self):
        session = services.save_session(self.auction.id, name="Serata 1")
        revived = services.resume_session(session.id)
        self.assertIsNotNone(services.start_auction(revived.id))
        revived.refresh_from_db()
        self.assertEqual(revived.status, Auction.Status.LIVE)

    def test_an_old_snapshot_recovers_the_pool_from_its_league(self):
        """Sessions saved before the fix still resume into a playable league."""
        session = services.save_session(self.auction.id, name="Vecchia")
        data = dict(session.data)
        data.pop("pool")                      # what a pre-fix snapshot looked like
        session.data = data
        session.save(update_fields=["data"])

        revived = services.resume_session(session.id)
        pool = Player.objects.filter(league=revived.league)
        self.assertEqual(pool.filter(owner__isnull=True).count(), 3)
        self.assertEqual(pool.filter(owner__isnull=False).count(), 1)
        self.assertIsNotNone(services.start_auction(revived.id))


class SessionCycleResultTests(TestCase):
    """Fase E: a saved session snapshots the auction's cycle results."""

    def test_save_session_snapshots_cycle_results(self):
        auction = make_live_auction(
            starting_price=Decimal("100"), current_price=Decimal("100"),
            min_increment=Decimal("10"),
        )
        p = Participant.objects.create(display_name="W", credits=Decimal("1000"))
        services.place_bid(auction.id, p.id, 50)   # 150
        services.close_auction(auction.id)          # records the cycle-1 result
        session = services.save_session(auction.id, created_by="admin")

        cr = session.data["cycle_results"]
        self.assertEqual(len(cr), 1)
        self.assertEqual(cr[0]["cycle"], 1)
        self.assertEqual(cr[0]["winner_name"], "W")
        self.assertEqual(cr[0]["amount"], "150.00")
        self.assertTrue(cr[0]["assigned"])

