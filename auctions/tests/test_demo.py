"""Leghe di prova (Supervisor → Prove): ogni tipo di mercato fino a ogni fase,
con le mosse delle squadre fatte dai servizi veri e le pagine che si aprono."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import Auction, League, MarketSession, Participant, Player
from ..services import demo

EXPECTED = {
    ("buste", "aperto"): "open", ("buste", "concluso"): "resolved", ("buste", "finito"): "resolved",
    ("buste_taglio", "aperto"): "open", ("buste_taglio", "finito"): "resolved",
    ("free_agency", "aperto"): "open", ("free_agency", "finito"): "resolved",
    ("waiver", "aperto"): "open", ("waiver", "finito"): "resolved",
    ("clausole", "aperto"): "open", ("clausole", "finito"): "resolved",
    ("rinnovi", "aperto"): "open", ("rinnovi", "concluso"): "open", ("rinnovi", "finito"): "resolved",
}


class DemoLeagueTests(TestCase):
    def setUp(self):
        self.sup = User.objects.create_superuser("supervisore", password="pw")

    def _healthy(self, res):
        league = res["league"]
        self.assertEqual(league.created_via, demo.DEMO)
        self.assertEqual([line for line in res["log"] if line.startswith("⚠")], [], res["log"])
        for team in Participant.objects.filter(league=league):
            self.assertGreaterEqual(team.remaining_credits, 0, team.display_name)
            for role in "PDCA":
                n = Player.objects.filter(owner=team, role=role).count()
                self.assertLessEqual(n, getattr(league, f"slots_{role.lower()}"), (team.display_name, role))

    def test_every_kind_and_stage(self):
        for kind, _label in demo.KINDS:
            for stage, _s in demo.STAGES:
                with self.subTest(kind=kind, stage=stage):
                    res = demo.create_demo(self.sup, kind, stage, seed=11)
                    self._healthy(res)
                    if kind == "asta_live":
                        want = {"aperto": "READY", "concluso": "LIVE", "finito": "PAUSED"}[stage]
                        self.assertEqual(res["auction"].status, want)
                    elif (kind, stage) in EXPECTED:
                        self.assertEqual(res["session"].status, EXPECTED[(kind, stage)])

    def test_buste_stages_tell_the_story(self):
        res = demo.create_demo(self.sup, "buste", "concluso", seed=3)
        s = res["session"]
        self.assertTrue(s.release_after_count)
        summary = s.results_summary
        self.assertTrue(summary["won"])
        self.assertTrue(summary.get("tied"))                         # un pareggio da risolvere
        self.assertTrue(any(w.get("release_pending") for w in summary["won"]))    # conferme da fare
        mine = Participant.objects.get(league=res["league"], display_name="La tua squadra (prova)")
        self.assertFalse(s.bids.filter(participant=mine).exists())   # la tua squadra non ha mosse
        for kind in ("buste", "buste_taglio"):
            league = demo.create_demo(self.sup, kind, "finito", seed=3)["league"]
            self.assertFalse(Player.objects.filter(league=league, owner__isnull=False,
                                                   contract_years__isnull=True).exists(), kind)
        done = demo.create_demo(self.sup, "buste", "finito", seed=3)["session"]
        self.assertFalse(any(w.get("release_pending") for w in done.results_summary["won"]))
        self.assertFalse(any(not t.get("settled") for t in done.results_summary.get("tied", [])))

    def test_live_auction_sells_lots(self):
        res = demo.create_demo(self.sup, "asta_live", "finito", seed=5)
        sold = Player.objects.filter(league=res["league"], owner__isnull=False).count()
        self.assertGreater(sold, len(demo.TEAM_NAMES[:8]) * sum(demo.DEAL.values()))

    def test_supervisor_tab_creates_opens_and_deletes(self):
        self.client.force_login(self.sup)
        url = reverse("supervisor_dashboard") + "?tab=prove"
        page = self.client.get(url)
        self.assertContains(page, "Crea una lega di prova")
        resp = self.client.post(url, {"action": "create_demo", "kind": "tutti", "stage": "concluso"}, follow=True)
        self.assertContains(resp, "Cosa hanno fatto le squadre")
        leagues = list(demo.demo_leagues())
        self.assertEqual(len(leagues), len(demo.KINDS))
        # Le pagine di ogni prova si aprono, in console e nell'app.
        for row in resp.context["demo"]["rows"]:
            for link in (row["manage"], row["teams"], row["regia"]):
                self.assertEqual(self.client.get(link).status_code, 200, link)
        session = MarketSession.objects.filter(league__created_via=demo.DEMO,
                                               session_type=MarketSession.SessionType.REPAIR).first()
        self.assertEqual(self.client.get(reverse("app_regia_market_session", args=[session.id])).status_code, 200)
        target = leagues[0]
        self.client.post(url, {"action": "delete_demo", "league_id": target.id})
        self.assertFalse(League.objects.filter(pk=target.id).exists())
        self.assertFalse(Player.objects.filter(league_id=target.id).exists())
        self.client.post(url, {"action": "delete_all_demos"})
        self.assertFalse(demo.demo_leagues().exists())
        self.assertFalse(Auction.objects.filter(title__contains="(prova)").exists())

    def test_only_demo_leagues_are_deleted_and_only_by_the_supervisor(self):
        real = League.objects.create(name="Lega Vera", owner=self.sup)
        self.assertIsNone(demo.delete_demo(real.id))
        self.assertTrue(League.objects.filter(pk=real.id).exists())
        admin = User.objects.create_user("admin_lega", password="pw", is_staff=True)
        self.client.force_login(admin)
        self.client.post(reverse("supervisor_dashboard") + "?tab=prove",
                         {"action": "create_demo", "kind": "buste", "stage": "aperto"})
        self.assertFalse(demo.demo_leagues().exists())

    def test_bad_choice_says_what_to_do(self):
        self.client.force_login(self.sup)
        resp = self.client.post(reverse("supervisor_dashboard") + "?tab=prove",
                                {"action": "create_demo", "kind": "boh", "stage": "aperto"}, follow=True)
        self.assertContains(resp, "Scegli il tipo di mercato e la fase")
