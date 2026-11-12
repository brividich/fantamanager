"""Le rose della lega, squadra per squadra: lo stesso pannello nella console
(Dashboard → Rose) e nell'app (Regia → Rose), partial ``_rose_manage.html``."""
from decimal import Decimal

from ..models import Participant, Player
from .admin_dashboard import _classifica_standings

ROSE_ROLES = (
    ("P", "Portieri", "portiere"),
    ("D", "Difensori", "difensore"),
    ("C", "Centrocampisti", "centrocampista"),
    ("A", "Attaccanti", "attaccante"),
)


def _role_cap(league, code):
    """I posti del reparto. In Mantra contano solo i portieri: il movimento
    ha un tetto unico, mostrato a parte (``out_cap``)."""
    if not league.slot_limits:
        return 0
    if league.is_mantra:
        return league.slots_gk if code == "P" else 0
    return league.slots_for(code)


def rose_manage_context(request, league):
    """Ogni squadra di ``league`` con crediti, posti e rosa per reparto.

    ``?team=<id>`` sceglie la squadra aperta (dopo un'azione si torna lì)."""
    if league is None:
        return {"rose_league": None, "rose_teams": []}
    players = Player.objects.filter(league=league)
    teams = _classifica_standings(
        league, players, Participant.objects.filter(league=league).select_related("user"))
    by_owner = {}
    for pl in players.filter(owner__isnull=False).order_by("name"):
        by_owner.setdefault(pl.owner_id, []).append(pl)

    out_cap = league.slots_out if (league.is_mantra and league.slot_limits) else 0
    rows = []
    for p in teams:
        roster = by_owner.get(p.id, [])
        groups = []
        for code, label, one in ROSE_ROLES:
            lst = [pl for pl in roster if (pl.role if pl.role in ("P", "D", "C", "A") else "A") == code]
            cap = _role_cap(league, code)
            groups.append({
                "code": code, "label": label, "one": one, "list": lst,
                "spent": sum((pl.cost or Decimal("0")) for pl in lst),
                "cap": cap, "full": bool(cap) and len(lst) >= cap,
                "free": range(max(0, cap - len(lst))),
            })
        rows.append({
            "team": p,
            "groups": groups,
            "out_n": sum(len(g["list"]) for g in groups if g["code"] != "P"),
        })

    try:
        wanted = int(request.GET.get("team") or 0)
    except (TypeError, ValueError):
        wanted = 0
    ids = [r["team"].id for r in rows]
    selected = wanted if wanted in ids else (ids[0] if ids else None)
    return {
        "rose_league": league,
        "rose_teams": rows,
        "rose_selected": selected,
        "rose_out_cap": out_cap,
    }
