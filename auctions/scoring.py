"""Fantacalcio scoring engine — pure, DB-free, fully configurable.

The functions here turn *lineups* + *player performances* into a giornata
score, applying the classic Italian ruleset:

  · fantavoto  = voto + bonus/malus (gol +3, assist +1, ammonizione -0.5, …)
  · panchina   = an s.v. starter (no vote) is replaced by the first bench
                 player of the SAME role who has a vote, in bench order,
                 up to ``max_subs`` (3) substitutions
  · gol        = the summed fantavoto is converted to goals on a threshold
                 ladder (66 → 1 gol, then +1 every 6 points)

Everything is driven by a ``rules`` dict (see ``DEFAULTS``) so a league can
tune bonus values or the conversion ladder without touching this code. Inputs
are plain dicts/lists — no models — so the whole engine is unit-testable in
isolation; the model layer (services) just marshals data in and out.
"""
from decimal import Decimal

# Classic Fantacalcio.it defaults. Values are points added to the base voto.
DEFAULTS = {
    "goal":          3,      # rete segnata (rigore incluso)
    "assist":        1,
    "own_goal":     -2,      # autorete
    "pen_missed":   -3,      # rigore sbagliato
    "pen_saved":     3,      # rigore parato (portiere)
    "yellow":       -0.5,    # ammonizione
    "red":          -1,      # espulsione
    "goal_conceded": -1,     # gol subìto (portiere), per rete
    "clean_sheet":   1,      # portiere imbattuto (0 gol subiti)
    "conv_base":     66,     # fantapunti per il 1º gol
    "conv_step":     6,      # fantapunti per ogni gol successivo
    "max_subs":      3,      # sostituzioni dalla panchina
    "modificatore_difesa": False,
    # media (portiere + migliori 3 difensori) → bonus, applicata solo se attiva.
    "modif_table": [(6.5, 3), (6.0, 1)],
}


def _d(x):
    """Coerce to Decimal without float noise (Decimal(str(0.5)) == 0.5)."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def player_fantavoto(perf, role, rules):
    """Return ``(fantavoto, has_vote)`` for one player's performance.

    ``perf`` is a dict; ``vote`` None (or missing) means *senza voto* — the
    player didn't earn a vote and becomes a substitution candidate. All event
    keys are optional and default to 0/False.
    """
    vote = perf.get("vote")
    if vote is None:
        return None, False

    r = rules
    total = _d(vote)
    total += _d(r["goal"])          * int(perf.get("goals", 0))
    total += _d(r["assist"])        * int(perf.get("assists", 0))
    total += _d(r["own_goal"])      * int(perf.get("own_goals", 0))
    total += _d(r["pen_missed"])    * int(perf.get("pen_missed", 0))
    if perf.get("yellow"):
        total += _d(r["yellow"])
    if perf.get("red"):
        total += _d(r["red"])
    if role == "P":
        # A saved penalty is a goalkeeper-only event by definition of the
        # game — scoped here alongside the other keeper-only bonuses rather
        # than applied regardless of role.
        total += _d(r["pen_saved"])     * int(perf.get("pen_saved", 0))
        conceded = int(perf.get("goals_conceded", 0))
        total += _d(r["goal_conceded"]) * conceded
        if r.get("clean_sheet") and conceded == 0:
            total += _d(r["clean_sheet"])
    if perf.get("is_captain"):
        if _d(vote) >= Decimal("6.5"):
            total += Decimal("0.5")
        elif _d(vote) <= Decimal("5.5"):
            total -= Decimal("0.5")
    return total, True


def goals_from_total(total, rules):
    """Convert a summed fantavoto to goals on the threshold ladder."""
    base, step = _d(rules["conv_base"]), _d(rules["conv_step"])
    total = _d(total)
    if total < base:
        return 0
    return 1 + int((total - base) / step)


def _modificatore_difesa(lines, rules):
    """Bonus for a solid defence: average the *base votes* of the goalkeeper and
    the best 3 defenders actually fielded, then map it onto ``modif_table``.
    Returns 0 when there's no keeper + 3 graded defenders."""
    gk = [l["vote"] for l in lines if l["role"] == "P" and l["vote"] is not None]
    df = sorted((l["vote"] for l in lines if l["role"] == "D" and l["vote"] is not None), reverse=True)
    if not gk or len(df) < 3:
        return Decimal("0")
    avg = (gk[0] + sum(df[:3])) / _d(4)
    for threshold, bonus in sorted(rules["modif_table"], reverse=True):
        if avg >= _d(threshold):
            return _d(bonus)
    return Decimal("0")


def score_lineup(starters, bench, perf, rules=None):
    """Score one manager's lineup for a giornata.

    ``starters`` / ``bench`` are ordered lists of ``{"id", "role"}`` (bench order
    is the substitution priority). ``perf`` maps player id → performance dict.
    Returns a detailed result: total fantapunti, goals, the applied defensive
    modifier, and per-slot lines (with which bench player, if any, came on).
    """
    rules = {**DEFAULTS, **(rules or {})}

    lines = []
    for s in starters:
        p = perf.get(s["id"], {})
        fv, has = player_fantavoto(p, s["role"], rules)
        lines.append({
            "id": s["id"], "role": s["role"],
            "vote": _d(p["vote"]) if p.get("vote") is not None else None,
            "fantavoto": fv, "has_vote": has, "sub_in": None,
        })

    # Substitutions: each s.v. starter (in lineup order) is replaced by the first
    # still-available bench player of the same role who has a vote.
    bench_avail = list(bench)
    subs_done = 0
    for line in lines:
        if line["has_vote"] or subs_done >= rules["max_subs"]:
            continue
        for i, b in enumerate(bench_avail):
            if b["role"] != line["role"]:
                continue
            bp = perf.get(b["id"], {})
            bfv, bhas = player_fantavoto(bp, b["role"], rules)
            if not bhas:
                continue
            line.update({
                "vote": _d(bp["vote"]), "fantavoto": bfv,
                "has_vote": True, "sub_in": b["id"],
            })
            subs_done += 1
            bench_avail.pop(i)
            break

    total = sum((l["fantavoto"] for l in lines if l["has_vote"]), Decimal("0"))
    modificatore = Decimal("0")
    if rules.get("modificatore_difesa"):
        modificatore = _modificatore_difesa(lines, rules)
        total += modificatore

    return {
        "total": total,
        "goals": goals_from_total(total, rules),
        "modificatore": modificatore,
        "subs": subs_done,
        "lines": lines,
    }


def fixture_outcome(home_goals, away_goals, rules=None):
    """League points for a head-to-head result: 3 / 1 / 0."""
    if home_goals > away_goals:
        return 3, 0
    if home_goals < away_goals:
        return 0, 3
    return 1, 1
