"""Export a league's final rosters and standings.

Pure builders (no HTTP) so they can be unit-tested in isolation: the views just
wrap the returned bytes in an HttpResponse. Everything is scoped to one
``League`` (or the legacy ``league=None`` global pool).
"""
import csv
import io
from decimal import Decimal

from .models import Participant, Player

ROLE_ORDER = ["P", "D", "C", "A"]
ROLE_LABELS = {"P": "Portiere", "D": "Difensore", "C": "Centrocampista", "A": "Attaccante"}


def _participants_for(league):
    qs = Participant.objects.all()
    qs = qs.filter(league=league) if league is not None else qs
    return qs.order_by("display_name")


def _roster_for(participant):
    """Owned players of one participant, ordered P→D→C→A then name."""
    return list(
        Player.objects.filter(owner=participant).order_by("role", "name")
    )


def build_standings(league):
    """One row per team: credits, spent, remaining, slot counts, roster size.

    Sorted by spent (desc) — the natural "who committed most" ranking.
    """
    rows = []
    for p in _participants_for(league):
        roster = _roster_for(p)
        by_role = {r: sum(1 for pl in roster if pl.role == r) for r in ROLE_ORDER}
        rows.append({
            "participant": p,
            "name": p.display_name,
            "credits": p.credits,
            "spent": p.spent_credits,
            "remaining": p.remaining_credits,
            "by_role": by_role,
            "total": len(roster),
            "roster": roster,
        })
    rows.sort(key=lambda r: (r["spent"], r["total"]), reverse=True)
    return rows


def _flat_roster_rows(standings):
    """Flatten standings into (team, role, role_label, player, club, price) tuples."""
    out = []
    for s in standings:
        for pl in s["roster"]:
            out.append((
                s["name"], pl.role, ROLE_LABELS.get(pl.role, pl.role),
                pl.name, pl.team, pl.cost,
            ))
    return out


def build_csv(league):
    """UTF-8 CSV (BOM for Excel) of the flat roster: one row per owned player."""
    standings = build_standings(league)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Squadra", "Ruolo", "Ruolo (esteso)", "Giocatore", "Club", "Prezzo"])
    for team, role, label, player, club, price in _flat_roster_rows(standings):
        w.writerow([team, role, label, player, club, _num(price)])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def build_xlsx(league):
    """Workbook with two sheets: Classifica (standings) + Rose (flat roster)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    standings = build_standings(league)
    wb = Workbook()

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F2937")
    right = Alignment(horizontal="right")

    def _header(ws, cols):
        ws.append(cols)
        for c in ws[1]:
            c.font = head_font
            c.fill = head_fill

    # Sheet 1: Classifica
    ws1 = wb.active
    ws1.title = "Classifica"
    _header(ws1, ["Squadra", "Budget", "Spesi", "Residui", "P", "D", "C", "A", "Tot"])
    for s in standings:
        ws1.append([
            s["name"], _num(s["credits"]), _num(s["spent"]), _num(s["remaining"]),
            s["by_role"]["P"], s["by_role"]["D"], s["by_role"]["C"], s["by_role"]["A"],
            s["total"],
        ])
    for col in "BCDEFGHI":
        for cell in ws1[col]:
            cell.alignment = right
    ws1.column_dimensions["A"].width = 24

    # Sheet 2: Rose (flat)
    ws2 = wb.create_sheet("Rose")
    _header(ws2, ["Squadra", "Ruolo", "Giocatore", "Club", "Prezzo"])
    for team, role, _label, player, club, price in _flat_roster_rows(standings):
        ws2.append([team, role, player, club, _num(price)])
    ws2.column_dimensions["A"].width = 24
    ws2.column_dimensions["C"].width = 24
    for cell in ws2["E"]:
        cell.alignment = right

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_leghe_csv(league):
    """CSV for the Leghe Fantacalcio "Importa rose" flow.

    Reverse-engineered from a real FantaAsta Buzz export for the same import
    target (verified against an actual file, not guessed from documentation —
    Leghe Fantacalcio's own import spec isn't publicly published):

    * no header row — a ``$,$,$`` line marks the start of each fantateam's block
    * comma-separated, plain UTF-8 (no BOM), ``\\n`` line endings
    * each player is one row: ``Fantasquadra,Id ufficiale,Prezzo`` — Leghe
      Fantacalcio resolves name/role/club from the official numeric Id itself
      (``Player.ext_id``, populated from the listone import), matching on the
      Id alone rather than on the name

    A player without an ``ext_id`` cannot be represented in this format at all
    (there is no name column to fall back on) and is skipped — use the CSV/xlsx
    export instead for a human-readable roster that always includes every
    player. Row order within a team's block doesn't affect the import (matching
    is per-row by Id, not by position), so it keeps the app's own P→D→C→A
    ordering rather than mimicking the source export's arbitrary one.
    """
    standings = build_standings(league)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=",", lineterminator="\n")
    rank = {r: i for i, r in enumerate(ROLE_ORDER)}
    for s in standings:
        roster = [pl for pl in s["roster"] if pl.ext_id]
        if not roster:
            continue
        roster.sort(key=lambda p: (rank.get(p.role, 9), p.name))
        w.writerow(["$", "$", "$"])
        for pl in roster:
            w.writerow([s["name"], pl.ext_id, _num(pl.cost)])
    return buf.getvalue().encode("utf-8")


def _num(value):
    """Decimals → int when whole (cleaner cells), else float."""
    d = Decimal(str(value or 0))
    return int(d) if d == d.to_integral_value() else float(d)
