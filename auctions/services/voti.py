"""Voti and matchday performance management for FantaManager.

Parses official matchday vote sheets (Excel/CSV), synchronizes PlayerPerformance,
and computes matchday results and tournament formats (like Coppa Italia Battle Royale).
"""
import csv
import io
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction

from ..models import Giornata, GiornataScore, League, Player, PlayerPerformance
from ..providers.importers import _find_match
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


def parse_voti_file(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    if hasattr(file_bytes, "read"):
        file_bytes = file_bytes.read()

    rows: List[Dict[str, Any]] = []
    fname = filename.lower()

    if fname.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        sheet = wb.active
        raw_rows = list(sheet.iter_rows(values_only=True))
        if not raw_rows:
            return []

        # Find header row
        header_idx = 0
        for i, row in enumerate(raw_rows[:15]):
            row_str = " ".join(_clean_str(c).lower() for c in row if c is not None)
            if "voto" in row_str or "nome" in row_str or "calciatore" in row_str:
                header_idx = i
                break

        headers = [_clean_str(c).lower() for c in raw_rows[header_idx]]
        for row in raw_rows[header_idx + 1:]:
            if not row or all(c is None for c in row):
                continue
            entry = dict(zip(headers, row))
            parsed = _normalize_vote_row(entry)
            if parsed and parsed.get("name"):
                rows.append(parsed)

    elif fname.endswith(".csv"):
        text = file_bytes.decode("utf-8", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2048]) if ";" in text or "," in text else None
        reader = csv.DictReader(io.StringIO(text), dialect=dialect or "excel")
        for row in reader:
            cleaned_row = {_clean_str(k).lower(): v for k, v in row.items()}
            parsed = _normalize_vote_row(cleaned_row)
            if parsed and parsed.get("name"):
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
        "own_goals": _parse_int(get_first("autogol", "autorete", "ag")),
        "pen_scored": _parse_int(get_first("rigori segnati", "rigore segnato", "r")),
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
