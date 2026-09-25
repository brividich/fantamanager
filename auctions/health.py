"""In-memory "is anything wrong right now" signals for the regia dashboard.

Process-global and not persisted — a restart clears it, which is fine: this
describes live status, not history (the fantamanager.log file is still the
record of what happened). Kept in its own module, not services.py or
consumers.py, so both can import it without a circular import: consumers.py
records into it, services.py reads from it when building the state payload
every client (including the regia) already receives.
"""
import threading
import time

_LOCK = threading.Lock()
_TICKER_ERRORS = {}  # auction_id -> {"at": epoch seconds, "message": str}

# How long a logged tick failure keeps showing as a live warning on the
# dashboard. Long enough that a regia glancing over a minute later still
# sees it; short enough that an old, since-recovered blip doesn't linger.
ERROR_TTL_SECONDS = 180


def record_ticker_error(auction_id, message):
    with _LOCK:
        _TICKER_ERRORS[int(auction_id)] = {"at": time.time(), "message": str(message)[:300]}


def clear_ticker_error(auction_id):
    with _LOCK:
        _TICKER_ERRORS.pop(int(auction_id), None)


def ticker_error(auction_id):
    """The most recent tick failure for this auction, or None if it's
    stale (older than ERROR_TTL_SECONDS) or there wasn't one."""
    with _LOCK:
        entry = _TICKER_ERRORS.get(int(auction_id))
    if entry is None:
        return None
    if time.time() - entry["at"] > ERROR_TTL_SECONDS:
        return None
    return entry
