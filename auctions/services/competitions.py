"""Service for multi-format competition management:
- Classic Round Robin 1vs1 (Calendario a girone all'italiana)
- Total Points Gran Premio (Formula 1)
- Knockout Cup Bracket (Tabellone a eliminazione diretta)
- Groups + Knockout (Fase a gironi + tabellone finale)
- Phase Tournaments (Apertura / Clausura)
- Supercoppa di Lega (Gara secca)
"""
import math
import random
from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from ..models import Competition, Fixture, Giornata, GiornataScore, Participant, Season
from .voti import compute_coppa_italia_battle_royale


def generate_round_robin_schedule(team_ids):
    """Generate round-robin pairings using the circle method (Berger algorithm).

    Returns a list of rounds, where each round is a list of (home_id, away_id) tuples.
    If team_ids length is odd, a dummy None is added for byes.
    """
    teams = list(team_ids)
    if len(teams) % 2 != 0:
        teams.append(None)

    n = len(teams)
    rounds_count = n - 1
    schedule = []

    for round_idx in range(rounds_count):
        round_matches = []
        for i in range(n // 2):
            t1 = teams[i]
            t2 = teams[n - 1 - i]
            if t1 is not None and t2 is not None:
                # Alternate home/away based on round index to balance venues
                if round_idx % 2 == 1:
                    round_matches.append((t2, t1))
                else:
                    round_matches.append((t1, t2))
            elif t1 is not None:
                round_matches.append((t1, None))
            elif t2 is not None:
                round_matches.append((t2, None))

        schedule.append(round_matches)
        # Rotate teams keeping first fixed
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]

    return schedule


@transaction.atomic
def setup_round_robin_competition(competition, team_ids=None, start_giornata=1, end_giornata=None):
    """Generate calendar fixtures for a round-robin or season split competition."""
    season = competition.season
    if not team_ids:
        team_ids = list(season.league.participants.values_list("id", flat=True)) if season.league else []

    if len(team_ids) < 2:
        return []

    giornate_qs = season.giornate.filter(number__gte=start_giornata)
    if end_giornata:
        giornate_qs = giornate_qs.filter(number__lte=end_giornata)
    giornate = list(giornate_qs.order_by("number"))

    if not giornate:
        return []

    base_schedule = generate_round_robin_schedule(team_ids)
    cycle_len = len(base_schedule)

    # Clean existing fixtures for this competition
    competition.fixtures.all().delete()

    created_fixtures = []
    for g_idx, g in enumerate(giornate):
        round_matches = base_schedule[g_idx % cycle_len]
        cycle_number = (g_idx // cycle_len) + 1
        # In alternate cycles (e.g. girone di ritorno), invert home and away
        invert = (cycle_number % 2 == 0)

        for match in round_matches:
            h_id, a_id = match
            if invert and a_id is not None:
                h_id, a_id = a_id, h_id

            fix = Fixture.objects.create(
                giornata=g,
                competition=competition,
                stage=f"Turno {g_idx + 1}",
                home_id=h_id,
                away_id=a_id,
            )
            created_fixtures.append(fix)

    return created_fixtures


@transaction.atomic
def setup_knockout_competition(competition, team_ids=None, start_giornata=1, two_legged=False):
    """Generate a single or two-legged knockout cup tournament bracket."""
    season = competition.season
    if not team_ids:
        team_ids = list(season.league.participants.values_list("id", flat=True)) if season.league else []

    num_teams = len(team_ids)
    if num_teams < 2:
        return []

    # Round up to next power of 2
    bracket_size = 2 ** math.ceil(math.log2(num_teams))
    seeds = list(team_ids)
    while len(seeds) < bracket_size:
        seeds.append(None)  # Byes for top seeds

    competition.fixtures.all().delete()

    # Determine rounds: e.g. Quarti (8), Semifinali (4), Finale (2)
    stage_names = {2: "Finale", 4: "Semifinale", 8: "Quarti di finale", 16: "Ottavi di finale"}

    giornate = list(season.giornate.filter(number__gte=start_giornata).order_by("number"))
    g_pointer = 0

    created_fixtures = []
    current_teams = seeds
    round_count = bracket_size

    # Set up first round
    if g_pointer < len(giornate):
        g = giornate[g_pointer]
        stage_title = stage_names.get(round_count, f"Turno {round_count}")

        pairings = []
        for i in range(round_count // 2):
            h = current_teams[i]
            a = current_teams[round_count - 1 - i]
            if h is not None:
                pairings.append((h, a))

        for h_id, a_id in pairings:
            fix = Fixture.objects.create(
                giornata=g,
                competition=competition,
                stage=f"{stage_title}" + (" (Andata)" if two_legged else ""),
                home_id=h_id,
                away_id=a_id,
            )
            created_fixtures.append(fix)

            if two_legged and a_id is not None and (g_pointer + 1) < len(giornate):
                g_ret = giornate[g_pointer + 1]
                fix_ret = Fixture.objects.create(
                    giornata=g_ret,
                    competition=competition,
                    stage=f"{stage_title} (Ritorno)",
                    home_id=a_id,
                    away_id=h_id,
                )
                created_fixtures.append(fix_ret)

    return created_fixtures


@transaction.atomic
def setup_groups_knockout_competition(competition, team_ids=None, start_giornata=1, end_giornata=None):
    """Generate group stage fixtures for groups + playoff cup."""
    season = competition.season
    if not team_ids:
        team_ids = list(season.league.participants.values_list("id", flat=True)) if season.league else []

    if len(team_ids) < 4:
        return setup_round_robin_competition(competition, team_ids=team_ids, start_giornata=start_giornata, end_giornata=end_giornata)

    mid = len(team_ids) // 2
    group_a = team_ids[:mid]
    group_b = team_ids[mid:]

    competition.fixtures.all().delete()

    giornate_qs = season.giornate.filter(number__gte=start_giornata)
    if end_giornata:
        giornate_qs = giornate_qs.filter(number__lte=end_giornata)
    giornate = list(giornate_qs.order_by("number"))
    if not giornate:
        return []

    sched_a = generate_round_robin_schedule(group_a)
    sched_b = generate_round_robin_schedule(group_b)
    cycle_a = len(sched_a)
    cycle_b = len(sched_b)

    created_fixtures = []
    for g_idx, g in enumerate(giornate):
        # Girone A
        matches_a = sched_a[g_idx % cycle_a]
        inv_a = ((g_idx // cycle_a) + 1) % 2 == 0
        for h, a in matches_a:
            if inv_a and a is not None:
                h, a = a, h
            created_fixtures.append(Fixture.objects.create(
                giornata=g, competition=competition,
                stage=f"Girone A · Turno {g_idx + 1}", home_id=h, away_id=a,
            ))
        # Girone B
        matches_b = sched_b[g_idx % cycle_b]
        inv_b = ((g_idx // cycle_b) + 1) % 2 == 0
        for h, a in matches_b:
            if inv_b and a is not None:
                h, a = a, h
            created_fixtures.append(Fixture.objects.create(
                giornata=g, competition=competition,
                stage=f"Girone B · Turno {g_idx + 1}", home_id=h, away_id=a,
            ))

    return created_fixtures


@transaction.atomic
def setup_supercoppa(competition, home_id, away_id, giornata_num=1):
    """Set up a single head-to-head match for Supercoppa."""
    season = competition.season
    giornata = season.giornate.filter(number=giornata_num).first()
    if not giornata:
        giornata = Giornata.objects.create(season=season, number=giornata_num)

    competition.fixtures.all().delete()
    fix = Fixture.objects.create(
        giornata=giornata,
        competition=competition,
        stage="Finale Secca",
        home_id=home_id,
        away_id=away_id,
    )
    return [fix]


def compute_competition_standings(competition):
    """Compute and return standings/results data for any competition type."""
    kind = competition.kind
    season = competition.season

    if kind in (Competition.Type.ROUND_ROBIN, Competition.Type.SEASON_SPLIT):
        return _compute_round_robin_standings(competition)
    elif kind == Competition.Type.TOTAL_POINTS:
        return _compute_total_points_standings(competition)
    elif kind == Competition.Type.BATTLE_ROYALE:
        return _compute_battle_royale_standings(competition)
    elif kind in (Competition.Type.KNOCKOUT, Competition.Type.SUPERCOPPA):
        return _compute_bracket_standings(competition)
    else:
        return _compute_round_robin_standings(competition)


def _compute_round_robin_standings(competition):
    """Standings table for classic head-to-head fixtures."""
    fixtures = competition.fixtures.select_related("home", "away", "giornata")
    teams = defaultdict(lambda: {
        "team": None, "played": 0, "won": 0, "drawn": 0, "lost": 0,
        "goals_for": 0, "goals_against": 0, "goal_diff": 0, "points": 0,
        "total_fantapunti": Decimal("0")
    })

    # Initialize all league participants
    if competition.season.league:
        for p in competition.season.league.participants.all():
            teams[p.id]["team"] = p

    # Collect fantapunti from matchdays associated with this competition
    giornate_ids = list(fixtures.values_list("giornata_id", flat=True).distinct())
    scores = GiornataScore.objects.filter(giornata_id__in=giornate_ids).select_related("participant")
    for sc in scores:
        if sc.participant_id in teams:
            teams[sc.participant_id]["total_fantapunti"] += sc.total

    for f in fixtures:
        if not f.computed or not f.away:
            continue
        h = teams[f.home_id]
        a = teams[f.away_id]
        h["team"] = f.home
        a["team"] = f.away
        h["played"] += 1
        a["played"] += 1
        h["goals_for"] += f.home_goals
        h["goals_against"] += f.away_goals
        a["goals_for"] += f.away_goals
        a["goals_against"] += f.home_goals
        h["points"] += f.home_points
        a["points"] += f.away_points

        if f.home_points > f.away_points:
            h["won"] += 1
            a["lost"] += 1
        elif f.home_points < f.away_points:
            a["won"] += 1
            h["lost"] += 1
        else:
            h["drawn"] += 1
            a["drawn"] += 1

    table = []
    for tid, data in teams.items():
        if data["team"]:
            data["goal_diff"] = data["goals_for"] - data["goals_against"]
            table.append(data)

    table.sort(key=lambda x: (x["points"], x["goal_diff"], x["goals_for"], x["total_fantapunti"]), reverse=True)
    return {"kind": "table", "standings": table}


def _compute_total_points_standings(competition):
    """Standings sorted purely by total fantapunti sum (Gran Premio / F1)."""
    season = competition.season
    settings = competition.settings or {}
    start_g = settings.get("start_giornata", 1)
    end_g = settings.get("end_giornata", season.matchdays)

    scores = GiornataScore.objects.filter(
        giornata__season=season,
        giornata__number__gte=start_g,
        giornata__number__lte=end_g,
    ).select_related("participant", "giornata")

    totals = defaultdict(lambda: {"team": None, "total": Decimal("0"), "giornate_played": 0, "scores_list": []})
    if season.league:
        for p in season.league.participants.all():
            totals[p.id]["team"] = p

    for sc in scores:
        t = totals[sc.participant_id]
        t["team"] = sc.participant
        t["total"] += sc.total
        t["giornate_played"] += 1
        t["scores_list"].append({"giornata": sc.giornata.number, "total": sc.total})

    table = [d for d in totals.values() if d["team"]]
    table.sort(key=lambda x: x["total"], reverse=True)
    return {"kind": "points", "standings": table}


def _compute_battle_royale_standings(competition):
    """Aggregate standings for Battle Royale (cumulative across all matchdays)."""
    season = competition.season
    cum_standings = defaultdict(lambda: {
        "team": None, "battle_points": 0, "won": 0, "drawn": 0, "lost": 0,
        "total_fantapunti": Decimal("0")
    })

    if season.league:
        for p in season.league.participants.all():
            cum_standings[p.id]["team"] = p

    for g in season.giornate.filter(status=Giornata.Status.SCORED).order_by("number"):
        day_results = compute_coppa_italia_battle_royale(g)
        for res in day_results:
            pid = res["participant_id"]
            if pid in cum_standings:
                c = cum_standings[pid]
                c["battle_points"] += res["battle_points"]
                c["total_fantapunti"] += res["fantapunti"]
                rec = res.get("record", "0-0-0").split("-")
                if len(rec) == 3:
                    c["won"] += int(rec[0])
                    c["drawn"] += int(rec[1])
                    c["lost"] += int(rec[2])

    table = [d for d in cum_standings.values() if d["team"]]
    table.sort(key=lambda x: (x["battle_points"], x["total_fantapunti"]), reverse=True)
    return {"kind": "battle_royale", "standings": table}


def _compute_bracket_standings(competition):
    """Match results and bracket structure for knockout and supercoppa."""
    fixtures = list(competition.fixtures.select_related("home", "away", "giornata").order_by("giornata__number", "id"))
    return {"kind": "bracket", "fixtures": fixtures}


@transaction.atomic
def ensure_league_season_and_competitions(league):
    """Ensure active Season, matchdays (1..38), and standard competitions exist for a league."""
    if not league:
        return None, []
    season, _ = Season.objects.get_or_create(
        league=league,
        is_current=True,
        defaults={"name": f"Stagione 2026/27 · {league.name}", "matchdays": 38}
    )
    if season.giornate.count() == 0:
        for num in range(1, (season.matchdays or 38) + 1):
            Giornata.objects.create(season=season, number=num)

    competitions = list(season.competitions.filter(is_active=True).order_by("id"))
    if not competitions and league.participants.count() >= 2:
        c1 = Competition.objects.create(
            season=season, name="Campionato 1vs1", kind=Competition.Type.ROUND_ROBIN
        )
        setup_round_robin_competition(c1)
        c2 = Competition.objects.create(
            season=season, name="Coppa Italia Battle Royale", kind=Competition.Type.BATTLE_ROYALE
        )
        c3 = Competition.objects.create(
            season=season, name="Gran Premio Punti", kind=Competition.Type.TOTAL_POINTS
        )
        competitions = [c1, c2, c3]

    return season, competitions

