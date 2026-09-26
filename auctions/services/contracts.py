"""Contratti di permanenza (regolamento 4).

* All'acquisto il giocatore ha il contratto "da tirare" (``contract_years`` None):
  il manager tira il DADO CONTRATTI (facce 1, 2, 2, 3, 3, 4 anni) dall'app;
  l'admin può anche registrare il risultato di un dado vero.
* Soglie clausola (4.3): chi è stato pagato almeno ``contract_min2_price``
  (``contract_min3_price``) ha almeno 2 (3) anni: se il dado dà di più, vale il dado.
* Nuova stagione: ogni contratto scala di un anno; chi arriva a 0 è scaduto e
  si apre la finestra dei rinnovi.
* Rinnovi (4.1): ogni squadra dichiara prima chi vuole rinnovare; chi non viene
  rinnovato è svincolato subito. Per gli altri si tira il DADO RINNOVO (3 verdi,
  3 rosse): verde = rinnovo col dado contratti, rossa = rescissione (svincolato;
  la squadra non può ricomprarlo e l'incasso della sua asta va a lei).
"""
import logging
import random
from decimal import Decimal

from django.db import transaction

from ..models import ContractEvent, League, Participant, Player, RosterLog

logger = logging.getLogger("auctions.contracts")

CONTRACT_FACES = (1, 2, 2, 3, 3, 4)
RENEWAL_FACES = (True, True, True, False, False, False)  # verde / rossa


def _err(message):
    return {"ok": False, "message": message}


def min_years(league, cost):
    cost = cost or Decimal("0")
    if league.contract_min3_price and cost >= league.contract_min3_price:
        return 3
    if league.contract_min2_price and cost >= league.contract_min2_price:
        return 2
    return 1


def _log(league, player, kind, *, participant=None, roll=None, years=None, manual=False,
         by_admin=False, note=""):
    ContractEvent.objects.create(
        league=league, player=player, player_name=player.name if player else "",
        participant=participant, participant_name=participant.display_name if participant else "",
        kind=kind, roll=roll, years=years, manual=manual, by_admin=by_admin,
        season=league.season_number, note=note[:200],
    )


def _owned_player(player_id, participant_id=None, by_admin=False):
    player = Player.objects.select_for_update().select_related("owner", "owner__league").filter(pk=player_id).first()
    if player is None or player.owner_id is None:
        return None, _err("Giocatore non trovato in nessuna rosa.")
    if not by_admin and (participant_id is None or int(participant_id) != player.owner_id):
        return None, _err("Puoi gestire solo i contratti dei tuoi giocatori.")
    league = player.owner.league
    if league is None or not league.contracts_enabled:
        return None, _err("I contratti non sono attivi in questa lega.")
    return player, None


@transaction.atomic
def roll_contract(player_id, *, participant_id=None, by_admin=False, manual_face=None, rng=None):
    """Dado contratti per un giocatore appena acquistato."""
    player, error = _owned_player(player_id, participant_id, by_admin)
    if error:
        return error
    if player.contract_years is not None:
        return _err(f"{player.name} ha già un contratto ({player.contract_years} anni).")
    league = player.owner.league
    if manual_face is not None:
        if not by_admin:
            return _err("Solo l'admin può inserire il risultato di un dado vero.")
        try:
            face = int(manual_face)
        except (TypeError, ValueError):
            return _err("Risultato del dado non valido.")
        if face not in set(CONTRACT_FACES):
            return _err("Il dado contratti dà 1, 2, 3 o 4 anni.")
    else:
        face = (rng or random.SystemRandom()).choice(CONTRACT_FACES)
    floor = min_years(league, player.cost)
    years = max(face, floor)
    player.contract_years = years
    player.save(update_fields=["contract_years"])
    note = f"minimo {floor} anni per la clausola ({player.cost:.0f} FM)" if floor > face else ""
    _log(league, player, ContractEvent.Kind.CONTRACT, participant=player.owner, roll=face, years=years,
         manual=manual_face is not None, by_admin=by_admin, note=note)
    return {"ok": True, "face": face, "years": years, "floor": floor, "player_name": player.name}


@transaction.atomic
def set_contract(player_id, years, *, note=""):
    """Admin: imposta a mano la durata (es. rose importate da un'altra stagione)."""
    player, error = _owned_player(player_id, by_admin=True)
    if error:
        return error
    try:
        years = int(years)
    except (TypeError, ValueError):
        return _err("Durata non valida.")
    if not 0 <= years <= 4:
        return _err("La durata va da 0 (scaduto) a 4 anni.")
    player.contract_years = years
    player.save(update_fields=["contract_years"])
    _log(player.owner.league, player, ContractEvent.Kind.SET, participant=player.owner, years=years,
         by_admin=True, note=note)
    return {"ok": True, "years": years}


def _release(player, note):
    owner = player.owner
    RosterLog.objects.create(
        participant=owner, participant_name=owner.display_name,
        player_name=player.name, player_role=player.role,
        action=RosterLog.Action.RELEASE, credits_delta=Decimal("0"), by_admin=False, note=note,
    )
    player.owner = None
    player.cost = Decimal("0")
    player.contract_years = None
    player.renewal_declared = None


@transaction.atomic
def new_season(league_id):
    """Scala di un anno tutti i contratti della lega e apre i rinnovi."""
    league = League.objects.select_for_update().get(pk=league_id)
    if not league.contracts_enabled:
        return _err("I contratti non sono attivi in questa lega.")
    league.season_number += 1
    league.renewals_open = True
    league.save(update_fields=["season_number", "renewals_open", "updated_at"])
    players = Player.objects.select_for_update().filter(owner__league=league, contract_years__isnull=False)
    expired = 0
    for p in players:
        p.contract_years = max(0, p.contract_years - 1)
        p.renewal_declared = None
        p.save(update_fields=["contract_years", "renewal_declared"])
        expired += p.contract_years == 0
    ContractEvent.objects.create(
        league=league, kind=ContractEvent.Kind.SEASON, season=league.season_number, by_admin=True,
        note=f"Stagione {league.season_number}: {expired} contratti scaduti da rinnovare",
    )
    logger.info(f"New season {league.season_number} for league {league.id}: {expired} expired contracts")
    return {"ok": True, "season": league.season_number, "expired": expired}


def expiring(participant):
    return list(Player.objects.filter(owner=participant, contract_years=0).order_by("role", "name"))


@transaction.atomic
def declare_renewals(participant_id, renew_ids):
    """Dichiarazione dei rinnovi (4.1): chi non è nella lista è svincolato subito.

    Si fa una volta sola per stagione, prima di tirare il dado rinnovo.
    """
    participant = Participant.objects.select_related("league").get(pk=participant_id)
    league = participant.league
    if league is None or not league.contracts_enabled or not league.renewals_open:
        return _err("La finestra dei rinnovi non è aperta.")
    players = list(Player.objects.select_for_update().filter(owner=participant, contract_years=0))
    if not players:
        return _err("Non hai contratti scaduti da rinnovare.")
    if any(p.renewal_declared is not None for p in players):
        return _err("Hai già dichiarato i rinnovi di questa stagione.")
    keep = {int(x) for x in renew_ids if str(x).isdigit()}
    released = []
    for p in players:
        if p.id in keep:
            p.renewal_declared = True
            p.save(update_fields=["renewal_declared"])
        else:
            _log(league, p, ContractEvent.Kind.NOT_RENEWED, participant=participant, note="Non dichiarato per il rinnovo")
            released.append(p.name)
            _release(p, "Contratto scaduto, non rinnovato")
            p.save(update_fields=["owner", "cost", "contract_years", "renewal_declared"])
    return {"ok": True, "renewing": len(players) - len(released), "released": released}


@transaction.atomic
def roll_renewal(player_id, *, participant_id=None, by_admin=False, manual_green=None,
                 manual_face=None, rng=None):
    """Dado rinnovo (e, se verde, dado contratti) per un giocatore dichiarato."""
    player, error = _owned_player(player_id, participant_id, by_admin)
    if error:
        return error
    league = player.owner.league
    if not league.renewals_open:
        return _err("La finestra dei rinnovi non è aperta.")
    if player.contract_years != 0 or player.renewal_declared is not True:
        return _err(f"{player.name} non è tra i rinnovi dichiarati.")
    manual = manual_green is not None
    if manual and not by_admin:
        return _err("Solo l'admin può inserire il risultato di un dado vero.")
    r = rng or random.SystemRandom()
    green = bool(manual_green) if manual else r.choice(RENEWAL_FACES)
    owner = player.owner
    if not green:
        _log(league, player, ContractEvent.Kind.RESCINDED, participant=owner, roll=0, manual=manual,
             by_admin=by_admin, note="Dado rinnovo rosso: rescissione")
        name = player.name
        _release(player, "Rescissione (dado rinnovo rosso)")
        player.rescinded_from = owner
        player.save(update_fields=["owner", "cost", "contract_years", "renewal_declared", "rescinded_from"])
        return {"ok": True, "green": False, "player_name": name}

    if manual_face is not None:
        if not by_admin:
            return _err("Solo l'admin può inserire il risultato di un dado vero.")
        face = int(manual_face)
        if face not in set(CONTRACT_FACES):
            return _err("Il dado contratti dà 1, 2, 3 o 4 anni.")
    else:
        face = r.choice(CONTRACT_FACES)
    player.contract_years = face
    player.renewal_declared = None
    player.save(update_fields=["contract_years", "renewal_declared"])
    _log(league, player, ContractEvent.Kind.RENEWED, participant=owner, roll=face, years=face,
         manual=manual or manual_face is not None, by_admin=by_admin)
    return {"ok": True, "green": True, "face": face, "years": face, "player_name": player.name}


@transaction.atomic
def close_renewals(league_id):
    league = League.objects.select_for_update().get(pk=league_id)
    league.renewals_open = False
    league.save(update_fields=["renewals_open", "updated_at"])
    return {"ok": True}


def on_player_acquired(player):
    """Hook: un giocatore è appena passato a una squadra per acquisto (asta,
    buste, assegnazione admin): il contratto è da tirare."""
    league = player.owner.league if player.owner_id else None
    if league is not None and league.contracts_enabled:
        Player.objects.filter(pk=player.pk).update(contract_years=None, renewal_declared=None)
