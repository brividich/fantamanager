"""Voti and matchday performance management for FantaManager.

Parses official matchday vote sheets (Excel/CSV), synchronizes PlayerPerformance,
and computes matchday results and tournament formats (like Coppa Italia Battle Royale).
"""
import csv
import io
import logging
from decimal import Decimal
from itertools import islice
from typing import Any, Dict, List, Optional

from django.db import transaction

from ..models import Giornata, GiornataScore, League, Player, PlayerPerformance
from ..providers.importers import _find_match
from ..uploads import MAX_IMPORT_ROWS
from .scoring import compute_giornata

logger = logging.getLogger(__name__)


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _parse_dec(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    s = str(val).replace(",", ".").strip()
    if not s or s.lower() in ("s.v.", "sv", "-", "*", "null", "none"):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _parse_int(val: Any) -> int:
    d = _parse_dec(val)
    return int(d) if d is not None else 0


def _parse_bool(val: Any) -> bool:
    if not val:
        return False
    s = str(val).strip().lower()
    return s in ("1", "true", "si", "sì", "x", "amm", "esp")


def _sheet_rows(file_bytes: bytes, fname: str) -> List[tuple]:
    """Every row of the first sheet (Excel .xlsx/.xls) or of the CSV, as tuples."""
    if fname.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        rows = [tuple(r) for r in islice(wb.worksheets[0].iter_rows(values_only=True), MAX_IMPORT_ROWS)]
        wb.close()
        return rows
    if fname.endswith(".xls"):
        import xlrd
        sheet = xlrd.open_workbook(file_contents=file_bytes).sheet_by_index(0)
        return [tuple(sheet.row_values(i)) for i in range(min(sheet.nrows, MAX_IMPORT_ROWS))]
    if fname.endswith(".csv"):
        text = file_bytes.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        return [tuple(r) for r in csv.reader(io.StringIO(text), dialect)]
    return []


_NAME_HEADERS = ("nome", "calciatore", "giocatore", "player")


def _is_header(cells: List[str]) -> bool:
    """A header row names the player column and at least one other known column."""
    if not any(c in _NAME_HEADERS for c in cells):
        return False
    return any(c in ("voto", "ruolo", "r", "gf", "squadra") or c.startswith("voto") for c in cells)


def _club_row(row) -> str:
    """The club name of a per-club block («ATALANTA» on a row of its own), or ''."""
    filled = [c for c in row if c is not None and str(c).strip() != ""]
    if len(filled) != 1 or not isinstance(filled[0], str):
        return ""
    text = filled[0].strip()
    if any(ch.isdigit() for ch in text) or len(text) > 30:
        return ""                                    # a title like «Voti Giornata 5»
    return text


def parse_voti_file(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """Rows of a votes file: one sheet with a header row (Nome, Voto, Gf, Gs, Ass…)
    or the per-club layout, where each club's block starts with a row holding
    only its name and repeats the header. The club of the block fills the team
    of its players, so two players with the same name don't get mixed up."""
    if hasattr(file_bytes, "read"):
        file_bytes = file_bytes.read()

    rows: List[Dict[str, Any]] = []
    headers: List[str] = []
    club = ""
    for raw in _sheet_rows(file_bytes, filename.lower()):
        if not raw or all(c is None or str(c).strip() == "" for c in raw):
            continue
        cells = [_clean_str(c).lower() for c in raw]
        if _is_header(cells):
            headers = cells
            continue
        if _club_row(raw):
            club = _club_row(raw)
            continue
        if not headers:
            continue
        parsed = _normalize_vote_row(dict(zip(headers, raw)))
        if parsed and parsed.get("name"):
            if not parsed["team"] and club:
                parsed["team"] = club
            rows.append(parsed)
    return rows


def _normalize_vote_row(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map arbitrary column header variations to canonical fields."""
    def get_first(*keys):
        # 1. First pass: exact matches
        for k in keys:
            for actual_key, val in d.items():
                if k == actual_key:
                    if val is not None and str(val).strip() != "":
                        return val
        # 2. Second pass: substring matches
        for k in keys:
            for actual_key, val in d.items():
                if k in actual_key:
                    if val is not None and str(val).strip() != "":
                        return val
        return None

    name = get_first("nome", "calciatore", "giocatore", "player")
    if not name:
        return None

    vote_val = get_first("voto italia", "v.i.", "vi", "voto statistico", "voto puro", "voto")
    vote = _parse_dec(vote_val)

    return {
        "name": _clean_str(name),
        "team": _clean_str(get_first("squadra", "sq", "club", "team")),
        "role": _clean_str(get_first("ruolo", "r", "role")),
        "vote": vote,
        "goals": _parse_int(get_first("gol", "gol fatti", "gol segnati", "gf", "goals", "reti")),
        "assists": _parse_int(get_first("assist", "ass", "as")),
        "own_goals": _parse_int(get_first("autogol", "autoreti", "autorete", "au", "ag")),
        "pen_scored": _parse_int(get_first("rigori segnati", "rigore segnato", "rf")),
        "pen_missed": _parse_int(get_first("rigori sbagliati", "rigore sbagliato", "rs")),
        "pen_saved": _parse_int(get_first("rigori parati", "rigore parato", "rp")),
        "goals_conceded": _parse_int(get_first("gol subiti", "gs", "goals_conceded")),
        "yellow": _parse_bool(get_first("ammonizione", "amm", "giallo", "yellow")),
        "red": _parse_bool(get_first("espulsione", "esp", "rosso", "red")),
    }


def import_voti_giornata(
    parsed_rows: List[Dict[str, Any]],
    giornata: Giornata,
    *,
    league: Optional[League] = None,
    recompute: bool = True
) -> Dict[str, Any]:
    """Persist parsed vote rows into PlayerPerformance for the specified Giornata."""
    pool_qs = Player.objects.all()
    if league:
        pool_qs = pool_qs.filter(league=league)

    existing_players = list(pool_qs)
    claimed_ids = set()
    created_count = 0
    updated_count = 0
    unmatched_count = 0

    with transaction.atomic():
        for row in parsed_rows:
            player = _find_match(row, existing_players, claimed_ids)
            if not player:
                unmatched_count += 1
                continue

            claimed_ids.add(player.pk)
            perf, created = PlayerPerformance.objects.update_or_create(
                giornata=giornata,
                player=player,
                defaults={
                    "vote": row.get("vote"),
                    "goals": row.get("goals", 0),
                    "assists": row.get("assists", 0),
                    "own_goals": row.get("own_goals", 0),
                    "pen_scored": row.get("pen_scored", 0),
                    "pen_missed": row.get("pen_missed", 0),
                    "pen_saved": row.get("pen_saved", 0),
                    "goals_conceded": row.get("goals_conceded", 0),
                    "yellow": row.get("yellow", False),
                    "red": row.get("red", False),
                }
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        if recompute:
            compute_giornata(giornata)

    return {
        "giornata": giornata.number,
        "created": created_count,
        "updated": updated_count,
        "unmatched": unmatched_count,
        "total_imported": created_count + updated_count,
    }


def compute_coppa_italia_battle_royale(giornata: Giornata) -> List[Dict[str, Any]]:
    """Compute Battle Royale round for Coppa Italia as specified by Fantalugnano regulations.
    
    Every participant in the league faces all other participants simultaneously.
    For each pairing:
      - More goals: 3 points
      - Same goals: 1 point
      - Fewer goals: 0 points
    """
    scores = list(GiornataScore.objects.filter(giornata=giornata).select_related("participant"))
    results = []

    for i, s1 in enumerate(scores):
        pts = 0
        w, d, l = 0, 0, 0
        p1 = s1.participant
        g1 = s1.goals

        matchups = []
        for j, s2 in enumerate(scores):
            if i == j:
                continue
            g2 = s2.goals
            if g1 > g2:
                pts += 3
                w += 1
                outcome = "V"
            elif g1 == g2:
                pts += 1
                d += 1
                outcome = "P"
            else:
                l += 1
                outcome = "S"
            matchups.append({
                "opp_name": s2.participant.display_name,
                "my_goals": g1,
                "opp_goals": g2,
                "my_score": float(s1.total),
                "opp_score": float(s2.total),
                "outcome": outcome,
            })

        results.append({
            "participant": p1,
            "participant_id": p1.id,
            "team_name": p1.display_name,
            "fantapunti": s1.total,
            "goals": g1,
            "battle_points": pts,
            "record": f"{w}V-{d}P-{l}S",
            "max_possible": (len(scores) - 1) * 3 if len(scores) > 1 else 0,
            "matchups": matchups,
        })

    # Sort by battle points descending, then by total fantapunti
    results.sort(key=lambda x: (-x["battle_points"], -x["fantapunti"]))
    return results
