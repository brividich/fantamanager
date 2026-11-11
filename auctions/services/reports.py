"""Report di mercato: registro dell'asta live, scambi e mercato della stagione.

Ogni report è un dizionario pronto per il suo template di stampa
(``print_*.html``) e per il suo CSV. Gli orari sono nell'ora locale, al
millesimo di secondo dove possono decidere chi vince.
"""
import csv
import io
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from ..models import (
    Auction, AuctionCycleResult, Bid, Participant, RosterLog, SealedBid, Trade,
)
from ..models.championship import Season
from .common import ERROR_LABELS, Reject
from .market import _bid_time, _gap_label


def _seconds(delta):
    return delta.total_seconds() if delta is not None else None


def _duration_label(seconds):
    """Una durata da leggere: «12 s», «3 min 05 s», «2 h 10 min», «3 g 4 h»."""
    if seconds is None:
        return ""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} g {h} h"
    if h:
        return f"{h} h {m:02d} min"
    return f"{m} min {s:02d} s"


def _csv_bytes(rows):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    for r in rows:
        w.writerow(r)
    return out.getvalue().encode("utf-8-sig")


def _dec(v):
    return f"{v:.3f}".replace(".", ",") if v is not None else ""


# ---------------------------------------------------------------------------
# 1. Registro dell'asta live
# ---------------------------------------------------------------------------

def live_auction_log(auction):
    """Ogni lotto dell'asta con tutti i rilanci in ordine d'arrivo.

    Per ogni rilancio: squadra, importo, orario al millesimo (quando il server
    l'ha ricevuto: è quello che conta) e i secondi che restavano al timer.
    I rilanci rifiutati restano, con il motivo; quelli troppo ravvicinati non
    sono nel registro (il server non li scrive). Le buste dello spareggio, se
    ci sono state, seguono i rilanci del loro lotto.
    """
    results = {r.cycle: r for r in AuctionCycleResult.objects.filter(auction=auction).select_related("player")}
    bids_by_cycle = defaultdict(list)
    for b in (Bid.objects.filter(auction=auction).exclude(rejection_reason=Reject.RATE_LIMITED)
              .select_related("participant").order_by("server_received_at", "id")):
        bids_by_cycle[b.cycle].append(b)
    sealed_by_cycle = defaultdict(list)
    for s in (SealedBid.objects.filter(auction=auction).select_related("participant", "player")
              .order_by("round", "-amount", "updated_at")):
        sealed_by_cycle[s.cycle].append(s)

    lots = []
    for cycle in sorted(set(results) | set(bids_by_cycle) | set(sealed_by_cycle)):
        res = results.get(cycle)
        bids = bids_by_cycle.get(cycle, [])
        if res is None and not bids:
            continue
        accepted = [b for b in bids if b.accepted and not b.cancelled]
        first_at = bids[0].server_received_at if bids else None
        closed_at = res.created_at if res else None
        rows = []
        for i, b in enumerate(bids, start=1):
            late = None
            if not b.accepted and b.rejection_reason == Reject.EXPIRED and closed_at:
                late = _seconds(b.server_received_at - closed_at)
            rows.append({
                "n": i,
                "participant_name": b.participant.display_name,
                "amount": int(b.amount),
                "increment": int(b.increment),
                "at": _bid_time(b.server_received_at),
                "gap_label": _gap_label(_seconds(b.server_received_at - first_at)),
                "remaining": b.remaining_at_bid,
                "remaining_label": (f"{b.remaining_at_bid:.3f} s".replace(".", ",")
                                    if b.remaining_at_bid is not None else ""),
                "late_label": (f"{late:.3f} s dopo la chiusura".replace(".", ",")
                               if late is not None and late > 0 else ""),
                "accepted": b.accepted and not b.cancelled,
                "cancelled": b.cancelled,
                "status_label": ("Annullato" + (f": {b.cancelled_reason}" if b.cancelled_reason else "")
                                 if b.cancelled else
                                 "Valido" if b.accepted else
                                 ERROR_LABELS.get(b.rejection_reason, b.rejection_reason or "Rifiutato")),
                "winning": False,
            })
        # Il rilancio che si è aggiudicato il lotto: l'ultimo valido.
        winning_bid = accepted[-1] if accepted else None
        if winning_bid is not None:
            for r, b in zip(rows, bids):
                if b is winning_bid:
                    r["winning"] = True
        sealed = [{
            "round": s.round,
            "participant_name": s.participant.display_name,
            "amount": int(s.amount),
            "at": _bid_time(s.updated_at),
        } for s in sealed_by_cycle.get(cycle, [])]

        player_name = (res.player_name or (res.player.name if res and res.player else "")) if res else ""
        role = (res.player_role or (res.player.role if res.player else "")) if res else ""
        team = res.player.team if res and res.player else ""
        lots.append({
            "cycle": cycle,
            "player_name": player_name or f"Lotto {cycle}",
            "player_role": role if role in ("P", "D", "C", "A") else "",
            "player_team": team or "",
            "winner_name": (res.winner_name if res else "") or "",
            "amount": int(res.amount) if res else (int(winning_bid.amount) if winning_bid else 0),
            "assigned": bool(res and res.assigned),
            "unsold": bool(res and not res.winner_name),
            "open": res is None,
            "note": res.note if res else "",
            "closed_at": _bid_time(closed_at) if closed_at else None,
            "bids": rows,
            "sealed": sealed,
            "n_valid": len(accepted),
            "n_rejected": sum(1 for r in rows if not r["accepted"]),
            "teams": len({b.participant_id for b in accepted}),
            "duration_label": _duration_label(
                _seconds(accepted[-1].server_received_at - accepted[0].server_received_at)
            ) if len(accepted) > 1 else "",
            "margin": winning_bid.remaining_at_bid if winning_bid else None,
            "margin_label": (f"{winning_bid.remaining_at_bid:.3f} s".replace(".", ",")
                             if winning_bid and winning_bid.remaining_at_bid is not None else ""),
        })

    sold = [l for l in lots if l["winner_name"]]
    close_calls = [l for l in sold if l["margin"] is not None and l["margin"] <= 3]
    return {
        "auction": auction,
        "league": auction.league,
        "lots": lots,
        "sold": len(sold),
        "unsold": sum(1 for l in lots if l["unsold"]),
        "total_spent": sum(l["amount"] for l in sold),
        "total_bids": sum(l["n_valid"] for l in lots),
        "total_rejected": sum(l["n_rejected"] for l in lots),
        "close_calls": len(close_calls),
        "generated_at": timezone.localtime(),
    }


def live_auction_csv(auction):
    data = live_auction_log(auction)
    rows = [
        ["REGISTRO DELL'ASTA", auction.title],
        ["Lega", auction.league.name if auction.league else ""],
        ["Orari", "ora locale, quando il server ha ricevuto il rilancio"],
        [],
        ["Lotto", "Calciatore", "Ruolo", "Squadra Serie A", "N.", "Societa", "Importo FM", "Rilancio FM",
         "Data", "Ora", "Millesimi", "Secondi rimasti al timer", "Esito del rilancio",
         "Aggiudicatario del lotto", "Prezzo finale FM"],
    ]
    for l in data["lots"]:
        for r in l["bids"]:
            rows.append([
                l["cycle"], l["player_name"], l["player_role"], l["player_team"], r["n"],
                r["participant_name"], r["amount"], r["increment"],
                r["at"]["date"], r["at"]["time"], r["at"]["ms"], _dec(r["remaining"]),
                ("VINCENTE" if r["winning"] else r["status_label"]) + (f" ({r['late_label']})" if r["late_label"] else ""),
                l["winner_name"], l["amount"],
            ])
        for s in l["sealed"]:
            rows.append([
                l["cycle"], l["player_name"], l["player_role"], l["player_team"], f"busta giro {s['round']}",
                s["participant_name"], s["amount"], "", s["at"]["date"], s["at"]["time"], s["at"]["ms"], "",
                "Busta di spareggio", l["winner_name"], l["amount"],
            ])
        if not l["bids"] and not l["sealed"]:
            rows.append([l["cycle"], l["player_name"], l["player_role"], l["player_team"], "", "", "", "",
                         "", "", "", "", "Nessun rilancio", l["winner_name"], l["amount"]])
    return _csv_bytes(rows)


def league_auctions_with_log(league, limit=12):
    """Le aste della lega che hanno qualcosa da mettere nel registro."""
    return list(
        Auction.objects.filter(league=league)
        .exclude(status=Auction.Status.DRAFT)
        .filter(cycle_results__isnull=False).distinct()
        .order_by("-created_at")[:limit]
    )


# ---------------------------------------------------------------------------
# 2. Report degli scambi
# ---------------------------------------------------------------------------

def _players(qs):
    return [{"name": p.name, "role": p.role if p.role in ("P", "D", "C", "A") else ""} for p in qs]


def trades_report(league):
    """Tutti gli scambi della lega, dal più recente, con i tempi di ogni passo:
    proposta, risposta, ratifica o esecuzione."""
    trades = list(
        Trade.objects.filter(league=league)
        .select_related("proposer", "receiver")
        .prefetch_related("proposer_players", "receiver_players")
        .order_by("-created_at", "-id")
    )
    rows = []
    by_team = {}
    for p in Participant.objects.filter(league=league).order_by("display_name"):
        by_team[p.id] = {"name": p.display_name, "proposed": 0, "received": 0, "completed": 0,
                         "credits_in": 0, "credits_out": 0, "players_in": 0, "players_out": 0}
    status_counts = defaultdict(int)
    for t in trades:
        status_counts[t.status] += 1
        respond_s = _seconds(t.responded_at - t.created_at) if t.responded_at else None
        # Con la ratifica l'admin decide dopo la risposta; senza, si esegue
        # alla risposta e i due orari coincidono.
        ratify_s = (_seconds(t.completed_at - t.responded_at)
                    if t.completed_at and t.responded_at else None)
        total_s = _seconds(t.completed_at - t.created_at) if t.completed_at else None
        gives = _players(t.proposer_players.all())
        gets = _players(t.receiver_players.all())
        rows.append({
            "id": t.id,
            "kind": t.get_kind_display(),
            "is_loan": t.kind == Trade.Kind.LOAN,
            "loan_sessions": t.loan_sessions,
            "proposer": t.proposer.display_name,
            "receiver": t.receiver.display_name,
            "proposer_players": gives,
            "receiver_players": gets,
            "proposer_credits": int(t.proposer_credits),
            "receiver_credits": int(t.receiver_credits),
            "message": t.message,
            "status": t.status,
            "status_label": t.get_status_display(),
            "status_note": t.status_note,
            "created": _bid_time(t.created_at),
            "responded": _bid_time(t.responded_at) if t.responded_at else None,
            "completed": _bid_time(t.completed_at) if t.completed_at else None,
            "respond_label": _duration_label(respond_s),
            "ratify_label": _duration_label(ratify_s) if ratify_s and ratify_s >= 1 else "",
            "total_label": _duration_label(total_s),
        })
        for pid in (t.proposer_id, t.receiver_id):
            by_team.setdefault(pid, {"name": (t.proposer if pid == t.proposer_id else t.receiver).display_name,
                                     "proposed": 0, "received": 0, "completed": 0, "credits_in": 0,
                                     "credits_out": 0, "players_in": 0, "players_out": 0})
        by_team[t.proposer_id]["proposed"] += 1
        by_team[t.receiver_id]["received"] += 1
        if t.status == Trade.Status.COMPLETED:
            a, b = by_team[t.proposer_id], by_team[t.receiver_id]
            a["completed"] += 1
            b["completed"] += 1
            a["players_out"] += len(gives)
            a["players_in"] += len(gets)
            b["players_out"] += len(gets)
            b["players_in"] += len(gives)
            a["credits_out"] += int(t.proposer_credits)
            a["credits_in"] += int(t.receiver_credits)
            b["credits_out"] += int(t.receiver_credits)
            b["credits_in"] += int(t.proposer_credits)

    responded = [t for t in trades if t.responded_at]
    avg_respond = (sum(_seconds(t.responded_at - t.created_at) for t in responded) / len(responded)
                   if responded else None)
    return {
        "league": league,
        "trades": rows,
        "total": len(rows),
        "completed": status_counts.get(Trade.Status.COMPLETED, 0),
        "open": status_counts.get(Trade.Status.PENDING, 0) + status_counts.get(Trade.Status.ACCEPTED, 0),
        "refused": sum(status_counts.get(s, 0) for s in (Trade.Status.REJECTED, Trade.Status.VETOED,
                                                         Trade.Status.CANCELLED, Trade.Status.FAILED)),
        "avg_respond_label": _duration_label(avg_respond),
        "teams": [t for t in by_team.values() if t["proposed"] or t["received"]],
        "generated_at": timezone.localtime(),
    }


def trades_csv(league):
    data = trades_report(league)
    names = lambda ps: ", ".join(f"{p['name']} ({p['role']})" if p["role"] else p["name"] for p in ps)
    rows = [
        ["REPORT DEGLI SCAMBI", league.name],
        ["Orari", "ora locale"],
        [],
        ["N.", "Tipo", "Proponente", "Destinatario", "Il proponente cede", "Crediti dal proponente",
         "Il destinatario cede", "Crediti dal destinatario", "Stato", "Nota", "Proposto il", "Risposta il",
         "Eseguito il", "Tempo di risposta", "Attesa della ratifica", "Messaggio"],
    ]
    for t in data["trades"]:
        rows.append([
            t["id"], t["kind"] + (f" ({t['loan_sessions']} sess.)" if t["is_loan"] else ""),
            t["proposer"], t["receiver"], names(t["proposer_players"]), t["proposer_credits"],
            names(t["receiver_players"]), t["receiver_credits"], t["status_label"], t["status_note"],
            t["created"]["full"], t["responded"]["full"] if t["responded"] else "",
            t["completed"]["full"] if t["completed"] else "",
            t["respond_label"], t["ratify_label"], t["message"],
        ])
    return _csv_bytes(rows)


# ---------------------------------------------------------------------------
# 3. Mercato della stagione, squadra per squadra
# ---------------------------------------------------------------------------

_KIND = {
    RosterLog.Action.ASSIGN: "acquisto", RosterLog.Action.ADMIN_ASSIGN: "acquisto",
    RosterLog.Action.RELEASE: "svincolo", RosterLog.Action.ADMIN_RELEASE: "svincolo",
    RosterLog.Action.TRADE: "scambio", RosterLog.Action.EDIT: "modifica",
}


def season_start(league):
    """Da quando conta la stagione: l'apertura della stagione corrente."""
    season = Season.objects.filter(league=league, is_current=True).order_by("-created_at").first()
    return season.created_at if season else None, season


def season_market_report(league, since=None):
    """Ogni movimento di rosa della stagione, squadra per squadra, con la spesa
    progressiva: acquisti, svincoli con i rimborsi, scambi e correzioni."""
    start, season = season_start(league) if since is None else (since, None)
    log = RosterLog.objects.filter(participant__league=league)
    if start is not None:
        log = log.filter(created_at__gte=start)
    log = list(log.order_by("created_at", "id"))

    teams = []
    by_id = {}
    for p in Participant.objects.filter(league=league).order_by("display_name"):
        t = {"id": p.id, "name": p.display_name, "moves": [], "bought": 0, "released": 0, "traded": 0,
             "spent": 0, "refund": 0, "net": 0, "credits": int(p.credits),
             "remaining": int(p.remaining_credits), "roster": p.roster.count()}
        teams.append(t)
        by_id[p.id] = t
    for m in log:
        t = by_id.get(m.participant_id)
        if t is None:
            continue
        kind = _KIND.get(m.action, "altro")
        delta = m.credits_delta or Decimal("0")
        spent = int(delta) if delta > 0 else 0
        refund = int(-delta) if delta < 0 else 0
        t["spent"] += spent
        t["refund"] += refund
        t["net"] += spent - refund
        t["bought"] += kind == "acquisto"
        t["released"] += kind == "svincolo"
        t["traded"] += kind == "scambio"
        t["moves"].append({
            "at": _bid_time(m.created_at),
            "kind": kind,
            "label": m.get_action_display(),
            "player_name": m.player_name,
            "player_role": m.player_role if m.player_role in ("P", "D", "C", "A") else "",
            "spent": spent,
            "refund": refund,
            "net_after": t["net"],
            "by_admin": m.by_admin,
            "note": m.note,
        })
    return {
        "league": league,
        "season": season,
        "since": timezone.localtime(start) if start else None,
        "teams": teams,
        "active_teams": [t for t in teams if t["moves"]],
        "total_moves": len(log),
        "total_spent": sum(t["spent"] for t in teams),
        "total_refund": sum(t["refund"] for t in teams),
        "generated_at": timezone.localtime(),
    }


def season_market_csv(league):
    data = season_market_report(league)
    rows = [
        ["REPORT DI MERCATO DELLA STAGIONE", league.name],
        ["Dal", data["since"].strftime("%d/%m/%Y") if data["since"] else "inizio"],
        [],
        ["Societa", "Data", "Ora", "Movimento", "Calciatore", "Ruolo", "Spesa FM", "Rimborso FM",
         "Spesa netta progressiva FM", "Dalla regia", "Note"],
    ]
    for t in data["teams"]:
        for m in t["moves"]:
            rows.append([
                t["name"], m["at"]["date"], m["at"]["time"], m["label"], m["player_name"], m["player_role"],
                m["spent"], m["refund"], m["net_after"], "SI" if m["by_admin"] else "", m["note"],
            ])
    rows.append([])
    rows.append(["RIEPILOGO", "", "", "Acquisti", "Svincoli", "Scambi", "Spesa FM", "Rimborsi FM",
                 "Spesa netta FM", "Crediti residui FM", "Giocatori in rosa"])
    for t in data["teams"]:
        rows.append([t["name"], "", "", t["bought"], t["released"], t["traded"], t["spent"], t["refund"],
                     t["net"], t["remaining"], t["roster"]])
    return _csv_bytes(rows)
