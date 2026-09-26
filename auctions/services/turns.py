"""Asta a chiamata a turno (regolamento 5.02).

Le squadre chiamano un giocatore a rotazione, nell'ordine della classifica
(dalla prima). Il turno avanza da solo a ogni lotto concluso (venduto o
invenduto); chi ha la rosa già completa passa automaticamente, e la regia può
far passare il turno a mano. Chi ha slot vuoti non può passare: è una regola
del tavolo, la regia decide.
"""
from ..models import AuctionCycleResult, LeagueRanking, Participant, Player


def default_order(league):
    """Ordine di chiamata: classifica di metà stagione, poi finale dell'anno prima, poi alfabetico."""
    teams = list(Participant.objects.filter(league=league, is_active=True).order_by("display_name"))
    ids = [t.id for t in teams]
    ranking = (LeagueRanking.objects.filter(league=league, season=league.season_number,
                                            kind=LeagueRanking.Kind.MIDSEASON).first()
               or LeagueRanking.objects.filter(league=league, season=league.season_number - 1,
                                               kind=LeagueRanking.Kind.FINAL).first())
    if ranking:
        ordered = [pid for pid in ranking.order if pid in ids]
        return ordered + [pid for pid in ids if pid not in ordered]
    return ids


def _has_room(participant, league):
    if league is None or not league.slot_limits or not league.total_slots:
        return True
    owned = Player.objects.filter(owner=participant, abroad_list=False).count()
    return owned < league.total_slots


def current_turn(auction):
    """{"participant_id", "name", "position"} di chi chiama, o None se non attivo."""
    order = list(auction.turn_order or [])
    if not order:
        return None
    teams = {p.id: p for p in Participant.objects.filter(id__in=order, is_active=True)}
    order = [pid for pid in order if pid in teams]
    if not order:
        return None
    done = AuctionCycleResult.objects.filter(auction=auction, player__isnull=False).count()
    index = done + auction.turn_skips
    for step in range(len(order)):
        pid = order[(index + step) % len(order)]
        if _has_room(teams[pid], auction.league):
            return {"participant_id": pid, "name": teams[pid].display_name,
                    "position": order.index(pid) + 1, "total": len(order)}
    return None
