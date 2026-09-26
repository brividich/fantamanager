"""Ranking UEFA per club (posizione), per i compensi dei giocatori ceduti (5.06).

Si scarica dal servizio pubblico che alimenta uefa.com; se la struttura cambia
o il sito non risponde, l'admin può incollare il ranking ("posizione;club").
"""
import logging
import re
from datetime import date

import requests

logger = logging.getLogger("auctions.uefa")

URL = ("https://comp.uefa.com/v2/coefficients?coefficientRange=OVERALL&coefficientType=MEN_CLUB"
       "&language=EN&page=1&pagesize=500&seasonYear={year}")


def _season_year():
    today = date.today()
    return today.year + 1 if today.month >= 7 else today.year


def _walk(node, out):
    """Raccoglie da un JSON qualunque le voci che hanno posizione e nome di un club."""
    if isinstance(node, dict):
        pos = node.get("position") or node.get("rank")
        member = node.get("member") or node.get("team") or node.get("club") or {}
        name = (member.get("displayName") or member.get("internationalName") or member.get("name")
                if isinstance(member, dict) else None) or node.get("displayName") or node.get("clubName")
        if isinstance(pos, int) and isinstance(name, str):
            country = member.get("countryName") or member.get("associationName") or "" if isinstance(member, dict) else ""
            out.append((name, pos, country))
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out)
    return out


def fetch_club_ranking(*, get=requests.get, year=None):
    try:
        resp = get(URL.format(year=year or _season_year()), timeout=20,
                   headers={"User-Agent": "FantaManager/1.0", "Accept": "application/json"})
        resp.raise_for_status()
        rows = _walk(resp.json(), [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Ranking UEFA non disponibile: %s", exc)
        return []
    seen, unique = set(), []
    for name, pos, country in sorted(rows, key=lambda r: r[1]):
        if name not in seen:
            seen.add(name)
            unique.append((name, pos, country))
    return unique


def parse_pasted(text):
    """Righe "12;Club Name" o "12 Club Name" o "Club Name;12"."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)[\s;,.\t]+(.+)$", line) or None
        if m:
            rows.append((m.group(2).strip(), int(m.group(1)), ""))
            continue
        m = re.match(r"^(.+?)[\s;,\t]+(\d+)$", line)
        if m:
            rows.append((m.group(1).strip(), int(m.group(2)), ""))
    return rows
