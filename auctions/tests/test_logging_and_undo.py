"""Tests for system logging and the Regia undo lot safety net."""
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import tempfile

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .. import services
from ..models import Auction, AuctionQueueItem, League, MarketSession, Participant, Player, RosterLog
from .common import make_live_auction


class SystemLoggingAndUndoTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("admin", "a@b.c", "pass12345"))
        self.league = League.objects.create(name="Lega Test")
        self.participant = Participant.objects.create(
            league=self.league,
            display_name="Squadra 1",
            credits=Decimal("500"),
            spent_credits=Decimal("0"),
        )
        self.player1 = Player.objects.create(
            league=self.league,
            name="Lautaro Martinez",
            role="A",
            team="Inter",
            initial_price=Decimal("1"),
            cost=Decimal("0"),
        )
        self.player2 = Player.objects.create(
            league=self.league,
            name="Rafael Leao",
            role="A",
            team="Milan",
            initial_price=Decimal("1"),
            cost=Decimal("0"),
        )

    def test_admin_logs_tail_permissions(self):
        """admin_logs_tail is open on local/LAN, but gated if accessed through remote tunnel."""
        url = reverse("admin_logs_tail")

        # 1. Local/LAN: 200 OK
        with patch("auctions.remote.request_is_remote", return_value=False):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))
            self.assertIsInstance(data.get("lines"), list)

        # 2. Remote access without PIN unlock: 302 redirect to unlock
        with patch("auctions.remote.request_is_remote", return_value=True):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302)

        # 3. Remote access with unlocked session: 200 OK
        with patch("auctions.remote.request_is_remote", return_value=True):
            session = self.client.session
            session["regia_unlocked"] = True
            session.save()
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))

    def test_admin_logs_tail_reads_log_file(self):
        """admin_logs_tail returns the last lines from the log file."""
        # A throwaway BASE_DIR: the test must not overwrite the real log.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings_override = override_settings(BASE_DIR=Path(tmp.name))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        log_dir = Path(settings.BASE_DIR) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "fantamanager.log"

        # Write test lines
        with open(log_file, "w", encoding="utf-8") as f:
            for i in range(120):
                f.write(f"Log line test {i}\n")

        with patch("auctions.remote.request_is_remote", return_value=False):
            res = self.client.get(reverse("admin_logs_tail"))
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))
            lines = data.get("lines")
            # Tail should return at most 100 lines
            self.assertLessEqual(len(lines), 100)
            self.assertIn("Log line test 119", lines[-1])

    def test_bidding_logging_emits(self):
        """services.place_bid logs accepted and rejected bids."""
        auction = make_live_auction(
            league=self.league,
            player=self.player1,
            flow_mode=Auction.FlowMode.CALL,
            starting_price=Decimal("1"),
            current_price=Decimal("1"),
            min_increment=Decimal("1"),
        )

        with self.assertLogs("auctions.bidding", level="INFO") as cm:
            res = services.place_bid(auction.id, self.participant.id, 5)
            self.assertTrue(res.accepted)
            self.assertTrue(any("ACCETTATO" in msg for msg in cm.output))
            self.assertTrue(any("Lautaro Martinez" in msg for msg in cm.output))

        with self.assertLogs("auctions.bidding", level="WARNING") as cm:
            # Reject: amount exceeds participant credits
            res = services.place_bid(auction.id, self.participant.id, 1000)
            self.assertFalse(res.accepted)
            self.assertTrue(any("RIFIUTATO" in msg for msg in cm.output))

    def test_market_logging_emits(self):
        """services.place_market_bid and resolve_market_session emit logs."""
        session = MarketSession.objects.create(
            league=self.league,
            title="Mercato Riparazione",
            status=MarketSession.Status.OPEN,
        )

        with self.assertLogs("auctions.market", level="INFO") as cm:
            res = services.place_market_bid(
                session.id, self.participant.id, self.player2.id, 25
            )
            self.assertTrue(res["ok"])
            self.assertTrue(any("Market bid placed" in msg for msg in cm.output))
            self.assertTrue(any("Rafael Leao" in msg for msg in cm.output))

            # Resolve
            session.status = MarketSession.Status.CLOSED
            session.save()
            services.resolve_market_session(session.id)
            self.assertTrue(any("Resolving market session" in msg for msg in cm.output))
            self.assertTrue(any("Market bid won" in msg for msg in cm.output))

    def test_admin_manual_step_undo_sale_flow(self):
        """Regia undo safety net: step back with confirm reverses sale and restores lot."""
        # Setup manual queue auction with player1 and player2
        auction = make_live_auction(
            league=self.league,
            player=self.player1,
            flow_mode=Auction.FlowMode.MANUAL,
            current_price=Decimal("1"),
            starting_price=Decimal("1"),
            min_increment=Decimal("1"),
        )
        q1 = AuctionQueueItem.objects.create(auction=auction, player=self.player1, order=1, done=True)
        q2 = AuctionQueueItem.objects.create(auction=auction, player=self.player2, order=2, done=False)

        # Participant bids on player 1 (current_price 1 + 30 = 31)
        bid_res = services.place_bid(auction.id, self.participant.id, 30)
        self.assertTrue(bid_res.accepted)

        # Step next: awards player 1 to participant, puts player 2 on block
        step_url = reverse("admin_manual_step", kwargs={"auction_id": auction.id})
        with patch("auctions.remote.request_is_remote", return_value=False):
            res = self.client.post(step_url, {"direction": "next"})
            self.assertEqual(res.status_code, 200)

            # Verify player 1 is owned by participant and credits deducted
            self.player1.refresh_from_db()
            self.participant.refresh_from_db()
            self.assertEqual(self.player1.owner_id, self.participant.id)
            self.assertEqual(self.player1.cost, Decimal("31.00"))
            self.assertEqual(self.participant.spent_credits, Decimal("31.00"))

            # Step prev WITHOUT undo_sale -> 409 needs_undo_confirm
            res = self.client.post(step_url, {"direction": "prev"})
            self.assertEqual(res.status_code, 409)
            data = res.json()
            self.assertEqual(data.get("error"), "needs_undo_confirm")
            undo_info = data.get("undo")
            self.assertEqual(undo_info["player_id"], self.player1.id)
            self.assertEqual(undo_info["winner_name"], self.participant.display_name)

            # Step prev WITH undo_sale="1" -> 200 OK, reverses sale!
            res = self.client.post(step_url, {"direction": "prev", "undo_sale": "1"})
            self.assertEqual(res.status_code, 200)

            # Verify player 1 is back on block, ownership cleared, credits refunded
            self.player1.refresh_from_db()
            self.participant.refresh_from_db()
            auction.refresh_from_db()

            self.assertIsNone(self.player1.owner_id)
            self.assertEqual(self.player1.cost, Decimal("0"))
            self.assertEqual(self.participant.spent_credits, Decimal("0"))
            self.assertEqual(auction.player_id, self.player1.id)
            self.assertEqual(auction.status, Auction.Status.LIVE)

            # Verify ADMIN_RELEASE logged in RosterLog
            log_entry = RosterLog.objects.filter(
                participant=self.participant,
                action=RosterLog.Action.ADMIN_RELEASE,
            ).first()
            self.assertIsNotNone(log_entry)
            self.assertEqual(log_entry.credits_delta, Decimal("31.00"))
