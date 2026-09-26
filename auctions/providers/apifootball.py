"""API-Football (api-sports.io): club di destinazione di un giocatore uscito dalla Serie A.

La chiave va nella variabile d'ambiente ``APIFOOTBALL_KEY`` (mai nel codice o
nel database). Senza chiave, o se l'API non risponde, si restituisce None e
l'admin indica il club a mano. Il piano gratuito ha un limite giornaliero di
richieste: se ne fanno due per giocatore, solo quando l'admin preme "Rileva".
"""
import logging
import os
from datetime import date

import requests

logger = logging.getLogger("auctions.apifootball")

BASE = "https://v3.football.api-sports.io"
SERIE_A = 135


def _season():
    explicit = os.environ.get("APIFOOTBALL_SEASON")
    if explicit and explicit.isdigit():
        return int(explicit)
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1


def _get(path, params, *, get=requests.get):
    key = os.environ.get("APIFOOTBALL_KEY", "").strip()
    if not key:
        return None
    try:
        resp = get(f"{BASE}{path}", params=params, headers={"x-apisports-key": key}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("API-Football %s non disponibile: %s", path, exc)
        return None
    if data.get("errors"):
        logger.warning("API-Football %s: %s", path, data["errors"])
        return None
    return data.get("response") or []


def is_configured():
    return bool(os.environ.get("APIFOOTBALL_KEY", "").strip())


def find_destination(name, team="", *, get=requests.get):
    """{"club": nome, "date": "YYYY-MM-DD"} dell'ultimo trasferimento, o None."""
    surname = (name or "").replace(".", " ").split()[-1] if name else ""
    if len(surname) < 3:
        return None
    for season in (_season(), _season() - 1):
        players = _get("/players", {"search": surname, "league": SERIE_A, "season": season}, get=get)
        if players:
            break
    if not players:
        return None
    wanted = (team or "").lower()[:3]
    player_id = None
    for item in players:
        stats = item.get("statistics") or [{}]
        club = ((stats[0].get("team") or {}).get("name") or "").lower()
        if not wanted or club.startswith(wanted) or wanted in club:
            player_id = (item.get("player") or {}).get("id")
            break
    player_id = player_id or ((players[0].get("player") or {}).get("id"))
    if not player_id:
        return None
    transfers = _get("/transfers", {"player": player_id}, get=get)
    moves = []
    for block in transfers or []:
        moves.extend(block.get("transfers") or [])
    if not moves:
        return None
    last = max(moves, key=lambda t: t.get("date") or "")
    club = ((last.get("teams") or {}).get("in") or {}).get("name")
    return {"club": club, "date": last.get("date")} if club else None
