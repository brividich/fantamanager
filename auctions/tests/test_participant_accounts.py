"""The coaches' portal accounts, handled from the Squadre page.

The league's president creates a login for a coach, links one the coach
already has, resets a forgotten password, switches it off. Only the league's
owner (or a superuser) may, and a president never gets to change the password
of a login they did not hand out: linking somebody's account to a team and
then resetting it would be a way to steal it.
"""
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from ..models import League, ManagedAccount, Participant
from ..views.admin_participants import SESSION_ACCOUNT_SECRET_KEY


class ParticipantAccountTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superadmin = User.objects.create_superuser("root", "root@x.local", "pw")
        self.owner = User.objects.create_user("presidente", password="pw")
        self.foreign_admin = User.objects.create_user("admin_beta", password="pw")
        # A coach who registered on the portal by himself.
        self.coach = User.objects.create_user("mario", email="mario@x.local", password="mario-pw")

        self.league = League.objects.create(name="Lega Alfa", owner=self.owner)
        self.other_league = League.objects.create(name="Lega Beta", owner=self.foreign_admin)

        self.team = Participant.objects.create(
            league=self.league, display_name="Alfa Real", user=self.coach, credits=Decimal("500"),
        )
        self.free_team = Participant.objects.create(
            league=self.league, display_name="Alfa United", credits=Decimal("500"),
        )
        self.foreign_team = Participant.objects.create(
            league=self.other_league, display_name="Beta United", credits=Decimal("500"),
        )

    def _as(self, user):
        self.client.force_login(user)

    def _post(self, team, **data):
        return self.client.post(f"/admin-auction/participants/{team.id}/account/", data)

    def _page(self, league=None):
        return self.client.get(f"/admin-auction/participants/?league={(league or self.league).id}")

    def _managed(self, username="coach1", password="old-password", team=None, by=None):
        """An account the president created from the page, linked to ``team``."""
        user = User.objects.create_user(username, password=password)
        ManagedAccount.objects.create(user=user, created_by=by or self.owner)
        team = team or self.free_team
        team.user = user
        team.save(update_fields=["user"])
        return user

    # --- The page ------------------------------------------------------------

    def test_page_shows_accounts_to_the_president(self):
        self._as(self.owner)
        resp = self._page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Account portale")
        self.assertContains(resp, "mario")
        self.assertContains(resp, "Crea account")
        self.assertContains(resp, f'/dashboard/participants/{self.free_team.id}/account/')

    def test_page_hides_accounts_in_an_ownerless_league_to_non_superusers(self):
        legacy = League.objects.create(name="Lega Legacy")
        Participant.objects.create(league=legacy, display_name="Vecchia", user=self.coach)
        self._as(self.owner)
        resp = self._page(legacy)
        self.assertEqual(resp.status_code, 403)     # nobody's league: superadmin only
        self.assertNotContains(resp, "Account portale", status_code=403)
        self.assertNotContains(resp, "mario@x.local", status_code=403)

    # --- Create ------------------------------------------------------------

    def test_create_with_generated_password_shows_it_once(self):
        self._as(self.owner)
        resp = self._post(self.free_team, action="create", username="Coach1",
                          email="coach1@x.local", first_name="Luigi", password="")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"league={self.league.id}", resp["Location"])

        self.free_team.refresh_from_db()
        account = self.free_team.user
        self.assertIsNotNone(account)
        self.assertEqual((account.username, account.email, account.first_name),
                         ("Coach1", "coach1@x.local", "Luigi"))
        self.assertFalse(account.is_staff or account.is_superuser)
        self.assertEqual(ManagedAccount.objects.get(user=account).created_by, self.owner)

        password = self.client.session[SESSION_ACCOUNT_SECRET_KEY]["password"]
        self.assertGreaterEqual(len(password), 8)
        self.assertTrue(account.check_password(password))

        page = self._page()
        self.assertContains(page, password)
        self.assertNotIn(SESSION_ACCOUNT_SECRET_KEY, self.client.session)
        self.assertNotContains(self._page(), password)

    def test_created_account_logs_into_the_app_as_its_team(self):
        self._as(self.owner)
        self._post(self.free_team, action="create", username="coach1", password="segreta123")
        self.client.logout()
        resp = self.client.post("/app/login/", {
            "login_mode": "account", "identifier": "coach1", "password": "segreta123",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session.get("participant_id"), self.free_team.id)

    def test_create_rejects_bad_input(self):
        self._as(self.owner)
        cases = [
            {"username": "MARIO", "password": "segreta123"},             # taken, any case
            {"username": "ab", "password": "segreta123"},                # too short
            {"username": "con spazi", "password": "segreta123"},         # invalid chars
            {"username": "coach1", "password": "123"},                   # short password
            {"username": "coach1", "email": "non-una-mail"},             # bad email
            {"username": "coach1", "email": "MARIO@x.local"},            # email taken
        ]
        for data in cases:
            with self.subTest(data=data):
                self._post(self.free_team, action="create", **data)
                self.free_team.refresh_from_db()
                self.assertIsNone(self.free_team.user_id)
        self.assertFalse(User.objects.filter(username="coach1").exists())

    def test_create_refuses_a_team_that_already_has_an_account(self):
        self._as(self.owner)
        self._post(self.team, action="create", username="coach1", password="segreta123")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, self.coach)
        self.assertFalse(User.objects.filter(username="coach1").exists())

    # --- Link / unlink -----------------------------------------------------

    def test_link_by_username_or_email_and_unlink(self):
        # Mario plays in the league (Alfa Real): his login is the president's to link.
        self._as(self.owner)
        self._post(self.free_team, action="link", identifier="MARIO@x.local")
        self.free_team.refresh_from_db()
        self.assertEqual(self.free_team.user, self.coach)

        self._post(self.team, action="unlink")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.user_id)
        self.assertTrue(User.objects.filter(pk=self.coach.pk).exists())   # unlinked, not deleted

        self._post(self.team, action="link", identifier="Mario")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, self.coach)

    def test_a_self_registered_login_outside_the_league_is_not_linked(self):
        """Once Mario plays in none of the president's leagues, his login is not theirs to link:
        he joins with the team code himself."""
        self._as(self.owner)
        self._post(self.team, action="unlink")
        self._post(self.free_team, action="link", identifier="MARIO@x.local")
        self.free_team.refresh_from_db()
        self.assertIsNone(self.free_team.user_id)

    def test_link_unknown_account_changes_nothing(self):
        self._as(self.owner)
        self._post(self.free_team, action="link", identifier="nessuno")
        self.free_team.refresh_from_db()
        self.assertIsNone(self.free_team.user_id)

    # --- Credentials of an account the president created -------------------

    def test_president_resets_password_of_an_account_they_created(self):
        account = self._managed()
        self._as(self.owner)
        self._post(self.free_team, action="password", password="nuova-password")
        account.refresh_from_db()
        self.assertTrue(account.check_password("nuova-password"))

        self._post(self.free_team, action="password", password="")
        account.refresh_from_db()
        generated = self.client.session[SESSION_ACCOUNT_SECRET_KEY]["password"]
        self.assertTrue(account.check_password(generated))

    def test_president_edits_username_and_email(self):
        account = self._managed()
        self._as(self.owner)
        self._post(self.free_team, action="update", username="coach-uno",
                   email="uno@x.local", first_name="Uno")
        account.refresh_from_db()
        self.assertEqual((account.username, account.email, account.first_name),
                         ("coach-uno", "uno@x.local", "Uno"))
        # Someone else's username is refused.
        self._post(self.free_team, action="update", username="mario", email="")
        account.refresh_from_db()
        self.assertEqual(account.username, "coach-uno")

    def test_president_switches_an_account_off_and_on(self):
        account = self._managed()
        self._as(self.owner)
        self._post(self.free_team, action="toggle_active")
        account.refresh_from_db()
        self.assertFalse(account.is_active)
        self._post(self.free_team, action="toggle_active")
        account.refresh_from_db()
        self.assertTrue(account.is_active)

    def test_president_deletes_an_account_they_created(self):
        account = self._managed()
        self._as(self.owner)
        self._post(self.free_team, action="delete")
        self.assertFalse(User.objects.filter(pk=account.pk).exists())
        self.free_team.refresh_from_db()
        self.assertIsNone(self.free_team.user_id)
        self.assertTrue(Participant.objects.filter(pk=self.free_team.pk).exists())

    # --- Accounts a president may not touch ---------------------------------

    def test_president_cannot_reset_a_self_registered_account(self):
        """Linking someone's own login to a team does not hand it over."""
        self._as(self.owner)
        for action, extra in (("password", {"password": "rubata123"}),
                              ("update", {"username": "rubato", "email": ""}),
                              ("toggle_active", {}), ("delete", {})):
            with self.subTest(action=action):
                self._post(self.team, action=action, **extra)
        self.coach.refresh_from_db()
        self.assertTrue(self.coach.check_password("mario-pw"))
        self.assertEqual(self.coach.username, "mario")
        self.assertTrue(self.coach.is_active)
        self.assertContains(self._page(), "se l&#x27;è registrato da solo")

    def test_president_cannot_reset_an_account_with_a_team_elsewhere(self):
        account = self._managed()
        self.foreign_team.user = account
        self.foreign_team.save(update_fields=["user"])
        self._as(self.owner)
        self._post(self.free_team, action="password", password="rubata123")
        account.refresh_from_db()
        self.assertTrue(account.check_password("old-password"))

    def test_president_cannot_reset_another_league_owner(self):
        ManagedAccount.objects.create(user=self.foreign_admin, created_by=self.owner)
        self.free_team.user = self.foreign_admin
        self.free_team.save(update_fields=["user"])
        self._as(self.owner)
        self._post(self.free_team, action="password", password="rubata123")
        self.foreign_admin.refresh_from_db()
        self.assertTrue(self.foreign_admin.check_password("pw"))

    def test_president_cannot_change_their_own_login_here(self):
        self.free_team.user = self.owner
        self.free_team.save(update_fields=["user"])
        self._as(self.owner)
        self._post(self.free_team, action="password", password="nuova-password")
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("pw"))
        # ...but may unlink it from the team.
        self._post(self.free_team, action="unlink")
        self.free_team.refresh_from_db()
        self.assertIsNone(self.free_team.user_id)

    # --- Superuser ---------------------------------------------------------

    def test_superuser_resets_any_coach_account(self):
        self._as(self.superadmin)
        self._post(self.team, action="password", password="nuova-password")
        self.coach.refresh_from_db()
        self.assertTrue(self.coach.check_password("nuova-password"))

    def test_nobody_deletes_a_league_owner_or_switches_themselves_off(self):
        self.free_team.user = self.owner
        self.free_team.save(update_fields=["user"])
        self._as(self.superadmin)
        self._post(self.free_team, action="delete")
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())

        self.free_team.user = self.superadmin
        self.free_team.save(update_fields=["user"])
        self._post(self.free_team, action="toggle_active")
        self._post(self.free_team, action="delete")
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.is_active)

    # --- Who may call the endpoint at all -----------------------------------

    def test_intruders_are_forbidden(self):
        for user in (self.coach, self.foreign_admin):
            with self.subTest(user=user.username):
                self._as(user)
                resp = self._post(self.free_team, action="create",
                                  username="intruso", password="segreta123")
                self.assertEqual(resp.status_code, 403)
        self.free_team.refresh_from_db()
        self.assertIsNone(self.free_team.user_id)

    def test_ownerless_league_is_superuser_only(self):
        legacy = League.objects.create(name="Lega Legacy")
        legacy_team = Participant.objects.create(league=legacy, display_name="Vecchia", user=self.coach)
        self._as(self.owner)
        resp = self._post(legacy_team, action="unlink")
        self.assertEqual(resp.status_code, 403)
        legacy_team.refresh_from_db()
        self.assertEqual(legacy_team.user, self.coach)

    def test_anonymous_is_refused(self):
        resp = self._post(self.free_team, action="create", username="anon", password="segreta123")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(User.objects.filter(username="anon").exists())

    # --- Manage Action (Dashboard & Modal) -----------------------------------

    def test_manage_update_email_and_password(self):
        self._managed(username="coach_edit", password="old_password", team=self.free_team)
        self._as(self.owner)
        resp = self._post(
            self.free_team,
            action="manage",
            username="coach_edit",
            email="coach_new@x.local",
            password="new_password_123",
            league_role="admin",
        )
        self.assertEqual(resp.status_code, 302)
        self.free_team.refresh_from_db()
        self.assertEqual(self.free_team.user.email, "coach_new@x.local")
        self.assertTrue(self.free_team.user.check_password("new_password_123"))
        self.assertTrue(self.league.admins.filter(pk=self.free_team.user.pk).exists())

    def test_manage_create_new_account_if_unlinked(self):
        self._as(self.owner)
        resp = self._post(
            self.free_team,
            action="manage",
            username="nuovo_mister",
            email="mister@x.local",
            password="password_sicura_456",
            league_role="manager",
        )
        self.assertEqual(resp.status_code, 302)
        self.free_team.refresh_from_db()
        self.assertIsNotNone(self.free_team.user)
        self.assertEqual(self.free_team.user.username, "nuovo_mister")
        self.assertEqual(self.free_team.user.email, "mister@x.local")
        self.assertTrue(self.free_team.user.check_password("password_sicura_456"))

    def test_manage_unlink_account(self):
        self._managed(username="coach_to_unlink", team=self.free_team)
        self._as(self.owner)
        resp = self._post(self.free_team, action="manage", unlink_account="1")
        self.assertEqual(resp.status_code, 302)
        self.free_team.refresh_from_db()
        self.assertIsNone(self.free_team.user)

    def test_coadmin_can_manage_team_accounts(self):
        coadmin = User.objects.create_user("coadmin1", password="pw")
        self.league.admins.add(coadmin)
        self._managed(username="coach_by_admin", team=self.free_team)
        self._as(coadmin)
        resp = self._post(
            self.free_team,
            action="manage",
            username="coach_by_admin",
            email="coadmin_set@x.local",
        )
        self.assertEqual(resp.status_code, 302)
        self.free_team.refresh_from_db()
        self.assertEqual(self.free_team.user.email, "coadmin_set@x.local")

    def test_admin_create_participant_with_direct_user_account(self):
        self._as(self.owner)
        resp = self.client.post("/admin-auction/participants/create/", {
            "league_id": self.league.id,
            "display_name": "Nuovo Team Express",
            "credits": "500",
            "access_code": "999999",
            "new_user_username": "coach_express",
            "new_user_email": "express@x.local",
            "new_user_password": "express_pass_123",
        })
        self.assertEqual(resp.status_code, 302)
        p = Participant.objects.filter(display_name="Nuovo Team Express").first()
        self.assertIsNotNone(p)
        self.assertIsNotNone(p.user)
        self.assertEqual(p.user.username, "coach_express")
        self.assertEqual(p.user.email, "express@x.local")
        self.assertTrue(p.user.check_password("express_pass_123"))
        self.assertTrue(ManagedAccount.objects.filter(user=p.user).exists())

    def test_supervisor_impersonate_and_exit(self):
        self._as(self.superadmin)
        # Impersonate mario
        resp = self.client.post("/supervisor/", {
            "action": "impersonate_user",
            "user_id": self.coach.id,
        })
        self.assertEqual(resp.status_code, 302)
        # Verify current logged in user is mario
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.coach.id)
        self.assertEqual(self.client.session.get("supervisor_impersonator_id"), self.superadmin.id)

        # Exit impersonation
        exit_resp = self.client.get(reverse("supervisor_impersonate_exit"))
        self.assertEqual(exit_resp.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.superadmin.id)
        self.assertNotIn("supervisor_impersonator_id", self.client.session)

    def test_supervisor_delete_user(self):
        temp_user = User.objects.create_user("user_to_delete", password="pw")
        self.team.user = temp_user
        self.team.save(update_fields=["user"])

        self._as(self.superadmin)
        resp = self.client.post("/supervisor/", {
            "action": "delete_user",
            "user_id": temp_user.id,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(pk=temp_user.id).exists())
        self.team.refresh_from_db()
        self.assertIsNone(self.team.user)



class TeamsPageTests(TestCase):
    """La stessa pagina Squadre in console e nell'app (Regia)."""

    def setUp(self):
        self.owner = User.objects.create_user("presidente_sq", password="pw")
        self.foreign_admin = User.objects.create_user("admin_sq", password="pw")
        self.league = League.objects.create(name="Lega Squadre", owner=self.owner)
        self.other_league = League.objects.create(name="Lega Altrui", owner=self.foreign_admin)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa Real",
                                               email="alfa@x.local", credits=Decimal("500"))
        Participant.objects.create(league=self.other_league, display_name="Beta United", credits=Decimal("500"))

    @staticmethod
    def _part(resp):
        html = resp.content.decode()
        part = html[html.index("<!-- teams:start -->"):html.index("<!-- teams:end -->")]
        return re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)

    def test_same_screen_in_console_and_app(self):
        self.client.force_login(self.owner)
        q = f"?league={self.league.id}"
        console = self.client.get(reverse("admin_participants") + q)
        app = self.client.get(reverse("app_regia_teams") + q)
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        self.assertEqual(self._part(console), self._part(app))
        self.assertContains(app, "Alfa Real")
        self.assertContains(app, "Crea account")

    def test_app_shows_only_leagues_you_manage(self):
        self.client.force_login(self.foreign_admin)
        resp = self.client.get(reverse("app_regia_teams") + f"?league={self.league.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Beta United")
        self.assertNotContains(resp, "Alfa Real")

    def test_app_is_for_league_admins(self):
        User.objects.create_user("nessuno_sq", password="pw")
        self.client.force_login(User.objects.get(username="nessuno_sq"))
        resp = self.client.get(reverse("app_regia_teams"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("app_login")))

    def test_actions_from_the_app_return_to_the_app(self):
        self.client.force_login(self.owner)
        back = reverse("app_regia_teams") + f"?league={self.league.id}"
        resp = self.client.post(reverse("admin_participant_account", args=[self.team.id]), {
            "action": "create", "username": "alfa-coach", "password": "", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        page = self.client.get(back)
        self.assertContains(page, "Credenziali per «Alfa Real»")
        self.assertContains(page, "alfa-coach")
        # La password generata si vede una volta sola.
        self.assertNotContains(self.client.get(back), "Credenziali per «Alfa Real»")

        resp = self.client.post(reverse("admin_participant_email", args=[self.team.id]),
                                {"email": "nuova@x.local", "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        self.team.refresh_from_db()
        self.assertEqual(self.team.email, "nuova@x.local")

    def test_regia_opens_the_app_page(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_regia") + f"?league={self.league.id}")
        self.assertContains(resp, reverse("app_regia_teams"))
