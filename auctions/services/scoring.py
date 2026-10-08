"""Season play: scoring a giornata and resolving fixtures."""
from django.utils import timezone

from .. import scoring
from ..models import Giornata, GiornataScore, Participant, Player, PlayerPerformance
from .formation import _ordered_bench, _owned, _saved_lineup, lock_formations


def lineup_io(participant, giornata=None):
    """(starters, bench) as ordered ``{"id","role"}`` lists for the engine.

    A giornata already under way uses its own frozen lineup, exactly as it was
    at the lock: a player sold since still played for this team that day.
    Otherwise the saved lineup against the current roster — starters in slot
    order, bench in the manager's order, then the rest by role and name."""
    saved = _saved_lineup(participant, giornata)
    if saved["frozen"]:
        ids = [pid for pid in saved["starter_ids"] + saved["bench_ids"] if pid]
        players = {p.id: p for p in Player.objects.filter(id__in=ids)}
        starters, seen = [], set()
        for pid in saved["starter_ids"]:
            p = players.get(pid) if pid else None
            if p and pid not in seen:
                starters.append({"id": p.id, "role": p.role})
                seen.add(pid)
        bench = []
        for pid in saved["bench_ids"]:
            p = players.get(pid)
            if p and pid not in seen:
                bench.append({"id": p.id, "role": p.role})
                seen.add(pid)
        return starters, bench

    owned = _owned(participant)
    by_id = {p.id: p for p in owned}
    starters, seen = [], set()
    for pid in saved["starter_ids"]:
        p = by_id.get(pid) if pid else None      # slot vuoto = None nella lista
        if p and pid not in seen:
            starters.append({"id": p.id, "role": p.role})
            seen.add(pid)
    bench = [{"id": p.id, "role": p.role} for p in _ordered_bench(owned, list(seen), saved["bench_ids"])]
    return starters, bench


def giornata_perf_map(giornata):
    """player id → performance dict for one giornata (engine input)."""
    return {pp.player_id: pp.as_perf()
            for pp in PlayerPerformance.objects.filter(giornata=giornata)}


def _serialisable_lines(res):
    """JSON-safe breakdown (Decimals → float) for GiornataScore.breakdown."""
    def f(v):
        return float(v) if v is not None else None
    return {
        "subs": res["subs"],
        "modificatore": f(res["modificatore"]),
        "fair_play": f(res.get("fair_play")),
        "lines": [{
            "id": l["id"], "role": l["role"], "vote": f(l["vote"]),
            "fantavoto": f(l["fantavoto"]), "has_vote": l["has_vote"], "sub_in": l["sub_in"],
        } for l in res["lines"]],
    }


def score_participant_giornata(participant, giornata, *, persist=True):
    """Score one manager for a giornata from their saved lineup + the giornata's
    performances. Returns the engine result; persists a GiornataScore when asked."""
    starters, bench = lineup_io(participant, giornata=giornata)
    rules = (giornata.season.rules or {}) if giornata.season_id else {}
    res = scoring.score_lineup(starters, bench, giornata_perf_map(giornata), rules)
    if persist:
        GiornataScore.objects.update_or_create(
            giornata=giornata, participant=participant,
            defaults={
                "total": res["total"], "goals": res["goals"],
                "modificatore": res["modificatore"], "breakdown": _serialisable_lines(res),
            },
        )
    return res


def compute_giornata(giornata, mark_scored: bool = True):
    """Score every active team in the season's league for this giornata, resolve
    its head-to-head fixtures, and mark it SCORED (or LIVE if mark_scored=False). Returns the GiornataScore rows
    (highest total first)."""
    # The giornata has started: every team's lineup is frozen as it is now.
    lock_formations(giornata)
    league = giornata.season.league if giornata.season_id else None
    teams = Participant.objects.filter(is_active=True)
    teams = teams.filter(league=league) if league is not None else teams.filter(league__isnull=True)
    for p in teams:
        score_participant_giornata(p, giornata, persist=True)

    _resolve_fixtures(giornata)

    if mark_scored:
        giornata.status = Giornata.Status.SCORED
        giornata.scored_at = timezone.now()
        giornata.save(update_fields=["status", "scored_at"])
    elif giornata.status != Giornata.Status.SCORED:
        giornata.status = Giornata.Status.LIVE
        giornata.save(update_fields=["status"])

    return list(GiornataScore.objects.filter(giornata=giornata)
                .select_related("participant").order_by("-total"))


def _resolve_fixtures(giornata):
    """Head-to-head results of the giornata's fixtures from its GiornataScores."""
    goals_by_team = {gs.participant_id: gs.goals
                     for gs in GiornataScore.objects.filter(giornata=giornata)}
    for fx in giornata.fixtures.all():
        hg = goals_by_team.get(fx.home_id, 0)
        if fx.away_id is None:                       # bye — no match
            fx.home_goals, fx.home_points, fx.computed = hg, 0, True
            fx.save(update_fields=["home_goals", "home_points", "computed"])
            continue
        ag = goals_by_team.get(fx.away_id, 0)
        hp, ap = scoring.fixture_outcome(hg, ag)
        fx.home_goals, fx.away_goals, fx.home_points, fx.away_points, fx.computed = hg, ag, hp, ap, True
        fx.save(update_fields=["home_goals", "away_goals", "home_points", "away_points", "computed"])


def set_manual_scores(giornata, entries):
    """The giornata's result typed in by the league admin: each team's total
    fantapunti as another site shows it (Fantapazz exports only a picture), and
    optionally its goals — else from the league's thresholds (66 = 1 gol, then
    one every 6 points). Teams not in ``entries`` keep what they had. Resolves
    the fixtures and marks the giornata SCORED; a later votes import or live
    sync computes it again from the votes and replaces these totals.

    ``entries``: ``{participant: (total, goals_or_None)}``. Returns the rows."""
    rules = scoring.effective_rules(giornata.season.rules if giornata.season_id else None)
    lock_formations(giornata)
    for team, (total, goals) in entries.items():
        GiornataScore.objects.update_or_create(
            giornata=giornata, participant=team,
            defaults={
                "total": total,
                "goals": goals if goals is not None else scoring.goals_from_total(total, rules),
                "modificatore": 0,
                "breakdown": {"manual": True, "goals_typed": goals is not None,
                              "lines": [], "subs": 0, "modificatore": 0},
            },
        )
    _resolve_fixtures(giornata)
    giornata.status = Giornata.Status.SCORED
    giornata.scored_at = timezone.now()
    giornata.save(update_fields=["status", "scored_at"])
    return list(GiornataScore.objects.filter(giornata=giornata)
                .select_related("participant").order_by("-total"))


def recompute_season(season, summary=None):
    """Apply the league's (new) rules to every giornata already played: those
    with votes are computed again; those scored by hand keep their totals and
    get their goals again from the thresholds, unless the goals were typed in.
    Returns how many giornate changed.

    Con il voto algoritmico i voti si rifanno prima dalla riga salvata in
    ``vote_detail`` (nessuna chiamata all'API); chi non ha la riga tiene il
    voto che aveva. ``summary`` (un dict, se passato) riceve
    ``algo_regenerated``, ``algo_missing`` e ``algo_missing_giornate``."""
    from . import voto_algo
    rules = scoring.effective_rules(season.rules)
    algo_rules = voto_algo.algo_rules_for(season) if voto_algo.is_algo_source(voto_algo.vote_source_for(season)) else None
    report = {"algo_regenerated": 0, "algo_missing": 0, "algo_missing_giornate": []}
    done = 0
    for giornata in season.giornate.filter(status=Giornata.Status.SCORED).order_by("number"):
        if giornata.performances.exists():
            if algo_rules is not None:
                regenerated, missing = voto_algo.regenerate_votes(giornata, algo_rules)
                report["algo_regenerated"] += regenerated
                report["algo_missing"] += missing
                if missing:
                    report["algo_missing_giornate"].append(giornata.number)
            compute_giornata(giornata)
        else:
            for gs in GiornataScore.objects.filter(giornata=giornata):
                info = gs.breakdown or {}
                if info.get("manual") and not info.get("goals_typed"):
                    gs.goals = scoring.goals_from_total(gs.total, rules)
                    gs.save(update_fields=["goals"])
            _resolve_fixtures(giornata)
        done += 1
    if summary is not None:
        summary.update(report)
    return done
