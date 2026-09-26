"""Tests for Market Sessions (Buste di mercato chiuse/asincrone)."""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Auction, League, MarketBid, MarketSession, Participant, Player, RosterLog
from ..services.market import (
    delete_market_bid,
    get_participant_market_bids,
    place_market_bid,
    plan_market_resolution,
    resolve_market_session,
    settle_market_tie,
    sync_market_schedule,
    undo_market_resolution,
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


class MarketResolutionRulesTests(TestCase):
    """Priority order, availability, ownership and roster-slot checks at resolution."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega Spoglio", budget=Decimal("500"),
            slots_p=3, slots_d=8, slots_c=8, slots_a=6,
        )
        self.a = Participant.objects.create(display_name="Alfa", league=self.league, credits=Decimal("150"))
        self.b = Participant.objects.create(display_name="Beta", league=self.league, credits=Decimal("500"))
        self.session = MarketSession.objects.create(
            league=self.league, title="Buste", status=MarketSession.Status.OPEN,
        )

    def _player(self, name, role="A", **kw):
        return Player.objects.create(name=name, role=role, league=self.league, **kw)

    def _bid(self, who, player, amount, priority=1, release=None):
        res = place_market_bid(
            session_id=self.session.id, participant_id=who.id, player_id=player.id,
            amount=Decimal(amount), priority=priority,
            release_player_id=release.id if release else None,
        )
        self.assertTrue(res["ok"], res)
        return MarketBid.objects.get(pk=res["bid_id"])

    def _status(self, bid):
        bid.refresh_from_db()
        return bid.status

    def test_priority_decides_budget_not_player_id(self):
        """With budget for one of two, the priority-1 target wins even if created later."""
        low_id = self._player("Primo")
        high_id = self._player("Secondo")
        b2 = self._bid(self.a, low_id, 100, priority=2)
        b1 = self._bid(self.a, high_id, 100, priority=1)
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(b1), MarketBid.Status.WON)
        self.assertEqual(self._status(b2), MarketBid.Status.LOST)
        self.assertIn("Crediti insufficienti", b2.note)

    def test_losing_top_priority_frees_budget_for_next(self):
        x = self._player("X")
        y = self._player("Y")
        a_x = self._bid(self.a, x, 100, priority=1)
        a_y = self._bid(self.a, y, 100, priority=2)
        b_x = self._bid(self.b, x, 120, priority=1)
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(b_x), MarketBid.Status.WON)
        self.assertEqual(self._status(a_x), MarketBid.Status.LOST)
        self.assertEqual(self._status(a_y), MarketBid.Status.WON)

    def test_highest_offer_wins_even_with_lower_priority(self):
        """A priority-2 envelope that is the highest offer is not beaten by a priority-1 lower one."""
        x = self._player("X")
        z = self._player("Z")
        a_x = self._bid(self.a, x, 50, priority=1)
        self._bid(self.b, z, 10, priority=1)
        b_x = self._bid(self.b, x, 90, priority=2)
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(b_x), MarketBid.Status.WON)
        self.assertEqual(self._status(a_x), MarketBid.Status.LOST)

    def test_crossed_preferences_terminate(self):
        x = self._player("X")
        y = self._player("Y")
        self._bid(self.a, x, 50, priority=1)
        a_y = self._bid(self.a, y, 100, priority=2)
        self._bid(self.b, y, 60, priority=1)
        b_x = self._bid(self.b, x, 90, priority=2)
        summary = resolve_market_session(self.session.id)
        self.assertEqual(summary["total_acquisitions"], 2)
        self.assertEqual(self._status(a_y), MarketBid.Status.WON)
        self.assertEqual(self._status(b_x), MarketBid.Status.WON)

    def test_player_taken_during_session_is_not_stolen(self):
        x = self._player("X")
        bid = self._bid(self.a, x, 30)
        x.owner = self.b
        x.cost = Decimal("5")
        x.save()
        resolve_market_session(self.session.id)
        x.refresh_from_db()
        self.assertEqual(x.owner, self.b)
        self.assertEqual(self._status(bid), MarketBid.Status.LOST)
        self.assertIn("non più disponibile", bid.note)
        self.a.refresh_from_db()
        self.assertEqual(self.a.spent_credits, Decimal("0"))

    def test_roster_full_falls_to_next_bidder(self):
        for i in range(6):
            self._player(f"A{i}", owner=self.a, cost=Decimal("0"))
        x = self._player("X")
        a_x = self._bid(self.a, x, 80)
        b_x = self._bid(self.b, x, 40)
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(a_x), MarketBid.Status.LOST)
        self.assertIn("Rosa piena", a_x.note)
        self.assertEqual(self._status(b_x), MarketBid.Status.WON)

    def test_conditional_release_makes_room(self):
        owned = [self._player(f"A{i}", owner=self.a, cost=Decimal("10")) for i in range(6)]
        x = self._player("X")
        bid = self._bid(self.a, x, 30, release=owned[0])
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(bid), MarketBid.Status.WON)
        owned[0].refresh_from_db()
        self.assertIsNone(owned[0].owner)
        self.a.refresh_from_db()
        self.assertEqual(self.a.spent_credits, Decimal("20"))

    def test_release_no_longer_owned_is_checked(self):
        """If the cut player left the roster, the bid is judged without the cut."""
        owned = [self._player(f"A{i}", owner=self.a, cost=Decimal("10")) for i in range(6)]
        x = self._player("X")
        bid = self._bid(self.a, x, 30, release=owned[0])
        owned[0].owner = self.b
        owned[0].save()
        self._player("A-extra", owner=self.a)  # roster back to 6 forwards
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(bid), MarketBid.Status.LOST)
        self.assertIn("taglio condizionato non più possibile", bid.note)
        owned[0].refresh_from_db()
        self.assertEqual(owned[0].owner, self.b)

    def test_same_cut_used_twice_only_once(self):
        cut = self._player("Cut", role="C", owner=self.a, cost=Decimal("10"))
        x = self._player("X")
        y = self._player("Y")
        self._bid(self.a, x, 40, priority=1, release=cut)
        by = self._bid(self.a, y, 40, priority=2, release=cut)
        resolve_market_session(self.session.id)
        self.assertEqual(self._status(by), MarketBid.Status.WON)
        self.a.refresh_from_db()
        # 40 - 10 refund + 40 without refund
        self.assertEqual(self.a.spent_credits, Decimal("70"))
        self.assertEqual(RosterLog.objects.filter(action=RosterLog.Action.RELEASE).count(), 1)

    def test_preview_writes_nothing(self):
        x = self._player("X")
        bid = self._bid(self.a, x, 30)
        preview = plan_market_resolution(self.session.id)
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["total_acquisitions"], 1)
        self.assertEqual(self._status(bid), MarketBid.Status.PENDING)
        x.refresh_from_db()
        self.assertIsNone(x.owner)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MarketSession.Status.OPEN)


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
        self.client.force_login(User.objects.create_superuser("root", "root@x.local", "pw"))
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


class MarketAdminTemplateTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", "root@x.local", "pw"))
        self.league = League.objects.create(name="Lega T")
        self.session = MarketSession.objects.create(
            league=self.league, title="Sessione T", status=MarketSession.Status.OPEN,
        )

    def _get(self):
        url = reverse("admin_market_dashboard") + f"?league={self.league.id}&session={self.session.id}"
        return self.client.get(url)

    def test_open_session_shows_close_and_resolve_actions(self):
        resp = self._get()
        self.assertContains(resp, "Chiudi finestra")
        self.assertContains(resp, 'name="status" value="closed"')
        self.assertContains(resp, 'value="purchase"')

    def test_resolved_session_shows_results(self):
        p = Participant.objects.create(display_name="Squadra", league=self.league, credits=Decimal("100"))
        pl = Player.objects.create(name="Retegui", role="A", league=self.league)
        place_market_bid(self.session.id, p.id, pl.id, 20)
        resolve_market_session(self.session.id)
        resp = self._get()
        self.assertContains(resp, "Esito dello spoglio")
        self.assertContains(resp, "Retegui")


class MarketAdminTenantIsolationTests(TestCase):
    """A league admin can only manage the market sessions of leagues they own."""

    def setUp(self):
        self.admin_a = User.objects.create_user("admin_a", password="pw")
        self.admin_b = User.objects.create_user("admin_b", password="pw")
        self.league_a = League.objects.create(name="Lega A", owner=self.admin_a)
        self.league_b = League.objects.create(name="Lega B", owner=self.admin_b)
        self.session_b = MarketSession.objects.create(
            league=self.league_b, title="Mercato B", status=MarketSession.Status.OPEN,
        )
        self.client.force_login(self.admin_a)

    def test_cannot_touch_foreign_session(self):
        for name, data in (
            ("admin_market_status", {"status": "closed"}),
            ("admin_market_resolve", {}),
            ("admin_market_delete", {}),
        ):
            resp = self.client.post(reverse(name, kwargs={"session_id": self.session_b.id}), data)
            self.assertEqual(resp.status_code, 403, name)
        self.session_b.refresh_from_db()
        self.assertEqual(self.session_b.status, MarketSession.Status.OPEN)

    def test_cannot_create_in_foreign_league(self):
        self.client.post(reverse("admin_market_create"), {"league_id": self.league_b.id, "title": "X"})
        self.assertFalse(MarketSession.objects.filter(league=self.league_b, title="X").exists())

    def test_dashboard_lists_only_owned_leagues(self):
        resp = self.client.get(reverse("admin_market_dashboard") + f"?league={self.league_a.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["leagues"]), [self.league_a])
        self.assertNotContains(resp, "Mercato B")

    def test_owner_can_manage_own_session(self):
        self.client.force_login(self.admin_b)
        resp = self.client.post(
            reverse("admin_market_status", kwargs={"session_id": self.session_b.id}),
            {"status": "closed"},
        )
        self.assertEqual(resp.status_code, 302)
        self.session_b.refresh_from_db()
        self.assertEqual(self.session_b.status, MarketSession.Status.CLOSED)


class MarketAfterResolutionTests(TestCase):
    """Tie settlement, undo, scheduling and the admin/app pages around them."""

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.local", "pw")
        self.league = League.objects.create(name="Lega Post")
        self.a = Participant.objects.create(display_name="Alfa", league=self.league, credits=Decimal("100"))
        self.b = Participant.objects.create(display_name="Beta", league=self.league, credits=Decimal("100"))
        self.session = MarketSession.objects.create(
            league=self.league, title="Buste", status=MarketSession.Status.OPEN,
        )
        self.x = Player.objects.create(name="X", role="A", league=self.league)

    def _tie(self):
        place_market_bid(self.session.id, self.a.id, self.x.id, 30)
        place_market_bid(self.session.id, self.b.id, self.x.id, 30)
        resolve_market_session(self.session.id)

    def test_admin_picks_tie_winner(self):
        self._tie()
        res = settle_market_tie(self.session.id, self.x.id, winner_id=self.b.id)
        self.assertTrue(res["ok"], res)
        self.x.refresh_from_db()
        self.assertEqual(self.x.owner, self.b)
        self.assertEqual(self.x.cost, Decimal("30"))
        self.b.refresh_from_db()
        self.assertEqual(self.b.spent_credits, Decimal("30"))
        self.session.refresh_from_db()
        summary = self.session.results_summary
        self.assertEqual(summary["open_ties"], 0)
        self.assertEqual(summary["tied"][0]["settled"]["winner_id"], self.b.id)
        self.assertEqual(summary["total_acquisitions"], 1)
        statuses = dict(MarketBid.objects.filter(player=self.x).values_list("participant_id", "status"))
        self.assertEqual(statuses[self.b.id], MarketBid.Status.WON)
        self.assertEqual(statuses[self.a.id], MarketBid.Status.LOST)
        # Settling twice is refused
        self.assertFalse(settle_market_tie(self.session.id, self.x.id)["ok"])

    def test_draw_uses_rng_and_skips_contenders_who_cannot_pay(self):
        self._tie()
        Participant.objects.filter(pk=self.a.pk).update(spent_credits=Decimal("90"))

        class First:
            def choice(self, seq):
                return seq[0]

        res = settle_market_tie(self.session.id, self.x.id, rng=First())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["winner_id"], self.b.id)
        self.assertEqual(res["method"], "draw")

    def test_admin_cannot_pick_contender_who_cannot_pay(self):
        self._tie()
        Participant.objects.filter(pk=self.a.pk).update(spent_credits=Decimal("90"))
        res = settle_market_tie(self.session.id, self.x.id, winner_id=self.a.id)
        self.assertFalse(res["ok"])
        self.assertIn("Crediti insufficienti", res["message"])

    def test_undo_restores_rosters_credits_and_bids(self):
        cut = Player.objects.create(name="Cut", role="C", league=self.league, owner=self.a, cost=Decimal("12"))
        place_market_bid(self.session.id, self.a.id, self.x.id, 40, release_player_id=cut.id)
        resolve_market_session(self.session.id)
        self.a.refresh_from_db()
        self.assertEqual(self.a.spent_credits, Decimal("28"))

        res = undo_market_resolution(self.session.id)
        self.assertTrue(res["ok"], res)
        self.x.refresh_from_db()
        cut.refresh_from_db()
        self.a.refresh_from_db()
        self.session.refresh_from_db()
        self.assertIsNone(self.x.owner)
        self.assertEqual(cut.owner, self.a)
        self.assertEqual(cut.cost, Decimal("12"))
        self.assertEqual(self.a.spent_credits, Decimal("0"))
        self.assertEqual(self.session.status, MarketSession.Status.CLOSED)
        self.assertEqual(
            set(self.session.bids.values_list("status", flat=True)), {MarketBid.Status.PENDING}
        )
        # And it can be resolved again with the same outcome
        resolve_market_session(self.session.id)
        self.x.refresh_from_db()
        self.assertEqual(self.x.owner, self.a)

    def test_undo_includes_tie_break_awards(self):
        self._tie()
        settle_market_tie(self.session.id, self.x.id, winner_id=self.a.id)
        self.assertTrue(undo_market_resolution(self.session.id)["ok"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.spent_credits, Decimal("0"))
        self.x.refresh_from_db()
        self.assertIsNone(self.x.owner)

    def test_undo_refused_when_rosters_changed(self):
        place_market_bid(self.session.id, self.a.id, self.x.id, 40)
        resolve_market_session(self.session.id)
        Player.objects.filter(pk=self.x.pk).update(owner=self.b)
        res = undo_market_resolution(self.session.id)
        self.assertFalse(res["ok"])
        self.assertIn("X non è più di Alfa", res["message"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MarketSession.Status.RESOLVED)

    def test_schedule_opens_and_closes(self):
        now = timezone.now()
        draft = MarketSession.objects.create(
            league=self.league, status=MarketSession.Status.DRAFT,
            opens_at=now - timedelta(minutes=1), closes_at=now + timedelta(days=1),
        )
        future = MarketSession.objects.create(
            league=self.league, status=MarketSession.Status.DRAFT, opens_at=now + timedelta(days=1),
        )
        self.session.closes_at = now - timedelta(seconds=1)
        self.session.save()
        sync_market_schedule(self.league)
        for obj in (draft, future, self.session):
            obj.refresh_from_db()
        self.assertEqual(draft.status, MarketSession.Status.OPEN)
        self.assertEqual(future.status, MarketSession.Status.DRAFT)
        self.assertEqual(self.session.status, MarketSession.Status.CLOSED)

    def test_create_with_future_opening_is_scheduled(self):
        self.client.force_login(self.root)
        when = (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(reverse("admin_market_create"), {
            "league_id": self.league.id, "title": "Programmata", "opens_at": when,
        })
        s = MarketSession.objects.get(title="Programmata")
        self.assertEqual(s.status, MarketSession.Status.DRAFT)
        self.assertIsNotNone(s.opens_at)

    def test_admin_preview_page(self):
        self.client.force_login(self.root)
        place_market_bid(self.session.id, self.a.id, self.x.id, 25)
        url = reverse("admin_market_dashboard") + f"?league={self.league.id}&session={self.session.id}&preview=1"
        resp = self.client.get(url)
        self.assertContains(resp, "Anteprima:")
        self.assertContains(resp, "Esito previsto dello spoglio")
        self.x.refresh_from_db()
        self.assertIsNone(self.x.owner)

    def test_admin_tie_and_undo_views(self):
        self.client.force_login(self.root)
        self._tie()
        page = self.client.get(
            reverse("admin_market_dashboard") + f"?league={self.league.id}&session={self.session.id}"
        )
        self.assertContains(page, "Sorteggio")
        self.assertContains(page, "Annulla spoglio")
        resp = self.client.post(
            reverse("admin_market_settle_tie", kwargs={"session_id": self.session.id}),
            {"player_id": self.x.id, "winner_id": self.a.id},
        )
        self.assertEqual(resp.status_code, 302)
        self.x.refresh_from_db()
        self.assertEqual(self.x.owner, self.a)
        resp = self.client.post(reverse("admin_market_undo", kwargs={"session_id": self.session.id}))
        self.assertEqual(resp.status_code, 302)
        self.x.refresh_from_db()
        self.assertIsNone(self.x.owner)

    def test_foreign_admin_cannot_settle_or_undo(self):
        other = User.objects.create_user("other", password="pw")
        self.league.owner = self.root
        self.league.save()
        self._tie()
        self.client.force_login(other)
        for name, data in (("admin_market_settle_tie", {"player_id": self.x.id}), ("admin_market_undo", {})):
            resp = self.client.post(reverse(name, kwargs={"session_id": self.session.id}), data)
            self.assertEqual(resp.status_code, 403, name)

    def test_app_shows_outcome_after_resolution(self):
        place_market_bid(self.session.id, self.a.id, self.x.id, 25)
        resolve_market_session(self.session.id)
        session = self.client.session
        session["participant_id"] = self.a.id
        session.save()
        resp = self.client.get(reverse("app_mercato"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Spoglio eseguito")
        self.assertContains(resp, "Aggiudicato")
        self.assertFalse(resp.context["market_open"])
        self.assertNotContains(resp, "Consegna Busta")

    def test_app_cut_refund_uses_league_price(self):
        self.league.game_mode = League.GameMode.MANTRA
        self.league.save()
        self.session.release_refund_mode = Auction.RefundMode.CURRENT
        self.session.save()
        Player.objects.create(
            name="Mantra Guy", role="D", league=self.league, owner=self.a,
            initial_price=Decimal("5"), price_m=Decimal("9"),
        )
        session = self.client.session
        session["participant_id"] = self.a.id
        session.save()
        resp = self.client.get(reverse("app_mercato"))
        self.assertContains(resp, 'data-refund="9"')
