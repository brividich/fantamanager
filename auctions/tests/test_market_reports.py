"""Report di mercato: registro dell'asta live, scambi, stagione e pareggi in vista."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction, AuctionCycleResult, Bid, League, MarketBid, MarketSession, Participant, Player, RosterLog, Trade,
)
from auctions.models.championship import Season
from auctions.services.market import place_market_bid, tie_forecast


class _Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pwd12345")
        self.league = League.objects.create(name="Lega Nostra", owner=self.owner, budget=Decimal("500"))
        self.alfa = Participant.objects.create(league=self.league, display_name="Alfa", credits=Decimal("500"))
        self.beta = Participant.objects.create(league=self.league, display_name="Beta <b>", credits=Decimal("500"))
        self.other_league = League.objects.create(name="Altra")
        self.outsider = Participant.objects.create(league=self.other_league, display_name="Fuori")

    def as_team(self, p):
        s = self.client.session
        s["participant_id"] = p.id
        s.save()


class LiveAuctionLogTests(_Base):
    def setUp(self):
        super().setUp()
        self.player = Player.objects.create(name="Lookman", role="A", team="Atalanta", league=self.league)
        self.auction = Auction.objects.create(title="Asta di gennaio", league=self.league, status=Auction.Status.CLOSED)
        t0 = timezone.now() - timedelta(hours=1)
        self.t0 = t0
        Bid.objects.create(auction=self.auction, participant=self.alfa, amount=10, increment=1, cycle=1,
                           accepted=True, server_received_at=t0, remaining_at_bid=20.0, ip_address="10.0.0.9")
        Bid.objects.create(auction=self.auction, participant=self.beta, amount=15, increment=5, cycle=1,
                           accepted=True, server_received_at=t0 + timedelta(seconds=18, milliseconds=400),
                           remaining_at_bid=0.512)
        Bid.objects.create(auction=self.auction, participant=self.alfa, amount=16, increment=1, cycle=1,
                           accepted=False, rejection_reason="timer_expired",
                           server_received_at=t0 + timedelta(seconds=40))
        Bid.objects.create(auction=self.auction, participant=self.alfa, amount=17, increment=1, cycle=1,
                           accepted=False, rejection_reason="rate_limited",
                           server_received_at=t0 + timedelta(seconds=41))
        r = AuctionCycleResult.objects.create(auction=self.auction, cycle=1, player=self.player, player_name="Lookman",
                                              player_role="A", winner=self.beta, winner_name="Beta <b>",
                                              amount=15, assigned=True)
        AuctionCycleResult.objects.filter(pk=r.pk).update(created_at=t0 + timedelta(seconds=39))

    def test_console_log_lists_every_raise_with_time_and_timer(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("admin_report_auction", args=[self.auction.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Registro dell'asta", html)
        self.assertIn(timezone.localtime(self.t0).strftime("%H:%M:%S"), html)
        self.assertIn("0,512 s", html)                     # timer when Beta's raise arrived
        self.assertIn("Si aggiudica il lotto", html)
        self.assertIn("Tempo scaduto", html)
        self.assertIn("1,000 s dopo la chiusura", html)
        self.assertIn("Beta &lt;b&gt;", html)              # escaped
        self.assertNotIn("10.0.0.9", html)                 # no IP addresses
        self.assertNotIn("Troppe offerte", html)           # rate-limited taps aren't in the log
        self.assertIn('class="sum-val">1</div><div class="sum-lbl">Vinti negli ultimi 3 s', html)

        csv = self.client.get(reverse("admin_report_auction_csv", args=[self.auction.id])).content.decode("utf-8-sig")
        self.assertIn("REGISTRO DELL'ASTA", csv)
        self.assertIn(";VINCENTE;", csv)
        self.assertIn(";0,512;", csv)

    def test_other_league_admin_is_refused(self):
        stranger = User.objects.create_user("estraneo", password="pwd12345")
        self.client.force_login(stranger)
        resp = self.client.get(reverse("admin_report_auction", args=[self.auction.id]))
        self.assertEqual(resp.status_code, 403)

    def test_app_teams_of_the_league_see_it_others_do_not(self):
        self.as_team(self.alfa)
        resp = self.client.get(reverse("app_report_auction", args=[self.auction.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Lookman", resp.content.decode())
        mercato = self.client.get(reverse("app_mercato")).content.decode()
        self.assertIn(reverse("app_report_auction", args=[self.auction.id]), mercato)
        self.assertIn(reverse("app_report_season"), mercato)

        self.as_team(self.outsider)
        self.assertEqual(self.client.get(reverse("app_report_auction", args=[self.auction.id])).status_code, 404)

    def test_console_asta_page_links_the_log(self):
        self.client.force_login(self.owner)
        html = self.client.get(f"{reverse('admin_market_repair')}?league={self.league.id}").content.decode()
        self.assertIn(reverse("admin_report_auction", args=[self.auction.id]), html)


class TradesReportTests(_Base):
    def setUp(self):
        super().setUp()
        self.p1 = Player.objects.create(name="Bastoni", role="D", league=self.league, owner=self.alfa)
        self.p2 = Player.objects.create(name="Barella", role="C", league=self.league, owner=self.beta)
        now = timezone.now()
        self.trade = Trade.objects.create(league=self.league, proposer=self.alfa, receiver=self.beta,
                                          proposer_credits=Decimal("5"), status=Trade.Status.COMPLETED,
                                          responded_at=now - timedelta(hours=2), completed_at=now - timedelta(minutes=30),
                                          message="Ci stai?")
        Trade.objects.filter(pk=self.trade.pk).update(created_at=now - timedelta(hours=5))
        self.trade.proposer_players.add(self.p1)
        self.trade.receiver_players.add(self.p2)
        Trade.objects.create(league=self.league, proposer=self.beta, receiver=self.alfa, status=Trade.Status.REJECTED,
                             responded_at=now)

    def test_console_and_app_show_every_trade_with_times(self):
        self.client.force_login(self.owner)
        resp = self.client.get(f"{reverse('admin_report_trades')}?league={self.league.id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Report degli scambi", html)
        self.assertIn("Bastoni", html)
        self.assertIn("Barella", html)
        self.assertIn("dopo 3 h 00 min", html)          # risposta
        self.assertIn("ratifica dopo 1 h 30 min", html)
        self.assertIn("Rifiutato", html)
        csv = self.client.get(f"{reverse('admin_report_trades_csv')}?league={self.league.id}").content.decode("utf-8-sig")
        self.assertIn("Bastoni (D)", csv)
        self.assertIn("Completato", csv)

        self.client.logout()
        self.as_team(self.beta)
        app = self.client.get(reverse("app_report_trades"))
        self.assertEqual(app.status_code, 200)
        self.assertIn("ratifica dopo 1 h 30 min", app.content.decode())
        self.assertIn(reverse("app_report_trades"), self.client.get(reverse("app_scambi")).content.decode())

    def test_other_league_team_sees_only_its_league(self):
        self.as_team(self.outsider)
        html = self.client.get(reverse("app_report_trades")).content.decode()
        self.assertNotIn("Bastoni", html)
        self.assertIn("Nessuno scambio", html)


class SeasonMarketReportTests(_Base):
    def test_moves_of_the_season_per_team_with_running_net(self):
        RosterLog.objects.create(participant=self.alfa, participant_name="Alfa", player_name="Vecchio", player_role="P",
                                 action=RosterLog.Action.ASSIGN, credits_delta=Decimal("99"))
        RosterLog.objects.filter(player_name="Vecchio").update(created_at=timezone.now() - timedelta(days=400))
        season = Season.objects.create(league=self.league, name="Stagione 2026/27")
        Season.objects.filter(pk=season.pk).update(created_at=timezone.now() - timedelta(days=60))
        RosterLog.objects.create(participant=self.alfa, participant_name="Alfa", player_name="Lookman", player_role="A",
                                 action=RosterLog.Action.ASSIGN, credits_delta=Decimal("40"))
        RosterLog.objects.create(participant=self.alfa, participant_name="Alfa", player_name="Zapata", player_role="A",
                                 action=RosterLog.Action.RELEASE, credits_delta=Decimal("-12"), note="Rimborso")

        self.client.force_login(self.owner)
        resp = self.client.get(f"{reverse('admin_report_season')}?league={self.league.id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Stagione 2026/27", html)
        self.assertIn("Lookman", html)
        self.assertIn("Zapata", html)
        self.assertNotIn("Vecchio", html)              # before the season
        self.assertIn("28 FM", html)                   # 40 spent − 12 refunded
        csv = self.client.get(f"{reverse('admin_report_season_csv')}?league={self.league.id}").content.decode("utf-8-sig")
        self.assertIn(";Lookman;A;40;0;40;", csv)
        self.assertIn(";Zapata;A;0;12;28;", csv)

        html = self.client.get(f"{reverse('admin_market_moves')}?league={self.league.id}").content.decode()
        self.assertIn(reverse("admin_report_season"), html)

        self.client.logout()
        self.as_team(self.beta)
        app = self.client.get(reverse("app_report_season"))
        self.assertEqual(app.status_code, 200)
        self.assertIn("Lookman", app.content.decode())

    def test_app_needs_a_team(self):
        resp = self.client.get(reverse("app_report_season"))
        self.assertEqual(resp.status_code, 302)


class TieForecastTests(_Base):
    def setUp(self):
        super().setUp()
        self.gamma = Participant.objects.create(league=self.league, display_name="Gamma", credits=Decimal("500"))
        self.player = Player.objects.create(name="Modric", role="C", team="Milan", league=self.league)
        self.session = MarketSession.objects.create(
            league=self.league, title="Riparazione", session_type=MarketSession.SessionType.SEALED_BIDS,
            status=MarketSession.Status.OPEN, tie_break=MarketSession.TieBreak.FIRST,
        )
        place_market_bid(self.session.id, self.beta.id, self.player.id, 30)
        place_market_bid(self.session.id, self.alfa.id, self.player.id, 30)
        place_market_bid(self.session.id, self.gamma.id, self.player.id, 20)
        t0 = timezone.now() - timedelta(minutes=10)
        MarketBid.objects.filter(participant=self.alfa).update(created_at=t0)
        MarketBid.objects.filter(participant=self.beta).update(created_at=t0 + timedelta(seconds=4))

    def test_nothing_while_the_window_is_open(self):
        self.assertEqual(tie_forecast(self.session), [])

    def test_first_wins_says_who_and_by_how_much(self):
        self.session.status = MarketSession.Status.CLOSED
        self.session.save()
        ties = tie_forecast(self.session)
        self.assertEqual(len(ties), 1)
        t = ties[0]
        self.assertEqual(t["winner"], "Alfa")
        self.assertEqual([c["name"] for c in t["teams"]], ["Alfa", "Beta <b>"])
        self.assertEqual(t["teams"][1]["gap_label"], "+4,000 s")
        self.assertEqual(MarketBid.objects.filter(status=MarketBid.Status.PENDING).count(), 3)  # nothing written

    def test_manual_rule_leaves_it_to_the_admin(self):
        self.session.status = MarketSession.Status.CLOSED
        self.session.tie_break = MarketSession.TieBreak.MANUAL
        self.session.save()
        t = tie_forecast(self.session)[0]
        self.assertEqual(t["winner"], "")
        self.assertEqual(t["rule"], "manual")

    def test_session_page_shows_it_only_once_the_offers_are_looked_at(self):
        self.session.status = MarketSession.Status.CLOSED
        self.session.save()
        self.client.force_login(self.owner)
        url = reverse("admin_market_session", args=[self.session.id])
        self.assertNotIn("Pareggi in vista <span", self.client.get(url).content.decode())
        html = self.client.get(url + "?preview=1").content.decode()
        self.assertIn("Pareggi in vista <span", html)
        self.assertIn("Vince Alfa", html)
        # Seen once: it stays (the window can't be reopened anyway).
        self.assertIn("Pareggi in vista <span", self.client.get(url).content.decode())
