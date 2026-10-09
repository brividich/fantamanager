"""Le azioni «lato PC» dell'asta in sala non fanno del server un cliente HTTP.

``sala_connect`` fa partire una richiesta verso l'indirizzo scritto nel form:
sul server sarebbe un POST verso la sua rete interna (database, router,
metadata del cloud) con l'errore letto nella risposta. E uno snapshot enorme
creerebbe leghe con squadre e giocatori senza limite. Quindi: le tre azioni
solo sull'app del PC; anche lì indirizzi pubblici, niente redirect, risposta
letta fino a un tetto, tetti su squadre, giocatori e registro; l'API del sito
rifiuta corpi e risultati oltre gli stessi tetti.
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import League, Participant, Player
from ..services import sala

PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


class _FakeResp:
    def __init__(self, body, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


def _snapshot(n_teams=2, n_players=3):
    return {"ok": True, "lock_id": "L1", "snapshot": {
        "format": sala.FORMAT, "league": {"id": 1, "name": "Enorme"},
        "teams": [{"id": i, "display_name": f"T{i}"} for i in range(n_teams)],
        "players": [{"id": i, "owner": None, "name": f"P{i}", "role": "C"} for i in range(n_players)],
    }}


class SalaConnectOnTheServerTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pw-presidente")
        self.league = League.objects.create(name="Lega", owner=self.owner)
        self.client.force_login(self.owner)

    @override_settings(DESKTOP_APP=False)
    def test_the_pc_actions_are_refused_on_the_server(self):
        with mock.patch("requests.post") as post:
            for action, extra in (("sala_connect", {"site": "http://169.254.169.254", "key": "k"}),
                                  ("sala_send", {"league_id": self.league.id}),
                                  ("sala_release", {"league_id": self.league.id})):
                with self.subTest(action=action):
                    resp = self.client.post(reverse("admin_config_action"),
                                            {"action": action, **extra}, follow=True)
                    self.assertContains(resp, "app FantaManager del PC")
            post.assert_not_called()
        self.assertEqual(League.objects.count(), 1)

    @override_settings(DESKTOP_APP=False)
    def test_the_service_refuses_too(self):
        with mock.patch("requests.post") as post, self.assertRaises(sala.SalaError):
            sala.connect("https://lega.example", "k")
        post.assert_not_called()


@override_settings(DESKTOP_APP=True)
class SalaConnectOnThePcTests(TestCase):
    def setUp(self):
        resolve = mock.patch("auctions.services.sala._resolve", return_value=PUBLIC)
        resolve.start()
        self.addCleanup(resolve.stop)

    def test_internal_addresses_are_refused_without_a_request(self):
        with mock.patch("auctions.services.sala._resolve", side_effect=lambda host, port: [
                (2, 1, 6, "", (host, 80))]), mock.patch("requests.post") as post:
            for site in ("http://127.0.0.1", "http://10.0.0.1", "http://169.254.169.254",
                         "http://[::1]", "ftp://lega.example"):
                with self.subTest(site=site), self.assertRaises(sala.SalaError):
                    sala.connect(site, "k")
        post.assert_not_called()

    def test_a_name_that_resolves_inside_is_refused(self):
        with mock.patch("auctions.services.sala._resolve",
                        return_value=[(2, 1, 6, "", ("10.1.2.3", 443))]), \
                mock.patch("requests.post") as post, self.assertRaises(sala.SalaError):
            sala.connect("https://interno.example", "k")
        post.assert_not_called()

    def test_redirects_are_not_followed(self):
        with mock.patch("requests.post", return_value=_FakeResp(
                b"", status=302, headers={"Location": "http://169.254.169.254/"})) as post:
            with self.assertRaises(sala.SalaError):
                sala.connect("https://lega.example", "k")
        self.assertIs(post.call_args.kwargs.get("allow_redirects"), False)
        self.assertIs(post.call_args.kwargs.get("stream"), True)

    def test_a_huge_answer_is_cut(self):
        body = b'{"ok": true, "pad": "' + b"x" * (sala.MAX_RESPONSE_BYTES + 10) + b'"}'
        with mock.patch("requests.post", return_value=_FakeResp(body)):
            with self.assertRaisesMessage(sala.SalaError, "troppo grande"):
                sala.connect("https://lega.example", "k")
        self.assertEqual(League.objects.count(), 0)

    def test_too_many_players_create_nothing(self):
        with mock.patch("requests.post", return_value=_FakeResp(_snapshot(n_players=5000))):
            with self.assertRaisesMessage(sala.SalaError, "giocatori"):
                sala.connect("https://lega.example", "k")
        self.assertEqual(League.objects.count(), 0)
        self.assertEqual(Player.objects.count(), 0)

    def test_too_many_teams_create_nothing(self):
        with mock.patch("requests.post", return_value=_FakeResp(_snapshot(n_teams=sala.MAX_TEAMS + 1))):
            with self.assertRaisesMessage(sala.SalaError, "squadre"):
                sala.connect("https://lega.example", "k")
        self.assertEqual(League.objects.count(), 0)

    def test_a_normal_league_comes_down(self):
        with mock.patch("requests.post", return_value=_FakeResp(_snapshot())):
            league = sala.connect("https://lega.example", "k")
        self.assertEqual((league.participants.count(), league.players.count()), (2, 3))


class SalaApiCeilingsTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lega", budget=Decimal("500"))
        self.team = Participant.objects.create(league=self.league, display_name="A", credits=Decimal("500"))
        self.key = sala.make_key(self.league)
        self.lock_id = sala.lock(self.league)

    def _results(self, payload, **extra):
        return self.client.post(reverse("sala_results"), data=json.dumps(payload),
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.key}", **extra)

    def test_a_body_over_the_ceiling_is_refused(self):
        resp = self._results({"lock_id": self.lock_id, "pad": "x" * (sala.MAX_BODY_BYTES + 1)})
        self.assertEqual(resp.status_code, 413)
        self.assertIn("troppo grande", resp.json()["error"])
        self.league.refresh_from_db()
        self.assertTrue(sala.is_locked(self.league))

    def test_results_over_the_ceilings_change_nothing(self):
        for field, n in (("log", sala.MAX_LOG_ENTRIES + 1), ("players", sala.MAX_PLAYERS + 1),
                         ("teams", sala.MAX_TEAMS + 1)):
            with self.subTest(field=field):
                resp = self._results({"lock_id": self.lock_id, field: [{}] * n})
                self.assertEqual(resp.status_code, 409)
                self.league.refresh_from_db()
                self.assertTrue(sala.is_locked(self.league))


class SalaConnectFormTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("presidente", password="pw-presidente"))

    def test_the_download_form_is_only_on_the_pc(self):
        for desktop, shown in ((False, False), (True, True)):
            with self.subTest(desktop=desktop), self.settings(DESKTOP_APP=desktop):
                html = self.client.get(reverse("admin_config")).content.decode()
                self.assertEqual('value="sala_connect"' in html, shown)
