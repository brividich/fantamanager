"""La lega che va dal sito al PC della sala e torna (services/sala.py).

Il PC e il sito qui sono lo stesso processo: le chiamate HTTP del PC
(``requests.post``) arrivano alle view dell'API attraverso il client di test,
così si prova tutto il giro, messaggi d'errore compresi.
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, TransactionTestCase, override_settings
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

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


# Il PC vero è l'app desktop (DESKTOP_APP), e il sito ha un indirizzo pubblico:
# qui la rete è finta, quindi anche la risoluzione del nome.
_PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


def _as_the_pc(test):
    resolve = mock.patch("auctions.services.sala._resolve", return_value=_PUBLIC)
    resolve.start()
    test.addCleanup(resolve.stop)


@override_settings(DESKTOP_APP=True)
class _SalaSite(TestCase):
    """Un sito con una lega e la sua chiave; le chiamate del PC vanno all'API."""

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
        _as_the_pc(self)

    def _post(self, url, json=None, timeout=None, headers=None, **kwargs):
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


class SalaRoundTripTests(_SalaSite):
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


@override_settings(DESKTOP_APP=True)
class SalaPageTests(TestCase):
    """I tasti dell'asta in sala nella pagina Impostazioni (console e app)."""

    def setUp(self):
        self.owner = User.objects.create_user("presidente_pagina", password="pw")
        self.league = League.objects.create(name="Lega Pagina", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.player = Player.objects.create(league=self.league, name="Libero", role="C")
        self.client.force_login(self.owner)
        self.page = reverse("admin_config") + f"?league={self.league.id}"
        self.http = Client()
        patcher = mock.patch("requests.post", side_effect=self._post)
        patcher.start()
        self.addCleanup(patcher.stop)
        _as_the_pc(self)

    def _post(self, url, json=None, timeout=None, headers=None, **kwargs):
        resp = self.http.post(url[len(SITE):], data=json or {}, content_type="application/json",
                              HTTP_AUTHORIZATION=(headers or {}).get("Authorization", ""))
        return _Resp(resp)

    def _action(self, action, **data):
        return self.client.post(reverse("admin_config_action"),
                                {"action": action, "league_id": self.league.id, "next": self.page, **data})

    def test_key_is_shown_once_with_the_site_address(self):
        self._action("sala_key")
        page = self.client.get(self.page).content.decode()
        self.league.refresh_from_db()
        key = page.split('data-copy-text="', 1)[1].split('"', 1)[0]
        self.assertEqual(sala.league_for_key(key), self.league)
        self.assertIn("http://testserver", page)
        self.assertNotIn(key, self.client.get(self.page).content.decode())   # una volta sola
        self.assertContains(self.client.get(self.page), "Chiave attiva")

    def test_revoke_and_manual_unlock(self):
        key = sala.make_key(self.league)
        sala.lock(self.league)
        self.assertContains(self.client.get(self.page), "In corso: rose, crediti e listone")
        self._action("sala_unlock")
        self._action("sala_key_revoke")
        self.league.refresh_from_db()
        self.assertFalse(sala.is_locked(self.league))
        self.assertIsNone(sala.league_for_key(key))

    def test_other_admins_cannot_touch_the_key(self):
        stranger = User.objects.create_user("estraneo", password="pw")
        self.client.force_login(stranger)
        self._action("sala_key")
        self.league.refresh_from_db()
        self.assertFalse(sala.has_key(self.league))

    def test_the_pc_downloads_and_sends_back(self):
        key = sala.make_key(self.league)
        resp = self.client.post(reverse("admin_config_action"),
                                {"action": "sala_connect", "site": SITE, "key": key, "next": self.page})
        copy = League.objects.exclude(pk=self.league.pk).get(name="Lega Pagina")
        self.assertIn(f"#lg-{copy.id}", resp["Location"])
        self.assertEqual(copy.owner, self.owner)
        page = self.client.get(reverse("admin_config") + f"?league={copy.id}").content.decode()
        self.assertIn("Invia i risultati al sito", page)

        Player.objects.filter(league=copy, name="Libero").update(
            owner=copy.participants.get(display_name="Alfa"), cost=Decimal("7"))
        self.client.post(reverse("admin_config_action"),
                         {"action": "sala_send", "league_id": copy.id, "next": self.page})
        self.player.refresh_from_db(); self.league.refresh_from_db()
        self.assertEqual((self.player.owner, self.player.cost), (self.team, Decimal("7")))
        self.assertFalse(sala.is_locked(self.league))
        self.assertContains(self.client.get(reverse("admin_config") + f"?league={copy.id}"), "Risultati inviati")

    def test_wrong_key_shows_the_reason(self):
        self.client.post(reverse("admin_config_action"),
                         {"action": "sala_connect", "site": SITE, "key": "fmsala_no", "next": self.page})
        self.assertContains(self.client.get(self.page), "Chiave non valida")
        self.assertEqual(League.objects.filter(name="Lega Pagina").count(), 1)


class SalaLiveTests(_SalaSite):
    """Fase 3: chi gioca da fuori entra dall'app del sito nell'asta del PC."""

    TUNNEL = "https://abc-def.trycloudflare.com"

    def test_pc_publishes_the_address_and_teams_enter_from_the_site(self):
        copy = sala.connect(SITE, self.key)
        sala.publish_live(self.TUNNEL, wait=True)
        self.league.refresh_from_db()
        live = sala.live_info(self.league)
        self.assertEqual(live["url"], self.TUNNEL)
        pc_alfa = copy.participants.get(display_name="Alfa")
        self.assertEqual(sala.live_entry_url(self.alfa), f"{self.TUNNEL}/join/?t={pc_alfa.public_token}")
        self.assertEqual(sala.link_info(Participant.objects.get(pk=pc_alfa.pk).league)["live_url"], self.TUNNEL)

        # Il manager di Alfa, nell'app del sito: il richiamo e l'ingresso.
        app = Client()
        s = app.session; s["participant_id"] = self.alfa.id; s.save()
        self.assertContains(app.get(reverse("app_home")), "Asta in corso in sala")
        resp = app.get(reverse("app_sala_enter"))
        self.assertEqual(resp["Location"], f"{self.TUNNEL}/join/?t={pc_alfa.public_token}")

        # Tunnel chiuso: niente ingresso, la home dice cosa aspettare.
        sala.publish_live(None, wait=True)
        self.assertIsNone(sala.live_entry_url(self.alfa))
        self.assertContains(app.get(reverse("app_home")), "appena la regia attiva")
        self.assertRedirects(app.get(reverse("app_sala_enter")), reverse("app_home"), fetch_redirect_response=False)

    def test_results_clear_the_address(self):
        copy = sala.connect(SITE, self.key)
        sala.publish_live(self.TUNNEL, wait=True)
        sala.send_results(copy)
        self.assertIsNone(sala.live_entry_url(self.alfa))
        sala.publish_live(self.TUNNEL, wait=True)    # già rimandata: niente da pubblicare
        self.league.refresh_from_db()
        self.assertIsNone(sala.live_info(self.league))

    def test_site_refuses_bad_addresses_and_old_locks(self):
        sala.connect(SITE, self.key)
        self.league.refresh_from_db()
        lock_id = sala.lock_info(self.league)["id"]
        with self.assertRaisesMessage(sala.SalaError, "https://"):
            sala.set_live(self.league, lock_id, "http://in-chiaro.example", {})
        with self.assertRaisesMessage(sala.SalaError, "non è più valido"):
            sala.set_live(self.league, "vecchio", self.TUNNEL, {})
        # Squadre di altre leghe non entrano nella mappa.
        sala.set_live(self.league, lock_id, self.TUNNEL, {str(self.alfa.id): "a", "999999": "x", "nonnum": "y"})
        self.league.refresh_from_db()
        self.assertEqual(sala.live_info(self.league)["teams"], {str(self.alfa.id): "a"})


class TunnelRestartTests(TestCase):
    """Il tunnel che cade si riapre da solo, finché la regia lo vuole aperto."""

    class _Proc:
        def __init__(self, lines):
            import io
            self.stderr = io.BytesIO("".join(lines).encode())

        def poll(self):
            return 0

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()
        self.addCleanup(remote.stop)
        self.heard = []
        remote.on_public_url(self.heard.append)
        self.addCleanup(lambda: remote._LISTENERS.remove(self.heard.append))

    def _run(self, wanted=True):
        proc = self._Proc(["INF |  https://abc-def.trycloudflare.com  |\n"])
        self.remote._PROC = proc
        self.remote._set(status="starting", wanted=wanted, port=8000, attempt=0)
        with mock.patch.object(self.remote, "_restart_later") as again, \
                mock.patch.object(self.remote, "_harden"), mock.patch.object(self.remote, "_unharden"):
            self.remote._watch(proc)
        return again

    def test_a_dead_tunnel_comes_back_by_itself(self):
        again = self._run()
        self.assertEqual(self.heard, ["https://abc-def.trycloudflare.com", None])
        again.assert_called_once_with(8000, 0)
        self.assertIn("lo riapro da solo", self.remote.status()["error"])

    def test_no_restart_after_disattiva(self):
        again = self._run(wanted=False)
        again.assert_not_called()


class LiveSocketAccessTests(TransactionTestCase):
    """Con l'asta raggiungibile da internet la connessione in tempo reale
    accetta solo squadre dell'asta, chi gestisce la lega e il maxischermo."""

    def setUp(self):
        from .common import make_live_auction
        self.owner = User.objects.create_user("regista_ws", password="pw")
        self.league = League.objects.create(name="Lega WS", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa")
        self.auction = make_live_auction(league=self.league)

    def _connect(self, session=None, user=None):
        from asgiref.sync import async_to_sync
        from channels.routing import URLRouter
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser
        from ..routing import websocket_urlpatterns

        async def go():
            comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/auction/{self.auction.id}/")
            comm.scope["session"] = session or {}
            comm.scope["user"] = user or AnonymousUser()
            ok, _ = await comm.connect()
            await comm.disconnect()
            return ok
        return async_to_sync(go)()

    def test_on_the_lan_anyone_can_watch(self):
        with self.settings(PUBLIC_TOKENS_REQUIRED=False):
            self.assertTrue(self._connect())

    def test_from_the_internet_only_who_belongs(self):
        with self.settings(PUBLIC_TOKENS_REQUIRED=True):
            self.assertFalse(self._connect())                                         # sconosciuto
            self.assertTrue(self._connect(session={"participant_id": self.team.id}))  # squadra
            self.assertTrue(self._connect(user=self.owner))                           # regia
            self.assertTrue(self._connect(session={f"screen_ok_{self.auction.id}": True}))  # maxischermo

    def test_screen_page_marks_the_session(self):
        with self.settings(PUBLIC_TOKENS_REQUIRED=True):
            self.client.get(reverse("screen", args=[self.auction.id]) + f"?t={self.auction.public_token}")
            self.assertTrue(self.client.session.get(f"screen_ok_{self.auction.id}"))
