"""Serialization and live state builders for WebSocket and UI."""
from decimal import Decimal

from django.utils import timezone

from .. import health
from ..models import Auction, Player
from .lifecycle import stuck_closed_lot
from .sealed import sealed_status
from .turns import current_turn


def roster_plan(participant):
    """Budget/slot planner for one manager (drives the bidder page strip).

    Returns remaining credits, per-role slot usage, and ``max_bid`` — the most a
    manager can commit to the *current* lot while still keeping 1 credit for each
    of their other still-empty slots (the same reserve rule ``place_bid``
    enforces). Legacy participants without a League get slot-less figures.
    """
    remaining = participant.remaining_credits
    league = participant.league
    owned_by_role = {"P": 0, "D": 0, "C": 0, "A": 0}
    for role in Player.objects.filter(owner=participant, abroad_list=False).values_list("role", flat=True):
        if role in owned_by_role:
            owned_by_role[role] += 1

    # Come si mostra la rosa all'allenatore: in Classic quattro righe per
    # reparto, in Mantra due — portieri e movimento — perché è così che la lega
    # conta gli slot. Le sigle restano quelle classiche: sono chiavi, non
    # etichette, e le pagine che le disegnano ci mappano sopra il nome esteso.
    if league is not None and league.is_mantra:
        bands = [("P", "POR", ("P",)), ("X", "MOV", ("D", "C", "A"))]
    else:
        bands = [(r, r, (r,)) for r in ("P", "D", "C", "A")]

    roles = []
    total_slots = free_slots = 0
    for key, label, bucket in bands:
        slots = league.slots_for(bucket[0]) if league else 0
        owned = sum(owned_by_role[r] for r in bucket)
        free = max(0, slots - owned)
        total_slots += slots
        free_slots += free
        roles.append({"role": key, "label": label, "owned": owned,
                      "slots": slots, "free": free})

    # Keep 1 credit per *other* empty slot; the rest is spendable on this lot.
    reserve = max(0, free_slots - 1)
    max_bid = max(Decimal("0"), remaining - reserve) if free_slots else remaining
    return {
        "remaining_credits": remaining,
        "roles": roles,
        "slot_limits": bool(league.slot_limits) if league else False,
        "total_slots": total_slots,
        "owned": sum(owned_by_role.values()),
        "free_slots": free_slots,
        "max_bid": max_bid,
    }


def _player_stats(player, league=None):
    """Compact stat dict for the auction card, or None when nothing was imported.

    Keys are only present when the underlying value is set, so the client can
    show just the figures a given pool actually has. ``fvm`` rides in on the
    Quotazioni listone; the rest come from the season "Statistiche" file.
    """
    def _fmt(v):
        # Trim trailing zeros so a fantamedia reads "8,2"/"60", not "8.20"/"60.00",
        # without ever falling into scientific notation (Decimal.normalize would).
        # The separator is the Italian comma, matching what ``floatformat`` renders
        # server-side under it-it — otherwise the card visibly flips from "8,97"
        # to "8.97" the moment the first live state lands.
        if not isinstance(v, Decimal):
            return v
        return ("%f" % v).rstrip("0").rstrip(".").replace(".", ",")

    raw = {
        "fvm": player.fvm_for(league),
        "presences": player.presences,
        "avg_vote": player.avg_vote,
        "fanta_avg": player.fanta_avg,
        "goals": player.goals,
        "assists": player.assists,
    }
    out = {k: _fmt(v) for k, v in raw.items() if v is not None}
    return out or None


def _ticker_warning(auction):
    """A short, regia-facing message when the background ticker looks like
    it might not be advancing this auction — a recent tick failure, or a
    lot closed well past its own pause with nobody having caught it yet.
    None when everything looks normal.

    Read-only: unlike the ticker's own use of stuck_closed_lot, this never
    advances or charges anything — it only reports what it sees.
    """
    if health.ticker_error(auction.id) is not None:
        return "Il ticker ha incontrato un errore — verifica che l'asta avanzi regolarmente."
    stall = health.recent_stall(auction.id)
    if stall is not None:
        return (f"Il database è rimasto fermo {stall['seconds']:.0f} s e quei secondi sono tornati al timer. "
                "Durante l'asta evita import ed export; se succede ancora, controlla l'antivirus sulla cartella dei dati.")
    if stuck_closed_lot(auction.id) is not None:
        return "Un lotto è chiuso da un po' senza avanzare — ricarica la pagina o controlla la connessione."
    return None


def serialize_state(auction, now=None):
    now = now or timezone.now()
    best = auction.best_bid
    best_logo = None
    if best and best.participant.logo:
        best_logo = best.participant.logo.url
    player = auction.player
    is_mantra = auction.league is not None and auction.league.is_mantra
    pending = 0
    next_up = None
    if auction.flow_mode != Auction.FlowMode.CALL:
        qs = auction.queue_items.filter(done=False).order_by("order", "id")
        pending = qs.count()
        nxt = qs.first()
        # A random draw must stay a draw: the state reaches every screen and
        # phone in the room, so who is next is withheld — only how many are
        # left. Any other order is public by design.
        if nxt is not None and auction.call_order != Auction.CallOrder.RANDOM:
            next_up = {"name": nxt.player.name, "role": nxt.player.role,
                       "team": nxt.player.team}
    notice = getattr(auction, "advance_notice", None)

    # The gap between two lots, as seconds still to run — None whenever we are
    # not in one, so a client can simply test for null.
    #
    # It is measured from ``updated_at`` (stamped by close_if_expired the moment
    # the ticker noticed the expiry), not from ``ends_at``: the ticker only wakes
    # every TIMER_SYNC_INTERVAL_SECONDS, so the pause really starts at detection.
    # Counting from ends_at would reach zero up to one tick before the next
    # player actually goes up, and a countdown that sits at 0 reads as a freeze.
    #
    # A last lot with an empty queue is not an interlude but the end of the
    # auction, so ``pending`` has to be non-zero for the countdown to appear.
    #
    # A knocked-down lot (had_bid) does not get this auto-resolving countdown:
    # it waits for ``awaiting_confirm`` below instead, however long that takes.
    had_bid = best is not None and not best.cancelled
    interlude = None
    if (auction.status == Auction.Status.CLOSED
            and auction.ends_at is not None
            and auction.auto_advances
            and auction.cycle_break_seconds > 0
            and pending > 0
            and not had_bid):
        elapsed = (now - auction.updated_at).total_seconds()
        interlude = round(max(0.0, auction.cycle_break_seconds - elapsed), 3)

    # True once a sale has closed on an auto-advancing flow (CONTINUOUS, or
    # MANUAL with manual_auto_advance on): the running order does not roll on
    # by itself any more — it parks here until the regia clicks "Prosegui"
    # (admin_confirm_advance), which just calls reset_if_closed by hand.
    awaiting_confirm = (
        auction.status == Auction.Status.CLOSED
        and auction.ends_at is not None
        and auction.auto_advances
        and had_bid
    )

    return {
        "type": "state",
        # Regia-only in practice (bid.html / screen.html don't render this
        # key), but sent to everyone: it rides the same state payload every
        # client already gets, rather than a second admin-only poll.
        "ticker_warning": _ticker_warning(auction),
        "auction_id": auction.id,
        "title": auction.title,
        "status": auction.status,
        # Player currently on the block (None when no concrete player is set).
        "player": None if player is None else {
            "id": player.id, "name": player.name,
            "role": player.role, "team": player.team,
            "initial_price": str(player.price_for(auction.league)),
            "photo_url": player.photo_url or "",
            "audio_url": player.audio_url,
            # I ruoli Mantra, già pronti da disegnare come badge separati.
            # Lista vuota in Classic: le pagine non mostrano nulla in più.
            "mantra_roles": player.role_list if is_mantra else [],
            # Decision-support stats (None when never imported → UI hides them).
            "stats": _player_stats(player, auction.league),
        },
        "game_mode": "MANTRA" if is_mantra else "CLASSIC",
        "flow_mode": auction.flow_mode,
        "turn": current_turn(auction),
        "manual_auto_advance": auction.manual_auto_advance,
        "cycle_break_seconds": auction.cycle_break_seconds,
        "interlude_seconds": interlude,
        "awaiting_confirm": awaiting_confirm,
        "screen_timer_size": auction.screen_timer_size,
        "screen_name_size": auction.screen_name_size,
        "auto_advances": auction.auto_advances,
        "call_order": auction.call_order,
        "within_role_order": auction.within_role_order,
        "by_role": auction.by_role,
        "queue_pending": pending,
        # Distinct free agents left in the pool — unlike ``queue_pending`` this
        # never goes back up: an unsold lot re-enters the running order (so the
        # queue count can hold steady or even look stuck), but it stays the same
        # player, still uncounted here only once they are actually bought.
        "players_remaining": Player.objects.filter(owner__isnull=True, league=auction.league).count(),
        "queue_next": next_up,
        "notice": notice if notice else None,
        "current_price": str(auction.current_price),
        "min_increment": str(auction.min_increment),
        "quick_increments": [str(i) for i in auction.allowed_increments()],
        "duration_seconds": auction.duration_seconds,
        "antisnipe_seconds": auction.antisnipe_seconds,
        "current_cycle": auction.current_cycle,
        "remaining_seconds": round(auction.remaining(now), 3),
        "ends_at": auction.ends_at.isoformat() if auction.ends_at else None,
        "server_time": now.isoformat(),
        # Asta alle buste: aperta o no, a che giro, con che minimo e quante
        # buste sono gia' arrivate. Mai il contenuto delle buste altrui finche'
        # il giro e' aperto — ``reveal`` si riempie solo allo spoglio.
        "sealed": sealed_status(auction),
        "best_bidder": best.participant.display_name if best else None,
        "best_amount": str(best.amount) if best else None,
        "best_bidder_logo": best_logo,
    }


def serialize_bid(bid):
    logo = bid.participant.logo.url if bid.participant.logo else None
    return {
        "id": bid.id,
        "participant": bid.participant.display_name,
        "participant_logo": logo,
        "amount": str(bid.amount),
        "increment": str(bid.increment),
        "accepted": bid.accepted and not bid.cancelled,
        "rejection_reason": bid.rejection_reason,
        "server_received_at": bid.server_received_at.isoformat(),
        "remaining_at_bid": bid.remaining_at_bid,
    }
