"""Common constants, errors, and shared validation helpers for auction services."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..models import (
    Bid, Player,
)


class Reject:
    AUCTION_NOT_FOUND    = "auction_not_found"
    PARTICIPANT_NOT_FOUND = "participant_not_found"
    PARTICIPANT_INACTIVE  = "participant_inactive"
    NOT_LIVE              = "auction_not_live"
    EXPIRED               = "timer_expired"
    BAD_INCREMENT         = "increment_not_allowed"
    RATE_LIMITED          = "rate_limited"
    INSUFFICIENT_CREDITS  = "insufficient_credits"
    ROSTER_SLOT_FULL      = "roster_slot_full"
    BUDGET_RESERVE        = "budget_reserve"
    WRONG_LEAGUE          = "wrong_league"
    ALREADY_LEADING       = "already_leading"
    # Asta alle buste (§3.1 E).
    SEALED_ACTIVE         = "sealed_active"
    SEALED_NOT_OPEN       = "sealed_not_open"
    SEALED_CLOSED         = "sealed_closed"
    SEALED_TOO_LOW        = "sealed_too_low"
    SEALED_NOT_CONTENDER  = "sealed_not_contender"
    # Contratti (regolamento 4.1): chi ha perso il giocatore col dado rinnovo
    # non può ricomprarlo all'asta successiva.
    RESCINDED_REBUY       = "rescinded_rebuy"
    # Tetto salariale (regolamento 1.2).
    SALARY_CAP            = "salary_cap"
    # Portieri di troppe squadre di Serie A (regolamento 2.02).
    GK_CLUBS              = "gk_clubs"


# Italian labels for every code the UI can receive — rejected bids plus the
# error strings the JSON views return. Single source of truth: both the bidder
# page and the regia console render from this map, so a new code can never show
# up on screen as a bare identifier.
ERROR_LABELS = {
    "gk_clubs": "Hai già portieri di due squadre di Serie A: puoi prendere solo portieri di quelle squadre",
    "salary_cap": "Tetto salariale raggiunto: non puoi spendere di più in questo mercato",
    "rescinded_rebuy": "Hai perso questo giocatore al rinnovo: non puoi ricomprarlo in questo mercato",
    Reject.AUCTION_NOT_FOUND:     "Asta non trovata",
    Reject.PARTICIPANT_NOT_FOUND: "Squadra non trovata",
    Reject.PARTICIPANT_INACTIVE:  "Squadra disattivata",
    Reject.NOT_LIVE:              "Asta non attiva",
    Reject.EXPIRED:               "Tempo scaduto",
    Reject.BAD_INCREMENT:         "Rilancio non valido",
    Reject.RATE_LIMITED:          "Troppe offerte, rallenta",
    Reject.INSUFFICIENT_CREDITS:  "Crediti insufficienti",
    Reject.ROSTER_SLOT_FULL:      "Reparto già completo per questa squadra",
    Reject.BUDGET_RESERVE:        "Servono crediti per gli slot ancora vuoti",
    Reject.WRONG_LEAGUE:          "Questa squadra non partecipa a quest'asta",
    Reject.ALREADY_LEADING:       "Stai già vincendo questo giocatore",
    Reject.SEALED_ACTIVE:         "Si è alle buste: niente rilanci, scrivi la tua offerta",
    Reject.SEALED_NOT_OPEN:       "Nessuno scrutinio aperto in questo momento",
    Reject.SEALED_CLOSED:         "Buste chiuse, tempo scaduto",
    Reject.SEALED_TOO_LOW:        "Offerta sotto il minimo di questo scrutinio",
    Reject.SEALED_NOT_CONTENDER:  "Allo spareggio partecipa solo chi ha pareggiato",
    # View-level errors.
    "no_session":         "Sessione scaduta, rientra",
    "no_participant":     "Nessuna squadra selezionata",
    "player_not_found":   "Giocatore non trovato",
    "not_owned":          "Giocatore non in rosa",
    "player_unavailable": "Giocatore non disponibile (già assegnato o inesistente)",
    "forbidden":          "Operazione non consentita",
    "league_mismatch":    "Giocatore, squadra e asta devono essere della stessa lega",
    "bad_direction":      "Comando non valido",
    "bad_delta":          "Valore timer non valido",
    "out_of_range":       "Fuori intervallo (max ±600s)",
    "timer_not_running":  "Il timer non è ancora partito: parte alla prima offerta",
    "needs_undo_confirm": "Serve conferma per annullare l'aggiudicazione",
    "not_winning_bid": "Solo l'offerta vincente del round può essere annullata",
    "bid_in_progress": "C'è già un'offerta in corso: non si può saltare, si aspetta il timer",
    "already_released": "Il giocatore è già stato svincolato in altro modo",
}


def participates_in(participant, auction):
    """Is this team actually taking part in this auction?

    A team belongs to its league and carries its roster and credits from one of
    that league's auctions to the next (the repair auction continues where the
    main one left off) — but it has no business bidding in a *different*
    league's auction, where its budget and slots mean nothing.

    Legacy rows predate leagues: when either side has no league we cannot tell
    them apart, so we allow it rather than locking existing installs out.
    """
    if participant is None or auction is None:
        return False
    if participant.league_id is None or auction.league_id is None:
        return True
    return participant.league_id == auction.league_id


@dataclass
class BidResult:
    accepted: bool
    bid: Bid
    reason: str = ""
    extended: bool = False


@dataclass
class SealedResult:
    accepted: bool
    reason: str = ""
    amount: Decimal = Decimal("0")


def _credits_str(value):
    """Crediti come li scrive la sala: 160, non 160.00 (i decimali solo se ci sono)."""
    value = Decimal(value)
    if value == value.to_integral_value():
        return str(value.quantize(Decimal("1")))
    return str(value)


def _check_roster_limits(auction, participant, new_amount):
    """Validate a prospective winning bid against roster slot + budget reserve.

    Returns a ``Reject.*`` reason string when the bid would violate a limit,
    or ``None`` when it is allowed. Enforcement is deliberately scoped:

    * It is skipped entirely when ``auction.enforce_limits`` is off (the admin
      override) — useful for free-form or non-fantacalcio auctions.
    * It applies **only** to bidders tied to a ``League``. Legacy participants
      (``league_id is None``) behave exactly as before, so existing
      single-auction installs are untouched.

    Two rules, both using the bidder's league config:
    * **Slot**: when a concrete ``Player`` is on the block, the bidder must
      still have a free slot for that player's role.
    * **Reserve**: after winning this lot the bidder must keep at least one
      credit for every still-empty roster slot, so they can complete the rosa.
    """
    if not auction.enforce_limits:
        return None
    cfg = participant.league
    if cfg is None:
        return None
    # "Nessun limite slot": the rosa has no shape to respect, so neither the
    # per-role cap nor the empty-slot reserve applies — only the budget does.
    if not cfg.slot_limits:
        return None

    owned_total = Player.objects.filter(owner=participant, abroad_list=False).count()

    role = auction.player.role if auction.player_id else ""
    if role:
        # In Mantra il tetto non è per reparto ma per portieri/movimento, quindi
        # si contano tutti i ruoli che condividono lo stesso slot (vedi
        # ``League.slot_roles``). In Classic l'insieme è il singolo ruolo.
        owned_role = Player.objects.filter(
            owner=participant, abroad_list=False, role__in=cfg.slot_roles(role)).count()
        if owned_role >= cfg.slots_for(role):
            return Reject.ROSTER_SLOT_FULL

    slots_after_win = cfg.total_slots - (owned_total + 1)
    if slots_after_win > 0 and new_amount > participant.remaining_credits - slots_after_win:
        return Reject.BUDGET_RESERVE

    return None


def _normalize_increment(auction, increment):
    try:
        inc = Decimal(str(increment))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if inc <= 0:
        return None
    allowed = auction.allowed_increments()
    if inc in allowed or inc >= auction.min_increment:
        return inc
    return None
