"""Bidding operations and validations."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Auction, Bid, Participant
from .common import (
    Reject, BidResult, _normalize_increment, _check_roster_limits, participates_in,
)
from .sealed import _SEALED_FIELDS, _sealed_triggered, _open_sealed

logger = logging.getLogger("auctions.bidding")


@transaction.atomic
def place_bid(auction_id, participant_id, increment, *, user_agent="", ip_address=None):
    """Register a rilancio, or reject it with a single, predictable reason."""
    now = timezone.now()

    try:
        auction = Auction.objects.select_for_update().get(pk=auction_id)
    except Auction.DoesNotExist:
        logger.warning("Rilancio rifiutato: asta #%s non trovata", auction_id)
        return BidResult(False, bid=None, reason=Reject.AUCTION_NOT_FOUND)

    try:
        participant = Participant.objects.select_for_update().get(pk=participant_id)
    except Participant.DoesNotExist:
        logger.warning("Rilancio rifiutato: partecipante #%s non trovato", participant_id)
        return BidResult(False, bid=None, reason=Reject.PARTICIPANT_NOT_FOUND)

    def _reject(reason, inc=Decimal("0"), amount=None):
        bid = Bid.objects.create(
            auction=auction, participant=participant,
            amount=amount if amount is not None else auction.current_price,
            increment=inc, server_received_at=now, accepted=False,
            rejection_reason=reason, cycle=auction.current_cycle,
            user_agent=(user_agent or "")[:300], ip_address=ip_address,
        )
        logger.warning(
            "Rilancio RIFIUTATO [Asta #%s] da '%s': %s (inc: %s FM, prezzo attuale: %s FM)",
            auction.id, participant.display_name, reason, inc, auction.current_price,
        )
        return BidResult(False, bid=bid, reason=reason)

    if not participant.is_active:
        return _reject(Reject.PARTICIPANT_INACTIVE)

    if not participates_in(participant, auction):
        return _reject(Reject.WRONG_LEAGUE)

    if auction.status != Auction.Status.LIVE:
        return _reject(Reject.NOT_LIVE)

    if auction.ends_at is not None and auction.is_expired(now):
        return _reject(Reject.EXPIRED)

    # Alle buste non si grida più: il lotto è passato allo scrutinio segreto e
    # l'unica offerta valida è quella scritta (vedi place_sealed_bid).
    if auction.sealed_open:
        return _reject(Reject.SEALED_ACTIVE)

    # Anti double-click: the team already on top cannot raise itself.
    if (auction.block_leader_rebid
            and auction.best_bid_id
            and auction.best_bid.participant_id == participant.id):
        return _reject(Reject.ALREADY_LEADING)

    inc = _normalize_increment(auction, increment)
    if inc is None:
        return _reject(Reject.BAD_INCREMENT)

    # Credit check: can this participant afford to win at this new price?
    new_amount = auction.current_price + inc
    if new_amount > participant.remaining_credits:
        return _reject(Reject.INSUFFICIENT_CREDITS, inc=inc)

    # Roster slot + budget-reserve limits (league bidders only; admin-overridable).
    roster_reject = _check_roster_limits(auction, participant, new_amount)
    if roster_reject is not None:
        return _reject(roster_reject, inc=inc, amount=new_amount)

    interval = timedelta(milliseconds=settings.BID_MIN_INTERVAL_MS)
    recent = (
        Bid.objects.filter(participant=participant, server_received_at__gte=now - interval)
        .exclude(rejection_reason=Reject.RATE_LIMITED)
        .exists()
    )
    if recent:
        return _reject(Reject.RATE_LIMITED, inc=inc)

    if auction.ends_at is None:
        remaining_at_bid = None
    else:
        remaining_at_bid = max(0.0, round((auction.ends_at - now).total_seconds(), 3))

    bid = Bid.objects.create(
        auction=auction, participant=participant,
        amount=new_amount, increment=inc,
        server_received_at=now, accepted=True,
        cycle=auction.current_cycle,
        remaining_at_bid=remaining_at_bid,
        user_agent=(user_agent or "")[:300], ip_address=ip_address,
    )
    auction.current_price = new_amount
    auction.best_bid = bid

    player_name = auction.player.name if auction.player else "Lotto"
    logger.info(
        "Rilancio ACCETTATO [Asta #%s]: '%s' offre %s FM (+%s) su %s",
        auction.id, participant.display_name, new_amount, inc, player_name,
    )

    fields = ["current_price", "best_bid", "updated_at"]

    # Soglia del ruolo raggiunta: da qui in poi si va alle buste, e il timer
    # delle grida si ferma — il lotto non deve scadere mentre si scrive.
    if _sealed_triggered(auction, new_amount):
        _open_sealed(auction, new_amount, now)
        fields += _SEALED_FIELDS + ["ends_at"]
        auction.save(update_fields=fields)
        logger.info(
            "Soglia buste raggiunta [Asta #%s] a %s FM su %s: avviato scrutinio segreto",
            auction.id, new_amount, player_name,
        )
        return BidResult(True, bid=bid, reason="", extended=False)

    if auction.ends_at is None:
        auction.ends_at = now + timedelta(seconds=auction.duration_seconds)
        fields.append("ends_at")

    extended = False
    if auction.antisnipe_seconds and auction.ends_at is not None:
        remaining = (auction.ends_at - now).total_seconds()
        if 0 < remaining < auction.antisnipe_seconds:
            auction.ends_at = now + timedelta(seconds=auction.antisnipe_seconds)
            if "ends_at" not in fields:
                fields.append("ends_at")
            extended = True
            logger.info(
                "Antisnipe attivato [Asta #%s]: timer reimpostato a %ss",
                auction.id, auction.antisnipe_seconds,
            )

    auction.save(update_fields=fields)
    return BidResult(True, bid=bid, reason="", extended=extended)
