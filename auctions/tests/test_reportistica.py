from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from auctions.models import League, MarketBid, MarketSession, Participant, Player
from auctions.services.market import place_market_bid, resolve_market_session


class ReportisticaTests(TestCase):
    """Test suite verifying team sheets (REAL SERAVEZZA format), renewals reports,
    and sealed bids official reports (Verbale Ufficiale di Spoglio)."""

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.local", "pw")
        self.league = League.objects.create(name="FantaLugnano", contracts_enabled=True)
        self.team_a = Participant.objects.create(
            display_name="Real Seravezza", short_name="RS",
            president_name="Nicolò Bartelletti", coach_name="Nicolò Bartelletti",
            stadium="Campo Lavazza", stadium_capacity="72100",
            league=self.league, credits=Decimal("500")
        )
        self.team_b = Participant.objects.create(
            display_name="Slavia Viafonda", short_name="SV",
            league=self.league, credits=Decimal("500")
        )
        # Players with contracts
        self.p_gk = Player.objects.create(
            name="Sommer", role="P", team="Inter", league=self.league,
            owner=self.team_a, cost=Decimal("171"), contract_years=1
        )
        self.p_def = Player.objects.create(
            name="Bastoni", role="D", team="Inter", league=self.league,
            owner=self.team_a, cost=Decimal("162"), contract_years=0  # Expiring / RIN.
        )
        self.p_att = Player.objects.create(
            name="Thuram", role="A", team="Inter", league=self.league,
            owner=self.team_a, cost=Decimal("1675"), contract_years=0
        )
        # Free agent for market
        self.free_pl = Player.objects.create(
            name="Modric", role="C", team="Milan", league=self.league,
            initial_price=Decimal("160")
        )

        # Market session
        self.session = MarketSession.objects.create(
            league=self.league,
            title="Mercato di Riparazione",
            session_type=MarketSession.SessionType.SEALED_BIDS,
            status=MarketSession.Status.OPEN,
        )

    def _login_app(self, participant):
        session = self.client.session
        session["participant_id"] = participant.id
        session.save()

    def test_app_print_team_sheet_real_seravezza_layout(self):
        """Verify the team sheet matches the exact layout of REAL SERAVEZZA.pdf."""
        self._login_app(self.team_a)
        resp = self.client.get(reverse("app_print_team_sheet"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        # Header info
        self.assertIn("Real Seravezza", content)
        self.assertIn("NICOLÒ BARTELLETTI", content)
        self.assertIn("CAMPO LAVAZZA", content)
        # Table columns and players
        self.assertIn("CALCIATORE", content)
        self.assertIn("SQUADRA", content)
        self.assertIn("SPESA", content)
        self.assertIn("ANNI", content)
        self.assertIn("SOMMER", content)
        self.assertIn("BASTONI", content)
        # Status class for expiring / renewal
        self.assertIn("st-renewal", content)
        # Tables for temporary loans and reservations
        self.assertIn("OPERAZIONI TEMPORANEE", content)
        self.assertIn("PRELAZIONE CEDUTI TEMPORANEI", content)
        self.assertIn("LEGENDA", content)

    def test_app_export_team_sheet_xlsx(self):
        """Verify downloading team sheet Excel."""
        self._login_app(self.team_a)
        resp = self.client.get(reverse("app_export_team_sheet_xlsx", args=[self.team_a.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", resp["Content-Type"])
        self.assertIn("Real_Seravezza_scheda.xlsx", resp["Content-Disposition"])

    def test_app_print_renewals_report(self):
        """Verify the league renewals report matching RINNOVO CONTRATTI format."""
        self._login_app(self.team_a)
        resp = self.client.get(reverse("app_print_renewals"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("RINNOVO CONTRATTI", content)
        self.assertIn("SVINCOLO", content)
        self.assertIn("RINNOVO", content)
        self.assertIn("ANNI DI", content)
        self.assertIn("BASTONI", content)
        self.assertIn("THURAM", content)

    def test_app_export_renewals_xlsx(self):
        """Verify downloading renewals report Excel."""
        self._login_app(self.team_a)
        resp = self.client.get(reverse("app_export_renewals_xlsx"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", resp["Content-Type"])
        self.assertIn("Rinnovi_", resp["Content-Disposition"])

    def test_verbale_lists_every_bid_with_time_only_after_spoglio(self):
        """Il verbale mostra ogni offerta con l'orario; prima dello spoglio nessuna."""
        from datetime import timedelta
        from django.utils import timezone
        self.session.tie_break = MarketSession.TieBreak.FIRST
        self.session.save()
        place_market_bid(self.session.id, self.team_a.id, self.free_pl.id, 170)
        place_market_bid(self.session.id, self.team_b.id, self.free_pl.id, 170)
        t0 = timezone.now() - timedelta(minutes=5)
        MarketBid.objects.filter(participant=self.team_a).update(created_at=t0)
        MarketBid.objects.filter(participant=self.team_b).update(created_at=t0 + timedelta(seconds=2, milliseconds=250))

        # Buste ancora chiuse: il verbale non rivela nulla, nemmeno a un manager.
        self._login_app(self.team_b)
        resp = self.client.get(reverse("app_print_buste_report", args=[self.session.id]))
        self.assertNotIn("Dettaglio delle offerte", resp.content.decode())
        csv_text = self.client.get(reverse("app_export_buste_csv", args=[self.session.id])).content.decode("utf-8-sig")
        self.assertNotIn("DETTAGLIO DI TUTTE LE OFFERTE", csv_text)
        self.assertNotIn("Slavia Viafonda", csv_text)

        self.session.status = MarketSession.Status.CLOSED
        self.session.save()
        resolve_market_session(self.session.id)

        local = timezone.localtime(t0)
        content = self.client.get(reverse("app_print_buste_report", args=[self.session.id])).content.decode()
        self.assertIn("Dettaglio delle offerte", content)
        self.assertIn("vince chi ha offerto prima", content)
        self.assertIn(local.strftime("%H:%M:%S"), content)
        self.assertIn("+2,250 s", content)
        self.assertIn("Slavia Viafonda", content)
        self.assertIn('class="same"', content)

        csv_text = self.client.get(reverse("app_export_buste_csv", args=[self.session.id])).content.decode("utf-8-sig")
        self.assertIn("DETTAGLIO DI TUTTE LE OFFERTE", csv_text)
        rows = [r for r in csv_text.splitlines() if r.startswith("Modric;C;Milan;")]
        self.assertEqual(len(rows), 2)
        self.assertIn(";Real Seravezza;170;", rows[0])
        self.assertIn("Aggiudicato", rows[0])
        self.assertIn(";Slavia Viafonda;170;", rows[1])
        self.assertIn(";2,250;SI;", rows[1])

        # La console vede lo stesso dettaglio.
        self.client.force_login(self.root)
        admin = self.client.get(reverse("admin_print_buste_report", args=[self.session.id])).content.decode()
        self.assertIn("Dettaglio delle offerte", admin)
        self.assertIn("+2,250 s", admin)

    def test_official_buste_verbale_print_and_csv(self):
        """Verify the official Verbale di Spoglio Buste report in A4 print and CSV."""
        place_market_bid(self.session.id, self.team_a.id, self.free_pl.id, 180)
        resolve_market_session(self.session.id)

        # 1. App print verbale view
        self._login_app(self.team_a)
        resp = self.client.get(reverse("app_print_buste_report", args=[self.session.id]))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("Verbale Ufficiale di Spoglio Buste", content)
        self.assertIn("FantaLugnano", content)
        self.assertIn("QUADRO ASSEGNAZIONI UFFICIALI CALCIATORI", content)
        self.assertIn("Modric", content)
        self.assertIn("Real Seravezza", content)
        self.assertIn("180 FM", content)
        self.assertIn("BILANCIO MOVIMENTI E CREDITI PER SOCIETÀ", content)

        # 2. App export CSV view
        resp_csv = self.client.get(reverse("app_export_buste_csv", args=[self.session.id]))
        self.assertEqual(resp_csv.status_code, 200)
        self.assertIn("text/csv", resp_csv["Content-Type"])
        csv_text = resp_csv.content.decode("utf-8-sig")
        self.assertIn("VERBALE UFFICIALE DI SPOGLIO BUSTE", csv_text)
        self.assertIn("Modric", csv_text)
        self.assertIn("AGGIUDICATO", csv_text)

        # 3. Admin print verbale view
        self.client.force_login(self.root)
        resp_admin = self.client.get(reverse("admin_print_buste_report", args=[self.session.id]))
        self.assertEqual(resp_admin.status_code, 200)
        self.assertIn("Verbale Ufficiale di Spoglio Buste", resp_admin.content.decode("utf-8"))

        # 4. Admin export CSV view
        resp_admin_csv = self.client.get(reverse("admin_export_buste_csv", args=[self.session.id]))
        self.assertEqual(resp_admin_csv.status_code, 200)
        self.assertIn("text/csv", resp_admin_csv["Content-Type"])
