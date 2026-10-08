"""Le pagine della console aperte dall'app (Regia): stessa view, stesso
template, cambia solo la cornice (_frame_console.html / _frame_app.html).

Per ogni pagina: console e app rispondono, l'app ha la sua cornice e il
contenuto è lo stesso (i link fra pagine puntano alla console o all'app).
"""
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import League, Participant, Player
from ..views.common import APP_PAGES

# (pagina in console, pagina nell'app): quelle che usano la cornice condivisa.
FRAMED_PAGES = [
    ("admin_players", "app_regia_players"),
    ("admin_contracts", "app_regia_contracts"),
]


class AppPagesParityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente_ap", password="pw")
        self.stranger = User.objects.create_user("altro_ap", password="pw")
        self.league = League.objects.create(name="Lega Pagine", owner=self.owner)
        self.other = League.objects.create(name="Lega Altrui", owner=self.stranger)
        self.team = Participant.objects.create(league=self.league, display_name="Alfa Real",
                                               credits=Decimal("500"))
        Participant.objects.create(league=self.other, display_name="Beta United", credits=Decimal("500"))
        Player.objects.create(name="Lautaro", role="A", team="Inter", league=self.league,
                              owner=self.team, cost=Decimal("40"))
        Player.objects.create(name="Barella", role="C", team="Inter", league=self.other)

    @staticmethod
    def _page(html):
        part = html[html.index("<!-- page:start -->"):html.index("<!-- page:end -->")]
        return re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)

    @staticmethod
    def _as_console(html):
        """I link dell'app riportati a quelli della console, per il confronto."""
        for console_name, app_name in APP_PAGES.items():
            try:
                html = html.replace(f'"{reverse(app_name)}', f'"{reverse(console_name)}')
            except Exception:  # noqa: BLE001 — pagine con argomenti
                pass
        return html

    def test_same_page_in_console_and_app(self):
        self.client.force_login(self.owner)
        q = f"?league={self.league.id}"
        for console_name, app_name in FRAMED_PAGES:
            with self.subTest(page=console_name):
                console = self.client.get(reverse(console_name) + q)
                app = self.client.get(reverse(app_name) + q)
                self.assertEqual(console.status_code, 200)
                self.assertEqual(app.status_code, 200)
                self.assertIn("app-nav", app.content.decode())          # cornice dell'app
                self.assertNotIn("app-nav", console.content.decode())
                self.assertEqual(self._page(console.content.decode()),
                                 self._as_console(self._page(app.content.decode())))

    def test_app_pages_stay_in_the_app(self):
        """Dall'app i link verso pagine che l'app ha restano nell'app."""
        self.client.force_login(self.owner)
        html = self.client.get(reverse("app_regia_players") + f"?league={self.league.id}&q=lau").content.decode()  # con un filtro c'è «Azzera»
        self.assertIn(f'href="{reverse("app_regia_players")}?league={self.league.id}"', html)

    def test_only_your_leagues(self):
        self.client.force_login(self.stranger)
        for _console_name, app_name in FRAMED_PAGES:
            with self.subTest(page=app_name):
                resp = self.client.get(reverse(app_name) + f"?league={self.league.id}")
                self.assertNotContains(resp, "Lautaro", status_code=resp.status_code)

    def test_login_required(self):
        for _console_name, app_name in FRAMED_PAGES:
            with self.subTest(page=app_name):
                resp = self.client.get(reverse(app_name))
                self.assertEqual(resp.status_code, 302)

    def test_actions_return_to_the_page_they_came_from(self):
        """Un form della pagina nell'app torna all'app; senza next, alla console."""
        self.client.force_login(self.owner)
        back = reverse("app_regia_contracts") + f"?league={self.league.id}"
        data = {"league_id": self.league.id, "action": "settings", "faces": "1,2,3"}
        resp = self.client.post(reverse("admin_contracts_action"), {**data, "next": back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        # La pagina dell'app mostra il messaggio una volta sola.
        page = self.client.get(back).content.decode()
        self.assertEqual(page.count("Impostazioni contratti salvate."), 1)
        resp = self.client.post(reverse("admin_contracts_action"), {**data, "next": "https://evil.example/"})
        self.assertRedirects(resp, reverse("admin_contracts") + f"?league={self.league.id}",
                             fetch_redirect_response=False)
