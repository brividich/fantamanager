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

    if kind in (Competition.Type.ROUND_ROBIN, Competition.Type.SEASON_SPLIT):
        return _compute_round_robin_standings(competition)
    elif kind == Competition.Type.FORMULA_1:
        return _compute_formula_1_standings(competition)
    elif kind == Competition.Type.SURVIVAL:
        return _compute_survival_standings(competition)
    elif kind == Competition.Type.SWISS_LEAGUE:
        return _compute_swiss_league_standings(competition)
    elif kind == Competition.Type.FANTA_DAVIS:
        return _compute_fanta_davis_standings(competition)
    elif kind == Competition.Type.GROUPS_KNOCKOUT:
        return _compute_groups_standings(competition)
    elif kind == Competition.Type.TOTAL_POINTS:
        return _compute_total_points_standings(competition)
    elif kind == Competition.Type.BATTLE_ROYALE:
        return _compute_battle_royale_standings(competition)
    elif kind in (Competition.Type.KNOCKOUT, Competition.Type.SUPERCOPPA):
        return _compute_bracket_standings(competition)
    else:
        return _compute_round_robin_standings(competition)


F1_POINTS_SCALE = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]


def _compute_formula_1_standings(competition):
    """Standings based on Formula 1 Grand Prix points awarded in each matchday."""
    season = competition.season
    settings = competition.settings or {}
    start_g = int(settings.get("start_giornata", 1) or 1)
    end_g = int(settings.get("end_giornata", season.matchdays or 38) or (season.matchdays or 38))

    participants = list(season.league.participants.filter(is_active=True)) if season.league else []
    data_by_team = {
        p.id: {
            "team": p,
            "gp_points": 0,
            "wins": 0,
            "podiums": 0,
            "best_score": Decimal("0"),
            "total_fantapunti": Decimal("0"),
            "giornate_played": 0,
            "last_points": 0,
        }
        for p in participants
    }

    giornate = season.giornate.filter(
        number__gte=start_g,
        number__lte=end_g,
        status=Giornata.Status.SCORED,
    ).order_by("number")

    for g in giornate:
        scores = list(GiornataScore.objects.filter(giornata=g, participant_id__in=data_by_team.keys()).order_by("-total"))
        for rank, sc in enumerate(scores):
            team_data = data_by_team[sc.participant_id]
            team_data["total_fantapunti"] += sc.total
            team_data["giornate_played"] += 1
            if sc.total > team_data["best_score"]:
                team_data["best_score"] = sc.total

            pts = F1_POINTS_SCALE[rank] if rank < len(F1_POINTS_SCALE) else 0
            team_data["gp_points"] += pts
            team_data["last_points"] = pts
            if rank == 0:
                team_data["wins"] += 1
            if rank < 3:
                team_data["podiums"] += 1

    table = list(data_by_team.values())
    table.sort(key=lambda x: (x["gp_points"], x["wins"], x["podiums"], x["total_fantapunti"]), reverse=True)
    return {
        "kind": "formula_1",
        "standings": table,
        "scale": F1_POINTS_SCALE,
    }


def _compute_survival_standings(competition):
    """Survival Cup (L'Uomo Morto): lowest scoring alive team eliminated each matchday."""
    season = competition.season
    settings = competition.settings or {}
    start_g = int(settings.get("start_giornata", 1) or 1)
    end_g = int(settings.get("end_giornata", season.matchdays or 38) or (season.matchdays or 38))

    participants = list(season.league.participants.filter(is_active=True)) if season.league else []
    alive_ids = {p.id for p in participants}
    data_by_team = {
        p.id: {
            "team": p,
            "is_alive": True,
            "status_text": "In gara",
            "eliminated_at": None,
            "eliminated_score": None,
            "total_fantapunti": Decimal("0"),
            "giornate_survived": 0,
        }
        for p in participants
    }

    giornate = season.giornate.filter(
        number__gte=start_g,
        number__lte=end_g,
        status=Giornata.Status.SCORED,
    ).order_by("number")

    for g in giornate:
        scores = list(GiornataScore.objects.filter(giornata=g, participant_id__in=data_by_team.keys()))
        score_by_pid = {sc.participant_id: sc.total for sc in scores}

        for pid, sc_tot in score_by_pid.items():
            if pid in data_by_team:
                data_by_team[pid]["total_fantapunti"] += sc_tot

        if len(alive_ids) > 1:
            alive_scores = [(pid, score_by_pid.get(pid, Decimal("0"))) for pid in alive_ids]
            if alive_scores:
                alive_scores.sort(key=lambda x: x[1])
                lowest_pid, lowest_score = alive_scores[0]
                alive_ids.remove(lowest_pid)
                data_by_team[lowest_pid]["is_alive"] = False
                data_by_team[lowest_pid]["eliminated_at"] = g.number
                data_by_team[lowest_pid]["eliminated_score"] = lowest_score
                data_by_team[lowest_pid]["status_text"] = f"Eliminato G.{g.number}"

        for pid in alive_ids:
            data_by_team[pid]["giornate_survived"] += 1

    if len(alive_ids) == 1 and giornate.exists():
        winner_id = list(alive_ids)[0]
        data_by_team[winner_id]["status_text"] = "👑 Ultimo Sopravvissuto"

    table = list(data_by_team.values())
    table.sort(key=lambda x: (
        1 if x["is_alive"] else 0,
        x["eliminated_at"] or 999 if not x["is_alive"] else 0,
        x["total_fantapunti"]
    ), reverse=True)

    alive_count = len(alive_ids)
    return {
        "kind": "survival",
        "standings": table,
        "alive_count": alive_count,
        "eliminated_count": len(participants) - alive_count,
    }


def _compute_swiss_league_standings(competition):
    """Standings for Swiss League / UEFA-style single table with qualification tiers."""
    base = _compute_round_robin_standings(competition)
    standings = base.get("standings", [])
    for idx, row in enumerate(standings, 1):
        if idx <= 8:
            row["tier"] = "top8"
            row["tier_label"] = "Qualificazione Diretta (Top 8)"
        elif idx <= 24:
            row["tier"] = "playoff"
            row["tier_label"] = "Fase Play-off (9°-24°)"
        else:
            row["tier"] = "out"
            row["tier_label"] = "Eliminazione"
    return {
        "kind": "swiss_league",
        "standings": standings,
    }


def _compute_fanta_davis_standings(competition):
    """Standings for Fanta-Davis (Pairs / Doppio): groups participants in pairs."""
    base = _compute_round_robin_standings(competition)
    standings = base.get("standings", [])
    pairs = []
    for i in range(0, len(standings), 2):
        t1 = standings[i]
        t2 = standings[i + 1] if i + 1 < len(standings) else None
        pair_pts = t1["points"] + (t2["points"] if t2 else 0)
        pair_fantapunti = t1["total_fantapunti"] + (t2["total_fantapunti"] if t2 else Decimal("0"))
        pairs.append({
            "pair_name": f"{t1['team'].display_name} & {t2['team'].display_name}" if t2 else t1['team'].display_name,
            "team_1": t1["team"],
            "team_2": t2["team"] if t2 else None,
            "points": pair_pts,
            "total_fantapunti": pair_fantapunti,
            "t1_pts": t1["points"],
            "t2_pts": t2["points"] if t2 else 0,
        })
    pairs.sort(key=lambda x: (x["points"], x["total_fantapunti"]), reverse=True)
    return {
        "kind": "fanta_davis",
        "standings": standings,
        "pairs": pairs,
    }


def _compute_groups_standings(competition):
    """Standings for Groups + Knockout (Girone A e Girone B)."""
    fixtures = competition.fixtures.select_related("home", "away", "giornata")
    groups = {
        "Girone A": defaultdict(lambda: {"team": None, "played": 0, "won": 0, "drawn": 0, "lost": 0, "goals_for": 0, "goals_against": 0, "goal_diff": 0, "points": 0, "total_fantapunti": Decimal("0")}),
        "Girone B": defaultdict(lambda: {"team": None, "played": 0, "won": 0, "drawn": 0, "lost": 0, "goals_for": 0, "goals_against": 0, "goal_diff": 0, "points": 0, "total_fantapunti": Decimal("0")}),
    }

    for f in fixtures:
        gname = "Girone A" if "Girone A" in (f.stage or "") else ("Girone B" if "Girone B" in (f.stage or "") else None)
        if not gname:
            continue
        table_dict = groups[gname]
        if f.home:
            table_dict[f.home_id]["team"] = f.home
        if f.away:
            table_dict[f.away_id]["team"] = f.away
        if not f.computed or not f.away:
            continue
        h = table_dict[f.home_id]
        a = table_dict[f.away_id]
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

    grouped_tables = {}
    for gname, table_dict in groups.items():
        tbl = []
        for tid, data in table_dict.items():
            if data["team"]:
                data["goal_diff"] = data["goals_for"] - data["goals_against"]
                tbl.append(data)
        tbl.sort(key=lambda x: (x["points"], x["goal_diff"], x["goals_for"]), reverse=True)
        grouped_tables[gname] = tbl

    return {
        "kind": "groups_knockout",
        "groups": grouped_tables,
        "standings": grouped_tables.get("Girone A", []) + grouped_tables.get("Girone B", []),
    }


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


def get_competition_matchdays(competition, participant_id=None):
    """Retrieve full matchday-by-matchday schedule and results for any competition.

    Returns a list of dicts ordered by giornata number:
    - For Round Robin / Bracket / Knockout: match fixtures with teams, scores, fantavoti, and user highlight.
    - For Total Points: ranked managers with fantapunti per giornata.
    - For Battle Royale: matchday battle results and records.
    """
    season = competition.season
    giornate = list(season.giornate.all().order_by("number"))
    matchdays = []

    kind = competition.kind

    if (
        kind in (Competition.Type.ROUND_ROBIN, Competition.Type.SEASON_SPLIT, Competition.Type.KNOCKOUT, Competition.Type.SUPERCOPPA)
        or competition.fixtures.exists()
    ):
        fixtures = list(
            competition.fixtures.select_related("home", "away", "giornata")
            .order_by("giornata__number", "id")
        )
        if fixtures:
            # Map of scores for fantavoti
            g_ids = list({f.giornata_id for f in fixtures})
            scores = GiornataScore.objects.filter(giornata_id__in=g_ids).values("giornata_id", "participant_id", "total")
            score_map = {(s["giornata_id"], s["participant_id"]): s["total"] for s in scores}

            grouped = defaultdict(list)
            for f in fixtures:
                f.home_score = score_map.get((f.giornata_id, f.home_id))
                f.away_score = score_map.get((f.giornata_id, f.away_id)) if f.away_id else None
                f.is_user_match = bool(participant_id and (f.home_id == participant_id or f.away_id == participant_id))
                grouped[f.giornata].append(f)

            for g in sorted(grouped.keys(), key=lambda x: x.number):
                fix_list = grouped[g]
                matchdays.append({
                    "giornata": g,
                    "kind": "fixtures",
                    "fixtures": fix_list,
                    "has_user_match": any(f.is_user_match for f in fix_list),
                    "is_scored": g.status == Giornata.Status.SCORED,
                    "is_live": g.status == Giornata.Status.LIVE,
                })
            return matchdays

    if kind == Competition.Type.TOTAL_POINTS:
        settings = competition.settings or {}
        start_g = settings.get("start_giornata", 1)
        end_g = settings.get("end_giornata", season.matchdays or 38)
        comp_giornate = [g for g in giornate if start_g <= g.number <= end_g]

        all_scores = list(
            GiornataScore.objects.filter(giornata__in=comp_giornate)
            .select_related("participant", "giornata")
        )
        scores_by_g = defaultdict(list)
        for s in all_scores:
            scores_by_g[s.giornata_id].append(s)

        for g in comp_giornate:
            day_scores = sorted(scores_by_g[g.id], key=lambda x: x.total, reverse=True)
            formatted_scores = []
            for rank, s in enumerate(day_scores, start=1):
                formatted_scores.append({
                    "rank": rank,
                    "participant": s.participant,
                    "total": s.total,
                    "goals": s.goals,
                    "is_user": bool(participant_id and s.participant_id == participant_id),
                })
            matchdays.append({
                "giornata": g,
                "kind": "points",
                "scores": formatted_scores,
                "is_scored": len(formatted_scores) > 0,
                "is_live": g.status == Giornata.Status.LIVE,
            })
        return matchdays

    if kind == Competition.Type.BATTLE_ROYALE:
        for g in giornate:
            if g.status == Giornata.Status.SCORED:
                day_res = compute_coppa_italia_battle_royale(g)
                for r in day_res:
                    r["is_user"] = bool(participant_id and r["participant"].id == participant_id)
                matchdays.append({
                    "giornata": g,
                    "kind": "battle_royale",
                    "results": day_res,
                    "is_scored": True,
                    "is_live": False,
                })
            else:
                matchdays.append({
                    "giornata": g,
                    "kind": "battle_royale",
                    "results": [],
                    "is_scored": False,
                    "is_live": g.status == Giornata.Status.LIVE,
                })
        return matchdays

    return matchdays


