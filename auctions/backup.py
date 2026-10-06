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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PERIODIC = "periodico"          # the live ticker's reason: see consumers.RoomTicker
PG_REQUEST_FILE = ".richiesta"  # watched by the backup service in docker-compose

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
    file, or None when it could not be written (logged, never raised)."""
    path = _backup_dir() / PG_REQUEST_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{reason or 'richiesta'}\n", encoding="utf-8")
    except OSError:
        logger.exception("could not request a database dump (%s)", reason or "no reason given")
        return None
    return path


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
