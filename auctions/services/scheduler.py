"""What has to happen on time, whether or not anybody has a page open.

Run by ``manage.py run_scheduler`` (a service of its own next to the web
server). Each pass:

- closes the auction lots whose timer ran out and moves on to the next one —
  the in-process ``RoomTicker`` only runs while a WebSocket is connected, so
  without this a lot left alone never expires. Closing is idempotent
  (``close_if_expired`` is a conditional UPDATE, ``finalize_expired`` guards
  on auction+cycle), so the two tickers never charge a winner twice;
- opens and closes the market sessions on their dates (``sync_market_schedule``,
  otherwise run only when someone visits a market page);
- locks the lineups of a giornata when its deadline (``Giornata.starts_at``)
  passes: from then on its formations don't change.

Every step is safe to run again: a pass that finds nothing due does nothing.
"""
import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..models import Auction, Giornata

logger = logging.getLogger("auctions.scheduler")

# A lot closed on expiry stays CLOSED during its pause; past this age an
# auction left CLOSED is a finished one, not a stuck lot.
STUCK_LOT_WINDOW = timedelta(hours=6)


def due_auction_ids(now=None):
    """Auctions with something that may be due: a running timer, sealed bids
    to open, or a lot closed on expiry that hasn't moved on."""
    now = now or timezone.now()
    return list(Auction.objects.filter(
        Q(status=Auction.Status.LIVE, ends_at__lte=now)
        | Q(sealed_round__gt=0, sealed_ends_at__lte=now)
        | Q(status=Auction.Status.CLOSED, ends_at__isnull=False, updated_at__gte=now - STUCK_LOT_WINDOW)
    ).values_list("id", flat=True))


def auction_tick(auction_id, broadcast=None):
    """One pass of the room ticker for one auction, without waiting: the pause
    after a lot is left to ``stuck_closed_lot`` on a later pass. Returns what
    happened ("sealed", "closed", "advanced") for the log and the tests."""
    from . import (close_if_expired, finalize_expired, lot_had_bid, reset_if_closed,
                   sealed_tick, stuck_closed_lot)

    done = []
    if sealed_tick(auction_id) is not None:
        done.append("sealed")
    closed = close_if_expired(auction_id)
    if closed is not None:
        finalize_expired(auction_id)
        done.append("closed")
    else:
        stuck = stuck_closed_lot(auction_id)
        if stuck is not None and not (lot_had_bid(stuck) and stuck.auto_advances):
            if reset_if_closed(auction_id) is not None:
                done.append("advanced")
    if done and broadcast is not None:
        broadcast(auction_id)
    return done


def lock_due_giornate(now=None, league=None):
    """Lock the lineups of every giornata (of ``league``, or of every league)
    whose deadline has passed. Returns the giornate locked."""
    from .formation import EDITABLE, lock_formations

    now = now or timezone.now()
    locked = []
    due = (Giornata.objects.select_related("season", "season__league")
           .filter(status__in=EDITABLE, starts_at__isnull=False, starts_at__lte=now))
    if league is not None:
        due = due.filter(season__league=league)
    for giornata in due.order_by("number"):
        lock_formations(giornata)
        locked.append(giornata)
        logger.info("Giornata %s (lega %s): formazioni bloccate alla scadenza.",
                    giornata.number, giornata.season.league_id)
    return locked


def deadlines_from_calendar(season, *, get=None):
    """Set each giornata's deadline to the first Serie A kick-off of its round
    (API-Football). Giornate already started keep theirs. Returns how many
    deadlines were set; raises ``ApiFootballError`` when the API can't answer."""
    from ..providers import apifootball
    from .formation import EDITABLE

    kickoffs = apifootball.round_kickoffs(**({"get": get} if get else {}))
    changed = 0
    for giornata in season.giornate.filter(status__in=EDITABLE):
        kickoff = kickoffs.get(giornata.serie_a_matchday or giornata.number)
        if kickoff is not None and giornata.starts_at != kickoff:
            giornata.starts_at = kickoff
            giornata.save(update_fields=["starts_at"])
            changed += 1
    return changed


def run_once(broadcast=None, now=None):
    """Every due job, once. Returns a summary dict."""
    from .market import sync_market_schedule

    summary = {"auctions": {}, "market": (0, 0), "giornate": []}
    for auction_id in due_auction_ids(now):
        try:
            done = auction_tick(auction_id, broadcast)
        except Exception:                     # one broken auction must not stop the rest
            logger.exception("Scheduler: asta %s non aggiornata", auction_id)
            continue
        if done:
            summary["auctions"][auction_id] = done
    try:
        summary["market"] = sync_market_schedule()
    except Exception:
        logger.exception("Scheduler: sessioni di mercato non aggiornate")
    try:
        summary["giornate"] = [g.id for g in lock_due_giornate(now)]
    except Exception:
        logger.exception("Scheduler: blocco formazioni non riuscito")
    return summary


def channel_broadcast(auction_id):
    """Push the auction's new state to its room (reaches the browsers when the
    channel layer is shared, i.e. Redis; with the in-memory layer of a single
    process the clients pick it up from their own ticker)."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from . import serialize_state

    layer = get_channel_layer()
    auction = Auction.objects.filter(pk=auction_id).first()
    if layer is None or auction is None:
        return
    async_to_sync(layer.group_send)(f"auction_{auction_id}",
                                    {"type": "state.update", "state": serialize_state(auction)})
