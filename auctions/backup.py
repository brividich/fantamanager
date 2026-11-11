"""Timestamped, rotated snapshots of the live SQLite database.

Used both by the launcher (run_app.py, once at startup and once on a clean
quit) and by the app itself while an auction is running — see the
``transaction.on_commit`` calls in services.py and the periodic tick in
consumers.py. A single "Termina asta" click, or the running order simply
running out, doesn't happen often enough on its own to protect a session
that can run for hours, so the app also backs itself up every few minutes
while something is actually LIVE.

Runs off sqlite3's own online backup API rather than a plain file copy, so a
snapshot taken while WAL journal entries are still unflushed is still a
consistent copy.

On PostgreSQL (the Docker deployment) the dumps are the ``backup`` service's
job in docker-compose: pg_dump of the server's own version, every few hours
and within a minute of a request. The app only asks, when something worth a
snapshot of its own happens (an auction ending), by leaving a file in the
shared backups folder; the periodic ticker asks nothing, the service already
keeps its own schedule.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PERIODIC = "periodico"          # the live ticker's reason: see consumers.RoomTicker
PG_REQUEST_FILE = ".richiesta"  # watched by the backup service in docker-compose
PG_SERVICE_FILE = ".servizio"   # touched by the backup service at every round
PG_ERROR_FILE = ".errore"       # what the last failed pg_dump said (gone on success)
# The service looks for a request every minute: one still there after three
# means nobody is picking it up. Its heartbeat is at most ten minutes apart
# (the wait after a failed dump).
PG_REQUEST_STALE_SECONDS = 180
PG_SERVICE_STALE_SECONDS = 12 * 60
# While it dumps, the service keeps the request as .richiesta.in-corso (back
# to .richiesta if the dump fails, gone when it succeeds). One left there for
# longer than this is a leftover of a killed container, not a dump running.
PG_RUNNING_FILE = ".richiesta.in-corso"
PG_RUNNING_MAX_SECONDS = 30 * 60

_bg_lock = threading.Lock()
_last_bg_backup = 0.0
_last_periodic_backup = 0.0
BG_MIN_INTERVAL_SECONDS = 60  # never more than once a minute, however often triggered


def _db_path():
    """The sqlite file backing the default database, or None (not sqlite)."""
    from django.conf import settings
    default = settings.DATABASES.get("default", {})
    if "sqlite3" not in default.get("ENGINE", ""):
        return None  # e.g. Postgres — that's its own backup story, not this one
    name = default.get("NAME")
    return Path(str(name)) if name else None


def _is_postgres():
    from django.conf import settings
    return "postgresql" in settings.DATABASES.get("default", {}).get("ENGINE", "")


def _backup_dir():
    """Where the PostgreSQL dumps land (./backups in docker-compose)."""
    from django.conf import settings
    return Path(settings.BACKUP_DIR)


def request_pg_dump(reason=""):
    """Ask the backup service for a PostgreSQL dump now. Returns the request
    file, or None when it could not be written (logged, never raised).

    A request already waiting keeps its time: that is how
    :func:`pg_backup_status` tells a service that never picks requests up."""
    path = _backup_dir() / PG_REQUEST_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            waiting = path.stat()
        except OSError:
            waiting = None
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{reason or 'richiesta'}\n")
        if waiting is not None:
            os.utime(path, (waiting.st_atime, waiting.st_mtime))
    except OSError:
        logger.exception("could not request a database dump (%s)", reason or "no reason given")
        return None
    return path


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def pg_backup_status(now=None):
    """What the backups folder says about the compose backup service:
    ``{"pending", "running", "stuck", "seen", "error", "error_at", "problem"}``.

    ``pending`` is when the request still waiting was made (None: nothing
    waiting), ``running`` when the dump now in progress started, ``stuck``
    whether the waiting request has waited too long, ``seen`` the service's
    last heartbeat (None with a compose from before the heartbeat, or a
    service that never ran), ``error`` what the last failed dump said, and
    ``problem`` the sentence for the Supervisor, or "" when all is well."""
    folder = _backup_dir()
    now = time.time() if now is None else now
    pending = _mtime(folder / PG_REQUEST_FILE)
    running_at = None
    try:
        # ctime: when the service renamed it, i.e. when the dump started.
        running_at = (folder / PG_RUNNING_FILE).stat().st_ctime
    except OSError:
        pass
    running = running_at is not None and now - running_at < PG_RUNNING_MAX_SECONDS
    seen = _mtime(folder / PG_SERVICE_FILE)
    error_at = _mtime(folder / PG_ERROR_FILE)
    error = ""
    if error_at is not None:
        try:
            error = (folder / PG_ERROR_FILE).read_text(encoding="utf-8", errors="replace")
        except OSError:
            error = ""
        error = " ".join(error.split())[:300]

    def when(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    stuck = pending is not None and now - pending > PG_REQUEST_STALE_SECONDS
    status = {
        "pending": when(pending) if pending is not None else None,
        "running": when(running_at) if running else None,
        "stuck": stuck,
        "seen": when(seen) if seen is not None else None,
        "error": error,
        "error_at": when(error_at) if error_at is not None else None,
        "problem": "",
    }

    def hhmm(ts):
        from django.utils import timezone as dj_tz
        return dj_tz.localtime(when(ts)).strftime("%d/%m %H:%M")

    logs = "docker compose logs backup"
    if error_at is not None:
        status["problem"] = (
            f"L'ultimo backup non è riuscito ({hhmm(error_at)}): «{error or 'nessun dettaglio'}». "
            f"Il servizio backup riprova da solo ogni 10 minuti; per capire cosa non va: {logs}.")
    elif stuck and seen is None:
        status["problem"] = (
            f"Il servizio backup non ha ancora risposto alla richiesta del {hhmm(pending)}. "
            "Controlla che il container fantamanager-backup esista e sia acceso "
            "(docker compose up -d backup). Su Synology: aggiorna il progetto con il "
            "docker-compose.yml nuovo, che contiene il servizio backup, e ricompilalo.")
    elif stuck and now - seen > PG_SERVICE_STALE_SECONDS:
        status["problem"] = (
            f"Il servizio backup è fermo dal {hhmm(seen)}: la richiesta del {hhmm(pending)} è ancora in "
            f"attesa. Riavvialo (docker compose up -d backup) e guarda cosa dice: {logs}.")
    elif stuck:
        status["problem"] = (
            f"Il servizio backup è acceso ma non ha ancora eseguito la richiesta del {hhmm(pending)}: "
            f"guarda cosa dice ({logs}).")
    return status


def latest_backup():
    """The newest backup on disk, for the Supervisor: ``{"name", "at", "count",
    "folder"}``, or None when there is none — the pg-*.sql.gz dumps on
    PostgreSQL, the db-*.sqlite3 snapshots on SQLite."""
    if _is_postgres():
        folder, pattern = _backup_dir(), "pg-*.sql.gz"
    else:
        src = _db_path()
        if src is None:
            return None
        folder, pattern = src.parent / "backups", "db-*.sqlite3"
    try:
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    if not files:
        return None
    newest = files[0]
    return {
        "name": newest.name,
        "at": datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc),
        "count": len(files),
        "folder": str(folder),
    }


# What is kept of the SQLite snapshots. The live ticker writes one every few
# minutes: with a single "newest 10" rule those pushed the snapshot from before
# the auction out within the hour, so an assignment gone wrong and noticed late
# was in every copy left. Three tiers instead:
#   - automatic (periodic) snapshots: the newest ``keep``;
#   - snapshots of an event (startup, an auction ending, a manual one): the
#     newest KEEP_EVENTS;
#   - whatever the tier, the newest snapshot of each of the last KEEP_DAYS days.
AUTO_TAG = "-auto"
KEEP_EVENTS = 20
KEEP_DAYS = 7


def _is_auto(path):
    return path.stem.endswith(AUTO_TAG)


def _prune(backups_dir, *, keep, keep_events=KEEP_EVENTS, keep_days=KEEP_DAYS):
    try:
        files = sorted(backups_dir.glob("db-*.sqlite3"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    protected = set()
    days = []
    for f in files:                       # newest first: first seen = newest of its day
        try:
            day = datetime.fromtimestamp(f.stat().st_mtime).date()
        except OSError:
            continue
        if day not in days:
            days.append(day)
            if len(days) <= keep_days:
                protected.add(f)
    auto = [f for f in files if _is_auto(f)]
    events = [f for f in files if not _is_auto(f)]
    for old in auto[keep:] + events[keep_events:]:
        if old in protected:
            continue
        try:
            old.unlink()
        except OSError:
            pass


def _unchanged_since_last_backup(src_path, backups_dir):
    """True when nothing was written to the database since the newest
    snapshot: a screen left open overnight keeps the ticker alive, and copies
    identical to the last one only rotate the useful ones away."""
    try:
        newest = max((p.stat().st_mtime for p in backups_dir.glob("db-*.sqlite3")), default=None)
    except OSError:
        return False
    if newest is None:
        return False
    written = 0.0
    for candidate in (src_path, src_path.with_name(src_path.name + "-wal")):
        try:
            written = max(written, candidate.stat().st_mtime)
        except OSError:
            pass
    return written <= newest


def backup_database(*, keep=10, reason=""):
    """Blocking snapshot — call this from a background thread (see
    :func:`backup_database_async`), never inline on a request or websocket
    path: sqlite3's backup API does real disk IO and must not add latency to
    a bid, a lot closing, or anything else someone is waiting on.

    ``keep`` is how many automatic (``PERIODIC``) snapshots stay; event
    snapshots and one per day are kept apart (see ``_prune``).

    Returns the path written, or None if there was nothing to back up / it
    failed (logged, never raised — a failed backup must not take the
    request or the ticker down with it).
    """
    src_path = _db_path()
    if src_path is None:
        if _is_postgres() and reason != PERIODIC:
            return request_pg_dump(reason)
        return None
    if not src_path.exists():
        return None

    backups_dir = src_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    periodic = reason == PERIODIC
    if periodic and _unchanged_since_last_backup(src_path, backups_dir):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = AUTO_TAG if periodic else ""
    dst_path = backups_dir / f"db-{stamp}{tag}.sqlite3"
    n = 1
    while dst_path.exists():  # two backups within the same second
        n += 1
        dst_path = backups_dir / f"db-{stamp}-{n}{tag}.sqlite3"

    try:
        import sqlite3
        src = sqlite3.connect(str(src_path))
        try:
            dst = sqlite3.connect(str(dst_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception:
        logger.exception("database backup failed (%s)", reason or "no reason given")
        return None

    _prune(backups_dir, keep=keep)
    return dst_path


def backup_database_async(*, keep=10, reason="", min_interval=BG_MIN_INTERVAL_SECONDS):
    """Fire-and-forget: runs the (blocking) snapshot on a daemon thread so a
    caller on the request/websocket path never waits on disk IO.

    Coalesces bursts behind ``min_interval`` — several lots can close back
    to back, and every connected client's ticker polls independently — so
    this can be called liberally without spawning a thread per event.
    """
    global _last_bg_backup, _last_periodic_backup
    now = time.monotonic()
    # Periodic and event snapshots are throttled apart: the ticker's routine
    # copy taken a few seconds earlier must not swallow the one of an auction
    # starting or ending.
    with _bg_lock:
        if reason == PERIODIC:
            if now - _last_periodic_backup < min_interval:
                return
            _last_periodic_backup = now
        else:
            if now - _last_bg_backup < min_interval:
                return
            _last_bg_backup = now
    threading.Thread(
        target=backup_database, kwargs={"keep": keep, "reason": reason}, daemon=True,
    ).start()


def backups_folder():
    """Where the snapshots of this installation live (None: nothing to list)."""
    if _is_postgres():
        return _backup_dir()
    src = _db_path()
    return None if src is None else src.parent / "backups"


def backup_file(name):
    """The backup called ``name`` in the backups folder, or None. Only a bare
    file name of a snapshot or dump is accepted: nothing outside the folder."""
    folder = backups_folder()
    name = (name or "").strip()
    if folder is None or not name or Path(name).name != name:
        return None
    if not (name.startswith("db-") and name.endswith(".sqlite3")) and \
            not (name.startswith("pg-") and name.endswith(".sql.gz")):
        return None
    path = (folder / name).resolve()
    try:
        path.relative_to(folder.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _intact(path):
    """Un file SQLite di FantaManager che si apre e passa il controllo veloce."""
    import sqlite3
    try:
        con = sqlite3.connect(str(path), timeout=30)
        try:
            ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            return ok and con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='django_migrations'"
            ).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def repair_at_startup():
    """All'avvio dell'app del PC, prima di tutto il resto: il database è integro?

    Uno spegnimento brusco, un disco che si guasta o un programma che tocca il
    file possono rovinarlo, e l'asta non partirebbe. Se è rovinato lo mette da
    parte nella cartella dei salvataggi (``danneggiato-<data>.sqlite3``, mai
    cancellato) e rimette l'ultima copia integra. Ritorna il messaggio per chi
    apre l'app, o None se era tutto a posto (o non è SQLite).
    """
    import shutil
    import sqlite3
    src = _db_path()
    if src is None or not src.exists() or _intact(src):
        return None
    folder = src.parent / "backups"
    try:
        copies = sorted(folder.glob("db-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        copies = []
    good = next((c for c in copies if _intact(c)), None)
    if good is None:
        logger.error("database danneggiato e nessuna copia integra in %s", folder)
        return ("Il database di FantaManager risulta danneggiato e non c'è una copia integra "
                f"da rimettere. Non ho toccato niente: il file è {src}. "
                "Se l'app non funziona, scrivi a chi te l'ha installata.")
    folder.mkdir(parents=True, exist_ok=True)
    aside = folder / f"danneggiato-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        part = src.with_name(src.name + suffix)
        if part.exists():
            shutil.move(str(part), str(aside.with_name(aside.name + suffix)))
    con = sqlite3.connect(str(good))
    try:
        dst = sqlite3.connect(str(src))
        try:
            con.backup(dst)
        finally:
            dst.close()
    finally:
        con.close()
    when = datetime.fromtimestamp(good.stat().st_mtime).strftime("%d/%m/%Y alle %H:%M")
    logger.warning("database danneggiato: messo da parte in %s, rimessa la copia %s", aside.name, good.name)
    return ("Il database di FantaManager era danneggiato (succede dopo uno spegnimento brusco). "
            f"Ho rimesso l'ultima copia integra, del {when}: controlla nella regia le ultime "
            f"aggiudicazioni dopo quell'ora. Il file danneggiato è conservato in {aside}.")


class RestoreError(Exception):
    """Why a snapshot was not restored (the message is shown to the admin)."""


def restore_sqlite(name):
    """Put a SQLite snapshot back in place of the live database.

    A snapshot of the current state is taken first ("prima del ripristino"),
    so a restore can itself be undone. Then the migrations run, because the
    snapshot may predate the code now running. Returns that safety copy.
    Raises RestoreError with a message for the admin when refused.
    """
    import sqlite3
    from django.core.management import call_command
    from django.db import connections

    src_path = _db_path()
    if src_path is None:
        raise RestoreError("Il ripristino da qui vale solo per il database SQLite.")
    snapshot = backup_file(name)
    if snapshot is None or not snapshot.name.startswith("db-"):
        raise RestoreError("Copia non trovata.")
    try:
        con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            has_schema = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='django_migrations'"
            ).fetchone() is not None
        finally:
            con.close()
    except sqlite3.DatabaseError:
        ok = has_schema = False
    if not ok or not has_schema:
        raise RestoreError(f"{snapshot.name} non è un database FantaManager integro.")

    safety = backup_database(reason="prima del ripristino")
    if safety is None:
        raise RestoreError("Non riesco a salvare lo stato attuale: ripristino annullato.")

    connections.close_all()
    try:
        src = sqlite3.connect(str(snapshot))
        try:
            dst = sqlite3.connect(str(src_path), timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        logger.exception("restore of %s failed", snapshot.name)
        raise RestoreError(f"Ripristino non riuscito: {exc}. Lo stato attuale è in {safety.name}.")
    connections.close_all()
    call_command("migrate", interactive=False, verbosity=0)
    logger.warning("Database ripristinato da %s (copia di sicurezza: %s)", snapshot.name, safety.name)
    return safety


# --- PostgreSQL: ripristino di un dump dal Supervisor ---------------------------
#
# Nel container dell'app non c'è psql: il dump in chiaro di pg_dump (quello del
# servizio «backup» del compose, ``pg-*.sql.gz``) si legge qui e si esegue in
# una sola transazione. O passa tutto, o il database resta com'era.

import re as _re

_DOLLAR_QUOTE = _re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _scan_sql_line(line, state):
    """Aggiorna lo stato (apice aperto, dollar quote aperto, escape E'') con una
    riga di SQL; ritorna True se la riga chiude l'istruzione (``;`` fuori da
    apici, commenti e dollar quote)."""
    i, n, last = 0, len(line), ""
    while i < n:
        c = line[i]
        quote, dollar, escapes = state
        if dollar:
            if line.startswith(dollar, i):
                state[1] = None
                i += len(dollar)
                last = "$"
            else:
                i += 1
            continue
        if quote:
            if escapes and c == "\\":
                i += 2
                continue
            if c == quote:
                if i + 1 < n and line[i + 1] == quote:
                    i += 2
                    continue
                state[0], state[2] = None, False
                last = c
            i += 1
            continue
        if line.startswith("--", i):
            break
        if c in "'\"":
            state[0] = c
            state[2] = c == "'" and i > 0 and line[i - 1] in "eE"
        elif c == "$" and (m := _DOLLAR_QUOTE.match(line, i)):
            state[1] = m.group(0)
            i = m.end()
            last = "$"
            continue
        if not c.isspace():
            last = c
        i += 1
    return last == ";" and not state[0] and not state[1]


def iter_pg_dump(lines):
    """Le istruzioni di un dump SQL in chiaro di pg_dump, nell'ordine:
    ``("sql", testo)`` oppure ``("copy", "COPY … FROM stdin;", righe)`` con le
    righe di dati (ognuna col suo a capo). Salta commenti, righe vuote e i
    comandi di psql (``\\restrict``, ``\\connect``…), che non sono SQL."""
    buf, state = [], [None, None, False]
    it = iter(lines)
    for raw in it:
        line = raw.rstrip("\r\n")
        if not buf:
            bare = line.strip()
            if not bare or bare.startswith("--") or bare.startswith("\\"):
                continue
        buf.append(line)
        if not _scan_sql_line(line, state):
            continue
        stmt = "\n".join(buf).strip()
        buf = []
        head = stmt.rstrip(";").rstrip()
        if head.upper().startswith("COPY ") and head.upper().endswith("FROM STDIN"):
            rows = []
            for data in it:
                if data.rstrip("\r\n") == "\\.":
                    break
                rows.append(data if data.endswith("\n") else data + "\n")
            else:
                raise RestoreError("Il file finisce a metà dei dati di una tabella: è incompleto.")
            yield "copy", stmt, rows
        else:
            yield "sql", stmt
    if buf:
        raise RestoreError("Il file finisce a metà di un'istruzione: è incompleto.")


PG_DUMP_END = "PostgreSQL database dump complete"
_RESTORE_LOCK_TIMEOUT = "60s"     # attesa massima per un lucchetto durante il ripristino


def pg_dump_complete(path):
    """Il dump è finito e leggibile? Il servizio backup scrive il ``.sql.gz``
    mentre comprime: finché non ha finito il gzip è troncato, e un dump
    tagliato fra due istruzioni sembrerebbe valido. Si legge fino in fondo e
    si cerca la riga con cui pg_dump chiude."""
    import gzip
    tail = ""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            while chunk := fh.read(1 << 20):
                tail = (tail + chunk)[-400:]
    except (OSError, EOFError, UnicodeDecodeError):
        return False
    return PG_DUMP_END in tail


def pg_dump_since(timestamp, *, exclude=""):
    """Il dump completo più recente fatto dopo ``timestamp`` (secondi), o None."""
    folder = _backup_dir()
    try:
        dumps = [p for p in folder.glob("pg-*.sql.gz") if p.name != exclude and p.stat().st_mtime > timestamp]
    except OSError:
        return None
    for dump in sorted(dumps, key=lambda p: p.stat().st_mtime, reverse=True):
        if pg_dump_complete(dump):
            return dump
    return None


def _clean_public_schema(cur):
    """Toglie tutte le tabelle e sequenze dello schema public, dentro la
    transazione del ripristino. Il dump cancella solo le tabelle che contiene:
    una tabella aggiunta da un aggiornamento dopo il backup, con le sue chiavi
    verso le tabelle vecchie, ne bloccherebbe la cancellazione. Le migrazioni
    dopo il ripristino la ricreano."""
    from psycopg import sql
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    for (table,) in cur.fetchall():
        cur.execute(sql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(sql.Identifier(table)))
    cur.execute("SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public'")
    for (seq,) in cur.fetchall():
        cur.execute(sql.SQL("DROP SEQUENCE IF EXISTS public.{} CASCADE").format(sql.Identifier(seq)))


def restore_postgres(name):
    """Rimette il database PostgreSQL com'era nel dump ``name`` (``pg-*.sql.gz``).

    Tutto in una transazione: se un'istruzione fallisce non cambia niente.
    Si parte da uno schema pulito (vedi _clean_public_schema) e solo da un
    dump completo. Le migrazioni girano dopo, perché il dump può essere di
    una versione precedente del codice. La copia dello stato attuale la chiede chi chiama
    (vedi la pagina Supervisor), prima. Solleva RestoreError col messaggio
    per l'admin.
    """
    import gzip

    import psycopg
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connections

    if not _is_postgres():
        raise RestoreError("Questo ripristino vale solo per il database PostgreSQL.")
    dump = backup_file(name)
    if dump is None or not dump.name.startswith("pg-"):
        raise RestoreError("Copia non trovata.")
    try:
        with gzip.open(dump, "rt", encoding="utf-8") as fh:
            head = fh.read(400)
    except (OSError, EOFError, UnicodeDecodeError):
        head = ""
    if "PostgreSQL database dump" not in head:
        raise RestoreError(f"{dump.name} non è un backup del database leggibile.")
    if not pg_dump_complete(dump):
        raise RestoreError(f"{dump.name} è incompleto (backup interrotto o ancora in corso): "
                           "scegli un'altra copia. Il database non è cambiato.")

    db = settings.DATABASES["default"]
    params = {k: v for k, v in {
        "dbname": db.get("NAME"), "user": db.get("USER"), "password": db.get("PASSWORD"),
        "host": db.get("HOST"), "port": db.get("PORT"),
    }.items() if v}
    done = 0
    try:
        with psycopg.connect(**params) as conn, gzip.open(dump, "rt", encoding="utf-8") as fh:
            with conn.transaction(), conn.cursor() as cur:
                # Prima di tutto: un lucchetto di un'altra connessione si aspetta
                # al massimo un minuto, anche mentre si svuota lo schema.
                cur.execute(f"SET lock_timeout = '{_RESTORE_LOCK_TIMEOUT}'")
                _clean_public_schema(cur)
                for item in iter_pg_dump(fh):
                    if item[0] == "copy":
                        with cur.copy(item[1]) as copy:
                            for row in item[2]:
                                copy.write(row)
                    elif item[1].upper().startswith("SET LOCK_TIMEOUT"):
                        # Il dump aspetterebbe per sempre un lucchetto: un minuto basta.
                        cur.execute(f"SET lock_timeout = '{_RESTORE_LOCK_TIMEOUT}'")
                    else:
                        cur.execute(item[1])
                    done += 1
    except psycopg.Error as exc:
        logger.exception("restore of %s failed after %s statements", dump.name, done)
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        raise RestoreError(f"Ripristino non riuscito, il database non è cambiato: {detail}")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise RestoreError(f"Non riesco a leggere {dump.name}: {exc}. Il database non è cambiato.")
    connections.close_all()
    call_command("migrate", interactive=False, verbosity=0)
    logger.warning("Database PostgreSQL ripristinato da %s (%s istruzioni)", dump.name, done)
    return done
