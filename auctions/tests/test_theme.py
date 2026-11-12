"""Tema chiaro / scuro.

Il tema scuro è quello di partenza; html.light-theme (applicato da fmTheme in
base.html, prima del primo disegno) ridefinisce gli stessi token in
static/css/console.css. Questi test tengono insieme le tre cose che lo fanno
funzionare: ogni token ha la sua versione chiara, le pagine non scrivono
veli bianchi fissi, e la scelta «Tema» è la stessa in console e app.
"""
import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ..models import League, Participant

ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "static" / "css" / "console.css"
TEMPLATES = ROOT / "auctions" / "templates"
# Fuori dal tema: le stampe (fondo bianco loro), le email, le pagine d'errore
# autonome, il maxischermo (resta scuro) e il campo della formazione (erba).
OUTSIDE = re.compile(r"(^|/)(print_[^/]*|_print_base|email/[^/]*|errors/[^/]*|screen|_formation_pitch)\.html$")


def _block(css, head):
    start = css.index(head)
    return css[start:css.index("\n}\n", start)]


class ThemeTokensTests(SimpleTestCase):
    def test_every_token_has_a_light_value(self):
        css = CSS.read_text()
        dark = set(re.findall(r"(--fm-[\w-]+)\s*:", _block(css, ":root {")))
        light = set(re.findall(r"(--fm-[\w-]+)\s*:", _block(css, "html.light-theme {")))
        layout = {"--fm-display", "--fm-sans", "--fm-radius-sm", "--fm-radius-md", "--fm-radius-lg"}
        self.assertEqual(sorted(dark - light - layout), [],
                         "Un token nuovo in :root vuole anche il valore chiaro in html.light-theme")
        self.assertEqual(sorted(light - dark), [], "Token chiaro senza il valore scuro in :root")

    def test_no_fixed_white_veil_in_pages(self):
        """Un velo bianco fisso sparisce sul tema chiaro: rgba(var(--fm-tint-rgb), a)."""
        bad = []
        for path in TEMPLATES.rglob("*.html"):
            rel = str(path.relative_to(TEMPLATES))
            if OUTSIDE.search(rel):
                continue
            text = path.read_text()
            for m in re.finditer(r"background(-color)?\s*:[^;\"]*rgba\(\s*255\s*,\s*255\s*,\s*255", text):
                rule = text.rfind("{", 0, m.start())
                selector = text[max(text.rfind("}", 0, rule), text.rfind("\n\n", 0, rule)) + 1:rule]
                if "light-theme" in selector:
                    continue  # regole scritte apposta per il tema chiaro
                bad.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
        self.assertEqual(bad, [])

    def test_no_undefined_white_fallbacks(self):
        """var(--non-esiste, #fff) è bianco fisso: usare var(--fm-strong)."""
        bad = []
        for path in TEMPLATES.rglob("*.html"):
            rel = str(path.relative_to(TEMPLATES))
            if OUTSIDE.search(rel):
                continue
            if re.search(r"var\(--(ink-bright|text)\s*,", path.read_text()):
                bad.append(rel)
        self.assertEqual(bad, [])


class ThemeSwitchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("tema_admin", password="pw")
        self.league = League.objects.create(name="Lega Tema", owner=self.owner)

    def test_theme_applied_before_first_paint(self):
        html = self.client.get(reverse("login")).content.decode()
        head = html[:html.index("<body")]
        self.assertIn("window.fmTheme", head)
        self.assertIn('id="fm-theme-color"', head)

    def test_same_choice_in_console_and_app(self):
        self.client.force_login(self.owner)
        q = f"?league={self.league.id}"
        for name in ("account", "app_account"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name) + q).content.decode()
                for value in ("dark", "light", "auto"):
                    self.assertIn(f'data-fm-theme="{value}"', html)
        console = self.client.get(reverse("dashboard_league", args=[self.league.id])).content.decode()
        self.assertIn("data-fm-theme-toggle", console)   # scorciatoia nella barra della console

    def test_app_altro_and_menu(self):
        self.client.force_login(self.owner)
        team = Participant.objects.create(league=self.league, display_name="Tema FC", user=self.owner)
        session = self.client.session
        session["participant_id"] = team.id
        session.save()
        html = self.client.get(reverse("app_altro")).content.decode()
        self.assertIn('data-fm-theme="auto"', html)
        self.assertIn("data-fm-theme-toggle", html)        # menu laterale dell'app

    def test_big_screen_stays_dark(self):
        src = (TEMPLATES / "auctions" / "screen.html").read_text()
        self.assertIn('data-theme-fixed="dark"', src)
