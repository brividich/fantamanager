"""Sealed-bid market session (mercato di riparazione) business logic."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import logging

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..models import (
    Auction,
    MarketBid,
    MarketSession,
    Participant,
    Player,
    RosterLog,
)

logger = logging.getLogger("auctions.market")


def _calc_release_refund(session, player):
    """Calculate credits returned when a player is released in a market session."""
    if player is None:
        return Decimal("0")
    mode = session.release_refund_mode
    if mode == Auction.RefundMode.NONE:
        return Decimal("0")
    if mode == Auction.RefundMode.CURRENT:
        p = player.price_for(session.league)
        return p if p is not None else Decimal("1")
    # Default: PURCHASE (cost paid)
    return player.cost if player.cost is not None else Decimal("1")


@transaction.atomic
def place_market_bid(
    session_id,
    participant_id,
    player_id,
    amount,
    priority=1,
    release_player_id=None,
):
    """Submit or update a sealed market bid for a free agent."""
    try:
        session = MarketSession.objects.select_for_update().get(pk=session_id)
    except MarketSession.DoesNotExist:
        return {"ok": False, "error": "session_not_found", "message": "Sessione non trovata."}

    if not session.is_open:
        return {"ok": False, "error": "session_closed", "message": "La sessione di mercato è chiusa."}

    try:
        participant = Participant.objects.select_for_update().get(pk=participant_id)
    except Participant.DoesNotExist:
        return {"ok": False, "error": "participant_not_found", "message": "Partecipante non trovato."}

    if not participant.is_active or participant.league_id != session.league_id:
        return {"ok": False, "error": "invalid_participant", "message": "Partecipante non valido per questa lega."}

    try:
        player = Player.objects.get(pk=player_id)
    except Player.DoesNotExist:
        return {"ok": False, "error": "player_not_found", "message": "Calciatore non trovato."}

    if player.owner_id is not None or (player.league_id and player.league_id != session.league_id):
        return {"ok": False, "error": "player_unavailable", "message": "Calciatore non disponibile sul mercato svincolati."}

    try:
        val = Decimal(str(amount).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError):
        return {"ok": False, "error": "invalid_amount", "message": "Importo offerta non valido."}

    val = val.quantize(Decimal("1"))
    if val < Decimal("1"):
        return {"ok": False, "error": "amount_too_low", "message": "L'offerta minima è 1 FM."}

    release_player = None
    refund = Decimal("0")
    if release_player_id:
        if not session.allow_conditional_release:
            return {"ok": False, "error": "release_not_allowed", "message": "Svincoli condizionati non ammessi in questa sessione."}
        try:
            release_player = Player.objects.get(pk=release_player_id)
        except Player.DoesNotExist:
            return {"ok": False, "error": "release_player_not_found", "message": "Calciatore da svincolare non trovato."}

        if release_player.owner_id != participant.id:
            return {"ok": False, "error": "release_player_not_owned", "message": "Il calciatore da svincolare non appartiene alla tua rosa."}

        refund = _calc_release_refund(session, release_player)

    max_spendable = participant.remaining_credits + refund
    if val > max_spendable:
        return {
            "ok": False,
            "error": "insufficient_credits",
            "message": f"Crediti insufficienti. Massimo spendibile: {max_spendable:.0f} FM.",
        }

    try:
        prio = max(1, int(priority or 1))
    except (ValueError, TypeError):
        prio = 1

    bid, _ = MarketBid.objects.update_or_create(
        session=session,
        participant=participant,
        player=player,
        defaults={
            "amount": val,
            "priority": prio,
            "release_player": release_player,
            "status": MarketBid.Status.PENDING,
            "note": "",
        },
    )

    logger.info(
        f"Market bid placed: session={session_id}, participant='{participant.display_name}', "
        f"player='{player.name}' ({player.role}), amount={val:.0f} FM, priority={prio}, "
        f"release={release_player.name if release_player else None}"
    )

    return {
        "ok": True,
        "bid_id": bid.id,
        "bid": {
            "id": bid.id,
            "player_id": player.id,
            "player_name": player.name,
            "player_role": player.role,
            "player_team": player.team,
            "amount": str(bid.amount),
            "priority": bid.priority,
            "release_player_id": release_player.id if release_player else None,
            "release_player_name": release_player.name if release_player else None,
            "refund": str(refund),
        },
    }


@transaction.atomic
def delete_market_bid(session_id, participant_id, bid_id):
    """Cancel and delete a market bid before session closes."""
    try:
        session = MarketSession.objects.get(pk=session_id)
    except MarketSession.DoesNotExist:
        return {"ok": False, "error": "session_not_found"}

    if not session.is_open:
        return {"ok": False, "error": "session_closed", "message": "Mercato chiuso: non è più possibile ritirare offerte."}

    deleted, _ = MarketBid.objects.filter(
        pk=bid_id, session_id=session_id, participant_id=participant_id
    ).delete()

    if deleted:
        logger.info(f"Market bid deleted: session={session_id}, participant={participant_id}, bid={bid_id}")

    return {"ok": bool(deleted)}


def get_participant_market_bids(session_id, participant_id):
    """Retrieve all submitted bids by a participant in a session."""
    session = MarketSession.objects.filter(pk=session_id).first()
    if session is None:
        return []

    bids = (
        MarketBid.objects.filter(session_id=session_id, participant_id=participant_id)
        .select_related("player", "release_player")
        .order_by("priority", "-amount", "created_at")
    )
    result = []
    for b in bids:
        refund = _calc_release_refund(session, b.release_player) if b.release_player else Decimal("0")
        result.append({
            "id": b.id,
            "player_id": b.player_id,
            "player_name": b.player.name if b.player else "",
            "player_role": b.player.role if b.player else "",
            "player_team": b.player.team if b.player else "",
            "amount": int(b.amount),
            "priority": b.priority,
            "release_player_id": b.release_player_id,
            "release_player_name": b.release_player.name if b.release_player else "",
            "refund": int(refund),
            "status": b.status,
            "status_label": b.get_status_display(),
            "note": b.note,
        })
    return result


# --- Spoglio -----------------------------------------------------------------
#
# Lo spoglio è diviso in due: ``plan_market_resolution`` calcola l'esito senza
# scrivere nulla (serve anche all'anteprima dell'admin), ``resolve_market_session``
# lo applica.
#
# Regole:
# * Ogni calciatore va all'offerta più alta *valida*; a parità d'importo vince
#   la priorità più bassa (1 prima di 2), a parità anche di priorità è pari merito.
# * La priorità è l'ordine di preferenza del singolo manager: una sua busta
#   entra in gioco solo quando quelle con priorità più alta (numero più basso)
#   sono state decise. Così budget e slot vengono spesi prima sugli obiettivi
#   principali, qualunque sia l'ordine dei calciatori nel database.
# * Un calciatore si decide quando tutte le sue offerte migliori sono "in gioco".
#   Se nessun calciatore è decidibile (preferenze incrociate) si decide quello
#   con l'offerta più alta in assoluto, così lo spoglio termina sempre.
# * Al momento dell'aggiudicazione si ricontrollano: calciatore ancora libero,
#   partecipante attivo, tetto acquisti per ruolo, taglio condizionato ancora
#   possibile, crediti e slot della rosa.


class _Plan:
    """Stato in memoria dello spoglio: nessuna scrittura sul database."""

    def __init__(self, session):
        self.session = session
        self.league = session.league
        self.bids = list(
            session.bids.filter(status=MarketBid.Status.PENDING)
            .select_related("participant", "player", "release_player")
        )
        self.participants = {
            p.id: p for p in Participant.objects.filter(league=self.league)
        }
        for b in self.bids:
            self.participants.setdefault(b.participant_id, b.participant)
        self.remaining = {pid: p.remaining_credits for pid, p in self.participants.items()}

        player_ids = {b.player_id for b in self.bids}
        player_ids |= {b.release_player_id for b in self.bids if b.release_player_id}
        self.players = {p.id: p for p in Player.objects.filter(pk__in=player_ids)}
        self.owner = {pid: p.owner_id for pid, p in self.players.items()}

        self.owned = defaultdict(lambda: defaultdict(int))
        for owner_id, role in Player.objects.filter(
            owner_id__in=list(self.participants)
        ).values_list("owner_id", "role"):
            self.owned[owner_id][role] += 1
        self.role_acquisitions = defaultdict(lambda: defaultdict(int))

        # bid.id -> (status, note)
        self.outcome = {}
        self.won = []
        self.tied = []
        # bid.id -> (release_player, refund) for the awarded bids
        self.awards = {}

    # -- helpers --------------------------------------------------------------

    def _pending(self):
        return [b for b in self.bids if b.id not in self.outcome]

    def _decide(self, bid, status, note):
        self.outcome[bid.id] = (status, note)

    @staticmethod
    def _rank(b):
        return (-b.amount, b.priority, b.created_at, b.id)

    def _check(self, bid):
        """Can ``bid`` be awarded now? Returns (ok, note, release_player, refund)."""
        participant = self.participants.get(bid.participant_id)
        if participant is None or not participant.is_active or participant.league_id != self.league.id:
            return False, "Partecipante non valido o non attivo", None, Decimal("0")

        role = bid.player.role
        max_role = self.session.max_for_role(role)
        if max_role > 0 and self.role_acquisitions[participant.id][role] >= max_role:
            return False, f"Raggiunto limite acquisti per ruolo {role} ({max_role})", None, Decimal("0")

        release = None
        refund = Decimal("0")
        release_note = ""
        if bid.release_player_id:
            if self.owner.get(bid.release_player_id) == participant.id:
                release = self.players[bid.release_player_id]
                refund = _calc_release_refund(self.session, release)
            else:
                release_note = " (taglio condizionato non più possibile)"

        available = self.remaining[participant.id] + refund
        if available < bid.amount:
            return (
                False,
                f"Crediti insufficienti al momento dello spoglio (disponibili: {available:.0f} FM)" + release_note,
                None,
                Decimal("0"),
            )

        cap = self.league.slots_for(role)
        if cap > 0:
            bucket = self.league.slot_roles(role)
            count = sum(self.owned[participant.id][r] for r in bucket)
            if release is not None and release.role in bucket:
                count -= 1
            if count + 1 > cap:
                return False, f"Rosa piena per il ruolo {role} ({cap} slot)" + release_note, None, Decimal("0")

        return True, release_note.strip(), release, refund

    def _award(self, bid, release, refund, note):
        pid = bid.participant_id
        player = bid.player
        self.owner[player.id] = pid
        self.owned[pid][player.role] += 1
        if release is not None:
            self.owner[release.id] = None
            self.owned[pid][release.role] -= 1
        self.remaining[pid] -= bid.amount - refund
        self.role_acquisitions[pid][player.role] += 1
        self.awards[bid.id] = (release, refund)
        self._decide(bid, MarketBid.Status.WON, ("Aggiudicato con successo " + note).strip())
        self.won.append({
            "bid_id": bid.id,
            "player_id": player.id,
            "player_name": player.name,
            "player_role": player.role,
            "player_team": player.team,
            "winner_id": pid,
            "winner_name": self.participants[pid].display_name,
            "amount": int(bid.amount),
            "released_player_id": release.id if release else None,
            "released_player": release.name if release else None,
            "released_player_cost": str(release.cost) if release else None,
            "refund": int(refund),
        })

    def _resolve_player(self, player_id, bids):
        player = bids[0].player
        if self.owner.get(player_id) is not None:
            for b in bids:
                self._decide(b, MarketBid.Status.LOST, "Calciatore non più disponibile")
            return

        bids = sorted(bids, key=self._rank)
        winner = None
        for b in bids:
            if winner is not None:
                break
            ok, note, release, refund = self._check(b)
            if not ok:
                self._decide(b, MarketBid.Status.LOST, note)
                continue
            winner = (b, note, release, refund)

        if winner is None:
            return

        w, note, release, refund = winner
        tied = []
        for b in bids:
            if b.id in self.outcome or b is w:
                continue
            if b.amount == w.amount and b.priority == w.priority:
                ok, bnote, _, _ = self._check(b)
                if ok:
                    tied.append(b)
                else:
                    self._decide(b, MarketBid.Status.LOST, bnote)

        if tied:
            group = [w] + tied
            names = [b.participant.display_name for b in group]
            for b in group:
                others = ", ".join(n for n in names if n != b.participant.display_name)
                self._decide(b, MarketBid.Status.TIED, f"Pari merito ({w.amount:.0f} FM) con: {others}")
            self.tied.append({
                "player_id": player.id,
                "player_name": player.name,
                "player_role": player.role,
                "player_team": player.team,
                "amount": int(w.amount),
                "contenders": names,
                "contender_ids": [b.participant_id for b in group],
            })
        else:
            self._award(w, release, refund, note)

        for b in bids:
            if b.id not in self.outcome:
                self._decide(b, MarketBid.Status.LOST, "Offerta superata")

    # -- main loop -------------------------------------------------------------

    def run(self):
        while True:
            pending = self._pending()
            if not pending:
                break

            active_prio = {}
            for b in pending:
                cur = active_prio.get(b.participant_id)
                if cur is None or b.priority < cur:
                    active_prio[b.participant_id] = b.priority

            by_player = defaultdict(list)
            for b in pending:
                by_player[b.player_id].append(b)

            candidates = []
            for player_id, bids in by_player.items():
                ranked = sorted(bids, key=self._rank)
                top = ranked[0]
                top_group = [b for b in ranked if b.amount == top.amount]
                decidable = all(b.priority == active_prio[b.participant_id] for b in top_group)
                candidates.append((not decidable, self._rank(top), player_id, bids))

            # Decidable players first, then by best top offer; one at a time,
            # because each award changes budgets, slots and active priorities.
            candidates.sort(key=lambda c: (c[0], c[1]))
            _, _, player_id, bids = candidates[0]
            self._resolve_player(player_id, bids)
        return self


def plan_market_resolution(session_id):
    """Simula lo spoglio senza scrivere nulla. Ritorna il riepilogo previsto."""
    session = MarketSession.objects.select_related("league").get(pk=session_id)
    plan = _Plan(session).run()
    return _summary(plan, preview=True)


def _summary(plan, preview=False):
    lost = []
    for b in plan.bids:
        status, note = plan.outcome[b.id]
        if status == MarketBid.Status.LOST:
            lost.append({
                "bid_id": b.id,
                "player_name": b.player.name,
                "player_role": b.player.role,
                "participant_name": b.participant.display_name,
                "amount": int(b.amount),
                "note": note,
            })
    summary = {
        "won": plan.won,
        "tied": plan.tied,
        "lost": lost,
        "total_acquisitions": len(plan.won),
        "total_ties": len(plan.tied),
    }
    if preview:
        summary["preview"] = True
    else:
        summary["resolved_at"] = timezone.now().isoformat()
    return summary


@transaction.atomic
def resolve_market_session(session_id):
    """Scrutinize all sealed bids and assign players, releases, and refunds."""
    session = (
        MarketSession.objects.select_for_update()
        .select_related("league")
        .get(pk=session_id)
    )

    if session.status == MarketSession.Status.RESOLVED:
        return session.results_summary

    plan = _Plan(session).run()
    logger.info(
        f"Resolving market session {session_id} ('{session.title}'). "
        f"Total pending bids: {len(plan.bids)}"
    )

    bids_by_id = {b.id: b for b in plan.bids}
    for bid_id, (status, note) in plan.outcome.items():
        b = bids_by_id[bid_id]
        b.status = status
        b.note = note[:200]
        b.save(update_fields=["status", "note", "updated_at"])

    for w in plan.won:
        bid = bids_by_id[w["bid_id"]]
        participant = plan.participants[w["winner_id"]]
        player = plan.players[w["player_id"]]
        amount = bid.amount
        rel, refund = plan.awards[bid.id]

        player.owner = participant
        player.cost = amount
        player.save(update_fields=["owner", "cost"])

        if rel is not None:
            rel.owner = None
            rel.cost = Decimal("0")
            rel.save(update_fields=["owner", "cost"])
            RosterLog.objects.create(
                participant=participant,
                participant_name=participant.display_name,
                player_name=rel.name,
                player_role=rel.role,
                action=RosterLog.Action.RELEASE,
                credits_delta=-refund,
                by_admin=True,
                note=f"Taglio mercato: {session.title}",
            )

        Participant.objects.filter(pk=participant.id).update(
            spent_credits=F("spent_credits") + (amount - refund)
        )
        RosterLog.objects.create(
            participant=participant,
            participant_name=participant.display_name,
            player_name=player.name,
            player_role=player.role,
            action=RosterLog.Action.ASSIGN,
            credits_delta=amount,
            by_admin=True,
            note=f"Acquisto mercato: {session.title}",
        )
        logger.info(
            f"Market bid won: player='{player.name}' ({player.role}), winner='{participant.display_name}', "
            f"amount={amount:.0f} FM, release={w['released_player']}"
        )

    summary = _summary(plan)
    session.results_summary = summary
    session.status = MarketSession.Status.RESOLVED
    session.save(update_fields=["status", "results_summary", "updated_at"])
    logger.info(
        f"Market session {session_id} ('{session.title}') resolved: "
        f"{summary['total_acquisitions']} acquisitions, {summary['total_ties']} ties"
    )
    return summary
