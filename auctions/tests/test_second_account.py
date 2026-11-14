"""Due account per una squadra: il secondo allenatore (``Participant.co_user``).

L'admin della lega lo crea o lo collega dalla pagina Squadre (``slot=2``),
oppure manda il link per il secondo allenatore, che vale una volta. Il
secondo account entra nell'app come la squadra, come il primo. Il link
d'invito della squadra e il codice continuano a collegare solo il primo.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import League, ManagedAccount, Participant
from ..services import privacy


class SecondAccountTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente2", password="pw")
        self.coach = User.objects.create_user("primo", password="Password-Lunga-1")
        self.league = League.objects.create(name="Lega Doppia", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Doppia Real",
                                               user=self.coach, credits=Decimal("500"))
        self.other = Participant.objects.create(league=self.league, display_name="Altra FC",
                                                credits=Decimal("500"))

    def _post(self, **data):
        self.client.force_login(self.owner)
        return self.client.post(reverse("admin_participant_account", args=[self.team.id]), data)

    def _page(self, name="admin_participants"):
        self.client.force_login(self.owner)
        return self.client.get(reverse(name) + f"?league={self.league.id}")

    # --- Dalla pagina Squadre ------------------------------------------------

    def test_president_creates_the_second_account(self):
        self._post(action="create", slot="2", username="secondo", password="Password-Lunga-2")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, self.coach)            # il primo resta
        self.assertEqual(self.team.co_user.username, "secondo")
        self.assertTrue(ManagedAccount.objects.filter(user=self.team.co_user, created_by=self.owner).exists())
        for name in ("admin_participants", "app_regia_teams"):
            with self.subTest(page=name):
                page = self._page(name)
                self.assertContains(page, "Secondo allenatore")
                self.assertContains(page, "secondo")

    def test_second_account_plays_the_team(self):
        self._post(action="create", slot="2", username="secondo", password="Password-Lunga-2")
        self.client.logout()
        resp = self.client.post(reverse("app_login"), {
            "login_mode": "account", "identifier": "secondo", "password": "Password-Lunga-2"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session.get("participant_id"), self.team.id)
        self.assertEqual(self.client.get(reverse("app_home")).status_code, 200)
        # Dal portale, già dentro con l'account, si arriva alla squadra come il primo.
        self.client.get(reverse("app_logout"))
        self.client.force_login(User.objects.get(username="secondo"))
        self.client.get(reverse("home"))
        self.assertEqual(self.client.session.get("participant_id"), self.team.id)

    def test_president_as_second_coach_is_not_viewing_as(self):
        self._post(action="link", slot="2", identifier="presidente2")
        self.team.refresh_from_db()
        self.assertEqual(self.team.co_user, self.owner)
        self.client.force_login(self.owner)
        session = self.client.session
        session["participant_id"] = self.team.id
        session.save()
        home = self.client.get(reverse("app_home"))
        self.assertEqual(home.status_code, 200)
        self.assertFalse(home.context["viewing_as"])

    def test_same_account_twice_or_from_another_team_is_refused(self):
        # Il primo account non può essere anche il secondo.
        self._post(action="link", slot="2", identifier="primo")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.co_user_id)
        # Chi guida già un'altra squadra della lega non diventa secondo qui.
        rival = User.objects.create_user("rivale", password="pw")
        self.other.user = rival
        self.other.save()
        self._post(action="link", slot="2", identifier="rivale")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.co_user_id)

    def test_unlink_second_leaves_the_first(self):
        self._post(action="create", slot="2", username="secondo", password="Password-Lunga-2")
        self._post(action="unlink", slot="2")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, self.coach)
        self.assertIsNone(self.team.co_user_id)
        self.assertTrue(User.objects.filter(username="secondo").exists())

    def test_create_refuses_when_the_second_place_is_taken(self):
        self._post(action="create", slot="2", username="secondo", password="Password-Lunga-2")
        self._post(action="create", slot="2", username="terzo", password="Password-Lunga-3")
        self.assertFalse(User.objects.filter(username="terzo").exists())

    # --- Il link per il secondo allenatore -------------------------------------

    def test_co_invite_link_links_once(self):
        self._post(action="co_invite", slot="2")
        self.team.refresh_from_db()
        token = self.team.co_invite_token
        self.assertTrue(token)
        self.assertContains(self._page(), reverse("invite", args=[token]))

        self.client.logout()
        url = reverse("invite", args=[token])
        page = self.client.get(url)
        self.assertContains(page, "secondo allenatore")
        self.assertNotContains(page, "Solo l'asta, senza account")
        # Senza account non si entra: niente «guest» con questo link.
        self.client.post(url, {"action": "guest"})
        self.assertIsNone(self.client.session.get("participant_id"))

        # Un allenatore che si è registrato da solo entra col suo account.
        User.objects.create_user("socio", password="Password-Lunga-4")
        resp = self.client.post(url, {"action": "login", "identifier": "socio", "password": "Password-Lunga-4"})
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.co_user.username, "socio")
        self.assertEqual(self.team.user, self.coach)
        self.assertEqual(self.team.co_invite_token, "")
        self.assertEqual(self.client.session.get("participant_id"), self.team.id)
        # Il link vale una volta.
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_co_invite_refuses_an_account_with_a_team_in_the_league(self):
        self._post(action="co_invite", slot="2")
        self.team.refresh_from_db()
        rival = User.objects.create_user("rivale", password="pw")
        self.other.user = rival
        self.other.save()
        self.client.force_login(rival)
        page = self.client.post(reverse("invite", args=[self.team.co_invite_token]), {"action": "link"})
        self.assertContains(page, "Hai già una squadra in questa lega")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.co_user_id)

    def test_co_invite_revoke(self):
        self._post(action="co_invite", slot="2")
        self.team.refresh_from_db()
        token = self.team.co_invite_token
        self._post(action="co_invite_revoke", slot="2")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("invite", args=[token])).status_code, 404)

    def test_team_link_still_does_not_take_a_linked_team(self):
        """Il link d'invito della squadra non riempie il secondo posto: solo l'admin lo apre."""
        stranger = User.objects.create_user("estraneo", password="pw")
        self.client.force_login(stranger)
        page = self.client.post(reverse("invite", args=[self.team.public_token]), {"action": "link"})
        self.assertContains(page, "già collegata a un altro account")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.co_user_id)

    # --- Account ----------------------------------------------------------------

    def test_deleting_the_second_account_frees_the_place(self):
        socio = User.objects.create_user("socio", password="pw")
        self.team.co_user = socio
        self.team.save()
        self.assertIn(self.team, Participant.objects.of_user(socio))
        privacy.delete_account(socio)
        self.team.refresh_from_db()
        self.assertIsNone(self.team.co_user_id)
        self.assertEqual(self.team.user, self.coach)
