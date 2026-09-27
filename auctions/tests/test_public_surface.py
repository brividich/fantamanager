"""What a stranger on the internet meets: the error pages, and which leagues
the public pages (portal, app login, join) are allowed to show them."""
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.views.defaults import server_error

from ..models import Auction, League, Participant

NAVIGATE = {"HTTP_SEC_FETCH_MODE": "navigate", "HTTP_ACCEPT": "text/html"}
FETCH = {"HTTP_SEC_FETCH_MODE": "cors", "HTTP_ACCEPT": "*/*"}


class ErrorPagesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pwd12345")
        self.league = League.objects.create(name="Mia", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Squadra")

    def test_404_is_the_apps_page_in_italian(self):
        resp = self.client.get("/non-esiste/", **NAVIGATE)
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "Pagina non trovata", status_code=404)
        self.assertContains(resp, "Torna all'inizio", status_code=404)
        self.assertContains(resp, '<html lang="it">', status_code=404)

    def test_500_renders_without_request_or_context(self):
        self.assertIn("Qualcosa è andato storto", render_to_string("500.html"))
        resp = server_error(RequestFactory().get("/"))
        self.assertEqual(resp.status_code, 500)
        self.assertIn("Qualcosa è andato storto", resp.content.decode())

    def test_a_views_plain_refusal_becomes_a_page_for_a_person(self):
        other = User.objects.create_user("vicino", password="pwd12345")
        self.client.force_login(other)
        resp = self.client.get(reverse("admin_export_csv"), {"league": self.league.id}, **NAVIGATE)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "Scegli prima una lega.", status_code=403)   # the view's own words
        self.assertContains(resp, "Torna all'inizio", status_code=403)

    def test_the_apps_own_fetch_calls_still_get_the_plain_text(self):
        other = User.objects.create_user("vicino", password="pwd12345")
        self.client.force_login(other)
        resp = self.client.get(reverse("admin_export_csv"), {"league": self.league.id}, **FETCH)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.content.decode(), "Scegli prima una lega.")
        # No Fetch Metadata and no text/html asked for (scripts, the test client): untouched.
        resp = self.client.get(reverse("admin_export_csv"), {"league": self.league.id})
        self.assertEqual(resp.content.decode(), "Scegli prima una lega.")

    def test_json_refusals_stay_json(self):
        self.client.force_login(User.objects.create_user("vicino", password="pwd12345"))
        resp = self.client.get(reverse("admin_remote_status"), **NAVIGATE)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp["Content-Type"], "application/json")

    def test_opening_a_post_only_address_explains_itself(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("admin_edit_participant", args=[self.team.id]), **NAVIGATE)
        self.assertEqual(resp.status_code, 405)
        self.assertContains(resp, "Azione non disponibile qui", status_code=405)
        self.assertEqual(resp["Allow"], "POST")

    def test_a_stranger_is_offered_the_login(self):
        resp = self.client.get(reverse("participant_qr", args=[self.team.id]), **NAVIGATE)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "/login/?next=", status_code=403)

    def test_a_stale_form_says_so(self):
        client = Client(enforce_csrf_checks=True)
        resp = client.post("/login/", {"identifier": "x", "password": "y"}, **NAVIGATE)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "La pagina è scaduta", status_code=403)


@override_settings(PUBLIC_TOKENS_REQUIRED=True)
class PublicListsOnlineTests(TestCase):
    """Reachable from the internet: a visitor sees only the leagues they are part of."""

    def setUp(self):
        self.mine = League.objects.create(name="Lega Mia")
        self.theirs = League.objects.create(name="Lega Altrui")
        self.my_team = Participant.objects.create(league=self.mine, display_name="Squadra Mia")
        self.their_team = Participant.objects.create(league=self.theirs, display_name="Squadra Altrui")
        self.my_auction = Auction.objects.create(league=self.mine, title="Asta Mia",
                                                 status=Auction.Status.LIVE)
        self.their_auction = Auction.objects.create(league=self.theirs, title="Asta Altrui",
                                                    status=Auction.Status.LIVE)

    def _assert_shows_nobodys(self, resp, *names):
        for name in names:
            self.assertNotContains(resp, name)

    def test_a_stranger_sees_no_league(self):
        for path in ("/portal/", "/app/login/", "/join/"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self._assert_shows_nobodys(resp, "Lega Mia", "Lega Altrui", "Squadra Altrui",
                                       "Asta Altrui", "Asta Mia")
        self.assertNotContains(self.client.get("/app/login/"), "Seleziona Squadra")

    def test_the_team_code_alone_still_gets_a_stranger_in(self):
        resp = self.client.get("/join/")
        self.assertNotContains(resp, 'name="auction_id"')     # no empty, required picker to block the form
        self.my_team.access_code = "PIC77"
        self.my_team.save(update_fields=["access_code"])
        resp = self.client.post("/join/", {"access_code": "PIC77"})
        self.assertRedirects(resp, f"/bid/{self.my_auction.id}/", fetch_redirect_response=False)

    def test_picking_a_league_by_id_opens_nothing(self):
        resp = self.client.get("/portal/", {"league": self.theirs.id})
        self._assert_shows_nobodys(resp, "Lega Altrui", "Asta Altrui", self.their_auction.public_token)

    def test_a_team_sees_its_own_league_only(self):
        self.client.get("/join/", {"t": self.my_team.public_token})    # the team's link
        resp = self.client.get("/app/login/")
        self.assertContains(resp, "Lega Mia")
        self._assert_shows_nobodys(resp, "Lega Altrui", "Squadra Altrui")
        resp = self.client.get("/portal/", {"league": self.theirs.id})
        self._assert_shows_nobodys(resp, "Lega Altrui", "Asta Altrui")

    def test_an_organiser_sees_the_leagues_they_run(self):
        owner = User.objects.create_user("presidente", password="pwd12345")
        self.mine.owner = owner
        self.mine.save(update_fields=["owner"])
        self.client.force_login(owner)
        resp = self.client.get("/portal/")
        self.assertContains(resp, "Lega Mia")
        self.assertNotContains(resp, "Lega Altrui")


@override_settings(PUBLIC_TOKENS_REQUIRED=False)
class PublicListsOnTheLanTests(TestCase):
    """On a trusted LAN everybody in the room plays: nothing changes there."""

    def test_the_room_still_sees_every_league(self):
        for name in ("Lega Uno", "Lega Due"):
            league = League.objects.create(name=name)
            Participant.objects.create(league=league, display_name=f"Squadra di {name}")
        resp = self.client.get("/portal/")
        self.assertContains(resp, "Lega Uno")
        self.assertContains(resp, "Lega Due")
        resp = self.client.get("/app/login/")
        self.assertContains(resp, "Seleziona Squadra")
        self.assertContains(resp, "Squadra di Lega Due")
