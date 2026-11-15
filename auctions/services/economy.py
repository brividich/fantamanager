"""Il tabellone del budget: per ogni squadra della lega, nella stagione in corso,
budget, tetto salariale e i movimenti che li hanno fatti, come nel foglio Excel
che le leghe tenevano a mano (budget a disposizione, tetto iniziale, tetto
spendibile, totale spesa; spese e ricavi del mercato estivo e invernale,
bonus di metà stagione, Decreto, premi, scambi).

Non tiene conti suoi: il budget a disposizione è quello vero della squadra
(``Participant.remaining_credits``) e i movimenti si leggono dal registro
(``RosterLog``) e dagli scambi conclusi (``Trade``). Il budget di inizio
stagione è quindi «a disposizione meno i movimenti»: torna sempre.

Le finestre: la stagione parte con il campionato in corso (``Season``) o con
la prima fase del tetto; l'inverno con la fase invernale del tetto o con la
classifica di metà stagione.
"""
from decimal import Decimal

from ..models import CapEntry, CapPhase, League, LeagueRanking, Participant, RosterLog, Trade

ZERO = Decimal("0")

# Le righe che annullano un acquisto (non sono un incasso: tolgono la spesa).
_UNDO_PREFIXES = ("Aggiudicazione annullata", "Annullamento spoglio", "Riassegnato a")

COLUMNS = (
    ("summer_spent", "Spese mercato estivo", "aste e buste fino a metà stagione"),
    ("summer_income", "Ricavi mercato estivo", "cessioni, svincoli ed estero"),
    ("winter_spent", "Spese mercato invernale", "aste e buste da metà stagione"),
    ("winter_income", "Ricavi mercato invernale", "cessioni, svincoli ed estero"),
    ("trades", "Scambi", "conguagli in crediti (+ ricevuti, − dati)"),
    ("decree", "Decreto Salvacalcio", "crediti per posizione"),
    ("prizes", "Premi", "coppe e competizioni"),
    ("extra", "Extra salary cap", "budget convertito in tetto"),
    ("manual", "Crediti a mano", "rettifiche dell'admin"),
)


def season_window(league):
    """(inizio della stagione, inizio dell'inverno): date o None."""
    from ..models import Season

    starts = []
    current = Season.objects.filter(league=league, is_current=True).order_by("-created_at").first()
    if current is not None:
        starts.append(current.created_at)
    first_phase = CapPhase.objects.filter(league=league, season=league.season_number).order_by("started_at").first()
    if first_phase is not None:
        starts.append(first_phase.started_at)
    start = min(starts) if starts else None

    winter = CapPhase.objects.filter(league=league, season=league.season_number,
                                     kind=CapPhase.Kind.WINTER).first()
    if winter is not None:
        winter_at = winter.started_at
    else:
        mid = LeagueRanking.objects.filter(league=league, season=league.season_number,
                                           kind=LeagueRanking.Kind.MIDSEASON).first()
        winter_at = mid.created_at if mid else None
    return start, winter_at


def _money_kind(log):
    """La voce di una riga di soli crediti (anche le righe scritte prima del tipo)."""
    if log.money:
        return log.money
    if log.player_name.startswith("Decreto Salvacalcio"):
        return RosterLog.Money.DECREE
    if log.player_name == "Extra salary cap":
        return RosterLog.Money.EXTRA
    return ""


def classify(log, winter_at):
    """(colonna, importo con segno sul budget, testo) di una riga del registro,
    o None se non muove crediti. Importo > 0: la squadra incassa."""
    delta = log.credits_delta or ZERO
    season_part = "winter" if winter_at is not None and log.created_at >= winter_at else "summer"
    if log.action in (RosterLog.Action.ASSIGN, RosterLog.Action.ADMIN_ASSIGN):
        if log.note.startswith("Annullamento spoglio"):     # il giocatore lasciato torna: rende la cessione
            return f"{season_part}_income", -abs(delta), f"{log.player_name}: torna in rosa"
        return f"{season_part}_spent", -delta, f"{log.player_name}: acquisto"
    if log.action in (RosterLog.Action.RELEASE, RosterLog.Action.ADMIN_RELEASE):
        if log.note.startswith(_UNDO_PREFIXES):
            return f"{season_part}_spent", abs(delta), f"{log.player_name}: acquisto annullato"
        return f"{season_part}_income", abs(delta), f"{log.player_name}: {'svincolo' if log.action == RosterLog.Action.RELEASE else 'svincolo (admin)'}"
    if log.action == RosterLog.Action.EDIT:
        kind = _money_kind(log)
        column = {RosterLog.Money.DECREE: "decree", RosterLog.Money.PRIZE: "prizes",
                  RosterLog.Money.EXTRA: "extra", RosterLog.Money.MANUAL: "manual"}.get(kind)
        if column:
            return column, -delta, log.player_name
        return f"{season_part}_spent", -delta, f"{log.player_name}: {log.note or 'correzione prezzo'}"
    return None


def team_ledger(participant, start, winter_at):
    """Colonne e movimenti di una squadra dalla data ``start``."""
    totals = {key: ZERO for key, _label, _hint in COLUMNS}
    moves = []
    logs = RosterLog.objects.filter(participant=participant)
    if start is not None:
        logs = logs.filter(created_at__gte=start)
    for log in logs.order_by("created_at", "id"):
        found = classify(log, winter_at)
        if found is None or not found[1]:
            continue
        column, amount, text = found
        totals[column] += amount
        moves.append({"at": log.created_at, "column": column, "amount": amount, "text": text})
    trades = Trade.objects.filter(status=Trade.Status.COMPLETED).filter(
        proposer=participant) | Trade.objects.filter(status=Trade.Status.COMPLETED, receiver=participant)
    if start is not None:
        trades = trades.filter(completed_at__gte=start)
    for trade in trades.select_related("proposer", "receiver").order_by("completed_at"):
        net = trade.proposer_credits - trade.receiver_credits      # > 0: paga chi propone
        amount = -net if trade.proposer_id == participant.id else net
        if not amount:
            continue
        other = trade.receiver if trade.proposer_id == participant.id else trade.proposer
        totals["trades"] += amount
        moves.append({"at": trade.completed_at, "column": "trades", "amount": amount,
                      "text": f"Scambio #{trade.id} con {other.display_name}"})
    moves.sort(key=lambda m: m["at"])
    return totals, moves


def _cap(participant, league):
    """Il tetto come nel foglio: iniziale (fase estiva), bonus di metà stagione
    (fase invernale), spesa contata e spendibile. None se il tetto è spento."""
    from . import salary

    status = salary.cap_status(participant)
    if status is None:
        return None
    by_phase = {CapPhase.Kind.SUMMER: ZERO, CapPhase.Kind.WINTER: ZERO}
    for entry in status["entries"]:
        by_phase[entry.phase.kind] = by_phase.get(entry.phase.kind, ZERO) + entry.amount
    return {
        "initial": by_phase[CapPhase.Kind.SUMMER],
        "winter_bonus": by_phase[CapPhase.Kind.WINTER],
        "cap": status["cap"],
        "spent": status["spent"],
        "left": status["left"],
        "capped": status["cap_before_max"] > status["cap"],
        "entries": [{"kind": e.get_kind_display(), "amount": e.amount, "note": e.note,
                     "winter": e.phase.kind == CapPhase.Kind.WINTER}
                    for e in status["entries"] if e.kind != CapEntry.Kind.MANUAL or e.amount],
    }


def tabellone(league):
    """Tutto il tabellone della lega per la stagione in corso."""
    from . import salary

    start, winter_at = season_window(league)
    rules = salary.rules(league)
    rows = []
    for team in Participant.objects.filter(league=league, is_active=True).order_by("display_name"):
        totals, moves = team_ledger(team, start, winter_at)
        budget = team.remaining_credits
        moved = sum(totals.values(), ZERO)
        spent = -(totals["summer_spent"] + totals["winter_spent"])
        rows.append({
            "team": team,
            "budget": budget,
            "budget_start": budget - moved,
            "totals": totals,
            "spent": spent,
            "income": totals["summer_income"] + totals["winter_income"],
            "lines": [(label, hint, totals[key]) for key, label, hint in COLUMNS if totals[key]],
            "moves": moves,
            "cap": _cap(team, league),
        })
    return {
        "rows": rows,
        "columns": COLUMNS,
        "start": start,
        "winter_at": winter_at,
        "season_number": league.season_number,
        "cap_on": league.salary_cap_enabled,
        "budget_max": rules["budget_max"],
        "max_per_year": rules["max_per_year"],
        "decree_mid_on": league.decree_mid_enabled,
        "decree_final_on": league.decree_final_enabled,
        "spend_mode": league.get_salary_cap_spend_display() if league.salary_cap_enabled else "",
        "net": league.salary_cap_spend == League.CapSpend.NET,
    }
