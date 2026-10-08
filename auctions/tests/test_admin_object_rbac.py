"""Admin views acting on a team, a player or an auction by id must check that
the logged-in user manages that object's league.

``staff_member_required`` only proves the user is logged in: a manager who
merely owns a team, or the admin of another league, is logged in too.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from ..models import Auction, AuctionQueueItem, Bid, League, Participant, Player
from .common import make_live_auction


class AdminObjectRbacTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superadmin = User.objects.create_superuser("root", "root@x.local", "pw")
        self.owner = User.objects.create_user("admin_alfa", password="pw")
        self.foreign_admin = User.objects.create_user("admin_beta", password="pw")
        self.manager = User.objects.create_user("mario_manager", password="pw")

        self.league = League.objects.create(name="Lega Alfa", owner=self.owner)
        self.other_league = League.objects.create(name="Lega Beta", owner=self.foreign_admin)

        self.team = Participant.objects.create(
            league=self.league, display_name="Alfa Real", access_code="ALFA01",
            user=self.manager, credits=Decimal("500"),
        )
        self.rival = Participant.objects.create(
            league=self.league, display_name="Alfa United", access_code="ALFA02",
        )
        self.foreign_team = Participant.objects.create(
            league=self.other_league, display_name="Beta United", access_code="BETA01",
        )
        self.free_player = Player.objects.create(
            league=self.league, name="Free Agent", role="A", initial_price=Decimal("10"),
        )
        self.owned_player = Player.objects.create(
            league=self.league, name="Owned Player", role="C",
            initial_price=Decimal("20"), owner=self.team, cost=Decimal("30"),
        )
        self.foreign_player = Player.objects.create(
            league=self.other_league, name="Beta Striker", role="A", initial_price=Decimal("15"),
        )
        self.auction = make_live_auction(league=self.league, title="Asta Alfa")

    def _as(self, user):
        self.client.force_login(user)

    def _intruders(self):
        """A manager who only owns a team, and the admin of another league."""
        return (self.manager, self.foreign_admin)

    # --- Team credits / PIN ------------------------------------------------

    def test_adjust_team_credits_forbidden_to_intruders(self):
        url = f"/dashboard/team/{self.team.id}/credits/"
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(url, {"mode": "set", "amount": "9999"})
            self.assertEqual(resp.status_code, 403, user.username)
            self.team.refresh_from_db()
            self.assertEqual(self.team.credits, Decimal("500"))

    def test_adjust_team_credits_allowed_to_owner_and_superuser(self):
        url = f"/dashboard/team/{self.team.id}/credits/"
        self._as(self.owner)
        resp = self.client.post(url, {"mode": "add", "amount": "50"})
        self.assertRedirects(resp, f"/dashboard/{self.league.id}/#rose", fetch_redirect_response=False)
        self.team.refresh_from_db()
        self.assertEqual(self.team.credits, Decimal("550"))

        self._as(self.superadmin)
        resp = self.client.post(url, {"mode": "set", "amount": "400"},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.team.refresh_from_db()
        self.assertEqual(self.team.credits, Decimal("400"))

    def test_reset_team_pin_forbidden_to_intruders(self):
        url = f"/dashboard/team/{self.team.id}/pin/"
        token = self.team.public_token
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(url, {"pin": "0000", "regenerate_token": "1"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(resp.status_code, 403, user.username)
            self.team.refresh_from_db()
            self.assertEqual(self.team.access_code, "ALFA01")
            self.assertEqual(self.team.public_token, token)

    def test_reset_team_pin_allowed_to_owner_and_superuser(self):
        url = f"/dashboard/team/{self.team.id}/pin/"
        token = self.team.public_token
        self._as(self.owner)
        resp = self.client.post(url, {"pin": "123456", "regenerate_token": "1"},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.access_code, "123456")
        self.assertNotEqual(self.team.public_token, token)

        self._as(self.superadmin)
        self.client.post(url, {"pin": "654321"})
        self.team.refresh_from_db()
        self.assertEqual(self.team.access_code, "654321")

    # --- Team edit / delete / roster / quick assign -------------------------

    def test_edit_participant_forbidden_to_intruders(self):
        url = f"/admin-auction/participants/{self.team.id}/edit/"
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(url, {"display_name": "Hacked", "credits": "9999",
                                          "user_id": str(user.id), "is_active": "1"})
            self.assertEqual(resp.status_code, 403, user.username)
            self.team.refresh_from_db()
            self.assertEqual(self.team.display_name, "Alfa Real")
            self.assertEqual(self.team.user, self.manager)

    def test_edit_participant_allowed_to_owner_and_superuser(self):
        url = f"/admin-auction/participants/{self.team.id}/edit/"
        for user, name in ((self.owner, "Alfa Nuova"), (self.superadmin, "Alfa Super")):
            self._as(user)
            resp = self.client.post(url, {"display_name": name, "is_active": "1",
                                          "user_id": str(self.manager.id)})
            self.assertEqual(resp.status_code, 302)
            self.team.refresh_from_db()
            self.assertEqual(self.team.display_name, name)

    def test_delete_participant_forbidden_to_intruders(self):
        url = f"/admin-auction/participants/{self.rival.id}/delete/"
        for user in self._intruders():
            self._as(user)
            self.assertEqual(self.client.post(url).status_code, 403, user.username)
            self.assertTrue(Participant.objects.filter(pk=self.rival.id).exists())

    def test_delete_participant_allowed_to_owner_and_superuser(self):
        self._as(self.owner)
        self.client.post(f"/admin-auction/participants/{self.rival.id}/delete/")
        self.assertFalse(Participant.objects.filter(pk=self.rival.id).exists())

        self._as(self.superadmin)
        self.client.post(f"/admin-auction/participants/{self.foreign_team.id}/delete/")
        self.assertFalse(Participant.objects.filter(pk=self.foreign_team.id).exists())

    def test_participant_roster_forbidden_to_intruders(self):
        url = f"/admin-auction/participants/{self.team.id}/roster/"
        for user in self._intruders():
            self._as(user)
            self.assertEqual(self.client.get(url).status_code, 403, user.username)

    def test_participant_roster_allowed_to_owner_and_superuser(self):
        url = f"/admin-auction/participants/{self.team.id}/roster/"
        for user in (self.owner, self.superadmin):
            self._as(user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual([r["name"] for r in resp.json()["roster"]], ["Owned Player"])

    def test_quick_assign_forbidden_to_intruders(self):
        for user in self._intruders():
            self._as(user)
            resp = self.client.post("/dashboard/team/assign-player/", {
                "participant_id": self.team.id, "player_id": self.free_player.id, "price": "1",
            }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(resp.status_code, 403, user.username)
            self.free_player.refresh_from_db()
            self.assertIsNone(self.free_player.owner_id)

    def test_quick_assign_rejects_a_player_from_another_league(self):
        """Owning the team is not enough: the player must be in a league you run too."""
        self._as(self.owner)
        resp = self.client.post("/dashboard/team/assign-player/", {
            "participant_id": self.team.id, "player_id": self.foreign_player.id, "price": "1",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 403)
        self.foreign_player.refresh_from_db()
        self.assertIsNone(self.foreign_player.owner_id)

    def test_quick_assign_allowed_to_owner(self):
        self._as(self.owner)
        resp = self.client.post("/dashboard/team/assign-player/", {
            "participant_id": self.team.id, "player_id": self.free_player.id, "price": "12",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.free_player.refresh_from_db()
        self.assertEqual(self.free_player.owner_id, self.team.id)

    # --- Players: delete / release / assign ---------------------------------

    def test_player_actions_forbidden_to_intruders(self):
        for user in self._intruders():
            self._as(user)
            self.assertEqual(
                self.client.post(f"/admin-auction/players/{self.free_player.id}/delete/").status_code,
                403, user.username)
            self.assertEqual(
                self.client.post(f"/admin-auction/players/{self.owned_player.id}/release/").status_code,
                403, user.username)
            self.assertEqual(
                self.client.post(f"/admin-auction/players/{self.free_player.id}/assign/",
                                 {"participant_id": self.team.id, "price": "1"}).status_code,
                403, user.username)
        self.assertTrue(Player.objects.filter(pk=self.free_player.id, owner__isnull=True).exists())
        self.owned_player.refresh_from_db()
        self.assertEqual(self.owned_player.owner_id, self.team.id)
        self.team.refresh_from_db()
        self.assertEqual(self.team.spent_credits, Decimal("0"))

    def test_assign_player_rejects_a_team_or_auction_from_another_league(self):
        self._as(self.owner)
        resp = self.client.post(f"/admin-auction/players/{self.free_player.id}/assign/",
                                {"participant_id": self.foreign_team.id, "price": "1"})
        self.assertEqual(resp.status_code, 403)
        other_auction = make_live_auction(league=self.other_league)
        resp = self.client.post(f"/admin-auction/players/{self.owned_player.id}/release/",
                                {"auction_id": other_auction.id})
        self.assertEqual(resp.status_code, 403)
        self.free_player.refresh_from_db()
        self.owned_player.refresh_from_db()
        self.assertIsNone(self.free_player.owner_id)
        self.assertEqual(self.owned_player.owner_id, self.team.id)

    def test_player_actions_allowed_to_owner_and_superuser(self):
        self._as(self.owner)
        resp = self.client.post(f"/admin-auction/players/{self.free_player.id}/assign/",
                                {"participant_id": self.rival.id, "price": "5",
                                 "auction_id": self.auction.id})
        self.assertEqual(resp.status_code, 200)
        self.free_player.refresh_from_db()
        self.assertEqual(self.free_player.owner_id, self.rival.id)

        resp = self.client.post(f"/admin-auction/players/{self.owned_player.id}/release/",
                                {"auction_id": self.auction.id})
        self.assertEqual(resp.status_code, 200)
        self.owned_player.refresh_from_db()
        self.assertIsNone(self.owned_player.owner_id)

        self._as(self.superadmin)
        resp = self.client.post(f"/admin-auction/players/{self.foreign_player.id}/delete/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Player.objects.filter(pk=self.foreign_player.id).exists())

    # --- Auction regia endpoints --------------------------------------------

    def _auction_posts(self, auction):
        a = auction.id
        return [
            (f"/admin-auction/{a}/edit/", {"title": "Hacked"}),
            (f"/admin-auction/{a}/pause/", {}),
            (f"/admin-auction/{a}/close/", {}),
            (f"/admin-auction/{a}/build-queue/", {}),
            (f"/admin-auction/{a}/queue/prioritize/", {"player_id": self.free_player.id}),
            (f"/admin-auction/{a}/queue/postpone/", {"player_id": self.free_player.id}),
            (f"/admin-auction/{a}/queue/exclude/", {"player_id": self.free_player.id}),
            (f"/admin-auction/{a}/call-player/", {"player_id": self.free_player.id}),
            (f"/admin-auction/{a}/step/", {"direction": "next"}),
            (f"/admin-auction/{a}/announce/", {"text": "ciao"}),
            (f"/admin-auction/{a}/timer/", {"delta": "30"}),
            (f"/admin-auction/{a}/force-close/", {}),
            (f"/admin-auction/{a}/turns/", {"action": "enable"}),
            (f"/admin-auction/{a}/auto-advance/", {"on": "1"}),
            (f"/admin-auction/{a}/confirm-advance/", {}),
            (f"/admin-auction/{a}/bid-for/", {"participant_id": self.team.id, "increment": "10"}),
            (f"/admin-auction/{a}/open-sealed/", {}),
            (f"/admin-auction/{a}/resolve-sealed/", {}),
            (f"/admin-auction/{a}/save-session/", {"name": "snap"}),
        ]

    def _auction_gets(self, auction):
        a = auction.id
        return [
            f"/admin-auction/{a}/queue/",
            f"/admin-auction/{a}/classifica/",
            f"/admin-auction/{a}/storico/",
        ]

    def test_auction_endpoints_forbidden_to_intruders(self):
        before = Auction.objects.filter(pk=self.auction.id).values().get()
        for user in self._intruders():
            self._as(user)
            for url, data in self._auction_posts(self.auction):
                self.assertEqual(self.client.post(url, data).status_code, 403, (user.username, url))
            for url in self._auction_gets(self.auction):
                self.assertEqual(self.client.get(url).status_code, 403, (user.username, url))
        self.assertEqual(Auction.objects.filter(pk=self.auction.id).values().get(), before)
        # The full regia page: another league's admin is refused, a manager
        # with no league of their own is sent back to the app.
        self._as(self.foreign_admin)
        self.assertEqual(self.client.get(f"/regia/{self.auction.id}/").status_code, 403)
        self._as(self.manager)
        self.assertRedirects(self.client.get(f"/regia/{self.auction.id}/"), "/app/",
                             fetch_redirect_response=False)
        self.assertFalse(Bid.objects.filter(auction=self.auction).exists())
        self.assertFalse(self.auction.queue_items.exists())
        self.free_player.refresh_from_db()
        self.assertIsNone(self.free_player.owner_id)

    def test_auction_endpoints_allowed_to_owner_and_superuser(self):
        for user, title in ((self.owner, "Asta Rinominata"), (self.superadmin, "Asta Super")):
            self._as(user)
            resp = self.client.post(f"/admin-auction/{self.auction.id}/edit/", {"title": title})
            self.assertEqual(resp.status_code, 200)
            self.auction.refresh_from_db()
            self.assertEqual(self.auction.title, title)
            resp = self.client.get(f"/admin-auction/{self.auction.id}/classifica/")
            self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f"/admin-auction/{self.auction.id}/pause/")
        self.assertEqual(resp.status_code, 200)
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.status, Auction.Status.PAUSED)

    def test_call_player_rejects_a_player_from_another_league(self):
        self._as(self.owner)
        resp = self.client.post(f"/admin-auction/{self.auction.id}/call-player/",
                                {"player_id": self.foreign_player.id})
        self.assertEqual(resp.status_code, 403)
        self.auction.refresh_from_db()
        self.assertNotEqual(self.auction.player_id, self.foreign_player.id)

    def test_cancel_bid_forbidden_to_intruders(self):
        bid = Bid.objects.create(auction=self.auction, participant=self.team,
                                 amount=Decimal("110"), increment=Decimal("10"), accepted=True)
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(f"/admin-auction/bid/{bid.id}/cancel/")
            self.assertEqual(resp.status_code, 403, user.username)
            bid.refresh_from_db()
            self.assertFalse(bid.cancelled)

    # --- Objects outside any league ------------------------------------------

    def test_objects_without_a_league_are_superuser_only(self):
        orphan_team = Participant.objects.create(display_name="Orfana", access_code="ORF01")
        orphan_auction = make_live_auction(title="Asta orfana")
        self._as(self.owner)
        resp = self.client.post(f"/dashboard/team/{orphan_team.id}/credits/",
                                {"mode": "set", "amount": "1"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.post(f"/admin-auction/{orphan_auction.id}/pause/").status_code, 403)
        self.assertEqual(self.client.get(f"/regia/{orphan_auction.id}/").status_code, 403)

        self._as(self.superadmin)
        self.client.post(f"/dashboard/team/{orphan_team.id}/credits/", {"mode": "set", "amount": "1"})
        orphan_team.refresh_from_db()
        self.assertEqual(orphan_team.credits, Decimal("1"))
        self.assertEqual(self.client.post(f"/admin-auction/{orphan_auction.id}/pause/").status_code, 200)

    # --- ?next= open redirect -----------------------------------------------

    def test_offsite_next_is_ignored(self):
        self._as(self.owner)
        fallback = f"/dashboard/{self.league.id}/#rose"
        for evil in ("https://evil.example/phish", "//evil.example/phish",
                     "http:///evil.example", "javascript:alert(1)"):
            for url, data in (
                (f"/dashboard/team/{self.team.id}/credits/", {"mode": "add", "amount": "1"}),
                (f"/dashboard/team/{self.team.id}/pin/", {"pin": "1111"}),
                (f"/admin-auction/participants/{self.team.id}/edit/",
                 {"display_name": "Alfa Real", "is_active": "1", "user_id": str(self.manager.id)}),
                ("/admin-auction/participants/create/",
                 {"display_name": "Nuova", "league_id": self.league.id}),
            ):
                resp = self.client.post(url, {**data, "next": evil})
                self.assertEqual(resp.status_code, 302, (url, evil))
                self.assertEqual(resp["Location"], fallback, (url, evil))

    def test_same_site_next_is_kept(self):
        self._as(self.owner)
        resp = self.client.post(f"/dashboard/team/{self.team.id}/credits/",
                                {"mode": "add", "amount": "1",
                                 "next": f"/dashboard/{self.league.id}/?tab=rose#rose"})
        self.assertEqual(resp["Location"], f"/dashboard/{self.league.id}/?tab=rose#rose")

    # --- One league at a time --------------------------------------------------

    def _second_league_of_owner(self):
        """Another league the same admin runs, with a free player and a team."""
        league = League.objects.create(name="Lega Alfa Due", owner=self.owner)
        player = Player.objects.create(
            league=league, name="Second Pool Striker", role="A", initial_price=Decimal("8"))
        team = Participant.objects.create(league=league, display_name="Alfa Due FC")
        return league, player, team

    def test_queue_rejects_a_player_from_another_league(self):
        """The queue services look players up in every league: the view keeps
        the auction on its own pool."""
        _league, own_other_pool, _team = self._second_league_of_owner()
        self._as(self.owner)
        for verb in ("prioritize", "postpone"):
            url = f"/admin-auction/{self.auction.id}/queue/{verb}/"
            resp = self.client.post(url, {"player_id": self.foreign_player.id})
            self.assertEqual(resp.status_code, 403, verb)
            resp = self.client.post(url, {"player_id": own_other_pool.id})
            self.assertEqual(resp.status_code, 400, verb)
            self.assertEqual(resp.json()["error"], "league_mismatch")
        self.assertFalse(AuctionQueueItem.objects.filter(auction=self.auction).exists())

        resp = self.client.post(f"/admin-auction/{self.auction.id}/queue/prioritize/",
                                {"player_id": self.free_player.id})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(AuctionQueueItem.objects.filter(
            auction=self.auction, player=self.free_player, done=False).exists())

    def test_call_player_rejects_a_player_of_another_league_of_the_same_admin(self):
        _league, own_other_pool, _team = self._second_league_of_owner()
        self._as(self.owner)
        resp = self.client.post(f"/admin-auction/{self.auction.id}/call-player/",
                                {"player_id": own_other_pool.id})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "league_mismatch")
        self.auction.refresh_from_db()
        self.assertNotEqual(self.auction.player_id, own_other_pool.id)

    def test_same_admin_cannot_mix_two_of_their_leagues(self):
        """Managing both leagues is not enough to move a player between them."""
        _league, own_other_pool, other_team = self._second_league_of_owner()
        other_auction = make_live_auction(league=other_team.league)
        self._as(self.owner)

        resp = self.client.post(f"/admin-auction/players/{own_other_pool.id}/assign/",
                                {"participant_id": self.team.id, "price": "1"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "league_mismatch")
        resp = self.client.post(f"/admin-auction/players/{self.free_player.id}/assign/",
                                {"participant_id": self.team.id, "price": "1",
                                 "auction_id": other_auction.id})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(f"/admin-auction/players/{self.owned_player.id}/release/",
                                {"auction_id": other_auction.id})
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post("/dashboard/team/assign-player/", {
            "participant_id": other_team.id, "player_id": self.free_player.id, "price": "1",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(resp.json()["ok"])

        for player in (own_other_pool, self.free_player):
            player.refresh_from_db()
            self.assertIsNone(player.owner_id)
        self.owned_player.refresh_from_db()
        self.assertEqual(self.owned_player.owner_id, self.team.id)
        for team in (self.team, other_team):
            team.refresh_from_db()
            self.assertEqual(team.spent_credits, Decimal("0"))

    # --- Legacy /admin-auction/create/ ----------------------------------------

    def test_legacy_create_puts_the_auction_in_a_league_its_creator_runs(self):
        self._as(self.owner)
        resp = self.client.post("/admin-auction/create/", {"player_id": self.free_player.id})
        auction = Auction.objects.exclude(pk=self.auction.pk).get()
        self.assertEqual(auction.league_id, self.league.id)
        self.assertEqual(self.client.get(resp["Location"]).status_code, 200)

        # No player: the league the console is on (the owner's only league).
        self.client.post("/admin-auction/create/", {"title": "Riparazione"})
        self.assertEqual(Auction.objects.get(title="Riparazione").league_id, self.league.id)

    def test_legacy_create_refuses_an_auction_nobody_but_a_superuser_could_open(self):
        self._as(self.manager)
        resp = self.client.post("/admin-auction/create/", {"title": "Orfana"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Auction.objects.filter(title="Orfana").exists())

        self._as(self.foreign_admin)
        resp = self.client.post("/admin-auction/create/", {"player_id": self.free_player.id})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Auction.objects.count(), 1)
