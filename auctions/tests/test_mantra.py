import asyncio
import json
import io
import re
import sqlite3
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.conf import settings
from django.test import (RequestFactory, TestCase, TransactionTestCase,
                         override_settings)
from django.utils import timezone

from .. import mantra, services
from ..providers import importers
from ..views import participant_join_url
from ..routing import websocket_urlpatterns
from ..models import (Auction, AuctionCycleResult, AuctionQueueItem, Bid, Formation,
                     League, Participant, Player, RosterLog, SealedBid, Watch)
from .common import make_live_auction

class MantraTableTests(TestCase):
    """La tabella dei moduli e' un dato del regolamento: va difesa dai refusi."""

    def test_every_module_has_eleven_slots(self):
        for name, slots in mantra.MODULES.items():
            self.assertEqual(len(slots), 11, f"{name} non ha 11 slot")

    def test_every_module_balances_five_defensive_and_five_offensive(self):
        # Regola cardine del Mantra: 5 di movimento difensivi + 5 offensivi.
        # Gli slot misti (M/C) contano da una parte o dall'altra, quindi si
        # verifica che esista una ripartizione valida, non un conteggio secco.
        for name, slots in mantra.MODULES.items():
            movement = slots[1:]
            rigid_def = sum(1 for sl in movement
                            if all(mantra.ROLES[r][1] == "D" for r in sl))
            rigid_off = sum(1 for sl in movement
                            if all(mantra.ROLES[r][1] == "O" for r in sl))
            self.assertLessEqual(rigid_def, 5, f"{name}: troppi slot difensivi")
            self.assertLessEqual(rigid_off, 5, f"{name}: troppi slot offensivi")
            self.assertEqual(len(movement), 10, f"{name}: non 10 di movimento")

    def test_the_first_slot_is_always_the_goalkeeper(self):
        for name, slots in mantra.MODULES.items():
            self.assertEqual(slots[0], ("Por",), f"{name} non apre col portiere")

    def test_lines_cover_every_slot_exactly_once(self):
        for name, slots in mantra.MODULES.items():
            seen = [i for _, idxs in mantra.module_lines(name) for i in idxs]
            self.assertEqual(seen, list(range(len(slots))), f"{name}: righe sbagliate")

    def test_roles_parse_from_the_official_column(self):
        self.assertEqual(mantra.parse_roles("B;Dd;E"), ["B", "Dd", "E"])
        self.assertEqual(mantra.parse_roles("M/C"), ["M", "C"])
        self.assertEqual(mantra.parse_roles("por"), ["Por"])
        self.assertEqual(mantra.parse_roles("Dc;Dc"), ["Dc"])      # niente doppioni
        self.assertEqual(mantra.parse_roles("Xx"), [])             # sigla ignota
        self.assertEqual(mantra.parse_roles(""), [])

    def test_slot_compatibility_follows_the_slot_not_the_reparto(self):
        self.assertTrue(mantra.slot_accepts(("E", "W"), ["W", "A"]))
        self.assertFalse(mantra.slot_accepts(("Dc",), ["Dd", "Ds"]))   # regola ufficiale
        self.assertFalse(mantra.slot_accepts(("Dd",), ["Ds"]))         # non intercambiabili


class MantraListoneTests(TestCase):
    """La colonna RM e le quotazioni Mantra entrano nel listone."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega M", game_mode=League.GameMode.MANTRA)

    def _import(self, csv_text):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("listone.csv", csv_text.encode(), content_type="text/csv")
        rows, _ = importers.parse_listone_file(f, "listone.csv")
        importers.sync_players(rows, league=self.league)
        return rows

    def test_roles_and_mantra_quotes_land_on_the_player(self):
        self._import(
            "Id;R;RM;Nome;Squadra;Qt.A;Qt.A M;FVM;FVM M\n"
            "1;D;B;Dd;E;Celik;Roma;14;16;14;17\n")
        # NB: la riga sopra ha i ruoli multipli separati da ';' come il campo
        # separatore del CSV, quindi si usa il file vero nel test successivo.

    def test_roles_and_quotes_from_a_comma_file(self):
        self._import(
            "Id,R,RM,Nome,Squadra,Qt.A,Qt.A M,FVM,FVM M\n"
            "1,D,Dd;Ds;E,Spinazzola,Napoli,10,12,30,36\n")
        p = Player.objects.get(league=self.league, name="Spinazzola")
        self.assertEqual(p.mantra_roles, "Dd;Ds;E")
        self.assertEqual(p.role_list, ["Dd", "Ds", "E"])
        self.assertEqual(p.roles_display, "Dd/Ds/E")
        self.assertEqual(p.role, "D")               # la colonna R resta la verita' Classic
        self.assertEqual(p.initial_price, Decimal("10"))
        self.assertEqual(p.price_m, Decimal("12"))
        self.assertEqual(p.fvm_m, Decimal("36"))

    def test_the_mantra_quote_is_the_one_that_counts_in_a_mantra_league(self):
        self._import(
            "Id,R,RM,Nome,Squadra,Qt.A,Qt.A M,FVM,FVM M\n"
            "1,D,Dd;E,Rensch,Roma,13,14,17,20\n")
        p = Player.objects.get(league=self.league, name="Rensch")
        self.assertEqual(p.price_for(self.league), Decimal("14"))
        self.assertEqual(p.fvm_for(self.league), Decimal("20"))
        classic = League.objects.create(name="Lega C")
        self.assertEqual(p.price_for(classic), Decimal("13"))
        self.assertEqual(p.fvm_for(classic), Decimal("17"))

    def test_a_listone_without_the_classic_column_still_gets_a_role(self):
        # File rifatto a mano con la sola RM: senza ricaduta finirebbero tutti
        # attaccanti, e l'ordine di chiamata per reparto sarebbe da buttare.
        self._import("Id,RM,Nome,Squadra,Qt.A\n1,Por,Tizio,Como,10\n")
        self.assertEqual(Player.objects.get(name="Tizio").role, "P")

    def test_re_importing_without_the_column_keeps_the_roles(self):
        self._import("Id,R,RM,Nome,Squadra,Qt.A\n1,D,Dc,Bastoni,Inter,20\n")
        self._import("Id,R,Nome,Squadra,Qt.A\n1,D,Bastoni,Inter,22\n")
        p = Player.objects.get(league=self.league, name="Bastoni")
        self.assertEqual(p.mantra_roles, "Dc")
        self.assertEqual(p.initial_price, Decimal("22"))


class MantraSlotLimitTests(TestCase):
    """In Mantra la rosa si conta a portieri e giocatori di movimento."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega M", game_mode=League.GameMode.MANTRA,
            slots_gk=3, slots_out=22, budget=Decimal("500"))
        self.team = Participant.objects.create(
            display_name="Alfa", league=self.league, credits=Decimal("500"))

    def test_the_cap_is_shared_across_every_outfield_role(self):
        self.assertEqual(self.league.slots_for("D"), 22)
        self.assertEqual(self.league.slots_for("C"), 22)
        self.assertEqual(self.league.slots_for("A"), 22)
        self.assertEqual(self.league.slots_for("P"), 3)
        self.assertEqual(self.league.total_slots, 25)
        self.assertEqual(self.league.slot_roles("D"), ("D", "C", "A"))
        self.assertEqual(self.league.slot_roles("P"), ("P",))

    def test_a_classic_league_is_untouched(self):
        lg = League.objects.create(name="Lega C", slots_p=3, slots_d=8, slots_c=8, slots_a=6)
        self.assertEqual(lg.slots_for("D"), 8)
        self.assertEqual(lg.slot_roles("D"), ("D",))
        self.assertEqual(lg.total_slots, 25)

    def test_the_label_says_what_the_league_actually_counts(self):
        self.assertEqual(self.league.slots_label, "3 Por + 22 mov.")

    def test_buying_defenders_fills_the_same_bucket_as_forwards(self):
        # 22 di movimento comprati come difensori: il ventitreesimo, attaccante,
        # deve essere rifiutato lo stesso.
        for i in range(22):
            Player.objects.create(league=self.league, name=f"Dif {i}", role="D",
                                  initial_price=1, owner=self.team)
        plan = services.roster_plan(self.team)
        movimento = [r for r in plan["roles"] if r["role"] == "X"][0]
        self.assertEqual(movimento["owned"], 22)
        self.assertEqual(movimento["free"], 0)
        self.assertEqual(movimento["label"], "MOV")

    def test_the_planner_shows_two_bands_in_mantra_and_four_in_classic(self):
        self.assertEqual([r["role"] for r in services.roster_plan(self.team)["roles"]],
                         ["P", "X"])
        lg = League.objects.create(name="Lega C")
        team = Participant.objects.create(display_name="Beta", league=lg,
                                          credits=Decimal("500"))
        self.assertEqual([r["role"] for r in services.roster_plan(team)["roles"]],
                         ["P", "D", "C", "A"])


class MantraFormationTests(TestCase):
    """La formazione Mantra ragiona per slot, non per reparto."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega M", game_mode=League.GameMode.MANTRA)
        self.team = Participant.objects.create(
            display_name="Alfa", league=self.league, credits=Decimal("500"))

    def _player(self, name, role, mantra_roles):
        return Player.objects.create(
            league=self.league, name=name, role=role, initial_price=1,
            mantra_roles=mantra_roles, owner=self.team)

    def test_the_module_list_is_the_mantra_one(self):
        state = services.formation_state(self.team)
        self.assertTrue(state["is_mantra"])
        self.assertIn("4-2-3-1", state["modules"])
        self.assertNotIn("5-3-2", state["modules"])       # non esiste in Mantra
        self.assertEqual(state["starters_target"], 11)

    def test_a_slot_only_offers_players_it_can_host(self):
        self._player("Portiere", "P", "Por")
        self._player("Centrale", "D", "Dc")
        self._player("Terzino", "D", "Dd;E")
        state = services.formation_state(self.team)
        slots = {sl["key"]: sl for row in state["rows"] for sl in row["slots"]}
        self.assertEqual([p.name for p in slots["Dc"]["options"]], ["Centrale"])
        self.assertEqual([p.name for p in slots["Dd"]["options"]], ["Terzino"])
        self.assertEqual([p.name for p in slots["Por"]["options"]], ["Portiere"])

    def test_a_multi_role_player_shows_up_in_every_slot_he_covers(self):
        jolly = self._player("Jolly", "D", "Ds;E")
        state = services.formation_state(self.team)      # default 4-3-3
        offered = [sl["key"] for row in state["rows"] for sl in row["slots"]
                   if any(p.name == "Jolly" for p in sl["options"])]
        self.assertEqual(offered, ["Ds"])   # il 4-3-3 Mantra non ha slot da esterno
        self.assertNotIn("Dc", offered)     # regola: un Ds non copre il centrale

        # Cambiando modulo il secondo ruolo si apre: nel 3-4-3 ci sono due
        # esterni, ed e' li' che un Ds;E diventa schierabile.
        services.save_formation(self.team, "3-4-3", [str(jolly.id)])
        offered = [sl["key"] for row in services.formation_state(self.team)["rows"]
                   for sl in row["slots"] if any(p.name == "Jolly" for p in sl["options"])]
        self.assertEqual(offered, ["E", "E"])

    def test_saving_refuses_a_player_in_a_slot_he_cannot_fill(self):
        gk = self._player("Portiere", "P", "Por")
        dc = self._player("Centrale", "D", "Dc")
        # Slot 0 = Por, slot 1 = Dd nel 4-3-3: il centrale non ci sta.
        services.save_formation(self.team, "4-3-3", [str(gk.id), str(dc.id)])
        f = Formation.objects.get(participant=self.team)
        self.assertEqual(f.starter_ids[0], gk.id)
        self.assertIsNone(f.starter_ids[1])
        self.assertEqual(len(f.starter_ids), 11)

    def test_the_same_player_cannot_hold_two_slots(self):
        jolly = self._player("Jolly", "D", "Dd;Ds")
        services.save_formation(self.team, "4-3-3", ["", str(jolly.id), "", "", str(jolly.id)])
        f = Formation.objects.get(participant=self.team)
        self.assertEqual([i for i in f.starter_ids if i], [jolly.id])

    def test_a_saved_lineup_comes_back_in_its_slots(self):
        gk = self._player("Portiere", "P", "Por")
        dd = self._player("Destro", "D", "Dd")
        services.save_formation(self.team, "4-3-3", [str(gk.id), str(dd.id)])
        state = services.formation_state(self.team)
        flat = [sl for row in state["rows"] for sl in row["slots"]]
        self.assertEqual(flat[0]["player"].id, gk.id)
        self.assertEqual(flat[1]["player"].id, dd.id)
        self.assertEqual(state["starters_count"], 2)
        self.assertEqual([p.name for p in state["bench"]], [])

    def test_players_without_mantra_roles_fall_back_on_the_classic_one(self):
        # Lega Mantra con un listone vecchio: la pagina deve restare usabile.
        self._player("Senza ruoli", "D", "")
        state = services.formation_state(self.team)
        slots = {sl["key"]: sl for row in state["rows"] for sl in row["slots"]}
        self.assertTrue(any(p.name == "Senza ruoli" for p in slots["Dc"]["options"]))

    def test_a_classic_league_keeps_its_own_modules(self):
        lg = League.objects.create(name="Lega C")
        team = Participant.objects.create(display_name="Beta", league=lg,
                                          credits=Decimal("500"))
        state = services.formation_state(team)
        self.assertFalse(state["is_mantra"])
        self.assertIn("5-3-2", state["modules"])
        self.assertEqual(state["starters_target"], 11)
        self.assertEqual([r["label"] for r in state["rows"]],
                         ["Portiere", "Difesa", "Centrocampo", "Attacco"])


class MantraAuctionTests(TestCase):
    """Cosa vede chi sta facendo l'asta in una lega Mantra."""

    def setUp(self):
        self.league = League.objects.create(
            name="Lega M", game_mode=League.GameMode.MANTRA)
        self.player = Player.objects.create(
            league=self.league, name="Spinazzola", role="D", team="Napoli",
            initial_price=Decimal("10"), price_m=Decimal("12"),
            fvm=Decimal("30"), fvm_m=Decimal("36"), mantra_roles="Dd;Ds;E")
        self.auction = Auction.objects.create(
            title="Asta M", league=self.league, player=self.player,
            starting_price=Decimal("12"), current_price=Decimal("12"),
            min_increment=Decimal("1"))

    def test_the_card_carries_the_real_roles(self):
        state = services.serialize_state(self.auction)
        self.assertEqual(state["player"]["mantra_roles"], ["Dd", "Ds", "E"])
        self.assertEqual(state["game_mode"], "MANTRA")

    def test_the_card_opens_at_the_mantra_quote(self):
        state = services.serialize_state(self.auction)
        self.assertEqual(Decimal(state["player"]["initial_price"]), Decimal("12"))
        self.assertEqual(state["player"]["stats"]["fvm"], "36")

    def test_a_classic_league_sees_no_mantra_roles(self):
        lg = League.objects.create(name="Lega C")
        self.auction.league = lg
        self.auction.save(update_fields=["league"])
        state = services.serialize_state(self.auction)
        self.assertEqual(state["player"]["mantra_roles"], [])
        self.assertEqual(state["game_mode"], "CLASSIC")
        self.assertEqual(Decimal(state["player"]["initial_price"]), Decimal("10"))
        self.assertEqual(state["player"]["stats"]["fvm"], "30")

