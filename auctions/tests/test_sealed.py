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

@override_settings(BID_MIN_INTERVAL_MS=0)
class SealedBidTests(TestCase):
    """Regolamento §3.1 E — asta alle buste oltre la soglia del ruolo."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega", budget=Decimal("1000"),
            slots_p=3, slots_d=8, slots_c=8, slots_a=6,
        )
        self.striker = Player.objects.create(
            name="Bomber", role="A", league=self.league, initial_price=Decimal("1"))
        self.a = self._team("Alfa")
        self.b = self._team("Beta")
        self.c = self._team("Gamma")

    def _team(self, name):
        return Participant.objects.create(
            display_name=name, league=self.league, credits=Decimal("1000"))

    def _auction(self, **kwargs):
        opts = dict(
            league=self.league, player=self.striker,
            starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5,10,50",
            sealed_bids=True, sealed_seconds=30,
            enforce_limits=False,
        )
        opts.update(kwargs)
        return make_live_auction(**opts)

    def test_below_threshold_stays_open_outcry(self):
        auction = self._auction()
        services.place_bid(auction.id, self.a.id, 50)   # 51, soglia A = 150
        auction.refresh_from_db()
        self.assertFalse(auction.sealed_open)

    def test_threshold_opens_the_sealed_round(self):
        auction = self._auction(current_price=Decimal("140"))
        r = services.place_bid(auction.id, self.a.id, 10)   # 150 = soglia
        self.assertTrue(r.accepted)
        auction.refresh_from_db()
        self.assertTrue(auction.sealed_open)
        self.assertEqual(auction.sealed_round, 1)
        # "non inferiore all'ultimo dichiarato +1"
        self.assertEqual(auction.sealed_floor, Decimal("151"))
        # Il timer delle grida si ferma: il lotto non deve scadere da solo.
        self.assertIsNone(auction.ends_at)

    def test_threshold_off_for_a_role_never_opens(self):
        auction = self._auction(current_price=Decimal("140"), sealed_threshold_a=0)
        services.place_bid(auction.id, self.a.id, 100)
        auction.refresh_from_db()
        self.assertFalse(auction.sealed_open)

    def test_option_off_keeps_the_old_behaviour(self):
        auction = self._auction(current_price=Decimal("140"), sealed_bids=False)
        services.place_bid(auction.id, self.a.id, 100)
        auction.refresh_from_db()
        self.assertFalse(auction.sealed_open)
        self.assertEqual(auction.current_price, Decimal("240"))

    def _open_sealed(self, auction):
        services.place_bid(auction.id, self.a.id, 10)   # porta a 150
        auction.refresh_from_db()
        return auction

    def test_no_shouting_once_sealed(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        r = services.place_bid(auction.id, self.b.id, 10)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.SEALED_ACTIVE)

    def test_envelope_below_floor_is_refused(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        r = services.place_sealed_bid(auction.id, self.b.id, "150")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.SEALED_TOO_LOW)

    def test_envelope_can_be_rewritten_until_the_deadline(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        services.place_sealed_bid(auction.id, self.b.id, "160")
        services.place_sealed_bid(auction.id, self.b.id, "200")
        envelopes = SealedBid.objects.filter(auction=auction, participant=self.b)
        self.assertEqual(envelopes.count(), 1)
        self.assertEqual(envelopes.first().amount, Decimal("200"))

    def test_envelope_over_credits_is_refused(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        poor = self._team("Povera")
        poor.credits = Decimal("100")
        poor.save()
        r = services.place_sealed_bid(auction.id, poor.id, "160")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.INSUFFICIENT_CREDITS)

    def test_highest_envelope_wins_and_the_lot_closes(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        services.place_sealed_bid(auction.id, self.a.id, "160")
        services.place_sealed_bid(auction.id, self.b.id, "220")
        resolved = services.resolve_sealed(auction.id, force=True)
        self.assertEqual(resolved.sealed_event, "won")
        self.assertFalse(resolved.sealed_open)
        self.assertEqual(resolved.current_price, Decimal("220"))
        self.assertEqual(resolved.best_bid.participant_id, self.b.id)
        # Il lotto e' scaduto: la chiusura di sempre lo aggiudica.
        self.assertIsNotNone(resolved.ends_at)
        services.close_if_expired(auction.id)
        services.finalize_expired(auction.id)
        self.striker.refresh_from_db()
        self.assertEqual(self.striker.owner_id, self.b.id)
        self.assertEqual(self.striker.cost, Decimal("220"))

    def test_tie_reopens_between_the_tied_teams_only(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        services.place_sealed_bid(auction.id, self.a.id, "200")
        services.place_sealed_bid(auction.id, self.b.id, "200")
        services.place_sealed_bid(auction.id, self.c.id, "180")
        resolved = services.resolve_sealed(auction.id, force=True)
        self.assertEqual(resolved.sealed_event, "tie")
        self.assertEqual(resolved.sealed_round, 2)
        self.assertEqual(resolved.sealed_floor, Decimal("201"))
        self.assertCountEqual(resolved.sealed_contenders, [self.a.id, self.b.id])
        # Chi non ha pareggiato resta fuori dallo spareggio.
        r = services.place_sealed_bid(auction.id, self.c.id, "300")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.SEALED_NOT_CONTENDER)
        # A oltranza: lo spareggio si risolve come il primo giro.
        services.place_sealed_bid(auction.id, self.a.id, "210")
        services.place_sealed_bid(auction.id, self.b.id, "205")
        final = services.resolve_sealed(auction.id, force=True)
        self.assertEqual(final.sealed_event, "won")
        self.assertEqual(final.best_bid.participant_id, self.a.id)
        self.assertEqual(final.current_price, Decimal("210"))

    def test_no_envelope_leaves_the_lot_to_the_last_shouted_bid(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        resolved = services.resolve_sealed(auction.id, force=True)
        self.assertEqual(resolved.sealed_event, "empty")
        self.assertFalse(resolved.sealed_open)
        self.assertEqual(resolved.current_price, Decimal("150"))
        self.assertEqual(resolved.best_bid.participant_id, self.a.id)

    def test_round_does_not_resolve_before_its_time(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        services.place_sealed_bid(auction.id, self.b.id, "300")
        self.assertIsNone(services.resolve_sealed(auction.id))
        self.assertIsNone(services.sealed_tick(auction.id))
        Auction.objects.filter(pk=auction.id).update(
            sealed_ends_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNotNone(services.sealed_tick(auction.id))

    def test_status_never_leaks_other_envelopes(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        services.place_sealed_bid(auction.id, self.a.id, "160")
        services.place_sealed_bid(auction.id, self.b.id, "500")
        auction.refresh_from_db()
        mine = services.sealed_status(auction, self.a)
        self.assertEqual(mine["submitted"], 2)
        self.assertEqual(mine["your_amount"], "160")
        self.assertEqual(mine["reveal"], [])
        self.assertNotIn("500", json.dumps(mine))
        # Allo spoglio le buste diventano pubbliche.
        services.resolve_sealed(auction.id, force=True)
        auction.refresh_from_db()
        self.assertEqual(
            [r["amount"] for r in services.sealed_status(auction)["reveal"]],
            ["500", "160"],
        )

    def test_new_lot_clears_the_sealed_state(self):
        auction = self._open_sealed(self._auction(current_price=Decimal("140")))
        other = Player.objects.create(
            name="Altro", role="A", league=self.league, initial_price=Decimal("1"))
        services.call_player(auction.id, other.id)
        auction.refresh_from_db()
        self.assertFalse(auction.sealed_open)
        self.assertEqual(auction.sealed_reveal, [])

    def test_admin_can_open_the_scrutinio_early(self):
        auction = self._auction()
        opened = services.open_sealed_now(auction.id)
        self.assertIsNone(opened.sealed_error)
        self.assertTrue(opened.sealed_open)
        self.assertEqual(opened.sealed_floor, Decimal("2"))   # prezzo 1 + 1
        # Due volte no: lo scrutinio e' gia' aperto.
        again = services.open_sealed_now(auction.id)
        self.assertEqual(again.sealed_error, services.Reject.SEALED_ACTIVE)


@override_settings(BID_MIN_INTERVAL_MS=0)
@override_settings(BID_MIN_INTERVAL_MS=0)
class SealedBidRulesTests(TestCase):
    """Alle buste valgono le regole dei rilanci, se la lega lo sceglie
    (``sealed_enforce_rules``): tetto salariale, portieri, riacquisto 4.02."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega", budget=Decimal("1000"), gk_max_clubs=2,
            slots_p=3, slots_d=8, slots_c=8, slots_a=6,
        )
        self.keeper = Player.objects.create(
            name="Numero Uno", role="P", team="Napoli", league=self.league,
            initial_price=Decimal("1"))
        self.a = Participant.objects.create(
            display_name="Alfa", league=self.league, credits=Decimal("1000"))
        self.b = Participant.objects.create(
            display_name="Beta", league=self.league, credits=Decimal("1000"))

    def _sealed_auction(self, **kwargs):
        opts = dict(
            league=self.league, player=self.keeper,
            starting_price=Decimal("1"), current_price=Decimal("60"),
            min_increment=Decimal("1"), quick_increments="1,5,10",
            sealed_bids=True, enforce_limits=False,
        )
        opts.update(kwargs)
        auction = make_live_auction(**opts)
        services.open_sealed_now(auction.id)
        auction.refresh_from_db()
        return auction

    def _two_club_keepers(self, team):
        for club in ("Inter", "Milan"):
            Player.objects.create(name=f"P {club}", role="P", team=club,
                                  league=self.league, owner=team)

    def test_gk_clubs_rule_applies_to_envelopes(self):
        self._two_club_keepers(self.b)
        auction = self._sealed_auction()
        r = services.place_sealed_bid(auction.id, self.b.id, "70")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.GK_CLUBS)

    def test_rules_off_only_credits_and_slots_count(self):
        self._two_club_keepers(self.b)
        auction = self._sealed_auction(sealed_enforce_rules=False)
        r = services.place_sealed_bid(auction.id, self.b.id, "70")
        self.assertTrue(r.accepted)

    def test_salary_cap_applies_to_envelopes(self):
        auction = self._sealed_auction()
        with mock.patch("auctions.services.salary.check_purchase",
                        return_value="Tetto salariale"):
            r = services.place_sealed_bid(auction.id, self.a.id, "70")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.SALARY_CAP)

    def test_salary_cap_ignored_when_rules_off(self):
        auction = self._sealed_auction(sealed_enforce_rules=False)
        with mock.patch("auctions.services.salary.check_purchase",
                        return_value="Tetto salariale"):
            r = services.place_sealed_bid(auction.id, self.a.id, "70")
        self.assertTrue(r.accepted)

    def test_lost_at_renewal_cannot_be_rebought_by_envelope(self):
        self.keeper.rescinded_from = self.a
        self.keeper.save()
        auction = self._sealed_auction()
        r = services.place_sealed_bid(auction.id, self.a.id, "70")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, services.Reject.RESCINDED_REBUY)
        # Gli altri scrivono la loro busta come sempre.
        self.assertTrue(services.place_sealed_bid(auction.id, self.b.id, "70").accepted)


class SealedBidViewTests(TestCase):
    """I comandi di regia e le pagine, con l'asta alle buste accesa."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.client.force_login(self.user)
        self.league = League.objects.create(name="L", budget=Decimal("1000"))
        self.p = Participant.objects.create(display_name="Eve", league=self.league,
                                            credits=Decimal("1000"))
        self.player = Player.objects.create(name="Kvara", role="A", league=self.league,
                                            initial_price=Decimal("10"))
        self.auction = make_live_auction(
            league=self.league, player=self.player, current_price=Decimal("10"),
            min_increment=Decimal("1"), quick_increments="1,5,10",
            sealed_bids=True, enforce_limits=False,
        )

    def test_open_and_resolve_from_the_console(self):
        r = self.client.post(f"/admin-auction/{self.auction.id}/open-sealed/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["state"]["sealed"]["active"])

        services.place_sealed_bid(self.auction.id, self.p.id, "60")
        r = self.client.post(f"/admin-auction/{self.auction.id}/resolve-sealed/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["event"], "won")
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.current_price, Decimal("60"))

    def test_sealed_settings_survive_a_saved_session(self):
        """Riprendere una sessione salvata non deve spegnere le buste."""
        self.auction.sealed_threshold_c = 80
        self.auction.sealed_seconds = 90
        self.auction.save()
        session = services.save_session(self.auction.id, name="Ripresa")
        resumed = services.resume_session(session.id)
        self.assertTrue(resumed.sealed_bids)
        self.assertEqual(resumed.sealed_threshold_c, 80)
        self.assertEqual(resumed.sealed_seconds, 90)

    def test_resolve_without_an_open_round_is_refused(self):
        r = self.client.post(f"/admin-auction/{self.auction.id}/resolve-sealed/")
        self.assertEqual(r.status_code, 409)

    def test_settings_form_saves_thresholds(self):
        r = self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "sealed_bids": "1", "sealed_threshold_p": "30", "sealed_threshold_d": "0",
            "sealed_threshold_c": "80", "sealed_threshold_a": "200",
            "sealed_seconds": "90",
        })
        self.assertEqual(r.status_code, 200)
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.sealed_bids)
        self.assertEqual(self.auction.sealed_threshold_p, 30)
        self.assertEqual(self.auction.sealed_threshold_d, 0)
        self.assertEqual(self.auction.sealed_threshold_a, 200)
        self.assertEqual(self.auction.sealed_seconds, 90)

    def test_settings_form_saves_the_rules_choice(self):
        self.assertTrue(self.auction.sealed_enforce_rules)
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "sealed_bids": "1", "sealed_enforce_rules": "0",
        })
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.sealed_enforce_rules)
        # Spunta + gemello nascosto: il form posta "0" e poi "1".
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "sealed_bids": "1", "sealed_enforce_rules": ["0", "1"],
        })
        self.auction.refresh_from_db()
        self.assertTrue(self.auction.sealed_enforce_rules)

    def test_rules_choice_survives_a_saved_session(self):
        self.auction.sealed_enforce_rules = False
        self.auction.save()
        session = services.save_session(self.auction.id, name="Ripresa")
        self.assertFalse(services.resume_session(session.id).sealed_enforce_rules)

    def test_forms_offer_the_rules_choice(self):
        r = self.client.get(f"/admin-auction/?auction={self.auction.id}")
        self.assertContains(r, 'name="sealed_enforce_rules"')
        self.assertContains(r, "Alle buste valgono le regole dei rilanci")
        for url in ("/admin-auction/wizard/", "/admin-auction/setup/"):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertContains(r, 'name="sealed_enforce_rules"')
                self.assertContains(r, "Alle buste valgono le regole dei rilanci")

    def test_settings_form_can_switch_the_option_off(self):
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {})
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.sealed_bids)

    def test_junk_threshold_keeps_the_stored_value(self):
        self.client.post(f"/admin-auction/{self.auction.id}/edit/", {
            "sealed_bids": "1", "sealed_threshold_a": "boh",
        })
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.sealed_threshold_a, 150)

    def test_pages_render_during_a_sealed_round(self):
        services.open_sealed_now(self.auction.id)
        session = self.client.session
        session["participant_id"] = self.p.id
        session.save()
        for url in (f"/bid/{self.auction.id}/", f"/screen/{self.auction.id}/",
                    f"/admin-auction/?auction={self.auction.id}"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

