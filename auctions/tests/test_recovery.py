"""Ripristino di una copia dal Supervisor e aste rimaste a metà."""
import sqlite3
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import django.db
import django.test
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .. import backup, services
from ..models import Auction, Participant, Player
from ..services.recovery import pending_recovery, recover_auction
from .common import make_live_auction


def _sqlite_file(path, value):
    con = sqlite3.connect(str(path))
    con.execute("create table django_migrations(id integer)")
    con.execute("create table t(x)")
    con.execute("insert into t values (?)", (value,))
    con.commit()
    con.close()


def _value(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("select x from t").fetchone()[0]
    finally:
        con.close()


class RestoreSqliteTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.live = self.dir / "db.sqlite3"
        _sqlite_file(self.live, "oggi")
        (self.dir / "backups").mkdir()
        self.snap = self.dir / "backups" / "db-20261001-200000.sqlite3"
        _sqlite_file(self.snap, "ieri")
        for target, kwargs in (
            ("auctions.backup._db_path", {"return_value": self.live}),
            ("auctions.backup._is_postgres", {"return_value": False}),
            ("django.core.management.call_command", {}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The test database must stay open: closing it would drop it.
        patcher = mock.patch.object(django.db.connections, "close_all")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_restore_puts_the_snapshot_back_and_keeps_the_current_state(self):
        safety = backup.restore_sqlite(self.snap.name)
        self.assertEqual(_value(self.live), "ieri")
        self.assertEqual(_value(safety), "oggi")
        self.assertTrue(safety.name.startswith("db-"))

    def test_corrupt_file_is_refused_and_nothing_changes(self):
        bad = self.dir / "backups" / "db-20261002-200000.sqlite3"
        bad.write_bytes(b"non un database")
        with self.assertRaises(backup.RestoreError):
            backup.restore_sqlite(bad.name)
        self.assertEqual(_value(self.live), "oggi")

    def test_only_a_snapshot_name_inside_the_folder(self):
        for name in ("../db.sqlite3", "db.sqlite3", "/etc/passwd", "", "db-x.sqlite3"):
            with self.subTest(name=name), self.assertRaises(backup.RestoreError):
                backup.restore_sqlite(name)
        self.assertEqual(_value(self.live), "oggi")


class SupervisorRestoreViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("sup", "s@x.it", "pass12345")
        self.client.force_login(self.admin)

    def test_refused_while_an_auction_is_live(self):
        make_live_auction()
        with mock.patch("auctions.backup.restore_sqlite") as restore:
            r = self.client.post("/supervisor/", {"action": "restore_backup",
                                                  "file": "db-20261001-200000.sqlite3"}, follow=True)
        restore.assert_not_called()
        self.assertContains(r, "aste in corso")

    def test_restore_reports_the_safety_copy(self):
        with mock.patch("auctions.backup._is_postgres", return_value=False), \
                mock.patch("auctions.backup.restore_sqlite",
                           return_value=Path("db-20261006-210000.sqlite3")) as restore:
            r = self.client.post("/supervisor/", {"action": "restore_backup",
                                                  "file": "db-20261001-200000.sqlite3"}, follow=True)
        restore.assert_called_once_with("db-20261001-200000.sqlite3")
        self.assertContains(r, "db-20261006-210000.sqlite3")

    def test_restore_button_offered_on_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "backups").mkdir()
            _sqlite_file(Path(tmp) / "backups" / "db-20261001-200000.sqlite3", "x")
            with mock.patch("auctions.backup._is_postgres", return_value=False), \
                    mock.patch("auctions.backup._db_path", return_value=Path(tmp) / "db.sqlite3"):
                r = self.client.get("/supervisor/?tab=reports")
        self.assertContains(r, 'value="restore_backup"')


# Un dump come quello di pg_dump 16 (servizio «backup» del compose): comandi di
# psql, SET, set_config, punti e virgola dentro gli apici e un blocco COPY.
PG_DUMP = """--
-- PostgreSQL database dump
--

\\restrict AbC123

SET statement_timeout = 0;
SET lock_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);

DROP TABLE IF EXISTS public.fm_restore_probe;
--
-- Name: fm_restore_probe; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fm_restore_probe (
    id integer NOT NULL,
    note text DEFAULT 'a; b -- non un commento' NOT NULL
);

COMMENT ON TABLE public.fm_restore_probe IS $c$prova; con $ dentro$c$;

COPY public.fm_restore_probe (id, note) FROM stdin;
1\tBoban; il 10
2\tcon -- trattini e 'apici'
\\.

--
-- PostgreSQL database dump complete
--

\\unrestrict AbC123
"""


def _write_gz(path, text, *, cut=None):
    import gzip
    data = gzip.compress(text.encode("utf-8"))
    path.write_bytes(data if cut is None else data[:cut])


class PgDumpReaderTests(TestCase):
    def test_complete_and_truncated_dumps(self):
        with tempfile.TemporaryDirectory() as tmp:
            whole, cut, no_end = Path(tmp) / "a.sql.gz", Path(tmp) / "b.sql.gz", Path(tmp) / "c.sql.gz"
            _write_gz(whole, PG_DUMP)
            _write_gz(cut, PG_DUMP, cut=60)                       # gzip ancora in scrittura
            _write_gz(no_end, PG_DUMP.split("-- PostgreSQL database dump complete")[0])
            self.assertTrue(backup.pg_dump_complete(whole))
            self.assertFalse(backup.pg_dump_complete(cut))
            self.assertFalse(backup.pg_dump_complete(no_end))

    def test_statements_and_copy_blocks(self):
        items = list(backup.iter_pg_dump(PG_DUMP.splitlines(keepends=True)))
        kinds = [i[0] for i in items]
        self.assertEqual(kinds, ["sql"] * 6 + ["copy"])
        self.assertTrue(items[0][1].startswith("SET statement_timeout"))
        self.assertIn("'a; b -- non un commento'", items[4][1])
        self.assertTrue(items[4][1].endswith(");"))
        self.assertIn("$c$prova; con $ dentro$c$", items[5][1])
        self.assertEqual(items[6][2], ["1\tBoban; il 10\n", "2\tcon -- trattini e 'apici'\n"])
        self.assertFalse(any("restrict" in i[1] for i in items))

    def test_a_cut_file_is_refused(self):
        cut = PG_DUMP.split("2\tcon")[0]
        with self.assertRaises(backup.RestoreError):
            list(backup.iter_pg_dump(cut.splitlines(keepends=True)))
        with self.assertRaises(backup.RestoreError):
            list(backup.iter_pg_dump(["CREATE TABLE x (\n", "  id integer\n"]))


class SupervisorPgRestoreTests(TestCase):
    """Il tasto «Ripristina» su PostgreSQL: il primo clic chiede la copia dello
    stato attuale, il secondo (a copia pronta) ripristina."""

    def setUp(self):
        self.admin = User.objects.create_superuser("sup_pg", "s@x.it", "pass12345")
        self.client.force_login(self.admin)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        _write_gz(self.folder / "pg-20261111-090000.sql.gz", PG_DUMP)
        patches = [mock.patch("auctions.backup._is_postgres", return_value=True),
                   mock.patch("auctions.backup._db_path", return_value=None),
                   mock.patch("auctions.backup._backup_dir", return_value=self.folder)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _post(self):
        return self.client.post("/supervisor/", {"action": "restore_backup",
                                                 "file": "pg-20261111-090000.sql.gz"}, follow=True)

    def test_first_click_asks_for_a_safety_copy_second_restores(self):
        import os
        import time
        with mock.patch("auctions.backup.restore_postgres") as restore:
            r = self._post()
            restore.assert_not_called()
            self.assertContains(r, "Passo 1 di 2")
            self.assertTrue((self.folder / backup.PG_REQUEST_FILE).exists())
            self.assertContains(r, "Ripristina ora")
            # Copia non ancora arrivata: niente ripristino.
            r = self._post()
            restore.assert_not_called()
            self.assertContains(r, "non è ancora pronta")
            # La copia è ancora in scrittura (gzip troncato): non vale.
            safety = self.folder / "pg-20261111-093000.sql.gz"
            _write_gz(safety, PG_DUMP, cut=60)
            os.utime(safety, (time.time() + 5, time.time() + 5))
            r = self._post()
            restore.assert_not_called()
            self.assertContains(r, "non è ancora pronta")
            # Il servizio backup finisce il suo pg-*.sql.gz.
            _write_gz(safety, PG_DUMP)
            os.utime(safety, (time.time() + 5, time.time() + 5))
            r = self._post()
        restore.assert_called_once_with("pg-20261111-090000.sql.gz")
        self.assertContains(r, "pg-20261111-093000.sql.gz")
        self.assertContains(r, "Database ripristinato")

    def test_admin_stays_signed_in_when_the_restore_replaces_the_sessions(self):
        import os
        import time
        from django.contrib.sessions.models import Session
        self._post()
        safety = self.folder / "pg-20261111-093000.sql.gz"
        _write_gz(safety, PG_DUMP)
        os.utime(safety, (time.time() + 5, time.time() + 5))
        # La copia è di prima del login: la sessione di chi ripristina sparisce.
        with mock.patch("auctions.backup.restore_postgres", side_effect=lambda name: Session.objects.all().delete()):
            r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.redirect_chain[-1][0], "/supervisor/?tab=reports")
        self.assertContains(r, "Database ripristinato")

    def test_button_is_offered_on_postgres(self):
        r = self.client.get("/supervisor/?tab=reports")
        self.assertContains(r, 'value="restore_backup"')
        self.assertContains(r, "il primo clic salva lo stato")


class SupervisorPgManualBackupTests(TestCase):
    """«Crea Snapshot» su PostgreSQL: l'app chiede il dump al servizio backup
    del compose e dice com'è andata, senza un «completato» che non c'è."""

    def setUp(self):
        self.admin = User.objects.create_superuser("sup_bk", "b@x.it", "pass12345")
        self.client.force_login(self.admin)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        patches = [mock.patch("auctions.backup._is_postgres", return_value=True),
                   mock.patch("auctions.backup._db_path", return_value=None),
                   mock.patch("auctions.backup._backup_dir", return_value=self.folder)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _ask(self):
        return self.client.post("/supervisor/", {"action": "manual_backup"}, follow=True)

    def _age(self, name, seconds):
        import os
        import time
        path = self.folder / name
        path.touch()
        os.utime(path, (time.time() - seconds, time.time() - seconds))
        return path

    def test_the_request_is_announced_not_reported_as_done(self):
        r = self._ask()
        self.assertTrue((self.folder / backup.PG_REQUEST_FILE).exists())
        self.assertNotContains(r, "completato con successo")
        self.assertContains(r, "Richiesta inviata al servizio backup")
        self.assertContains(r, "Backup richiesto alle")
        self.assertContains(r, "location.reload")

    def test_a_request_nobody_picks_up_says_to_start_the_service(self):
        self._age(backup.PG_REQUEST_FILE, 600)
        r = self._ask()
        self.assertContains(r, "Richiesta di backup registrata, ma")
        self.assertContains(r, "docker compose up -d backup")
        # La richiesta vecchia tiene la sua ora: resta «in attesa da troppo».
        self.assertTrue(backup.pg_backup_status()["stuck"])
        self.assertNotContains(r, "location.reload")

    def test_a_stopped_service_and_a_failing_dump_are_told_apart(self):
        self._age(backup.PG_REQUEST_FILE, 900)
        self._age(backup.PG_SERVICE_FILE, 3600)
        self.assertIn("è fermo dal", backup.pg_backup_status()["problem"])
        self._age(backup.PG_SERVICE_FILE, 30)
        self.assertIn("è acceso ma", backup.pg_backup_status()["problem"])
        (self.folder / backup.PG_ERROR_FILE).write_text("pg_dump: error: connection to server failed\n")
        r = self.client.get("/supervisor/?tab=reports")
        self.assertContains(r, "L&#x27;ultimo backup non è riuscito")
        self.assertContains(r, "connection to server failed")

    def test_a_dump_in_progress_keeps_the_page_waiting(self):
        # Il servizio ha preso la richiesta (rinominata) e sta scrivendo il dump.
        (self.folder / backup.PG_RUNNING_FILE).touch()
        r = self.client.get("/supervisor/?tab=reports")
        self.assertContains(r, "Backup in corso dalle")
        self.assertContains(r, "location.reload")
        # Un avanzo di un container ucciso non tiene la pagina in attesa per sempre.
        later = backup.pg_backup_status(now=time.time() + backup.PG_RUNNING_MAX_SECONDS + 60)
        self.assertIsNone(later["running"])

    def test_all_quiet_when_the_service_did_its_job(self):
        self._age(backup.PG_SERVICE_FILE, 30)
        status = backup.pg_backup_status()
        self.assertEqual((status["pending"], status["problem"]), (None, ""))

    def test_an_unwritable_folder_is_an_error(self):
        with mock.patch("auctions.backup.request_pg_dump", return_value=None):
            r = self._ask()
        self.assertContains(r, "non è scrivibile")


class PgRestoreDatabaseTests(django.test.TransactionTestCase):
    """Il ripristino vero, su PostgreSQL (CI «django-postgres»)."""

    def setUp(self):
        if django.db.connection.vendor != "postgresql":
            self.skipTest("Il ripristino di un dump vale solo su PostgreSQL.")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        patch = mock.patch("auctions.backup._backup_dir", return_value=self.folder)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._drop)

    def _drop(self):
        with django.db.connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS public.fm_restore_probe")

    def _dump(self, name, text):
        _write_gz(self.folder / name, text)

    def _rows(self):
        with django.db.connection.cursor() as cur:
            cur.execute("SELECT id, note FROM public.fm_restore_probe ORDER BY id")
            return cur.fetchall()

    def test_the_dump_replaces_the_table(self):
        with django.db.connection.cursor() as cur:
            cur.execute("CREATE TABLE public.fm_restore_probe (id integer, note text)")
            cur.execute("INSERT INTO public.fm_restore_probe VALUES (9, 'dopo il backup')")
        self._dump("pg-20261111-090000.sql.gz", PG_DUMP)
        backup.restore_postgres("pg-20261111-090000.sql.gz")
        self.assertEqual(self._rows(), [(1, "Boban; il 10"), (2, "con -- trattini e 'apici'")])

    def test_a_table_added_after_the_backup_does_not_block_it(self):
        # Un aggiornamento dopo il backup ha aggiunto una tabella con una chiave
        # verso una tabella del dump: il DROP del dump da solo fallirebbe.
        with django.db.connection.cursor() as cur:
            cur.execute("CREATE TABLE public.fm_restore_probe (id integer PRIMARY KEY, note text)")
            cur.execute("CREATE TABLE public.fm_restore_newer (id integer, probe_id integer "
                        "REFERENCES public.fm_restore_probe(id))")
        self.addCleanup(self._drop_newer)
        self._dump("pg-20261111-090000.sql.gz", PG_DUMP)
        backup.restore_postgres("pg-20261111-090000.sql.gz")
        self.assertEqual([r[0] for r in self._rows()], [1, 2])
        with django.db.connection.cursor() as cur:
            cur.execute("SELECT to_regclass('public.fm_restore_newer')")
            self.assertIsNone(cur.fetchone()[0])
        # Le migrazioni hanno rimesso lo schema dell'app.
        self.assertEqual(Participant.objects.count(), 0)

    def _drop_newer(self):
        with django.db.connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS public.fm_restore_newer")

    def test_a_locked_table_makes_it_give_up_not_hang(self):
        # Un'altra connessione tiene un lucchetto su una tabella: il ripristino
        # rinuncia dopo il limite invece di aspettare per sempre.
        import psycopg
        from django.conf import settings
        db = settings.DATABASES["default"]
        other = psycopg.connect(dbname=django.db.connection.settings_dict["NAME"], user=db.get("USER"),
                                password=db.get("PASSWORD"), host=db.get("HOST"), port=db.get("PORT"))
        self.addCleanup(other.close)
        other.execute("LOCK TABLE public.django_migrations IN ACCESS SHARE MODE")   # tiene la transazione aperta
        self._dump("pg-20261111-090000.sql.gz", PG_DUMP)
        with mock.patch("auctions.backup._clean_public_schema", wraps=backup._clean_public_schema) as clean, \
                mock.patch.object(backup, "_RESTORE_LOCK_TIMEOUT", "1s"):
            with self.assertRaises(backup.RestoreError) as err:
                backup.restore_postgres("pg-20261111-090000.sql.gz")
        clean.assert_called_once()
        self.assertIn("il database non è cambiato", str(err.exception))

    def test_an_incomplete_dump_is_refused(self):
        _write_gz(self.folder / "pg-20261111-090000.sql.gz",
                  PG_DUMP.split("-- PostgreSQL database dump complete")[0])
        with self.assertRaises(backup.RestoreError) as err:
            backup.restore_postgres("pg-20261111-090000.sql.gz")
        self.assertIn("incompleto", str(err.exception))

    def test_a_failing_dump_changes_nothing(self):
        with django.db.connection.cursor() as cur:
            cur.execute("CREATE TABLE public.fm_restore_probe (id integer, note text)")
            cur.execute("INSERT INTO public.fm_restore_probe VALUES (9, 'prima')")
        broken = PG_DUMP.replace("COMMENT ON TABLE", "COMMENTO SBAGLIATO ON TABLE")
        self._dump("pg-20261111-090000.sql.gz", broken)
        with self.assertRaises(backup.RestoreError) as err:
            backup.restore_postgres("pg-20261111-090000.sql.gz")
        self.assertIn("il database non è cambiato", str(err.exception))
        self.assertEqual(self._rows(), [(9, "prima")])     # il DROP è stato annullato


@override_settings(BID_MIN_INTERVAL_MS=0)
class StalledAuctionTests(TestCase):
    def setUp(self):
        self.player = Player.objects.create(name="Osimhen", role="A")
        self.team = Participant.objects.create(display_name="Alfa", credits=Decimal("1000"))
        self.auction = make_live_auction(player=self.player, flow_mode=Auction.FlowMode.CALL,
                                         cycle_break_seconds=0)
        services.place_bid(self.auction.id, self.team.id, 50)
        # Server stopped mid-lot: the lot expired a minute ago, nobody closed it.
        Auction.objects.filter(pk=self.auction.id).update(
            ends_at=timezone.now() - timedelta(minutes=1))

    def test_an_expired_lot_nobody_closed_is_listed(self):
        found = pending_recovery()
        self.assertEqual([(a.id, label) for a, label in found],
                         [(self.auction.id, "Lotto scaduto e mai chiuso")])

    def test_an_auction_a_ticker_is_running_is_not_listed(self):
        self.assertEqual(pending_recovery(exclude_ids=[self.auction.id]), [])

    def test_a_lot_still_running_is_not_listed(self):
        Auction.objects.filter(pk=self.auction.id).update(
            ends_at=timezone.now() + timedelta(seconds=30))
        self.assertEqual(pending_recovery(), [])

    def test_recover_closes_charges_and_moves_on(self):
        auction = recover_auction(self.auction.id)
        self.player.refresh_from_db()
        self.team.refresh_from_db()
        self.assertEqual(self.player.owner_id, self.team.id)
        self.assertEqual(self.team.spent_credits, Decimal("150"))
        self.assertEqual(auction.status, Auction.Status.LIVE)
        self.assertIsNone(auction.player_id)
        self.assertEqual(pending_recovery(), [])
        # Running it again charges nobody twice.
        recover_auction(self.auction.id)
        self.team.refresh_from_db()
        self.assertEqual(self.team.spent_credits, Decimal("150"))

    def test_expired_envelopes_are_opened(self):
        Auction.objects.filter(pk=self.auction.id).update(
            sealed_bids=True, sealed_round=1, sealed_floor=Decimal("151"),
            sealed_ends_at=timezone.now() - timedelta(minutes=1), ends_at=None)
        found = pending_recovery()
        self.assertEqual(found[0][1], "Buste scadute da aprire")
        recover_auction(self.auction.id)
        self.auction.refresh_from_db()
        self.assertFalse(self.auction.sealed_open)

    def test_supervisor_lists_and_recovers(self):
        admin = User.objects.create_superuser("sup2", "s2@x.it", "pass12345")
        self.client.force_login(admin)
        r = self.client.get("/supervisor/?tab=health")
        self.assertContains(r, "Aste rimaste a metà")
        self.assertContains(r, "Lotto scaduto e mai chiuso")
        r = self.client.post("/supervisor/", {"action": "recover_auction",
                                              "auction_id": self.auction.id})
        self.assertEqual(r.status_code, 302)
        self.player.refresh_from_db()
        self.assertEqual(self.player.owner_id, self.team.id)
