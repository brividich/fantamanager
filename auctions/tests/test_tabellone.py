"""Il tabellone del budget (services/economy.py, _tabellone.html): torna con
i crediti veri, uguale in console e app, lo vedono tutti i membri della lega."""
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import (CapPhase, DecreeAward, League, LeagueRanking, Participant, Player, RosterLog, Season,
                      Trade)
from ..services import economy, salary, season
from ..services.lifecycle import assign_player, release_player


class TabelloneTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_tab", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lugnanese", owner=self.owner, salary_cap_enabled=True,
                                            decree_mid_enabled=True, season_number=3)
        Season.objects.create(league=self.league, name="2027/28", is_current=True)
        self.a = Participant.objects.create(display_name="Dinamo", league=self.league, credits=Decimal("3000"))
        self.b = Participant.objects.create(display_name="Spartak", league=self.league, credits=Decimal("2000"))
        prev = salary.save_ranking(self.league, 2, LeagueRanking.Kind.FINAL, [self.a.id, self.b.id])
        salary.open_phase(self.league, CapPhase.Kind.SUMMER, prev)

    def row(self, team):
        return next(r for r in economy.tabellone(self.league)["rows"] if r["team"].id == team.id)

    def test_every_move_adds_up_to_the_real_budget(self):
        bomber = Player.objects.create(name="Bomber", role="A", league=self.league)
        mezz = Player.objects.create(name="Mezzala", role="C", league=self.league)
        assign_player(bomber.id, self.a.id, price=Decimal("300"))
        assign_player(mezz.id, self.a.id, price=Decimal("100"))
        release_player(mezz.id, by_admin=True)
        trade = Trade.objects.create(league=self.league, proposer=self.a, receiver=self.b,
                                     proposer_credits=Decimal("50"), status=Trade.Status.COMPLETED)
        Trade.objects.filter(pk=trade.pk).update(completed_at=trade.created_at)
        self.a.refresh_from_db()
        self.a.credits -= 50
        self.a.save()
        report = season.midseason(self.league.id, mid_order=[self.b.id, self.a.id])   # A secondo: +150
        self.assertTrue(report["ok"])
        self.client.force_login(self.owner)
        self.client.post(reverse("admin_adjust_team_credits", args=[self.a.id]),
                         {"mode": "sub", "amount": "20", "note": "Multa modulo asta"})

        self.a.refresh_from_db()
        r = self.row(self.a)
        self.assertEqual(r["budget"], self.a.remaining_credits)
        self.assertEqual(r["budget_start"], Decimal("3000"))
        t = r["totals"]
        self.assertEqual(t["summer_spent"], Decimal("-400"))
        self.assertGreater(t["summer_income"], 0)          # lo svincolo rende qualcosa
        self.assertEqual(t["trades"], Decimal("-50"))
        self.assertEqual(t["decree"], Decimal("150"))
        self.assertEqual(t["manual"], Decimal("-20"))
        self.assertEqual(r["spent"], Decimal("400"))
        self.assertEqual(r["cap"]["initial"], 1000)          # primo l'anno scorso
        self.assertGreater(r["cap"]["winter_bonus"], 0)
        self.assertTrue(any(m["text"] == "Multa modulo asta" for m in r["moves"]))
        rb = self.row(self.b)
        self.assertEqual(rb["totals"]["trades"], Decimal("50"))

    def _section(self, html):
        part = html.split("<!-- tabellone:start -->", 1)[1].split("<!-- tabellone:end -->", 1)[0]
        part = re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)
        from ..views.common import APP_PAGES
        for console_name, app_name in APP_PAGES.items():
            try:
                urls = (reverse(console_name), reverse(app_name))
            except Exception:  # noqa: BLE001 — pagine con argomenti
                continue
            for url in urls:
                part = re.sub('"' + re.escape(url) + r'(?=["?#])', f'"PAGE:{console_name}', part)
        return re.sub(r"\s+", " ", part)

    def test_console_and_app_show_the_same_table(self):
        self.client.force_login(self.owner)
        console = self.client.get(reverse("admin_tabellone"), {"league": self.league.id})
        self.assertContains(console, "Tabellone budget")
        self.assertContains(console, "Tetto spendibile")
        session = self.client.session
        session["participant_id"] = self.a.id
        session.save()
        app = self.client.get(reverse("app_tabellone"))
        self.assertEqual(app.status_code, 200)
        self.assertEqual(self._section(console.content.decode()), self._section(app.content.decode()))

    def test_a_manager_sees_it_without_the_credit_tools(self):
        session = self.client.session
        session["participant_id"] = self.b.id
        session.save()
        page = self.client.get(reverse("app_tabellone"))
        self.assertContains(page, "Dinamo")
        self.assertContains(page, "Spartak")
        self.assertNotContains(page, reverse("admin_adjust_team_credits", args=[self.a.id]))

    def test_the_league_manager_corrects_credits_with_a_reason(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("admin_adjust_team_credits", args=[self.b.id]),
                                {"mode": "add", "amount": "250", "note": "Premio Coppa Lugnanese",
                                 "next": reverse("admin_tabellone")})
        self.assertEqual(resp.status_code, 302)
        log = RosterLog.objects.get(participant=self.b, money=RosterLog.Money.MANUAL)
        self.assertEqual((log.player_name, log.credits_delta), ("Premio Coppa Lugnanese", Decimal("-250")))
        self.assertEqual(self.row(self.b)["totals"]["manual"], Decimal("250"))


class DecreeSwitchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pres_dec", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lega Decreto", owner=self.owner, season_number=1)
        self.teams = [Participant.objects.create(display_name=f"T{i}", league=self.league, credits=Decimal("1000"))
                      for i in range(10)]
        self.order = [t.id for t in self.teams]

    def test_off_means_no_decree_even_with_the_salary_cap(self):
        self.league.salary_cap_enabled = True
        self.league.save()
        season.midseason(self.league.id, mid_order=self.order)
        self.assertFalse(DecreeAward.objects.exists())

    def test_winter_only_without_the_salary_cap(self):
        self.league.decree_mid_enabled = True
        self.league.save()
        season.midseason(self.league.id, mid_order=self.order)
        award = DecreeAward.objects.get()
        self.assertEqual(award.kind, LeagueRanking.Kind.MIDSEASON)
        self.assertEqual([d["credits"] for d in sorted(award.details, key=lambda d: d["position"])],
                         [100, 150, 150, 200, 200, 250, 250, 300, 300, 400])
        report = season.start_new_season(self.league.id, final_order=self.order)
        self.assertFalse(any("Decreto" in s for s in report["steps"]))      # l'estivo è spento

    def test_assign_now_from_the_season_page(self):
        self.client.force_login(self.owner)
        url = reverse("admin_season_action")
        self.client.post(url, {"league_id": self.league.id, "action": "decree_switches", "decree_final_enabled": "1"})
        self.league.refresh_from_db()
        self.assertEqual((self.league.decree_mid_enabled, self.league.decree_final_enabled), (False, True))
        page = self.client.get(reverse("admin_season"), {"league": self.league.id})
        self.assertContains(page, "Assegna ora")
        post = {"league_id": self.league.id, "action": "decree_final_now"}
        post.update({f"final_{t.id}": str(i + 1) for i, t in enumerate(self.teams)})
        self.client.post(url, post)
        award = DecreeAward.objects.get(kind=LeagueRanking.Kind.FINAL)
        first = next(d for d in award.details if d["position"] == 1)
        self.assertEqual((first["credits"], first["euro"]), (400, 350))
        self.teams[-1].refresh_from_db()
        self.assertEqual(self.teams[-1].credits, Decimal("2200"))          # decimo: +1200
        # Il passaggio di fine stagione lo trova già assegnato e non lo ripete.
        season.start_new_season(self.league.id, final_order=self.order)
        self.assertEqual(DecreeAward.objects.filter(kind=LeagueRanking.Kind.FINAL).count(), 1)
        self.teams[-1].refresh_from_db()
        self.assertEqual(self.teams[-1].credits, Decimal("2200"))

    def test_assign_now_needs_a_ranking(self):
        self.league.decree_mid_enabled = True
        self.league.save()
        self.client.force_login(self.owner)
        resp = self.client.post(reverse("admin_season_action"),
                                {"league_id": self.league.id, "action": "decree_mid_now"}, follow=True)
        self.assertContains(resp, "Serve la classifica di metà stagione")
        self.assertFalse(DecreeAward.objects.exists())


class PrizeTests(TestCase):
    def setUp(self):
        from ..models import Competition
        self.owner = User.objects.create_user("pres_prize", password="pw", is_staff=True)
        self.league = League.objects.create(name="Lega Premi", owner=self.owner)
        self.teams = [Participant.objects.create(display_name=f"Club {i}", league=self.league, credits=Decimal("1000"))
                      for i in range(4)]
        self.season = Season.objects.create(league=self.league, name="2027/28", is_current=True)
        self.cup = Competition.objects.create(season=self.season, name="Coppa Lugnanese",
                                              kind=Competition.Type.KNOCKOUT)

    def test_save_award_once_and_see_it_in_the_tabellone(self):
        self.client.force_login(self.owner)
        url = reverse("admin_competition_prizes", args=[self.cup.id])
        self.client.post(url, {"action": "save", "credits": "250,150,100,50", "euro": "50"})
        self.cup.refresh_from_db()
        self.assertEqual(self.cup.settings["prizes"], {"credits": [250, 150, 100, 50], "euro": [50]})
        page = self.client.get(reverse("admin_competitions"), {"league": self.league.id, "comp": self.cup.id})
        self.assertContains(page, "Assegna premi")
        order = [self.teams[2], self.teams[0], self.teams[3], self.teams[1]]
        post = {"action": "award"}
        post.update({f"pos_{i}": str(t.id) for i, t in enumerate(order, 1)})
        self.client.post(url, post)
        self.teams[2].refresh_from_db()
        self.assertEqual(self.teams[2].credits, Decimal("1250"))
        award = self.cup.prize_award
        self.assertEqual(award.details[0]["euro"], 50)
        self.client.post(url, post)                                   # una volta sola
        self.teams[2].refresh_from_db()
        self.assertEqual(self.teams[2].credits, Decimal("1250"))
        row = next(r for r in economy.tabellone(self.league)["rows"] if r["team"].id == self.teams[2].id)
        self.assertEqual(row["totals"]["prizes"], Decimal("250"))
        self.assertEqual(row["budget_start"], Decimal("1000"))
        # Nell'app (Regia) lo stesso riquadro.
        app = self.client.get(reverse("app_regia_competitions"), {"league": self.league.id, "comp": self.cup.id})
        self.assertContains(app, "Premi assegnati")

    def test_the_same_team_twice_is_refused(self):
        from ..services import prizes
        prizes.set_table(self.cup, "100,50", "")
        with self.assertRaises(ValueError):
            prizes.award_prizes(self.cup, [self.teams[0].id, self.teams[0].id])

    def test_the_cup_proposes_winner_and_finalist(self):
        from ..models import Fixture, Giornata
        from ..services import prizes
        g1 = Giornata.objects.create(season=self.season, number=1)
        g2 = Giornata.objects.create(season=self.season, number=2)
        a, b, c, d = self.teams
        mk = lambda g, h, aw, hg, ag, stage: Fixture.objects.create(
            giornata=g, competition=self.cup, home=h, away=aw, home_goals=hg, away_goals=ag,
            home_total=Decimal(60 + hg), away_total=Decimal(60 + ag), computed=True, stage=stage)
        mk(g1, a, d, 2, 0, "Semifinale")
        mk(g1, b, c, 1, 1, "Semifinale")          # pari: passa chi ha più fantapunti → in casa a parità
        mk(g2, a, b, 0, 3, "Finale")
        self.assertEqual(prizes.suggested_order(self.cup)[:2], [b.id, a.id])
        self.assertEqual(set(prizes.suggested_order(self.cup)[2:]), {c.id, d.id})

    def test_prizes_follow_the_competition_into_the_next_season(self):
        from ..services import prizes
        from ..services.competitions import roll_season
        prizes.set_table(self.cup, "250,150", "50")
        new = roll_season(self.league)
        clone = new.competitions.get(name="Coppa Lugnanese")
        self.assertEqual(prizes.table(clone)["credits"], [250, 150])


class IncomeNotDecreeTests(TestCase):
    """Un accredito per un giocatore perso (estero, rescisso e rivenduto) è un
    ricavo del mercato, non un Decreto (segnalato dalla revisione)."""

    def test_abroad_compensation_is_market_income(self):
        from ..services.salary import add_credits
        league = League.objects.create(name="Lega Estero")
        Season.objects.create(league=league, name="2027/28", is_current=True)
        team = Participant.objects.create(display_name="Club", league=league, credits=Decimal("500"))
        add_credits(team, 120, "Cessione fuori dalla Serie A: Bomber", money=RosterLog.Money.INCOME)
        row = economy.tabellone(league)["rows"][0]
        self.assertEqual(row["totals"]["summer_income"], Decimal("120"))
        self.assertEqual(row["totals"]["decree"], Decimal("0"))
        self.assertEqual(row["budget_start"], Decimal("500"))

    def test_old_rows_without_type_are_read_by_their_name(self):
        league = League.objects.create(name="Lega Vecchia")
        team = Participant.objects.create(display_name="Club", league=league, credits=Decimal("580"))
        RosterLog.objects.create(participant=team, participant_name="Club", action=RosterLog.Action.EDIT,
                                 player_name="Incasso asta di Rescisso (rescisso al rinnovo) · asta #3",
                                 credits_delta=Decimal("-80"))
        row = economy.tabellone(league)["rows"][0]
        self.assertEqual((row["totals"]["summer_income"], row["totals"]["decree"]), (Decimal("80"), Decimal("0")))
