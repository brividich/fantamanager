"""Trades (scambi) between teams of the same league."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Participant, Player, RosterLog, Trade, TradeWindow
from .sala import ensure_unlocked as _sala_guard

logger = logging.getLogger("auctions.trade")


def _err(message):
    return {"ok": False, "message": message}


def _credits(raw):
    try:
        val = Decimal(str(raw or 0).strip().replace(",", ".")).quantize(Decimal("1"))
    except (InvalidOperation, ValueError):
        return None
    return val if val >= 0 else None


def _ids(raw):
    out = []
    for x in raw or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _slot_problem(league, participant, gives, gets):
    """Why ``participant`` can't take this trade for roster slots, or ''."""
    if not league.slot_limits:
        return ""
    owned = Counter(Player.objects.filter(owner=participant, abroad_list=False).values_list("role", flat=True))
    delta = Counter(p.role for p in gets)
    delta.subtract(Counter(p.role for p in gives))
    checked = set()
    for role in ("P", "D", "C", "A"):
        bucket = league.slot_roles(role)
        if bucket in checked:
            continue
        checked.add(bucket)
        cap = league.slots_for(role)
        change = sum(delta[r] for r in bucket)
        if cap and change > 0 and sum(owned[r] for r in bucket) + change > cap:
            label = "movimento" if len(bucket) > 1 else role
            return f"{participant.display_name} supererebbe il limite di {cap} slot per il ruolo {label}"
    return ""


def trade_window_status(league, now=None):
    """(open?, current_or_next_window) for the league's trade windows.

    No windows defined = trades allowed all season (open, None).
    """
    now = now or timezone.now()
    windows = list(TradeWindow.objects.filter(league=league))
    if not windows:
        return True, None
    for w in windows:
        if w.opens_at <= now <= w.closes_at:
            return True, w
    upcoming = [w for w in windows if w.opens_at > now]
    return False, (min(upcoming, key=lambda w: w.opens_at) if upcoming else None)


def _window_problem(league):
    is_open, nxt = trade_window_status(league)
    if is_open:
        return ""
    if nxt is not None:
        return (f"Il periodo scambi è chiuso: il prossimo ({nxt.name}) apre il "
                f"{timezone.localtime(nxt.opens_at):%d/%m/%Y alle %H:%M}.")
    return "Il periodo scambi è chiuso."


def _validate(trade, proposer_players, receiver_players):
    """Every rule a trade must satisfy, both when proposed and when executed."""
    league = trade.league
    proposer, receiver = trade.proposer, trade.receiver
    if not league.trades_enabled:
        return "Gli scambi non sono attivi in questa lega."
    if proposer.id == receiver.id:
        return "Non puoi proporre uno scambio a te stesso."
    for p in (proposer, receiver):
        if p.league_id != league.id or not p.is_active:
            return f"{p.display_name} non può partecipare a scambi in questa lega."
    if not proposer_players and not receiver_players:
        return "Lo scambio deve includere almeno un calciatore."
    for pl in list(proposer_players) + list(receiver_players):
        if pl.loan_from_id:
            return f"{pl.name} è in prestito: può trattarlo solo chi ha il cartellino, quando rientra."
    is_loan = trade.kind == Trade.Kind.LOAN
    if is_loan:
        for pl in list(proposer_players) + list(receiver_players):
            if pl.contract_years is not None and trade.loan_sessions > max(1, pl.contract_years * 2):
                return f"Il prestito di {pl.name} non può durare oltre il suo contratto ({pl.contract_years} anni)."
    else:
        # 5.01: chi è stato comprato in questa sessione non si vende (scambi e
        # prestiti invece sì): vendere = cederlo per soli crediti.
        from .contracts import release_problem
        for sellers, gets in ((proposer_players, receiver_players), (receiver_players, proposer_players)):
            if sellers and not gets:
                for pl in sellers:
                    problem = release_problem(pl)
                    if problem and "acquistato" in problem:
                        return problem
    # 5.04: negli scambi alla pari stesso numero di giocatori e ruoli; le
    # vendite per crediti (5.08) e i prestiti (5.07) sono liberi.
    if league.trades_same_roles and not is_loan and proposer_players and receiver_players:
        give = Counter(p.role for p in proposer_players)
        get = Counter(p.role for p in receiver_players)
        if give != get:
            def fmt(c):
                return " ".join(f"{c[r]}{r}" for r in "PDCA" if c[r]) or "nessuno"
            return ("Lo scambio deve spostare lo stesso numero di giocatori per ruolo da entrambe le parti "
                    f"(cedi {fmt(give)}, ricevi {fmt(get)}).")
    for pl in proposer_players:
        if pl.owner_id != proposer.id:
            return f"{pl.name} non è più nella rosa di {proposer.display_name}."
    for pl in receiver_players:
        if pl.owner_id != receiver.id:
            return f"{pl.name} non è più nella rosa di {receiver.display_name}."
    if trade.proposer_credits > 0 and trade.receiver_credits > 0:
        return "I crediti possono passare in una sola direzione."
    if proposer.remaining_credits < trade.proposer_credits:
        return f"{proposer.display_name} non ha abbastanza crediti ({proposer.remaining_credits:.0f} FM)."
    if receiver.remaining_credits < trade.receiver_credits:
        return f"{receiver.display_name} non ha abbastanza crediti ({receiver.remaining_credits:.0f} FM)."
    return (
        _slot_problem(league, proposer, proposer_players, receiver_players)
        or _slot_problem(league, receiver, receiver_players, proposer_players)
    )


@transaction.atomic
def propose_trade(proposer_id, receiver_id, give_ids=(), get_ids=(),
                  give_credits=0, get_credits=0, message="", kind="definitive", loan_sessions=1):
    proposer = Participant.objects.select_related("league").filter(pk=proposer_id).first()
    receiver = Participant.objects.filter(pk=receiver_id).first()
    if proposer is None or receiver is None or proposer.league is None:
        return _err("Squadra non trovata.")
    pc, rc = _credits(give_credits), _credits(get_credits)
    if pc is None or rc is None:
        return _err("Importo crediti non valido.")

    window = _window_problem(proposer.league)
    if window:
        return _err(window)

    give = list(Player.objects.filter(pk__in=_ids(give_ids)))
    get = list(Player.objects.filter(pk__in=_ids(get_ids)))
    if kind not in Trade.Kind.values:
        kind = Trade.Kind.DEFINITIVE
    try:
        loan_sessions = max(1, min(8, int(loan_sessions or 1)))
    except (TypeError, ValueError):
        loan_sessions = 1
    trade = Trade(
        league=proposer.league, proposer=proposer, receiver=receiver,
        proposer_credits=pc, receiver_credits=rc, message=(message or "").strip()[:200],
        kind=kind, loan_sessions=loan_sessions,
    )
    problem = _validate(trade, give, get)
    if problem:
        return _err(problem)

    duplicate = Trade.objects.filter(
        proposer=proposer, receiver=receiver, status=Trade.Status.PENDING,
    )
    for t in duplicate:
        if (
            set(t.proposer_players.values_list("id", flat=True)) == {p.id for p in give}
            and set(t.receiver_players.values_list("id", flat=True)) == {p.id for p in get}
            and t.proposer_credits == pc and t.receiver_credits == rc
        ):
            return _err("Hai già proposto questo stesso scambio.")

    trade.save()
    trade.proposer_players.set(give)
    trade.receiver_players.set(get)
    logger.info(f"Trade proposed: #{trade.id} {proposer.display_name} -> {receiver.display_name}")
    return {"ok": True, "trade_id": trade.id}


def _close(trade, status, note=""):
    trade.status = status
    trade.status_note = note[:300]
    trade.responded_at = trade.responded_at or timezone.now()
    trade.save(update_fields=["status", "status_note", "responded_at"])


def _execute(trade):
    """Re-validate and carry out the swap. Returns the service result dict."""
    trade = Trade.objects.select_for_update().select_related(
        "league", "proposer", "receiver"
    ).get(pk=trade.pk)
    _sala_guard(trade.league_id)
    proposer = Participant.objects.select_for_update().get(pk=trade.proposer_id)
    receiver = Participant.objects.select_for_update().get(pk=trade.receiver_id)
    trade.proposer, trade.receiver = proposer, receiver
    give = list(trade.proposer_players.select_for_update())
    get = list(trade.receiver_players.select_for_update())

    problem = _validate(trade, give, get)
    if problem:
        _close(trade, Trade.Status.FAILED, problem)
        return _err(f"Scambio non eseguibile: {problem}")

    is_loan = trade.kind == Trade.Kind.LOAN
    note = f"{'Prestito' if is_loan else 'Scambio'} #{trade.id}"
    for pl, new_owner, old_owner in (
        [(p, receiver, proposer) for p in give] + [(p, proposer, receiver) for p in get]
    ):
        pl.owner = new_owner
        if is_loan:
            pl.loan_from = old_owner
            pl.loan_sessions_left = trade.loan_sessions
        pl.save(update_fields=["owner", "loan_from", "loan_sessions_left"])
        for who, verb in ((old_owner, "ceduto a"), (new_owner, "arrivato da")):
            other = new_owner if who is old_owner else old_owner
            RosterLog.objects.create(
                participant=who, participant_name=who.display_name,
                player_name=pl.name, player_role=pl.role,
                action=RosterLog.Action.TRADE,
                credits_delta=Decimal("0"),
                note=f"{note}: {verb} {other.display_name}",
            )

    net = trade.proposer_credits - trade.receiver_credits  # >0: proposer pays
    if net:
        proposer.credits -= net
        receiver.credits += net
        proposer.save(update_fields=["credits"])
        receiver.save(update_fields=["credits"])

    trade.status = Trade.Status.COMPLETED
    trade.completed_at = timezone.now()
    trade.save(update_fields=["status", "completed_at"])

    # Other open trades that counted on the players just moved can no longer happen.
    moved = [p.id for p in give + get]
    stale = (
        Trade.objects.filter(status__in=Trade.OPEN_STATUSES)
        .exclude(pk=trade.pk)
        .filter(Q(proposer_players__in=moved) | Q(receiver_players__in=moved))
        .distinct()
    )
    for t in stale:
        _close(t, Trade.Status.FAILED, f"Superato dallo scambio #{trade.id}")

    logger.info(f"Trade completed: #{trade.id} {proposer.display_name} <-> {receiver.display_name}")
    return {"ok": True, "status": trade.status}


@transaction.atomic
def respond_trade(trade_id, participant_id, accept):
    trade = Trade.objects.select_for_update().select_related("league").filter(pk=trade_id).first()
    if trade is None or trade.receiver_id != int(participant_id):
        return _err("Scambio non trovato.")
    if trade.status != Trade.Status.PENDING:
        return _err("Questo scambio non è più in attesa di risposta.")
    if not accept:
        _close(trade, Trade.Status.REJECTED)
        return {"ok": True, "status": trade.status}
    window = _window_problem(trade.league)
    if window:
        return _err(window)

    trade.responded_at = timezone.now()
    if trade.league.trades_need_approval:
        trade.proposer = Participant.objects.get(pk=trade.proposer_id)
        trade.receiver = Participant.objects.get(pk=trade.receiver_id)
        problem = _validate(trade, list(trade.proposer_players.all()), list(trade.receiver_players.all()))
        if problem:
            _close(trade, Trade.Status.FAILED, problem)
            return _err(f"Scambio non eseguibile: {problem}")
        trade.status = Trade.Status.ACCEPTED
        trade.save(update_fields=["status", "responded_at"])
        return {"ok": True, "status": trade.status}
    trade.save(update_fields=["responded_at"])
    return _execute(trade)


@transaction.atomic
def cancel_trade(trade_id, participant_id):
    trade = Trade.objects.select_for_update().filter(pk=trade_id).first()
    if trade is None or trade.proposer_id != int(participant_id):
        return _err("Scambio non trovato.")
    if not trade.is_open:
        return _err("Lo scambio è già concluso.")
    _close(trade, Trade.Status.CANCELLED, "Ritirato da chi l'ha proposto")
    return {"ok": True, "status": trade.status}


@transaction.atomic
def decide_trade(trade_id, approve, note=""):
    """Admin ratification of an accepted trade."""
    trade = Trade.objects.select_for_update().filter(pk=trade_id).first()
    if trade is None:
        return _err("Scambio non trovato.")
    if trade.status != Trade.Status.ACCEPTED:
        return _err("Lo scambio non è in attesa di ratifica.")
    if not approve:
        _close(trade, Trade.Status.VETOED, note or "Bocciato dall'admin")
        return {"ok": True, "status": trade.status}
    return _execute(trade)
