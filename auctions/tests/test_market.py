"""Tests for Market Sessions (Buste di mercato chiuse/asincrone)."""
import json
import re
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
        url = reverse("admin_market_session", args=[self.session.id])
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


class MarketHubTests(TestCase):
    """The Mercato hub says which markets are open; each market is its own screen."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", "root@x.local", "pw"))
        self.league = League.objects.create(name="Lega H")
        self.team = Participant.objects.create(display_name="Squadra H", league=self.league, credits=Decimal("100"))

    def _hub(self, qs=""):
        return self.client.get(reverse("admin_market_dashboard") + f"?league={self.league.id}{qs}")

    def _screen(self, name):
        return self.client.get(reverse(name) + f"?league={self.league.id}")

    def test_nav_says_mercato_and_hub_holds_no_market_content(self):
        self.league.trades_enabled = False
        self.league.save()
        resp = self._hub()
        self.assertContains(resp, "</svg> Mercato</a>")
        self.assertContains(resp, "Nessun mercato aperto")
        # Only links to the screens, none of their tools.
        for name in ("admin_market_buste", "admin_market_trades", "admin_market_repair", "admin_market_moves"):
            self.assertContains(resp, reverse(name))
        self.assertNotContains(resp, "Regole degli scambi")
        self.assertNotContains(resp, 'id="modal-new-session"')

    def test_hub_lists_the_open_markets_and_links_their_screen(self):
        open_s = MarketSession.objects.create(league=self.league, title="Buste Ottobre", status=MarketSession.Status.OPEN)
        MarketSession.objects.create(league=self.league, title="Vecchia", status=MarketSession.Status.RESOLVED)
        self.league.trades_enabled = True
        self.league.save()
        resp = self._hub()
        self.assertContains(resp, "In corso adesso")
        self.assertContains(resp, "Buste Ottobre")
        self.assertContains(resp, reverse("admin_market_session", args=[open_s.id]))
        self.assertContains(resp, "Scambi tra squadre")
        self.assertEqual(resp.context["n_live"], 2)
        self.assertNotIn("Vecchia", [s.title for s in resp.context["live_sessions"]])

    def test_old_tab_and_session_links_open_the_new_screens(self):
        s = MarketSession.objects.create(league=self.league, title="S", status=MarketSession.Status.OPEN)
        resp = self._hub("&tab=scambi")
        self.assertRedirects(resp, reverse("admin_market_trades") + f"?league={self.league.id}")
        resp = self._hub(f"&session={s.id}&preview=1")
        self.assertRedirects(resp, reverse("admin_market_session", args=[s.id]) + "?preview=1")

    def test_buste_lists_sessions_and_each_opens_alone(self):
        a = MarketSession.objects.create(league=self.league, title="Prima", status=MarketSession.Status.OPEN)
        MarketSession.objects.create(league=self.league, title="Seconda", status=MarketSession.Status.RESOLVED)
        resp = self._screen("admin_market_buste")
        self.assertContains(resp, "Prima")
        self.assertContains(resp, "Seconda")
        self.assertNotContains(resp, "Chiudi finestra")
        page = self.client.get(reverse("admin_market_session", args=[a.id]))
        self.assertContains(page, "Chiudi finestra")
        self.assertNotContains(page, "Seconda")

    def test_trade_settings_land_back_on_scambi(self):
        resp = self.client.post(reverse("admin_trade_settings"), {"league_id": self.league.id, "trades_enabled": "1"})
        self.assertEqual(resp["Location"], reverse("admin_market_trades") + f"?league={self.league.id}")

    def test_session_actions_land_on_the_session_screen(self):
        s = MarketSession.objects.create(league=self.league, title="S", status=MarketSession.Status.OPEN)
        resp = self.client.post(reverse("admin_market_status", args=[s.id]), {"status": "closed"})
        self.assertEqual(resp["Location"], reverse("admin_market_session", args=[s.id]))
        resp = self.client.post(reverse("admin_market_delete", args=[s.id]))
        self.assertEqual(resp["Location"], reverse("admin_market_buste") + f"?league={self.league.id}")

    def test_movimenti_lists_the_league_roster_log(self):
        RosterLog.objects.create(participant=self.team, participant_name="Squadra H", player_name="Kean",
                                 player_role="A", action=RosterLog.Action.ASSIGN, credits_delta=Decimal("18"))
        other = Participant.objects.create(display_name="Altrove", league=League.objects.create(name="Altra"))
        RosterLog.objects.create(participant=other, participant_name="Altrove", player_name="Vlahovic",
                                 player_role="A", action=RosterLog.Action.ASSIGN, credits_delta=Decimal("5"))
        resp = self._screen("admin_market_moves")
        self.assertContains(resp, "Kean")
        self.assertContains(resp, "−18 FM")
        self.assertNotContains(resp, "Vlahovic")

    def test_asta_offers_a_repair_auction_and_the_wizard_preselects_it(self):
        Player.objects.create(name="Svincolato", role="C", league=self.league)
        resp = self._screen("admin_market_repair")
        self.assertContains(resp, "mode=REPAIR_AUCTION")
        self.assertContains(resp, f"status=free&role=C")
        wizard = self.client.get(reverse("admin_auction_wizard") + f"?league={self.league.id}&mode=REPAIR_AUCTION")
        self.assertContains(wizard, 'value="REPAIR_AUCTION" checked')

    def test_foreign_session_screen_is_forbidden(self):
        owner = User.objects.create_user("owner", password="pw")
        other = League.objects.create(name="Altrui", owner=owner)
        s = MarketSession.objects.create(league=other, title="Segreta", status=MarketSession.Status.OPEN)
        self.client.force_login(User.objects.create_user("intruso", password="pw"))
        self.assertEqual(self.client.get(reverse("admin_market_session", args=[s.id])).status_code, 403)


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


class MarketPerLeagueTests(TestCase):
    """A market opened by a league's admin exists only for that league."""

    def setUp(self):
        self.owner = User.objects.create_user("owner_ml", password="pw")
        self.coadmin = User.objects.create_user("coadmin_ml", password="pw")
        self.league_a = League.objects.create(name="Lega Aperta", owner=self.owner)
        self.league_a.admins.add(self.coadmin)
        self.league_b = League.objects.create(name="Lega Chiusa")
        self.team_a = Participant.objects.create(display_name="Squadra A", league=self.league_a, credits=Decimal("300"))
        self.team_b = Participant.objects.create(display_name="Squadra B", league=self.league_b, credits=Decimal("300"))
        self.player_a = Player.objects.create(name="Kean", role="A", league=self.league_a, initial_price=Decimal("10"))
        self.player_b = Player.objects.create(name="Kean", role="A", league=self.league_b, initial_price=Decimal("10"))

    def test_coadmin_opens_a_market_in_their_league_only(self):
        self.client.force_login(self.coadmin)
        resp = self.client.get(reverse("admin_market_buste") + f"?league={self.league_a.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["current_league"], self.league_a)

        self.client.post(reverse("admin_market_create"), {"league_id": self.league_a.id, "title": "Riparazione A"})
        session = MarketSession.objects.get(title="Riparazione A")
        self.assertEqual(session.league, self.league_a)
        self.assertEqual(session.status, MarketSession.Status.OPEN)
        self.assertFalse(MarketSession.objects.filter(league=self.league_b).exists())

    def test_coadmin_cannot_scope_to_a_league_they_dont_run(self):
        self.client.force_login(self.coadmin)
        resp = self.client.get(reverse("admin_market_buste") + f"?league={self.league_b.id}")
        self.assertIsNone(resp.context["current_league"])
        self.client.post(reverse("admin_market_create"), {"league_id": self.league_b.id, "title": "Intruso"})
        self.assertFalse(MarketSession.objects.filter(title="Intruso").exists())

    def test_other_league_teams_dont_see_or_bid_in_the_market(self):
        session = MarketSession.objects.create(league=self.league_a, title="Solo A", status=MarketSession.Status.OPEN)
        res = place_market_bid(session.id, self.team_b.id, self.player_a.id, 5)
        self.assertEqual(res["error"], "invalid_participant")

        s = self.client.session
        s["participant_id"] = self.team_b.id
        s.save()
        resp = self.client.get(reverse("app_mercato"))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context["market_session"])
        self.assertFalse(resp.context["market_open"])
        self.assertNotContains(resp, "Solo A")

    def test_player_of_another_league_or_without_league_is_not_biddable(self):
        session = MarketSession.objects.create(league=self.league_a, title="Solo A", status=MarketSession.Status.OPEN)
        loose = Player.objects.create(name="Senza lega", role="A", league=None, initial_price=Decimal("5"))
        for player in (self.player_b, loose):
            res = place_market_bid(session.id, self.team_a.id, player.id, 5)
            self.assertEqual(res["error"], "player_unavailable", player.name)
        self.assertTrue(place_market_bid(session.id, self.team_a.id, self.player_a.id, 5)["ok"])

    def test_schedule_sync_touches_only_the_league(self):
        past = timezone.now() - timedelta(minutes=5)
        draft_a = MarketSession.objects.create(league=self.league_a, status=MarketSession.Status.DRAFT, opens_at=past)
        draft_b = MarketSession.objects.create(league=self.league_b, status=MarketSession.Status.DRAFT, opens_at=past)
        sync_market_schedule(self.league_a)
        draft_a.refresh_from_db()
        draft_b.refresh_from_db()
        self.assertEqual(draft_a.status, MarketSession.Status.OPEN)
        self.assertEqual(draft_b.status, MarketSession.Status.DRAFT)

    def test_hub_and_regia_always_offer_a_new_market(self):
        # Trades open all season count as a running market: the button stays.
        self.league_a.trades_enabled = True
        self.league_a.save()
        self.client.force_login(self.owner)
        hub = self.client.get(reverse("admin_market_dashboard") + f"?league={self.league_a.id}")
        self.assertGreater(hub.context["n_live"], 0)
        self.assertContains(hub, 'onclick="openMarketWizard()"')
        self.assertContains(hub, 'id="market-wizard-modal"')
        regia = self.client.get(reverse("app_regia") + f"?league={self.league_a.id}")
        self.assertContains(regia, 'onclick="openMarketWizard()"')
        self.assertContains(regia, 'id="market-wizard-modal"')

    def test_coadmin_lands_on_the_console_from_the_portal(self):
        self.client.force_login(self.coadmin)
        resp = self.client.get(reverse("home"))
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        dash = self.client.get(reverse("dashboard"))
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.context["current_league"], self.league_a)

    def test_coadmin_gets_the_regia_of_their_league(self):
        self.client.force_login(self.coadmin)
        resp = self.client.get(reverse("app_regia"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["app_league"], self.league_a)


class MarketWizardParityTests(TestCase):
    """Web console and mobile app offer the very same «Nuovo Mercato» wizard."""

    KINDS = ("buste", "free_agency", "waiver_wire", "buyout_clause", "renewals", "live")

    def setUp(self):
        self.owner = User.objects.create_user("owner_wz", password="pw")
        self.league = League.objects.create(name="Lega Wizard", owner=self.owner)
        self.team = Participant.objects.create(
            display_name="Squadra Owner", league=self.league, credits=Decimal("300"), user=self.owner)
        self.client.force_login(self.owner)
        s = self.client.session
        s["participant_id"] = self.team.id
        s.save()

    def _pages(self):
        q = f"?league={self.league.id}"
        return {
            "console hub": self.client.get(reverse("admin_market_dashboard") + q),
            "console buste": self.client.get(reverse("admin_market_buste") + q),
            "app mercato": self.client.get(reverse("app_mercato")),
            "app regia": self.client.get(reverse("app_regia") + q),
        }

    @staticmethod
    def _wizard(resp):
        html = resp.content.decode()
        start = html.index('id="market-wizard-modal"')
        return html[start:html.index("</form>", start)]

    def test_every_page_offers_the_same_market_kinds_and_rules(self):
        wizards = {}
        for name, resp in self._pages().items():
            self.assertEqual(resp.status_code, 200, name)
            wizard = self._wizard(resp)
            for kind in self.KINDS:
                self.assertIn(f'name="market_kind" value="{kind}"', wizard, f"{name}: {kind}")
            for field in ("budget_rule", "tie_break", "max_bids", "max_acquisitions_p", "fa_max_moves",
                          "waiver_order_type", "buyout_multiplier", "refund_mode", "closes_at"):
                self.assertIn(f'name="{field}"', wizard, f"{name}: {field}")
            # Each page posts into its own league; only the return address differs.
            self.assertIn(f'name="league_id" value="{self.league.id}"', wizard, name)
            wizards[name] = re.sub(r'name="(from|csrfmiddlewaretoken)" value="[^"]*"', "", wizard)
        self.assertEqual(len(set(wizards.values())), 1, "il wizard differisce tra web e app")

    def test_wizard_creates_each_kind_in_the_league_only(self):
        other = League.objects.create(name="Altra")
        for kind, stype in (("buste", MarketSession.SessionType.SEALED_BIDS),
                            ("free_agency", MarketSession.SessionType.FREE_AGENCY),
                            ("waiver_wire", MarketSession.SessionType.WAIVER_WIRE),
                            ("buyout_clause", MarketSession.SessionType.BUYOUT_CLAUSE),
                            ("renewals", MarketSession.SessionType.RENEWALS)):
            self.client.post(reverse("admin_market_create"), {
                "league_id": self.league.id, "market_kind": kind, "title": f"T {kind}",
                "open_timing": "now", "opens_at": "2099-01-01T10:00", "from": "console",
            })
            session = MarketSession.objects.get(title=f"T {kind}")
            self.assertEqual(session.session_type, stype)
            self.assertEqual(session.league, self.league)
            # «Apri subito» ignores a date left in the hidden field.
            self.assertEqual(session.status, MarketSession.Status.OPEN, kind)
        self.assertFalse(MarketSession.objects.filter(league=other).exists())

    def test_free_agency_page_opens_in_the_app(self):
        MarketSession.objects.create(league=self.league, title="FA", status=MarketSession.Status.OPEN,
                                     session_type=MarketSession.SessionType.FREE_AGENCY, config={"fa_max_moves": 3})
        MarketSession.objects.create(league=self.league, title="Clausole", status=MarketSession.Status.OPEN,
                                     session_type=MarketSession.SessionType.BUYOUT_CLAUSE)
        for title in ("FA", "Clausole"):
            sid = MarketSession.objects.get(title=title).id
            resp = self.client.get(reverse("app_mercato") + f"?session_id={sid}")
            self.assertEqual(resp.status_code, 200, title)

    def test_new_and_open_wizard_links_open_it(self):
        resp = self.client.get(reverse("admin_market_buste") + f"?league={self.league.id}&new=1")
        self.assertTrue(resp.context["open_wizard"])
        resp = self.client.get(reverse("app_mercato") + "?open_wizard=1")
        self.assertTrue(resp.context["open_wizard"])


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
        url = reverse("admin_market_session", args=[self.session.id]) + "?preview=1"
        resp = self.client.get(url)
        self.assertContains(resp, "Anteprima:")
        self.assertContains(resp, "Esito previsto dello spoglio")
        self.x.refresh_from_db()
        self.assertIsNone(self.x.owner)

    def test_admin_tie_and_undo_views(self):
        self.client.force_login(self.root)
        self._tie()
        page = self.client.get(
            reverse("admin_market_session", args=[self.session.id])
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


class AppMercatoListTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Lista")
        self.me = Participant.objects.create(display_name="Io", league=self.league, credits=Decimal("40"))
        s = self.client.session
        s["participant_id"] = self.me.id
        s.save()

    def _get(self, **params):
        from urllib.parse import urlencode
        return self.client.get(reverse("app_mercato") + "?" + urlencode(params))

    def test_paginates_instead_of_truncating(self):
        for i in range(45):
            Player.objects.create(name=f"P{i:02d}", role="C", league=self.league, initial_price=Decimal(i + 1))
        resp = self._get()
        self.assertEqual(resp.context["page"].paginator.count, 45)
        self.assertEqual(len(resp.context["free_agents"]), 30)
        self.assertContains(resp, "Successivi")
        resp = self._get(page=2)
        self.assertEqual(len(resp.context["free_agents"]), 15)

    def test_budget_filter_and_mantra_quota(self):
        self.league.game_mode = League.GameMode.MANTRA
        self.league.save()
        Player.objects.create(name="Cheap", role="A", league=self.league, initial_price=Decimal("10"), price_m=Decimal("60"))
        Player.objects.create(name="Fair", role="A", league=self.league, initial_price=Decimal("50"), price_m=Decimal("30"))
        resp = self._get(budget="1")
        names = [p.name for p in resp.context["free_agents"]]
        self.assertEqual(names, ["Fair"])  # Mantra price 30 fits 40; 'Cheap' costs 60 in Mantra
        resp = self._get()
        self.assertEqual([p.name for p in resp.context["free_agents"]], ["Cheap", "Fair"])

    def test_watched_players_are_marked(self):
        from ..models import Watch
        pl = Player.objects.create(name="Obiettivo", role="D", league=self.league)
        Watch.objects.create(participant=self.me, player=pl)
        resp = self._get()
        self.assertContains(resp, "🎯")

    def test_home_reminds_open_market_and_trades(self):
        other = Participant.objects.create(display_name="Altro", league=self.league)
        Player.objects.create(name="Suo", role="A", league=self.league, owner=other)
        MarketSession.objects.create(league=self.league, title="Buste Gennaio", status=MarketSession.Status.OPEN)
        from ..services.trade import propose_trade
        propose_trade(other.id, self.me.id, [Player.objects.get(name="Suo").id], [])
        resp = self.client.get(reverse("app_home"))
        self.assertContains(resp, "Buste aperte: Buste Gennaio")
        self.assertContains(resp, "1 proposta di scambio")


class RegolamentoBusteTests(TestCase):
    """Regolamento 5.2: max offerte, più offerte sullo stesso giocatore,
    pari ruolo obbligatorio, totale entro il budget, pari merito al primo."""

    def setUp(self):
        self.league = League.objects.create(name="Lega Lugnanese", slots_p=3, slots_d=8, slots_c=8, slots_a=6)
        self.a = Participant.objects.create(display_name="Team A", league=self.league, credits=Decimal("200"))
        self.b = Participant.objects.create(display_name="Team B", league=self.league, credits=Decimal("200"))
        self.session = MarketSession.objects.create(
            league=self.league, title="Buste", status=MarketSession.Status.OPEN,
            max_bids=5, require_same_role_release=True,
            budget_rule=MarketSession.BudgetRule.TOTAL, tie_break=MarketSession.TieBreak.FIRST,
            release_refund_mode=Auction.RefundMode.NONE,
        )
        self.free = {n: Player.objects.create(name=n, role=r, league=self.league)
                     for n, r in (("Boban", "C"), ("Raul", "A"), ("R.Carlos", "D"), ("Iniesta", "C"))}
        self.cuts = {}
        for team in (self.a, self.b):
            for r in "CCADD":
                pl = Player.objects.create(name=f"{team.display_name} {r}{len(self.cuts)}", role=r,
                                           league=self.league, owner=team, cost=Decimal("5"))
                self.cuts.setdefault((team.id, r), []).append(pl)

    def _cut(self, team, role, i=0):
        return self.cuts[(team.id, role)][i]

    def _bid(self, team, name, amount, cut_index=0):
        pl = self.free[name]
        return place_market_bid(self.session.id, team.id, pl.id, amount,
                                release_player_id=self._cut(team, pl.role, cut_index % len(self.cuts[(team.id, pl.role)])).id)

    def _example(self, team, amounts):
        for (name, amount), i in zip(amounts, range(5)):
            res = self._bid(team, name, amount, cut_index=i)
            self.assertTrue(res["ok"], res)

    def test_example_from_the_rules(self):
        # Team A: 1 + 10 + 2 + 35 + 100 = 148 (valide)
        self._example(self.a, [("Boban", 1), ("Raul", 10), ("R.Carlos", 2), ("R.Carlos", 35), ("Iniesta", 100)])
        # Team B: 99 + 10 + 2 + 35 + 100 = 246 > 200 → si annulla Iniesta (100)
        self._example(self.b, [("Boban", 99), ("Raul", 10), ("R.Carlos", 2), ("R.Carlos", 35), ("Iniesta", 100)])
        resolve_market_session(self.session.id)

        b_iniesta = MarketBid.objects.get(participant=self.b, player=self.free["Iniesta"])
        self.assertEqual(b_iniesta.status, MarketBid.Status.CANCELLED)
        self.assertIn("superava il budget", b_iniesta.note)

        owner = lambda n: Player.objects.get(pk=self.free[n].pk)
        self.assertEqual(owner("Iniesta").owner, self.a)        # unica offerta valida
        self.assertEqual(owner("Boban").owner, self.b)          # 99 > 1
        self.assertEqual(owner("Boban").cost, Decimal("99"))
        # R.Carlos: 35 (A) vs 35 (B) → pari merito, vince chi ha inserito prima (A)
        self.assertEqual(owner("R.Carlos").owner, self.a)
        self.assertEqual(owner("R.Carlos").cost, Decimal("35"))  # paga l'offerta vincente
        a_low = MarketBid.objects.get(participant=self.a, player=self.free["R.Carlos"], amount=2)
        self.assertEqual(a_low.status, MarketBid.Status.LOST)
        self.assertIn("tua offerta più alta", a_low.note)
        b_rc = MarketBid.objects.get(participant=self.b, player=self.free["R.Carlos"], amount=35)
        self.assertIn("inserita prima", b_rc.note)
        # Raul 10 vs 10 → vince A (prima)
        self.assertEqual(owner("Raul").owner, self.a)

    def test_example_from_the_august_2026_rules(self):
        # Team A: Boban 1, Raul 10, R.Carlos 2/35/100 → offerte massime 1+10+100 = 111 (valide)
        self._example(self.a, [("Boban", 1), ("Raul", 10), ("R.Carlos", 2), ("R.Carlos", 35), ("R.Carlos", 100)])
        # Team B: Boban 99, Raul 10, R.Carlos 2/35/100 → 209 > 200: si annulla R.Carlos 100
        self._example(self.b, [("Boban", 99), ("Raul", 10), ("R.Carlos", 2), ("R.Carlos", 35), ("R.Carlos", 100)])
        resolve_market_session(self.session.id)
        b100 = MarketBid.objects.get(participant=self.b, player=self.free["R.Carlos"], amount=100)
        self.assertEqual(b100.status, MarketBid.Status.CANCELLED)
        a100 = MarketBid.objects.get(participant=self.a, player=self.free["R.Carlos"], amount=100)
        self.assertEqual(a100.status, MarketBid.Status.WON)
        rc = Player.objects.get(pk=self.free["R.Carlos"].pk)
        self.assertEqual((rc.owner, rc.cost), (self.a, Decimal("100")))  # la minima che batte il 35 di B
        boban = Player.objects.get(pk=self.free["Boban"].pk)
        self.assertEqual((boban.owner, boban.cost), (self.b, Decimal("99")))

    def test_pays_the_lowest_own_offer_that_wins(self):
        for amount, i in ((2, 0), (35, 1), (100, 0)):
            self.assertTrue(self._bid(self.a, "R.Carlos", amount, cut_index=i)["ok"])
        self._bid(self.b, "R.Carlos", 20)
        resolve_market_session(self.session.id)
        rc = Player.objects.get(pk=self.free["R.Carlos"].pk)
        self.assertEqual((rc.owner, rc.cost), (self.a, Decimal("35")))
        unused = MarketBid.objects.get(participant=self.a, amount=100)
        self.assertIn("Non necessaria", unused.note)
        self.assertEqual(MarketBid.objects.get(participant=self.a, amount=2).status, MarketBid.Status.LOST)

    def test_unopposed_pays_its_minimum_offer(self):
        self._bid(self.a, "Boban", 1)
        self._bid(self.a, "Boban", 50, cut_index=1)
        resolve_market_session(self.session.id)
        self.assertEqual(Player.objects.get(pk=self.free["Boban"].pk).cost, Decimal("1"))

    def test_tie_second_round(self):
        from ..services.market import settle_market_tie
        self.session.tie_break = MarketSession.TieBreak.REBID
        self.session.save()
        self._bid(self.a, "Raul", 10)
        self._bid(self.b, "Raul", 10)
        summary = resolve_market_session(self.session.id)
        self.assertEqual(summary["total_ties"], 1)
        raul = self.free["Raul"]
        res = settle_market_tie(self.session.id, raul.id, rebids={self.a.id: 12, self.b.id: 12})
        self.assertFalse(res["ok"])  # nuovo pari merito
        res = settle_market_tie(self.session.id, raul.id, rebids={self.a.id: 12, self.b.id: 15})
        self.assertTrue(res["ok"], res)
        raul.refresh_from_db()
        self.assertEqual((raul.owner, raul.cost), (self.b, Decimal("15")))

    def test_every_purchase_replaces_a_same_role_player(self):
        self._bid(self.a, "Iniesta", 20)
        resolve_market_session(self.session.id)
        cut = Player.objects.get(pk=self._cut(self.a, "C").pk)
        self.assertIsNone(cut.owner)
        counts = Player.objects.filter(owner=self.a, role="C").count()
        self.assertEqual(counts, 2)  # 2 C prima, 1 tagliato, 1 acquistato

    def test_max_bids(self):
        self._example(self.a, [("Boban", 1), ("Raul", 1), ("R.Carlos", 1), ("R.Carlos", 2), ("Iniesta", 1)])
        res = self._bid(self.a, "Boban", 3)
        self.assertFalse(res["ok"])
        self.assertIn("massimo di 5", res["message"])

    def test_cut_is_required_and_same_role(self):
        res = place_market_bid(self.session.id, self.a.id, self.free["Iniesta"].id, 10)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "release_required")
        res = place_market_bid(self.session.id, self.a.id, self.free["Iniesta"].id, 10,
                               release_player_id=self._cut(self.a, "A").id)
        self.assertEqual(res["error"], "release_wrong_role")

    def test_purchase_fails_if_the_cut_left_the_roster(self):
        self._bid(self.a, "Iniesta", 20)
        Player.objects.filter(pk=self._cut(self.a, "C").pk).update(owner=self.b)
        resolve_market_session(self.session.id)
        bid = MarketBid.objects.get(participant=self.a)
        self.assertEqual(bid.status, MarketBid.Status.LOST)
        self.assertIn("pari ruolo", bid.note)

    def test_manual_tie_break_keeps_ties_for_the_admin(self):
        self.session.tie_break = MarketSession.TieBreak.MANUAL
        self.session.save()
        self._bid(self.a, "Raul", 10)
        self._bid(self.b, "Raul", 10)
        summary = resolve_market_session(self.session.id)
        self.assertEqual(summary["total_ties"], 1)

    def test_admin_edits_rules(self):
        root = User.objects.create_superuser("root", "r@x.local", "pw")
        self.client.force_login(root)
        self.client.post(reverse("admin_market_rules", args=[self.session.id]), {
            "max_bids": "3", "budget_rule": "priority", "tie_break": "manual", "refund_mode": "current",
            # The dialog names its checkboxes, so an unticked one switches off.
            "checks": ["require_same_role_release", "allow_conditional_release"],
        })
        self.session.refresh_from_db()
        self.assertEqual(self.session.max_bids, 3)
        self.assertEqual(self.session.budget_rule, "priority")
        self.assertEqual(self.session.tie_break, "manual")
        self.assertFalse(self.session.require_same_role_release)
        self.assertEqual(self.session.release_refund_mode, "current")
        page = self.client.get(reverse("admin_market_session", args=[self.session.id]))
        self.assertContains(page, "max 3 per squadra")

    def test_app_shows_rules_and_same_role_choices(self):
        s = self.client.session
        s["participant_id"] = self.a.id
        s.save()
        self._bid(self.a, "Boban", 5)
        resp = self.client.get(reverse("app_mercato"))
        self.assertContains(resp, "1/5")
        self.assertContains(resp, "pari ruolo")
        self.assertContains(resp, 'data-same-role="1"')
        self.assertNotContains(resp, "Priorità di scelta")


class MarketManageParityTests(TestCase):
    """Managing a session and the trades: the same screen in console and app."""

    def setUp(self):
        self.owner = User.objects.create_user("owner_mg", password="pw")
        self.other = User.objects.create_user("other_mg", password="pw")
        self.league = League.objects.create(name="Lega Gestione", owner=self.owner, trades_enabled=True)
        League.objects.create(name="Lega Altrui", owner=self.other)
        self.team = Participant.objects.create(display_name="Owner FC", league=self.league,
                                               credits=Decimal("300"), user=self.owner)
        Participant.objects.create(display_name="Rivali", league=self.league, credits=Decimal("300"))
        self.session = MarketSession.objects.create(league=self.league, title="Buste Ottobre",
                                                    status=MarketSession.Status.OPEN)
        self.client.force_login(self.owner)

    @staticmethod
    def _part(resp, tag):
        html = resp.content.decode()
        part = html[html.index(f"<!-- {tag}:start -->"):html.index(f"<!-- {tag}:end -->")]
        return re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)

    def test_session_screen_is_the_same_in_console_and_app(self):
        console = self.client.get(reverse("admin_market_session", args=[self.session.id]))
        app = self.client.get(reverse("app_regia_market_session", args=[self.session.id]))
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        self.assertContains(app, "Chiudi finestra")
        self.assertContains(app, "Scrutina buste")
        self.assertEqual(self._part(console, "session-manage"), self._part(app, "session-manage"))

    def test_trades_screen_is_the_same_in_console_and_app(self):
        q = f"?league={self.league.id}"
        console = self.client.get(reverse("admin_market_trades") + q)
        app = self.client.get(reverse("app_regia_trades") + q)
        self.assertEqual(app.status_code, 200)
        self.assertContains(app, "Regole degli scambi")
        self.assertContains(app, "Periodi scambi")
        self.assertEqual(self._part(console, "trades-manage"), self._part(app, "trades-manage"))

    def test_actions_from_the_app_land_back_in_the_app(self):
        back = reverse("app_regia_market_session", args=[self.session.id])
        resp = self.client.post(reverse("admin_market_status", args=[self.session.id]),
                                {"status": "closed", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MarketSession.Status.CLOSED)

        trades_back = reverse("app_regia_trades") + f"?league={self.league.id}"
        resp = self.client.post(reverse("admin_trade_settings"), {
            "league_id": self.league.id, "trades_need_approval": "1", "next": trades_back})
        self.assertRedirects(resp, trades_back, fetch_redirect_response=False)
        self.league.refresh_from_db()
        self.assertFalse(self.league.trades_enabled)
        self.assertTrue(self.league.trades_need_approval)

    def test_delete_from_the_app_lands_on_the_regia(self):
        regia = reverse("app_regia") + f"?league={self.league.id}"
        resp = self.client.post(reverse("admin_market_delete", args=[self.session.id]), {"next": regia})
        self.assertRedirects(resp, regia, fetch_redirect_response=False)
        self.assertFalse(MarketSession.objects.filter(pk=self.session.id).exists())

    def test_next_to_another_site_is_ignored(self):
        resp = self.client.post(reverse("admin_market_status", args=[self.session.id]),
                                {"status": "closed", "next": "https://evil.example/x"})
        self.assertRedirects(resp, reverse("admin_market_session", args=[self.session.id]),
                             fetch_redirect_response=False)
        resp = self.client.post(reverse("admin_market_create"), {
            "league_id": self.league.id, "market_kind": "buste", "title": "X", "next": "https://evil.example/x"})
        self.assertNotIn("evil.example", resp["Location"])

    def test_other_admin_cannot_open_the_app_screen(self):
        self.client.force_login(self.other)
        resp = self.client.get(reverse("app_regia_market_session", args=[self.session.id]))
        self.assertEqual(resp.status_code, 403)

    def test_regia_links_to_the_app_screens(self):
        resp = self.client.get(reverse("app_regia") + f"?league={self.league.id}")
        self.assertContains(resp, reverse("app_regia_market_session", args=[self.session.id]))
        self.assertContains(resp, reverse("app_regia_trades"))


class SessionManageByTypeTests(TestCase):
    """Each kind of market shows and edits its own rules, not the buste ones."""

    def setUp(self):
        self.owner = User.objects.create_user("owner_kind", password="pw")
        self.league = League.objects.create(name="Lega Tipi", owner=self.owner, contracts_enabled=True)
        self.a = Participant.objects.create(display_name="Alfa", league=self.league, credits=Decimal("300"))
        self.b = Participant.objects.create(display_name="Beta", league=self.league, credits=Decimal("300"))
        # Alfa: one expiring contract still to declare and one new buy with no
        # contract yet; Beta: one renewal declared, waiting for the dice.
        Player.objects.create(league=self.league, owner=self.a, name="Scaduto", role="C", contract_years=0)
        Player.objects.create(league=self.league, owner=self.a, name="Nuovo", role="A", contract_years=None)
        Player.objects.create(league=self.league, owner=self.b, name="Dichiarato", role="D",
                              contract_years=0, renewal_declared=True)
        Player.objects.create(league=self.league, owner=self.b, name="Coperto", role="P", contract_years=2)
        self.renewals = MarketSession.objects.create(
            league=self.league, title="Rinnovi", session_type=MarketSession.SessionType.RENEWALS,
            status=MarketSession.Status.OPEN, config={"description": "x"})
        self.client.force_login(self.owner)

    def test_renewals_screen_shows_renewals_not_buste(self):
        console = self.client.get(reverse("admin_market_session", args=[self.renewals.id]))
        app = self.client.get(reverse("app_regia_market_session", args=[self.renewals.id]))
        for resp in (console, app):
            self.assertContains(resp, "Rinnovi aperti")
            self.assertContains(resp, "Rinnovi delle squadre")
            self.assertContains(resp, "Dado rinnovo:")
            self.assertContains(resp, "1-1-2-2-3-3 anni")
            for buste in ("Consegna delle buste", "Offerte:", "Pari merito:", "Anteprima spoglio",
                          "Svincolati disponibili", "Offerte massime per squadra"):
                self.assertNotContains(resp, buste)
        board = {r["participant"].display_name: r for r in console.context["renewals"]["rows"]}
        self.assertEqual((board["Alfa"]["to_declare"], board["Alfa"]["to_roll"], board["Alfa"]["new_contracts"]), (1, 0, 1))
        self.assertEqual((board["Beta"]["to_declare"], board["Beta"]["to_roll"], board["Beta"]["new_contracts"]), (0, 1, 0))
        self.assertEqual(console.context["delivered"], 0)
        self.assertEqual(MarketManageParityTests._part(console, "session-manage"),
                         MarketManageParityTests._part(app, "session-manage"))

    def test_preview_is_not_offered_for_renewals(self):
        resp = self.client.get(reverse("admin_market_session", args=[self.renewals.id]) + "?preview=1")
        self.assertIsNone(resp.context["results"])

    def test_saving_rules_keeps_the_renewals_type(self):
        self.client.post(reverse("admin_market_rules", args=[self.renewals.id]), {"title": "Rinnovi 2026"})
        self.renewals.refresh_from_db()
        self.assertEqual(self.renewals.session_type, MarketSession.SessionType.RENEWALS)
        self.assertEqual(self.renewals.title, "Rinnovi 2026")
        self.assertEqual(self.renewals.config, {"description": "x"})

    def test_free_agency_rules_edit_their_own_settings(self):
        fa = MarketSession.objects.create(
            league=self.league, title="FA", session_type=MarketSession.SessionType.FREE_AGENCY,
            status=MarketSession.Status.OPEN, max_bids=4,
            config={"fa_max_moves": 3, "fa_cost_type": "quotation", "buyout_multiplier": 2.0})
        page = self.client.get(reverse("admin_market_session", args=[fa.id]))
        self.assertContains(page, 'name="fa_max_moves"')
        self.assertNotContains(page, 'name="max_bids"')
        self.client.post(reverse("admin_market_rules", args=[fa.id]), {
            "fa_max_moves": "1", "fa_cost_type": "base", "refund_mode": "none",
            "checks": ["allow_conditional_release"]})
        fa.refresh_from_db()
        self.assertEqual(fa.session_type, MarketSession.SessionType.FREE_AGENCY)
        self.assertEqual(fa.config, {"fa_max_moves": 1, "fa_cost_type": "base", "buyout_multiplier": 2.0})
        self.assertEqual(fa.max_bids, 4)
        self.assertFalse(fa.allow_conditional_release)
        self.assertEqual(fa.release_refund_mode, "none")

    def test_closed_renewals_cannot_be_undone(self):
        resolve_market_session(self.renewals.id)
        res = undo_market_resolution(self.renewals.id)
        self.assertFalse(res["ok"])
        page = self.client.get(reverse("admin_market_session", args=[self.renewals.id]))
        self.assertContains(page, "Rinnovi chiusi")
        self.assertContains(page, "Esito dei rinnovi")
        self.assertNotContains(page, "Annulla spoglio")
        self.assertNotContains(page, "Verbale (PDF)")

    def test_team_page_speaks_renewals(self):
        s = self.client.session
        s["participant_id"] = self.a.id
        s.save()
        resp = self.client.get(reverse("app_mercato") + f"?session_id={self.renewals.id}")
        self.assertContains(resp, "Rinnovi aperti")
        self.assertNotContains(resp, "Buste aperte")
        # A multi-line {# #} is not a comment for Django: it was printed on the page.
        self.assertNotContains(resp, "Dado animato per i contratti")
