"""The coaches' portal accounts, handled from the Squadre page.

The league's president creates a login for a coach, links one the coach
already has, resets a forgotten password, switches it off. Only the league's
owner (or a superuser) may, and a president never gets to change the password
of a login they did not hand out: linking somebody's account to a team and
then resetting it would be a way to steal it.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

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
        self.assertContains(resp, f'/admin-auction/participants/{self.free_team.id}/account/')

    def test_page_hides_accounts_in_an_ownerless_league_to_non_superusers(self):
        legacy = League.objects.create(name="Lega Legacy")
        Participant.objects.create(league=legacy, display_name="Vecchia", user=self.coach)
        self._as(self.owner)
        resp = self._page(legacy)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Account portale")
        self.assertNotContains(resp, "mario@x.local")

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
        self._as(self.owner)
        self._post(self.team, action="unlink")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.user_id)
        self.assertTrue(User.objects.filter(pk=self.coach.pk).exists())   # unlinked, not deleted

        self._post(self.free_team, action="link", identifier="MARIO@x.local")
        self.free_team.refresh_from_db()
        self.assertEqual(self.free_team.user, self.coach)

        self._post(self.team, action="link", identifier="Mario")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, self.coach)

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
