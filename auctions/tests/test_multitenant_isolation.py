"""Tests for strict multi-tenant isolation across leagues, auctions and dashboards."""
from decimal import Decimal
from django.test import Client, TestCase
from django.urls import reverse

from ..models import Auction, League, Participant, Player
from ..views.common import SESSION_AUCTION_KEY, SESSION_LEAGUE_KEY


class MultiTenantIsolationTests(TestCase):
    def setUp(self):
        self.client = Client()
        from django.contrib.auth.models import User
        self.superadmin = User.objects.create_superuser(
            username="iso_superadmin",
            email="iso@test.local",
            password="password123",
        )
        self.client.force_login(self.superadmin)
        # Create two distinct leagues (tenants)
        self.league_a = League.objects.create(name="Lega Alfa", budget=Decimal("500"))
        self.league_b = League.objects.create(name="Lega Beta", budget=Decimal("300"))

        # Participants
        self.team_a1 = Participant.objects.create(league=self.league_a, display_name="Alfa Team 1")
        self.team_a2 = Participant.objects.create(league=self.league_a, display_name="Alfa Team 2")
        self.team_b1 = Participant.objects.create(league=self.league_b, display_name="Beta Team 1")

        # Players
        self.player_a = Player.objects.create(league=self.league_a, name="Giocatore A", role="A")
        self.player_b = Player.objects.create(league=self.league_b, name="Giocatore B", role="C")

        # Live auction ONLY in League A
        self.auction_a = Auction.objects.create(
            league=self.league_a,
            title="Asta Invernale Alfa",
            status=Auction.Status.LIVE,
            duration_seconds=60,
        )

    def test_multitenant_hub_shown_when_no_league_selected(self):
        """When multiple leagues exist and no league is specified, dashboard renders the Multi-Tenant Hub."""
        response = self.client.get(f"{reverse('admin_dashboard')}?home=1")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["current_league"])
        self.assertIsNone(response.context["active_auction"])
        # Should have league cards for both leagues
        self.assertEqual(len(response.context["league_cards"]), 2)
        # Verify text content
        content = response.content.decode()
        self.assertIn("Hub Multi-Tenant", content)
        self.assertIn("Lega Alfa", content)
        self.assertIn("Lega Beta", content)
        # Should NOT render active banner for League A inside the hub hero
        self.assertNotIn("ASTA IN CORSO ORA IN SALA", content)

    def test_single_league_scoped_properly(self):
        """Selecting League B scopes dashboard strictly to League B."""
        url = f"{reverse('admin_dashboard')}?home=1&league={self.league_b.id}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_league"].id, self.league_b.id)
        # League B has no active auction, so active_auction must be None
        self.assertIsNone(response.context["active_auction"])
        # Teams must only belong to League B
        participants = list(response.context["participants"])
        self.assertEqual(len(participants), 1)
        self.assertEqual(participants[0].id, self.team_b1.id)
        # Session must store selected_league_id
        self.assertEqual(self.client.session.get(SESSION_LEAGUE_KEY), self.league_b.id)

    def test_active_auction_does_not_leak_to_other_league(self):
        """League A's live auction must never appear when viewing League B."""
        # View League B
        response_b = self.client.get(f"{reverse('admin_dashboard')}?home=1&league={self.league_b.id}")
        self.assertEqual(response_b.status_code, 200)
        self.assertIsNone(response_b.context["active_auction"])
        content_b = response_b.content.decode()
        self.assertNotIn("Regia Asta Live", content_b)
        self.assertNotIn("Asta Invernale Alfa", content_b)

        # View League A
        response_a = self.client.get(f"{reverse('admin_dashboard')}?home=1&league={self.league_a.id}")
        self.assertEqual(response_a.status_code, 200)
        self.assertIsNotNone(response_a.context["active_auction"])
        self.assertEqual(response_a.context["active_auction"].id, self.auction_a.id)
        content_a = response_a.content.decode()
        self.assertIn("Regia Asta Live", content_a)
        self.assertIn("Asta Invernale Alfa", content_a)

    def test_league_switch_clears_stale_auction_pin(self):
        """Switching to League B clears a pinned auction from League A."""
        session = self.client.session
        session[SESSION_AUCTION_KEY] = self.auction_a.id
        session[SESSION_LEAGUE_KEY] = self.league_a.id
        session.save()

        # Switch to League B
        response = self.client.get(f"{reverse('admin_dashboard')}?home=1&league={self.league_b.id}")
        self.assertEqual(response.status_code, 200)
        # The pinned auction from League A must have been cleared from session
        self.assertNotIn(SESSION_AUCTION_KEY, self.client.session)
        self.assertEqual(self.client.session.get(SESSION_LEAGUE_KEY), self.league_b.id)

    def test_home_portal_multitenant_no_arbitrary_active_banner(self):
        """Home landing portal must not show League A's live auction when no league is chosen."""
        response = self.client.get(reverse("home_portal"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["current_league"])
        self.assertIsNone(response.context["active_auction"])
        content = response.content.decode()
        # Must not display the active banner
        self.assertNotIn("ASTA LIVE IN CORSO", content)
        # Must offer league selector
        self.assertIn("Seleziona la tua Lega", content)

    def test_league_reset_via_all(self):
        """Passing league=all clears session tenant and returns to Multi-Tenant Hub."""
        # Set session to League A first
        session = self.client.session
        session[SESSION_LEAGUE_KEY] = self.league_a.id
        session.save()

        response = self.client.get(f"{reverse('admin_dashboard')}?home=1&league=all")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["current_league"])
        self.assertNotIn(SESSION_LEAGUE_KEY, self.client.session)
        content = response.content.decode()
        self.assertIn("Hub Multi-Tenant", content)

    def test_join_flow_scopes_league_and_session(self):
        """Joining with access code records participant league in session and isolates candidate auctions."""
        self.team_b1.access_code = "BETA123"
        self.team_b1.save()

        # Create an auction for League B as well
        auction_b = Auction.objects.create(
            league=self.league_b,
            title="Asta Invernale Beta",
            status=Auction.Status.LIVE,
            duration_seconds=60,
        )

        # GET /join/?league=league_b.id should only display League B's auctions
        response = self.client.get(f"{reverse('join')}?league={self.league_b.id}")
        self.assertEqual(response.status_code, 200)
        joinable = list(response.context["joinable"])
        self.assertEqual(len(joinable), 1)
        self.assertEqual(joinable[0].id, auction_b.id)

        # POST /join/ with team_b1 credentials
        post_response = self.client.post(reverse("join"), {
            "access_code": "BETA123",
            "auction_id": self.auction_a.id, # malicious attempt to join League A's auction
        })
        # Should redirect to team_b1's own league auction (auction_b), not auction_a
        self.assertRedirects(post_response, reverse("bid", kwargs={"auction_id": auction_b.id}))
        # Session must have recorded participant's league
        self.assertEqual(self.client.session.get(SESSION_LEAGUE_KEY), self.league_b.id)
        self.assertEqual(self.client.session.get("participant_id"), self.team_b1.id)

    def test_app_login_and_logout_manages_league_session(self):
        """app_login saves SESSION_LEAGUE_KEY and app_logout removes it."""
        self.team_a1.access_code = "ALFA99"
        self.team_a1.save()

        login_resp = self.client.post(reverse("app_login"), {
            "access_code": "ALFA99",
        })
        self.assertRedirects(login_resp, reverse("app_home"))
        self.assertEqual(self.client.session.get(SESSION_LEAGUE_KEY), self.league_a.id)

        logout_resp = self.client.get(reverse("app_logout"))
        self.assertRedirects(logout_resp, reverse("app_login"))
        self.assertNotIn(SESSION_LEAGUE_KEY, self.client.session)

    def test_admin_export_scoped_to_target_league(self):
        """admin_export respects target_league and does not default to first league if unset."""
        # Unset / Hub view
        resp_all = self.client.get(f"{reverse('admin_export')}?league=all")
        self.assertEqual(resp_all.status_code, 200)
        self.assertIsNone(resp_all.context["current_league"])

        # Explicit League B
        resp_b = self.client.get(f"{reverse('admin_export')}?league={self.league_b.id}")
        self.assertEqual(resp_b.status_code, 200)
        self.assertEqual(resp_b.context["current_league"].id, self.league_b.id)
        # Standings should only have League B's participants
        standings = resp_b.context["standings"]
        self.assertEqual(len(standings), 1)
        self.assertEqual(standings[0]["participant"].id, self.team_b1.id)

