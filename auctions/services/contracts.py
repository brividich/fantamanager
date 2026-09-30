"""Contratti di permanenza (regolamento, comma 4).

* All'acquisto il giocatore ha il contratto "da tirare" (``contract_years`` None):
  il manager tira il DADO CONTRATTI dall'app (facce configurabili, default
  1-1-2-2-3-3 anni); l'admin può anche registrare il risultato di un dado vero.
* Clausola (4.03): oltre una soglia di prezzo, diversa per ruolo, il contratto
  parte da 2 o 3 anni; se il dado dà di più, vale il dado.
* Portieri (2.02): i portieri della stessa squadra di Serie A condividono il
  contratto ("blocco squadra"), quello del più quotato.
* Under 21 (5.10): la scommessa dichiarata dopo l'acquisto dà 3 anni.
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

RENEWAL_FACES = (True, True, True, False, False, False)  # verde / rossa

DEFAULT_CONTRACT_RULES = {
    "faces": [1, 1, 2, 2, 3, 3],
    # ruolo: [prezzo per almeno 2 anni, prezzo per almeno 3 anni]
    "thresholds": {"P": [100, 150], "D": [100, 150], "C": [300, 500], "A": [600, 1000]},
    # Incasso massimo, per ruolo, dall'asta di un giocatore rescisso (4.02).
    "rescind_proceeds_cap": {"P": 150, "D": 150, "C": 400, "A": 600},
    "u21_years": 3,
}


def contract_rules(league):
    merged = dict(DEFAULT_CONTRACT_RULES)
    merged.update({k: v for k, v in (league.contract_rules or {}).items() if k in DEFAULT_CONTRACT_RULES})
    return merged


def contract_faces(league):
    return tuple(int(f) for f in contract_rules(league)["faces"])


def _err(message):
    return {"ok": False, "message": message}


def min_years(league, cost, role="A"):
    cost = cost or Decimal("0")
    pair = list(contract_rules(league)["thresholds"].get(role) or []) + [0, 0]
    t2, t3 = pair[0], pair[1]
    if t3 and cost >= t3:
        return 3
    if t2 and cost >= t2:
        return 2
    return 1


def gk_block(player):
    """Gli altri portieri della stessa squadra di Serie A nella stessa rosa."""
    if player.role != "P" or not player.team or player.owner_id is None:
        return []
    return list(Player.objects.filter(owner_id=player.owner_id, role="P", team=player.team).exclude(pk=player.pk))


def _block_reference(player):
    """Contratto di riferimento del blocco portieri: quello del più quotato."""
    mates = [p for p in gk_block(player) if p.contract_years is not None]
    if not mates:
        return None
    return max(mates, key=lambda p: (p.price_for(p.league) or 0, p.contract_years))


def _log(league, player, kind, *, participant=None, roll=None, years=None, manual=False,
         by_admin=False, note=""):
    ContractEvent.objects.create(
        league=league, player=player, player_name=player.name if player else "",
        participant=participant, participant_name=participant.display_name if participant else "",
        kind=kind, roll=roll, years=years, manual=manual, by_admin=by_admin,
        season=league.season_number, note=note[:200],
    )


def _owned_player(player_id, participant_id=None, by_admin=False):
    player = Player.objects.select_for_update(of=("self",)).select_related("owner", "owner__league").filter(pk=player_id).first()
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
    faces = contract_faces(league)
    ref = _block_reference(player)
    if ref is not None:
        # Blocco portieri: nessun dado, si allinea al contratto del compagno.
        player.contract_years = ref.contract_years
        player.save(update_fields=["contract_years"])
        _log(league, player, ContractEvent.Kind.SET, participant=player.owner, years=ref.contract_years,
             by_admin=by_admin, note=f"Blocco portieri {player.team}: contratto di {ref.name}")
        return {"ok": True, "face": None, "years": ref.contract_years, "floor": ref.contract_years,
                "player_name": player.name, "block": ref.name}
    if manual_face is not None:
        if not by_admin:
            return _err("Solo l'admin può inserire il risultato di un dado vero.")
        try:
            face = int(manual_face)
        except (TypeError, ValueError):
            return _err("Risultato del dado non valido.")
        if face not in set(faces):
            return _err(f"Il dado contratti dà {', '.join(map(str, sorted(set(faces))))} anni.")
    else:
        face = (rng or random.SystemRandom()).choice(faces)
    floor = min_years(league, player.cost, player.role)
    years = max(face, floor)
    player.contract_years = years
    player.save(update_fields=["contract_years"])
    _sync_block(player)
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
    top = max(contract_faces(player.owner.league)) + 1
    if not 0 <= years <= top:
        return _err(f"La durata va da 0 (scaduto) a {top} anni.")
    player.contract_years = years
    player.save(update_fields=["contract_years"])
    _sync_block(player)
    _log(player.owner.league, player, ContractEvent.Kind.SET, participant=player.owner, years=years,
         by_admin=True, note=note)
    return {"ok": True, "years": years}


def _sync_block(player):
    """Porta i portieri dello stesso blocco allo stesso contratto."""
    for mate in gk_block(player):
        if mate.contract_years != player.contract_years:
            mate.contract_years = player.contract_years
            mate.save(update_fields=["contract_years"])


@transaction.atomic
def declare_u21(player_id, *, participant_id=None, by_admin=False):
    """Scommessa Under 21 (5.10): dopo l'acquisto all'asta estiva, 3 anni fissi."""
    player, error = _owned_player(player_id, participant_id, by_admin)
    if error:
        return error
    league = player.owner.league
    if player.contract_years is not None:
        return _err("La scommessa Under 21 si dichiara subito dopo l'acquisto, prima del dado contratti.")
    from ..models import CapPhase

    phase = CapPhase.objects.filter(league=league, season=league.season_number).order_by("-started_at").first()
    if phase is not None and phase.kind != CapPhase.Kind.SUMMER:
        return _err("La scommessa Under 21 vale solo nel mercato estivo.")
    already = ContractEvent.objects.filter(
        league=league, participant=player.owner, season=league.season_number, note__startswith="Scommessa Under 21",
    ).exists()
    if already:
        return _err("Hai già dichiarato la scommessa Under 21 in questa stagione.")
    years = int(contract_rules(league)["u21_years"])
    player.contract_years = years
    player.save(update_fields=["contract_years"])
    _log(league, player, ContractEvent.Kind.SET, participant=player.owner, years=years, by_admin=by_admin,
         note="Scommessa Under 21")
    return {"ok": True, "years": years, "player_name": player.name}


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
        expired += p.contract_years == 0 and not p.abroad_list
    # 5.07: un prestito non dura oltre il contratto: chi scade torna a chi ha il
    # cartellino, che tenterà il rinnovo.
    from .loans import return_loan
    for p in Player.objects.filter(owner__league=league, contract_years=0, loan_from__isnull=False):
        return_loan(p, note="Fine contratto durante il prestito")
    # 5.09: chi è nella lista ceduti non si rinnova; a fine contratto si perde
    # e la squadra incassa il compenso della cessione.
    from .abroad import expire_listed
    listed_lost = expire_listed(league)
    ContractEvent.objects.create(
        league=league, kind=ContractEvent.Kind.SEASON, season=league.season_number, by_admin=True,
        note=f"Stagione {league.season_number}: {expired} contratti scaduti da rinnovare",
    )
    logger.info(f"New season {league.season_number} for league {league.id}: {expired} expired contracts")
    return {"ok": True, "season": league.season_number, "expired": expired, "listed_lost": listed_lost}


def is_renewals_window_open(league):
    """Verifica se la modalità di mercato rinnovi è aperta per la lega.
    I contratti sono rinnovabili solo durante una sessione di mercato di tipo 'rinnovi' (o renewals_open).
    """
    if league is None or not league.contracts_enabled:
        return False
    from ..models import MarketSession
    has_open_renewals_session = MarketSession.objects.filter(
        league=league, session_type=MarketSession.SessionType.RENEWALS, status=MarketSession.Status.OPEN
    ).exists()
    return bool(has_open_renewals_session or league.renewals_open)


def expiring(participant):
    return list(Player.objects.filter(owner=participant, contract_years=0, abroad_list=False).order_by("role", "name"))


@transaction.atomic
def declare_renewals(participant_id, renew_ids):
    """Dichiarazione dei rinnovi (4.1): chi non è nella lista è svincolato subito.

    Si fa una volta sola per stagione, prima di tirare il dado rinnovo,
    esclusivamente durante una sessione di mercato Rinnovi.
    """
    participant = Participant.objects.select_related("league").get(pk=participant_id)
    league = participant.league
    if not is_renewals_window_open(league):
        return _err("I contratti sono rinnovabili solo durante una sessione di mercato Rinnovi.")
    players = list(Player.objects.select_for_update().filter(owner=participant, contract_years=0, abroad_list=False))
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
    if not is_renewals_window_open(league):
        return _err("I contratti sono rinnovabili solo durante una sessione di mercato Rinnovi.")
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
        if face not in set(contract_faces(league)):
            return _err("Risultato del dado contratti non valido.")
    else:
        face = r.choice(contract_faces(league))
    from django.utils import timezone

    player.contract_years = face
    player.renewal_declared = None
    player.renewed_at = timezone.now()
    player.save(update_fields=["contract_years", "renewal_declared", "renewed_at"])
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
    from django.utils import timezone

    Player.objects.filter(pk=player.pk).update(acquired_at=timezone.now(), loan_from=None, loan_sessions_left=None)
    league = player.owner.league if player.owner_id else None
    if league is not None and league.contracts_enabled:
        Player.objects.filter(pk=player.pk).update(contract_years=None, renewal_declared=None)


def release_problem(player):
    """Perché la squadra non può svincolare (o vendere alla Lega) il giocatore, o ''.

    * 4.02: chi ha rinnovato non si svincola nella stessa sessione di mercato
      (la sessione estiva che segue i rinnovi, fino all'apertura dell'invernale);
    * 5.01: chi è stato comprato non si vende nella stessa sessione di mercato;
    * 5.07: chi è in prestito non è della squadra che lo ha in rosa.
    """
    from ..models import CapPhase
    from .salary import current_session_start

    if player.loan_from_id:
        return f"{player.name} è in prestito: il cartellino è di un'altra squadra."
    league = player.owner.league if player.owner_id else None
    if league is None or not league.contracts_enabled:
        return ""
    start = current_session_start(league)
    if player.acquired_at and start and player.acquired_at >= start:
        return f"{player.name} è stato acquistato in questa sessione di mercato: non si può vendere o svincolare ora."
    if player.renewed_at and not CapPhase.objects.filter(
            league=league, kind=CapPhase.Kind.WINTER, started_at__gt=player.renewed_at).exists():
        return f"{player.name} ha rinnovato: non si può svincolare nella stessa sessione di mercato."
    return ""
