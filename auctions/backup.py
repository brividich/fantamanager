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
"""
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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
    if src_path is None or not src_path.exists():
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
