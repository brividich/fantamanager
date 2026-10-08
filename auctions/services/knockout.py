"""Knockout brackets that move on by themselves.

The cup is drawn one round at a time (``setup_knockout_competition`` draws
the first): once every match of the latest round has a result, the winners
meet in the next round on the following giornata (two giornate when the cup
is andata e ritorno), until the final names the winner
(``settings["winner_id"]``). A «Gironi + fase finale» cup with an
``end_giornata`` draws its semifinals from the group tables (1ª A – 2ª B,
1ª B – 2ª A) once the groups are over.

Tie on goals: ``settings["knockout_tiebreak"]`` — "fantapunti" (default:
more fantapunti over the tie) or "casa" (the team at home in the first leg,
the better seed, goes through).

Runs at the end of every scoring of a giornata; correcting an earlier result
redraws a next round that hasn't been played yet.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db import transaction

from ..models import Competition, Fixture

LEG_SUFFIXES = (" (Andata)", " (Ritorno)")
STAGE_NAMES = {2: "Finale", 4: "Semifinale", 8: "Quarti di finale", 16: "Ottavi di finale",
               32: "Sedicesimi di finale"}
KNOCKOUT_KINDS = (Competition.Type.KNOCKOUT, Competition.Type.GROUPS_KNOCKOUT)


def _base_stage(stage):
    stage = stage or ""
    for suffix in LEG_SUFFIXES:
        if stage.endswith(suffix):
            return stage[: -len(suffix)]
    return stage


def _rounds(competition):
    """The bracket's rounds in order: ``[(stage, [fixtures])]`` (group games left out)."""
    rounds = OrderedDict()
    for fx in (competition.fixtures.select_related("giornata")
               .exclude(stage__startswith="Girone").order_by("giornata__number", "id")):
        rounds.setdefault(_base_stage(fx.stage), []).append(fx)
    return list(rounds.items())


def _ties(fixtures):
    """Fixtures of one round grouped by pairing (one or two legs), in draw order."""
    ties = OrderedDict()
    for fx in fixtures:
        ties.setdefault(frozenset((fx.home_id, fx.away_id)), []).append(fx)
    return list(ties.values())


def _tie_winner(legs, tiebreak):
    first = legs[0]
    if first.away_id is None:                        # a bye
        return first.home_id
    goals = {first.home_id: 0, first.away_id: 0}
    totals = {first.home_id: Decimal("0"), first.away_id: Decimal("0")}
    for fx in legs:
        goals[fx.home_id] += fx.home_goals
        goals[fx.away_id] += fx.away_goals
        totals[fx.home_id] += fx.home_total or 0
        totals[fx.away_id] += fx.away_total or 0
    home, away = first.home_id, first.away_id
    if goals[home] != goals[away]:
        return home if goals[home] > goals[away] else away
    if tiebreak != "casa" and totals[home] != totals[away]:
        return home if totals[home] > totals[away] else away
    return home


def _winners(fixtures, tiebreak):
    return [_tie_winner(legs, tiebreak) for legs in _ties(fixtures)]


def _draw(competition, pairs, after_number):
    """Fixtures of a new round on the giornata(e) after ``after_number``."""
    two_legged = bool((competition.settings or {}).get("two_legged"))
    giornate = list(competition.season.giornate.filter(number__gt=after_number).order_by("number")[:2])
    if not giornate or (two_legged and len(giornate) < 2):
        return []
    stage = STAGE_NAMES.get(len(pairs) * 2, f"Turno a {len(pairs) * 2}")
    created = []
    for home, away in pairs:
        created.append(Fixture.objects.create(
            giornata=giornate[0], competition=competition, home_id=home, away_id=away,
            stage=stage + (" (Andata)" if two_legged else "")))
        if two_legged:
            created.append(Fixture.objects.create(
                giornata=giornate[1], competition=competition, home_id=away, away_id=home,
                stage=f"{stage} (Ritorno)"))
    return created


def _pairs(winners):
    """Best against worst, in draw order: 1st winner meets the last."""
    return [(winners[i], winners[len(winners) - 1 - i]) for i in range(len(winners) // 2)]


def _group_semifinals(competition):
    from .competitions import _compute_groups_standings

    groups = competition.fixtures.filter(stage__startswith="Girone")
    end = (competition.settings or {}).get("end_giornata")
    if not end or not groups.exists() or groups.filter(computed=False).exists():
        return None
    tables = _compute_groups_standings(competition)["groups"]
    a, b = tables.get("Girone A", []), tables.get("Girone B", [])
    if len(a) < 2 or len(b) < 2:
        return None
    last = max(groups.values_list("giornata__number", flat=True))
    return [(a[0]["team"].id, b[1]["team"].id), (b[0]["team"].id, a[1]["team"].id)], last


@transaction.atomic
def advance_knockout(competition):
    """Draw the next round if the latest one is over; returns the new fixtures."""
    if competition.kind not in KNOCKOUT_KINDS:
        return []
    settings = competition.settings or {}
    tiebreak = settings.get("knockout_tiebreak", "fantapunti")
    rounds = _rounds(competition)

    if not rounds:
        if competition.kind == Competition.Type.GROUPS_KNOCKOUT:
            semis = _group_semifinals(competition)
            if semis:
                return _draw(competition, *semis)
        return []

    stage, last = rounds[-1]
    # The latest round isn't played yet: make sure it still follows from the
    # one before (an earlier result may have been corrected since the draw).
    if not any(fx.computed for fx in last) and len(rounds) > 1:
        _prev_stage, prev = rounds[-2]
        expected = _pairs(_winners(prev, tiebreak))
        drawn = [(legs[0].home_id, legs[0].away_id) for legs in _ties(last)]
        if drawn != expected:
            Fixture.objects.filter(pk__in=[fx.pk for fx in last]).delete()
            return _draw(competition, expected, max(fx.giornata.number for fx in prev))
        return []
    if not all(fx.computed for fx in last):
        return []

    winners = _winners(last, tiebreak)
    if len(winners) == 1:
        if settings.get("winner_id") != winners[0]:
            competition.settings = {**settings, "winner_id": winners[0]}
            competition.save(update_fields=["settings"])
        return []
    return _draw(competition, _pairs(winners), max(fx.giornata.number for fx in last))


def advance_season_cups(season):
    """Every cup of the season, after a giornata got its results."""
    created = []
    for competition in season.competitions.filter(kind__in=KNOCKOUT_KINDS, is_active=True):
        created += advance_knockout(competition)
    return created
