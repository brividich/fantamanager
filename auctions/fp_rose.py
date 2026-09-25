"""Parser for the Fantapazz 'Rose Lega' .xls export.

Layout (confirmed): teams are laid out in horizontal blocks of 4 columns.
  block col +0 : role  (P/D/C/A)   — header row holds '#'
  block col +1 : player name        — header row holds the TEAM NAME
  block col +2 : cost paid          — header row holds remaining credits
  block col +3 : Serie A club code  — header row holds ''
Row 0 is the header; rows 1.. are players (blank rows skipped).
"""

ROLE_VALID = {"P", "D", "C", "A"}


def parse_rose_xls(path_or_bytes):
    """Return a list of teams: {name, credits, players:[{role,name,cost,club}]}."""
    import xlrd
    if isinstance(path_or_bytes, (bytes, bytearray)):
        wb = xlrd.open_workbook(file_contents=bytes(path_or_bytes),
                                ignore_workbook_corruption=True)
    else:
        wb = xlrd.open_workbook(path_or_bytes, ignore_workbook_corruption=True)

    sh = wb.sheets()[0]
    teams = []

    # Each team block is 4 columns wide.
    for base in range(0, sh.ncols, 4):
        if base + 1 >= sh.ncols:
            break
        team_name = str(sh.cell_value(0, base + 1)).strip()
        if not team_name:
            continue
        try:
            credits = float(sh.cell_value(0, base + 2))
        except (ValueError, TypeError):
            credits = None

        players = []
        for r in range(1, sh.nrows):
            role = str(sh.cell_value(r, base + 0)).strip().upper()
            name = str(sh.cell_value(r, base + 1)).strip()
            if not name or role not in ROLE_VALID:
                continue
            try:
                cost = float(sh.cell_value(r, base + 2))
            except (ValueError, TypeError):
                cost = 0.0
            club = str(sh.cell_value(r, base + 3)).strip()
            players.append({"role": role, "name": name, "cost": cost, "club": club})

        teams.append({"name": team_name, "credits": credits, "players": players})

    return teams


if __name__ == "__main__":
    import sys
    teams = parse_rose_xls(sys.argv[1])
    for t in teams:
        print(f"\n=== {t['name']}  (crediti residui: {t['credits']}) — {len(t['players'])} giocatori")
        for p in t["players"][:3]:
            print(f"   {p['role']} {p['name']:24} {p['cost']:>5}  {p['club']}")
    print(f"\nTotale squadre: {len(teams)}")
    print(f"Totale giocatori: {sum(len(t['players']) for t in teams)}")
