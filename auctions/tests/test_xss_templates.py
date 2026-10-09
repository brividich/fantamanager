"""Nomi di squadre e giocatori non diventano codice nel browser.

Un presidente chiama una squadra (o un giocatore, via import del listone)
``<img src=x onerror=…>``; quando un superuser apre la regia o il
maxischermo, lo script girerebbe nella sua sessione. Due difese:

- le pagine renderizzate dal server non contengono mai il markup grezzo
  (autoescape di Django, ``json_script`` per lo stato iniziale);
- nel JavaScript ogni dato del server che finisce in ``innerHTML`` /
  ``insertAdjacentHTML`` / template string HTML passa da ``fmEsc()``
  (``base.html``), l'unica funzione di escape: un controllo statico su tutti i
  template lo verifica.
"""
import json
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase

from .. import services
from ..models import League, Participant, Player
from .common import make_live_auction

EVIL = "<img src=x onerror=alert(1)>"
TEMPLATES = Path(settings.BASE_DIR, "auctions/templates/auctions")


class LivePagesRenderEscapedTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.local", "pw-root-123")
        self.league = League.objects.create(name=EVIL, owner=self.root, budget=Decimal("1000"))
        self.team = Participant.objects.create(display_name=EVIL, league=self.league,
                                               credits=Decimal("1000"))
        self.player = Player.objects.create(name=EVIL, role="A", team=EVIL, league=self.league,
                                            initial_price=Decimal("10"))
        self.auction = make_live_auction(
            league=self.league, player=self.player, current_price=Decimal("10"),
            min_increment=Decimal("1"), quick_increments="1,5,10", enforce_limits=False,
        )
        self.assertTrue(services.place_bid(self.auction.id, self.team.id, "5").accepted)
        self.client.force_login(self.root)
        session = self.client.session
        session["participant_id"] = self.team.id
        session.save()

    def test_regia_screen_and_bid_page_have_no_raw_markup(self):
        for url in (f"/dashboard/regia/{self.auction.id}/", f"/screen/{self.auction.id}/",
                    f"/bid/{self.auction.id}/"):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                # Escapato («&lt;img», «\\u003Cimg») resta testo: grezzo sarebbe un tag.
                self.assertNotIn("<img src=x", html)
                # Lo stato iniziale c'è, con i nomi intatti ma come dati JSON.
                m = re.search(r'<script id="fm-state-data" type="application/json">(.*?)</script>',
                              html, re.S)
                self.assertIsNotNone(m)
                self.assertNotIn("<", m.group(1))
                state = json.loads(m.group(1))
                self.assertEqual(state["player"]["name"], EVIL)
                self.assertEqual(state["player"]["team"], EVIL)
                self.assertEqual(state["best_bidder"], EVIL)


# Un dato del server in una template string HTML: ${…name…}, ${…team…},
# ${…display_name…}, ${…participant…}, ${…best_bidder…}. Va avvolto da fmEsc(.
_RISKY = re.compile(r"\$\{([^{}]*\b(?:name|team|display_name|participant|best_bidder|username|label)\b[^{}]*)\}")
# Concatenazione dentro un assegnamento a innerHTML: "…" + x.name + "…"
_RISKY_CONCAT = re.compile(r"\+\s*([\w.$\[\]'\"]*\.(?:name|team|display_name|participant|best_bidder)\b)")


def _statement_end(src, i):
    """Index where the JS statement starting at ``i`` ends: the first ``;`` (or
    unmatched closing bracket) outside strings, template literals and their
    ``${…}``. A tiny tokenizer, enough for the templates' inline scripts."""
    stack = ["code"]          # code | tpl | expr
    depth = [0]               # bracket depth per code/expr level
    while i < len(src):
        ch, top = src[i], stack[-1]
        if top == "tpl":
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                stack.pop()
            elif src.startswith("${", i):
                stack.append("expr")
                depth.append(0)
                i += 2
                continue
            i += 1
            continue
        if ch in "'\"":
            j = i + 1
            while j < len(src) and src[j] != ch and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = len(src) if j < 0 else j
            continue
        if ch == "`":
            stack.append("tpl")
        elif ch in "([{":
            depth[-1] += 1
        elif ch in ")]}":
            if ch == "}" and top == "expr" and depth[-1] == 0:
                stack.pop()
                depth.pop()
            else:
                depth[-1] -= 1
                if depth[-1] < 0 and top == "code":
                    return i
        elif ch == ";" and top == "code" and depth[-1] == 0:
            return i
        i += 1
    return i


def _html_sinks(src):
    """The JS statements that write HTML: from ``innerHTML =`` /
    ``insertAdjacentHTML(`` up to the end of the statement."""
    for m in re.finditer(r"(?:\.innerHTML\s*\+?=|\.outerHTML\s*=|insertAdjacentHTML\s*\()", src):
        start = m.end() - (1 if m.group(0).endswith("(") else 0)
        if m.group(0).endswith("("):
            # insertAdjacentHTML(...): up to its closing parenthesis.
            yield src.count("\n", 0, m.start()) + 1, src[m.start():_statement_end(src, m.end())]
        else:
            yield src.count("\n", 0, m.start()) + 1, src[m.start():_statement_end(src, start)]


def _risky_fragments(src):
    """Template-string pieces with a name-like field not wrapped in fmEsc(.

    Semplice di proposito: guarda ogni ``${…}`` che nomina un campo da nome
    (name, team, display_name, participant, best_bidder, username, label) e
    ogni ``+ x.name`` dentro un'istruzione che scrive HTML. Va bene se dentro
    c'è ``fmEsc(``; per i campi che non sono dati (un'etichetta scritta nel
    template) si usa ``fmEsc`` lo stesso: costa nulla.
    """
    bad = []
    for line, stmt in _html_sinks(src):
        for m in _RISKY.finditer(stmt):
            if "fmEsc(" not in m.group(1):
                bad.append((line, m.group(0)))
        for m in _RISKY_CONCAT.finditer(stmt):
            before = stmt[max(0, m.start() - 6):m.start()]
            if "fmEsc(" not in before:
                bad.append((line, m.group(0)))
    return bad


class InnerHtmlStaticTests(TestCase):
    """Regressione statica su ogni template che scrive HTML da JavaScript."""

    def _templates(self):
        files = [p for p in TEMPLATES.rglob("*.html")
                 if re.search(r"innerHTML|insertAdjacentHTML|outerHTML", p.read_text(encoding="utf-8"))]
        self.assertGreaterEqual(len(files), 20)     # il controllo guarda davvero i template
        return files

    def test_names_in_html_strings_go_through_fmEsc(self):
        problems = []
        for path in self._templates():
            for line, frag in _risky_fragments(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(TEMPLATES)}:{line}: {frag}")
        self.assertEqual(problems, [], "Dati del server in HTML senza fmEsc():\n" + "\n".join(problems))

    def test_one_escape_function(self):
        """fmEsc() in base.html è l'unico escape: niente copie locali (esc, escapeHtml…)."""
        self.assertIn("function fmEsc(s)", (TEMPLATES / "base.html").read_text(encoding="utf-8"))
        local = re.compile(r"(?:function\s+|(?:const|let|var)\s+)(esc|escapeHtml|callEsc|dashEsc|escHtml)\b")
        found = [f"{p.relative_to(TEMPLATES)}: {m.group(1)}"
                 for p in TEMPLATES.rglob("*.html")
                 for m in local.finditer(p.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])

    def test_role_classes_come_from_the_whitelist(self):
        """role-${…} solo con fmRole(): un ruolo importato non diventa una classe qualsiasi."""
        bad = []
        for p in TEMPLATES.rglob("*.html"):
            for m in re.finditer(r"role-\$\{([^}]*)\}", p.read_text(encoding="utf-8")):
                if not m.group(1).strip().startswith("fmRole("):
                    bad.append(f"{p.relative_to(TEMPLATES)}: {m.group(0)}")
        self.assertEqual(bad, [])

    def test_the_checker_catches_a_raw_name(self):
        """Il controllo stesso: un nome grezzo lo trova, uno escapato no."""
        self.assertTrue(_risky_fragments("el.innerHTML = `<b>${s.player.name}</b>`;"))
        self.assertTrue(_risky_fragments("el.innerHTML = '<b>' + bid.participant + '</b>';"))
        self.assertFalse(_risky_fragments("el.innerHTML = `<b>${fmEsc(s.player.name)}</b>`;"))
        self.assertFalse(_risky_fragments("el.textContent = `${s.player.name}`;"))


class ContentSecurityPolicyTests(TestCase):
    """Seconda difesa: una CSP minima su ogni pagina (gli script in linea restano)."""

    def test_every_page_has_the_policy(self):
        for url in ("/", "/login/", "/app/login/"):
            with self.subTest(url=url):
                csp = self.client.get(url)["Content-Security-Policy"]
                for part in ("object-src 'none'", "base-uri 'self'", "form-action 'self'",
                             "frame-ancestors 'none'"):
                    self.assertIn(part, csp)

    def test_a_response_with_its_own_policy_keeps_it(self):
        from django.http import HttpResponse
        from ..middleware import ContentSecurityPolicy

        def view(request):
            resp = HttpResponse("x")
            resp["Content-Security-Policy"] = "sandbox"
            return resp
        resp = ContentSecurityPolicy(view)(None)
        self.assertEqual(resp["Content-Security-Policy"], "sandbox")
