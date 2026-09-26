"""Asta a chiamata a turno (regolamento 5.02)."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import AuctionCycleResult, LeagueRanking, League, Participant, Player
from ..services import salary
from ..services.state import serialize_state
from ..services.turns import current_turn, default_order
from .common import make_live_auction


class TurnTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("adm", password="pw")
        self.league = League.objects.create(name="L", owner=self.owner, season_number=2,
                                            slots_p=1, slots_d=0, slots_c=0, slots_a=0)
        self.teams = [Participant.objects.create(display_name=n, league=self.league) for n in ("Alfa", "Beta", "Gamma")]
        # Classifica di metà stagione: Gamma prima, poi Alfa, poi Beta.
        salary.save_ranking(self.league, 2, LeagueRanking.Kind.MIDSEASON,
                            [self.teams[2].id, self.teams[0].id, self.teams[1].id])
        self.auction = make_live_auction(league=self.league, starting_price=Decimal("1"),
                                         current_price=Decimal("1"), min_increment=Decimal("1"),
                                         quick_increments="1")

    def _done_lot(self, cycle):
        p = Player.objects.create(name=f"P{cycle}", role="P", league=self.league)
        AuctionCycleResult.objects.create(auction=self.auction, cycle=cycle, player=p)

    def test_order_follows_ranking_and_rotates(self):
        self.auction.turn_order = default_order(self.league)
        self.auction.save()
        self.assertEqual(current_turn(self.auction)["name"], "Gamma")
        self._done_lot(1)
        self.assertEqual(current_turn(self.auction)["name"], "Alfa")
        self.auction.turn_skips = 1
        self.auction.save()
        self.assertEqual(current_turn(self.auction)["name"], "Beta")

    def test_full_rosters_pass_automatically(self):
        self.auction.turn_order = default_order(self.league)
        self.auction.save()
        Player.objects.create(name="GK", role="P", league=self.league, owner=self.teams[2])  # Gamma completa
        self.assertEqual(current_turn(self.auction)["name"], "Alfa")

    def test_disabled_by_default_and_in_state(self):
        self.assertIsNone(serialize_state(self.auction)["turn"])

    def test_regia_endpoint(self):
        self.client.force_login(self.owner)
        url = reverse("admin_auction_turns", args=[self.auction.id])
        d = self.client.post(url, {"action": "enable"}).json()
        self.assertEqual(d["state"]["turn"]["name"], "Gamma")
        d = self.client.post(url, {"action": "skip"}).json()
        self.assertEqual(d["state"]["turn"]["name"], "Alfa")
        d = self.client.post(url, {"action": "disable"}).json()
        self.assertIsNone(d["state"]["turn"])
        self.client.force_login(User.objects.create_user("x", password="pw"))
        self.assertEqual(self.client.post(url, {"action": "enable"}).status_code, 403)
