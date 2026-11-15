"""Premi delle competizioni (Coppa, Champions, Europa League, Supercoppe,
Miglior punteggio…): fantamilioni ed euro per posizione, scritti dall'admin
nella competizione (``Competition.settings["prizes"]``, passano alla stagione
dopo con la competizione) e accreditati una volta sola (``PrizeAward``).

I crediti entrano nel budget come il Decreto (``salary.add_credits``: oltre il
budget massimo si bruciano) e compaiono nel tabellone del budget fra i Premi.
"""
from django.db import transaction

from ..models import Competition, Participant, PrizeAward, RosterLog

KEY = "prizes"

# Il regolamento della lega che li ha chiesti, come esempio nei campi.
EXAMPLES = {
    Competition.Type.KNOCKOUT: ("250,150,100,50", "50"),
    Competition.Type.GROUPS_KNOCKOUT: ("250,150,100,50", "50"),
    Competition.Type.SWISS_LEAGUE: ("350,200,150", "100"),
    Competition.Type.SUPERCOPPA: ("25", ""),
    Competition.Type.TOTAL_POINTS: ("50", ""),
}


def _numbers(raw):
    out = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip().replace("€", "").replace("FM", "").strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError("I premi sono numeri interi separati da virgole, nell'ordine dei posti (es. 250,150,100,50).")
        out.append(int(part))
    return out


def table(competition):
    """{"credits": [...], "euro": [...]} della competizione (liste anche vuote)."""
    raw = (competition.settings or {}).get(KEY) or {}
    return {"credits": list(raw.get("credits") or []), "euro": list(raw.get("euro") or [])}


def set_table(competition, credits_raw, euro_raw):
    prizes = {"credits": _numbers(credits_raw), "euro": _numbers(euro_raw)}
    settings = dict(competition.settings or {})
    if prizes["credits"] or prizes["euro"]:
        settings[KEY] = prizes
    else:
        settings.pop(KEY, None)
    competition.settings = settings
    competition.save(update_fields=["settings"])
    return prizes


def places(competition):
    """Quanti posti hanno un premio."""
    t = table(competition)
    return max(len(t["credits"]), len(t["euro"]))


def suggested_order(competition):
    """Gli id delle squadre in ordine d'arrivo, per quanto si sa già: la
    classifica della competizione o, per coppe e supercoppe, vincitore,
    finalista e semifinalisti (per fantapunti nella semifinale)."""
    from . import knockout
    from .competitions import compute_competition_standings

    if competition.kind in (Competition.Type.KNOCKOUT, Competition.Type.GROUPS_KNOCKOUT,
                            Competition.Type.SUPERCOPPA):
        tiebreak = (competition.settings or {}).get("knockout_tiebreak", "fantapunti")
        rounds = knockout._rounds(competition)
        order = []
        for _stage, fixtures in reversed(rounds):
            if not all(fx.computed for fx in fixtures):
                if order:
                    break
                continue
            ties = knockout._ties(fixtures)
            if not order:
                if len(ties) != 1:
                    break
                winner = knockout._tie_winner(ties[0], tiebreak)
                legs = ties[0]
                loser = legs[0].away_id if winner == legs[0].home_id else legs[0].home_id
                order = [winner] + ([loser] if loser else [])
                continue
            losers = []
            for legs in ties:
                winner = knockout._tie_winner(legs, tiebreak)
                loser = legs[0].away_id if winner == legs[0].home_id else legs[0].home_id
                if loser and loser not in order:
                    total = sum(((fx.home_total if fx.home_id == loser else fx.away_total) or 0) for fx in legs)
                    losers.append((total, loser))
            order += [pid for _t, pid in sorted(losers, reverse=True)]
            break
        return order
    data = compute_competition_standings(competition) or {}
    order = []
    for row in data.get("standings") or []:
        team = row.get("team")
        pid = team.id if team is not None else row.get("participant_id")
        if pid and pid not in order:
            order.append(pid)
    return order


def award(competition):
    return PrizeAward.objects.filter(competition=competition).first()


@transaction.atomic
def award_prizes(competition, order):
    """Accredita i premi ai primi ``places`` di ``order`` (id delle squadre).
    Una volta sola per competizione."""
    from . import salary

    competition = Competition.objects.select_for_update(of=("self",)).select_related("season__league").get(pk=competition.pk)
    league = competition.season.league
    if PrizeAward.objects.filter(competition=competition).exists():
        raise ValueError(f"I premi di «{competition.name}» sono già stati assegnati.")
    t = table(competition)
    n = places(competition)
    if not n:
        raise ValueError("Scrivi prima i premi per posizione e salvali.")
    ids = [int(x) for x in order if str(x).strip()][:n]
    if len(ids) < n or len(set(ids)) != len(ids):
        raise ValueError(f"Scegli una squadra diversa per ognuno dei {n} posti premiati.")
    teams = {p.id: p for p in Participant.objects.select_for_update().filter(league=league, id__in=ids)}
    if len(teams) != len(ids):
        raise ValueError("Una delle squadre scelte non è di questa lega.")
    r = salary.rules(league)
    details = []
    for pos, pid in enumerate(ids, 1):
        team = teams[pid]
        credits = t["credits"][pos - 1] if pos <= len(t["credits"]) else 0
        euro = t["euro"][pos - 1] if pos <= len(t["euro"]) else 0
        credited = 0
        if credits:
            credited = int(salary.add_credits(team, credits, f"Premio {competition.name}: {pos}° posto",
                                              budget_max=r["budget_max"], money=RosterLog.Money.PRIZE))
        details.append({"participant_id": pid, "name": team.display_name, "position": pos,
                        "credits": credits, "credited": credited, "euro": euro})
    return PrizeAward.objects.create(competition=competition, league=league,
                                     season=league.season_number, details=details)
