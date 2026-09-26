"""Prestiti (regolamento 5.07): durano un numero di sessioni d'asta e il
giocatore rientra da solo alla squadra che ha il cartellino."""
from django.db import transaction

from ..models import Player, RosterLog


def return_loan(player, note="Fine prestito"):
    lender = player.loan_from
    borrower = player.owner
    if lender is None:
        return False
    for who, verb in ((borrower, "rientra a"), (lender, "rientra da")):
        if who is None:
            continue
        other = lender if who is borrower else borrower
        RosterLog.objects.create(
            participant=who, participant_name=who.display_name, player_name=player.name,
            player_role=player.role, action=RosterLog.Action.TRADE,
            note=f"{note}: {verb} {other.display_name if other else '—'}"[:200],
        )
    player.owner = lender
    player.loan_from = None
    player.loan_sessions_left = None
    player.save(update_fields=["owner", "loan_from", "loan_sessions_left"])
    return True


@transaction.atomic
def tick(league):
    """Una nuova sessione d'asta è iniziata: scala i prestiti e fa rientrare i finiti."""
    returned = []
    for p in Player.objects.select_for_update().filter(owner__league=league, loan_from__isnull=False):
        left = max(0, (p.loan_sessions_left or 1) - 1)
        if left == 0:
            name = p.name
            if return_loan(p):
                returned.append(name)
        else:
            p.loan_sessions_left = left
            p.save(update_fields=["loan_sessions_left"])
    return returned
