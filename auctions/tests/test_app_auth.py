"""Tests for Managerial App (FantaManager) authentication and login/logout flows."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Auction, League, Participant
from .common import make_live_auction


class AppAuthTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Serie A 2026", budget=Decimal("500"))
        self.team_open = Participant.objects.create(
            display_name="I Senza Codice",
            league=self.league,
            access_code="",
            credits=Decimal("500"),
        )
        self.team_protected = Participant.objects.create(
            display_name="Real Coded",
            league=self.league,
            access_code="SECRET42",
            credits=Decimal("500"),
        )

    def test_unauthenticated_app_access_redirects_to_login(self):
        """Visiting managerial pages without session redirects to app_login with ?next=."""
        endpoints = [
            reverse("app_home"),
            reverse("app_rosa"),
            reverse("app_mercato"),
            reverse("app_altro"),
        ]
        for url in endpoints:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn(reverse("app_login"), resp["Location"])
            self.assertIn(f"next={url}", resp["Location"])

    def test_login_via_access_code(self):
        """Entering a valid team access code signs in and redirects to destination."""
        resp = self.client.post(
            reverse("app_login"),
            data={"access_code": "SECRET42", "next": reverse("app_mercato")},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("app_mercato"))
        self.assertEqual(self.client.session.get("participant_id"), self.team_protected.id)
        self.assertEqual(self.client.session.get("display_name"), self.team_protected.display_name)

    def test_login_via_tokenized_link(self):
        """Opening app_login with ?t=<public_token> signs in automatically."""
        resp = self.client.get(
            f"{reverse('app_login')}?t={self.team_protected.public_token}&next={reverse('app_rosa')}"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("app_rosa"))
        self.assertEqual(self.client.session.get("participant_id"), self.team_protected.id)

    def test_login_via_team_select(self):
        """Guided team selection: open team logs in directly, protected team requires code."""
        # 1. Open team (no code needed)
        resp = self.client.post(
            reverse("app_login"),
            data={"participant_id": str(self.team_open.id)},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session.get("participant_id"), self.team_open.id)

        # 2. Protected team without code -> fails
        resp_fail = self.client.post(
            reverse("app_login"),
            data={"participant_id": str(self.team_protected.id), "access_code": "WRONG"},
        )
        self.assertEqual(resp_fail.status_code, 200)
        self.assertContains(resp_fail, "Codice di accesso errato")

        # 3. Protected team with correct code -> succeeds
        resp_ok = self.client.post(
            reverse("app_login"),
            data={"participant_id": str(self.team_protected.id), "access_code": "SECRET42"},
        )
        self.assertEqual(resp_ok.status_code, 302)
        self.assertEqual(self.client.session.get("participant_id"), self.team_protected.id)

    def test_login_invalid_code(self):
        """Submitting an invalid code shows error message and does not log in."""
        resp = self.client.post(
            reverse("app_login"),
            data={"access_code": "NON_EXISTENT"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Codice squadra non valido")
        self.assertIsNone(self.client.session.get("participant_id"))

    def test_logout(self):
        """Logging out clears session and redirects to app_login."""
        # Sign in first
        session = self.client.session
        session["participant_id"] = self.team_protected.id
        session["display_name"] = self.team_protected.display_name
        session.save()

        resp = self.client.get(reverse("app_logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("app_login"))
        self.assertIsNone(self.client.session.get("participant_id"))
        self.assertIsNone(self.client.session.get("display_name"))

    def test_auction_remains_free(self):
        """Live auction flow (/join/, /bid/) remains free and independent."""
        auction = make_live_auction(league=self.league)

        # 1. /join/ is accessible and renders without requiring app login
        resp = self.client.get(reverse("join"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Entra nell'asta")

        # 2. Manager already logged in can directly access bid page
        session = self.client.session
        session["participant_id"] = self.team_protected.id
        session.save()

        bid_resp = self.client.get(reverse("bid", kwargs={"auction_id": auction.id}))
        self.assertEqual(bid_resp.status_code, 200)

    def test_update_pin(self):
        """Manager can update their team access code from app_altro."""
        session = self.client.session
        session["participant_id"] = self.team_open.id
        session.save()

        # 1. Update successfully
        resp = self.client.post(
            reverse("app_update_pin"),
            data={"access_code": "MYNEWCODE"},
        )
        self.assertEqual(resp.status_code, 302)
        self.team_open.refresh_from_db()
        self.assertEqual(self.team_open.access_code, "MYNEWCODE")

        # 2. Too short (< 3 chars)
        resp_short = self.client.post(
            reverse("app_update_pin"),
            data={"access_code": "AB"},
        )
        self.assertEqual(resp_short.status_code, 302)
        self.team_open.refresh_from_db()
        self.assertEqual(self.team_open.access_code, "MYNEWCODE")

        # 3. Duplicate code with another team in the same league
        resp_dup = self.client.post(
            reverse("app_update_pin"),
            data={"access_code": "SECRET42"},
        )
        self.assertEqual(resp_dup.status_code, 302)
        self.team_open.refresh_from_db()
        self.assertEqual(self.team_open.access_code, "MYNEWCODE")

    def test_login_via_account_username_success(self):
        """Manager can log into /app using their Django User account username."""
        user = User.objects.create_user(username="mario", email="mario@example.com", password="password123")
        self.team_protected.user = user
        self.team_protected.save()

        resp = self.client.post(
            reverse("app_login"),
            data={"login_mode": "account", "identifier": "mario", "password": "password123"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("app_home"))
        self.assertEqual(self.client.session.get("participant_id"), self.team_protected.id)
        self.assertEqual(self.client.session.get("display_name"), self.team_protected.display_name)
        self.assertEqual(int(self.client.session.get("_auth_user_id")), user.id)

    def test_login_via_account_email_success(self):
        """Manager can log into /app using their email address."""
        user = User.objects.create_user(username="luigi", email="luigi@example.com", password="password123")
        self.team_open.user = user
        self.team_open.save()

        resp = self.client.post(
            reverse("app_login"),
            data={"identifier": "luigi@example.com", "password": "password123", "next": reverse("app_rosa")},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("app_rosa"))
        self.assertEqual(self.client.session.get("participant_id"), self.team_open.id)

    def test_login_via_account_invalid_credentials(self):
        """Entering incorrect account credentials shows error and denies access."""
        User.objects.create_user(username="gianni", password="password123")
        resp = self.client.post(
            reverse("app_login"),
            data={"login_mode": "account", "identifier": "gianni", "password": "wrongpassword"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Credenziali non valide")
        self.assertIsNone(self.client.session.get("participant_id"))

    def test_authenticated_user_direct_access_resolves_participant(self):
        """An already authenticated Django user visiting /app/ has their participant auto-resolved."""
        user = User.objects.create_user(username="paolo", password="password123")
        self.team_protected.user = user
        self.team_protected.save()

        self.client.force_login(user)
        # Session participant_id is not set yet
        self.assertIsNone(self.client.session.get("participant_id"))

        # Accessing /app/ should resolve participant without redirecting to login
        resp = self.client.get(reverse("app_home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.team_protected.display_name)
        self.assertEqual(self.client.session.get("participant_id"), self.team_protected.id)

    def test_logout_clears_participant_and_auth_user(self):
        """Logging out from /app clears participant session and logs out Django user."""
        user = User.objects.create_user(username="claudio", password="password123")
        self.team_protected.user = user
        self.team_protected.save()

        self.client.login(username="claudio", password="password123")
        self.client.post(
            reverse("app_login"),
            data={"login_mode": "account", "identifier": "claudio", "password": "password123"},
        )
        self.assertEqual(self.client.session.get("participant_id"), self.team_protected.id)

        resp = self.client.get(reverse("app_logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("app_login"))
        self.assertIsNone(self.client.session.get("participant_id"))
        self.assertIsNone(self.client.session.get("_auth_user_id"))

