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


def market_release_refund(session, player):
    """Crediti restituiti tagliando ``player`` in questa sessione (anche per l'app)."""
    return _calc_release_refund(session, player)


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
    from .bidding import gk_clubs_problem
    if gk_clubs_problem(participant, player):
        return {"ok": False, "error": "gk_clubs",
                "message": "Hai già portieri di due squadre di Serie A: puoi prendere solo portieri di quelle squadre."}
    if player.rescinded_from_id == participant.id:
        return {"ok": False, "error": "rescinded_rebuy",
                "message": "Hai perso questo giocatore al rinnovo: non puoi ricomprarlo in questo mercato."}

    try:
        val = Decimal(str(amount).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError):
        return {"ok": False, "error": "invalid_amount", "message": "Importo offerta non valido."}

    val = val.quantize(Decimal("1"))
    if val < Decimal("1"):
        return {"ok": False, "error": "amount_too_low", "message": "L'offerta minima è 1 FM."}

    total_rule = session.budget_rule == MarketSession.BudgetRule.TOTAL
    existing = MarketBid.objects.filter(session=session, participant=participant)
    # Con la regola "totale" (regolamento 5.2) ogni invio è una nuova offerta,
    # anche sullo stesso giocatore; altrimenti l'offerta sul giocatore si aggiorna.
    replaces = None if total_rule else existing.filter(player=player).first()
    if session.max_bids and replaces is None and existing.count() >= session.max_bids:
        return {
            "ok": False, "error": "too_many_bids",
            "message": f"Hai già inviato il massimo di {session.max_bids} offerte per questa sessione.",
        }

    if session.require_same_role_release and not release_player_id:
        return {
            "ok": False, "error": "release_required",
            "message": f"Ogni acquisto deve sostituire un tuo giocatore di pari ruolo ({player.role}): scegli chi tagliare.",
        }

    release_player = None
    refund = Decimal("0")
    if release_player_id:
        if not session.allow_conditional_release and not session.require_same_role_release:
            return {"ok": False, "error": "release_not_allowed", "message": "Svincoli condizionati non ammessi in questa sessione."}
        try:
            release_player = Player.objects.get(pk=release_player_id)
        except Player.DoesNotExist:
            return {"ok": False, "error": "release_player_not_found", "message": "Calciatore da svincolare non trovato."}

        if release_player.owner_id != participant.id:
            return {"ok": False, "error": "release_player_not_owned", "message": "Il calciatore da svincolare non appartiene alla tua rosa."}

        if session.require_same_role_release and release_player.role != player.role:
            return {
                "ok": False, "error": "release_wrong_role",
                "message": f"Devi tagliare un giocatore dello stesso ruolo ({player.role}).",
            }

        refund = _calc_release_refund(session, release_player)

    # Regola "totale": il tetto è il budget, i tagli non lo alzano (la somma
    # di tutte le offerte si controlla allo spoglio).
    max_spendable = participant.remaining_credits + (Decimal("0") if total_rule else refund)
    from .salary import purchase_room
    room = purchase_room(participant, market_session=session)
    if room is not None and val > room:
        return {
            "ok": False, "error": "salary_cap",
            "message": f"Tetto salariale: in questa sessione puoi spendere ancora {room:.0f} FM.",
        }
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

    fields = {
        "amount": val,
        "priority": prio,
        "release_player": release_player,
        "status": MarketBid.Status.PENDING,
        "note": "",
    }
    if replaces is not None:
        for k, v in fields.items():
            setattr(replaces, k, v)
        replaces.save()
        bid = replaces
    else:
        bid = MarketBid.objects.create(session=session, participant=participant, player=player, **fields)

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

    def __init__(self, session, bids=None):
        self.session = session
        self.league = session.league
        if bids is None:
            bids = (
                session.bids.filter(status=MarketBid.Status.PENDING)
                .select_related("participant", "player", "release_player")
            )
        self.bids = list(bids)
        self.participants = {
            p.id: p for p in Participant.objects.filter(league=self.league)
        }
        for b in self.bids:
            self.participants.setdefault(b.participant_id, b.participant)
        self.remaining = {pid: p.remaining_credits for pid, p in self.participants.items()}
        # Tetto salariale (None = nessun limite per quella squadra).
        from .salary import purchase_room
        self.cap_room = {pid: purchase_room(p, market_session=session) for pid, p in self.participants.items()}

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
        for pid, role in session.bids.filter(status=MarketBid.Status.WON).values_list(
            "participant_id", "player__role"
        ):
            self.role_acquisitions[pid][role] += 1

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
        if self.session.require_same_role_release and (release is None or release.role != role):
            return (
                False,
                "Il giocatore da tagliare (pari ruolo) non è più in rosa: l'acquisto non può sostituire nessuno",
                None,
                Decimal("0"),
            )

        available = self.remaining[participant.id] + refund
        if available < bid.amount:
            return (
                False,
                f"Crediti insufficienti al momento dello spoglio (disponibili: {available:.0f} FM)" + release_note,
                None,
                Decimal("0"),
            )

        room = self.cap_room.get(participant.id)
        if room is not None and bid.amount > room:
            return (False, f"Tetto salariale: margine rimasto {room:.0f} FM", None, Decimal("0"))

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
        if self.cap_room.get(pid) is not None:
            self.cap_room[pid] -= bid.amount
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
            "refund_exact": str(refund),
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
        seen = {w.participant_id}
        first_wins = self.session.tie_break == MarketSession.TieBreak.FIRST
        for b in bids:
            if b.id in self.outcome or b is w or b.participant_id in seen:
                continue
            if b.amount == w.amount and b.priority == w.priority:
                if first_wins:
                    # Bids are ranked by insertion time on equal amount, so w
                    # is the earliest: the others lose the tie outright.
                    self._decide(
                        b, MarketBid.Status.LOST,
                        f"Pari merito: vince l'offerta inserita prima ({w.participant.display_name})",
                    )
                    seen.add(b.participant_id)
                    continue
                ok, bnote, _, _ = self._check(b)
                if ok:
                    tied.append(b)
                    seen.add(b.participant_id)
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
                "contender_list": [
                    {"id": b.participant_id, "name": b.participant.display_name} for b in group
                ],
            })
        else:
            w, release, refund, note = self._ladder_price(w, release, refund, note, bids)
            self._award(w, release, refund, note)

        won_by = w.participant_id if not tied else None
        for b in bids:
            if b.id not in self.outcome:
                if b.participant_id == won_by and b.amount > w.amount:
                    self._decide(b, MarketBid.Status.LOST,
                                 f"Non necessaria: aggiudicato con la tua offerta di {w.amount:.0f} FM")
                elif b.participant_id == won_by:
                    self._decide(b, MarketBid.Status.LOST, "Superata da una tua offerta più alta")
                else:
                    self._decide(b, MarketBid.Status.LOST, "Offerta superata")

    def _ladder_price(self, w, release, refund, note, bids):
        """Regolamento 5.03: con più offerte sullo stesso giocatore si paga
        "l'offerta minima fatta" che basta a battere le altre squadre."""
        if self.session.budget_rule != MarketSession.BudgetRule.TOTAL:
            return w, release, refund, note
        # Le offerte rivali già scartate (non valide) non contano.
        rivals = [b.amount for b in bids
                  if b.participant_id != w.participant_id and b.id not in self.outcome]
        beat = max(rivals) if rivals else Decimal("0")
        mine = sorted((b for b in bids if b.participant_id == w.participant_id
                       and b.id not in self.outcome and b.amount > beat and b is not w),
                      key=lambda b: (b.amount, b.created_at))
        for b in mine:
            if b.amount >= w.amount:
                break
            ok, bnote, brelease, brefund = self._check(b)
            if ok:
                return b, brelease, brefund, bnote
        return w, release, refund, note

    def _apply_budget_rule(self):
        """Regolamento 5.03: il totale delle offerte massime (una per giocatore)
        di una squadra non può superare il suo budget; se lo supera si
        annullano le offerte partendo dalla più alta finché il totale non rientra."""
        if self.session.budget_rule != MarketSession.BudgetRule.TOTAL:
            return
        by_participant = defaultdict(list)
        for b in self.bids:
            by_participant[b.participant_id].append(b)

        def total_of_max(bids):
            best = {}
            for b in bids:
                best[b.player_id] = max(best.get(b.player_id, Decimal("0")), b.amount)
            return sum(best.values())

        for pid, bids in by_participant.items():
            budget = self.remaining.get(pid, Decimal("0"))
            if self.cap_room.get(pid) is not None:
                budget = min(budget, self.cap_room[pid])
            alive = list(bids)
            # Highest first; on equal amounts the most recent goes first.
            for b in sorted(bids, key=lambda x: (-x.amount, -x.created_at.timestamp(), -x.id)):
                if total_of_max(alive) <= budget:
                    break
                alive.remove(b)
                self._decide(
                    b, MarketBid.Status.CANCELLED,
                    f"Annullata: il totale delle offerte superava il budget ({budget:.0f} FM)",
                )

    # -- main loop -------------------------------------------------------------

    def run(self):
        self._apply_budget_rule()
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
        if status in (MarketBid.Status.LOST, MarketBid.Status.CANCELLED):
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
        "open_ties": len(plan.tied),
    }
    if preview:
        summary["preview"] = True
    else:
        summary["resolved_at"] = timezone.now().isoformat()
    return summary


def _apply_award(session, participant, player, amount, release, refund):
    """Write one acquisition: ownership, optional cut, credits and roster log."""
    player.owner = participant
    player.cost = amount
    player.save(update_fields=["owner", "cost"])
    from .contracts import on_player_acquired
    on_player_acquired(player)

    if release is not None:
        release.owner = None
        release.cost = Decimal("0")
        release.save(update_fields=["owner", "cost"])
        RosterLog.objects.create(
            participant=participant,
            participant_name=participant.display_name,
            player_name=release.name,
            player_role=release.role,
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
        f"amount={amount:.0f} FM, release={release.name if release else None}"
    )


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
        release, refund = plan.awards[bid.id]
        _apply_award(session, plan.participants[w["winner_id"]], bid.player, bid.amount, release, refund)

    summary = _summary(plan)
    session.results_summary = summary
    session.status = MarketSession.Status.RESOLVED
    session.save(update_fields=["status", "results_summary", "updated_at"])
    logger.info(
        f"Market session {session_id} ('{session.title}') resolved: "
        f"{summary['total_acquisitions']} acquisitions, {summary['total_ties']} ties"
    )
    return summary


# --- Dopo lo spoglio: pareggi e annullamento ---------------------------------


@transaction.atomic
def settle_market_tie(session_id, player_id, winner_id=None, rng=None, rebids=None):
    """Risolve un pari merito: vincitore scelto dall'admin o sorteggiato.

    ``winner_id`` None = sorteggio tra i contendenti ancora in regola (crediti,
    slot, tetti). Il calciatore va al vincitore al prezzo del pareggio.
    """
    import random

    session = (
        MarketSession.objects.select_for_update().select_related("league").get(pk=session_id)
    )
    if session.status != MarketSession.Status.RESOLVED:
        return {"ok": False, "message": "Lo spoglio non è ancora stato eseguito."}

    summary = session.results_summary or {}
    tie = next(
        (t for t in summary.get("tied", []) if t["player_id"] == player_id and not t.get("settled")),
        None,
    )
    if tie is None:
        return {"ok": False, "message": "Nessun pareggio aperto per questo calciatore."}

    bids = list(
        session.bids.filter(player_id=player_id, status=MarketBid.Status.TIED)
        .select_related("participant", "player", "release_player")
    )
    plan = _Plan(session, bids=bids)
    eligible = []
    reasons = {}
    for b in bids:
        if plan.owner.get(player_id) is not None:
            return {"ok": False, "message": "Il calciatore non è più svincolato."}
        ok, note, release, refund = plan._check(b)
        if ok:
            eligible.append((b, release, refund))
        else:
            reasons[b.participant.display_name] = note

    if rebids:
        # Secondo sfoglio speciale (5.03): vince l'offerta più alta tra le nuove,
        # che diventa il prezzo; un nuovo pari merito lascia il pareggio aperto.
        offers = {}
        for b, rel, ref in eligible:
            raw = rebids.get(b.participant_id)
            try:
                amount = Decimal(str(raw)).quantize(Decimal("1")) if raw not in (None, "") else None
            except (InvalidOperation, ValueError):
                amount = None
            if amount is not None and amount >= b.amount:
                offers[b.participant_id] = (amount, b, rel, ref)
        if not offers:
            return {"ok": False, "message": "Nessuna offerta valida: nel secondo sfoglio si offre almeno quanto la prima busta."}
        top = max(o[0] for o in offers.values())
        best = [o for o in offers.values() if o[0] == top]
        if len(best) > 1:
            return {"ok": False, "message": f"Nuovo pari merito a {top:.0f} FM: ripeti lo sfoglio."}
        amount, b, rel, ref = best[0]
        room = plan.cap_room.get(b.participant_id)
        if plan.remaining[b.participant_id] + ref < amount or (room is not None and amount > room):
            return {"ok": False, "message": f"{b.participant.display_name} non ha budget o tetto per {amount:.0f} FM."}
        b.amount = amount
        b.save(update_fields=["amount", "updated_at"])
        chosen = (b, rel, ref)
        method = "rebid"
    elif winner_id is not None:
        chosen = next((e for e in eligible if e[0].participant_id == int(winner_id)), None)
        if chosen is None:
            name = next((b.participant.display_name for b in bids if b.participant_id == int(winner_id)), None)
            why = reasons.get(name, "non è tra i contendenti")
            return {"ok": False, "message": f"Impossibile assegnare a {name or 'questa squadra'}: {why}."}
        method = "admin"
    else:
        if not eligible:
            return {"ok": False, "message": "Nessun contendente può più permettersi il calciatore."}
        chosen = (rng or random.SystemRandom()).choice(eligible)
        method = "draw"

    win_bid, release, refund = chosen
    participant = plan.participants[win_bid.participant_id]
    _apply_award(session, participant, win_bid.player, win_bid.amount, release, refund)

    how = {"draw": "sorteggio", "rebid": "secondo sfoglio"}.get(method, "scelta admin")
    for b in bids:
        if b.id == win_bid.id:
            b.status = MarketBid.Status.WON
            b.note = f"Aggiudicato allo spareggio ({how})"
        else:
            b.status = MarketBid.Status.LOST
            b.note = f"Spareggio perso ({how})"
        b.save(update_fields=["status", "note", "updated_at"])

    tie["settled"] = {
        "winner_id": participant.id,
        "winner_name": participant.display_name,
        "method": method,
        "at": timezone.now().isoformat(),
    }
    summary.setdefault("won", []).append({
        "bid_id": win_bid.id,
        "player_id": win_bid.player_id,
        "player_name": win_bid.player.name,
        "player_role": win_bid.player.role,
        "player_team": win_bid.player.team,
        "winner_id": participant.id,
        "winner_name": participant.display_name,
        "amount": int(win_bid.amount),
        "released_player_id": release.id if release else None,
        "released_player": release.name if release else None,
        "released_player_cost": str(release.cost) if release else None,
        "refund": int(refund),
        "refund_exact": str(refund),
        "tie_break": method,
    })
    summary["total_acquisitions"] = len(summary["won"])
    summary["open_ties"] = sum(1 for t in summary.get("tied", []) if not t.get("settled"))
    session.results_summary = summary
    session.save(update_fields=["results_summary", "updated_at"])
    logger.info(
        f"Market tie settled ({method}): session={session_id}, player='{win_bid.player.name}', "
        f"winner='{participant.display_name}'"
    )
    return {"ok": True, "winner_id": participant.id, "winner_name": participant.display_name, "method": method}


@transaction.atomic
def undo_market_resolution(session_id):
    """Annulla uno spoglio: rose e crediti tornano come prima, buste in attesa.

    Rifiuta se dopo lo spoglio qualcuno ha toccato i calciatori coinvolti
    (rivenduti, svincolati, riassegnati): in quel caso l'annullamento
    automatico non saprebbe più cosa ripristinare.
    """
    session = MarketSession.objects.select_for_update().get(pk=session_id)
    if session.status != MarketSession.Status.RESOLVED:
        return {"ok": False, "message": "La sessione non risulta scrutinata."}

    won = (session.results_summary or {}).get("won", [])
    ids = {w["player_id"] for w in won} | {w["released_player_id"] for w in won if w.get("released_player_id")}
    players = {p.id: p for p in Player.objects.select_for_update().filter(pk__in=ids)}

    conflicts = []
    for w in won:
        p = players.get(w["player_id"])
        if p is None or p.owner_id != w["winner_id"]:
            conflicts.append(f"{w['player_name']} non è più di {w['winner_name']}")
        rel_id = w.get("released_player_id")
        if rel_id:
            rel = players.get(rel_id)
            if rel is None or rel.owner_id is not None:
                conflicts.append(f"{w['released_player']} (tagliato) non è più svincolato")
    if conflicts:
        return {
            "ok": False,
            "message": "Impossibile annullare, le rose sono cambiate dopo lo spoglio: " + "; ".join(conflicts) + ".",
        }

    for w in reversed(won):
        p = players[w["player_id"]]
        participant = Participant.objects.get(pk=w["winner_id"])
        refund = Decimal(w.get("refund_exact") or w.get("refund") or 0)
        amount = Decimal(w["amount"])
        p.owner = None
        p.cost = Decimal("0")
        p.save(update_fields=["owner", "cost"])
        RosterLog.objects.create(
            participant=participant,
            participant_name=participant.display_name,
            player_name=p.name,
            player_role=p.role,
            action=RosterLog.Action.ADMIN_RELEASE,
            credits_delta=-amount,
            by_admin=True,
            note=f"Annullamento spoglio: {session.title}",
        )
        rel_id = w.get("released_player_id")
        if rel_id:
            rel = players[rel_id]
            rel.owner = participant
            rel.cost = Decimal(w.get("released_player_cost") or 0)
            rel.save(update_fields=["owner", "cost"])
            RosterLog.objects.create(
                participant=participant,
                participant_name=participant.display_name,
                player_name=rel.name,
                player_role=rel.role,
                action=RosterLog.Action.ADMIN_ASSIGN,
                credits_delta=refund,
                by_admin=True,
                note=f"Annullamento spoglio: {session.title}",
            )
        Participant.objects.filter(pk=participant.id).update(
            spent_credits=F("spent_credits") - (amount - refund)
        )

    session.bids.all().update(
        status=MarketBid.Status.PENDING, note="", updated_at=timezone.now()
    )
    session.status = MarketSession.Status.CLOSED
    session.results_summary = {}
    session.save(update_fields=["status", "results_summary", "updated_at"])
    logger.info(f"Market resolution undone: session={session_id} ('{session.title}'), {len(won)} acquisitions reverted")
    return {"ok": True, "reverted": len(won)}


def sync_market_schedule(league=None):
    """Apre le sessioni programmate e chiude quelle scadute.

    Chiamata in modo pigro dalle pagine mercato (niente scheduler esterno):
    DRAFT con ``opens_at`` passato → OPEN, OPEN con ``closes_at`` passato → CLOSED.
    Lo spoglio resta un'azione dell'admin, che può prima vederne l'anteprima.
    """
    now = timezone.now()
    qs = MarketSession.objects.all()
    if league is not None:
        qs = qs.filter(league=league)
    opened = qs.filter(
        status=MarketSession.Status.DRAFT, opens_at__isnull=False, opens_at__lte=now
    ).exclude(closes_at__lte=now).update(status=MarketSession.Status.OPEN, updated_at=now)
    closed = qs.filter(
        status__in=[MarketSession.Status.OPEN, MarketSession.Status.DRAFT],
        closes_at__isnull=False, closes_at__lte=now,
    ).update(status=MarketSession.Status.CLOSED, updated_at=now)
    return opened, closed
