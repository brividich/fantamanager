"""Automated tests for DeviceRoutingMiddleware and PC vs Mobile routing."""
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from ..models import League, Participant


class DeviceRoutingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="admin_user",
            email="admin@test.local",
            password="password123",
        )
        self.manager = User.objects.create_user(
            username="manager_user",
            email="manager@test.local",
            password="password123",
        )
        self.league = League.objects.create(name="Lega Serie A", owner=self.admin, budget=Decimal("500"))
        self.team = Participant.objects.create(
            league=self.league,
            display_name="Squadra Campione",
            access_code="CAMP01",
            user=self.manager,
        )

        self.mobile_ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
        )
        self.desktop_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    def test_mobile_unauthenticated_hits_home_redirects_to_app_login(self):
        """On smartphone, an anonymous visitor visiting / is redirected to the App login."""
        resp = self.client.get(reverse("home"), HTTP_USER_AGENT=self.mobile_ua)
        self.assertRedirects(resp, reverse("app_login"))

    def test_mobile_manager_hits_home_redirects_to_app_home(self):
        """On smartphone, a logged in manager visiting / is redirected to app_home."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("home"), HTTP_USER_AGENT=self.mobile_ua)
        self.assertRedirects(resp, reverse("app_home"))

    def test_mobile_admin_hits_home_redirects_to_app_regia(self):
        """On smartphone, a league admin visiting / is redirected to app_regia."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("home"), HTTP_USER_AGENT=self.mobile_ua)
        self.assertRedirects(resp, reverse("app_regia"))

    def test_mobile_visitor_hits_home_portal_redirects_to_app_login(self):
        """On smartphone, navigating to /portal/ automatically routes to app_login."""
        resp = self.client.get(reverse("home_portal"), HTTP_USER_AGENT=self.mobile_ua)
        self.assertRedirects(resp, reverse("app_login"))

    def test_mobile_manager_hits_home_portal_redirects_to_app_home(self):
        """On smartphone, a manager opening /portal/ goes to app_home."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("home_portal"), HTTP_USER_AGENT=self.mobile_ua)
        self.assertRedirects(resp, reverse("app_home"))

    def test_desktop_unauthenticated_hits_home_shows_web_portal(self):
        """On PC, an unauthenticated visitor sees the SaaS web portal."""
        resp = self.client.get(reverse("home"), HTTP_USER_AGENT=self.desktop_ua)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("FantaManager", content)
        self.assertIn("Accedi", content)

    def test_desktop_admin_hits_home_redirects_to_dashboard(self):
        """On PC, a league admin visiting / goes straight to the Web Dashboard/Regia."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("home"), HTTP_USER_AGENT=self.desktop_ua)
        self.assertRedirects(resp, reverse("dashboard"))

    def test_desktop_manager_hits_home_redirects_to_home_portal(self):
        """On PC, a team manager visiting / goes to the Web portal."""
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("home"), HTTP_USER_AGENT=self.desktop_ua)
        self.assertRedirects(resp, reverse("home_portal"))

    def test_mobile_admin_can_still_access_regia_and_dashboard(self):
        """Admin has full access to the Web Regia / Dashboard even from a mobile device."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("dashboard"), HTTP_USER_AGENT=self.mobile_ua)
        self.assertEqual(resp.status_code, 200)

    def test_manual_view_override(self):
        """Query parameter ?view=web forces desktop view even on a mobile user-agent."""
        resp = self.client.get(f"{reverse('home')}?view=web", HTTP_USER_AGENT=self.mobile_ua)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Accedi", resp.content.decode())
