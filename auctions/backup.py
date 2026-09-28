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


def backup_database(*, keep=10, reason=""):
    """Blocking snapshot — call this from a background thread (see
    :func:`backup_database_async`), never inline on a request or websocket
    path: sqlite3's backup API does real disk IO and must not add latency to
    a bid, a lot closing, or anything else someone is waiting on.

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
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst_path = backups_dir / f"db-{stamp}.sqlite3"
    n = 1
    while dst_path.exists():  # two backups within the same second
        n += 1
        dst_path = backups_dir / f"db-{stamp}-{n}.sqlite3"

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

    backups = sorted(
        backups_dir.glob("db-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst_path


def backup_database_async(*, keep=10, reason="", min_interval=BG_MIN_INTERVAL_SECONDS):
    """Fire-and-forget: runs the (blocking) snapshot on a daemon thread so a
    caller on the request/websocket path never waits on disk IO.

    Coalesces bursts behind ``min_interval`` — several lots can close back
    to back, and every connected client's ticker polls independently — so
    this can be called liberally without spawning a thread per event.
    """
    global _last_bg_backup
    now = time.monotonic()
    with _bg_lock:
        if now - _last_bg_backup < min_interval:
            return
        _last_bg_backup = now
    threading.Thread(
        target=backup_database, kwargs={"keep": keep, "reason": reason}, daemon=True,
    ).start()
