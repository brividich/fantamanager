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

class ProviderTests(TestCase):
    """Fantapazz provider parsing + DB importers on mock/local data (no network)."""

    MOCK_ROSA = (
        '<div class="nome-squadra">GELSI UNITED</div>'
        '<span class="credito-residuo">2168</span>'
        '<div class="card-calciatore" id="1" ID_Ruolo="1" quotazione="7">'
        '<div class="nomeCalciatore">Falcone</div><div class="nomeClub">Lecce</div><div class="costo">9</div></div>'
        '<div class="card-calciatore" id="2" ID_Ruolo="4" quotazione="30">'
        '<div class="nomeCalciatore">Lautaro</div><div class="nomeClub">Inter</div><div class="costo">120</div></div>'
    )

    def test_parse_rosters_mock(self):
        from ..providers import get_provider
        provider = get_provider("fantapazz")
        parsed = provider.parse_rosters(self.MOCK_ROSA)
        self.assertEqual(parsed["fantapazz_team"], "GELSI UNITED")
        self.assertEqual(parsed["remaining_credits"], "2168")
        self.assertEqual(len(parsed["players"]), 2)
        self.assertEqual(parsed["players"][0]["name"], "Falcone")
        # Fase D fix: ID_Ruolo is read case-insensitively, so roles are no
        # longer all collapsed to "A" by html.parser lowercasing the attribute.
        self.assertEqual(parsed["players"][0]["role"], "P")  # ID_Ruolo="1"
        self.assertEqual(parsed["players"][1]["role"], "A")  # ID_Ruolo="4"

    def test_get_provider_unknown_raises(self):
        from ..providers import ProviderError, get_provider
        with self.assertRaises(ProviderError):
            get_provider("does-not-exist")

    def test_import_rose_data_reconstructs_budget(self):
        from ..providers import importers
        teams = [{
            "name": "GELSI UNITED", "credits": 100, "external_id": "1449",
            "players": [
                {"role": "P", "name": "Falcone", "cost": 9,   "club": "Lecce"},
                {"role": "A", "name": "Lautaro", "cost": 120, "club": "Inter"},
            ],
        }]
        result = importers.import_rose_data(teams, replace=True)
        self.assertEqual(result["players"], 2)
        p = Participant.objects.get(display_name="GELSI UNITED")
        self.assertEqual(p.spent_credits, Decimal("129"))      # 9 + 120
        self.assertEqual(p.credits, Decimal("229"))            # 100 remaining + 129 spent
        self.assertEqual(p.remaining_credits, Decimal("100"))  # matches source site
        self.assertEqual(p.external_team_id, "1449")
        self.assertEqual(Player.objects.filter(owner=p).count(), 2)

    def test_listone_then_rose_keeps_free_agents(self):
        # Regression: load the Quotazioni (listone) first, then a roster file.
        # The roster only assigns owners onto the pool — it must NOT wipe the
        # listone, or the auction is left with no svincolati to call.
        from ..providers import importers
        importers.sync_players([
            {"name": "Falcone", "role": "P", "team": "Lecce", "price": Decimal("9")},
            {"name": "Lautaro", "role": "A", "team": "Inter", "price": Decimal("100")},
            {"name": "Vlahovic", "role": "A", "team": "Juventus", "price": Decimal("90")},
        ], replace=True)

        teams = [{
            "name": "GELSI UNITED", "credits": 100,
            "players": [{"role": "A", "name": "Lautaro", "cost": 120, "club": "Inter"}],
        }]
        # Even with replace=True the listone pool survives.
        importers.import_rose_data(teams, replace=True)

        # Lautaro is now owned, the other two remain free agents (svincolati).
        self.assertEqual(Player.objects.count(), 3)
        owned = Player.objects.get(name="Lautaro")
        self.assertIsNotNone(owned.owner_id)
        free = Player.objects.filter(owner__isnull=True).order_by("name")
        self.assertEqual([p.name for p in free], ["Falcone", "Vlahovic"])
        # Lautaro keeps its listone quotazione as the auction base price.
        self.assertEqual(owned.initial_price, Decimal("100"))

    def test_rose_matches_listone_fuzzily_without_duplicates(self):
        # "Bastoni A." in a roster must map onto listone "Bastoni", not create a
        # duplicate that would also linger as a phantom free agent.
        from ..providers import importers
        importers.sync_players(
            [{"name": "Bastoni", "role": "D", "team": "Inter", "price": Decimal("20")}],
            replace=True,
        )
        importers.import_rose_data([{
            "name": "TEAM", "credits": 100,
            "players": [{"role": "D", "name": "Bastoni A.", "cost": 25, "club": "INT"}],
        }])
        self.assertEqual(Player.objects.count(), 1)          # no duplicate
        self.assertIsNotNone(Player.objects.get().owner_id)  # assigned

    def test_import_players_simple_skips_short_names(self):
        from ..providers import importers
        created = importers.import_players_simple(
            [{"name": "Vlahovic", "role": "A"}, {"name": "X", "role": "A"}, {"name": "", "role": "D"}],
            replace=True,
        )
        self.assertEqual(created, 1)
        self.assertTrue(Player.objects.filter(name="Vlahovic").exists())

    def test_parse_rose_file_detects_flat_fantacalcio_format(self):
        # The Fantacalcio.it "Lista calciatori" flat export: one row per player,
        # owner in 'FantaSquadra', the plain role in 'R.' (never 'R.MANTRA'),
        # and a 'QUOT.' column so the same file also rebuilds the listone.
        import io
        from ..providers import importers
        csv_data = (
            "#,Nome,Sq.,R.,R.MANTRA,QUOT.,FantaSquadra,Costo\n"
            "1,Lautaro,Inter,A,Pc,34,Loco T,271\n"
            "2,Falcone,Lecce,P,Por,5,Loco T,9\n"
            "3,Vlahovic,Juventus,A,Pc,26,,\n"        # svincolato: no FantaSquadra
        ).encode("utf-8")
        teams, listone, meta = importers.parse_rose_file(io.BytesIO(csv_data), "rose.csv")
        self.assertEqual(meta["source"], "fantacalcio")
        self.assertEqual(meta["n_teams"], 1)
        self.assertEqual(meta["n_players"], 2)        # Vlahovic excluded (free agent)
        self.assertEqual(len(listone), 3)             # but kept in the listone
        team = teams[0]
        self.assertEqual(team["name"], "Loco T")
        self.assertIsNone(team["credits"])            # flat export carries no residui
        self.assertEqual({p["name"] for p in team["players"]}, {"Lautaro", "Falcone"})
        self.assertEqual({p["role"] for p in team["players"]}, {"A", "P"})  # 'R.', not Mantra

    def test_parse_rose_file_rejects_table_without_owner_column(self):
        # A bare listone (no owner column) is not a roster — it must be refused
        # so it isn't silently imported as zero teams.
        import io
        from ..providers import importers
        csv_data = b"Nome,Ruolo,Squadra,Quotazione\nLautaro,A,Inter,34\n"
        with self.assertRaises(ValueError):
            importers.parse_rose_file(io.BytesIO(csv_data), "listone.csv")

    def test_parse_rose_file_id_based_leghe_fantacalcio_format(self):
        """The "$,$,$"-delimited, Id-only export — verified against a real
        download, the exact shape auctions.exporters.build_leghe_csv writes
        back to it. Player identity resolves entirely through Player.ext_id
        against the league's already-imported listone."""
        import io
        from ..providers import importers
        league = League.objects.create(name="Lega", budget=Decimal("500"))
        Player.objects.create(name="Lautaro", role="A", team="Inter",
                              league=league, ext_id="7212")
        Player.objects.create(name="Falcone", role="P", team="Lecce",
                              league=league, ext_id="7155")
        csv_data = (
            "$,$,$\nDinamo Losca,7212,6\nDinamo Losca,7155,5\n"
            "$,$,$\nSPORTING PASSOA,9999,10\n"   # 9999 not in this league's pool
        ).encode("utf-8")

        teams, listone, meta = importers.parse_rose_file(
            io.BytesIO(csv_data), "rose.csv", league=league,
        )
        self.assertEqual(meta["source"], "leghe_fantacalcio_id")
        self.assertIsNone(listone)   # no name column at all — no listone to rebuild
        self.assertEqual(meta["n_teams"], 1)          # SPORTING PASSOA has no matched player
        self.assertEqual(meta["unmatched"], [("SPORTING PASSOA", "9999", Decimal("10"))])

        team = teams[0]
        self.assertEqual(team["name"], "Dinamo Losca")
        self.assertEqual(
            {(p["name"], p["role"], p["club"], p["cost"]) for p in team["players"]},
            {("Lautaro", "A", "Inter", Decimal("6")), ("Falcone", "P", "Lecce", Decimal("5"))},
        )

    def test_parse_rose_file_id_based_scoped_to_the_right_league(self):
        """Two leagues can legitimately reuse the same official Id for
        different (or the same) players — matching must never cross leagues,
        so a player that only exists in another league's pool is unmatched
        here, not silently borrowed from it."""
        import io
        from ..providers import importers
        league_a = League.objects.create(name="A", budget=Decimal("500"))
        league_b = League.objects.create(name="B", budget=Decimal("500"))
        Player.objects.create(name="Lautaro", role="A", team="Inter",
                              league=league_a, ext_id="7212")
        # league_b has a *different* player under the same official Id —
        # proves the match resolves per-league, not by Id alone.
        Player.objects.create(name="Someone Else", role="D", team="Roma",
                              league=league_b, ext_id="7212")
        csv_data = "$,$,$\nTeam,7212,6\n".encode("utf-8")

        teams, _listone, meta = importers.parse_rose_file(
            io.BytesIO(csv_data), "rose.csv", league=league_b,
        )
        self.assertEqual(meta["unmatched"], [])
        self.assertEqual(teams[0]["players"][0]["name"], "Someone Else")

    def test_parse_rose_file_raises_when_nothing_matches_at_all(self):
        import io
        from ..providers import importers
        league = League.objects.create(name="Lega", budget=Decimal("500"))
        csv_data = "$,$,$\nTeam,123,6\n".encode("utf-8")
        with self.assertRaises(ValueError):
            importers.parse_rose_file(io.BytesIO(csv_data), "rose.csv", league=league)

    def test_parse_rose_file_id_format_without_league_falls_through(self):
        """No league to resolve official Ids against — falls back to the
        flat-roster reader like before, rather than crashing or silently
        returning nothing."""
        import io
        from ..providers import importers
        csv_data = "$,$,$\nTeam,123,6\n".encode("utf-8")
        with self.assertRaises(ValueError):  # not a recognisable flat table either
            importers.parse_rose_file(io.BytesIO(csv_data), "rose.csv")

    def test_admin_import_rose_view_id_based_end_to_end(self):
        """The full HTTP round trip for the Leghe Fantacalcio Id-based export:
        preview reports the unmatched-Id warning, and the real import assigns
        ownership + cost using the league's own listone."""
        import io
        user = User.objects.create_superuser("admin", "a@b.c", "pass12345")
        self.client.force_login(user)
        league = League.objects.create(name="Lega", budget=Decimal("500"))
        Player.objects.create(name="Lautaro", role="A", team="Inter",
                              league=league, ext_id="7212")
        csv_data = (
            "$,$,$\nDinamo Losca,7212,6\n"
            "$,$,$\nSPORTING PASSOA,9999,10\n"
        ).encode("utf-8")

        upload = io.BytesIO(csv_data); upload.name = "rose.csv"
        preview = self.client.post("/admin-auction/rose/import/", {
            "rose_file": upload, "action": "preview", "league_id": str(league.id),
        })
        self.assertEqual(preview.status_code, 200)
        body = preview.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"], "leghe_fantacalcio_id")
        self.assertIn("1 giocatori", body["warning"])
        self.assertIn("SPORTING PASSOA", body["warning"])

        upload2 = io.BytesIO(csv_data); upload2.name = "rose.csv"
        result = self.client.post("/admin-auction/rose/import/", {
            "rose_file": upload2, "league_id": str(league.id),
        })
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["ok"])

        lautaro = Player.objects.get(name="Lautaro")
        team = Participant.objects.get(display_name="Dinamo Losca", league=league)
        self.assertEqual(lautaro.owner_id, team.id)
        self.assertEqual(lautaro.cost, Decimal("6"))

    def test_import_rose_data_uses_default_budget_when_no_remaining(self):
        # The flat export gives each player's cost but not the team budget, so
        # default_budget becomes the team total: remaining = budget - spent.
        from ..providers import importers
        teams = [{"name": "Loco T", "credits": None,
                  "players": [{"role": "A", "name": "Lautaro", "cost": 271, "club": "Inter"}]}]
        importers.import_rose_data(teams, default_budget=Decimal("500"))
        p = Participant.objects.get(display_name="Loco T")
        self.assertEqual(p.credits, Decimal("500"))
        self.assertEqual(p.spent_credits, Decimal("271"))
        self.assertEqual(p.remaining_credits, Decimal("229"))


class LeaguePlayerScopeTests(TestCase):
    """Fase J — each league owns its own listone + queue (per-league pools)."""

    def setUp(self):
        from ..providers import importers
        self.importers = importers
        self.la = League.objects.create(name="A")
        self.lb = League.objects.create(name="B")

    def test_sync_players_is_scoped_per_league(self):
        self.importers.sync_players(
            [{"name": "Vlahovic", "role": "A", "team": "Juventus", "price": 30}],
            league=self.la,
        )
        self.importers.sync_players(
            [{"name": "Osimhen", "role": "A", "team": "Napoli", "price": 28}],
            league=self.lb,
        )
        self.assertEqual(Player.objects.filter(league=self.la).count(), 1)
        self.assertEqual(Player.objects.filter(league=self.lb).count(), 1)
        self.assertEqual(Player.objects.get(league=self.la).name, "Vlahovic")
        # Same listone imported into B again touches only B's pool.
        self.importers.sync_players(
            [{"name": "Vlahovic", "role": "A", "team": "Juventus", "price": 31}],
            league=self.lb,
        )
        self.assertEqual(Player.objects.filter(league=self.la).count(), 1)
        self.assertEqual(Player.objects.filter(league=self.lb).count(), 2)

    def test_build_queue_only_sees_its_league_pool(self):
        Player.objects.create(name="Aaa", role="A", league=self.la, owner=None)
        Player.objects.create(name="Bbb", role="A", league=self.lb, owner=None)
        auction = Auction.objects.create(
            league=self.la, title="Asta A", flow_mode=Auction.FlowMode.CONTINUOUS,
            call_order=Auction.CallOrder.ALPHA, status=Auction.Status.READY,
        )
        n = services.build_queue(auction)
        self.assertEqual(n, 1)
        names = list(auction.queue_items.values_list("player__name", flat=True))
        self.assertEqual(names, ["Aaa"])   # never sees league B's "Bbb"


class ListoneSyncTests(TestCase):
    """Reconcile the official listone against the existing player pool."""

    def setUp(self):
        from ..providers import importers
        self.importers = importers
        self.team = Participant.objects.create(display_name="Squadra A")

    def _row(self, name, role, team, price=10):
        return {"name": name, "role": role, "team": team, "price": price}

    def test_owned_player_matched_not_duplicated(self):
        # Roster spelling differs from the listone (bare surname vs initial).
        owned = Player.objects.create(
            name="Bastoni A.", role="D", team="INT",
            owner=self.team, cost=Decimal("18"), initial_price=Decimal("18"),
        )
        report = self.importers.sync_players([self._row("Bastoni", "D", "Inter", 20)])

        owned.refresh_from_db()
        self.assertEqual(Player.objects.filter(team="Inter").count(), 1)  # no dup
        self.assertEqual(owned.owner_id, self.team.id)                    # still owned
        self.assertEqual(owned.cost, Decimal("18"))                       # cost kept
        self.assertEqual(owned.name, "Bastoni")                           # canonicalised
        self.assertEqual(owned.initial_price, Decimal("20"))              # quota refreshed
        self.assertEqual(report["matched_owned"], 1)
        self.assertEqual(report["created"], 0)

    def test_new_player_created_as_free_agent(self):
        report = self.importers.sync_players([self._row("Carnesecchi", "P", "Atalanta", 18)])
        p = Player.objects.get(name="Carnesecchi")
        self.assertIsNone(p.owner_id)
        self.assertEqual(report["created"], 1)

    def test_same_surname_same_club_not_swapped(self):
        # Two Inter "Martinez": the goalkeeper (J.) and Lautaro (L.).
        keeper = Player.objects.create(name="Martinez J.", role="P", team="INT", owner=self.team)
        lautaro = Player.objects.create(name="Martinez L.", role="A", team="INT", owner=self.team)
        self.importers.sync_players([
            self._row("Martinez Jo.", "P", "Inter", 1),
            self._row("Martinez L.", "A", "Inter", 33),
        ])
        keeper.refresh_from_db(); lautaro.refresh_from_db()
        self.assertEqual(keeper.name, "Martinez Jo.")   # J. -> Jo., not L.
        self.assertEqual(keeper.role, "P")
        self.assertEqual(lautaro.name, "Martinez L.")
        self.assertEqual(lautaro.role, "A")
        self.assertEqual(Player.objects.filter(team="Inter").count(), 2)  # no dup

    def test_accent_apostrophe_and_word_order(self):
        a = Player.objects.create(name="Ndicka", role="D", team="ROM", owner=self.team)
        b = Player.objects.create(name="Anguissa", role="C", team="NAP", owner=self.team)
        report = self.importers.sync_players([
            self._row("N'Dicka", "D", "Roma", 12),
            self._row("Zambo Anguissa", "C", "Napoli", 16),
        ])
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["matched_owned"], 2)

    def test_prune_removes_departed_free_agents_only(self):
        stale_free = Player.objects.create(name="Vecchio", role="C", team="OLD")
        owned_gone = Player.objects.create(name="Menke", role="P", team="COM", owner=self.team)
        report = self.importers.sync_players(
            [self._row("Carnesecchi", "P", "Atalanta", 18)], prune=True,
        )
        self.assertFalse(Player.objects.filter(pk=stale_free.pk).exists())  # pruned
        self.assertTrue(Player.objects.filter(pk=owned_gone.pk).exists())   # kept
        self.assertEqual(report["pruned"], 1)
        self.assertIn("Menke", report["owned_not_in_listone"])

    def test_player_on_block_not_pruned(self):
        free = Player.objects.create(name="SulPiatto", role="A", team="XYZ")
        make_live_auction(player=free)
        self.importers.sync_players([], prune=True)
        self.assertTrue(Player.objects.filter(pk=free.pk).exists())

    def test_replace_wipes_everything(self):
        Player.objects.create(name="Bastoni A.", role="D", team="INT", owner=self.team)
        report = self.importers.sync_players(
            [self._row("Carnesecchi", "P", "Atalanta", 18)], replace=True,
        )
        self.assertEqual(Player.objects.count(), 1)
        self.assertEqual(Player.objects.first().name, "Carnesecchi")
        self.assertEqual(report["created"], 1)

