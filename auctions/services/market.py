"""Sealed-bid market session (mercato di riparazione) business logic."""
from collections import defaultdict
from decimal import ROUND_CEILING, Decimal, InvalidOperation
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


def session_moves(session):
    """The purchases made on the spot in a free agency or clause session, from
    the roster log. They belong to the session by date, not by title: a
    renamed session keeps its moves (and its weekly limit)."""
    prefix = ("Clausola rescissoria pagata a" if session.session_type == MarketSession.SessionType.BUYOUT_CLAUSE
              else "Acquisto Free Agency: ")
    return RosterLog.objects.filter(
        participant__league_id=session.league_id, action=RosterLog.Action.ASSIGN,
        note__startswith=prefix, created_at__gte=session.created_at,
    )


def buyout_price(session, player):
    """The clause to pay for ``player``: cost paid times the session's
    multiplier, rounded up to the credit (in decimals: 50 × 1.1 is 55, not 56)."""
    mult = Decimal(str((session.config or {}).get("buyout_multiplier") or "1.5"))
    base_cost = player.cost if (player.cost is not None and player.cost >= 1) else Decimal("1")
    return (base_cost * mult).to_integral_value(rounding=ROUND_CEILING)


def _release_refused(session, release_player_id):
    """The error when the session doesn't allow the cut asked for, else None."""
    if release_player_id and not session.allow_conditional_release and not session.require_same_role_release:
        return {"ok": False, "error": "release_not_allowed", "message": "In questa sessione non si possono tagliare giocatori."}
    return None


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
    # Le buste valgono solo nei mercati a buste: un reclamo waiver pagherebbe
    # l'importo della busta, un rinnovo non ha offerte.
    if session.session_type not in (MarketSession.SessionType.SEALED_BIDS, MarketSession.SessionType.REPAIR):
        return {"ok": False, "error": "invalid_session_type", "message": "In questa sessione non si consegnano buste."}

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

    # Il mercato vale solo per la sua lega: anche un calciatore senza lega
    # (listone globale) non si compra nella sessione di una lega.
    if player.owner_id is not None or player.league_id != session.league_id:
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
            owner_id__in=list(self.participants), abroad_list=False
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
    if session.session_type == MarketSession.SessionType.WAIVER_WIRE:
        return plan_waiver_resolution(session.id)
    if session.session_type in (MarketSession.SessionType.FREE_AGENCY, MarketSession.SessionType.BUYOUT_CLAUSE):
        return {"won": [], "tied": [], "lost": [], "total_acquisitions": 0, "total_ties": 0, "open_ties": 0, "preview": True}
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
    player.acquired_at = timezone.now()
    player.save(update_fields=["owner", "cost", "acquired_at"])
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

    if session.session_type == MarketSession.SessionType.WAIVER_WIRE:
        return resolve_waiver_session(session.id)

    if session.session_type == MarketSession.SessionType.FREE_AGENCY:
        moves = list(session_moves(session).order_by("-created_at"))
        summary = {
            "won": [
                {
                    "player_name": m.player_name,
                    "player_role": m.player_role,
                    "winner_name": m.participant_name,
                    "amount": int(m.credits_delta),
                    "released_player": None,
                }
                for m in moves
            ],
            "total_acquisitions": len(moves),
            "total_ties": 0,
            "open_ties": 0,
            "resolved_at": timezone.now().isoformat(),
        }
        session.results_summary = summary
        session.status = MarketSession.Status.RESOLVED
        session.save(update_fields=["status", "results_summary", "updated_at"])
        return summary

    if session.session_type == MarketSession.SessionType.BUYOUT_CLAUSE:
        moves = list(session_moves(session).order_by("-created_at"))
        summary = {
            "won": [
                {
                    "player_name": m.player_name,
                    "player_role": m.player_role,
                    "winner_name": m.participant_name,
                    "amount": int(m.credits_delta),
                    "released_player": None,
                }
                for m in moves
            ],
            "total_acquisitions": len(moves),
            "total_ties": 0,
            "open_ties": 0,
            "resolved_at": timezone.now().isoformat(),
        }
        session.results_summary = summary
        session.status = MarketSession.Status.RESOLVED
        session.save(update_fields=["status", "results_summary", "updated_at"])
        return summary

    if session.session_type == MarketSession.SessionType.RENEWALS:
        from .contracts import close_renewals
        # Comma 4.1: Chi non è stato dichiarato per il rinnovo è svincolato
        undeclared = list(Player.objects.filter(
            owner__league=session.league,
            contract_years=0,
            abroad_list=False,
            renewal_declared__isnull=True,
        ))
        auto_released = []
        for p in undeclared:
            auto_released.append({
                "player_name": p.name,
                "participant_name": p.owner.display_name if p.owner else "Svincolato",
            })
            p.owner = None
            p.cost = Decimal("0")
            p.contract_years = None
            p.save(update_fields=["owner", "cost", "contract_years"])

        # Gather ContractEvents from current season
        from ..models import ContractEvent
        events = list(ContractEvent.objects.filter(
            league=session.league,
            season=session.league.season_number,
        ).order_by("-created_at"))

        renewed_events = [e for e in events if e.kind == ContractEvent.Kind.RENEWED]
        rescinded_events = [e for e in events if e.kind == ContractEvent.Kind.RESCINDED]
        not_renewed_events = [e for e in events if e.kind == ContractEvent.Kind.NOT_RENEWED]

        summary = {
            "won": [
                {
                    "player_name": e.player_name,
                    "player_role": e.player.role if e.player else "A",
                    "winner_name": e.participant_name,
                    "amount": 0,
                    "released_player": None,
                    "years": e.years,
                }
                for e in renewed_events
            ],
            "renewed": [
                {"player_name": e.player_name, "participant_name": e.participant_name, "years": e.years}
                for e in renewed_events
            ],
            "rescinded": [
                {"player_name": e.player_name, "participant_name": e.participant_name}
                for e in rescinded_events
            ],
            "released": [
                {"player_name": e.player_name, "participant_name": e.participant_name}
                for e in not_renewed_events
            ] + auto_released,
            "total_acquisitions": len(renewed_events),
            "total_ties": 0,
            "open_ties": 0,
            "resolved_at": timezone.now().isoformat(),
        }
        close_renewals(session.league_id)
        session.results_summary = summary
        session.status = MarketSession.Status.RESOLVED
        session.save(update_fields=["status", "results_summary", "updated_at"])
        return summary

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
    if session.session_type == MarketSession.SessionType.RENEWALS:
        # Dadi tirati e contratti svincolati non si riavvolgono in blocco.
        return {"ok": False, "message": "La chiusura dei rinnovi non si annulla: correggi i singoli contratti dalla pagina Contratti."}
    if session.session_type in (MarketSession.SessionType.FREE_AGENCY, MarketSession.SessionType.BUYOUT_CLAUSE):
        # Gli acquisti sono avvenuti uno per uno, all'istante: il riepilogo
        # finale non li ha fatti e non li può stornare.
        return {"ok": False, "message": "Gli acquisti di questa finestra sono definitivi: correggi le rose dalla pagina Giocatori."}

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
    summary = session.results_summary or {}
    if "waiver_order_before" in summary:
        config = dict(session.config or {})
        if summary["waiver_order_before"] is None:
            config.pop("waiver_order", None)
        else:
            config["waiver_order"] = summary["waiver_order_before"]
        session.config = config
    session.status = MarketSession.Status.CLOSED
    session.results_summary = {}
    session.save(update_fields=["status", "results_summary", "config", "updated_at"])
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


# =============================================================================
# MERCATO LIBERO CONTINUO (FREE AGENCY)
# =============================================================================

@transaction.atomic
def acquire_free_agent(session_id, participant_id, player_id, release_player_id=None):
    """Instant acquisition of a free agent (Free Agency mode)."""
    try:
        session = MarketSession.objects.select_for_update().get(pk=session_id)
    except MarketSession.DoesNotExist:
        return {"ok": False, "error": "session_not_found", "message": "Sessione non trovata."}

    if not session.is_open:
        return {"ok": False, "error": "session_closed", "message": "La finestra di mercato è chiusa."}

    if session.session_type != MarketSession.SessionType.FREE_AGENCY:
        return {"ok": False, "error": "invalid_session_type", "message": "Questa sessione non è impostata per il Mercato Libero (Free Agency)."}

    try:
        participant = Participant.objects.select_for_update().get(pk=participant_id)
    except Participant.DoesNotExist:
        return {"ok": False, "error": "participant_not_found", "message": "Partecipante non trovato."}

    if not participant.is_active or participant.league_id != session.league_id:
        return {"ok": False, "error": "invalid_participant", "message": "Partecipante non valido per questa lega."}

    cfg = session.config or {}
    max_moves = int(cfg.get("fa_max_moves") or 0)
    if max_moves > 0:
        week_ago = timezone.now() - timezone.timedelta(days=7)
        moves_count = session_moves(session).filter(participant=participant, created_at__gte=week_ago).count()
        if moves_count >= max_moves:
            return {
                "ok": False,
                "error": "move_limit_reached",
                "message": f"Hai raggiunto il limite di {max_moves} cambi per questa settimana ({moves_count}/{max_moves} effettuati)."
            }

    try:
        player = Player.objects.select_for_update().get(pk=player_id)
    except Player.DoesNotExist:
        return {"ok": False, "error": "player_not_found", "message": "Calciatore non trovato."}

    if player.owner_id is not None or player.league_id != session.league_id:
        return {"ok": False, "error": "player_unavailable", "message": "Calciatore non più disponibile tra gli svincolati."}

    from .bidding import gk_clubs_problem
    if gk_clubs_problem(participant, player):
        return {"ok": False, "error": "gk_clubs", "message": "Hai già portieri di due squadre di Serie A: puoi prendere solo portieri di quelle squadre."}

    cost_type = cfg.get("fa_cost_type", "quotation")
    if cost_type == "base":
        cost = Decimal("1")
    else:
        p = player.price_for(session.league)
        cost = p if (p is not None and p >= 1) else Decimal("1")

    refused = _release_refused(session, release_player_id)
    if refused:
        return refused
    release_player = None
    refund = Decimal("0")
    if release_player_id:
        try:
            release_player = Player.objects.select_for_update().get(pk=release_player_id)
        except Player.DoesNotExist:
            return {"ok": False, "error": "release_player_not_found", "message": "Calciatore da svincolare non trovato."}

        if release_player.owner_id != participant.id:
            return {"ok": False, "error": "release_player_not_owned", "message": "Il calciatore da svincolare non appartiene alla tua rosa."}

        if session.require_same_role_release and release_player.role != player.role:
            return {"ok": False, "error": "release_wrong_role", "message": f"Devi tagliare un calciatore dello stesso ruolo ({player.role})."}

        refund = _calc_release_refund(session, release_player)
    elif session.require_same_role_release:
        return {"ok": False, "error": "release_required", "message": f"Ogni acquisto deve sostituire un tuo calciatore di pari ruolo ({player.role})."}

    league = session.league
    cap = league.slots_for(player.role)
    if cap > 0:
        bucket = league.slot_roles(player.role)
        current_count = Player.objects.filter(owner=participant, abroad_list=False, role__in=bucket).count()
        if release_player is not None and release_player.role in bucket:
            current_count -= 1
        if current_count + 1 > cap:
            return {
                "ok": False,
                "error": "roster_full",
                "message": f"Rosa piena per il ruolo {player.role} ({cap} slot). Taglia un calciatore per fargli spazio."
            }

    available = participant.remaining_credits + refund
    if cost > available:
        return {
            "ok": False,
            "error": "insufficient_credits",
            "message": f"Crediti insufficienti. Costo: {cost:.0f} FM, Disponibili: {available:.0f} FM."
        }

    from .salary import purchase_room
    room = purchase_room(participant, market_session=session)
    if room is not None and cost > room:
        return {"ok": False, "error": "salary_cap", "message": f"Tetto salariale superato: puoi spendere al massimo {room:.0f} FM."}

    player.owner = participant
    player.cost = cost
    player.acquired_at = timezone.now()
    player.save(update_fields=["owner", "cost", "acquired_at"])
    from .contracts import on_player_acquired
    on_player_acquired(player)

    if release_player is not None:
        release_player.owner = None
        release_player.cost = Decimal("0")
        release_player.save(update_fields=["owner", "cost"])
        RosterLog.objects.create(
            participant=participant,
            participant_name=participant.display_name,
            player_name=release_player.name,
            player_role=release_player.role,
            action=RosterLog.Action.RELEASE,
            credits_delta=-refund,
            by_admin=False,
            note=f"Taglio Free Agency: {session.title}",
        )

    Participant.objects.filter(pk=participant.id).update(
        spent_credits=F("spent_credits") + (cost - refund)
    )
    RosterLog.objects.create(
        participant=participant,
        participant_name=participant.display_name,
        player_name=player.name,
        player_role=player.role,
        action=RosterLog.Action.ASSIGN,
        credits_delta=cost,
        by_admin=False,
        note=f"Acquisto Free Agency: {session.title}",
    )
    logger.info(
        f"Free Agency acquire: session={session_id}, participant='{participant.display_name}', "
        f"player='{player.name}', cost={cost:.0f} FM, release={release_player.name if release_player else None}"
    )

    return {
        "ok": True,
        "message": f"{player.name} acquistato con successo per {cost:.0f} FM!",
        "player_id": player.id,
        "player_name": player.name,
        "cost": int(cost),
        "refund": int(refund),
    }


# =============================================================================
# MERCATO CON CLAUSOLE RESCISORIE (BUYOUT CLAUSE)
# =============================================================================

@transaction.atomic
def execute_buyout(session_id, buyer_id, player_id, release_player_id=None):
    """Exercise a buyout clause on an opponent's player (Buyout Clause mode)."""
    try:
        session = MarketSession.objects.select_for_update().get(pk=session_id)
    except MarketSession.DoesNotExist:
        return {"ok": False, "error": "session_not_found", "message": "Sessione non trovata."}

    if not session.is_open:
        return {"ok": False, "error": "session_closed", "message": "La finestra di mercato è chiusa."}

    if session.session_type != MarketSession.SessionType.BUYOUT_CLAUSE:
        return {"ok": False, "error": "invalid_session_type", "message": "Questa sessione non ammette clausole rescissorie."}

    try:
        buyer = Participant.objects.select_for_update().get(pk=buyer_id)
    except Participant.DoesNotExist:
        return {"ok": False, "error": "buyer_not_found", "message": "Partecipante acquirente non trovato."}

    if not buyer.is_active or buyer.league_id != session.league_id:
        return {"ok": False, "error": "invalid_buyer", "message": "Partecipante non valido per questa lega."}

    try:
        player = Player.objects.select_for_update().get(pk=player_id)
    except Player.DoesNotExist:
        return {"ok": False, "error": "player_not_found", "message": "Calciatore non trovato."}

    if player.owner_id is None or player.league_id != session.league_id:
        return {"ok": False, "error": "player_not_owned", "message": "Il calciatore è svincolato, non puoi pagare una clausola su di lui."}

    if player.owner_id == buyer.id:
        return {"ok": False, "error": "already_owned", "message": "Il calciatore appartiene già alla tua squadra!"}

    seller = Participant.objects.select_for_update().get(pk=player.owner_id)

    cfg = session.config or {}
    # 0 = nessuna protezione: solo una chiave assente vale i 7 giorni di default.
    hold_days = int(cfg.get("buyout_min_hold_days") if cfg.get("buyout_min_hold_days") is not None else 7)
    if hold_days > 0:
        if player.acquired_at is not None:
            days_held = (timezone.now() - player.acquired_at).total_seconds() / 86400.0
            if days_held < hold_days:
                days_left = max(1, int(hold_days - days_held) + 1)
                return {
                    "ok": False,
                    "error": "player_protected",
                    "message": f"{player.name} è protetto da clausola per altri {days_left} giorni.",
                }
        else:
            last_assign = RosterLog.objects.filter(
                participant=seller,
                player_name=player.name,
                action__in=[RosterLog.Action.ASSIGN, RosterLog.Action.ADMIN_ASSIGN, RosterLog.Action.TRADE]
            ).order_by("-created_at").first()
            if last_assign is not None:
                days_held = (timezone.now() - last_assign.created_at).total_seconds() / 86400.0
                if days_held < hold_days:
                    days_left = max(1, int(hold_days - days_held) + 1)
                    return {
                        "ok": False,
                        "error": "player_protected",
                        "message": f"{player.name} è protetto da clausola per altri {days_left} giorni.",
                    }

    buyout_amount = buyout_price(session, player)

    refused = _release_refused(session, release_player_id)
    if refused:
        return refused
    release_player = None
    refund = Decimal("0")
    if release_player_id:
        try:
            release_player = Player.objects.select_for_update().get(pk=release_player_id)
        except Player.DoesNotExist:
            return {"ok": False, "error": "release_player_not_found", "message": "Calciatore da svincolare non trovato."}
        if release_player.owner_id != buyer.id:
            return {"ok": False, "error": "release_player_not_owned", "message": "Il calciatore da svincolare non appartiene alla tua rosa."}
        if session.require_same_role_release and release_player.role != player.role:
            return {"ok": False, "error": "release_wrong_role", "message": f"Devi tagliare un calciatore dello stesso ruolo ({player.role})."}
        refund = _calc_release_refund(session, release_player)
    elif session.require_same_role_release:
        return {"ok": False, "error": "release_required", "message": f"Devi tagliare un calciatore dello stesso ruolo ({player.role})."}

    league = session.league
    cap = league.slots_for(player.role)
    if cap > 0:
        bucket = league.slot_roles(player.role)
        current_count = Player.objects.filter(owner=buyer, abroad_list=False, role__in=bucket).count()
        if release_player is not None and release_player.role in bucket:
            current_count -= 1
        if current_count + 1 > cap:
            return {
                "ok": False,
                "error": "roster_full",
                "message": f"Rosa piena per il ruolo {player.role} ({cap} slot). Taglia un giocatore per fargli spazio.",
            }

    available = buyer.remaining_credits + refund
    if buyout_amount > available:
        return {
            "ok": False,
            "error": "insufficient_credits",
            "message": f"Crediti insufficienti. La clausola è {buyout_amount:.0f} FM, ne hai {available:.0f} FM.",
        }

    from .salary import purchase_room
    room = purchase_room(buyer, market_session=session)
    if room is not None and buyout_amount > room:
        return {"ok": False, "error": "salary_cap", "message": f"Tetto salariale superato: puoi spendere al massimo {room:.0f} FM."}

    if release_player is not None:
        release_player.owner = None
        release_player.cost = Decimal("0")
        release_player.save(update_fields=["owner", "cost"])
        RosterLog.objects.create(
            participant=buyer,
            participant_name=buyer.display_name,
            player_name=release_player.name,
            player_role=release_player.role,
            action=RosterLog.Action.RELEASE,
            credits_delta=-refund,
            by_admin=False,
            note=f"Taglio per clausola {player.name}: {session.title}",
        )

    player.owner = buyer
    player.cost = buyout_amount
    player.acquired_at = timezone.now()
    player.save(update_fields=["owner", "cost", "acquired_at"])
    from .contracts import on_player_acquired
    on_player_acquired(player)

    Participant.objects.filter(pk=seller.id).update(
        spent_credits=F("spent_credits") - buyout_amount
    )
    RosterLog.objects.create(
        participant=seller,
        participant_name=seller.display_name,
        player_name=player.name,
        player_role=player.role,
        action=RosterLog.Action.RELEASE,
        credits_delta=-buyout_amount,
        by_admin=False,
        note=f"Clausola rescissoria pagata da {buyer.display_name}: +{buyout_amount:.0f} FM",
    )

    Participant.objects.filter(pk=buyer.id).update(
        spent_credits=F("spent_credits") + (buyout_amount - refund)
    )
    RosterLog.objects.create(
        participant=buyer,
        participant_name=buyer.display_name,
        player_name=player.name,
        player_role=player.role,
        action=RosterLog.Action.ASSIGN,
        credits_delta=buyout_amount,
        by_admin=False,
        note=f"Clausola rescissoria pagata a {seller.display_name}: {buyout_amount:.0f} FM",
    )

    logger.info(
        f"Buyout executed: session={session_id}, buyer='{buyer.display_name}', seller='{seller.display_name}', "
        f"player='{player.name}', amount={buyout_amount:.0f} FM"
    )

    return {
        "ok": True,
        "message": f"Clausola rescissoria esercitata con successo! {player.name} è ora della tua squadra.",
        "player_id": player.id,
        "player_name": player.name,
        "amount": int(buyout_amount),
        "seller_name": seller.display_name,
    }


# =============================================================================
# DRAFT WAIVER WIRE A TURNI
# =============================================================================

@transaction.atomic
def place_waiver_claim(session_id, participant_id, player_id, priority=1, release_player_id=None):
    """Place or update a ranked waiver claim for a free agent."""
    try:
        session = MarketSession.objects.select_for_update().get(pk=session_id)
    except MarketSession.DoesNotExist:
        return {"ok": False, "error": "session_not_found", "message": "Sessione non trovata."}

    if not session.is_open:
        return {"ok": False, "error": "session_closed", "message": "La finestra waiver è chiusa."}
    if session.session_type != MarketSession.SessionType.WAIVER_WIRE:
        return {"ok": False, "error": "invalid_session_type", "message": "Questa sessione non è un draft waiver."}

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

    if player.owner_id is not None or player.league_id != session.league_id:
        return {"ok": False, "error": "player_unavailable", "message": "Calciatore non disponibile sul mercato svincolati."}

    cfg = session.config or {}
    cost_type = cfg.get("fa_cost_type", "quotation")
    if cost_type == "base":
        cost = Decimal("1")
    else:
        p = player.price_for(session.league)
        cost = p if (p is not None and p >= 1) else Decimal("1")

    refused = _release_refused(session, release_player_id)
    if refused:
        return refused
    if session.require_same_role_release and not release_player_id:
        return {"ok": False, "error": "release_required",
                "message": f"Ogni acquisto deve sostituire un tuo giocatore di pari ruolo ({player.role}): scegli chi tagliare."}
    release_player = None
    if release_player_id:
        try:
            release_player = Player.objects.get(pk=release_player_id)
        except Player.DoesNotExist:
            return {"ok": False, "error": "release_player_not_found", "message": "Calciatore da svincolare non trovato."}
        if release_player.owner_id != participant.id:
            return {"ok": False, "error": "release_player_not_owned", "message": "Il calciatore da svincolare non appartiene alla tua rosa."}
        if session.require_same_role_release and release_player.role != player.role:
            return {"ok": False, "error": "release_wrong_role", "message": f"Devi tagliare un calciatore dello stesso ruolo ({player.role})."}

    try:
        prio = max(1, int(priority or 1))
    except (ValueError, TypeError):
        prio = 1

    existing = MarketBid.objects.filter(session=session, participant=participant, player=player).first()
    if existing is not None:
        existing.priority = prio
        existing.amount = cost
        existing.release_player = release_player
        existing.status = MarketBid.Status.PENDING
        existing.save(update_fields=["priority", "amount", "release_player", "status", "updated_at"])
        bid = existing
    else:
        bid = MarketBid.objects.create(
            session=session,
            participant=participant,
            player=player,
            amount=cost,
            priority=prio,
            release_player=release_player,
            status=MarketBid.Status.PENDING,
        )

    return {
        "ok": True,
        "claim_id": bid.id,
        "bid_id": bid.id,
        "message": f"Reclamo waiver inserito per {player.name} con priorità #{prio}!",
        "claim": {
            "id": bid.id,
            "player_id": player.id,
            "player_name": player.name,
            "player_role": player.role,
            "player_team": player.team,
            "priority": bid.priority,
            "amount": int(bid.amount),
        },
    }


def delete_waiver_claim(session_id, participant_id, claim_id):
    """Remove a waiver claim."""
    return delete_market_bid(session_id, participant_id, claim_id)


def waiver_order(session, participants):
    """Chi sceglie prima nel draft waiver: id delle squadre di ``participants``
    nell'ordine della sessione (inverso di classifica o rotazione continua)."""
    cfg = session.config or {}
    order_type = cfg.get("waiver_order_type", "inverse_standing")
    all_participants = participants
    order_ids = []
    if order_type == "inverse_standing":
        season = session.league.seasons.order_by("-created_at").first()
        if season:
            try:
                from .calendar import standings
                std = standings(season)
                order_ids = [r["participant_id"] for r in reversed(std) if r["participant_id"] in all_participants]
            except Exception:
                order_ids = []
    elif order_type == "rolling":
        saved_order = cfg.get("waiver_order")
        if not isinstance(saved_order, list):
            # La rotazione continua da dove l'ha lasciata l'ultimo draft della lega.
            last = (
                MarketSession.objects.filter(
                    league=session.league, session_type=MarketSession.SessionType.WAIVER_WIRE,
                    status=MarketSession.Status.RESOLVED,
                ).exclude(pk=session.pk).order_by("-updated_at").first()
            )
            saved_order = (last.config or {}).get("waiver_order") if last else None
        if isinstance(saved_order, list):
            order_ids = [pid for pid in saved_order if pid in all_participants]

    for pid in all_participants:
        if pid not in order_ids:
            order_ids.append(pid)
    return order_ids


def _run_waiver_draft(session, preview=False):
    """Executes or previews waiver claims in round-robin order."""
    cfg = session.config or {}
    order_type = cfg.get("waiver_order_type", "inverse_standing")

    all_participants = {
        p.id: p for p in Participant.objects.filter(league=session.league, is_active=True)
    }

    order_ids = waiver_order(session, all_participants)

    claims = list(
        MarketBid.objects.filter(session=session, status=MarketBid.Status.PENDING)
        .select_related("participant", "player", "release_player")
        .order_by("priority", "-amount", "created_at")
    )

    claims_by_p = defaultdict(list)
    for c in claims:
        claims_by_p[c.participant_id].append(c)

    awarded_player_ids = set()
    won_list = []
    outcome = {}
    rolling_order = list(order_ids)

    owned = defaultdict(lambda: defaultdict(int))
    for owner_id, role in Player.objects.filter(
        owner_id__in=list(all_participants), abroad_list=False
    ).values_list("owner_id", "role"):
        owned[owner_id][role] += 1

    remaining_credits = {pid: p.remaining_credits for pid, p in all_participants.items()}
    from .salary import purchase_room
    cap_room = {pid: purchase_room(p, market_session=session) for pid, p in all_participants.items()}
    # Chi è già stato tagliato in questo draft non si taglia (e non rimborsa) due volte.
    released_ids = set()
    cuts_allowed = session.allow_conditional_release or session.require_same_role_release

    while True:
        round_pick_made = False
        current_round_participants = list(rolling_order)

        for pid in current_round_participants:
            user_claims = [c for c in claims_by_p[pid] if c.id not in outcome]
            pick_claim = None
            for c in user_claims:
                if c.player_id in awarded_player_ids:
                    outcome[c.id] = (MarketBid.Status.LOST, "Calciatore già assegnato a un'altra squadra con priorità più alta")
                    continue
                if c.player.owner_id is not None:
                    # Preso nel frattempo altrove (free agency, assegnazione dell'admin).
                    outcome[c.id] = (MarketBid.Status.LOST, "Calciatore non più svincolato")
                    continue

                release = None
                refund = Decimal("0")
                if c.release_player_id and cuts_allowed:
                    if c.release_player.owner_id == pid and c.release_player_id not in released_ids:
                        release = c.release_player
                        refund = _calc_release_refund(session, release)
                    else:
                        if session.require_same_role_release:
                            outcome[c.id] = (MarketBid.Status.LOST, "Il calciatore da tagliare non è più in rosa")
                            continue

                if session.require_same_role_release and (release is None or release.role != c.player.role):
                    outcome[c.id] = (MarketBid.Status.LOST, f"Richiesto taglio di pari ruolo ({c.player.role})")
                    continue

                role = c.player.role
                cap = session.league.slots_for(role)
                if cap > 0:
                    bucket = session.league.slot_roles(role)
                    count = sum(owned[pid][r] for r in bucket)
                    if release is not None and release.role in bucket:
                        count -= 1
                    if count + 1 > cap:
                        outcome[c.id] = (MarketBid.Status.LOST, f"Rosa piena per il ruolo {role} ({cap} slot)")
                        continue

                available = remaining_credits[pid] + refund
                if available < c.amount:
                    outcome[c.id] = (MarketBid.Status.LOST, f"Crediti insufficienti (disponibili: {available:.0f} FM)")
                    continue

                room = cap_room.get(pid)
                if room is not None and c.amount > room:
                    outcome[c.id] = (MarketBid.Status.LOST, f"Tetto salariale superato (margine {room:.0f} FM)")
                    continue

                pick_claim = (c, release, refund)
                break

            if pick_claim is not None:
                c, rel, ref = pick_claim
                awarded_player_ids.add(c.player_id)
                outcome[c.id] = (MarketBid.Status.WON, "Assegnato al turno waiver")

                owned[pid][c.player.role] += 1
                if rel:
                    owned[pid][rel.role] -= 1
                    released_ids.add(rel.id)
                remaining_credits[pid] -= (c.amount - ref)
                if cap_room.get(pid) is not None:
                    cap_room[pid] -= c.amount

                won_list.append({
                    "bid_id": c.id,
                    "player_id": c.player_id,
                    "player_name": c.player.name,
                    "player_role": c.player.role,
                    "player_team": c.player.team,
                    "winner_id": pid,
                    "winner_name": all_participants[pid].display_name,
                    "amount": int(c.amount),
                    "released_player_id": rel.id if rel else None,
                    "released_player": rel.name if rel else None,
                    "released_player_cost": str(rel.cost) if rel else None,
                    "refund": int(ref),
                    "refund_exact": str(ref),
                })

                if not preview:
                    _apply_award(session, all_participants[pid], c.player, c.amount, rel, ref)

                if order_type == "rolling":
                    rolling_order.remove(pid)
                    rolling_order.append(pid)

                round_pick_made = True

        if not round_pick_made:
            break

    for c in claims:
        if c.id not in outcome:
            outcome[c.id] = (MarketBid.Status.LOST, "Reclamo superato o non soddisfatto nel draft")

    if not preview:
        for c in claims:
            st, nt = outcome[c.id]
            c.status = st
            c.note = nt[:200]
            c.save(update_fields=["status", "note", "updated_at"])

        if order_type == "rolling":
            order_before = (session.config or {}).get("waiver_order")
            session.config["waiver_order"] = rolling_order
            session.save(update_fields=["config", "updated_at"])

    lost_list = []
    for c in claims:
        st, nt = outcome[c.id]
        if st == MarketBid.Status.LOST:
            lost_list.append({
                "bid_id": c.id,
                "player_name": c.player.name,
                "player_role": c.player.role,
                "participant_name": c.participant.display_name,
                "amount": int(c.amount),
                "note": nt,
            })

    summary = {
        "won": won_list,
        "tied": [],
        "lost": lost_list,
        "total_acquisitions": len(won_list),
        "total_ties": 0,
        "open_ties": 0,
        "waiver_order": [all_participants[pid].display_name for pid in rolling_order],
    }
    if preview:
        summary["preview"] = True
    else:
        summary["resolved_at"] = timezone.now().isoformat()
        if order_type == "rolling":
            # L'annullamento rimette l'ordine com'era prima del draft.
            summary["waiver_order_before"] = order_before
        session.results_summary = summary
        session.status = MarketSession.Status.RESOLVED
        session.save(update_fields=["status", "results_summary", "updated_at"])

    return summary


def plan_waiver_resolution(session_id):
    """Simula lo spoglio waiver senza scrivere modifiche."""
    session = MarketSession.objects.select_related("league").get(pk=session_id)
    return _run_waiver_draft(session, preview=True)


@transaction.atomic
def resolve_waiver_session(session_id):
    """Esegue lo spoglio ufficiale waiver con assegnazioni e log."""
    session = MarketSession.objects.select_for_update().select_related("league").get(pk=session_id)
    if session.status == MarketSession.Status.RESOLVED:
        return session.results_summary
    return _run_waiver_draft(session, preview=False)


def get_buste_report_context(session_id):
    """Costruisce il dataset strutturato per il Verbale Ufficiale di Spoglio Buste."""
    session = MarketSession.objects.select_related("league").filter(pk=session_id).first()
    if not session:
        return None
    league = session.league
    summary = session.results_summary or {}
    won_list = summary.get("won", [])
    lost_list = summary.get("lost", [])
    tied_list = summary.get("tied", [])

    participants = list(Participant.objects.filter(league=league).order_by("display_name"))
    teams_stats = {}
    for p in participants:
        teams_stats[p.id] = {
            "participant": p,
            "display_name": p.display_name,
            "won_players": [],
            "cuts": [],
            "lost_count": 0,
            "total_spent": 0,
            "total_refund": 0,
            "net_spent": 0,
            "credits_remaining": p.remaining_credits,
        }

    for w in won_list:
        pid = w.get("winner_id")
        target_st = teams_stats.get(pid)
        if not target_st:
            w_name = w.get("winner_name")
            for st in teams_stats.values():
                if st["display_name"] == w_name:
                    target_st = st
                    break
        if target_st:
            target_st["won_players"].append(w)
            target_st["total_spent"] += w.get("amount", 0)
            if w.get("released_player"):
                target_st["cuts"].append({
                    "name": w.get("released_player"),
                    "refund": w.get("refund", 0),
                })
                target_st["total_refund"] += w.get("refund", 0)

    for l in lost_list:
        pname = l.get("participant_name")
        for st in teams_stats.values():
            if st["display_name"] == pname:
                st["lost_count"] += 1
                break

    for st in teams_stats.values():
        st["net_spent"] = st["total_spent"] - st["total_refund"]

    role_counts = {"P": 0, "D": 0, "C": 0, "A": 0}
    for w in won_list:
        r = w.get("player_role", "")
        if r in role_counts:
            role_counts[r] += 1

    return {
        "session": session,
        "league": league,
        "summary": summary,
        "won_list": won_list,
        "lost_list": lost_list,
        "tied_list": tied_list,
        "role_counts": role_counts,
        "teams_breakdown": [st for st in teams_stats.values() if st["won_players"] or st["lost_count"] > 0],
        "all_teams_stats": list(teams_stats.values()),
        "total_spent": sum(w.get("amount", 0) for w in won_list),
        "total_refund": sum(w.get("refund", 0) for w in won_list),
        "total_acquisitions": len(won_list),
        "resolved_at": summary.get("resolved_at") or session.updated_at,
    }


def build_buste_csv(session):
    """Genera il file CSV per il Verbale Ufficiale di Spoglio Buste."""
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["VERBALE UFFICIALE DI SPOGLIO BUSTE"])
    writer.writerow(["Sessione", session.title])
    writer.writerow(["Lega", session.league.name if session.league else ""])
    summary = session.results_summary or {}
    writer.writerow(["Data Spoglio", summary.get("resolved_at") or ""])
    writer.writerow([])
    writer.writerow([
        "Stato", "Ruolo", "Calciatore", "Squadra Serie A", "Societa Aggiudicataria / Offerente",
        "Offerta FM", "Priorita", "Taglio Condizionato", "Rimborso FM", "Note"
    ])
    for w in summary.get("won", []):
        writer.writerow([
            "AGGIUDICATO",
            w.get("player_role", ""),
            w.get("player_name", ""),
            w.get("player_team", ""),
            w.get("winner_name", ""),
            w.get("amount", 0),
            w.get("priority", 1),
            w.get("released_player", "") or "",
            w.get("refund", 0),
            "Aggiudicazione definitiva"
        ])
    for l in summary.get("lost", []):
        writer.writerow([
            "NON AGGIUDICATO",
            l.get("player_role", ""),
            l.get("player_name", ""),
            l.get("player_team", ""),
            l.get("participant_name", ""),
            l.get("amount", 0),
            l.get("priority", 1),
            "",
            0,
            l.get("note", "")
        ])
    return output.getvalue().encode("utf-8-sig")


