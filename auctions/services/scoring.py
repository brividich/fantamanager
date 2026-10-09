"""Season play: scoring a giornata and resolving fixtures."""
from decimal import Decimal, InvalidOperation

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


def lineup_captains(participant, giornata=None):
    """``(captain_id, vice_id)`` of the lineup ``lineup_io`` plays."""
    saved = _saved_lineup(participant, giornata)
    return saved["captain_id"], saved["vice_id"]


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
        "captain": {"id": res["captain"]["id"], "bonus": f(res["captain"]["bonus"])} if res.get("captain") else None,
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
    captain_id, vice_id = lineup_captains(participant, giornata)
    res = scoring.score_lineup(starters, bench, giornata_perf_map(giornata), rules,
                               captain_id=captain_id, vice_id=vice_id)
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
    """Head-to-head results of the giornata's fixtures from its GiornataScores,
    with each competition's own rules: the home bonus (fantapunti added to the
    home side before the goals are counted) and the points for a win, a draw
    and a defeat. Goals typed in by hand stay as typed."""
    rules = scoring.effective_rules(giornata.season.rules if giornata.season_id else None)
    scores = {gs.participant_id: gs for gs in GiornataScore.objects.filter(giornata=giornata)}

    def side(team_id, bonus):
        gs = scores.get(team_id)
        if gs is None:
            return None, 0
        total = gs.total + bonus
        typed = (gs.breakdown or {}).get("goals_typed")
        goals = gs.goals if (typed or not bonus) else scoring.goals_from_total(total, rules)
        return total, goals

    fields = ["home_goals", "away_goals", "home_points", "away_points",
              "home_total", "away_total", "computed"]
    for fx in giornata.fixtures.select_related("competition"):
        settings = (fx.competition.settings or {}) if fx.competition_id else {}
        try:
            bonus = Decimal(str(settings.get("home_bonus") or 0))
        except (InvalidOperation, ValueError):
            bonus = Decimal("0")
        fx.home_total, fx.home_goals = side(fx.home_id, bonus)
        if fx.away_id is None:                       # bye — no match
            fx.away_total, fx.away_goals, fx.home_points, fx.away_points = None, 0, 0, 0
        else:
            fx.away_total, fx.away_goals = side(fx.away_id, Decimal("0"))
            fx.home_points, fx.away_points = scoring.fixture_outcome(fx.home_goals, fx.away_goals, settings)
        fx.computed = True
        fx.save(update_fields=fields)

    # Cups move on: a round just completed draws the next one. A round that
    # lands on a giornata already scored gets its results straight away.
    if giornata.season_id:
        from .knockout import advance_season_cups
        drawn = advance_season_cups(giornata.season)
        for later in {fx.giornata for fx in drawn if fx.giornata_id != giornata.id}:
            if GiornataScore.objects.filter(giornata=later).exists():
                _resolve_fixtures(later)


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
