from decimal import Decimal
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import League, Participant, Player, MarketSession, MarketBid, RosterLog
from ..services.market import (
    acquire_free_agent,
    execute_buyout,
    place_waiver_claim,
    delete_waiver_claim,
    resolve_waiver_session,
    resolve_market_session,
    place_market_bid,
    buyout_price,
    undo_market_resolution,
    waiver_order,
)


class FreeAgencyTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega FA", budget=Decimal("500"))
        self.p1 = Participant.objects.create(
            league=self.league,
            display_name="Squadra 1",
            credits=Decimal("500"),
            spent_credits=Decimal("100"),
        )
        self.session = MarketSession.objects.create(
            league=self.league,
            title="Free Agency Live",
            session_type=MarketSession.SessionType.FREE_AGENCY,
            status=MarketSession.Status.OPEN,
            config={
                "fa_max_moves": 2,
                "fa_cost_type": "quotation",
            },
        )
        self.player1 = Player.objects.create(
            league=self.league,
            name="Calciatore 1",
            role="A",
            team="Atalanta",
            initial_price=Decimal("15"),
        )
        self.player2 = Player.objects.create(
            league=self.league,
            name="Calciatore 2",
            role="C",
            team="Milan",
            initial_price=Decimal("10"),
        )
        self.player3 = Player.objects.create(
            league=self.league,
            name="Calciatore 3",
            role="D",
            team="Inter",
            initial_price=Decimal("5"),
        )

    def test_instant_buy_success(self):
        res = acquire_free_agent(self.session.id, self.p1.id, self.player1.id)
        self.assertTrue(res["ok"])
        
        self.player1.refresh_from_db()
        self.assertEqual(self.player1.owner, self.p1)
        self.assertEqual(self.player1.cost, Decimal("15"))
        
        self.p1.refresh_from_db()
        # Initial spent: 100 + 15 = 115
        self.assertEqual(self.p1.spent_credits, Decimal("115"))

    def test_weekly_moves_limit(self):
        res1 = acquire_free_agent(self.session.id, self.p1.id, self.player1.id)
        self.assertTrue(res1["ok"])

        res2 = acquire_free_agent(self.session.id, self.p1.id, self.player2.id)
        self.assertTrue(res2["ok"])

        # 3rd move exceeds limit of 2
        res3 = acquire_free_agent(self.session.id, self.p1.id, self.player3.id)
        self.assertFalse(res3["ok"])
        self.assertIn("limite di 2 cambi", res3["message"])

    def test_instant_buy_with_release_refund(self):
        # Give p1 an existing player
        old_player = Player.objects.create(
            league=self.league,
            name="Vecchio",
            role="A",
            team="Lecce",
            owner=self.p1,
            cost=Decimal("10"),
            initial_price=Decimal("8"),
        )
        # Session refund mode is PURCHASE (default)
        res = acquire_free_agent(
            self.session.id,
            self.p1.id,
            self.player1.id,
            release_player_id=old_player.id,
        )
        self.assertTrue(res["ok"])
        
        old_player.refresh_from_db()
        self.assertIsNone(old_player.owner)
        
        self.p1.refresh_from_db()
        # Initial spent: 100 - 10 (refund) + 15 (buy) = 105
        self.assertEqual(self.p1.spent_credits, Decimal("105"))


class BuyoutClauseTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Clausole", budget=Decimal("500"))
        self.seller = Participant.objects.create(
            league=self.league,
            display_name="Venditore",
            credits=Decimal("500"),
            spent_credits=Decimal("200"),
        )
        self.buyer = Participant.objects.create(
            league=self.league,
            display_name="Acquirente",
            credits=Decimal("500"),
            spent_credits=Decimal("100"),
        )
        self.session = MarketSession.objects.create(
            league=self.league,
            title="Mercato Clausole",
            session_type=MarketSession.SessionType.BUYOUT_CLAUSE,
            status=MarketSession.Status.OPEN,
            config={
                "buyout_multiplier": 1.5,
                "buyout_min_hold_days": 7,
            },
        )
        self.player = Player.objects.create(
            league=self.league,
            name="Stella",
            role="A",
            team="Juventus",
            owner=self.seller,
            cost=Decimal("20"),
            acquired_at=timezone.now() - timedelta(days=10), # held for 10 days
        )

    def test_execute_buyout_success(self):
        # Cost is 20 * 1.5 = 30 FM
        res = execute_buyout(self.session.id, self.buyer.id, self.player.id)
        self.assertTrue(res["ok"])
        
        self.player.refresh_from_db()
        self.assertEqual(self.player.owner, self.buyer)
        self.assertEqual(self.player.cost, Decimal("30"))
        
        self.buyer.refresh_from_db()
        # Buyer: 100 + 30 = 130
        self.assertEqual(self.buyer.spent_credits, Decimal("130"))
        
        self.seller.refresh_from_db()
        # Seller gets credited 30 FM: 200 - 30 = 170
        self.assertEqual(self.seller.spent_credits, Decimal("170"))

    def test_buyout_protection_period_blocks(self):
        # Set acquired_at to 2 days ago (within 7 days protection)
        self.player.acquired_at = timezone.now() - timedelta(days=2)
        self.player.save(update_fields=["acquired_at"])

        res = execute_buyout(self.session.id, self.buyer.id, self.player.id)
        self.assertFalse(res["ok"])
        self.assertIn("protetto", res["message"])


class WaiverWireDraftTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Waiver", budget=Decimal("500"))
        self.team1 = Participant.objects.create(league=self.league, display_name="Team A", credits=Decimal("500"), spent_credits=Decimal("50"))
        self.team2 = Participant.objects.create(league=self.league, display_name="Team B", credits=Decimal("500"), spent_credits=Decimal("50"))
        self.team3 = Participant.objects.create(league=self.league, display_name="Team C", credits=Decimal("500"), spent_credits=Decimal("50"))

        self.session = MarketSession.objects.create(
            league=self.league,
            title="Waiver Wire Round 1",
            session_type=MarketSession.SessionType.WAIVER_WIRE,
            status=MarketSession.Status.OPEN,
            config={
                "waiver_order_type": "rolling",
                # Priority order: Team C (#1), Team B (#2), Team A (#3)
                "waiver_order": [self.team3.id, self.team2.id, self.team1.id],
            },
        )
        self.player_target = Player.objects.create(
            league=self.league,
            name="Obiettivo Caldo",
            role="A",
            team="Napoli",
            initial_price=Decimal("20"),
        )
        self.player_other = Player.objects.create(
            league=self.league,
            name="Altro Giocatore",
            role="C",
            team="Roma",
            initial_price=Decimal("10"),
        )

    def test_place_and_delete_claim(self):
        res = place_waiver_claim(self.session.id, self.team1.id, self.player_target.id, priority=1)
        self.assertTrue(res["ok"])
        bid_id = res["bid_id"]

        bid = MarketBid.objects.get(pk=bid_id)
        self.assertEqual(bid.priority, 1)
        self.assertEqual(bid.status, MarketBid.Status.PENDING)

        del_res = delete_waiver_claim(self.session.id, self.team1.id, bid_id)
        self.assertTrue(del_res["ok"])
        self.assertFalse(MarketBid.objects.filter(pk=bid_id).exists())

    def test_waiver_draft_resolution_order(self):
        # Both Team A and Team C claim 'Obiettivo Caldo' as priority 1.
        # Team C has higher draft priority (rolling order: [C, B, A]).
        place_waiver_claim(self.session.id, self.team1.id, self.player_target.id, priority=1)
        place_waiver_claim(self.session.id, self.team3.id, self.player_target.id, priority=1)
        
        # Team A also claimed player_other as priority 2
        place_waiver_claim(self.session.id, self.team1.id, self.player_other.id, priority=2)

        res = resolve_waiver_session(self.session.id)
        self.assertEqual(res["total_acquisitions"], 2)

        self.player_target.refresh_from_db()
        # Team C gets player_target because Team C had 1st pick
        self.assertEqual(self.player_target.owner, self.team3)

        self.player_other.refresh_from_db()
        # Team A gets player_other
        self.assertEqual(self.player_other.owner, self.team1)


class MarketEndpointsIntegrationTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Endpoints", budget=Decimal("500"))
        self.team1 = Participant.objects.create(
            league=self.league,
            display_name="Squadra 1",
            credits=Decimal("500"),
            spent_credits=Decimal("50"),
        )
        self.team2 = Participant.objects.create(
            league=self.league,
            display_name="Squadra 2",
            credits=Decimal("500"),
            spent_credits=Decimal("80"),
        )
        self.player_free = Player.objects.create(
            league=self.league,
            name="Svincolato Doc",
            role="C",
            team="Atalanta",
            initial_price=Decimal("15"),
        )
        self.player_owned = Player.objects.create(
            league=self.league,
            owner=self.team2,
            name="Campione Avversario",
            role="A",
            team="Inter",
            cost=Decimal("40"),
            initial_price=Decimal("40"),
        )

    def test_free_agency_buy_endpoint(self):
        fa_session = MarketSession.objects.create(
            league=self.league,
            title="Free Agency",
            session_type=MarketSession.SessionType.FREE_AGENCY,
            status=MarketSession.Status.OPEN,
            config={"fa_cost_type": "quotation"},
        )
        session = self.client.session
        session["participant_id"] = self.team1.id
        session.save()

        resp = self.client.post(
            reverse("app_market_free_agency_buy"),
            data=json.dumps({
                "session_id": fa_session.id,
                "player_id": self.player_free.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))

        self.player_free.refresh_from_db()
        self.assertEqual(self.player_free.owner, self.team1)

    def test_buyout_execute_endpoint(self):
        bc_session = MarketSession.objects.create(
            league=self.league,
            title="Finestra Clausole",
            session_type=MarketSession.SessionType.BUYOUT_CLAUSE,
            status=MarketSession.Status.OPEN,
            config={"buyout_multiplier": 1.5, "buyout_compensation": "seller"},
        )
        session = self.client.session
        session["participant_id"] = self.team1.id
        session.save()

        # Buyout price is 40 * 1.5 = 60 FM
        resp = self.client.post(
            reverse("app_market_buyout_execute"),
            data=json.dumps({
                "session_id": bc_session.id,
                "player_id": self.player_owned.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))

        self.player_owned.refresh_from_db()
        self.assertEqual(self.player_owned.owner, self.team1)
        self.assertEqual(self.player_owned.cost, Decimal("60"))

    def test_waiver_claim_and_delete_endpoints(self):
        ww_session = MarketSession.objects.create(
            league=self.league,
            title="Waiver Wire Draft",
            session_type=MarketSession.SessionType.WAIVER_WIRE,
            status=MarketSession.Status.OPEN,
            config={"waiver_order_type": "rolling"},
        )
        session = self.client.session
        session["participant_id"] = self.team1.id
        session.save()

        # 1. Claim
        resp = self.client.post(
            reverse("app_market_waiver_claim"),
            data=json.dumps({
                "session_id": ww_session.id,
                "player_id": self.player_free.id,
                "priority": 1,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(len(data.get("my_claims", [])), 1)
        claim_id = data["claim_id"]

        # 2. Delete claim
        del_resp = self.client.post(
            reverse("app_market_waiver_delete"),
            data=json.dumps({
                "session_id": ww_session.id,
                "claim_id": claim_id,
            }),
            content_type="application/json",
        )
        self.assertEqual(del_resp.status_code, 200)
        del_data = del_resp.json()
        self.assertTrue(del_data.get("ok"))
        self.assertEqual(len(del_data.get("my_claims", [])), 0)

    def test_unauthenticated_endpoints(self):
        resp = self.client.post(
            reverse("app_market_free_agency_buy"),
            data=json.dumps({"session_id": 999, "player_id": 999}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_renewals_session_resolution(self):
        self.league.contracts_enabled = True
        self.league.renewals_open = True
        self.league.save()

        p_keep = Player.objects.create(
            league=self.league, owner=self.team1, name="Da Rinnovare",
            role="C", team="Milan", cost=Decimal("50"), contract_years=0,
            renewal_declared=True,
        )
        p_drop = Player.objects.create(
            league=self.league, owner=self.team1, name="Non Dichiarato",
            role="D", team="Lecce", cost=Decimal("10"), contract_years=0,
            renewal_declared=None,
        )

        session = MarketSession.objects.create(
            league=self.league,
            title="Mercato Rinnovi Contratti",
            session_type=MarketSession.SessionType.RENEWALS,
            status=MarketSession.Status.OPEN,
        )

        summary = resolve_market_session(session.id)
        session.refresh_from_db()
        self.assertEqual(session.status, MarketSession.Status.RESOLVED)

        # Non-declared player must be released
        p_drop.refresh_from_db()
        self.assertIsNone(p_drop.owner)

        # Renewals window closed
        self.league.refresh_from_db()
        self.assertFalse(self.league.renewals_open)
        self.assertIn("released", summary)

    def test_buste_spoglio_results_in_app_mercato(self):
        session = MarketSession.objects.create(
            league=self.league,
            title="Buste Riparazione",
            session_type=MarketSession.SessionType.SEALED_BIDS,
            status=MarketSession.Status.OPEN,
        )
        place_market_bid(session.id, self.team1.id, self.player_free.id, amount=20, priority=1)
        resolve_market_session(session.id)

        s = self.client.session
        s["participant_id"] = self.team1.id
        s.save()

        resp = self.client.get(reverse("app_mercato") + f"?session_id={session.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("buste_results", resp.context)
        self.assertEqual(len(resp.context["buste_results"]["my_won"]), 1)
        self.assertContains(resp, "Verbale di Spoglio Ufficiale")

    def test_renewals_workspace_in_app_mercato(self):
        self.league.contracts_enabled = True
        self.league.save()

        p_exp = Player.objects.create(
            league=self.league, owner=self.team1, name="In Scadenza",
            role="A", team="Roma", cost=Decimal("80"), contract_years=0,
        )
        session = MarketSession.objects.create(
            league=self.league,
            title="Sessione Rinnovi",
            session_type=MarketSession.SessionType.RENEWALS,
            status=MarketSession.Status.OPEN,
        )

        s = self.client.session
        s["participant_id"] = self.team1.id
        s.save()

        resp = self.client.get(reverse("app_mercato") + f"?session_id={session.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["is_renewals"])
        self.assertEqual(len(resp.context["my_expiring"]), 1)
        self.assertContains(resp, "Fase 1: Dichiarazione Rinnovi")




class MarketModeRulesTests(TestCase):
    """Each kind of market keeps to its own settings (free agency, clauses, waiver)."""

    def setUp(self):
        self.owner = User.objects.create_user("owner_modes", password="pw")
        self.league = League.objects.create(name="Lega Modi", budget=Decimal("500"), owner=self.owner)
        self.a = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.b = Participant.objects.create(league=self.league, display_name="Beta", credits=Decimal("500"))
        self.free = [
            Player.objects.create(league=self.league, name=f"Libero {i}", role="A", team="Pisa",
                                  initial_price=Decimal("10"))
            for i in range(4)
        ]
        self.mine = Player.objects.create(league=self.league, owner=self.a, name="Mio", role="A",
                                          team="Como", cost=Decimal("8"))

    def _session(self, kind, **kw):
        return MarketSession.objects.create(league=self.league, title=kind, session_type=kind,
                                            status=MarketSession.Status.OPEN, **kw)

    def test_cuts_follow_the_session_rules(self):
        fa = self._session("free_agency", allow_conditional_release=False)
        res = acquire_free_agent(fa.id, self.a.id, self.free[0].id, release_player_id=self.mine.id)
        self.assertEqual(res["error"], "release_not_allowed")
        bo = self._session("buyout_clause", allow_conditional_release=False, config={"buyout_min_hold_days": 0})
        theirs = Player.objects.create(league=self.league, owner=self.b, name="Suo", role="A", cost=Decimal("10"))
        res = execute_buyout(bo.id, self.a.id, theirs.id, release_player_id=self.mine.id)
        self.assertEqual(res["error"], "release_not_allowed")
        ww = self._session("waiver_wire", require_same_role_release=True)
        res = place_waiver_claim(ww.id, self.a.id, self.free[0].id)
        self.assertEqual(res["error"], "release_required")

    def test_each_entry_point_only_serves_its_own_kind(self):
        ww = self._session("waiver_wire")
        res = place_market_bid(ww.id, self.a.id, self.free[0].id, 1)
        self.assertEqual(res["error"], "invalid_session_type")
        buste = self._session("sealed_bids")
        res = place_waiver_claim(buste.id, self.a.id, self.free[0].id)
        self.assertEqual(res["error"], "invalid_session_type")

    def test_free_agency_moves_belong_to_the_session_not_its_title(self):
        fa = self._session("free_agency", config={"fa_max_moves": 2})
        self.assertTrue(acquire_free_agent(fa.id, self.a.id, self.free[0].id)["ok"])
        self.assertTrue(acquire_free_agent(fa.id, self.a.id, self.free[1].id)["ok"])
        fa.title = "Rinominata"
        fa.save()
        res = acquire_free_agent(fa.id, self.a.id, self.free[2].id)
        self.assertEqual(res["error"], "move_limit_reached")
        # An older window with the same title doesn't end up in this one's summary.
        RosterLog.objects.filter(participant=self.a).update(created_at=timezone.now() - timedelta(days=30))
        newer = self._session("free_agency")
        self.assertEqual(resolve_market_session(newer.id)["total_acquisitions"], 0)
        self.assertFalse(undo_market_resolution(newer.id)["ok"])

    def test_clause_price_has_no_float_rounding(self):
        bo = self._session("buyout_clause", config={"buyout_multiplier": 1.1})
        player = Player(cost=Decimal("50"))
        self.assertEqual(buyout_price(bo, player), Decimal("55"))

    def test_waiver_skips_players_taken_meanwhile_and_cuts_once(self):
        ww = self._session("waiver_wire", config={"waiver_order_type": "rolling", "waiver_order": [self.a.id, self.b.id]})
        place_waiver_claim(ww.id, self.a.id, self.free[0].id, priority=1, release_player_id=self.mine.id)
        place_waiver_claim(ww.id, self.a.id, self.free[1].id, priority=2, release_player_id=self.mine.id)
        place_waiver_claim(ww.id, self.b.id, self.free[2].id, priority=1)
        Player.objects.filter(pk=self.free[2].pk).update(owner=self.a)  # taken elsewhere
        summary = resolve_waiver_session(ww.id)
        cuts = [w for w in summary["won"] if w["released_player_id"] == self.mine.id]
        self.assertEqual(len(cuts), 1)
        self.free[2].refresh_from_db()
        self.assertEqual(self.free[2].owner, self.a)
        self.assertNotIn("Libero 2", [w["player_name"] for w in summary["won"]])

    def test_rolling_order_carries_over_and_undo_restores_it(self):
        first = self._session("waiver_wire", config={"waiver_order_type": "rolling", "waiver_order": [self.a.id, self.b.id]})
        place_waiver_claim(first.id, self.a.id, self.free[0].id)
        resolve_waiver_session(first.id)
        first.refresh_from_db()
        rotated = first.config["waiver_order"]
        self.assertEqual(rotated, [self.b.id, self.a.id])
        second = self._session("waiver_wire", config={"waiver_order_type": "rolling"})
        self.assertEqual(waiver_order(second, {self.a.id: self.a, self.b.id: self.b}), rotated)
        self.assertTrue(undo_market_resolution(first.id)["ok"])
        first.refresh_from_db()
        self.assertEqual(first.config["waiver_order"], [self.a.id, self.b.id])

    def test_waiver_window_closes_after_the_chosen_hours(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_market_create"), {
            "league_id": self.league.id, "market_kind": "waiver_wire", "title": "W", "waiver_claim_hours": "48"})
        ww = MarketSession.objects.get(title="W")
        self.assertAlmostEqual((ww.closes_at - ww.created_at).total_seconds(), 48 * 3600, delta=60)

    def test_free_agency_screen_counts_purchases_and_concludes(self):
        fa = self._session("free_agency")
        acquire_free_agent(fa.id, self.a.id, self.free[0].id)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("admin_market_session", args=[fa.id]))
        self.assertEqual(page.context["s"].n_bids, 1)
        self.assertEqual(page.context["delivered"], 1)
        self.assertContains(page, "Concludi la finestra")
        self.assertNotContains(page, "Anteprima spoglio")
        self.assertNotContains(page, "Offerte massime per squadra")

    def test_team_rules_tab_has_no_envelope_rules_outside_buste(self):
        fa = self._session("free_agency")
        s = self.client.session
        s["participant_id"] = self.a.id
        s.save()
        page = self.client.get(reverse("app_mercato") + f"?session_id={fa.id}")
        self.assertContains(page, "Limite Cambi Settimanali")
        self.assertNotContains(page, "Spareggio Pari Merito")
        self.assertNotContains(page, "Regola Budget")
