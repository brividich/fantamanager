from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from auctions.models import (
    Auction,
    AuctionCycleResult,
    League,
    Participant,
    Player,
)


class DashboardSportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("superadmin", "super@example.com", "pass123")
        self.client.force_login(self.user)

        self.league_a = League.objects.create(
            name="Serie A Fantacalcio",
            budget=Decimal("500"),
            game_mode="classic",
            slots_p=3,
            slots_d=8,
            slots_c=8,
            slots_a=6,
        )
        self.team_1 = Participant.objects.create(
            league=self.league_a,
            display_name="FC Real Colchoneros",
            credits=Decimal("420"),
            spent_credits=Decimal("80"),
        )
        self.team_2 = Participant.objects.create(
            league=self.league_a,
            display_name="AC Dinamo Milano",
            credits=Decimal("490"),
            spent_credits=Decimal("10"),
        )

        self.p_portiere = Player.objects.create(
            league=self.league_a,
            name="Maignan Mike",
            role="P",
            owner=self.team_1,
            cost=Decimal("35"),
        )
        self.p_attaccante = Player.objects.create(
            league=self.league_a,
            name="Lautaro Martinez",
            role="A",
            owner=self.team_1,
            cost=Decimal("45"),
        )

        self.auction = Auction.objects.create(
            league=self.league_a,
            title="Asta Estiva 2026",
            status=Auction.Status.LIVE,
        )

        AuctionCycleResult.objects.create(
            auction=self.auction,
            cycle=1,
            player_name="Lautaro Martinez",
            player_role="A",
            amount=Decimal("45"),
            winner_name="FC Real Colchoneros",
        )

    def test_dashboard_canonical_url(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Gestionale Sportivo")
        self.assertContains(resp, "Console Gestionale")

    def test_dashboard_hub_url(self):
        resp = self.client.get(reverse("dashboard_hub"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Federazione Leghe Sportive")
        self.assertContains(resp, "Hub Multi-Tenant")
        # Multi-tenant hub should show the league card
        self.assertContains(resp, "Serie A Fantacalcio")

    def test_dashboard_league_url_and_metrics(self):
        resp = self.client.get(reverse("dashboard_league", args=[self.league_a.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Serie A Fantacalcio")
        # Check telemetry HUD cards
        self.assertContains(resp, "Rose & Calciatori")
        self.assertContains(resp, "Monte Crediti & Spesa")
        self.assertContains(resp, "Squadre & Club")
        # Check standings and recent deals
        self.assertContains(resp, "FC Real Colchoneros")
        self.assertContains(resp, "Lautaro Martinez")
        self.assertContains(resp, "AC Dinamo Milano")

    def test_legacy_admin_auction_home_compatibility(self):
        resp = self.client.get(f"{reverse('admin_dashboard')}?home=1")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Gestionale Sportivo")

    def test_recent_assignments_feed(self):
        resp = self.client.get(reverse("dashboard_league", args=[self.league_a.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Ultime Assegnazioni")
        self.assertContains(resp, "Lautaro Martinez")
        self.assertContains(resp, "45")


class RosterContractYearsTests(TestCase):
    """Le rose mostrano gli anni di contratto, in sola lettura, con lo stesso
    badge (auctions/_contract_badge.html) in console, Regia e Rosa dell'app."""

    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pwd12345")
        self.league = League.objects.create(name="Lega Contratti", owner=self.owner, contracts_enabled=True)
        self.team = Participant.objects.create(league=self.league, display_name="Gelsi United", user=self.owner)
        for name, years in (("Ultimo Anno", 1), ("Triennale", 3), ("Senza Dado", None)):
            Player.objects.create(league=self.league, name=name, role="D", owner=self.team,
                                  cost=Decimal("5"), contract_years=years)
        self.client.force_login(self.owner)

    def _pages(self):
        console = self.client.get(reverse("dashboard_league", args=[self.league.id]))
        regia = self.client.get(reverse("app_regia") + f"?league={self.league.id}")
        session = self.client.session
        session["participant_id"] = self.team.id
        session.save()
        rosa = self.client.get(reverse("app_rosa"))
        return {"console": console, "regia": regia, "rosa": rosa}

    def test_years_shown_everywhere(self):
        for where, resp in self._pages().items():
            with self.subTest(where):
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                self.assertIn('title="Ultimo anno di contratto"', body)
                self.assertIn('title="3 anni"', body)
                self.assertIn("da tirare", body)

    def test_hidden_when_contracts_off(self):
        self.league.contracts_enabled = False
        self.league.save()
        for where, resp in self._pages().items():
            with self.subTest(where):
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn('class="pl-ct', resp.content.decode())
