"""The league's history: every season with its winners (albo d'oro) and the
all-time table. A dynasty league lasts years: this is what it remembers."""
from collections import Counter
from decimal import Decimal

from django.db.models import Count, Sum

from ..models import Competition, GiornataScore, Participant, Season


def competition_winner(competition):
    """Who won it (a finished season) or leads it (the current one); None
    before anything was played. A cup names its winner only after the final."""
    from .competitions import compute_competition_standings

    if competition.kind in (Competition.Type.KNOCKOUT, Competition.Type.GROUPS_KNOCKOUT,
                            Competition.Type.SUPERCOPPA):
        wid = (competition.settings or {}).get("winner_id")
        return Participant.objects.filter(pk=wid).first() if wid else None
    standings = compute_competition_standings(competition).get("standings") or []
    return standings[0].get("team") if standings else None


def league_history(league):
    """``{"seasons": [...], "alltime": [...]}`` for the history page."""
    seasons = []
    titles = Counter()
    for season in Season.objects.filter(league=league).order_by("-is_current", "-id"):
        played = GiornataScore.objects.filter(giornata__season=season).exists()
        rows = []
        for comp in season.competitions.filter(is_active=True).order_by("id"):
            winner = competition_winner(comp) if played else None
            rows.append({"competition": comp, "winner": winner})
            if winner is not None and not season.is_current:
                titles[winner.id] += 1
        seasons.append({"season": season, "played": played, "competitions": rows})

    totals = (GiornataScore.objects.filter(giornata__season__league=league)
              .values("participant").annotate(fantapunti=Sum("total"), giornate=Count("id")))
    teams = {p.id: p for p in Participant.objects.filter(league=league)}
    alltime = []
    for row in totals:
        team = teams.get(row["participant"])
        if team is None:
            continue
        n = row["giornate"] or 0
        fp = row["fantapunti"] or Decimal("0")
        alltime.append({"team": team, "titles": titles.get(team.id, 0), "giornate": n,
                        "fantapunti": fp, "media": (fp / n) if n else Decimal("0")})
    alltime.sort(key=lambda r: (r["titles"], r["fantapunti"]), reverse=True)
    return {"seasons": seasons, "alltime": alltime}
