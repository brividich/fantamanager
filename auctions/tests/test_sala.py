"""La lega che va dal sito al PC della sala e torna (services/sala.py).

Il PC e il sito qui sono lo stesso processo: le chiamate HTTP del PC
(``requests.post``) arrivano alle view dell'API attraverso il client di test,
così si prova tutto il giro, messaggi d'errore compresi.
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from ..models import League, Participant, Player, RosterLog
from ..services import sala

SITE = "https://lega.example"


class _Resp:
    def __init__(self, resp):
        self.status_code = resp.status_code
        self._body = resp.content

    def json(self):
        return json.loads(self._body)


class SalaRoundTripTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente_sala", password="pw")
        self.league = League.objects.create(name="Lega Sala", owner=self.owner, budget=Decimal("500"))
        self.alfa = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.beta = Participant.objects.create(league=self.league, display_name="Beta", credits=Decimal("500"),
                                               spent_credits=Decimal("40"))
        self.lautaro = Player.objects.create(league=self.league, name="Lautaro", role="A", team="Inter",
                                             owner=self.beta, cost=Decimal("40"))
        self.barella = Player.objects.create(league=self.league, name="Barella", role="C", team="Inter",
                                             initial_price=Decimal("12"))
        other = League.objects.create(name="Altra")
        self.foreign = Player.objects.create(league=other, name="Estraneo", role="D")
        self.key = sala.make_key(self.league, by="presidente_sala")
        self.http = Client()
        patcher = mock.patch("requests.post", side_effect=self._post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, url, json=None, timeout=None, headers=None):
        assert url.startswith(SITE + sala.API_PATH), url
        resp = self.http.post(url[len(SITE):], data=json or {}, content_type="application/json",
                              HTTP_AUTHORIZATION=(headers or {}).get("Authorization", ""))
        return _Resp(resp)

    def _pc_auction(self, copy):
        """Sul PC: Barella ad Alfa per 30, Lautaro svincolato con 20 di rimborso."""
        alfa = copy.participants.get(display_name="Alfa")
        beta = copy.participants.get(display_name="Beta")
        Player.objects.filter(league=copy, name="Barella").update(owner=alfa, cost=Decimal("30"))
        Participant.objects.filter(pk=alfa.pk).update(spent_credits=Decimal("30"))
        Player.objects.filter(league=copy, name="Lautaro").update(owner=None, cost=Decimal("0"))
        Participant.objects.filter(pk=beta.pk).update(spent_credits=Decimal("20"))
        RosterLog.objects.create(participant=alfa, participant_name="Alfa", player_name="Barella",
                                 player_role="C", action=RosterLog.Action.ASSIGN, credits_delta=Decimal("-30"))

    # --- la chiave ---------------------------------------------------------

    def test_key_identifies_the_league_and_is_shown_once(self):
        self.assertEqual(sala.league_for_key(self.key), self.league)
        self.assertNotIn(self.key, json.dumps(self.league.sala))       # solo l'impronta
        self.assertIsNone(sala.league_for_key("fmsala_sbagliata"))
        new = sala.make_key(self.league)
        self.assertIsNone(sala.league_for_key(self.key))               # la vecchia non vale più
        self.assertEqual(sala.league_for_key(new), self.league)

    def test_wrong_key_is_refused(self):
        with self.assertRaisesMessage(sala.SalaError, "Chiave non valida"):
            sala.connect(SITE, "fmsala_sbagliata")
        resp = self.http.post("/api/sala/v1/stato/", data={}, content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    # --- andata e ritorno --------------------------------------------------

    def test_download_locks_the_site_and_copies_the_league(self):
        copy = sala.connect(SITE, self.key)
        self.league.refresh_from_db()
        self.assertTrue(sala.is_locked(self.league))
        with self.assertRaises(sala.LeagueLocked):
            sala.ensure_unlocked(self.league)
        self.assertNotEqual(copy.pk, self.league.pk)
        self.assertEqual(copy.name, "Lega Sala")
        self.assertEqual(copy.budget, Decimal("500"))
        self.assertEqual(sorted(copy.participants.values_list("display_name", flat=True)), ["Alfa", "Beta"])
        lautaro = Player.objects.get(league=copy, name="Lautaro")
        self.assertEqual(lautaro.owner.display_name, "Beta")
        self.assertEqual(lautaro.cost, Decimal("40"))
        self.assertEqual(Player.objects.get(league=copy, name="Barella").initial_price, Decimal("12"))
        self.assertEqual(sala.link_info(copy)["site"], SITE)

    def test_results_come_back_and_unlock_the_site(self):
        copy = sala.connect(SITE, self.key)
        self._pc_auction(copy)
        report = sala.send_results(copy)
        self.assertEqual(report["players"], 2)
        self.assertEqual(report["teams"], 2)

        self.barella.refresh_from_db(); self.lautaro.refresh_from_db()
        self.alfa.refresh_from_db(); self.beta.refresh_from_db(); self.league.refresh_from_db()
        self.assertEqual((self.barella.owner, self.barella.cost), (self.alfa, Decimal("30")))
        self.assertEqual((self.lautaro.owner, self.lautaro.cost), (None, Decimal("0")))
        self.assertEqual(self.alfa.spent_credits, Decimal("30"))
        self.assertEqual(self.beta.spent_credits, Decimal("20"))
        self.assertFalse(sala.is_locked(self.league))
        log = RosterLog.objects.get(participant=self.alfa, player_name="Barella")
        self.assertTrue(log.note.startswith("Asta in sala"))
        # Già inviati: non si rimandano.
        with self.assertRaises(sala.SalaError):
            sala.send_results(copy)

    def test_second_download_needs_force(self):
        sala.connect(SITE, self.key)
        with self.assertRaisesMessage(sala.SalaError, "già bloccata"):
            sala.connect(SITE, self.key)
        first = sala.lock_info(League.objects.get(pk=self.league.pk))["id"]
        sala.connect(SITE, self.key, force=True)
        self.assertNotEqual(sala.lock_info(League.objects.get(pk=self.league.pk))["id"], first)

    def test_results_of_an_old_lock_change_nothing(self):
        old = sala.connect(SITE, self.key)
        sala.connect(SITE, self.key, force=True)     # un altro PC ha riscaricato
        self._pc_auction(old)
        with self.assertRaisesMessage(sala.SalaError, "non è più valido"):
            sala.send_results(old)
        self.barella.refresh_from_db()
        self.assertIsNone(self.barella.owner)

    def test_foreign_ids_change_nothing(self):
        copy = sala.connect(SITE, self.key)
        payload = sala.results_payload(copy)
        payload["players"].append({"id": self.foreign.id, "owner": None, "cost": "0"})
        payload["players"][0]["owner"] = self.alfa.id
        self.league.refresh_from_db()
        with self.assertRaisesMessage(sala.SalaError, "non è di questa lega"):
            sala.apply_results(self.league, payload["lock_id"], payload)
        self.assertEqual(Player.objects.filter(league=self.league, owner=self.alfa).count(), 0)
        self.league.refresh_from_db()
        self.assertTrue(sala.is_locked(self.league))

    def test_release_unlocks_without_changes(self):
        copy = sala.connect(SITE, self.key)
        self._pc_auction(copy)
        sala.release(copy)
        self.league.refresh_from_db(); self.barella.refresh_from_db()
        self.assertFalse(sala.is_locked(self.league))
        self.assertIsNone(self.barella.owner)

    def test_site_unreachable_says_so(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.ConnectionError):
            with self.assertRaisesMessage(sala.SalaError, "Il sito non risponde"):
                sala.connect(SITE, self.key)
        with self.assertRaisesMessage(sala.SalaError, "Indirizzo del sito non valido"):
            sala.connect("lega.example", self.key)


class SalaLockGuardTests(TestCase):
    """Con la lega bloccata dall'asta in sala il sito non cambia rose,
    crediti e listone; il resto della lega resta libero."""

    def setUp(self):
        from .. import services
        self.services = services
        self.owner = User.objects.create_user("presidente_blocco", password="pw")
        self.league = League.objects.create(name="Lega Bloccata", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.free = Player.objects.create(league=self.league, name="Libero", role="C", initial_price=Decimal("5"))
        self.owned = Player.objects.create(league=self.league, name="Preso", role="A", owner=self.team,
                                           cost=Decimal("10"))
        sala.lock(self.league, by="test")
        self.client.force_login(self.owner)

    def test_services_refuse(self):
        with self.assertRaises(sala.LeagueLocked):
            self.services.assign_player(self.free.id, self.team.id, price=3)
        with self.assertRaises(sala.LeagueLocked):
            self.services.release_player(self.owned.id, by_admin=True)
        self.free.refresh_from_db(); self.owned.refresh_from_db()
        self.assertIsNone(self.free.owner)
        self.assertEqual(self.owned.owner, self.team)

    def test_app_and_javascript_get_the_message_as_json(self):
        resp = self.client.post(reverse("admin_quick_assign_player"),
                                {"participant_id": self.team.id, "player_id": self.free.id, "price": "3"},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 423)
        self.assertIn("Asta in corso in sala", resp.json()["error"])
        self.assertIsNone(Player.objects.get(pk=self.free.pk).owner)

    def test_a_page_gets_the_message_and_goes_back(self):
        back = reverse("admin_participants") + f"?league={self.league.id}"
        resp = self.client.post(reverse("admin_adjust_team_credits", args=[self.team.id]),
                                {"mode": "add", "amount": "50", "next": back}, HTTP_SEC_FETCH_MODE="navigate")
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        page = self.client.get(back)
        self.assertContains(page, "Asta in corso in sala")
        self.assertEqual(Participant.objects.get(pk=self.team.pk).credits, Decimal("500"))

    def test_team_name_stays_editable_but_not_its_credits(self):
        url = reverse("admin_edit_participant", args=[self.team.id])
        self.client.post(url, {"display_name": "Alfa Nuova", "credits": "500", "is_active": "1"},
                         HTTP_SEC_FETCH_MODE="navigate")
        self.assertEqual(Participant.objects.get(pk=self.team.pk).display_name, "Alfa Nuova")
        self.client.post(url, {"display_name": "Alfa Nuova", "credits": "900", "is_active": "1"},
                         HTTP_SEC_FETCH_MODE="navigate")
        self.assertEqual(Participant.objects.get(pk=self.team.pk).credits, Decimal("500"))

    def test_no_live_auction_on_the_site_while_locked(self):
        from .common import make_live_auction
        from ..models import Auction
        auction = make_live_auction(league=self.league, status=Auction.Status.READY)
        with self.assertRaises(sala.LeagueLocked):
            self.services.start_auction(auction.id)

    def test_lock_refused_while_the_site_runs_an_auction(self):
        from .common import make_live_auction
        sala.unlock(self.league)
        make_live_auction(league=self.league)
        with self.assertRaisesMessage(sala.SalaError, "asta in corso"):
            sala.lock(self.league)

    def test_default_listone_skips_locked_leagues(self):
        from ..services import footballers
        with mock.patch.object(footballers, "apply_default_listone") as apply:
            footballers.apply_default_listoni()
        self.assertNotIn(self.league, [c.args[0] for c in apply.call_args_list])

    def test_unlocked_league_works_as_before(self):
        sala.unlock(self.league)
        res = self.services.assign_player(self.free.id, self.team.id, price=3)
        self.assertTrue(res["ok"])
