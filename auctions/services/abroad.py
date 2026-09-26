"""Giocatori usciti dalla Serie A (regolamento 5.05, 5.06, 5.09).

Flusso:
1. All'import del listone ufficiale, chi è in una rosa ma non c'è più viene
   segnalato (``Player.left_serie_a_at``).
2. "Rileva" prova a trovare da solo il club di destinazione (API-Football) e la
   sua posizione nel ranking UEFA (scaricato da internet); l'admin conferma o
   corregge.
3. L'admin chiude il caso:
   * ceduto a un club UEFA / a un campionato extra-UEFA (ranking FIFA della
     nazione) → la squadra incassa il compenso della tabella 5.06 e perde il
     giocatore;
   * svincolato o ritirato → 0 FM;
   * messo nella **lista ceduti temporanei** (5.09, max 3): resta della squadra
     fuori dagli slot fino a fine contratto, nessun rinnovo; alla scadenza (o se
     la squadra lo svincola in sede d'asta) incassa il compenso;
   * falso allarme → la segnalazione sparisce.
In ogni caso la perdita conta per le estensioni del tetto salariale (3.02).
"""
import logging
import re
import unicodedata
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import ContractEvent, Player, UefaClubRank

logger = logging.getLogger("auctions.abroad")

# [posizione massima (None = oltre), {ruolo: FM}]
UEFA_TABLE = [
    [5, {"P": 100, "D": 100, "C": 250, "A": 500}],
    [12, {"P": 80, "D": 80, "C": 200, "A": 300}],
    [20, {"P": 50, "D": 50, "C": 150, "A": 200}],
    [30, {"P": 40, "D": 40, "C": 80, "A": 150}],
    [50, {"P": 30, "D": 30, "C": 50, "A": 80}],
    [100, {"P": 20, "D": 20, "C": 25, "A": 40}],
    [None, {"P": 10, "D": 10, "C": 15, "A": 20}],
]
FIFA_TABLE = [
    [5, {"P": 50, "D": 50, "C": 70, "A": 100}],
    [10, {"P": 40, "D": 40, "C": 50, "A": 60}],
    [20, {"P": 20, "D": 20, "C": 25, "A": 30}],
    [None, {"P": 10, "D": 10, "C": 15, "A": 20}],
]
LIST_SLOTS = 3


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|ac|as|sc|afc|ssc|club|calcio|sk|fk|if)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def compensation(role, kind, position):
    """FM dovuti per un giocatore del ``role`` finito a un club in ``position``."""
    if kind == "free":
        return Decimal("0")
    table = UEFA_TABLE if kind == "uefa" else FIFA_TABLE
    for limit, amounts in table:
        # 5.05: fuori dalle prime 100 del ranking UEFA vale la soglia minima.
        if limit is None or (position and position <= limit):
            return Decimal(amounts.get(role, 0))
    return Decimal("0")


def uefa_position(club):
    """Posizione nel ranking UEFA del club, dal ranking salvato (None se ignota)."""
    key = _norm(club)
    if not key:
        return None
    exact = UefaClubRank.objects.filter(norm_name=key).first()
    if exact:
        return exact.position
    for rank in UefaClubRank.objects.all():
        if key in rank.norm_name or rank.norm_name in key:
            return rank.position
    return None


def store_uefa_ranking(rows):
    """``rows`` = [(nome, posizione, nazione)]: sostituisce il ranking salvato."""
    UefaClubRank.objects.all().delete()
    UefaClubRank.objects.bulk_create([
        UefaClubRank(name=name, norm_name=_norm(name), position=pos, country=country or "")
        for name, pos, country in rows
    ])
    return len(rows)


def flag_missing(league, names_missing):
    """Segnala i giocatori in rosa spariti dal listone; toglie la segnalazione a chi è tornato."""
    now = timezone.now()
    owned = Player.objects.filter(owner__league=league, abroad_list=False)
    flagged = owned.filter(name__in=names_missing, left_serie_a_at__isnull=True).update(left_serie_a_at=now)
    owned.exclude(name__in=names_missing).filter(left_serie_a_at__isnull=False, left_rank_kind="").update(
        left_serie_a_at=None, left_club="", left_rank_pos=None)
    return flagged


def detect(player_id, *, finder=None):
    """Prova a riempire club di destinazione e posizione ranking (API-Football + UEFA)."""
    from ..providers.apifootball import find_destination

    player = Player.objects.get(pk=player_id)
    found = (finder or find_destination)(player.name, player.team)
    if not found:
        return {"ok": False, "message": "Destinazione non trovata automaticamente: indicala a mano."}
    player.left_club = found["club"][:120]
    pos = uefa_position(found["club"])
    if pos:
        player.left_rank_kind, player.left_rank_pos = "uefa", pos
    player.save(update_fields=["left_club", "left_rank_kind", "left_rank_pos"])
    return {"ok": True, "club": player.left_club, "position": pos}


def _lose(player, amount, note):
    from .salary import add_credits

    owner = player.owner
    league = owner.league
    ContractEvent.objects.create(
        league=league, player=player, player_name=player.name, participant=owner,
        participant_name=owner.display_name, kind=ContractEvent.Kind.LEFT,
        season=league.season_number, by_admin=True, note=note[:200],
    )
    if amount:
        add_credits(owner, amount, f"Cessione fuori dalla Serie A: {player.name}")
    player.owner = None
    player.cost = Decimal("0")
    player.contract_years = None
    player.renewal_declared = None
    player.abroad_list = False
    player.abroad_compensation = None
    player.left_serie_a_at = None
    player.save()
    return amount


@transaction.atomic
def resolve(player_id, outcome, *, club="", position=None):
    """Chiude una segnalazione. ``outcome``: uefa / fifa / free / list / dismiss."""
    player = Player.objects.select_for_update().select_related("owner", "owner__league").get(pk=player_id)
    if player.owner_id is None:
        return {"ok": False, "message": "Il giocatore non è in nessuna rosa."}
    if outcome == "dismiss":
        player.left_serie_a_at = None
        player.left_club, player.left_rank_kind, player.left_rank_pos = "", "", None
        player.save(update_fields=["left_serie_a_at", "left_club", "left_rank_kind", "left_rank_pos"])
        return {"ok": True, "amount": 0, "outcome": outcome}

    kind = player.left_rank_kind or "uefa"
    if outcome in ("uefa", "fifa", "free"):
        kind = outcome
    try:
        pos = int(position) if position not in (None, "") else player.left_rank_pos
    except (TypeError, ValueError):
        return {"ok": False, "message": "Posizione nel ranking non valida."}
    if kind == "uefa" and not pos and club:
        pos = uefa_position(club)
    club = club or player.left_club
    amount = compensation(player.role, kind, pos)
    label = {"uefa": f"ranking UEFA {pos or '101+'}°", "fifa": f"ranking FIFA {pos or '21+'}°",
             "free": "svincolato o ritirato"}[kind]

    if outcome == "list":
        listed = Player.objects.filter(owner=player.owner, abroad_list=True).count()
        if listed >= LIST_SLOTS:
            return {"ok": False, "message": f"La lista ceduti temporanei è piena ({LIST_SLOTS} posti)."}
        player.abroad_list = True
        player.abroad_compensation = amount
        player.left_club, player.left_rank_kind, player.left_rank_pos = club, kind, pos
        player.left_serie_a_at = None
        player.save()
        return {"ok": True, "amount": 0, "outcome": outcome, "deferred": int(amount)}

    _lose(player, amount, f"{club or 'destinazione non indicata'} · {label}")
    return {"ok": True, "amount": int(amount), "outcome": kind}


@transaction.atomic
def release_from_list(player_id, *, participant_id=None):
    """5.09: svincolo dalla lista ceduti in sede d'asta → incassa il compenso."""
    player = Player.objects.select_for_update().select_related("owner", "owner__league").get(pk=player_id)
    if not player.abroad_list or player.owner_id is None:
        return {"ok": False, "message": "Il giocatore non è nella lista ceduti."}
    if participant_id is not None and int(participant_id) != player.owner_id:
        return {"ok": False, "message": "Non è un tuo giocatore."}
    amount = player.abroad_compensation or Decimal("0")
    _lose(player, amount, f"Svincolato dalla lista ceduti ({player.left_club or 'estero'})")
    return {"ok": True, "amount": int(amount)}


def expire_listed(league):
    """A fine contratto i giocatori in lista si perdono con il loro compenso (5.09)."""
    done = []
    for p in Player.objects.filter(owner__league=league, abroad_list=True, contract_years=0).select_related("owner"):
        amount = p.abroad_compensation or Decimal("0")
        name = p.name
        _lose(p, amount, f"Fine contratto in lista ceduti ({p.left_club or 'estero'})")
        done.append((name, int(amount)))
    return done
