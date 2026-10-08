"""Automated tests for SaaS RBAC architecture, Auth Gateway, Supervisor, and Tenant Governance."""
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ..models import Auction, League, Participant
from ..views.common import SESSION_LEAGUE_KEY


class SaasRbacTests(TestCase):
    def setUp(self):
        self.client = Client()
        # Clean users to test first user bootstrap
        User.objects.all().delete()

        # Create two leagues with different owners
        self.superadmin = User.objects.create_superuser(
            username="master_admin",
            email="master@platform.local",
            password="adminpassword123",
        )
        self.admin_a = User.objects.create_user(
            username="admin_alfa",
            email="alfa@league.local",
            password="alfapassword123",
        )
        self.admin_b = User.objects.create_user(
            username="admin_beta",
            email="beta@league.local",
            password="betapassword123",
        )
        self.user_manager = User.objects.create_user(
            username="mario_manager",
            email="mario@team.local",
            password="mariopassword123",
        )

        self.league_a = League.objects.create(name="Lega Alfa", owner=self.admin_a, budget=Decimal("500"))
        self.league_b = League.objects.create(name="Lega Beta", owner=self.admin_b, budget=Decimal("300"))

        self.team_a1 = Participant.objects.create(
            league=self.league_a, display_name="Alfa Real", access_code="ALFA01"
        )
        self.team_b1 = Participant.objects.create(
            league=self.league_b, display_name="Beta United", access_code="BETA01"
        )

    def test_unauthenticated_user_sees_portal(self):
        """Unauthenticated visitor sees the SaaS Auth Portal with login, register and guest tabs."""
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("FantaManager", content)
        self.assertIn("Accedi", content)
        self.assertIn("Registrati", content)
        self.assertIn("Accesso Rapido all'Asta Live", content)

    def test_superuser_redirected_to_supervisor(self):
        """Superadmin visiting / is automatically routed to the Supervisor Cockpit."""
        self.client.force_login(self.superadmin)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("supervisor_dashboard"))

    def test_supervisor_cockpit_views_and_security(self):
        """Only superadmin can access /supervisor/; non-superusers receive 403."""
        # Non-superadmin access denied
        self.client.force_login(self.admin_a)
        forbidden_resp = self.client.get(reverse("supervisor_dashboard"))
        self.assertEqual(forbidden_resp.status_code, 403)

        # Superadmin access granted
        self.client.force_login(self.superadmin)
        resp_health = self.client.get(f"{reverse('supervisor_dashboard')}?tab=health")
        self.assertEqual(resp_health.status_code, 200)
        self.assertIn("Master Supervisor", resp_health.content.decode())

        resp_users = self.client.get(f"{reverse('supervisor_dashboard')}?tab=users")
        self.assertEqual(resp_users.status_code, 200)
        self.assertIn("master_admin", resp_users.content.decode())
        self.assertIn("admin_alfa", resp_users.content.decode())

        resp_leagues = self.client.get(f"{reverse('supervisor_dashboard')}?tab=leagues")
        self.assertEqual(resp_leagues.status_code, 200)
        self.assertIn("Lega Alfa", resp_leagues.content.decode())
        self.assertIn("Lega Beta", resp_leagues.content.decode())

    @override_settings(DESKTOP_APP=True)
    def test_registration_bootstrap_and_onboarding(self):
        """Desktop app: first registered user becomes superadmin; subsequent users get onboarding."""
        User.objects.all().delete()

        # Register first user
        resp1 = self.client.post(reverse("register"), {
            "username": "first_user",
            "password": "securepassword123",
            "password_confirm": "securepassword123",
        })
        self.assertRedirects(resp1, reverse("supervisor_dashboard"))
        first_u = User.objects.get(username="first_user")
        self.assertTrue(first_u.is_superuser)

        self.client.logout()

        # Register second user
        resp2 = self.client.post(reverse("register"), {
            "username": "second_user",
            "password": "securepassword123",
            "password_confirm": "securepassword123",
        })
        self.assertRedirects(resp2, reverse("onboarding"))
        second_u = User.objects.get(username="second_user")
        self.assertFalse(second_u.is_superuser)

    def test_on_a_server_the_first_registration_is_an_ordinary_user(self):
        User.objects.all().delete()
        resp = self.client.post(reverse("register"), {
            "username": "first_user",
            "password": "securepassword123",
            "password_confirm": "securepassword123",
        })
        self.assertRedirects(resp, reverse("onboarding"))
        self.assertFalse(User.objects.get(username="first_user").is_superuser)

    def test_registration_refuses_a_weak_password(self):
        for weak in ("abc123", "12345678901", "password123"):
            resp = self.client.post(reverse("register"), {
                "username": "debole", "password": weak, "password_confirm": weak,
            })
            self.assertEqual(resp.status_code, 200, weak)
            self.assertFalse(User.objects.filter(username="debole").exists(), weak)

    def test_onboarding_create_league(self):
        """A user in onboarding can create a league and become its owner/admin."""
        self.client.force_login(self.user_manager)
        resp = self.client.post(reverse("onboarding"), {
            "action": "create_league",
            "name": "Lega Nuova di Mario",
            "game_mode": "CLASSIC",
            "budget": "600",
        })
        new_lg = League.objects.filter(name="Lega Nuova di Mario").first()
        self.assertIsNotNone(new_lg)
        self.assertEqual(new_lg.owner, self.user_manager)
        self.assertRedirects(resp, reverse("dashboard_league", kwargs={"league_id": new_lg.id}))

    def test_onboarding_join_team_with_access_code(self):
        """A user in onboarding enters a team access code and claims that participant."""
        self.client.force_login(self.user_manager)
        resp = self.client.post(reverse("onboarding"), {
            "action": "join_team",
            "access_code": "ALFA01",
        })
        self.assertRedirects(resp, reverse("app_home"))
        # Check team association
        self.team_a1.refresh_from_db()
        self.assertEqual(self.team_a1.user, self.user_manager)
        self.assertEqual(self.client.session.get("participant_id"), self.team_a1.id)

    def test_league_admin_tenant_isolation(self):
        """Admin A sees only League A in the Hub and cannot access League B."""
        self.client.force_login(self.admin_a)

        # Visiting Hub Multi-Tenant
        resp_hub = self.client.get(f"{reverse('admin_dashboard')}?home=1&league=all")
        self.assertEqual(resp_hub.status_code, 200)
        cards = resp_hub.context["league_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["league"].id, self.league_a.id)

        # Attempting to access League B directly must be forbidden
        resp_b = self.client.get(f"{reverse('admin_dashboard')}?home=1&league={self.league_b.id}")
        self.assertEqual(resp_b.status_code, 403)

        # Superadmin can view and manage both
        self.client.force_login(self.superadmin)
        resp_super = self.client.get(f"{reverse('admin_dashboard')}?home=1&league=all")
        self.assertEqual(resp_super.status_code, 200)
        self.assertEqual(len(resp_super.context["league_cards"]), 2)

    def test_supervisor_create_user_standalone(self):
        """Supervisor creates a user without league association."""
        self.client.force_login(self.superadmin)
        resp = self.client.post(f"{reverse('supervisor_dashboard')}?tab=users", {
            "action": "create_user",
            "username": "standalone_user",
            "email": "standalone@test.local",
            "password": "pwd123password",
        })
        self.assertRedirects(resp, f"{reverse('supervisor_dashboard')}?tab=users")
        u = User.objects.filter(username="standalone_user").first()
        self.assertIsNotNone(u)
        self.assertEqual(u.email, "standalone@test.local")
        self.assertEqual(u.leagues.count(), 0)
        self.assertEqual(u.teams.count(), 0)

    def test_supervisor_create_user_as_league_owner(self):
        """Supervisor creates a user and assigns them as owner of an existing league."""
        self.client.force_login(self.superadmin)
        resp = self.client.post(f"{reverse('supervisor_dashboard')}?tab=users", {
            "action": "create_user",
            "username": "league_president",
            "email": "pres@test.local",
            "password": "pwd123password",
            "league_id": str(self.league_a.id),
            "is_league_owner": "on",
        })
        self.assertRedirects(resp, f"{reverse('supervisor_dashboard')}?tab=users")
        u = User.objects.filter(username="league_president").first()
        self.assertIsNotNone(u)
        self.league_a.refresh_from_db()
        self.assertEqual(self.league_a.owner, u)

    def test_supervisor_create_user_with_new_team(self):
        """Supervisor creates a user and assigns a newly created team in the chosen league."""
        self.client.force_login(self.superadmin)
        resp = self.client.post(f"{reverse('supervisor_dashboard')}?tab=users", {
            "action": "create_user",
            "username": "team_manager_1",
            "email": "mgr1@test.local",
            "password": "pwd123password",
            "league_id": str(self.league_a.id),
            "assign_team": "on",
            "team_mode": "new",
            "team_name": "FC Galacticos",
            "team_credits": "480",
        })
        self.assertRedirects(resp, f"{reverse('supervisor_dashboard')}?tab=users")
        u = User.objects.filter(username="team_manager_1").first()
        self.assertIsNotNone(u)
        team = Participant.objects.filter(user=u, league=self.league_a).first()
        self.assertIsNotNone(team)
        self.assertEqual(team.display_name, "FC Galacticos")
        self.assertEqual(team.credits, Decimal("480"))

    def test_supervisor_create_user_owner_and_team(self):
        """Supervisor creates a user as both league owner and team manager."""
        self.client.force_login(self.superadmin)
        resp = self.client.post(f"{reverse('supervisor_dashboard')}?tab=users", {
            "action": "create_user",
            "username": "dual_role_user",
            "email": "dual@test.local",
            "password": "pwd123password",
            "league_id": str(self.league_b.id),
            "is_league_owner": "on",
            "assign_team": "on",
            "team_mode": "new",
            "team_name": "Beta Stars",
            "team_credits": "300",
        })
        self.assertRedirects(resp, f"{reverse('supervisor_dashboard')}?tab=users")
        u = User.objects.filter(username="dual_role_user").first()
        self.assertIsNotNone(u)
        self.league_b.refresh_from_db()
        self.assertEqual(self.league_b.owner, u)
        team = Participant.objects.filter(user=u, league=self.league_b).first()
        self.assertIsNotNone(team)
        self.assertEqual(team.display_name, "Beta Stars")

    def test_supervisor_create_user_with_existing_team(self):
        """Supervisor creates a user and attaches them to an existing unassigned team."""
        unassigned_team = Participant.objects.create(
            league=self.league_a,
            display_name="Squadra Senza Manager",
            access_code="FREE01",
        )
        self.client.force_login(self.superadmin)
        resp = self.client.post(f"{reverse('supervisor_dashboard')}?tab=users", {
            "action": "create_user",
            "username": "claimed_manager",
            "email": "claim@test.local",
            "password": "pwd123password",
            "league_id": str(self.league_a.id),
            "assign_team": "on",
            "team_mode": "existing",
            "existing_team_id": str(unassigned_team.id),
        })
        self.assertRedirects(resp, f"{reverse('supervisor_dashboard')}?tab=users")
        u = User.objects.filter(username="claimed_manager").first()
        self.assertIsNotNone(u)
        unassigned_team.refresh_from_db()
        self.assertEqual(unassigned_team.user, u)

    def test_supervisor_users_tab_display_badges(self):
        """Users tab displays league and team badges correctly."""
        self.team_a1.user = self.user_manager
        self.team_a1.save(update_fields=["user"])

        self.client.force_login(self.superadmin)
        resp = self.client.get(f"{reverse('supervisor_dashboard')}?tab=users")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Lega Alfa", content)
        self.assertIn("Alfa Real", content)


    def test_unauthenticated_access_to_dashboard_redirects_to_login(self):
        """Unauthenticated visitor trying to access dashboard or admin subroutes is redirected to login."""
        resp = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp, f"{reverse('login')}?next={reverse('dashboard')}")

        resp_sub = self.client.get("/admin-auction/players/")
        self.assertRedirects(resp_sub, f"{reverse('login')}?next=/admin-auction/players/")

    def test_unauthenticated_ajax_post_returns_401(self):
        """Unauthenticated AJAX request to admin endpoint returns 401 Unauthorized."""
        resp = self.client.post("/admin-auction/1/step/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("error"), "unauthenticated")

    def test_manager_user_cannot_access_admin_dashboard(self):
        """Logged-in user who owns no leagues is gracefully redirected away from the admin dashboard."""
        # Manager with a team:
        self.team_a1.user = self.user_manager
        self.team_a1.save(update_fields=["user"])
        self.client.force_login(self.user_manager)

        resp = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp, reverse("app_home"))

        # User with no team and no league:
        u_solo = User.objects.create_user(username="solo_user", password="pwd")
        self.client.force_login(u_solo)
        resp_solo = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp_solo, reverse("onboarding"))

    def test_claimed_team_cannot_be_selected_without_credentials(self):
        """In app_login select mode, a team linked to another user cannot be accessed without account credentials."""
        self.team_a1.user = self.user_manager
        self.team_a1.save(update_fields=["user"])

        # Anonymous visitor tries to select team_a1
        resp = self.client.post(reverse("app_login"), {
            "login_mode": "select",
            "participant_id": str(self.team_a1.id),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("associata all'account di un utente", resp.context.get("error", ""))
        self.assertIsNone(self.client.session.get("participant_id"))
