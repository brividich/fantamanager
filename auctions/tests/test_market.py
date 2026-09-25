"""Tests for Market Sessions (Buste di mercato chiuse/asincrone)."""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Auction, League, MarketBid, MarketSession, Participant, Player, RosterLog
from ..services.market import (
    delete_market_bid,
    get_participant_market_bids,
    place_market_bid,
    resolve_market_session,
)


class MarketSessionTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(
            name="Fantacalcio 2026",
            budget=Decimal("500"),
            slots_p=3, slots_d=8, slots_c=8, slots_a=6,
        )
        self.team_a = Participant.objects.create(
            display_name="Real Madrink",
            league=self.league,
            credits=Decimal("500"),
            spent_credits=Decimal("0"),
        )
        self.team_b = Participant.objects.create(
            display_name="Atletico Van Goof",
            league=self.league,
            credits=Decimal("500"),
            spent_credits=Decimal("0"),
        )
        self.p_striker1 = Player.objects.create(
            name="Lautaro", role="A", league=self.league, initial_price=Decimal("35")
        )
        self.p_striker2 = Player.objects.create(
            name="Osimhen", role="A", league=self.league, initial_price=Decimal("30")
        )
        self.p_mid = Player.objects.create(
            name="Barella", role="C", league=self.league, initial_price=Decimal("20")
        )
        self.p_def = Player.objects.create(
            name="Dimarco", role="D", league=self.league, initial_price=Decimal("15")
        )
        # An already owned player for conditional release tests
        self.owned_cut = Player.objects.create(
            name="Vecino", role="C", league=self.league,
            owner=self.team_a, cost=Decimal("10"), initial_price=Decimal("8")
        )
        self.session = MarketSession.objects.create(
            league=self.league,
            title="Mercato di Riparazione Invernale",
            status=MarketSession.Status.OPEN,
            allow_conditional_release=True,
            release_refund_mode=Auction.RefundMode.PURCHASE,
        )

    def test_place_and_update_bid(self):
        """Placing a valid bid creates a MarketBid; re-placing updates it."""
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("45"),
            priority=1,
            release_player_id=self.owned_cut.id,
        )
        self.assertTrue(res["ok"])
        bid_id = res["bid_id"]

        bid = MarketBid.objects.get(pk=bid_id)
        self.assertEqual(bid.amount, Decimal("45"))
        self.assertEqual(bid.priority, 1)
        self.assertEqual(bid.release_player, self.owned_cut)

        # Re-placing updates the existing record
        res2 = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("50"),
            priority=2,
        )
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["bid_id"], bid_id)
        self.assertEqual(MarketBid.objects.filter(session=self.session).count(), 1)
        bid.refresh_from_db()
        self.assertEqual(bid.amount, Decimal("50"))
        self.assertEqual(bid.priority, 2)

    def test_place_bid_validations(self):
        """Test budget check, closed session, invalid release player."""
        # 1. Closed session
        self.session.status = MarketSession.Status.CLOSED
        self.session.save()
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("10"),
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "session_closed")
        self.session.status = MarketSession.Status.OPEN
        self.session.save()

        # 2. Insufficient budget
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("600"),  # budget is 500
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "insufficient_credits")

        # 3. Conditional release allows higher bid if refund covers the difference
        # Budget = 500, owned_cut cost = 10 -> max available = 510
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("505"),
            release_player_id=self.owned_cut.id,
        )
        self.assertTrue(res["ok"])

        # 4. Release player not owned by participant
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_b.id,
            player_id=self.p_striker1.id,
            amount=Decimal("20"),
            release_player_id=self.owned_cut.id,  # Owned by team_a!
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "release_player_not_owned")

    def test_delete_bid(self):
        """Participants can retract bids before window closes."""
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("25"),
        )
        bid_id = res["bid_id"]

        del_res = delete_market_bid(self.session.id, self.team_a.id, bid_id)
        self.assertTrue(del_res["ok"])
        self.assertFalse(MarketBid.objects.filter(pk=bid_id).exists())

        # Cannot delete once closed
        res = place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("25"),
        )
        bid_id = res["bid_id"]
        self.session.status = MarketSession.Status.CLOSED
        self.session.save()
        del_res = delete_market_bid(self.session.id, self.team_a.id, bid_id)
        self.assertFalse(del_res["ok"])
        self.assertEqual(del_res["error"], "session_closed")

    def test_get_participant_market_bids(self):
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("30"),
            priority=1,
            release_player_id=self.owned_cut.id,
        )
        bids = get_participant_market_bids(self.session.id, self.team_a.id)
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0]["player_name"], "Lautaro")
        self.assertEqual(bids[0]["amount"], 30)
        self.assertEqual(bids[0]["refund"], 10)  # Vecino cost is 10

    def test_resolve_market_session_success_and_releases(self):
        """Resolution awards player to highest bidder and executes conditional release."""
        # Team A bids 40 on Lautaro (with Vecino cut, refund 10)
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("40"),
            priority=1,
            release_player_id=self.owned_cut.id,
        )
        # Team B bids 30 on Lautaro
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_b.id,
            player_id=self.p_striker1.id,
            amount=Decimal("30"),
            priority=1,
        )
        # Team B bids 25 on Osimhen
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_b.id,
            player_id=self.p_striker2.id,
            amount=Decimal("25"),
            priority=1,
        )

        summary = resolve_market_session(self.session.id)
        self.assertEqual(summary["total_acquisitions"], 2)
        self.assertEqual(summary["total_ties"], 0)

        # Verify Lautaro assigned to Team A
        self.p_striker1.refresh_from_db()
        self.assertEqual(self.p_striker1.owner, self.team_a)
        self.assertEqual(self.p_striker1.cost, Decimal("40"))

        # Verify Vecino was released
        self.owned_cut.refresh_from_db()
        self.assertIsNone(self.owned_cut.owner)
        self.assertEqual(self.owned_cut.cost, Decimal("0"))

        # Verify Team A spent credits: 40 - 10 refund = 30 net
        self.team_a.refresh_from_db()
        self.assertEqual(self.team_a.spent_credits, Decimal("30"))

        # Verify Team B assigned Osimhen for 25
        self.p_striker2.refresh_from_db()
        self.assertEqual(self.p_striker2.owner, self.team_b)
        self.assertEqual(self.p_striker2.cost, Decimal("25"))
        self.team_b.refresh_from_db()
        self.assertEqual(self.team_b.spent_credits, Decimal("25"))

        # Check RosterLogs created
        logs_a = RosterLog.objects.filter(participant=self.team_a)
        self.assertEqual(logs_a.count(), 2)
        actions = set(logs_a.values_list("action", flat=True))
        self.assertIn(RosterLog.Action.ASSIGN, actions)
        self.assertIn(RosterLog.Action.RELEASE, actions)

    def test_resolve_market_session_ties(self):
        """When two teams tie with the exact same amount and priority, player is not assigned."""
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("35"),
            priority=1,
        )
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_b.id,
            player_id=self.p_striker1.id,
            amount=Decimal("35"),
            priority=1,
        )

        summary = resolve_market_session(self.session.id)
        self.assertEqual(summary["total_acquisitions"], 0)
        self.assertEqual(summary["total_ties"], 1)

        # Player remains unassigned
        self.p_striker1.refresh_from_db()
        self.assertIsNone(self.p_striker1.owner)

        # Bids marked TIED
        bids = MarketBid.objects.filter(session=self.session, player=self.p_striker1)
        for b in bids:
            self.assertEqual(b.status, MarketBid.Status.TIED)

    def test_resolve_role_limits(self):
        """Participant role limits reject subsequent bids for that role."""
        self.session.max_acquisitions_a = 1  # Only 1 forward allowed!
        self.session.save()

        # Team A bids on both strikers
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker1.id,
            amount=Decimal("50"),
            priority=1,
        )
        place_market_bid(
            session_id=self.session.id,
            participant_id=self.team_a.id,
            player_id=self.p_striker2.id,
            amount=Decimal("40"),
            priority=2,
        )

        summary = resolve_market_session(self.session.id)
        self.assertEqual(summary["total_acquisitions"], 1)

        # First striker won, second lost due to limit
        b1 = MarketBid.objects.get(session=self.session, player=self.p_striker1)
        b2 = MarketBid.objects.get(session=self.session, player=self.p_striker2)
        self.assertEqual(b1.status, MarketBid.Status.WON)
        self.assertEqual(b2.status, MarketBid.Status.LOST)
        self.assertIn("limite acquisti", b2.note)


class MarketViewsTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Pro")
        self.participant = Participant.objects.create(
            display_name="Gigi Buffon Fan Club",
            league=self.league,
            credits=Decimal("300"),
        )
        self.player = Player.objects.create(
            name="Chiesa", role="A", league=self.league, initial_price=Decimal("15")
        )
        self.session = MarketSession.objects.create(
            league=self.league,
            title="Sessione Estiva",
            status=MarketSession.Status.OPEN,
        )

    def test_app_market_bid_and_delete_endpoints(self):
        """HTTP JSON endpoints for participant bidding in app_mercato."""
        # Set session participant cookie/token
        session = self.client.session
        session["participant_id"] = self.participant.id
        session.save()

        # 1. Submit bid
        resp = self.client.post(
            reverse("app_market_bid"),
            data=json.dumps({
                "session_id": self.session.id,
                "player_id": self.player.id,
                "amount": 25,
                "priority": 1,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["my_bids"]), 1)

        bid_id = data["bid_id"]

        # 2. Delete bid
        del_resp = self.client.post(
            reverse("app_market_delete_bid"),
            data=json.dumps({
                "session_id": self.session.id,
                "bid_id": bid_id,
            }),
            content_type="application/json",
        )
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.json()["ok"])
        self.assertEqual(len(del_resp.json()["my_bids"]), 0)

    def test_admin_market_views(self):
        """Admin views for creating, toggling status, and resolving session."""
        # 1. Dashboard
        resp = self.client.get(reverse("admin_market_dashboard") + f"?league={self.league.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sessione Estiva")

        # 2. Create session
        create_resp = self.client.post(
            reverse("admin_market_create"),
            data={
                "league_id": self.league.id,
                "title": "Mercato Flash",
                "allow_conditional_release": "1",
                "refund_mode": "PURCHASE",
                "max_acquisitions_p": "1",
            },
        )
        self.assertEqual(create_resp.status_code, 302)
        self.assertTrue(MarketSession.objects.filter(title="Mercato Flash").exists())
        new_sess = MarketSession.objects.get(title="Mercato Flash")
        self.assertEqual(new_sess.max_acquisitions_p, 1)

        # 3. Toggle status
        toggle_resp = self.client.post(
            reverse("admin_market_status", kwargs={"session_id": self.session.id}),
            data={"status": "CLOSED"},
        )
        self.assertEqual(toggle_resp.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MarketSession.Status.CLOSED)

        # 4. Resolve session
        resolve_resp = self.client.post(
            reverse("admin_market_resolve", kwargs={"session_id": self.session.id})
        )
        self.assertEqual(resolve_resp.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MarketSession.Status.RESOLVED)

        # 5. Delete session
        del_resp = self.client.post(
            reverse("admin_market_delete", kwargs={"session_id": new_sess.id})
        )
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(MarketSession.objects.filter(pk=new_sess.id).exists())
