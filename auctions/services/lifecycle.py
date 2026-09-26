"""Auction lifecycle, control endpoints, and roster assignments/releases."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..backup import backup_database_async
from ..models import (
    Auction, AuctionCycleResult, Bid, Participant, Player, RosterLog,
)
from .queue import (
    _next_pending, _set_on_block, _role_jump_target, enqueue_released_player,
)
from .sealed import _clear_sealed

logger = logging.getLogger("auctions.lifecycle")


def _backup_async(*args, **kwargs):
    from unittest.mock import Mock
    if isinstance(backup_database_async, Mock):
        return backup_database_async(*args, **kwargs)
    import sys
    svc = sys.modules.get("auctions.services")
    func = getattr(svc, "backup_database_async", backup_database_async) if svc else backup_database_async
    return func(*args, **kwargs)


def listone_loaded(auction):
    """True if the auction's league pool holds a listone (any Player).

    The listone (official Quotazioni) is the master player pool and is required
    to run an auction, independently of whether rosters were imported. ``None``
    league selects the legacy/global pool, so existing installs are unaffected.
    """
    return Player.objects.filter(league=auction.league).exists()


@transaction.atomic
def start_auction(auction_id):
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    # The listone (Quotazioni) is mandatory: without a player pool there is
    # nothing to auction. This holds regardless of whether rosters were loaded.
    if not listone_loaded(auction):
        return None
    # Refuse to start an ordered flow (continuous / manual) with nothing to put
    # up: no player on the block and an empty queue. Otherwise it would go LIVE
    # on an empty block and (continuous) close on the next tick. CALL mode is
    # fine starting empty — the admin nominates players by hand.
    if (auction.is_ordered_flow
            and auction.player_id is None
            and _next_pending(auction) is None):
        return None
    now = timezone.now()
    auction.status = Auction.Status.LIVE
    auction.starts_at = now
    auction.ends_at = None
    auction.remaining_seconds = None
    auction.best_bid = None
    auction.current_cycle = 1
    # Ordered flows (continuous / manual) put the first queued free agent on the
    # block; the self-advancing ones arm the presentation timer immediately.
    if auction.is_ordered_flow and auction.player_id is None:
        nxt = _next_pending(auction)
        if nxt is not None:
            nxt.done = True
            nxt.save(update_fields=["done"])
            _set_on_block(auction, nxt.player, arm_timer=auction.auto_advances)
    # A self-advancing flow counts down from the start: arm the timer for
    # whatever player is on the block — pulled from the queue just above, or
    # already set because the auction was created with an initial player / is
    # being restarted. Without this the lot stays frozen at full duration.
    if (auction.auto_advances
            and auction.player_id is not None and auction.ends_at is None):
        auction.ends_at = now + timedelta(seconds=auction.duration_seconds)
    auction.current_price = auction.starting_price
    auction.save()
    logger.info("Asta #%s AVVIATA (Stato: LIVE, Ciclo: %s)", auction.id, auction.current_cycle)
    return auction


@transaction.atomic
def call_player(auction_id, player_id):
    """CALL mode: admin nominates a specific free agent onto the block.

    Resets the price to the player's quotazione and arms a fresh cycle (LIVE,
    no deadline yet — the first bid starts the clock). Rejects owned players.
    """
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    try:
        player = Player.objects.get(pk=player_id)
    except Player.DoesNotExist:
        return None
    if player.owner_id is not None:
        return None
    _set_on_block(auction, player)
    auction.status = Auction.Status.LIVE
    auction.ends_at = None
    auction.remaining_seconds = None
    auction.current_cycle = auction.current_cycle + 1
    auction.save()
    auction.queue_items.filter(player=player).update(done=True)
    return auction


@transaction.atomic
def set_auto_advance(auction_id, on):
    """MANUAL flow: turn the self-advancing running order on or off, live.

    The flag also applies to the lot already on the block, so the regista does
    not have to wait for the next player to see it take effect: switching on
    arms the presentation clock, switching off disarms it. Both only touch a lot
    nobody has bid on — a clock started by a real bid keeps running either way,
    because that countdown belongs to the bidding, not to the auto-advance.
    """
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    auction.manual_auto_advance = bool(on)
    fields = ["manual_auto_advance"]
    unbid_live_lot = (
        auction.status == Auction.Status.LIVE
        and auction.player_id is not None
        and (auction.best_bid is None or auction.best_bid.cancelled)
    )
    if unbid_live_lot and auction.auto_advances and auction.ends_at is None:
        auction.ends_at = timezone.now() + timedelta(seconds=auction.duration_seconds)
        fields.append("ends_at")
    elif unbid_live_lot and not auction.auto_advances and auction.ends_at is not None:
        auction.ends_at = None
        fields.append("ends_at")
    auction.save(update_fields=fields)
    return auction


@transaction.atomic
def pause_auction(auction_id):
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    if auction.status == Auction.Status.LIVE:
        auction.remaining_seconds = auction.remaining()
        auction.status = Auction.Status.PAUSED
        auction.save()
    return auction


@transaction.atomic
def resume_auction(auction_id):
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    if auction.status == Auction.Status.PAUSED:
        remaining = auction.remaining_seconds or auction.duration_seconds
        if remaining >= auction.duration_seconds:
            auction.ends_at = None
        else:
            auction.ends_at = timezone.now() + timedelta(seconds=remaining)
        auction.remaining_seconds = None
        auction.status = Auction.Status.LIVE
        auction.save()
    return auction


@transaction.atomic
def adjust_timer(auction_id, delta_seconds):
    """Regista control: add/remove seconds from the running lot timer.

    Works whether the auction is LIVE (shifts ``ends_at``) or PAUSED (shifts the
    stored ``remaining_seconds``). A no-op if the timer hasn't started yet
    (``ends_at`` is None while LIVE — no lot clock to move). The result is
    floored at 1 second so a negative delta can't retro-expire the lot; the
    caller decides whether to close it.

    The returned auction carries a transient ``timer_changed`` flag so callers
    can tell a real adjustment from a no-op instead of reporting success either
    way (in CALL / MANUAL play the clock only exists after the first bid).
    """
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    auction.timer_changed = False
    try:
        delta = float(delta_seconds)
    except (TypeError, ValueError):
        return auction
    now = timezone.now()
    if auction.status == Auction.Status.LIVE and auction.ends_at is not None:
        remaining = max(1.0, (auction.ends_at - now).total_seconds() + delta)
        auction.ends_at = now + timedelta(seconds=remaining)
        auction.save(update_fields=["ends_at", "updated_at"])
        auction.timer_changed = True
    elif auction.status == Auction.Status.PAUSED and auction.remaining_seconds is not None:
        auction.remaining_seconds = max(1.0, auction.remaining_seconds + delta)
        auction.save(update_fields=["remaining_seconds", "updated_at"])
        auction.timer_changed = True
    return auction


@transaction.atomic
def force_close_lot(auction_id):
    """Regia override: skip a lot nobody is bidding on, right now, instead
    of waiting out its full countdown — for CONTINUOUS (and MANUAL with
    auto-advance), where the timer runs from the moment the lot goes up
    regardless of bids, so a player nobody wants still sits there for the
    whole duration before going invenduto on its own.

    Refused once *any* bid has landed on this lot (``auction.best_bid`` is
    set and not cancelled): cutting the clock short then would end a live
    bidding war early and hand the lot to whoever happens to be leading at
    that instant, denying every other team the time they still had left to
    raise — the opposite of what this button is for. Skipping is only for
    the case nobody has offered anything at all yet.

    Deliberately does nothing beyond moving ``ends_at`` to now: every ticker
    already polls close_if_expired/finalize_expired/reset_if_closed on its
    own short interval (see AuctionConsumer._run_ticker), so cutting the
    clock short here reaches the exact same, already-tested closing path a
    real expiry takes — logged invenduto, backed up, advanced — without
    duplicating any of that logic. The regia's own browser is itself one of
    those tickers, so the result lands within one tick
    (TIMER_SYNC_INTERVAL_SECONDS), not instantly.

    Returns the auction either way; a transient ``force_close_error`` on it
    tells a no-op apart from success, and why: ``"timer_not_running"`` (the
    lot isn't LIVE, or the timer hasn't started yet — CALL/MANUAL wait for
    the first bid before it does) or ``"bid_in_progress"``.
    """
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    auction.force_close_error = None
    if auction.status != Auction.Status.LIVE or auction.ends_at is None:
        auction.force_close_error = "timer_not_running"
        return auction
    if auction.best_bid_id is not None and not auction.best_bid.cancelled:
        auction.force_close_error = "bid_in_progress"
        return auction
    auction.ends_at = timezone.now()
    auction.save(update_fields=["ends_at", "updated_at"])
    return auction


@transaction.atomic
def close_auction(auction_id):
    """Admin-initiated close. Records winner spend. Setting ends_at=None prevents auto-reset."""
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    _record_winner_spend(auction, assigned_by="admin")
    auction.status = Auction.Status.CLOSED
    auction.ends_at = None
    auction.remaining_seconds = 0
    auction.save()
    logger.info("Asta #%s CHIUSA da admin", auction.id)
    # The admin just ended the whole draft ("Termina asta") — a clean recovery
    # point beyond the periodic ticker backup below.
    transaction.on_commit(lambda: _backup_async(reason="asta terminata"))
    return auction


def close_if_expired(auction_id):
    now = timezone.now()
    updated = (
        Auction.objects.filter(
            pk=auction_id, status=Auction.Status.LIVE, ends_at__lte=now
        ).update(status=Auction.Status.CLOSED, remaining_seconds=0, updated_at=now)
    )
    if updated:
        return Auction.objects.get(pk=auction_id)
    return None


def lot_had_bid(auction):
    """Whether the lot just closed on ``auction`` was actually knocked down."""
    return auction.best_bid is not None and not auction.best_bid.cancelled


@transaction.atomic
def finalize_expired(auction_id):
    """Charge the winner (or log the lot as invenduto) the moment a lot closes
    on natural expiry — independent of ``reset_if_closed``'s post-close pause.

    The ticker used to only charge/assign inside ``reset_if_closed``, called
    after sleeping ``cycle_break_seconds``. Each connected client runs its own
    ticker task, and a disconnect (winner closing their tab right after
    winning, a refresh, a network blip) cancels that task mid-sleep — before
    ``reset_if_closed`` ever ran, leaving the lot CLOSED forever with the
    winner never charged and the player never added to their roster. Calling
    this immediately on close removes that dependency; it's idempotent (same
    (auction, cycle) guard as :func:`_record_winner_spend`) so being called
    again later inside ``reset_if_closed`` is a no-op.
    """
    try:
        auction = Auction.objects.select_for_update().get(
            pk=auction_id, status=Auction.Status.CLOSED, ends_at__isnull=False,
        )
    except Auction.DoesNotExist:
        return None
    had_bid = lot_had_bid(auction)
    _record_winner_spend(auction)
    if not had_bid:
        _record_unsold(auction)
    return auction


def stuck_closed_lot(auction_id):
    """A lot that closed on expiry and never advanced (see :func:`finalize_expired`).

    Used by the ticker to catch up an auction whose post-close pause was
    interrupted by a disconnect before ``reset_if_closed`` could run. Charging
    itself is never at risk (``finalize_expired`` runs synchronously on
    close) — this only recovers the "move on to the next lot" step.

    Only reports a lot once its own ``cycle_break_seconds`` pause has actually
    had time to elapse (plus a small grace margin): a lot freshly closed is
    just in its normal, intentional pause, not stuck — every other connected
    client's ticker also polls on this, so without the time gate a second
    viewer's very next tick would short-circuit the pause the instant it
    started, instead of only recovering a pause that was genuinely abandoned.
    """
    auction = Auction.objects.filter(
        pk=auction_id, status=Auction.Status.CLOSED, ends_at__isnull=False,
    ).first()
    if auction is None:
        return None
    grace = timedelta(seconds=auction.cycle_break_seconds + 1)
    if timezone.now() - auction.updated_at < grace:
        return None  # still within its normal post-close pause — not stuck
    return auction


@transaction.atomic
def reset_if_closed(auction_id):
    """Cyclic reset: records winner spend, then returns to LIVE+waiting state.

    Only fires for natural expiry (ends_at IS NOT NULL). For auto-advancing
    flows with a knocked-down lot, the ticker no longer calls this by itself —
    it waits for the regia's "Prosegui" click (admin_confirm_advance), which
    calls this exact function by hand. Everything else (unsold lots, and any
    flow that already waits on the admin) still resolves on its own."""
    now = timezone.now()
    try:
        auction = Auction.objects.select_for_update().get(
            pk=auction_id,
            status=Auction.Status.CLOSED,
            ends_at__isnull=False,
        )
    except Auction.DoesNotExist:
        return None

    had_bid = lot_had_bid(auction)
    _record_winner_spend(auction)
    if not had_bid:
        _record_unsold(auction)  # log the expired lot as invenduto

    _clear_sealed(auction)
    auction.current_cycle = auction.current_cycle + 1
    auction.ends_at = None
    auction.remaining_seconds = None
    auction.best_bid = None
    auction.updated_at = now

    if auction.auto_advances:
        return _advance_continuous(auction, had_bid)

    # CALL and hand-stepped MANUAL: clear the (now sold) player and stay LIVE.
    # CALL waits for the admin to nominate the next one; MANUAL waits for the
    # avanti/indietro controls. Clearing the block stops stray bids landing on
    # an already-assigned player; the price resets so nothing rolls over.
    auction.player = None
    auction.status = Auction.Status.LIVE
    auction.current_price = auction.starting_price
    auction.save()
    return auction


def _role_saturated(auction, role):
    """True when every active league participant has filled that role's slots.

    Unscoped (legacy, no league) auctions never saturate — there are no per-role
    caps to fill — so continuous mode just runs the queue to exhaustion.
    """
    if not role:
        return False
    league = auction.league
    if league is None or not league.slot_limits:
        return False
    participants = list(Participant.objects.filter(league=league, is_active=True))
    if not participants:
        return False
    cap = league.slots_for(role)
    if cap <= 0:
        return True
    bucket = league.slot_roles(role)
    for p in participants:
        if Player.objects.filter(owner=p, abroad_list=False, role__in=bucket).count() < cap:
            return False
    return True


def _next_unsaturated_pending(auction):
    """Next pending item whose role is still playable.

    Items in a saturated role are marked ``done`` (skipped) so the run advances
    to the next playable role. Returns ``None`` when nothing playable remains.
    """
    cache = {}
    while True:
        item = _next_pending(auction)
        if item is None:
            return None
        role = item.role or (item.player.role if item.player_id else "")
        if role not in cache:
            cache[role] = _role_saturated(auction, role)
        if not cache[role]:
            return item
        item.done = True
        item.save(update_fields=["done"])


def _advance_continuous(auction, had_bid):
    """Self-advancing flow: move from the just-finished lot to the next one.

    Used by CONTINUOUS and by MANUAL with ``manual_auto_advance`` on.

    No bids → the player went unsold and is re-queued at the end of their role,
    up to :func:`max_unsold_passes` times; past that the lot is parked so the
    run cannot ping-pong between unsold players forever. Saturated roles are
    skipped; if that crosses into a new role a one-shot ``advance_notice`` is
    attached for the dashboard popup. Empty queue closes.
    """
    prev_role = auction.player.role if auction.player_id else None
    if not had_bid and auction.player_id is not None:
        enqueue_released_player(auction, auction.player, unsold=True)

    nxt = _next_unsaturated_pending(auction)
    if nxt is None:
        auction.player = None
        auction.status = Auction.Status.CLOSED
        auction.ends_at = None
        auction.remaining_seconds = 0
        auction.save()
        # The running order just ran out on its own — the draft is over.
        transaction.on_commit(lambda: _backup_async(reason="asta esaurita"))
        return auction

    nxt.done = True
    nxt.save(update_fields=["done"])
    _set_on_block(auction, nxt.player, arm_timer=True)
    auction.status = Auction.Status.LIVE

    if prev_role and nxt.player.role != prev_role and _role_saturated(auction, prev_role):
        auction.advance_notice = {"from": prev_role, "to": nxt.player.role}

    auction.save()
    return auction


def _prev_target(auction, direction, cur):
    """The queue item a backward step would land on (``None`` when there is none)."""
    if direction == "prev_role":
        return _role_jump_target(auction, forward=False)
    qs = auction.queue_items.filter(done=True)
    if cur is not None:
        qs = qs.filter(order__lt=cur.order)
    return qs.order_by("-order", "-id").first()


def _sale_to_undo(auction, player):
    """The recorded sale of ``player`` in this auction, if it still stands.

    Returns the ``AuctionCycleResult`` only when the player is currently owned,
    so a lot that was merely skipped (or already released by hand) steps back
    with no confirmation needed.
    """
    if player is None or player.owner_id is None:
        return None
    return (
        AuctionCycleResult.objects.filter(auction=auction, player=player, assigned=True)
        .order_by("-cycle")
        .first()
    )


def _undo_sale(auction, result, player):
    """Reverse a knock-down: free the player and give the exact price back.

    The buyer is refunded what they actually paid (``Player.cost``, falling back
    to the recorded amount), floored so a roster can never yield more credits
    than it cost, and the cycle result is marked un-assigned so the lot can be
    run again. Writes a RosterLog row — an undo is a roster change like any other.
    """
    owner = Participant.objects.select_for_update().filter(pk=player.owner_id).first()
    amount = player.cost or result.amount or Decimal("0")
    if owner is not None:
        new_spent = max(Decimal("0"), owner.spent_credits - amount)
        applied = owner.spent_credits - new_spent
        owner.spent_credits = new_spent
        owner.save(update_fields=["spent_credits"])
        RosterLog.objects.create(
            participant=owner,
            participant_name=owner.display_name,
            player_name=player.name,
            player_role=player.role,
            action=RosterLog.Action.ADMIN_RELEASE,
            credits_delta=applied,
            by_admin=True,
            note=f"Aggiudicazione annullata · asta #{auction.id} ciclo {result.cycle}",
        )
    Player.objects.filter(pk=player.pk).update(owner=None, cost=Decimal("0"))
    player.owner = None
    player.cost = Decimal("0")
    result.assigned = False
    result.winner = None
    result.winner_name = ""
    result.amount = Decimal("0")
    result.assigned_at = None
    result.note = (result.note or "")[:150] + " · annullata"
    result.save(update_fields=["assigned", "winner", "winner_name", "amount",
                               "assigned_at", "note"])
    logger.info(
        "Aggiudicazione ANNULLATA [Asta #%s ciclo %s]: %s revocato a '%s', stornati %s FM",
        auction.id, result.cycle, player.name, owner.display_name if owner else "—", applied if owner else 0,
    )
    return result


def manual_step(auction_id, direction, *, undo_sale=False):
    """Step the running order forward / backward by hand (MANUAL & CONTINUOUS).

    ``next`` finalises the current lot (charging the winner when there is a best
    bid, else recording it invenduto), marks it done and puts the next pending
    player on the block; an empty queue closes the auction. ``prev`` brings the
    previous player back on the block (recall a skipped one) and returns the
    current un-won player to the pending queue. ``next_role`` / ``prev_role``
    behave like ``next`` / ``prev`` but jump straight to the adjacent role,
    skipping whatever is left of the current one.

    CONTINUOUS gives the admin a live override of the auto-advancing queue: a
    forward step arms the presentation timer on the new lot (the lot counts down
    at once, as in normal continuous play); a backward recall leaves the timer
    disarmed so the recalled lot waits for a bid instead of auto-advancing away.
    Hand-stepped MANUAL never arms the timer — the clock starts on the first
    bid, as in CALL — unless ``manual_auto_advance`` is on, which makes forward
    steps behave like CONTINUOUS.

    Stepping back onto an already-sold lot needs ``undo_sale``: without it the
    auction is returned untouched carrying a transient ``needs_undo_confirm``
    describing the sale the caller must confirm; with it the knock-down is
    reversed (player freed, buyer refunded) before the lot goes back up.
    """
    auction = Auction.objects.select_for_update().get(pk=auction_id)
    if auction.flow_mode not in (Auction.FlowMode.MANUAL, Auction.FlowMode.CONTINUOUS):
        return auction
    arm = auction.auto_advances

    if direction in ("next", "next_role"):
        if _record_winner_spend(auction) is None:
            _record_unsold(auction)  # no winner → log the lot as invenduto
        if auction.player_id:
            auction.queue_items.filter(player_id=auction.player_id).update(done=True)
        nxt = (_role_jump_target(auction, forward=True) if direction == "next_role"
               else _next_pending(auction))
        auction.current_cycle = auction.current_cycle + 1
        auction.best_bid = None
        auction.ends_at = None
        auction.remaining_seconds = None
        if nxt is None:
            auction.player = None
            auction.status = Auction.Status.CLOSED
            auction.current_price = auction.starting_price
            auction.save()
            # The regia just stepped past the last player by hand.
            transaction.on_commit(lambda: _backup_async(reason="asta esaurita"))
            return auction
        nxt.done = True
        nxt.save(update_fields=["done"])
        _set_on_block(auction, nxt.player, arm_timer=arm)
        auction.status = Auction.Status.LIVE
        auction.save()
        return auction

    if direction in ("prev", "prev_role"):
        cur = (
            auction.queue_items.filter(player_id=auction.player_id).first()
            if auction.player_id else None
        )
        prev_item = _prev_target(auction, direction, cur)
        if prev_item is None:
            return auction
        # Stepping back onto a lot that was already knocked down would put an
        # owned player up for sale a second time — the first buyer would keep
        # the charge while someone else takes the player. Undoing the sale is
        # the only safe way back, and it needs the admin to say so explicitly:
        # without ``undo_sale`` we return untouched, flagging what the caller
        # must confirm.
        sale = _sale_to_undo(auction, prev_item.player)
        if sale is not None:
            if not undo_sale:
                auction.needs_undo_confirm = {
                    "player_id": prev_item.player_id,
                    "player_name": prev_item.player.name,
                    "winner_name": sale.winner_name or "—",
                    "amount": str(prev_item.player.cost or sale.amount),
                }
                return auction
            _undo_sale(auction, sale, prev_item.player)
        # Return the current un-won player to the queue so Successivo can come
        # back to them (skip this when a winning bid is already standing).
        if cur is not None and (auction.best_bid is None or auction.best_bid.cancelled):
            cur.done = False
            cur.save(update_fields=["done"])
        auction.current_cycle = auction.current_cycle + 1
        auction.best_bid = None
        auction.ends_at = None
        auction.remaining_seconds = None
        _set_on_block(auction, prev_item.player)
        auction.status = Auction.Status.LIVE
        auction.save()
        return auction

    return auction


def _record_winner_spend(auction, *, assigned_by="auto"):
    """Idempotently finalise the current cycle: charge the winner once, record
    the result, update roster ownership and write a RosterLog entry.

    Idempotency is enforced by the unique (auction, cycle) AuctionCycleResult
    row: once a cycle is marked ``assigned`` the spend is never re-applied, so
    a double close / reset cannot charge twice. Callers run inside an atomic
    block with the auction row locked (``select_for_update``), so the
    get-or-create + check below is serialised per auction.
    """
    best = auction.best_bid
    if not best or best.cancelled:
        return None

    result, _created = AuctionCycleResult.objects.get_or_create(
        auction=auction, cycle=auction.current_cycle,
    )
    if result.assigned:
        return result  # already charged for this cycle — no-op

    result.winner      = best.participant
    result.winner_name = best.participant.display_name
    result.amount      = best.amount
    if auction.player_id:
        result.player      = auction.player
        result.player_name = auction.player.name
        result.player_role = auction.player.role
    result.assigned    = True
    result.assigned_at = timezone.now()
    result.assigned_by = (assigned_by or "auto")[:80]
    result.save()

    Participant.objects.filter(pk=best.participant_id).update(
        spent_credits=F("spent_credits") + best.amount
    )

    # Transfer roster ownership when a concrete Player was on the block.
    if auction.player_id:
        Player.objects.filter(pk=auction.player_id).update(
            owner=best.participant, cost=best.amount
        )
        _contracts_after_sale(auction.player_id, best.participant, best.amount, auction)

    RosterLog.objects.create(
        participant=best.participant,
        participant_name=best.participant.display_name,
        player_name=result.player_name or auction.title,
        player_role=result.player_role,
        action=RosterLog.Action.ASSIGN,
        credits_delta=best.amount,
        by_admin=(assigned_by not in ("", "auto")),
        note=f"Asta #{auction.id} ciclo {auction.current_cycle}",
    )
    logger.info(
        "Lotto ASSEGNATO [Asta #%s ciclo %s]: %s aggiudicato a '%s' per %s FM",
        auction.id, auction.current_cycle, result.player_name or auction.title,
        best.participant.display_name, best.amount,
    )
    return result


def _contracts_after_sale(player_id, winner, amount, auction):
    """Regolamento 4: contratto da tirare per chi compra; se il giocatore era
    stato rescisso al rinnovo, l'incasso dell'asta va alla squadra che lo aveva."""
    from .contracts import on_player_acquired

    player = Player.objects.select_related("owner", "owner__league", "rescinded_from").get(pk=player_id)
    on_player_acquired(player)
    former = player.rescinded_from
    if former is None:
        return
    if former.id != winner.id:
        from .contracts import contract_rules
        from .salary import add_credits

        league = former.league
        cap = (contract_rules(league)["rescind_proceeds_cap"].get(player.role) if league else None)
        proceeds = min(amount, Decimal(cap)) if cap else amount
        add_credits(former, proceeds,
                    f"Incasso asta di {player.name} (rescisso al rinnovo) · asta #{auction.id}")
    Player.objects.filter(pk=player_id).update(rescinded_from=None)


def _record_unsold(auction):
    """Record the current lot as *invenduto* (no winner) in the cycle history.

    Mirrors :func:`_record_winner_spend`'s idempotency: at most one
    AuctionCycleResult per (auction, cycle). Only writes a player snapshot when
    a concrete player was on the block, and never downgrades a row that is
    already an assignment. Used so the Storico panel shows unsold lots too.
    """
    if not auction.player_id:
        return None
    result, _created = AuctionCycleResult.objects.get_or_create(
        auction=auction, cycle=auction.current_cycle,
    )
    if result.assigned:
        return result  # already a recorded sale — leave it
    result.player      = auction.player
    result.player_name = auction.player.name
    result.player_role = auction.player.role
    result.save(update_fields=["player", "player_name", "player_role"])
    return result


@transaction.atomic
def release_player(player_id, *, auction_id=None, by_admin=False, participant_id=None):
    """Release (svincola) an owned player back to the free-agent pool.

    Refund policy comes from the auction in context (``auction_id``):
    ``PURCHASE`` returns what was paid (``Player.cost``), ``CURRENT`` returns
    the player's current listone quotazione (``Player.initial_price``), and
    ``NONE`` returns nothing. The refund lowers the owner's ``spent_credits``
    (floored at 0 so a roster can never yield more credits than it cost),
    writes a ``RosterLog`` audit row, and frees the player.

    Permission: admins may release anyone's player; a participant may only
    release a player they currently own (``participant_id`` must match).
    Returns a JSON-safe dict with ``ok`` plus, on success, the applied refund.
    """
    try:
        player = Player.objects.select_for_update().get(pk=player_id)
    except Player.DoesNotExist:
        return {"ok": False, "error": "player_not_found"}

    if player.owner_id is None:
        return {"ok": False, "error": "not_owned"}

    if not by_admin:
        if participant_id is None or int(participant_id) != player.owner_id:
            return {"ok": False, "error": "forbidden"}

    owner = Participant.objects.select_for_update().get(pk=player.owner_id)

    refund_mode = Auction.RefundMode.PURCHASE
    auction = Auction.objects.filter(pk=auction_id).first() if auction_id else None
    if auction is not None:
        refund_mode = auction.release_refund_mode

    if refund_mode == Auction.RefundMode.CURRENT:
        refund = player.initial_price or Decimal("0")
    elif refund_mode == Auction.RefundMode.NONE:
        refund = Decimal("0")
    else:
        refund = player.cost or Decimal("0")

    # Never let a refund push spent below zero (would mint free credits).
    new_spent = owner.spent_credits - refund
    if new_spent < 0:
        new_spent = Decimal("0")
    applied = owner.spent_credits - new_spent
    owner.spent_credits = new_spent
    owner.save(update_fields=["spent_credits"])

    RosterLog.objects.create(
        participant=owner,
        participant_name=owner.display_name,
        player_name=player.name,
        player_role=player.role,
        action=RosterLog.Action.ADMIN_RELEASE if by_admin else RosterLog.Action.RELEASE,
        credits_delta=applied,
        by_admin=by_admin,
        note=f"Svincolo · rimborso {refund_mode}" + (f" · asta #{auction.id}" if auction else ""),
    )

    player.owner = None
    player.cost = Decimal("0")
    player.save(update_fields=["owner", "cost"])

    # A released player rejoins the running order of an ordered auction, at the
    # end of its own role (or the absolute end when not grouped by role).
    if auction is not None and auction.flow_mode != Auction.FlowMode.CALL:
        enqueue_released_player(auction, player)

    return {
        "ok": True,
        "player_id": player.id,
        "player_name": player.name,
        "owner_id": owner.id,
        "refund": str(applied),
        "refund_mode": refund_mode,
        "remaining_credits": str(owner.remaining_credits),
        "spent_credits": str(owner.spent_credits),
    }


@transaction.atomic
def assign_player(player_id, participant_id, *, price=None, by_admin=True, note=""):
    """Manually assign (or re-assign / price-correct) a player to a participant.

    The admin counterpart to :func:`release_player`: it lets the regista fix a
    mis-click, settle an off-microphone deal, or pre-load a roster without
    running an auction cycle. Money bookkeeping mirrors the auction path:

    * **New assignment** (player was a free agent): the new owner is charged
      ``price`` (``spent_credits += price``).
    * **Re-assignment** (player owned by someone else): the previous owner is
      first refunded what they had paid (``Player.cost``, floored at 0 so credits
      are never minted), then the new owner is charged ``price``.
    * **Price correction** (same owner, different price): only the delta is
      applied to that owner's ``spent_credits``.

    ``price`` defaults to the player's current listone quotazione
    (``initial_price``). A ``RosterLog`` row (ADMIN_ASSIGN, plus an
    ADMIN_RELEASE for any displaced previous owner) records every move. This is
    an admin override tool, so roster-slot / budget-reserve limits are NOT
    enforced here. Returns a JSON-safe dict.
    """
    try:
        player = Player.objects.select_for_update().get(pk=player_id)
    except Player.DoesNotExist:
        return {"ok": False, "error": "player_not_found"}
    try:
        new_owner = Participant.objects.select_for_update().get(pk=participant_id)
    except Participant.DoesNotExist:
        return {"ok": False, "error": "participant_not_found"}

    price = Decimal(str(price)) if price is not None else (player.initial_price or Decimal("0"))
    if price < 0:
        price = Decimal("0")

    prev_owner_id = player.owner_id

    # Price-only correction: same owner keeps the player, adjust the delta.
    if prev_owner_id == new_owner.id:
        delta = price - (player.cost or Decimal("0"))
        new_spent = max(Decimal("0"), new_owner.spent_credits + delta)
        new_owner.spent_credits = new_spent
        new_owner.save(update_fields=["spent_credits"])
        player.cost = price
        player.save(update_fields=["cost"])
        RosterLog.objects.create(
            participant=new_owner, participant_name=new_owner.display_name,
            player_name=player.name, player_role=player.role,
            action=RosterLog.Action.EDIT, credits_delta=delta, by_admin=by_admin,
            note=(note or "Correzione prezzo")[:200],
        )
        return {
            "ok": True, "player_id": player.id, "player_name": player.name,
            "owner_id": new_owner.id, "owner_name": new_owner.display_name,
            "price": str(price), "corrected": True,
            "remaining_credits": str(new_owner.remaining_credits),
        }

    # Refund the displaced previous owner (if any) what they had paid.
    if prev_owner_id is not None:
        prev = Participant.objects.select_for_update().get(pk=prev_owner_id)
        refund = player.cost or Decimal("0")
        prev.spent_credits = max(Decimal("0"), prev.spent_credits - refund)
        prev.save(update_fields=["spent_credits"])
        RosterLog.objects.create(
            participant=prev, participant_name=prev.display_name,
            player_name=player.name, player_role=player.role,
            action=RosterLog.Action.ADMIN_RELEASE, credits_delta=refund, by_admin=by_admin,
            note=f"Riassegnato a {new_owner.display_name}"[:200],
        )

    # Charge the new owner and transfer ownership.
    new_owner.spent_credits = new_owner.spent_credits + price
    new_owner.save(update_fields=["spent_credits"])
    player.owner = new_owner
    player.cost = price
    player.save(update_fields=["owner", "cost"])
    from .contracts import on_player_acquired
    on_player_acquired(player)

    RosterLog.objects.create(
        participant=new_owner, participant_name=new_owner.display_name,
        player_name=player.name, player_role=player.role,
        action=RosterLog.Action.ADMIN_ASSIGN, credits_delta=price, by_admin=by_admin,
        note=(note or "Assegnazione manuale")[:200],
    )
    return {
        "ok": True, "player_id": player.id, "player_name": player.name,
        "owner_id": new_owner.id, "owner_name": new_owner.display_name,
        "price": str(price), "corrected": False,
        "remaining_credits": str(new_owner.remaining_credits),
    }


@transaction.atomic
def cancel_bid(bid_id, reason="cancelled_by_admin"):
    """Cancel a bid from the "registro offerte" panel — only ever the
    *winning* bid of its round. An outbid, non-winning offer carries no
    consequence of its own once something else leads or won that round, so
    there is nothing meaningful to undo there; refused as ``not_winning_bid``.

    What "winning" undoes depends on whether the round has closed:

    * **Still open** (the live current round): the winning bid is whichever
      one is currently the auction's leader (``auction.best_bid``).
      Cancelling it un-does the lead and recomputes the price from what is
      left of that same round — a live mis-click correction.
    * **Already resolved** (an ``AuctionCycleResult`` was recorded): the
      winning bid is the one that actually got charged. Cancelling it here
      properly undoes the sale — refunds the buyer, frees the player, marks
      the round unassigned again, and re-queues the player for an ordered
      running order — instead of only marking the Bid row cancelled while
      the player stayed sold and the credits stayed spent (that used to
      look exactly like a broken assignment: bug report "il giocatore
      comprato non risulta assegnato").

    Returns ``{"ok": True, "auction": auction}`` on success, or
    ``{"ok": False, "error": <code>}`` — ``not_winning_bid`` or (a resolved
    round whose player was already freed some other way) ``already_released``.
    """
    bid = Bid.objects.select_for_update().get(pk=bid_id)
    auction = Auction.objects.select_for_update().get(pk=bid.auction_id)

    result = (
        AuctionCycleResult.objects.select_for_update()
        .filter(auction=auction, cycle=bid.cycle).first()
    )

    if result is not None and result.assigned:
        top_bid = (
            Bid.objects.filter(auction=auction, cycle=bid.cycle, accepted=True)
            .order_by("-amount", "server_received_at").first()
        )
        if top_bid is None or top_bid.id != bid.id:
            return {"ok": False, "error": "not_winning_bid"}

        player = Player.objects.select_for_update().filter(pk=result.player_id).first()
        if player is None or player.owner_id is None:
            return {"ok": False, "error": "already_released"}

        _undo_sale(auction, result, player)
        if auction.flow_mode != Auction.FlowMode.CALL:
            enqueue_released_player(auction, player)

        bid.cancelled = True
        bid.cancelled_at = timezone.now()
        bid.cancelled_reason = reason[:120]
        bid.save(update_fields=["cancelled", "cancelled_at", "cancelled_reason"])
        return {"ok": True, "auction": auction}

    if auction.best_bid_id != bid.id:
        return {"ok": False, "error": "not_winning_bid"}

    bid.cancelled = True
    bid.cancelled_at = timezone.now()
    bid.cancelled_reason = reason[:120]
    bid.save(update_fields=["cancelled", "cancelled_at", "cancelled_reason"])

    _recompute_price(auction)
    return {"ok": True, "auction": auction}


def _recompute_price(auction):
    """Re-derive the current lot's leader/price after a bid is cancelled.

    Scoped to ``auction.current_cycle`` — without this, cancelling a bid on
    an already-closed lot searched every cycle's history for the highest
    surviving bid and could resurrect an unrelated past lot's amount as the
    *current* one's price/leader (e.g. undoing a mis-click on a much earlier
    round while a later round is live would overwrite that later round's
    price with the earlier round's leftover bid).
    """
    best = (
        Bid.objects.filter(
            auction=auction, cycle=auction.current_cycle, accepted=True, cancelled=False,
        )
        .order_by("-amount", "server_received_at")
        .first()
    )
    auction.best_bid = best
    auction.current_price = best.amount if best else auction.starting_price
    auction.save(update_fields=["current_price", "best_bid", "updated_at"])
