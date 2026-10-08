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
        """Guided team selection: a team without a code needs a logged-in account,
        a protected team needs its code."""
        # 1. A team with no code is not free for anyone to take.
        resp = self.client.post(
            reverse("app_login"),
            data={"participant_id": str(self.team_open.id)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "accedi al tuo account")
        self.assertIsNone(self.client.session.get("participant_id"))

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

        # 2. Too short (sotto la lunghezza minima dei codici scelti a mano)
        resp_short = self.client.post(
            reverse("app_update_pin"),
            data={"access_code": "ABCDE"},
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

        # 4. Codice già usato da una squadra di un'altra lega: il codice da
        #    solo fa entrare, quindi dev'essere unico ovunque.
        other = League.objects.create(name="Altra lega")
        Participant.objects.create(league=other, display_name="Altrove",
                                   access_code="ALTROVE9", is_active=True)
        self.client.post(reverse("app_update_pin"), data={"access_code": "altrove9"})
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



class AppLoginTeamLinkTests(TestCase):
    """Opening a team by code while signed in links it to the account only when
    it is the manager's own first team in that league, never for an admin."""

    def setUp(self):
        self.admin = User.objects.create_user("presidente", password="pwd12345")
        self.league = League.objects.create(name="Lega", owner=self.admin)
        self.own = Participant.objects.create(league=self.league, display_name="Mia", user=self.admin)
        self.other = Participant.objects.create(league=self.league, display_name="Altra", access_code="DRAGO23")
        self.manager = User.objects.create_user("mario", password="pwd12345")

    def _code(self, code="DRAGO23"):
        return self.client.post(reverse("app_login"), {"login_mode": "code", "access_code": code})

    def test_admin_opens_a_team_by_code_without_taking_it(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self._code(), reverse("app_home"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["participant_id"], self.other.id)
        self.other.refresh_from_db()
        self.assertIsNone(self.other.user_id)
        # Next login still opens the admin's own team.
        self.client.logout()
        self.client.post(reverse("app_login"), {"login_mode": "account", "identifier": "presidente", "password": "pwd12345"})
        self.assertEqual(self.client.session["participant_id"], self.own.id)

    def test_admin_opens_a_team_from_the_list_without_taking_it(self):
        self.client.force_login(self.admin)
        free = Participant.objects.create(league=self.league, display_name="Senza Codice")
        resp = self.client.post(reverse("app_login"), {"login_mode": "select", "participant_id": free.id})
        self.assertEqual(resp.status_code, 302)
        free.refresh_from_db()
        self.assertIsNone(free.user_id)

    def test_manager_claims_first_team_by_code(self):
        self.client.force_login(self.manager)
        self._code()
        self.other.refresh_from_db()
        self.assertEqual(self.other.user_id, self.manager.id)

    def test_manager_with_a_team_does_not_take_a_second_one(self):
        Participant.objects.create(league=self.league, display_name="Di Mario", user=self.manager)
        self.client.force_login(self.manager)
        self._code()
        self.other.refresh_from_db()
        self.assertIsNone(self.other.user_id)


class StaleCsrfTokenTests(TestCase):
    def test_pages_send_the_current_csrf_token(self):
        """A form drawn before a login elsewhere sends the cookie's token (base.html),
        on the app login as on the console login."""
        for url in (reverse("app_login"), reverse("login")):
            with self.subTest(url):
                body = self.client.get(url).content.decode()
                self.assertIn("csrftoken=", body)
                self.assertIn('input[name="csrfmiddlewaretoken"]', body)


class LoginIdentifierTests(TestCase):
    """The phone capitalises the first letter and autocomplete may add a space:
    the account still has to be found, on the app login as on the console's."""

    def setUp(self):
        self.user = User.objects.create_user("lazze85", email="Lazze@Example.it", password="segreta1")
        self.league = League.objects.create(name="Lega", owner=self.user)
        self.team = Participant.objects.create(league=self.league, display_name="Gelsi", user=self.user)

    def _app(self, identifier, password="segreta1"):
        self.client.logout()
        self.client.post(reverse("app_login"), {"login_mode": "account", "identifier": identifier, "password": password})
        return self.client.session.get("_auth_user_id")

    def _console(self, identifier, password="segreta1"):
        self.client.logout()
        self.client.post(reverse("login"), {"identifier": identifier, "password": password})
        return self.client.session.get("_auth_user_id")

    def test_variants_are_recognised(self):
        for login in (self._app, self._console):
            for identifier, password in (("Lazze85", "segreta1"), ("LAZZE85 ", "segreta1"),
                                         ("lazze@example.it", "segreta1"), ("lazze85", "segreta1 ")):
                with self.subTest(login=login.__name__, identifier=identifier, password=password):
                    self.assertEqual(login(identifier, password), str(self.user.id))

    def test_wrong_password_is_still_refused(self):
        for login in (self._app, self._console):
            with self.subTest(login.__name__):
                self.assertIsNone(login("Lazze85", "Segreta1"))

    def test_exact_username_wins_over_a_case_twin(self):
        twin = User.objects.create_user("Lazze85", password="altra")
        self.assertEqual(self._app("Lazze85", "altra"), str(twin.id))
        self.assertEqual(self._app("lazze85", "segreta1"), str(self.user.id))


class MultiTeamAccountTests(TestCase):
    """Un account con squadre in due leghe sceglie con quale entrare: mai la
    prima che capita nel database."""

    def setUp(self):
        from django.contrib.auth.models import User
        from ..models import League
        self.user = User.objects.create_user("doppio", password="Pw-doppio-2026")
        self.one = Participant.objects.create(display_name="Squadra Uno", user=self.user,
                                              league=League.objects.create(name="Lega Uno"), access_code="UNO111")
        self.two = Participant.objects.create(display_name="Squadra Due", user=self.user,
                                              league=League.objects.create(name="Lega Due"), access_code="DUE222")

    def test_login_asks_which_team(self):
        resp = self.client.post(reverse("app_login"), {"login_mode": "account", "identifier": "doppio",
                                                      "password": "Pw-doppio-2026"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("switch=1", resp["Location"])
        self.assertIsNone(self.client.session.get("participant_id"))
        page = self.client.get(resp["Location"])
        self.assertContains(page, "Con quale squadra entri?")
        self.assertContains(page, "Lega Due")

    def test_picking_one_of_your_teams_needs_no_code(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("app_login"), {"login_mode": "select", "participant_id": self.two.id})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session["participant_id"], self.two.id)
        # Back on the app later: still the team picked, not the first one.
        self.assertRedirects(self.client.get(reverse("app_login")), reverse("app_home"),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.session["participant_id"], self.two.id)
