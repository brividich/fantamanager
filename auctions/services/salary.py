"""Economia delle società (regolamento, comma 3).

* 3.01 Budget: i crediti oltre ``budget_max`` (4500) vengono "bruciati".
* 3.02 Tetto salariale annuale e cumulativo: all'apertura della fase estiva
  ogni squadra riceve la base secondo la classifica finale dell'anno prima, a
  quella invernale i fondi del girone di ritorno (classifica attuale); a ogni
  fase si aggiungono le estensioni per i giocatori persi (punti per ruolo) e
  per i rinnovi mancati. La spesa (aste e buste, non le trattative private) si
  conta su tutta la stagione, lorda o netta a scelta della lega, e non può
  superare ``max_per_year`` (2500) in nessun caso.
* 3.03 Extra salary cap: prima delle aste si convertono 400 FM di budget in
  100 FM di tetto, finché l'asta non è iniziata.
* 3.04 Decreto Salvacalcio a metà e a fine stagione.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from ..models import (
    AuctionCycleResult,
    CapEntry,
    CapPhase,
    ContractEvent,
    DecreeAward,
    League,
    LeagueRanking,
    MarketBid,
    Participant,
    RosterLog,
)
from .sala import ensure_unlocked as _sala_guard

DEFAULT_RULES = {
    # Posizione 1..10 (oltre l'ultima vale l'ultimo valore).
    "base_by_rank": [1000, 1200, 1200, 1200, 1300, 1300, 1400, 1500, 1800, 1800],
    "winter_by_rank": [100, 100, 100, 200, 200, 200, 300, 300, 400, 500],
    # Giocatori persi: punti per ruolo e scaglioni [punti minimi, estensione].
    "lost_points": {"P": 0.5, "D": 0.5, "C": 0.75, "A": 1},
    "lost_bonus": [[2.5, 100], [4, 200], [6, 300], [8, 500]],
    # Per ogni giocatore non rinnovato (4.02).
    "renewal_lost_bonus": 50,
    "max_per_year": 2500,
    "budget_max": 4500,
    # Extra salary cap (3.03): ogni X FM di budget danno Y FM di tetto.
    "extra_cap_cost": 400,
    "extra_cap_gain": 100,
    "decree_mid": [50, 50, 50, 100, 100, 100, 100, 200, 200, 200],
    "decree_final": [300, 300, 300, 400, 400, 400, 500, 500, 800, 800],
    "prize_euro": [350, 200, 100, 50],
}

LOST_KINDS = (
    ContractEvent.Kind.NOT_RENEWED,
    ContractEvent.Kind.RESCINDED,
    "left",  # giocatore uscito dalla Serie A (estero, ritiro, svincolo): vedi 5.4
)


def rules(league):
    merged = dict(DEFAULT_RULES)
    merged.update({k: v for k, v in (league.salary_rules or {}).items() if k in DEFAULT_RULES})
    return merged


def by_rank(table, position):
    if not table or not position:
        return 0
    return table[min(position, len(table)) - 1]


def lost_bonus(table, points):
    bonus = 0
    for threshold, amount in sorted(table):
        if points >= threshold:
            bonus = amount
    return bonus


def add_credits(participant, amount, note, *, budget_max=None):
    """Accredita fantamilioni; la parte oltre il budget massimo si brucia (3.01)."""
    participant.refresh_from_db()
    _sala_guard(participant.league_id)
    amount = Decimal(amount)
    limit = Decimal(budget_max if budget_max is not None else rules(participant.league)["budget_max"] or 0)
    burned = Decimal("0")
    if limit and participant.remaining_credits + amount > limit:
        burned = min(amount, participant.remaining_credits + amount - limit)
    participant.credits += amount - burned
    participant.save(update_fields=["credits"])
    RosterLog.objects.create(
        participant=participant, participant_name=participant.display_name, player_name=note[:120],
        action=RosterLog.Action.EDIT, credits_delta=-(amount - burned), by_admin=True,
        note=(f"{note} (+{amount:.0f} FM" + (f", {burned:.0f} bruciati oltre {limit:.0f}" if burned else "") + ")")[:200],
    )
    return amount - burned


# --- Classifiche ---------------------------------------------------------------

def app_ranking(league):
    """Classifica dalle giornate calcolate in FantaManager, se ci sono."""
    from ..models import Season
    from .calendar import standings

    season = Season.objects.filter(league=league, is_current=True).first()
    if season is None:
        return None
    table = standings(season)
    if not table or not any(r["played"] for r in table):
        return None
    return [r["participant_id"] for r in table]


def save_ranking(league, season, kind, order, source=LeagueRanking.Source.MANUAL):
    ids = [int(x) for x in order]
    valid = set(Participant.objects.filter(league=league).values_list("id", flat=True))
    if not ids or set(ids) - valid or len(set(ids)) != len(ids):
        raise ValueError("Classifica non valida: servono tutte squadre diverse della lega.")
    ranking, _ = LeagueRanking.objects.update_or_create(
        league=league, season=season, kind=kind, defaults={"order": ids, "source": source},
    )
    return ranking


# --- Fasi e voci del tetto -----------------------------------------------------

def _lost_since(participant, since, points_by_role):
    """(punti giocatori persi, numero di non rinnovati) dal ``since``."""
    qs = ContractEvent.objects.filter(participant=participant, kind__in=LOST_KINDS).select_related("player")
    if since is not None:
        qs = qs.filter(created_at__gt=since)
    points = 0.0
    not_renewed = 0
    for ev in qs:
        role = ev.player.role if ev.player else ""
        points += float(points_by_role.get(role, 0))
        if ev.kind in (ContractEvent.Kind.NOT_RENEWED, ContractEvent.Kind.RESCINDED):
            not_renewed += 1
    return points, not_renewed


@transaction.atomic
def open_phase(league, kind, ranking):
    """Apre la fase (estiva o invernale) della stagione corrente e registra,
    per ogni squadra, base o fondi invernali e bonus giocatori persi."""
    r = rules(league)
    season = league.season_number
    if CapPhase.objects.filter(league=league, season=season, kind=kind).exists():
        raise ValueError("Questa fase è già stata aperta.")
    previous = CapPhase.objects.filter(league=league).order_by("-started_at").first()
    since = previous.started_at if previous else None
    phase = CapPhase.objects.create(league=league, season=season, kind=kind)
    rows = []
    for p in Participant.objects.filter(league=league, is_active=True):
        pos = ranking.position_of(p.id) if ranking else None
        table = r["base_by_rank"] if kind == CapPhase.Kind.SUMMER else r["winter_by_rank"]
        amount = by_rank(table, pos or len(table))
        entry_kind = CapEntry.Kind.BASE if kind == CapPhase.Kind.SUMMER else CapEntry.Kind.WINTER
        CapEntry.objects.create(phase=phase, participant=p, kind=entry_kind, amount=amount,
                                note=f"{pos}° in classifica" if pos else "Senza classifica: ultimo scaglione")
        points, not_renewed = _lost_since(p, since, r["lost_points"])
        bonus = lost_bonus(r["lost_bonus"], points)
        if bonus:
            CapEntry.objects.create(phase=phase, participant=p, kind=CapEntry.Kind.LOST, amount=bonus,
                                    note=f"Giocatori persi: {points:g} punti")
        if not_renewed and r["renewal_lost_bonus"]:
            CapEntry.objects.create(phase=phase, participant=p, kind=CapEntry.Kind.RENEWALS,
                                    amount=not_renewed * r["renewal_lost_bonus"],
                                    note=f"{not_renewed} giocatori non rinnovati")
        rows.append((p, pos, amount, bonus, not_renewed))
    return phase


def adjust_cap(league, participant, amount, note=""):
    phase = CapPhase.objects.filter(league=league, season=league.season_number).order_by("-started_at").first()
    if phase is None:
        raise ValueError("Nessuna fase aperta in questa stagione.")
    return CapEntry.objects.create(phase=phase, participant=participant, kind=CapEntry.Kind.MANUAL,
                                   amount=Decimal(amount), note=note[:200])


# --- Stato e controlli ---------------------------------------------------------

def current_session_start(league):
    """Inizio della sessione di mercato in corso (ultima fase aperta), o None."""
    phase = CapPhase.objects.filter(league=league).order_by("-started_at").first()
    return phase.started_at if phase else None


def _season_start(league):
    first = CapPhase.objects.filter(league=league, season=league.season_number).order_by("started_at").first()
    return first.started_at if first else None


def season_spent(participant):
    league = participant.league
    start = _season_start(league)
    if start is None:
        return Decimal("0")
    logs = RosterLog.objects.filter(participant=participant, created_at__gte=start)
    spent = logs.filter(action__in=[RosterLog.Action.ASSIGN, RosterLog.Action.ADMIN_ASSIGN]).aggregate(
        s=Sum("credits_delta"))["s"] or Decimal("0")
    if league.salary_cap_spend == League.CapSpend.NET:
        refunds = logs.filter(action__in=[RosterLog.Action.RELEASE, RosterLog.Action.ADMIN_RELEASE])
        spent -= sum(abs(x) for x in refunds.values_list("credits_delta", flat=True))
    return max(Decimal("0"), spent)


def session_spent(participant, *, auction=None, market_session=None):
    if auction is not None:
        return AuctionCycleResult.objects.filter(
            auction=auction, winner=participant, assigned=True).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    if market_session is not None:
        return MarketBid.objects.filter(
            session=market_session, participant=participant, status=MarketBid.Status.WON,
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
    return Decimal("0")


def cap_status(participant):
    """None se il tetto non si applica; altrimenti cap, spesa e margine."""
    league = participant.league
    if league is None or not league.salary_cap_enabled:
        return None
    entries = CapEntry.objects.filter(participant=participant, phase__league=league,
                                      phase__season=league.season_number)
    if not entries.exists():
        return None
    total = entries.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    max_year = Decimal(rules(league)["max_per_year"] or 0)
    cap = min(total, max_year) if max_year else total
    spent = season_spent(participant)
    return {
        "cap": cap,
        "cap_before_max": total,
        "max_per_year": max_year,
        "spent": spent,
        "left": max(Decimal("0"), cap - spent),
        "entries": list(entries.select_related("phase")),
    }


def purchase_room(participant, *, auction=None, market_session=None):
    """Quanto la squadra può ancora spendere per il tetto (None = senza limite)."""
    status = cap_status(participant)
    if status is None:
        return None
    return status["left"]


def check_purchase(participant, amount, *, auction=None, market_session=None):
    room = purchase_room(participant, auction=auction, market_session=market_session)
    if room is not None and amount > room:
        return f"Tetto salariale: puoi spendere ancora {room:.0f} FM"
    return None


# --- Decreto Salvacalcio ----------------------------------------------------------

@transaction.atomic
def award_decree(league, kind, ranking):
    if DecreeAward.objects.filter(league=league, season=ranking.season, kind=kind).exists():
        raise ValueError("Il Decreto Salvacalcio di questa fase è già stato assegnato.")
    r = rules(league)
    table = r["decree_mid"] if kind == LeagueRanking.Kind.MIDSEASON else r["decree_final"]
    euro = r["prize_euro"] if kind == LeagueRanking.Kind.FINAL else []
    details = []
    for p in Participant.objects.select_for_update().filter(league=league, id__in=ranking.order):
        pos = ranking.position_of(p.id)
        credits = Decimal(by_rank(table, pos))
        credited = add_credits(
            p, credits, f"Decreto Salvacalcio ({LeagueRanking.Kind(kind).label}): {pos}° posto",
            budget_max=r["budget_max"],
        )
        details.append({"participant_id": p.id, "name": p.display_name, "position": pos,
                        "credits": int(credits), "credited": int(credited),
                        "euro": euro[pos - 1] if euro and pos <= len(euro) else 0})
    details.sort(key=lambda d: d["position"])
    return DecreeAward.objects.create(league=league, season=ranking.season, kind=kind, details=details)


# --- Extra salary cap (3.03) ------------------------------------------------------

def extra_cap_locked(league):
    """La conversione si chiude quando parte l'asta della fase in corso."""
    from ..models import Auction

    phase = CapPhase.objects.filter(league=league, season=league.season_number).order_by("-started_at").first()
    if phase is None:
        return True
    return Auction.objects.filter(league=league, starts_at__gte=phase.started_at).exists()


@transaction.atomic
def convert_budget(participant_id, blocks):
    participant = Participant.objects.select_for_update(of=("self",)).select_related("league").get(pk=participant_id)
    league = participant.league
    _sala_guard(league)
    if league is None or not league.salary_cap_enabled:
        return {"ok": False, "message": "Il tetto salariale non è attivo."}
    try:
        blocks = int(blocks)
    except (TypeError, ValueError):
        blocks = 0
    if blocks < 1:
        return {"ok": False, "message": "Indica quanti blocchi convertire."}
    if extra_cap_locked(league):
        return {"ok": False, "message": "Conversione chiusa: l'asta di questa fase è già iniziata."}
    r = rules(league)
    cost = Decimal(r["extra_cap_cost"]) * blocks
    gain = Decimal(r["extra_cap_gain"]) * blocks
    if participant.remaining_credits < cost:
        return {"ok": False, "message": f"Budget insufficiente: servono {cost:.0f} FM."}
    participant.credits -= cost
    participant.save(update_fields=["credits"])
    phase = CapPhase.objects.filter(league=league, season=league.season_number).order_by("-started_at").first()
    CapEntry.objects.create(phase=phase, participant=participant, kind=CapEntry.Kind.EXTRA, amount=gain,
                            note=f"Extra salary cap: {cost:.0f} FM di budget convertiti")
    RosterLog.objects.create(
        participant=participant, participant_name=participant.display_name, player_name="Extra salary cap",
        action=RosterLog.Action.EDIT, credits_delta=cost, by_admin=False,
        note=f"Convertiti {cost:.0f} FM di budget in {gain:.0f} FM di tetto",
    )
    return {"ok": True, "cost": int(cost), "gain": int(gain)}
