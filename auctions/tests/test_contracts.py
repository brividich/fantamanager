"""Contratti di permanenza (regolamento 4): dadi, soglie, stagioni, rinnovi."""
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ..models import ContractEvent, League, MarketSession, Participant, Player
from ..services import contracts
from ..services.lifecycle import _contracts_after_sale, assign_player
from ..services.market import place_market_bid


class Fixed:
    """Deterministic 'dice': returns the given faces in order."""

    def __init__(self, *faces):
        self.faces = list(faces)

    def choice(self, seq):
        return self.faces.pop(0)


class ContractServiceTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Lugnanese", contracts_enabled=True)
        self.a = Participant.objects.create(display_name="A", league=self.league, credits=Decimal("2000"))
        self.b = Participant.objects.create(display_name="B", league=self.league, credits=Decimal("2000"))

    def _player(self, cost, owner=None, years=None, name="X"):
        return Player.objects.create(name=name, role="A", league=self.league, owner=owner or self.a,
                                     cost=Decimal(cost), contract_years=years)

    def test_contract_die_and_thresholds(self):
        # Clausola per ruolo (4.03): A 600 → 2 anni, 1000 → 3; P/D 100 → 2, 150 → 3.
        cases = [("A", 100, 1, 1), ("A", 600, 1, 2), ("A", 600, 3, 3), ("A", 1000, 2, 3),
                 ("P", 100, 1, 2), ("D", 150, 1, 3), ("C", 299, 1, 1), ("C", 300, 1, 2)]
        for role, cost, face, expected in cases:
            p = self._player(cost, name=f"P{role}{cost}{face}")
            p.role = role
            p.team = f"T{cost}{face}"
            p.save()
            res = contracts.roll_contract(p.id, participant_id=self.a.id, rng=Fixed(face))
            self.assertTrue(res["ok"], res)
            p.refresh_from_db()
            self.assertEqual(p.contract_years, expected, (cost, face))
        self.assertEqual(ContractEvent.objects.filter(kind="contract").count(), len(cases))

    def test_default_die_is_one_to_three(self):
        self.assertEqual(sorted(set(contracts.contract_faces(self.league))), [1, 2, 3])

    def test_only_owner_rolls_once_and_manual_is_admin_only(self):
        p = self._player(10)
        self.assertFalse(contracts.roll_contract(p.id, participant_id=self.b.id)["ok"])
        self.assertFalse(contracts.roll_contract(p.id, participant_id=self.a.id, manual_face=3)["ok"])
        res = contracts.roll_contract(p.id, by_admin=True, manual_face=3)
        self.assertEqual(res["years"], 3)
        self.assertTrue(ContractEvent.objects.get(kind="contract").manual)
        self.assertFalse(contracts.roll_contract(p.id, participant_id=self.a.id)["ok"])  # già tirato

    def test_disabled_league(self):
        self.league.contracts_enabled = False
        self.league.save()
        p = self._player(10)
        self.assertFalse(contracts.roll_contract(p.id, participant_id=self.a.id)["ok"])

    def test_acquisitions_reset_contract_to_be_rolled(self):
        free = Player.objects.create(name="Free", role="C", league=self.league, contract_years=3)
        assign_player(free.id, self.a.id, price=20)
        free.refresh_from_db()
        self.assertIsNone(free.contract_years)

    def test_season_renewals_flow(self):
        keep = self._player(50, years=1, name="Keep")
        drop = self._player(40, years=1, name="Drop")
        red = self._player(30, years=1, name="Red")
        long = self._player(30, years=3, name="Long")
        res = contracts.new_season(self.league.id)
        self.assertEqual(res["expired"], 3)
        self.league.refresh_from_db()
        self.assertTrue(self.league.renewals_open)
        long.refresh_from_db()
        self.assertEqual(long.contract_years, 2)

        # Rolling before declaring is refused.
        self.assertFalse(contracts.roll_renewal(keep.id, participant_id=self.a.id)["ok"])
        res = contracts.declare_renewals(self.a.id, [keep.id, red.id])
        self.assertEqual(res["released"], ["Drop"])
        drop.refresh_from_db()
        self.assertIsNone(drop.owner)
        self.assertFalse(contracts.declare_renewals(self.a.id, [])["ok"])  # una volta sola

        green = contracts.roll_renewal(keep.id, participant_id=self.a.id, rng=Fixed(True, 2))
        self.assertTrue(green["green"])
        keep.refresh_from_db()
        self.assertEqual(keep.contract_years, 2)

        lost = contracts.roll_renewal(red.id, participant_id=self.a.id, rng=Fixed(False))
        self.assertFalse(lost["green"])
        red.refresh_from_db()
        self.assertIsNone(red.owner)
        self.assertEqual(red.rescinded_from, self.a)

        # A non può ricomprarlo alle buste...
        session = MarketSession.objects.create(league=self.league, status=MarketSession.Status.OPEN)
        res = place_market_bid(session.id, self.a.id, red.id, 5)
        self.assertEqual(res["error"], "rescinded_rebuy")
        self.assertTrue(place_market_bid(session.id, self.b.id, red.id, 5)["ok"])

    def test_rescinded_player_auction_proceeds_go_to_former_team(self):
        p = Player.objects.create(name="Ex", role="D", league=self.league, rescinded_from=self.a)
        Player.objects.filter(pk=p.pk).update(owner=self.b, cost=Decimal("120"))
        _contracts_after_sale(p.id, self.b, Decimal("200"), SimpleNamespace(id=1))
        self.a.refresh_from_db()
        p.refresh_from_db()
        # Incasso con tetto per ruolo (4.02): difensore massimo 150.
        self.assertEqual(self.a.credits, Decimal("2150"))
        self.assertIsNone(p.rescinded_from)
        self.assertIsNone(p.contract_years)


class ContractViewsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("admin", password="pw")
        self.league = League.objects.create(name="L", owner=self.owner, contracts_enabled=True)
        self.a = Participant.objects.create(display_name="Alfa", league=self.league)
        self.p = Player.objects.create(name="Dybala", role="A", league=self.league, owner=self.a, cost=Decimal("600"))
        s = self.client.session
        s["participant_id"] = self.a.id
        s.save()

    def test_manager_rolls_from_rosa(self):
        page = self.client.get(reverse("app_rosa"))
        # In the rosa screen, contracts are strictly visual: badge "da tirare" is shown, no roll button
        self.assertContains(page, "da tirare")
        self.assertNotContains(page, "Tira il dado contratti")
        resp = self.client.post(reverse("app_contract_roll", args=[self.p.id]), follow=True)
        self.assertContains(resp, "Dado contratti per Dybala")
        self.p.refresh_from_db()
        self.assertGreaterEqual(self.p.contract_years, 2)
        # Verify it now visually renders the contract years
        page2 = self.client.get(reverse("app_rosa"))
        self.assertContains(page2, f"{self.p.contract_years}a")

    def test_manager_renewal_panel(self):
        Player.objects.filter(pk=self.p.pk).update(contract_years=0)
        self.league.renewals_open = True
        self.league.save()
        page = self.client.get(reverse("app_rosa"))
        # On app_rosa, renewals are visual-only ("scad.") - declaration form is not in rosa
        self.assertContains(page, "scad.")
        self.assertNotContains(page, "Conferma dichiarazione")
        # Direct backend declaration and roll endpoints remain operational
        self.client.post(reverse("app_renewals_declare"), {"renew": [self.p.id]})
        resp = self.client.post(reverse("app_renewal_roll", args=[self.p.id]), follow=True)
        self.assertEqual(resp.status_code, 200)

    def test_view_other_team_roster(self):
        other = Participant.objects.create(display_name="Beta", league=self.league)
        other_p = Player.objects.create(name="Lautaro", role="A", league=self.league, owner=other, cost=Decimal("500"), contract_years=2)
        page = self.client.get(reverse("app_rosa") + f"?team={other.id}")
        self.assertContains(page, "Lautaro")
        self.assertContains(page, "2a")
        # When viewing another team, releasing their players is not allowed from the UI
        self.assertNotContains(page, f"action=\"/participant/release/{other_p.id}/\"")

    def test_admin_page_and_actions(self):
        self.client.force_login(self.owner)
        url = reverse("admin_contracts") + f"?league={self.league.id}"
        page = self.client.get(url)
        self.assertContains(page, "Dybala")
        self.assertContains(page, "da tirare (min 2)")
        self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "roll", "player_id": self.p.id, "manual_face": "1"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.contract_years, 2)  # dado 1, clausola 600 FM → 2
        self.client.post(reverse("admin_contracts_action"), {"league_id": self.league.id, "action": "new_season"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.contract_years, 1)
        self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "set", "player_id": self.p.id, "years": "4"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.contract_years, 4)

    def test_foreign_admin_forbidden(self):
        self.client.force_login(User.objects.create_user("intruder", password="pw"))
        resp = self.client.post(reverse("admin_contracts_action"), {
            "league_id": self.league.id, "action": "set", "player_id": self.p.id, "years": "4"})
        self.assertEqual(resp.status_code, 403)


from django.test import override_settings  # noqa: E402

from .. import services as svc  # noqa: E402
from .common import make_live_auction  # noqa: E402


@override_settings(BID_MIN_INTERVAL_MS=0)
class RescindedLiveAuctionTests(TestCase):
    def test_former_team_cannot_bid_on_rescinded_player(self):
        league = League.objects.create(name="L", contracts_enabled=True)
        a = Participant.objects.create(display_name="A", league=league, credits=Decimal("500"))
        b = Participant.objects.create(display_name="B", league=league, credits=Decimal("500"))
        p = Player.objects.create(name="Ex", role="A", league=league, rescinded_from=a)
        auction = make_live_auction(
            league=league, player=p, starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1,5", enforce_limits=False,
        )
        r = svc.place_bid(auction.id, a.id, 1)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "rescinded_rebuy")
        self.assertTrue(svc.place_bid(auction.id, b.id, 1).accepted)

    def test_former_team_can_rebuy_after_an_unsold_first_round(self):
        from ..models import AuctionCycleResult
        league = League.objects.create(name="L2", contracts_enabled=True)
        a = Participant.objects.create(display_name="A", league=league, credits=Decimal("500"))
        p = Player.objects.create(name="Ex2", role="A", league=league, rescinded_from=a)
        auction = make_live_auction(
            league=league, player=p, starting_price=Decimal("1"), current_price=Decimal("1"),
            min_increment=Decimal("1"), quick_increments="1", enforce_limits=False,
        )
        AuctionCycleResult.objects.create(auction=auction, cycle=auction.current_cycle, player=p, assigned=False)
        auction.current_cycle += 1
        auction.save(update_fields=["current_cycle"])
        self.assertTrue(svc.place_bid(auction.id, a.id, 1).accepted)



class GoalkeeperAndU21Tests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="L", contracts_enabled=True, gk_max_clubs=2)
        self.a = Participant.objects.create(display_name="A", league=self.league, credits=Decimal("500"))

    def test_goalkeeper_block_shares_contract(self):
        first = Player.objects.create(name="Sommer", role="P", team="INT", league=self.league, owner=self.a,
                                      initial_price=Decimal("20"), contract_years=2)
        second = Player.objects.create(name="Martinez", role="P", team="INT", league=self.league, owner=self.a,
                                       initial_price=Decimal("5"))
        res = contracts.roll_contract(second.id, participant_id=self.a.id, rng=Fixed(3))
        self.assertEqual(res["years"], 2)
        self.assertEqual(res["block"], "Sommer")
        contracts.set_contract(first.id, 3)
        second.refresh_from_db()
        self.assertEqual(second.contract_years, 3)

    def test_goalkeepers_from_max_two_clubs(self):
        from ..services.bidding import gk_clubs_problem
        for team in ("INT", "MIL"):
            Player.objects.create(name=f"GK {team}", role="P", team=team, league=self.league, owner=self.a)
        third = Player.objects.create(name="GK JUV", role="P", team="JUV", league=self.league)
        same = Player.objects.create(name="GK2 INT", role="P", team="INT", league=self.league)
        self.assertTrue(gk_clubs_problem(self.a, third))
        self.assertFalse(gk_clubs_problem(self.a, same))
        session = MarketSession.objects.create(league=self.league, status=MarketSession.Status.OPEN)
        self.assertEqual(place_market_bid(session.id, self.a.id, third.id, 5)["error"], "gk_clubs")

    def test_under21_bet(self):
        p = Player.objects.create(name="Esposito", role="A", league=self.league, owner=self.a, cost=Decimal("10"))
        res = contracts.declare_u21(p.id, participant_id=self.a.id)
        self.assertEqual(res["years"], 3)
        other = Player.objects.create(name="Camarda", role="A", league=self.league, owner=self.a)
        self.assertFalse(contracts.declare_u21(other.id, participant_id=self.a.id)["ok"])  # una per stagione



class AnimatedDiceTests(TestCase):
    """Il dado animato: le risposte portano le facce, la console risponde in JSON al tiro."""

    def setUp(self):
        self.owner = User.objects.create_user("admin", password="pw")
        self.league = League.objects.create(name="L", owner=self.owner, contracts_enabled=True,
                                            contract_rules={"faces": [1, 2, 3, 4]})
        self.a = Participant.objects.create(display_name="Alfa", league=self.league)
        self.p = Player.objects.create(name="Dybala", role="A", league=self.league, owner=self.a, cost=Decimal("5"))

    def test_results_carry_the_die_faces(self):
        res = contracts.roll_contract(self.p.id, participant_id=self.a.id, rng=Fixed(4))
        self.assertEqual((res["face"], res["years"], res["faces"]), (4, 4, [1, 2, 3, 4]))
        Player.objects.filter(pk=self.p.pk).update(contract_years=0, renewal_declared=True)
        self.league.renewals_open = True
        self.league.save()
        res = contracts.roll_renewal(self.p.id, participant_id=self.a.id, rng=Fixed(True, 2))
        self.assertEqual((res["green"], res["face"], res["faces"]), (True, 2, [1, 2, 3, 4]))

    def test_console_roll_answers_json_to_the_animation(self):
        self.client.force_login(self.owner)
        url = reverse("admin_contracts_action")
        resp = self.client.post(url, {"league_id": self.league.id, "action": "roll", "player_id": self.p.id,
                                      "manual_face": "3"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = resp.json()
        self.assertEqual((data["ok"], data["face"], data["years"], data["faces"]), (True, 3, 3, [1, 2, 3, 4]))
        self.assertIn("Dybala", data["feedback_message"])
        # Il messaggio resta anche per la pagina che si ricarica dopo il dado.
        self.assertContains(self.client.get(reverse("admin_contracts") + f"?league={self.league.id}"),
                            "Dybala: dado 3")
        # Senza JavaScript il modulo funziona come prima.
        other = Player.objects.create(name="Kean", role="A", league=self.league, owner=self.a)
        resp = self.client.post(url, {"league_id": self.league.id, "action": "roll", "player_id": other.id})
        self.assertEqual(resp.status_code, 302)

    def test_pages_load_the_die(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("admin_contracts") + f"?league={self.league.id}")
        self.assertContains(page, "window.FMDice")
        self.assertContains(page, 'data-dice="contract" data-player="Dybala"')

    def test_goalkeeper_block_message_has_no_missing_die(self):
        Player.objects.create(name="Sommer", role="P", team="INT", league=self.league, owner=self.a, contract_years=2)
        keeper = Player.objects.create(name="Martinez", role="P", team="INT", league=self.league, owner=self.a)
        s = self.client.session
        s["participant_id"] = self.a.id
        s.save()
        data = self.client.post(reverse("app_contract_roll", args=[keeper.id]),
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual((data["face"], data["block"]), (None, "Sommer"))
        self.assertIn("blocco portieri", data["feedback_message"])
        self.assertNotIn("None", data["feedback_message"])
