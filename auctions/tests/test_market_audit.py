"""Regole dei mercati emerse dal secondo controllo: tagli che lo svincolo non
ammette, clausole sui prestiti, rimborsi «Lugnano», portieri allo spoglio,
finestra rinnovi e azioni dell'admin sulla sessione."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import Auction, League, MarketBid, MarketSession, Participant, Player
from ..services.market import (
    _calc_release_refund,
    acquire_free_agent,
    execute_buyout,
    place_market_bid,
    place_waiver_claim,
    resolve_market_session,
    sync_market_schedule,
)


class MarketCutsAndClausesTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega Controllo", budget=Decimal("500"))
        self.a = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"),
                                            spent_credits=Decimal("100"))
        self.b = Participant.objects.create(league=self.league, display_name="Beta", credits=Decimal("500"))
        self.lender = Participant.objects.create(league=self.league, display_name="Gamma", credits=Decimal("500"))
        self.free = Player.objects.create(league=self.league, name="Libero", role="D", team="Pisa",
                                          initial_price=Decimal("5"))

    def _session(self, kind, **kw):
        return MarketSession.objects.create(league=self.league, title=kind, session_type=kind,
                                            status=MarketSession.Status.OPEN, allow_conditional_release=True, **kw)

    def test_a_player_on_loan_cannot_be_cut(self):
        loaned = Player.objects.create(league=self.league, owner=self.a, loan_from=self.lender, name="Prestito",
                                       role="D", team="Lecce", cost=Decimal("50"))
        fa = self._session("free_agency")
        res = acquire_free_agent(fa.id, self.a.id, self.free.id, release_player_id=loaned.id)
        self.assertEqual(res["error"], "release_locked")
        bids = self._session("sealed_bids")
        res = place_market_bid(bids.id, self.a.id, self.free.id, 10, release_player_id=loaned.id)
        self.assertEqual(res["error"], "release_locked")
        ww = self._session("waiver_wire")
        res = place_waiver_claim(ww.id, self.a.id, self.free.id, release_player_id=loaned.id)
        self.assertEqual(res["error"], "release_locked")

    def test_a_player_on_the_abroad_list_cannot_be_cut(self):
        abroad = Player.objects.create(league=self.league, owner=self.a, name="Ceduto", role="D", team="Estero",
                                       cost=Decimal("40"), abroad_list=True)
        fa = self._session("free_agency")
        res = acquire_free_agent(fa.id, self.a.id, self.free.id, release_player_id=abroad.id)
        self.assertEqual(res["error"], "release_locked")
        abroad.refresh_from_db()
        self.assertEqual(abroad.owner_id, self.a.id)

    def test_a_cut_already_filed_is_dropped_at_the_count(self):
        cut = Player.objects.create(league=self.league, owner=self.a, name="Taglio", role="D", team="Lecce",
                                    cost=Decimal("30"))
        bids = self._session("sealed_bids")
        self.assertTrue(place_market_bid(bids.id, self.a.id, self.free.id, 10, release_player_id=cut.id)["ok"])
        # Dopo la busta il giocatore passa in prestito: allo spoglio il taglio non vale più.
        Player.objects.filter(pk=cut.pk).update(loan_from=self.lender)
        resolve_market_session(bids.id)
        cut.refresh_from_db()
        self.assertEqual(cut.owner_id, self.a.id)
        self.a.refresh_from_db()
        self.assertEqual(self.a.spent_credits, Decimal("110"))

    def test_no_clause_on_a_player_on_loan(self):
        loaned = Player.objects.create(league=self.league, owner=self.a, loan_from=self.lender, name="Prestito",
                                       role="C", team="Lecce", cost=Decimal("50"))
        bo = self._session("buyout_clause", config={"buyout_min_hold_days": 0})
        res = execute_buyout(bo.id, self.b.id, loaned.id)
        self.assertEqual(res["error"], "on_loan")
        loaned.refresh_from_db()
        self.assertEqual(loaned.owner_id, self.a.id)

    def test_lugnano_refunds_follow_the_release_rule(self):
        player = Player(league=self.league, name="X", role="C", team="Lecce", cost=Decimal("80"),
                        initial_price=Decimal("20"))
        sept = MarketSession(league=self.league, release_refund_mode=Auction.RefundMode.LUGNANO_SEPT)
        jan = MarketSession(league=self.league, release_refund_mode=Auction.RefundMode.LUGNANO_JAN)
        self.assertEqual(_calc_release_refund(sept, player), Decimal("20"))
        self.assertEqual(_calc_release_refund(jan, player), Decimal("20"))
        player.team = "Estero"
        self.assertEqual(_calc_release_refund(sept, player), Decimal("80"))
        self.assertEqual(_calc_release_refund(jan, player), Decimal("40"))

    def test_a_refund_never_brings_spending_below_zero(self):
        Participant.objects.filter(pk=self.a.pk).update(spent_credits=Decimal("0"))
        cut = Player.objects.create(league=self.league, owner=self.a, name="Caro", role="D", team="Lecce",
                                    cost=Decimal("1"), initial_price=Decimal("60"))
        fa = self._session("free_agency", release_refund_mode=Auction.RefundMode.CURRENT)
        self.assertTrue(acquire_free_agent(fa.id, self.a.id, self.free.id, release_player_id=cut.id)["ok"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.spent_credits, Decimal("0"))

    def test_goalkeeper_clubs_hold_at_the_count(self):
        League.objects.filter(pk=self.league.pk).update(gk_max_clubs=2)
        Player.objects.create(league=self.league, owner=self.a, name="Portiere Inter", role="P", team="Inter",
                              cost=Decimal("5"))
        milan = Player.objects.create(league=self.league, name="Portiere Milan", role="P", team="Milan",
                                      initial_price=Decimal("1"))
        roma = Player.objects.create(league=self.league, name="Portiere Roma", role="P", team="Roma",
                                     initial_price=Decimal("1"))
        bids = self._session("sealed_bids")
        self.assertTrue(place_market_bid(bids.id, self.a.id, milan.id, 5, priority=1)["ok"])
        self.assertTrue(place_market_bid(bids.id, self.a.id, roma.id, 5, priority=2)["ok"])
        summary = resolve_market_session(bids.id)
        self.assertEqual([w["player_name"] for w in summary["won"]], ["Portiere Milan"])
        self.assertIn("2 squadre", MarketBid.objects.get(player=roma).note)


class SessionAdminActionsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner_audit", "o@x.local", "pw")
        self.league = League.objects.create(name="Lega Admin", budget=Decimal("500"), owner=self.owner,
                                            contracts_enabled=True)
        self.client.force_login(self.owner)

    def _session(self, kind, status=MarketSession.Status.OPEN, **kw):
        return MarketSession.objects.create(league=self.league, title=kind, session_type=kind, status=status, **kw)

    def test_renewals_window_closes_at_the_deadline(self):
        self.client.post(reverse("admin_market_create"), {
            "league_id": self.league.id, "session_type": "renewals", "open_timing": "now",
            "closes_at": (timezone.localtime() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        self.league.refresh_from_db()
        self.assertTrue(self.league.renewals_open)
        MarketSession.objects.filter(league=self.league).update(closes_at=timezone.now() - timedelta(minutes=1))
        sync_market_schedule(self.league)
        self.league.refresh_from_db()
        self.assertFalse(self.league.renewals_open)

    def test_a_resolved_session_is_not_reopened(self):
        s = self._session("sealed_bids", status=MarketSession.Status.RESOLVED)
        self.client.post(reverse("admin_market_status", args=[s.id]), {"status": "open"})
        s.refresh_from_db()
        self.assertEqual(s.status, MarketSession.Status.RESOLVED)

    def test_reopening_after_the_deadline_asks_to_move_it(self):
        s = self._session("sealed_bids", status=MarketSession.Status.CLOSED,
                          closes_at=timezone.now() - timedelta(hours=1))
        res = self.client.post(reverse("admin_market_status", args=[s.id]), {"status": "open"}, follow=True)
        s.refresh_from_db()
        self.assertEqual(s.status, MarketSession.Status.CLOSED)
        self.assertContains(res, "sposta prima la chiusura")

    def test_deleting_an_old_renewals_session_keeps_the_new_season_window(self):
        old = self._session("renewals", status=MarketSession.Status.RESOLVED)
        League.objects.filter(pk=self.league.pk).update(renewals_open=True)  # «Nuova stagione»
        self.client.post(reverse("admin_market_delete", args=[old.id]))
        self.league.refresh_from_db()
        self.assertTrue(self.league.renewals_open)

    def test_rules_numbers_are_parsed_safely(self):
        s = self._session("buyout_clause", config={"buyout_multiplier": 1.5})
        res = self.client.post(reverse("admin_market_rules", args=[s.id]), {"buyout_multiplier": "1,2"})
        self.assertEqual(res.status_code, 302)
        s.refresh_from_db()
        self.assertEqual(s.config["buyout_multiplier"], 1.2)
        self.client.post(reverse("admin_market_rules", args=[s.id]), {"buyout_multiplier": "-2"})
        s.refresh_from_db()
        self.assertEqual(s.config["buyout_multiplier"], 1.0)
        fa = self._session("free_agency", config={"fa_max_moves": 5})
        self.client.post(reverse("admin_market_rules", args=[fa.id]), {"fa_max_moves": ""})
        fa.refresh_from_db()
        self.assertEqual(fa.config["fa_max_moves"], 0)

    def test_rules_refuse_a_deadline_before_the_opening(self):
        opens = timezone.now() + timedelta(days=2)
        s = self._session("sealed_bids", status=MarketSession.Status.DRAFT, opens_at=opens)
        self.client.post(reverse("admin_market_rules", args=[s.id]), {
            "closes_at": timezone.localtime(opens - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        s.refresh_from_db()
        self.assertIsNone(s.closes_at)

    def test_bids_are_counted_only_after_closing(self):
        s = self._session("sealed_bids")
        res = self.client.post(reverse("admin_market_resolve", args=[s.id]), follow=True)
        s.refresh_from_db()
        self.assertEqual(s.status, MarketSession.Status.OPEN)
        self.assertContains(res, "chiudila prima")
        page = self.client.get(reverse("admin_market_session", args=[s.id]))
        self.assertNotContains(page, reverse("admin_market_resolve", args=[s.id]))
        s.status = MarketSession.Status.CLOSED
        s.save()
        self.client.post(reverse("admin_market_resolve", args=[s.id]))
        s.refresh_from_db()
        self.assertEqual(s.status, MarketSession.Status.RESOLVED)

    def test_declared_renewals_without_dice_are_flagged(self):
        team = Participant.objects.create(league=self.league, display_name="Delta", credits=Decimal("500"))
        Player.objects.create(league=self.league, owner=team, name="Senza Dado", role="C", team="Lecce",
                              cost=Decimal("10"), contract_years=0, renewal_declared=True)
        s = self._session("renewals")
        res = self.client.post(reverse("admin_market_status", args=[s.id]), {"status": "closed"}, follow=True)
        self.assertContains(res, "Senza Dado (Delta)")
        page = self.client.get(reverse("app_regia_market_session", args=[s.id]))
        self.assertContains(page, "Dado rinnovo non tirato")
