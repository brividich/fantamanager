"""Regressions for the security review: what a hostile team name, upload or
guess must never get through."""
from django.contrib.auth.models import User
from django.test import TestCase

from ..models import Auction, League, Participant, Player

EVIL = "</script><script>alert(1)</script>"


class JsonInScriptTests(TestCase):
    """A team name is shown inside page data: it must stay data, never close
    the <script> it travels in."""

    def setUp(self):
        self.owner = User.objects.create_superuser("root", password="pwd12345")
        self.league = League.objects.create(name="Lega", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name=EVIL,
                                               access_code="EVIL1")
        player = Player.objects.create(league=self.league, name=EVIL, team=EVIL)
        self.auction = Auction.objects.create(league=self.league, title="Asta", player=player,
                                              status=Auction.Status.LIVE)

    def _assert_inert(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(EVIL, resp.content.decode())

    def test_bid_page(self):
        self.client.post("/join/", {"access_code": "EVIL1"})
        self._assert_inert(self.client.get(f"/bid/{self.auction.id}/"))

    def test_screen(self):
        self.client.force_login(self.owner)
        self._assert_inert(self.client.get(f"/screen/{self.auction.id}/"))

    def test_console_and_supervisor(self):
        self.client.force_login(self.owner)
        self._assert_inert(self.client.get("/dashboard/", {"auction": self.auction.id}))
        self._assert_inert(self.client.get("/supervisor/"))
