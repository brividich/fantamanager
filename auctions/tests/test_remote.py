import asyncio
import json
import io
import re
import sqlite3
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.conf import settings
from django.test import (RequestFactory, TestCase, TransactionTestCase,
                         override_settings)
from django.utils import timezone

from .. import mantra, services
from ..providers import importers
from ..views import participant_join_url
from ..routing import websocket_urlpatterns
from ..models import (Auction, AuctionCycleResult, AuctionQueueItem, Bid, Formation,
                     League, Participant, Player, RosterLog, SealedBid, Watch)
from .common import make_live_auction

class RemoteAccessTests(TestCase):
    """Internet exposure: the tunnel state machine and the regia PIN gate.

    No tunnel is actually opened (that would dial out to Cloudflare); the module
    state is set directly, which is exactly what the views and the gate read.
    """

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()          # known-clean state, whatever a previous test left
        self.addCleanup(remote.stop)
        # The console always needs the login; through the tunnel the PIN comes on top.
        self.client.force_login(User.objects.create_superuser("admin", "a@b.c", "pw"))
        self.league = League.objects.create(name="L", budget=Decimal("500"))
        self.p = Participant.objects.create(display_name="Alfa", league=self.league,
                                            credits=Decimal("500"))

    def _open(self, host="abc-def.trycloudflare.com", pin="123456"):
        """Pretend cloudflared answered with a public URL."""
        url = f"https://{host}"
        self.remote._harden(host, url)
        self.remote._set(status="on", url=url, host=host, pin=pin, error="", detail="")

    # --- state -------------------------------------------------------------

    def test_status_is_off_and_json_safe_by_default(self):
        s = self.remote.status()
        self.assertEqual(s["status"], "off")
        self.assertEqual(s["url"], "")
        json.dumps(s)          # the dashboard embeds this

    def test_opening_hardens_the_settings_that_matter(self):
        self._open()
        self.assertTrue(settings.PUBLIC_TOKENS_REQUIRED)      # no anonymous joins
        self.assertIn("https://abc-def.trycloudflare.com", settings.CSRF_TRUSTED_ORIGINS)
        self.assertEqual(settings.SECURE_PROXY_SSL_HEADER,
                         ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_stopping_reverts_every_hardened_setting(self):
        before = (settings.PUBLIC_TOKENS_REQUIRED,
                  list(settings.CSRF_TRUSTED_ORIGINS),
                  getattr(settings, "SECURE_PROXY_SSL_HEADER", None))
        self._open()
        self.remote.stop()
        self.assertEqual(settings.PUBLIC_TOKENS_REQUIRED, before[0])
        self.assertEqual(list(settings.CSRF_TRUSTED_ORIGINS), before[1])
        self.assertEqual(getattr(settings, "SECURE_PROXY_SSL_HEADER", None), before[2])
        self.assertEqual(self.remote.status()["status"], "off")

    # --- the gate ----------------------------------------------------------

    def test_console_stays_open_on_the_lan(self):
        """No tunnel, no PIN: on the LAN the login is enough."""
        r = self.client.get("/admin-auction/")
        self.assertEqual(r.status_code, 200)

    def test_console_is_locked_through_the_public_host(self):
        self._open()
        r = self.client.get("/admin-auction/", HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/regia/unlock/", r["Location"])

    def test_console_still_open_on_the_lan_while_the_tunnel_runs(self):
        """Publishing must not lock the operator out of their own machine."""
        self._open()
        r = self.client.get("/admin-auction/", HTTP_HOST="localhost")
        self.assertEqual(r.status_code, 200)

    def test_admin_post_through_the_tunnel_is_refused_not_redirected(self):
        self._open()
        auction = make_live_auction(league=self.league)
        r = self.client.post(f"/admin-auction/{auction.id}/close/",
                             HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "regia_locked")

    def test_correct_pin_unlocks_and_returns_to_the_target(self):
        self._open(pin="424242")
        r = self.client.post("/regia/unlock/", {"pin": "424242", "next": "/admin-auction/"},
                             HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/admin-auction/")
        r = self.client.get("/admin-auction/", HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 200)

    def test_wrong_pin_does_not_unlock(self):
        self._open(pin="424242")
        r = self.client.post("/regia/unlock/", {"pin": "000000"},
                             HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/admin-auction/", HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 302)

    def test_pin_attempts_are_throttled(self):
        self._open(pin="424242")
        for _ in range(5):
            self.client.post("/regia/unlock/", {"pin": "000000"},
                             HTTP_HOST="abc-def.trycloudflare.com")
        r = self.client.post("/regia/unlock/", {"pin": "424242"},
                             HTTP_HOST="abc-def.trycloudflare.com")
        self.assertNotEqual(r.status_code, 302)       # even the right PIN waits
        self.assertIn("Troppi tentativi", r.content.decode())

    def test_unlock_page_redirects_home_when_there_is_no_tunnel(self):
        r = self.client.get("/regia/unlock/")
        self.assertEqual(r.status_code, 302)

    def test_unlock_refuses_an_offsite_next_target(self):
        self._open(pin="424242")
        r = self.client.post("/regia/unlock/",
                             {"pin": "424242", "next": "https://evil.example/x"},
                             HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r["Location"], "/dashboard/")

    # --- join links --------------------------------------------------------

    def test_join_links_switch_to_the_public_address(self):
        """A LAN IP would be useless to someone who is not on this wifi."""
        r = self.client.get("/admin-auction/participants/")
        self.assertNotIn("trycloudflare.com", r.content.decode())
        self._open()
        r = self.client.get("/admin-auction/participants/")
        body = r.content.decode()
        # Senza un'asta il link della squadra è il suo invito (/invito/<token>/).
        self.assertIn(f"https://abc-def.trycloudflare.com/invito/{self.p.public_token}/", body)

    def test_participants_page_is_gated_through_the_tunnel_too(self):
        self._open()
        r = self.client.get("/admin-auction/participants/",
                            HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/regia/unlock/", r["Location"])

    def test_app_teams_page_is_gated_through_the_tunnel_too(self):
        """The app's Squadre shows the same join links and accounts."""
        self._open()
        r = self.client.get(f"/app/regia/squadre/?league={self.league.id}",
                            HTTP_HOST="abc-def.trycloudflare.com")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/regia/unlock/", r["Location"])
        self.assertIn(f"league%3D{self.league.id}", r["Location"])     # back to the same league
        self.assertEqual(self.client.get(f"/app/regia/squadre/?league={self.league.id}").status_code, 200)


class RemoteErrorShieldTests(TestCase):
    """Tracebacks must not leave the building through the public URL."""

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()
        self.addCleanup(remote.stop)

    def test_shield_passes_local_requests_through(self):
        from ..middleware import RemoteErrorShield
        shield = RemoteErrorShield(lambda r: None)
        request = RequestFactory().get("/")
        self.assertIsNone(shield.process_exception(request, ValueError("boom")))

    def test_shield_hides_the_debug_404_page_from_the_tunnel(self):
        """Con DEBUG acceso (app desktop) il 404 di Django elenca tutti gli
        indirizzi del sito: dal tunnel esce una pagina semplice, in rete
        locale resta quella di debug."""
        host = "abc-def.trycloudflare.com"
        self.remote._harden(host, f"https://{host}")
        self.remote._set(status="on", url=f"https://{host}", host=host, pin="123456")
        with override_settings(DEBUG=True):
            remote = self.client.get("/non-esiste/", HTTP_HOST=host)
            local = self.client.get("/non-esiste/", HTTP_HOST="192.168.1.10:8000")
        self.assertEqual(remote.status_code, 404)
        self.assertContains(remote, "Pagina non trovata", status_code=404)
        self.assertNotContains(remote, "dashboard/", status_code=404)
        self.assertEqual(local.status_code, 404)
        self.assertContains(local, "dashboard/", status_code=404)

    def test_shield_leaves_404s_alone_without_debug(self):
        host = "abc-def.trycloudflare.com"
        self.remote._harden(host, f"https://{host}")
        self.remote._set(status="on", url=f"https://{host}", host=host, pin="123456")
        resp = self.client.get("/non-esiste/", HTTP_HOST=host)
        self.assertEqual(resp.status_code, 404)

    def test_shield_returns_a_generic_500_for_tunnel_requests(self):
        from ..middleware import RemoteErrorShield
        host = "abc-def.trycloudflare.com"
        self.remote._set(status="on", url=f"https://{host}", host=host)
        shield = RemoteErrorShield(lambda r: None)
        request = RequestFactory().get("/", HTTP_HOST=host)
        response = shield.process_exception(request, ValueError("secret in traceback"))
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertNotIn("secret in traceback", body)
        self.assertIn("Qualcosa è andato storto", body)


class RegiaPinThrottleTests(TestCase):
    """The 5-tries-then-60s lockout on the tunnel's PIN gate must not be
    resettable for free by simply not sending the session cookie back."""

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()
        self.addCleanup(remote.stop)
        self.host = "abc-def.trycloudflare.com"
        self.remote._set(status="on", url=f"https://{self.host}", host=self.host,
                         pin="123456")

    def _attempt(self, pin, fresh_session=True):
        # A brand-new client each time == a request with no session cookie at
        # all, exactly what an attacker would do to dodge a session-based
        # counter.
        client = self.client_class() if fresh_session else self.client
        return client.post("/regia/unlock/", {"pin": pin}, HTTP_HOST=self.host)

    def test_five_wrong_pins_from_five_different_sessions_still_locks_out(self):
        for _ in range(5):
            r = self._attempt("000000")
            self.assertEqual(r.status_code, 401)

        # A 6th attempt, again from a brand-new session, must still be locked
        # out — even with the *correct* PIN.
        r = self._attempt("123456")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Troppi tentativi", r.content.decode())

    def test_correct_pin_still_works_before_any_lockout(self):
        r = self._attempt("123456")
        self.assertEqual(r.status_code, 302)

    def test_success_clears_the_failure_count(self):
        for _ in range(4):
            self._attempt("000000")
        r = self._attempt("123456")
        self.assertEqual(r.status_code, 302)
        # The next wrong guess starts counting from zero again, not from 4.
        r = self._attempt("000000")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("Troppi tentativi", r.content.decode())


class PortalPinGateTests(TestCase):
    """The PIN box on /portal/ sets the same ``regia_unlocked`` flag the tunnel
    gate checks, so it must be exactly as strict as /regia/unlock/: only the
    minted PIN, the shared lockout, and the PIN never printed on the page."""

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()
        self.addCleanup(remote.stop)
        self.addCleanup(remote._set, pin="")
        self.host = "abc-def.trycloudflare.com"

    def _mint(self, pin):
        self.remote._set(status="on", url=f"https://{self.host}", host=self.host, pin=pin)

    def _post(self, pin, **extra):
        return self.client.post("/portal/", {"pin": pin, **extra})

    def test_admin_and_123456_are_not_backdoors(self):
        self._mint("424242")
        for pin in ("admin", "ADMIN", "123456"):
            with self.subTest(pin=pin):
                r = self._post(pin)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(self.client.session.get("regia_unlocked"))
                self.assertContains(r, "PIN errato")
                self.assertNotContains(r, "424242")

    def test_nothing_unlocks_before_a_tunnel_mints_a_pin(self):
        self.remote._set(pin="")
        for pin in ("admin", "123456", ""):
            with self.subTest(pin=pin):
                self._post(pin)
                self.assertFalse(self.client.session.get("regia_unlocked"))

    def test_the_minted_pin_unlocks(self):
        self._mint("424242")
        r = self._post("424242")
        self.assertRedirects(r, "/dashboard/", fetch_redirect_response=False)
        self.assertTrue(self.client.session.get("regia_unlocked"))

        r = self.client.post("/portal/", {"action": "logout"})
        self.assertRedirects(r, "/portal/", fetch_redirect_response=False)
        self.assertFalse(self.client.session.get("regia_unlocked"))

    def test_123456_works_only_when_it_is_the_minted_pin(self):
        self._mint("123456")
        r = self._post("123456")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.client.session.get("regia_unlocked"))

    def test_the_page_never_shows_the_pin(self):
        self._mint("424242")
        r = self.client.get("/portal/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "424242")
        self.assertNotContains(r, "PIN predefinito")
        self.assertNotIn("expected_pin", r.context)

    def test_the_form_posts_back_to_the_portal(self):
        r = self.client.get("/portal/")
        self.assertContains(r, 'action="/portal/"')

    def test_shares_the_lockout_with_the_tunnel_gate(self):
        self._mint("424242")
        for _ in range(5):
            self._post("000000")
        r = self._post("424242")
        self.assertContains(r, "Troppi tentativi")
        self.assertFalse(self.client.session.get("regia_unlocked"))
        # ...and the lockout it tripped holds on /regia/unlock/ as well.
        r = self.client.post("/regia/unlock/", {"pin": "424242"}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 401)
        self.assertIn("Troppi tentativi", r.content.decode())


class LocalAddressTests(TestCase):
    """Links handed to other people must not point at 'localhost'."""

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()
        self.addCleanup(remote.stop)
        self.client.force_login(User.objects.create_superuser("admin", "a@b.c", "pw"))
        self.league = League.objects.create(name="L", budget=Decimal("500"))
        self.p = Participant.objects.create(display_name="Alfa", league=self.league,
                                            credits=Decimal("500"))

    def test_lan_url_replaces_loopback_but_keeps_the_port(self):
        request = RequestFactory().get("/", HTTP_HOST="localhost:8123")
        url = self.remote.lan_base_url(request)
        if not url:
            self.skipTest("nessun indirizzo di rete su questa macchina")
        self.assertTrue(url.startswith("http://"))
        self.assertTrue(url.endswith(":8123"))
        self.assertNotIn("localhost", url)

    def test_lan_url_is_empty_when_already_on_that_address(self):
        ip = self.remote.lan_ip()
        if not ip:
            self.skipTest("nessun indirizzo di rete su questa macchina")
        request = RequestFactory().get("/", HTTP_HOST=ip)
        self.assertEqual(self.remote.lan_base_url(request), "")

    def test_join_links_avoid_localhost(self):
        r = self.client.get("/admin-auction/participants/", HTTP_HOST="localhost:8123")
        body = r.content.decode()
        self.assertIn(self.p.public_token, body)
        self.assertNotIn("http://localhost:8123/join/", body)

    def test_tunnel_wins_over_the_lan_address(self):
        host = "abc-def.trycloudflare.com"
        self.remote._set(status="on", url=f"https://{host}", host=host)
        request = RequestFactory().get("/", HTTP_HOST="localhost:8123")
        self.assertEqual(self.remote.best_base_url(request), f"https://{host}")

    # --- the wifi twin of a public link ------------------------------------

    def _tunnelled_request(self, host="abc-def.trycloudflare.com", port=8123):
        self.remote._set(status="on", url=f"https://{host}", host=host, port=port)
        return RequestFactory().get("/", HTTP_HOST=host)

    def test_the_remote_page_shows_a_reachable_lan_address_through_the_tunnel(self):
        """Regression: it used to inherit the tunnel's https + missing port."""
        ip = self.remote.lan_ip()
        if not ip:
            self.skipTest("nessun indirizzo di rete su questa macchina")
        host = "abc-def.trycloudflare.com"
        self.remote._harden(host, f"https://{host}")
        self.remote._set(status="on", url=f"https://{host}", host=host, port=8123, pin="123456")
        session = self.client.session
        session["regia_unlocked"] = True
        session.save()
        resp = self.client.get("/admin-auction/remote/console/", HTTP_HOST=host)
        self.assertEqual(resp.context["lan_url"], f"http://{ip}:8123")
        self.assertNotIn(f"https://{ip}", resp.content.decode())

    def test_lan_url_through_the_tunnel_uses_the_local_port_and_http(self):
        ip = self.remote.lan_ip()
        if not ip:
            self.skipTest("nessun indirizzo di rete su questa macchina")
        request = self._tunnelled_request()
        self.assertEqual(self.remote.lan_url(request), f"http://{ip}:8123")

    def test_lan_join_url_is_offered_only_while_the_tunnel_is_open(self):
        from ..views import participant_lan_join_url
        ip = self.remote.lan_ip()
        if not ip:
            self.skipTest("nessun indirizzo di rete su questa macchina")

        # Tunnel off: the handed-out link is already the LAN one, nothing to add.
        local = RequestFactory().get("/", HTTP_HOST="localhost:8123")
        self.assertEqual(participant_lan_join_url(local, self.p), "")

        # Tunnel on: the same seat, reachable over the wifi, token included.
        request = self._tunnelled_request()
        url = participant_lan_join_url(request, self.p)
        self.assertTrue(url.startswith(f"http://{ip}:8123/invito/"))
        self.assertIn(self.p.public_token, url)

    def test_team_page_shows_both_doors_while_the_tunnel_is_open(self):
        ip = self.remote.lan_ip()
        if not ip:
            self.skipTest("nessun indirizzo di rete su questa macchina")
        host = "abc-def.trycloudflare.com"
        self.remote._harden(host, f"https://{host}")
        self.remote._set(status="on", url=f"https://{host}", host=host, port=8123,
                         pin="123456")
        session = self.client.session
        session["regia_unlocked"] = True
        session.save()
        url = f"/admin-auction/participants/?league={self.league.id}"
        # No auction running: one link only, the wifi twin is live-auction business.
        body = self.client.get(url, HTTP_HOST=host).content.decode()
        self.assertNotIn(f"http://{ip}:8123/join/", body)
        self.assertNotIn("net=lan", body)
        Auction.objects.create(league=self.league, title="Estiva", status=Auction.Status.LIVE)
        body = self.client.get(url, HTTP_HOST=host).content.decode()
        self.assertIn(f"http://{ip}:8123/join/", body)   # wifi
        self.assertIn(f"https://{host}/join/", body)     # internet
        self.assertIn("net=lan", body)                   # the wifi QR


@override_settings(DESKTOP_APP=True)
class QuitAppTests(TestCase):
    """The in-app quit button — the only way to stop a macOS .app bundle."""

    def setUp(self):
        from .. import remote
        self.remote = remote
        remote.stop()
        self.addCleanup(remote.stop)
        self.client.force_login(User.objects.create_superuser("admin", "a@b.c", "pw"))
        self.calls = []
        self._real = remote.shutdown_process
        remote.shutdown_process = lambda *a, **k: self.calls.append(1)
        self.addCleanup(lambda: setattr(remote, "shutdown_process", self._real))

    def test_local_request_shuts_the_app_down(self):
        r = self.client.post("/admin-auction/quit/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(len(self.calls), 1)

    def test_quit_is_refused_through_the_tunnel(self):
        """Stopping the auction belongs to whoever sits at the host machine."""
        host = "abc-def.trycloudflare.com"
        self.remote._harden(host, f"https://{host}")
        self.remote._set(status="on", url=f"https://{host}", host=host, pin="123456")
        session = self.client.session
        session["regia_unlocked"] = True          # even an unlocked remote regia
        session.save()
        r = self.client.post("/admin-auction/quit/", HTTP_HOST=host)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "local_only")
        self.assertEqual(self.calls, [])

    def test_get_is_not_enough(self):
        r = self.client.get("/admin-auction/quit/")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(self.calls, [])

