"""Tests for trades (scambi) between teams and the Rosa release button."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import League, Participant, Player, RosterLog, Trade
from ..services.trade import cancel_trade, decide_trade, propose_trade, respond_trade


class TradeServiceTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(
            name="Lega Scambi", slots_p=3, slots_d=8, slots_c=8, slots_a=6,
            trades_need_approval=False,
        )
        self.a = Participant.objects.create(display_name="Alfa", league=self.league, credits=Decimal("100"))
        self.b = Participant.objects.create(display_name="Beta", league=self.league, credits=Decimal("100"))
        self.pa = Player.objects.create(name="Dybala", role="A", league=self.league, owner=self.a, cost=Decimal("20"))
        self.pb = Player.objects.create(name="Kvara", role="A", league=self.league, owner=self.b, cost=Decimal("30"))

    def test_swap_with_credits_executes_on_accept(self):
        res = propose_trade(self.a.id, self.b.id, [self.pa.id], [self.pb.id], give_credits=15)
        self.assertTrue(res["ok"], res)
        res = respond_trade(res["trade_id"], self.b.id, accept=True)
        self.assertTrue(res["ok"], res)
        self.pa.refresh_from_db()
        self.pb.refresh_from_db()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.pa.owner, self.b)
        self.assertEqual(self.pb.owner, self.a)
        self.assertEqual(self.pa.cost, Decimal("20"))  # purchase cost travels with the player
        self.assertEqual(self.a.credits, Decimal("85"))
        self.assertEqual(self.b.credits, Decimal("115"))
        self.assertEqual(RosterLog.objects.filter(action=RosterLog.Action.TRADE).count(), 4)

    def test_approval_flow(self):
        self.league.trades_need_approval = True
        self.league.save()
        tid = propose_trade(self.a.id, self.b.id, [self.pa.id], [self.pb.id])["trade_id"]
        self.assertEqual(respond_trade(tid, self.b.id, True)["status"], Trade.Status.ACCEPTED)
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.owner, self.a)  # nothing moves before ratification
        self.assertTrue(decide_trade(tid, approve=True)["ok"])
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.owner, self.b)

    def test_veto(self):
        self.league.trades_need_approval = True
        self.league.save()
        tid = propose_trade(self.a.id, self.b.id, [self.pa.id], [self.pb.id])["trade_id"]
        respond_trade(tid, self.b.id, True)
        decide_trade(tid, approve=False, note="Squilibrato")
        t = Trade.objects.get(pk=tid)
        self.assertEqual(t.status, Trade.Status.VETOED)
        self.assertEqual(t.status_note, "Squilibrato")
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.owner, self.a)

    def test_validations_on_propose(self):
        cases = [
            ((self.a.id, self.a.id, [self.pa.id], []), "te stesso"),
            ((self.a.id, self.b.id, [], []), "almeno un calciatore"),
            ((self.a.id, self.b.id, [self.pb.id], []), "non è più nella rosa di Alfa"),
            ((self.a.id, self.b.id, [self.pa.id], [], 500), "abbastanza crediti"),
            ((self.a.id, self.b.id, [self.pa.id], [], -5), "non valido"),
        ]
        for args, expected in cases:
            res = propose_trade(*args)
            self.assertFalse(res["ok"], args)
            self.assertIn(expected, res["message"])

    def test_disabled_league(self):
        self.league.trades_enabled = False
        self.league.save()
        res = propose_trade(self.a.id, self.b.id, [self.pa.id], [])
        self.assertFalse(res["ok"])

    def test_roster_slots_checked(self):
        for i in range(6):
            Player.objects.create(name=f"A{i}", role="A", league=self.league, owner=self.b)
        res = propose_trade(self.a.id, self.b.id, [self.pa.id], [])
        self.assertFalse(res["ok"])
        self.assertIn("limite di 6 slot", res["message"])

    def test_revalidated_at_execution_and_stale_trades_fail(self):
        c = Participant.objects.create(display_name="Gamma", league=self.league, credits=Decimal("100"))
        t1 = propose_trade(self.a.id, self.b.id, [self.pa.id], [])["trade_id"]
        t2 = propose_trade(self.a.id, c.id, [self.pa.id], [])["trade_id"]
        self.assertTrue(respond_trade(t1, self.b.id, True)["ok"])
        self.assertEqual(Trade.objects.get(pk=t2).status, Trade.Status.FAILED)
        res = respond_trade(t2, c.id, True)
        self.assertFalse(res["ok"])

    def test_only_receiver_responds_and_only_proposer_cancels(self):
        tid = propose_trade(self.a.id, self.b.id, [self.pa.id], [])["trade_id"]
        self.assertFalse(respond_trade(tid, self.a.id, True)["ok"])
        self.assertFalse(cancel_trade(tid, self.b.id)["ok"])
        self.assertTrue(cancel_trade(tid, self.a.id)["ok"])
        self.assertFalse(respond_trade(tid, self.b.id, True)["ok"])

    def test_duplicate_proposal_refused(self):
        propose_trade(self.a.id, self.b.id, [self.pa.id], [self.pb.id])
        res = propose_trade(self.a.id, self.b.id, [self.pa.id], [self.pb.id])
        self.assertFalse(res["ok"])


class TradeViewsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw")
        self.league = League.objects.create(name="Lega V", owner=self.owner)
        self.a = Participant.objects.create(display_name="Alfa", league=self.league, credits=Decimal("100"))
        self.b = Participant.objects.create(display_name="Beta", league=self.league, credits=Decimal("100"))
        self.pa = Player.objects.create(name="Dybala", role="A", league=self.league, owner=self.a)
        self.pb = Player.objects.create(name="Kvara", role="A", league=self.league, owner=self.b)

    def _as(self, participant):
        s = self.client.session
        s["participant_id"] = participant.id
        s.save()

    def test_full_flow_through_the_app_and_admin(self):
        self._as(self.a)
        page = self.client.get(reverse("app_scambi") + f"?with={self.b.id}")
        self.assertContains(page, "Kvara")
        resp = self.client.post(reverse("app_trade_propose"), {
            "receiver_id": self.b.id, "give": [self.pa.id], "get": [self.pb.id], "give_credits": "5",
        }, follow=True)
        self.assertContains(resp, "Proposta di scambio inviata")
        trade = Trade.objects.get()

        self._as(self.b)
        page = self.client.get(reverse("app_scambi"))
        self.assertContains(page, "Proposte ricevute")
        resp = self.client.post(reverse("app_trade_respond", args=[trade.id]), {"action": "accept"}, follow=True)
        self.assertContains(resp, "ratifica")

        self.client.force_login(self.owner)
        page = self.client.get(reverse("admin_market_dashboard") + f"?league={self.league.id}")
        self.assertContains(page, "Da ratificare (1)")
        self.client.post(reverse("admin_trade_decide", args=[trade.id]), {"action": "approve"})
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.owner, self.b)

    def test_foreign_admin_cannot_decide_or_change_settings(self):
        tid = propose_trade(self.a.id, self.b.id, [self.pa.id], [])["trade_id"]
        respond_trade(tid, self.b.id, True)
        self.client.force_login(User.objects.create_user("intruder", password="pw"))
        resp = self.client.post(reverse("admin_trade_decide", args=[tid]), {"action": "approve"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(reverse("admin_trade_settings"), {"league_id": self.league.id})
        self.assertEqual(resp.status_code, 403)
        self.league.refresh_from_db()
        self.assertTrue(self.league.trades_enabled)

    def test_mercato_links_to_trades_with_pending_count(self):
        propose_trade(self.a.id, self.b.id, [self.pa.id], [])
        self._as(self.b)
        resp = self.client.get(reverse("app_mercato"))
        self.assertContains(resp, "1 da valutare")


class RosaReleaseButtonTests(TestCase):
    def test_form_post_redirects_back_with_message(self):
        league = League.objects.create(name="L")
        p = Participant.objects.create(display_name="Alfa", league=league, credits=Decimal("100"),
                                       spent_credits=Decimal("10"))
        pl = Player.objects.create(name="Pinamonti", role="A", league=league, owner=p, cost=Decimal("10"))
        s = self.client.session
        s["participant_id"] = p.id
        s.save()
        resp = self.client.post(
            reverse("participant_release_player", args=[pl.id]),
            HTTP_ACCEPT="text/html,application/xhtml+xml", follow=True,
        )
        self.assertEqual(resp.redirect_chain[-1][0], reverse("app_rosa"))
        self.assertContains(resp, "Pinamonti svincolato: +10 FM")

    def test_ajax_still_gets_json(self):
        league = League.objects.create(name="L")
        p = Participant.objects.create(display_name="Alfa", league=league)
        pl = Player.objects.create(name="X", role="A", league=league, owner=p)
        s = self.client.session
        s["participant_id"] = p.id
        s.save()
        resp = self.client.post(reverse("participant_release_player", args=[pl.id]))
        self.assertTrue(resp.json()["ok"])
