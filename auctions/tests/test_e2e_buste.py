"""Prova completa del mercato a buste, dall'inizio alla fine, come la farebbe
una lega vera: l'admin crea la lega col wizard importando listone e rose, i
manager entrano dal link d'invito, l'admin apre le buste con la sostituzione
dopo lo spoglio; ogni squadra dichiara chi lascerebbe andare e consegna le
buste dall'app; la busta chiude da sola, l'admin fa lo spoglio; chi ha vinto
conferma chi esce (dadi subito o dopo) e chi non lo fa in tempo si vede
applicata la sua lista. Alla fine rose, crediti, registro e verbale tornano.

Ogni passo passa dagli stessi indirizzi che usano le pagine (console e app),
non dai servizi: se una pagina o un modulo si rompe, la prova lo dice."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import League, MarketBid, MarketSession, Participant, Player, RosterLog
from ..services import scheduler

# Listone (Id, Nome, Ruolo, Squadra, Quotazione) e rose nel formato di Leghe.
LISTONE = """Id,Nome,R,Squadra,Qt.A
101,Portiere Alfa,P,Roma,5
102,Difensore Alfa,D,Roma,5
103,Centro Alfa Uno,C,Roma,8
104,Centro Alfa Due,C,Lazio,6
105,Punta Alfa,A,Lazio,10
201,Portiere Beta,P,Milan,5
202,Difensore Beta,D,Milan,5
203,Centro Beta Uno,C,Milan,8
204,Centro Beta Due,C,Inter,6
205,Punta Beta,A,Inter,10
301,Portiere Gamma,P,Napoli,5
302,Difensore Gamma,D,Napoli,5
303,Centro Gamma Uno,C,Napoli,8
304,Punta Gamma Uno,A,Torino,10
305,Punta Gamma Due,A,Torino,7
900,Boban,C,Genoa,20
901,Iniesta,C,Bologna,18
902,Raul,A,Como,15
"""
ROSE = """$,$,$
Alfa,101,10
Alfa,102,10
Alfa,103,30
Alfa,104,20
Alfa,105,40
$,$,$
Beta,201,10
Beta,202,10
Beta,203,30
Beta,204,20
Beta,205,40
$,$,$
Gamma,301,10
Gamma,302,10
Gamma,303,30
Gamma,304,40
Gamma,305,25
"""
PASSWORD = "Ombrello-Verde-77"


class BusteEndToEndTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("presidente", password="pw", email="pres@x.it")
        self.console = Client()
        self.console.force_login(self.admin)

    # --- passi -------------------------------------------------------------

    def _create_league(self):
        resp = self.console.post(reverse("admin_setup_create"), {
            "name": "Lega Prova Buste", "start_choice": "rose", "import_choice": "leghe", "source_site": "leghe",
            "budget": "500", "create_auction": "0", "contracts_enabled": "1",
            "listone_file": SimpleUploadedFile("Quotazioni.csv", LISTONE.encode(), content_type="text/csv"),
            "rose_file": SimpleUploadedFile("rose.csv", ROSE.encode(), content_type="text/csv"),
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, "content", b"")[:500])
        league = League.objects.get(name="Lega Prova Buste")
        self.assertTrue(league.contracts_enabled)
        self.assertEqual(set(Participant.objects.filter(league=league).values_list("display_name", flat=True)),
                         {"Alfa", "Beta", "Gamma"})
        return league

    def _join(self, team, username):
        """Il manager apre il suo link d'invito, crea l'account ed entra nell'app."""
        client = Client()
        resp = client.post(reverse("invite", args=[team.public_token]), {
            "action": "signup", "username": username, "email": f"{username}@x.it",
            "password": PASSWORD, "accept_terms": "1", "accept_age": "1"})
        self.assertRedirects(resp, reverse("app_home"), fetch_redirect_response=False)
        team.refresh_from_db()
        self.assertEqual(team.user.username, username)
        self.assertEqual(client.get(reverse("app_home")).status_code, 200)
        return client

    def _bid(self, client, session, player, amount, priority=1):
        return client.post(reverse("app_market_bid"), {
            "session_id": session.id, "player_id": player.id, "amount": amount, "priority": priority}).json()

    def _save_list(self, client, session, players):
        resp = client.post(reverse("app_market_release_list"),
                           {"session_id": session.id, "player_id": [p.id for p in players]})
        self.assertEqual(resp.status_code, 302)
        return client.get(resp["Location"])

    def _won(self, session, player):
        session.refresh_from_db()
        return next(w for w in session.results_summary["won"] if w["player_id"] == player.id)

    # --- la prova ------------------------------------------------------------

    def test_full_market(self):
        league = self._create_league()
        p = {pl.name: pl for pl in Player.objects.filter(league=league)}
        alfa, beta, gamma = (Participant.objects.get(league=league, display_name=n) for n in ("Alfa", "Beta", "Gamma"))
        self.assertEqual(p["Centro Alfa Uno"].owner, alfa)
        self.assertIsNone(p["Boban"].owner)
        # Il file di Leghe non porta i crediti rimasti: ogni squadra ha il budget del wizard.
        for t in (alfa, beta, gamma):
            self.assertEqual(t.credits, Decimal("500"), t.display_name)
        self.assertEqual(alfa.remaining_credits, Decimal("390"))
        start_roster = {t.id: Player.objects.filter(owner=t).count() for t in (alfa, beta, gamma)}
        start_spent = {t.id: Participant.objects.get(pk=t.pk).spent_credits for t in (alfa, beta, gamma)}

        # 1. I manager entrano dal link d'invito.
        app_alfa = self._join(alfa, "mario")
        app_beta = self._join(beta, "luigi")
        app_gamma = self._join(gamma, "anna")

        # 2. L'admin crea le buste: sostituzione dopo lo spoglio, 24 ore per confermare.
        closes = timezone.localtime(timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        resp = self.console.post(reverse("admin_market_create"), {
            "league_id": league.id, "market_kind": "buste", "title": "Riparazione Novembre", "open_timing": "now",
            "closes_at": closes, "require_same_role_release": "1", "release_timing": "after",
            "release_confirm_hours": "24", "refund_mode": "PURCHASE", "budget_rule": "priority",
            "tie_break": "manual"})
        self.assertEqual(resp.status_code, 302)
        session = MarketSession.objects.get(league=league, title="Riparazione Novembre")
        self.assertEqual(session.status, MarketSession.Status.OPEN)
        self.assertTrue(session.release_after_count)
        self.assertEqual(session.release_confirm_hours, 24)

        # 3. Senza lista la busta non parte, e il messaggio dice cosa fare.
        res = self._bid(app_alfa, session, p["Iniesta"], 20)
        self.assertEqual(res["error"], "release_list_short")
        self.assertIn("Chi lasceresti andare", res["message"])
        home = app_alfa.get(reverse("app_home"))
        self.assertContains(home, "prima compila «Chi lasceresti andare»")
        page = app_alfa.get(reverse("app_mercato") + f"?session_id={session.id}")
        self.assertContains(page, "Chi lasceresti andare")

        # 4. Alfa mette in lista i due centrocampisti e offre su due C.
        page = self._save_list(app_alfa, session, [p["Centro Alfa Due"], p["Centro Alfa Uno"]])
        self.assertContains(page, "Lista salvata: 2 giocatori")
        self.assertTrue(self._bid(app_alfa, session, p["Boban"], 30)["ok"])
        self.assertTrue(self._bid(app_alfa, session, p["Iniesta"], 20, priority=2)["ok"])
        # Beta: un C in lista, una busta su Boban (più alta).
        self._save_list(app_beta, session, [p["Centro Beta Uno"]])
        self.assertTrue(self._bid(app_beta, session, p["Boban"], 41)["ok"])
        # Una seconda busta su un altro C non torna con la lista.
        self.assertEqual(self._bid(app_beta, session, p["Iniesta"], 10)["error"], "release_list_short")

        # 5. Gamma non usa la lista dall'app: gliela compila l'admin, poi lui offre.
        url = reverse("admin_market_session", args=[session.id])
        page = self.console.get(url)
        self.assertContains(page, "Senza lista: <b>Gamma</b>")
        self.assertNotContains(page, "41 FM")                              # buste segrete anche per l'admin
        regia = self.console.get(reverse("app_regia") + f"?league={league.id}")
        self.assertContains(regia, "1 squadra senza «Chi lasceresti andare»")
        resp = self.console.post(reverse("admin_market_release_list", args=[session.id]), {
            "participant_id": gamma.id, "player_id": [p["Punta Gamma Due"].id, p["Punta Gamma Uno"].id],
            "next": url + f"?list_team={gamma.id}"}, follow=True)
        self.assertContains(resp, "Lista salvata: 2 giocatori che la squadra lascerebbe andare")
        self.assertTrue(self._bid(app_gamma, session, p["Raul"], 12)["ok"])

        # 6. La finestra si chiude da sola (scheduler).
        MarketSession.objects.filter(pk=session.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        scheduler.run_once()
        session.refresh_from_db()
        self.assertEqual(session.status, MarketSession.Status.CLOSED)
        self.assertEqual(self._bid(app_alfa, session, p["Raul"], 5)["ok"], False)

        # 7. Spoglio dalla console.
        resp = self.console.post(reverse("admin_market_resolve", args=[session.id]), {"next": url})
        self.assertEqual(resp.status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.status, MarketSession.Status.RESOLVED)
        self.assertEqual(Player.objects.get(pk=p["Boban"].pk).owner, beta)
        self.assertEqual(Player.objects.get(pk=p["Iniesta"].pk).owner, alfa)
        self.assertEqual(Player.objects.get(pk=p["Raul"].pk).owner, gamma)
        self.assertEqual(MarketBid.objects.get(participant=alfa, player=p["Boban"]).status, MarketBid.Status.LOST)
        page = self.console.get(url)
        self.assertContains(page, "3 sostituzioni da scegliere")
        self.assertContains(page, "Conferma e tira i dadi")
        self.assertContains(page, "Riepilogo da condividere")
        self.assertContains(page, "entro il")
        home = app_alfa.get(reverse("app_home"))
        self.assertContains(home, "conferma chi esce per 1 giocatore")

        # 8. Alfa conferma la proposta della sua lista e tira subito i dadi.
        iniesta = self._won(session, p["Iniesta"])
        page = app_alfa.get(reverse("app_mercato") + f"?session_id={session.id}")
        self.assertContains(page, "Centro Alfa Due (Lazio) · dalla tua lista")
        resp = app_alfa.post(reverse("app_market_release"), {
            "session_id": session.id, f"rel_{iniesta['bid_id']}": p["Centro Alfa Due"].id, "roll": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(Player.objects.get(pk=p["Centro Alfa Due"].pk).owner)
        self.assertIn(Player.objects.get(pk=p["Iniesta"].pk).contract_years, (1, 2, 3))

        # 9. Beta sceglie un giocatore fuori lista e tira i dadi dopo.
        boban = self._won(session, p["Boban"])
        app_beta.post(reverse("app_market_release"), {
            "session_id": session.id, f"rel_{boban['bid_id']}": p["Centro Beta Due"].id, "roll": "0"})
        self.assertIsNone(Player.objects.get(pk=p["Centro Beta Due"].pk).owner)
        self.assertEqual(Player.objects.get(pk=p["Centro Beta Uno"].pk).owner, beta)
        self.assertIsNone(Player.objects.get(pk=p["Boban"].pk).contract_years)

        # 10. Gamma non conferma: passate le 24 ore vale la sua lista.
        session.refresh_from_db()
        summary = session.results_summary
        for w in summary["won"]:
            if w.get("release_pending"):
                w["pending_since"] = (timezone.now() - timedelta(hours=25)).isoformat()
        MarketSession.objects.filter(pk=session.pk).update(results_summary=summary)
        scheduler.run_once()
        raul = self._won(session, p["Raul"])
        self.assertFalse(raul["release_pending"])
        self.assertTrue(raul["auto_confirmed"])
        self.assertEqual(raul["released_player"], "Punta Gamma Due")

        # 11. Alla fine: rose della stessa misura, crediti giusti, registro e verbale.
        for t in (alfa, beta, gamma):
            self.assertEqual(Player.objects.filter(owner=t).count(), start_roster[t.id], t.display_name)
        for t, amount, out_cost in ((alfa, 20, 20), (beta, 41, 20), (gamma, 12, 25)):
            spent = Participant.objects.get(pk=t.pk).spent_credits
            # Rimborso «prezzo d'acquisto»: torna quanto era costato chi esce.
            self.assertEqual(spent, start_spent[t.id] + Decimal(amount) - Decimal(out_cost), t.display_name)
        self.assertEqual(RosterLog.objects.filter(participant__league=league, action=RosterLog.Action.ASSIGN,
                                                  note__contains="Riparazione Novembre").count(), 3)
        self.assertEqual(RosterLog.objects.filter(participant__league=league, action=RosterLog.Action.RELEASE,
                                                  note__contains="Riparazione Novembre").count(), 3)
        page = self.console.get(url)
        self.assertNotContains(page, "sostituzioni da scegliere")
        self.assertContains(page, "dalla lista, tempo scaduto")
        share = page.context["share_text"]
        self.assertIn("+ Boban (C) 41 FM · esce Centro Beta Due", share)
        self.assertNotIn("da confermare", share)
        csv = self.console.get(reverse("admin_export_buste_csv", args=[session.id]))
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Boban", csv.content.decode("utf-8"))
        regia = self.console.get(reverse("app_regia") + f"?league={league.id}")
        self.assertNotContains(regia, "da confermare")
        self.assertNotContains(app_gamma.get(reverse("app_home")), "conferma chi esce")
