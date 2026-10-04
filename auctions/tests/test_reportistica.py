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
