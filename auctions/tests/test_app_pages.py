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
    ("admin_season", "app_regia_season"),
    ("admin_fantapazz", "app_regia_import"),
    ("admin_export", "app_regia_export"),
    ("admin_config", "app_regia_config"),
    ("admin_competitions", "app_regia_competitions"),
    ("admin_market_dashboard", "app_regia_market"),
    ("admin_market_buste", "app_regia_buste"),
    ("admin_market_repair", "app_regia_market_auction"),
    ("admin_market_moves", "app_regia_moves"),
    ("admin_auction_wizard", "app_regia_auction_wizard"),
    # Il mio account (privacy): per chi gestisce leghe la console, nell'app l'app.
    ("account", "app_account"),
    # «Nuova lega»: il wizard, unica strada per creare una lega (anche nell'app).
    ("admin_setup", "app_regia_setup"),
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
        """Il contenuto della pagina, con i link fra pagine in forma canonica
        (``PAGE:nome``, che sia l'indirizzo della console o dell'app) e senza
        i campi che dicono da dove parte il form (next, from dei wizard)."""
        part = html[html.index("<!-- page:start -->"):html.index("<!-- page:end -->")]
        part = re.sub(r'name="(next|from|csrfmiddlewaretoken)" value="[^"]*"', "", part)
        for console_name, app_name in APP_PAGES.items():
            try:
                urls = (reverse(console_name), reverse(app_name))
            except Exception:  # noqa: BLE001 — pagine con argomenti
                continue
            for url in urls:
                part = re.sub('"' + re.escape(url) + r'(?=["?#])', f'"PAGE:{console_name}', part)
        return part

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
                self.assertEqual(self._page(console.content.decode()), self._page(app.content.decode()))

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

    def test_config_returns_to_its_page_and_tab(self):
        """Impostazioni: torna alla pagina da cui parte (console o app), sulla
        scheda del form; un next fuori dal sito vale come nessun next."""
        self.client.force_login(self.owner)
        data = {"action": "delete_auction", "auction_id": "999999", "tab": "manut"}
        for nxt, base in ((reverse("app_regia_config") + f"?league={self.league.id}", reverse("app_regia_config")),
                          ("https://evil.example/x", reverse("admin_config")),
                          ("", reverse("admin_config"))):
            with self.subTest(next=nxt):
                resp = self.client.post(reverse("admin_config_action"), {**data, "next": nxt})
                self.assertEqual(resp["Location"], base + "#manut")

    def test_competition_actions_return_to_the_app(self):
        from ..models import Competition, Season
        self.client.force_login(self.owner)
        season = Season.objects.create(league=self.league, name="2026/27", is_current=True)
        comp = Competition.objects.create(season=season, name="Coppa", kind=Competition.Type.KNOCKOUT)
        back = reverse("app_regia_competitions") + f"?league={self.league.id}&comp={comp.id}"
        resp = self.client.post(reverse("admin_competition_delete", args=[comp.id]), {"next": back})
        self.assertEqual(resp["Location"], reverse("app_regia_competitions") + f"?league={self.league.id}")
        self.assertFalse(Competition.objects.filter(pk=comp.id).exists())
