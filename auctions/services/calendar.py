"""Matchday calendar generation (round-robin) and championship standings."""
from decimal import Decimal

from ..models import Fixture, Giornata, GiornataScore, Participant


def _round_robin(team_ids):
    """Single round-robin pairings (circle method). Returns a list of rounds,
    each a list of (home_id, away_id); a bye pairs a team with None. Home/away
    alternate for fairness."""
    teams = list(team_ids)
    if len(teams) % 2:
        teams.append(None)
    n = len(teams)
    rounds = []
    arr = teams[:]
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]   # rotate, first fixed
    return rounds


def generate_calendar(season, *, teams=None):
    """Create Giornate + Fixtures for a season by cycling a round-robin over
    ``season.matchdays``. Return legs swap home/away. Replaces any existing
    giornate. Giornata 1 opens for lineups; the rest are SCHEDULED."""
    if teams is None:
        qs = Participant.objects.filter(is_active=True)
        qs = qs.filter(league=season.league) if season.league_id else qs.filter(league__isnull=True)
        teams = list(qs.order_by("id"))
    team_ids = [t.id for t in teams]
    base = _round_robin(team_ids) if len(team_ids) >= 2 else []

    season.giornate.all().delete()   # rebuild
    if not base:
        return []

    made = []
    for n in range(1, season.matchdays + 1):
        cycle, idx = divmod(n - 1, len(base))
        pairs = base[idx]
        g = Giornata.objects.create(
            season=season, number=n,
            status=Giornata.Status.OPEN if n == 1 else Giornata.Status.SCHEDULED,
        )
        for home, away in pairs:
            if cycle % 2 == 1:              # ritorno: swap venue
                home, away = away, home
            if home is None:               # bye lands on the away side
                home, away = away, home
            Fixture.objects.create(giornata=g, home_id=home, away_id=away)
        made.append(g)
    return made


def standings(season):
    """League table across all SCORED giornate: points, W/D/L, fanta-goals for/
    against, and total fantapunti (the tiebreak). Highest first."""
    rows = {}

    def row(pid):
        return rows.setdefault(pid, {
            "participant_id": pid, "played": 0, "won": 0, "drawn": 0, "lost": 0,
            "gf": 0, "ga": 0, "points": 0, "fantapunti": Decimal("0"),
        })

    fixtures = Fixture.objects.filter(giornata__season=season, computed=True, away__isnull=False)
    for fx in fixtures:
        h, a = row(fx.home_id), row(fx.away_id)
        h["played"] += 1; a["played"] += 1
        h["gf"] += fx.home_goals; h["ga"] += fx.away_goals
        a["gf"] += fx.away_goals; a["ga"] += fx.home_goals
        h["points"] += fx.home_points; a["points"] += fx.away_points
        if fx.home_goals > fx.away_goals: h["won"] += 1; a["lost"] += 1
        elif fx.away_goals > fx.home_goals: a["won"] += 1; h["lost"] += 1
        else: h["drawn"] += 1; a["drawn"] += 1

    for gs in GiornataScore.objects.filter(giornata__season=season):
        if gs.participant_id in rows:
            rows[gs.participant_id]["fantapunti"] += gs.total

    table = sorted(rows.values(), key=lambda r: (-r["points"], -(r["gf"] - r["ga"]),
                                                 -float(r["fantapunti"])))
    for i, r in enumerate(table, 1):
        r["rank"] = i
    return table
