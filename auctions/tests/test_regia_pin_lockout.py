"""Il PIN della regia: chi sbaglia si blocca da solo, non blocca tutti.

Prima i tentativi falliti erano contati per l'intero processo: cinque PIN
sbagliati da un telefono qualsiasi (anche di un estraneo che ha il link del
tunnel) chiudevano la regia a tutti per un minuto, all'infinito. Ora si
contano per indirizzo (``CF-Connecting-IP``, che cloudflared passa dal bordo
di Cloudflare) e per sessione; un tetto globale più alto resta come ultima
difesa contro chi prova da tanti indirizzi.
"""
from django.test import TestCase

from .. import remote


class RegiaPinPerClientTests(TestCase):
    HOST = "abc-def.trycloudflare.com"

    def setUp(self):
        remote.stop()
        self.addCleanup(remote.stop)
        remote._set(status="on", url=f"https://{self.HOST}", host=self.HOST, pin="424242")

    def _attempt(self, pin, ip, client=None):
        client = client or self.client_class()
        return client.post("/regia/unlock/", {"pin": pin}, HTTP_HOST=self.HOST,
                           HTTP_CF_CONNECTING_IP=ip)

    def test_one_client_failing_does_not_lock_out_another(self):
        for _ in range(5):
            self.assertEqual(self._attempt("000000", "203.0.113.5").status_code, 401)
        locked = self._attempt("424242", "203.0.113.5")
        self.assertIn("Troppi tentativi", locked.content.decode())
        # Un altro telefono, col PIN giusto, entra.
        other = self.client_class()
        self.assertEqual(self._attempt("424242", "198.51.100.7", other).status_code, 302)
        self.assertTrue(other.session.get("regia_unlocked"))

    def test_a_new_session_from_the_same_address_stays_locked(self):
        for _ in range(5):
            self._attempt("000000", "203.0.113.5")
        self.assertIn("Troppi tentativi", self._attempt("424242", "203.0.113.5").content.decode())

    def test_the_global_ceiling_still_holds(self):
        for n in range(remote.PIN_GLOBAL_TRIES):
            self._attempt("000000", f"203.0.113.{n % 250}" if n < 250 else f"198.51.100.{n % 250}")
        r = self._attempt("424242", "192.0.2.77")
        self.assertIn("Troppi tentativi", r.content.decode())

    def test_new_pins_have_eight_digits(self):
        remote.stop()
        remote._set(pin="")
        with remote._LOCK:
            remote._STATE["pin"] = ""
        pin = remote.new_regia_pin()
        self.assertEqual(len(pin), 8)
        self.assertTrue(pin.isdigit())
