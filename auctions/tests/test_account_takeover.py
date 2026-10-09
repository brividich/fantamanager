"""«Collega account» non è un modo per rubare l'account di qualcun altro.

Chiunque si registra e crea una lega diventa presidente. Se dalla scheda di una
sua squadra potesse collegare un account qualsiasi (nome utente o email della
vittima) e nella stessa richiesta cambiargli password ed email, si
prenderebbe l'account di chiunque, perfino di un superuser. Un presidente
collega solo gli account di ``linkable_users`` (chi gioca già nelle sue leghe o
gli account che ha creato lui) e cambia le credenziali solo degli account che ha
creato lui (``ManagedAccount``).
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import League, ManagedAccount, Participant
from ..views.common import linkable_users


class AccountTakeoverTests(TestCase):
    def setUp(self):
        self.president = User.objects.create_user("presidente_a", password="pw-presidente")
        self.league = League.objects.create(name="Lega A", owner=self.president)
        self.team = Participant.objects.create(
            league=self.league, display_name="Squadra Libera", credits=Decimal("500"))

        # Le vittime: nessuna gioca nelle leghe del presidente.
        self.stranger = User.objects.create_user(
            "estraneo", email="estraneo@x.local", password="pw-estraneo")
        self.foreign_owner = User.objects.create_user("presidente_b", password="pw-b")
        self.other_league = League.objects.create(name="Lega B", owner=self.foreign_owner)
        self.foreign_coadmin = User.objects.create_user(
            "coadmin_b", email="coadmin@x.local", password="pw-coadmin")
        self.other_league.admins.add(self.foreign_coadmin)
        self.root = User.objects.create_superuser("root", "root@x.local", "pw-root")
        self.staff = User.objects.create_user(
            "staff", email="staff@x.local", password="pw-staff", is_staff=True)
        self.victims = (self.stranger, self.foreign_coadmin, self.root, self.staff)

        self.client.force_login(self.president)

    def _post(self, team, back=None, **data):
        if back:
            data["next"] = back
        return self.client.post(reverse("admin_participant_account", args=[team.id]), data)

    def _console(self):
        return reverse("admin_participants") + f"?league={self.league.id}"

    def _app(self):
        return reverse("app_regia_teams") + f"?league={self.league.id}"

    def _assert_untouched(self, victim, password, email):
        self.team.refresh_from_db()
        self.assertIsNone(self.team.user_id)
        victim.refresh_from_db()
        self.assertTrue(victim.check_password(password))
        self.assertEqual(victim.email, email)
        self.assertTrue(victim.is_active)
        self.assertFalse(User.objects.filter(username="rubato").exists())

    # --- L'attacco -----------------------------------------------------------

    def test_president_cannot_link_and_take_over_a_foreign_account(self):
        passwords = {self.stranger: "pw-estraneo", self.foreign_coadmin: "pw-coadmin",
                     self.root: "pw-root", self.staff: "pw-staff"}
        for back in (self._console(), self._app()):
            for victim, password in passwords.items():
                email = victim.email
                attempts = {
                    "manage per nome utente": {"action": "manage", "link_identifier": victim.username},
                    "manage per email": {"action": "manage", "link_identifier": email},
                    "manage per id": {"action": "manage", "manage_subaction": "link",
                                      "link_user_id": victim.pk},
                    "link per nome utente": {"action": "link", "identifier": victim.username},
                    "link per email": {"action": "link", "identifier": email},
                }
                for label, data in attempts.items():
                    with self.subTest(vittima=victim.username, via=label, da=back):
                        resp = self._post(self.team, back=back, password="rubata-123456",
                                          email="ladro@evil.tld", username="rubato", **data)
                        self.assertEqual(resp.status_code, 302)
                        self.assertTrue(resp["Location"].startswith(back.split("?")[0]))
                        self._assert_untouched(victim, password, email)

    def test_refused_link_has_no_side_effect(self):
        before = User.objects.get(pk=self.stranger.pk)
        self._post(self.team, action="manage", link_identifier="estraneo",
                   password="rubata-123456", email="ladro@evil.tld", league_role="owner")
        self.team.refresh_from_db()
        self.assertIsNone(self.team.user_id)
        self.league.refresh_from_db()
        self.assertEqual(self.league.owner, self.president)
        self.assertFalse(self.league.admins.filter(pk=self.stranger.pk).exists())
        after = User.objects.get(pk=self.stranger.pk)
        self.assertEqual((after.password, after.email), (before.password, before.email))

    def test_refused_link_says_what_to_do(self):
        resp = self._post(self.team, back=self._console(), action="link", identifier="estraneo")
        page = self.client.get(resp["Location"])
        self.assertContains(page, "codice della squadra")

    def test_unknown_and_foreign_accounts_get_the_same_answer(self):
        """Il messaggio non dice se il nome utente esiste: niente elenco degli iscritti."""
        msgs = []
        for ident in ("estraneo", "nessuno-cosi"):
            resp = self._post(self.team, back=self._console(), action="link", identifier=ident)
            page = self.client.get(resp["Location"])
            msgs.append([str(m) for m in page.context["messages"]])
        self.assertEqual(msgs[0], msgs[1])

    def test_linkable_users_leaves_out_admins_and_foreign_presidents(self):
        # Anche se giocano in una lega del presidente, non sono suoi da collegare.
        for victim in (self.root, self.staff, self.foreign_coadmin, self.foreign_owner):
            Participant.objects.create(league=self.league, display_name=f"T {victim.username}", user=victim)
        users = set(linkable_users(self.president))
        for victim in (self.root, self.staff, self.foreign_coadmin, self.foreign_owner, self.stranger):
            self.assertNotIn(victim, users, victim.username)
        self.assertIn(self.president, users)

    # --- Un account registrato da solo e già collegato -------------------------

    def test_self_registered_account_in_my_league_keeps_its_credentials(self):
        coach = User.objects.create_user("mister", email="mister@x.local", password="pw-mister")
        Participant.objects.create(league=self.league, display_name="Altra", user=coach)
        # Collegarlo a una seconda squadra si può...
        self._post(self.team, action="manage", link_user_id=coach.pk,
                   password="rubata-123456", email="ladro@evil.tld")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, coach)
        coach.refresh_from_db()
        self.assertTrue(coach.check_password("pw-mister"))
        self.assertEqual(coach.email, "mister@x.local")
        # ...ma dalla gestione non gli si cambiano password, email, nome utente o stato.
        for data in ({"password": "rubata-123456"}, {"email": "ladro@evil.tld"},
                     {"username": "rubato"}, {"generate_password": "1"}):
            with self.subTest(**data):
                form = {"username": "mister", "email": "mister@x.local", "is_active": "1"}
                form.update(data)
                self._post(self.team, action="manage", **form)
                coach.refresh_from_db()
                self.assertTrue(coach.check_password("pw-mister"))
                self.assertEqual((coach.username, coach.email, coach.is_active),
                                 ("mister", "mister@x.local", True))
        self._post(self.team, action="manage", username="mister", email="mister@x.local")
        coach.refresh_from_db()
        self.assertTrue(coach.is_active)

    def test_self_registered_account_role_still_changes(self):
        """Il ruolo nella lega è della lega: quello il presidente lo decide."""
        coach = User.objects.create_user("mister", email="mister@x.local", password="pw-mister")
        self.team.user = coach
        self.team.save(update_fields=["user"])
        self._post(self.team, action="manage", username="mister", email="mister@x.local",
                   is_active="1", league_role="admin")
        self.assertTrue(self.league.admins.filter(pk=coach.pk).exists())

    # --- Quello che resta possibile -----------------------------------------

    def test_president_links_a_linkable_account(self):
        mine = User.objects.create_user("creato", password="pw-creato")
        ManagedAccount.objects.create(user=mine, created_by=self.president)
        self._post(self.team, back=self._app(), action="link", identifier="creato")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, mine)

    def test_president_creates_a_managed_account_and_resets_it(self):
        self._post(self.team, action="manage", username="nuovo_mister",
                   email="nuovo@x.local", password="password-sicura-1")
        self.team.refresh_from_db()
        account = self.team.user
        self.assertTrue(ManagedAccount.objects.filter(user=account, created_by=self.president).exists())
        self._post(self.team, action="manage", username="nuovo_mister",
                   email="nuovo@x.local", password="password-nuova-2")
        account.refresh_from_db()
        self.assertTrue(account.check_password("password-nuova-2"))
        self._post(self.team, action="password", password="password-terza-3")
        account.refresh_from_db()
        self.assertTrue(account.check_password("password-terza-3"))

    def test_superuser_does_everything(self):
        self.client.force_login(self.root)
        self._post(self.team, action="manage", link_identifier="estraneo")
        self.team.refresh_from_db()
        self.assertEqual(self.team.user, self.stranger)
        self._post(self.team, action="manage", username="estraneo",
                   email="nuova@x.local", password="password-dal-root")
        self.stranger.refresh_from_db()
        self.assertTrue(self.stranger.check_password("password-dal-root"))
        self.assertEqual(self.stranger.email, "nuova@x.local")
