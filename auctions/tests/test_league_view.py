"""Tests for the Single League Page, Clean URLs, Rose & Squadre, and Member management."""
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auctions.models import League, Participant, Player, Auction

User = get_user_model()


class SingleLeagueViewTests(TestCase):
    def setUp(self):
        self.super_user = User.objects.create_superuser(
            username="admin_boss", password="password123", email="boss@fantamanager.local"
        )
        self.manager_user = User.objects.create_user(
            username="manager_carlo", password="password123", email="carlo@example.com"
        )
        self.client.force_login(self.super_user)

        self.league = League.objects.create(
            name="Serie A Fantacalcio 2026",
            owner=self.super_user,
            budget=Decimal("500"),
            slots_p=3,
            slots_d=8,
            slots_c=8,
            slots_a=6,
        )

        self.team_1 = Participant.objects.create(
            league=self.league,
            display_name="FC Galacticos",
            user=self.manager_user,
            access_code="7777",
            credits=Decimal("500"),
            spent_credits=Decimal("150"),
        )
        self.team_2 = Participant.objects.create(
            league=self.league,
            display_name="Real Bomber",
            access_code="8888",
            credits=Decimal("500"),
            spent_credits=Decimal("200"),
        )

        # Create players for team 1
        self.p1 = Player.objects.create(
            league=self.league,
            name="Maignan",
            role=Player.Role.P,
            team="MIL",
            initial_price=Decimal("40"),
            cost=Decimal("45"),
            owner=self.team_1,
        )
        self.p2 = Player.objects.create(
            league=self.league,
            name="Dimarco",
            role=Player.Role.D,
            team="INT",
            initial_price=Decimal("35"),
            cost=Decimal("38"),
            owner=self.team_1,
        )
        self.p3 = Player.objects.create(
            league=self.league,
            name="Barella",
            role=Player.Role.C,
            team="INT",
            initial_price=Decimal("32"),
            cost=Decimal("35"),
            owner=self.team_1,
        )
        self.p4 = Player.objects.create(
            league=self.league,
            name="Lautaro",
            role=Player.Role.A,
            team="INT",
            initial_price=Decimal("110"),
            cost=Decimal("120"),
            owner=self.team_1,
        )

        # Free player
        self.free_p = Player.objects.create(
            league=self.league,
            name="Kvaratskhelia",
            role=Player.Role.A,
            team="NAP",
            initial_price=Decimal("95"),
            cost=Decimal("0"),
            owner=None,
        )

    def test_single_league_url_renders_successfully(self):
        """The clean /dashboard/<league_id>/ URL loads the full single league cockpit."""
        resp = self.client.get(f"/dashboard/{self.league.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Serie A Fantacalcio 2026")
        self.assertContains(resp, "FC Galacticos")
        self.assertContains(resp, "Rose & Squadre")
        self.assertContains(resp, "Iscritti & Manager")

    def test_roster_by_role_is_displayed(self):
        """Players are organized by role (P, D, C, A) with their purchase price."""
        resp = self.client.get(f"/dashboard/{self.league.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Maignan")
        self.assertContains(resp, "Dimarco")
        self.assertContains(resp, "Barella")
        self.assertContains(resp, "Lautaro")
        self.assertContains(resp, "Portieri")
        self.assertContains(resp, "Difensori")
        self.assertContains(resp, "Centrocampisti")
        self.assertContains(resp, "Attaccanti")

    def test_legacy_admin_auction_league_redirects_to_clean_url(self):
        """Accessing /admin-auction/?league=X redirects to /dashboard/X/ cleanly."""
        resp = self.client.get(f"/admin-auction/?league={self.league.id}")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/dashboard/{self.league.id}/")

    def test_adjust_team_credits_add(self):
        """Bonus credits can be added to a team with instant feedback."""
        url = reverse("admin_adjust_team_credits", args=[self.team_1.id])
        resp = self.client.post(url, {"mode": "add", "amount": "25"})
        self.assertEqual(resp.status_code, 302)
        self.team_1.refresh_from_db()
        self.assertEqual(self.team_1.credits, Decimal("525"))

    def test_adjust_team_credits_sub(self):
        """Penalties can be subtracted from team credits."""
        url = reverse("admin_adjust_team_credits", args=[self.team_1.id])
        resp = self.client.post(url, {"mode": "sub", "amount": "50"})
        self.assertEqual(resp.status_code, 302)
        self.team_1.refresh_from_db()
        self.assertEqual(self.team_1.credits, Decimal("450"))

    def test_reset_team_pin(self):
        """Team PIN can be customized or regenerated."""
        url = reverse("admin_reset_team_pin", args=[self.team_1.id])
        resp = self.client.post(url, {"pin": "999999"})
        self.assertEqual(resp.status_code, 302)
        self.team_1.refresh_from_db()
        self.assertEqual(self.team_1.access_code, "999999")
        # Too short to resist guessing: refused, the old code stays.
        self.client.post(url, {"pin": "9999"})
        self.team_1.refresh_from_db()
        self.assertEqual(self.team_1.access_code, "999999")
        # Empty: a fresh random code, 8 characters from secrets.
        self.client.post(url, {"pin": ""})
        self.team_1.refresh_from_db()
        self.assertEqual(len(self.team_1.access_code), 8)

    def test_quick_assign_player(self):
        """Free player can be directly assigned to a team."""
        url = reverse("admin_quick_assign_player")
        resp = self.client.post(url, {
            "participant_id": self.team_2.id,
            "player_id": self.free_p.id,
            "price": "90",
        })
        self.assertEqual(resp.status_code, 302)
        self.free_p.refresh_from_db()
        self.assertEqual(self.free_p.owner_id, self.team_2.id)
        self.assertEqual(self.free_p.cost, Decimal("90"))
