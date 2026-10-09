"""Asta alle buste (§3.1 E) logic and workflows."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..models import Auction, Bid, Participant, SealedBid
from . import stall
from .common import (
    Reject, SealedResult, _credits_str, _check_roster_limits, participates_in,
)

# I campi di stato che ogni transizione dello scrutinio tocca insieme.
_SEALED_FIELDS = [
    "sealed_round", "sealed_floor", "sealed_ends_at",
    "sealed_contenders", "sealed_reveal",
]


def _sealed_triggered(auction, amount):
    """Questo rilancio fa scattare le buste?"""
    if not auction.sealed_bids or auction.sealed_open or not auction.player_id:
        return False
    threshold = auction.sealed_threshold_for(auction.player.role)
    return threshold > 0 and amount >= threshold


def _open_sealed(auction, last_amount, now):
    """Apri il primo giro di buste sul lotto in corso (il save tocca al chiamante)."""
    auction.sealed_round = 1
    auction.sealed_floor = Decimal(last_amount) + Decimal("1")
    auction.sealed_ends_at = now + timedelta(seconds=auction.sealed_seconds)
    auction.sealed_contenders = []
    auction.sealed_reveal = []
    # Le grida sono finite: fermando il timer del lotto niente scade mentre si
    # scrive, e sara' la risoluzione a farlo ripartire (a zero) per aggiudicare.
    auction.ends_at = None


def _clear_sealed(auction):
    """Riporta il lotto fuori dallo scrutinio (il save tocca al chiamante)."""
    auction.sealed_round = 0
    auction.sealed_floor = Decimal("0")
    auction.sealed_ends_at = None
    auction.sealed_contenders = []
    auction.sealed_reveal = []


def sealed_envelopes(auction, *, round_no=None):
    """Le buste di un giro, dalla piu' alta alla piu' bassa."""
    return list(
        SealedBid.objects.filter(
            auction=auction, cycle=auction.current_cycle,
            round=round_no if round_no is not None else auction.sealed_round,
        ).select_related("participant").order_by("-amount", "created_at")
    )


def sealed_can_take_part(auction, participant):
    """Questa squadra puo' presentare una busta in questo giro?

    Al primo giro tutti quelli che giocano l'asta; negli spareggi solo chi ha
    pareggiato (``sealed_contenders``).
    """
    if participant is None or not participates_in(participant, auction):
        return False
    if not participant.is_active:
        return False
    contenders = auction.sealed_contenders or []
    return not contenders or participant.id in contenders


def _sealed_rule_reject(auction, participant, value):
    """Le regole del rilancio che valgono anche alle buste, se la lega lo
    vuole (``sealed_enforce_rules``): senza, con una busta si vincerebbe un
    giocatore che a rilancio non si potrebbe comprare."""
    if not auction.sealed_enforce_rules:
        return None
    from .bidding import gk_clubs_problem, rescinded_rebuy_blocked
    from .salary import check_purchase
    if rescinded_rebuy_blocked(auction, participant):
        return Reject.RESCINDED_REBUY
    if auction.player_id and gk_clubs_problem(participant, auction.player):
        return Reject.GK_CLUBS
    if check_purchase(participant, value, auction=auction):
        return Reject.SALARY_CAP
    return None


@transaction.atomic
def place_sealed_bid(auction_id, participant_id, amount, *, received_at=None):
    """Consegna (o riscrivi) la busta di una squadra per il giro in corso.

    Le stesse verifiche di un rilancio - crediti, slot di reparto, riserva per
    gli slot vuoti - piu' il minimo del giro. Finche' il tempo non scade si puo'
    riscrivere: vale l'ultima consegnata. Il tempo si guarda all'arrivo della
    busta (``received_at``), non a quando il database riesce a scriverla.
    """
    now = timezone.now()
    try:
        auction = Auction.objects.select_for_update(of=("self",)).select_related("player").get(pk=auction_id)
    except Auction.DoesNotExist:
        return SealedResult(False, Reject.AUCTION_NOT_FOUND)
    moved = stall.give_back(auction, received_at, now)
    if moved:
        auction.save(update_fields=moved)
    if received_at is not None:
        now = min(received_at, now)
    try:
        participant = Participant.objects.select_for_update().get(pk=participant_id)
    except Participant.DoesNotExist:
        return SealedResult(False, Reject.PARTICIPANT_NOT_FOUND)

    if auction.status != Auction.Status.LIVE:
        return SealedResult(False, Reject.NOT_LIVE)
    if not auction.sealed_open:
        return SealedResult(False, Reject.SEALED_NOT_OPEN)
    if auction.sealed_ends_at is not None and now >= auction.sealed_ends_at:
        return SealedResult(False, Reject.SEALED_CLOSED)
    if not participant.is_active:
        return SealedResult(False, Reject.PARTICIPANT_INACTIVE)
    if not participates_in(participant, auction):
        return SealedResult(False, Reject.WRONG_LEAGUE)
    if not sealed_can_take_part(auction, participant):
        return SealedResult(False, Reject.SEALED_NOT_CONTENDER)

    try:
        value = Decimal(str(amount).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError, TypeError):
        return SealedResult(False, Reject.BAD_INCREMENT)
    value = value.quantize(Decimal("1"))
    if value < auction.sealed_floor:
        return SealedResult(False, Reject.SEALED_TOO_LOW, amount=value)
    if value > participant.remaining_credits:
        return SealedResult(False, Reject.INSUFFICIENT_CREDITS, amount=value)
    roster_reject = _check_roster_limits(auction, participant, value)
    if roster_reject is not None:
        return SealedResult(False, roster_reject, amount=value)
    rule_reject = _sealed_rule_reject(auction, participant, value)
    if rule_reject is not None:
        return SealedResult(False, rule_reject, amount=value)

    SealedBid.objects.update_or_create(
        auction=auction, cycle=auction.current_cycle,
        round=auction.sealed_round, participant=participant,
        defaults={"amount": value, "player": auction.player},
    )
    return SealedResult(True, "", amount=value)


@transaction.atomic
def resolve_sealed(auction_id, *, force=False):
    """Apri le buste del giro: aggiudica, oppure indici lo spareggio.

    ``force`` le apre subito senza aspettare il tempo (comando della regia).
    Ritorna l'asta con un ``sealed_event`` transitorio che dice cosa e' successo
    (``"won"`` / ``"tie"`` / ``"empty"``), o ``None`` se non c'era niente da
    aprire. Non chiude il lotto da se': gli fa scadere il timer, e la chiusura
    segue la strada di sempre.
    """
    now = timezone.now()
    try:
        auction = Auction.objects.select_for_update(of=("self",)).select_related(
            "player", "best_bid", "best_bid__participant").get(pk=auction_id)
    except Auction.DoesNotExist:
        return None
    if not auction.sealed_open:
        return None
    if not force and auction.sealed_ends_at is not None and now < auction.sealed_ends_at:
        return None

    envelopes = sealed_envelopes(auction)
    reveal = [{"team": e.participant.display_name,
               "amount": _credits_str(e.amount)} for e in envelopes]

    if not envelopes:
        # Nessuno ha consegnato: il lotto resta a chi era in testa alle grida
        # all'ultimo prezzo dichiarato (o invenduto, se non c'era nessuno).
        _clear_sealed(auction)
        auction.ends_at = now
        auction.save(update_fields=_SEALED_FIELDS + ["ends_at", "updated_at"])
        auction.sealed_event = "empty"
        return auction

    top = envelopes[0].amount
    winners = [e for e in envelopes if e.amount == top]

    if len(winners) > 1:
        auction.sealed_round += 1
        auction.sealed_floor = top + Decimal("1")
        auction.sealed_contenders = [e.participant_id for e in winners]
        auction.sealed_ends_at = now + timedelta(seconds=auction.sealed_seconds)
        auction.sealed_reveal = reveal
        auction.save(update_fields=_SEALED_FIELDS + ["updated_at"])
        auction.sealed_event = "tie"
        return auction

    winner = winners[0]
    bid = Bid.objects.create(
        auction=auction, participant=winner.participant,
        amount=winner.amount,
        increment=max(Decimal("0"), winner.amount - auction.current_price),
        server_received_at=now, accepted=True, cycle=auction.current_cycle,
        user_agent="busta", ip_address=None,
    )
    auction.current_price = winner.amount
    auction.best_bid = bid
    _clear_sealed(auction)
    auction.sealed_reveal = reveal
    # Il lotto e' deciso: farlo scadere adesso lo manda dritto alla chiusura
    # normale, che addebita il vincitore e fa salire il giocatore dopo.
    auction.ends_at = now
    auction.save(update_fields=_SEALED_FIELDS + [
        "current_price", "best_bid", "ends_at", "updated_at"])
    auction.sealed_event = "won"
    return auction


def sealed_tick(auction_id, as_of=None, grace=None):
    """Chiamata dal ticker: apri le buste se il tempo del giro e' finito.

    ``as_of`` e' quando il ticker ha chiesto: conta il tempo di allora, non
    quello dopo un'attesa in coda (vedi stall.py), e un fermo lungo restituisce
    prima i suoi secondi alle buste. ``grace``: apre solo se il giro e' finito
    da almeno tanto (lo scheduler)."""
    now = timezone.now()
    as_of = as_of or now
    if stall.stalled(as_of, now):
        with transaction.atomic():
            auction = Auction.objects.select_for_update(of=("self",)).filter(pk=auction_id).first()
            # L'ora dopo aver preso il database: il fermo finisce qui.
            moved = stall.give_back(auction, as_of, timezone.now()) if auction else []
            if moved:
                auction.save(update_fields=moved)
    pending = Auction.objects.filter(
        pk=auction_id, sealed_round__gt=0, sealed_ends_at__lte=as_of - (grace or timedelta(0))
    ).exists()
    if not pending:
        return None
    return resolve_sealed(auction_id)


def open_sealed_now(auction_id):
    """Comando della regia: manda il lotto alle buste senza aspettare la soglia.

    Serve quando la sala decide di andare a scrutinio comunque (o quando la
    soglia e' stata alzata a mano a meta' asta). Rifiutato se non c'e' un lotto
    vivo o se lo scrutinio e' gia' aperto: ``sealed_error`` dice quale dei due.
    """
    with transaction.atomic():
        auction = Auction.objects.select_for_update(of=("self",)).select_related("player").get(pk=auction_id)
        auction.sealed_error = None
        if auction.status != Auction.Status.LIVE or auction.player_id is None:
            auction.sealed_error = "timer_not_running"
            return auction
        if auction.sealed_open:
            auction.sealed_error = Reject.SEALED_ACTIVE
            return auction
        _open_sealed(auction, auction.current_price, timezone.now())
        auction.save(update_fields=_SEALED_FIELDS + ["ends_at", "updated_at"])
        return auction


def sealed_status(auction, participant=None):
    """Lo stato dello scrutinio per una singola squadra (busta propria inclusa).

    Le buste altrui non escono mai da qui: solo quante ne sono state consegnate.
    """
    data = {
        "active": auction.sealed_open,
        "round": auction.sealed_round,
        "floor": _credits_str(auction.sealed_floor),
        "ends_at": auction.sealed_ends_at.isoformat() if auction.sealed_ends_at else None,
        "seconds_left": None,
        "submitted": 0,
        "your_amount": None,
        "can_bid": False,
        "reveal": auction.sealed_reveal or [],
        "threshold": (auction.sealed_threshold_for(auction.player.role)
                      if auction.player_id else 0),
        "enabled": auction.sealed_bids,
    }
    if auction.sealed_ends_at is not None:
        data["seconds_left"] = round(
            max(0.0, (auction.sealed_ends_at - timezone.now()).total_seconds()), 1)
    if auction.sealed_open:
        data["submitted"] = SealedBid.objects.filter(
            auction=auction, cycle=auction.current_cycle, round=auction.sealed_round
        ).count()
        if participant is not None:
            data["can_bid"] = sealed_can_take_part(auction, participant)
            mine = SealedBid.objects.filter(
                auction=auction, cycle=auction.current_cycle,
                round=auction.sealed_round, participant=participant,
            ).first()
            data["your_amount"] = _credits_str(mine.amount) if mine else None
    return data
