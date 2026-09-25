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

    # Get all pending bids with participants and players
    bids = list(
        session.bids.filter(status=MarketBid.Status.PENDING)
        .select_related("participant", "player", "release_player")
        .order_by("player_id", "priority", "-amount", "created_at")
    )

    # Group bids by player
    bids_by_player = defaultdict(list)
    for b in bids:
        bids_by_player[b.player_id].append(b)

    logger.info(f"Resolving market session {session_id} ('{session.title}'). Total pending bids: {len(bids)}, distinct players: {len(bids_by_player)}")

    # State trackers during resolution
    participants = {
        p.id: p
        for p in Participant.objects.select_for_update().filter(league=session.league)
    }
    credits_spent = defaultdict(Decimal)
    released_player_ids = set()
    acquired_by_participant = defaultdict(list)
    role_acquisitions = defaultdict(lambda: defaultdict(int))

    won_results = []
    tied_results = []
    lost_results = []

    # Process player by player
    for player_id, player_bids in bids_by_player.items():
        # Sort bids for this player: highest amount first, then lowest priority (1 before 2), then oldest
        player_bids.sort(key=lambda b: (-b.amount, b.priority, b.created_at))

        top_amount = player_bids[0].amount

        # Check for ties at the top
        top_bids = [b for b in player_bids if b.amount == top_amount]
        if len(top_bids) > 1 and top_bids[0].priority == top_bids[1].priority:
            # Exact tie at the top amount and priority!
            tied_names = [b.participant.display_name for b in top_bids]
            for b in top_bids:
                b.status = MarketBid.Status.TIED
                b.note = f"Pari merito ({top_amount:.0f} FM) con: {', '.join(t for t in tied_names if t != b.participant.display_name)}"
                b.save(update_fields=["status", "note", "updated_at"])
            logger.info(f"Market tie detected: player='{player_bids[0].player.name}' ({top_amount:.0f} FM) between: {', '.join(tied_names)}")
            tied_results.append({
                "player_id": player_id,
                "player_name": player_bids[0].player.name,
                "player_role": player_bids[0].player.role,
                "player_team": player_bids[0].player.team,
                "amount": int(top_amount),
                "contenders": tied_names,
            })
            # Remaining lower bids for this player are lost
            for b in player_bids[len(top_bids):]:
                b.status = MarketBid.Status.LOST
                b.note = "Offerta superata"
                b.save(update_fields=["status", "note", "updated_at"])
            continue

        assigned = False
        for bid in player_bids:
            participant = participants.get(bid.participant_id)
            if not participant:
                bid.status = MarketBid.Status.LOST
                bid.note = "Partecipante non valido"
                bid.save(update_fields=["status", "note", "updated_at"])
                continue

            role = bid.player.role
            # Check max acquisitions per role limit
            max_role = session.max_for_role(role)
            if max_role > 0 and role_acquisitions[participant.id][role] >= max_role:
                bid.status = MarketBid.Status.LOST
                bid.note = f"Raggiunto limite acquisti per ruolo {role} ({max_role})"
                bid.save(update_fields=["status", "note", "updated_at"])
                continue

            # Check conditional release
            rel_player = bid.release_player
            refund = Decimal("0")
            if rel_player:
                if rel_player.id in released_player_ids:
                    # Player was already released for another acquisition earlier in this resolution!
                    rel_player = None
                else:
                    refund = _calc_release_refund(session, rel_player)

            # Check budget availability
            effective_spent = credits_spent[participant.id]
            current_rem = participant.remaining_credits - effective_spent
            if current_rem + refund < bid.amount:
                bid.status = MarketBid.Status.LOST
                bid.note = f"Crediti insufficienti al momento dello spoglio (disponibili: {current_rem + refund:.0f} FM)"
                bid.save(update_fields=["status", "note", "updated_at"])
                continue

            # Bid is successful!
            assigned = True
            bid.status = MarketBid.Status.WON
            bid.note = "Aggiudicato con successo"
            bid.save(update_fields=["status", "note", "updated_at"])
            logger.info(
                f"Market bid won: player='{bid.player.name}' ({bid.player.role}), winner='{participant.display_name}', "
                f"amount={bid.amount:.0f} FM, release={rel_player.name if rel_player else None}"
            )

            # 1. Assign player
            bid.player.owner = participant
            bid.player.cost = bid.amount
            bid.player.save(update_fields=["owner", "cost"])

            # 2. Release conditional player if any
            if rel_player:
                rel_player.owner = None
                rel_player.cost = Decimal("0")
                rel_player.save(update_fields=["owner", "cost"])
                released_player_ids.add(rel_player.id)
                RosterLog.objects.create(
                    participant=participant,
                    participant_name=participant.display_name,
                    player_name=rel_player.name,
                    player_role=rel_player.role,
                    action=RosterLog.Action.RELEASE,
                    credits_delta=-refund,
                    by_admin=True,
                    note=f"Taglio mercato: {session.title}",
                )

            # 3. Update participant credits
            net_delta = bid.amount - refund
            Participant.objects.filter(pk=participant.id).update(
                spent_credits=F("spent_credits") + net_delta
            )
            credits_spent[participant.id] += net_delta

            # 4. RosterLog for assignment
            RosterLog.objects.create(
                participant=participant,
                participant_name=participant.display_name,
                player_name=bid.player.name,
                player_role=bid.player.role,
                action=RosterLog.Action.ASSIGN,
                credits_delta=bid.amount,
                by_admin=True,
                note=f"Acquisto mercato: {session.title}",
            )

            role_acquisitions[participant.id][role] += 1
            acquired_by_participant[participant.id].append(bid.player.name)

            won_results.append({
                "player_id": bid.player_id,
                "player_name": bid.player.name,
                "player_role": bid.player.role,
                "player_team": bid.player.team,
                "winner_id": participant.id,
                "winner_name": participant.display_name,
                "amount": int(bid.amount),
                "released_player": rel_player.name if rel_player else None,
                "refund": int(refund),
            })
            break

        # Mark all other lower bids for this player as LOST
        for b in player_bids:
            if b.status == MarketBid.Status.PENDING:
                b.status = MarketBid.Status.LOST
                b.note = "Offerta superata o non valida"
                b.save(update_fields=["status", "note", "updated_at"])

    # Finalize session
    summary = {
        "resolved_at": timezone.now().isoformat(),
        "won": won_results,
        "tied": tied_results,
        "total_acquisitions": len(won_results),
        "total_ties": len(tied_results),
    }
    session.results_summary = summary
    session.status = MarketSession.Status.RESOLVED
    session.save(update_fields=["status", "results_summary", "updated_at"])
    logger.info(f"Market session {session_id} ('{session.title}') resolved: {len(won_results)} acquisitions, {len(tied_results)} ties")

    return summary
