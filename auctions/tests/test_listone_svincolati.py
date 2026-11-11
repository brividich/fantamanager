"""Chi non è più nel listone non è uno svincolato: non si vede e non si compra."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Auction, AuctionQueueItem, League, MarketBid, MarketSession, Participant, Player
from ..providers.importers import sync_players
from ..services import abroad
from ..services.lifecycle import call_player, release_player
from ..services.market import place_market_bid, resolve_market_session
from ..services.queue import build_queue


def row(name, role="A", team="JUV", price=10):
    return {"name": name, "role": role, "team": team, "price": price}


class OutOfListoneTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="L", own_listone=True)
        self.team = Participant.objects.create(display_name="Alfa", league=self.league, credits=Decimal("500"))
        self.free = Player.objects.create(name="Kean", role="A", team="FIO", league=self.league)
        self.owned = Player.objects.create(name="Vlahovic", role="A", team="JUV", league=self.league,
                                           owner=self.team, cost=Decimal("80"))

    def free_names(self):
        return set(Player.objects.free_agents().filter(league=self.league).values_list("name", flat=True))

    def test_listone_without_prune_hides_who_is_gone(self):
        sync_players([row("Lautaro", team="INT")], league=self.league, prune=False)
        self.free.refresh_from_db()
        self.assertFalse(self.free.in_listone)
        self.assertEqual(self.free_names(), {"Lautaro"})

    def test_owned_player_missing_from_listone_is_not_free_after_release(self):
        sync_players([row("Kean", team="FIO")], league=self.league)
        release_player(self.owned.id, by_admin=True)
        self.owned.refresh_from_db()
        self.assertIsNone(self.owned.owner_id)
        self.assertEqual(self.free_names(), {"Kean"})

    def test_back_in_the_listone_is_free_again(self):
        sync_players([row("Lautaro", team="INT")], league=self.league)
        sync_players([row("Kean", team="FIO"), row("Lautaro", team="INT")], league=self.league)
        self.assertEqual(self.free_names(), {"Kean", "Lautaro"})

    def test_lost_abroad_is_not_free(self):
        abroad.resolve(self.owned.id, "free")
        self.owned.refresh_from_db()
        self.assertIsNone(self.owned.owner_id)
        self.assertFalse(self.owned.in_listone)
        self.assertEqual(self.free_names(), {"Kean"})

    def test_dismissed_flag_keeps_him_in_the_listone(self):
        self.owned.in_listone = False
        self.owned.save(update_fields=["in_listone"])
        abroad.flag_player(self.owned.id)
        abroad.resolve(self.owned.id, "dismiss")
        self.owned.refresh_from_db()
        self.assertTrue(self.owned.in_listone)

    def test_cannot_be_bought_or_called(self):
        abroad.resolve(self.owned.id, "free")
        session = MarketSession.objects.create(league=self.league, title="M", status=MarketSession.Status.OPEN)
        res = place_market_bid(session.id, self.team.id, self.owned.id, 5)
        self.assertEqual(res["error"], "player_unavailable")
        auction = Auction.objects.create(league=self.league, title="Asta")
        self.assertIsNone(call_player(auction.id, self.owned.id))
        self.assertIsNotNone(call_player(auction.id, self.free.id))

    def test_pages_list_only_free_agents_in_the_listone(self):
        abroad.resolve(self.owned.id, "free")
        s = self.client.session
        s["participant_id"] = self.team.id
        s.save()
        resp = self.client.get(reverse("app_mercato"))
        self.assertEqual([p.name for p in resp.context["free_agents"]], ["Kean"])

        self.client.force_login(User.objects.create_superuser("root", "root@x.local", "pw"))
        for name in ("admin_footballers", "app_footballers"):
            url = reverse(name) + f"?league={self.league.id}&vista=listone"
            resp = self.client.get(url + "&status=free")
            self.assertEqual([p.name for p in resp.context["lst_rows"]], ["Kean"], name)
            self.assertEqual((resp.context["lst_free"], resp.context["lst_out"]), (1, 1), name)
            resp = self.client.get(url + "&status=out")
            self.assertEqual([p.name for p in resp.context["lst_rows"]], ["Vlahovic"], name)
            self.assertContains(resp, "fuori listone")

    def test_bid_placed_before_leaving_the_listone_loses(self):
        session = MarketSession.objects.create(league=self.league, title="M", status=MarketSession.Status.OPEN)
        res = place_market_bid(session.id, self.team.id, self.free.id, 5)
        self.assertTrue(res["ok"], res)
        sync_players([row("Lautaro", team="INT")], league=self.league)   # Kean esce dal listone
        session.status = MarketSession.Status.CLOSED
        session.save(update_fields=["status"])
        resolve_market_session(session.id)
        self.free.refresh_from_db()
        self.assertIsNone(self.free.owner_id)
        self.assertEqual(MarketBid.objects.get(pk=res["bid_id"]).status, MarketBid.Status.LOST)

    def test_queue_drops_who_leaves_the_listone(self):
        auction = Auction.objects.create(league=self.league, title="Asta")
        build_queue(auction)
        self.assertTrue(AuctionQueueItem.objects.filter(auction=auction, player=self.free).exists())
        sync_players([row("Lautaro", team="INT")], league=self.league)
        self.assertFalse(AuctionQueueItem.objects.filter(auction=auction, player=self.free, done=False).exists())
        # Svincolato dalla rosa mentre è fuori listone: non torna in coda.
        release_player(self.owned.id, auction_id=auction.id, by_admin=True)
        self.assertFalse(AuctionQueueItem.objects.filter(auction=auction, player=self.owned, done=False).exists())
