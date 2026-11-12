"""Eliminare una squadra della lega.

Serve per la squadra creata per sbaglio (una in più nel wizard, un doppione):
sparisce con le cose che non hanno ancora contato, i suoi giocatori tornano
svincolati. Una squadra che ha già giocato (punteggi, partite calcolate,
offerte d'asta, buste, scambi) resta: cancellarla toglierebbe partite e
risultati anche agli avversari e allo storico della lega.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from ..models import (Bid, Fixture, GiornataScore, MarketBid, Participant, Player, RosterLog,
                      SealedBid, Trade)
from . import sala


def team_removal(team):
    """Che cosa vuol dire eliminare ``team``: ``blocks`` (perché non si può,
    vuoto se si può), ``roster`` (giocatori che tornano svincolati) e
    ``fixtures`` (partite del calendario ancora da giocare che spariscono)."""
    blocks = []
    played = Fixture.objects.filter(Q(home=team) | Q(away=team), computed=True).count()
    scores = GiornataScore.objects.filter(participant=team).count()
    if played or scores:
        blocks.append(f"ha già giocato ({max(played, scores)} giornate con il punteggio)")
    bids = Bid.objects.filter(participant=team).count() + SealedBid.objects.filter(participant=team).count()
    if bids:
        blocks.append(f"ha fatto {bids} offerte in asta")
    market = MarketBid.objects.filter(participant=team).count()
    if market:
        blocks.append(f"ha {market} buste nei mercati")
    trades = Trade.objects.filter(Q(proposer=team) | Q(receiver=team)).count()
    if trades:
        blocks.append(f"ha {trades} scambi")
    return {
        "blocks": blocks,
        "roster": Player.objects.filter(owner=team).count(),
        "fixtures": Fixture.objects.filter(Q(home=team) | Q(away=team), computed=False).count(),
    }


class TeamNotRemovable(Exception):
    """Perché la squadra non si elimina (il messaggio è per l'admin)."""


@transaction.atomic
def delete_team(team):
    """Elimina ``team`` se ``team_removal`` lo permette; i giocatori tornano
    svincolati (registro movimenti «Svincolo (admin)»). Ritorna
    ``team_removal`` com'era prima, per dire all'admin che cosa è successo."""
    team = Participant.objects.select_for_update().get(pk=team.pk)
    sala.ensure_unlocked(team.league_id)
    info = team_removal(team)
    if info["blocks"]:
        raise TeamNotRemovable(
            f"«{team.display_name}» non si può eliminare: " + "; ".join(info["blocks"]) + ". "
            "Quello che ha fatto resta nello storico della lega. Se non gioca più, togli l'accesso dalla colonna "
            "Account e svincola i suoi giocatori dalla scheda Rose.")
    for player in Player.objects.select_for_update().filter(owner=team):
        RosterLog.objects.create(
            participant=team, participant_name=team.display_name,
            player_name=player.name, player_role=player.role,
            action=RosterLog.Action.ADMIN_RELEASE, credits_delta=Decimal("0"), by_admin=True,
            note="Squadra eliminata: torna svincolato",
        )
        player.owner = None
        player.cost = Decimal("0")
        player.save(update_fields=["owner", "cost"])
    team.delete()
    return info
