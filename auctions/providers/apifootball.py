"""API-Football (api-sports.io): club di destinazione di un giocatore uscito dalla Serie A.

La chiave va nella variabile d'ambiente ``APIFOOTBALL_KEY`` (mai nel codice o
nel database). Senza chiave, o se l'API non risponde, l'admin indica il club a
mano.

Il piano gratuito ha un limite giornaliero di richieste e non copre le
stagioni in corso: se la ricerca per stagione in Serie A viene rifiutata si
ripiega sull'anagrafica dei giocatori (``/players/profiles``), che non dipende
dalla stagione, e si riconosce il giocatore giusto dai suoi trasferimenti. Per
giocatore servono da 2 a 5 richieste.
"""
import logging
import os
import re
import time
import unicodedata
from datetime import date

import requests

logger = logging.getLogger("auctions.apifootball")

BASE = "https://v3.football.api-sports.io"
SERIE_A = 135
# Ruolo dell'anagrafica API → ruolo del listone.
POSITIONS = {"Goalkeeper": "P", "Defender": "D", "Midfielder": "C", "Attacker": "A"}
# Omonimi da verificare coi trasferimenti quando si cerca nell'anagrafica.
MAX_CANDIDATES = 3

# Stagioni che il piano della chiave non copre: per qualche ora non si
# richiedono più (se il piano cambia, basta aspettare o riavviare).
REFUSED_TTL = 6 * 3600
_refused_seasons = {}


class ApiFootballError(Exception):
    """Un problema che vale per ogni richiesta (chiave, limite, rete): inutile insistere."""


class _SeasonRefused(Exception):
    pass


def _season():
    explicit = os.environ.get("APIFOOTBALL_SEASON")
    if explicit and explicit.isdigit():
        return int(explicit)
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1


def _get(path, params, *, get=requests.get):
    key = os.environ.get("APIFOOTBALL_KEY", "").strip()
    if not key:
        raise ApiFootballError("API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia")
    try:
        resp = get(f"{BASE}{path}", params=params, headers={"x-apisports-key": key}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        logger.warning("API-Football %s: HTTP %s", path, status)
        if status in (401, 403):
            raise ApiFootballError("API-Football ha rifiutato la chiave (APIFOOTBALL_KEY)") from exc
        if status == 429:
            raise ApiFootballError("limite di richieste API-Football raggiunto: riprova più tardi") from exc
        raise ApiFootballError(f"API-Football non risponde (HTTP {status})") from exc
    except (requests.RequestException, ValueError) as exc:
        logger.warning("API-Football %s non disponibile: %s", path, exc)
        raise ApiFootballError("API-Football non raggiungibile da questo server") from exc
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        logger.warning("API-Football %s %s: %s", path, params, errors)
        if isinstance(errors, dict):
            if "plan" in errors:
                raise _SeasonRefused(str(errors["plan"]))
            if "token" in errors or "access" in errors:
                raise ApiFootballError("API-Football ha rifiutato la chiave (APIFOOTBALL_KEY)")
            if "requests" in errors or "rateLimit" in errors:
                raise ApiFootballError("limite di richieste API-Football raggiunto: riprova più tardi")
        # Un parametro che l'API non accetta vale solo per questa ricerca.
        return []
    return (data.get("response") if isinstance(data, dict) else None) or []


def is_configured():
    return bool(os.environ.get("APIFOOTBALL_KEY", "").strip())


def _ascii(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def _norm_club(s):
    s = _ascii(s).lower()
    s = re.sub(r"\b(fc|cf|ac|as|sc|ssc|ss|us|afc|club|calcio|hellas|1907|1909|1913)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def same_club(club, team):
    """``club`` (nome API) è la squadra ``team`` del listone (nome o sigla)?"""
    a, b = _norm_club(club), _norm_club(team)
    if not a or not b:
        return False
    if len(b) <= 3:  # sigla (JUV, INT, …)
        return a.startswith(b)
    return a == b or set(b.split()) <= set(a.split()) or set(a.split()) <= set(b.split())


def _search_term(name):
    """(parola da cercare, iniziale del nome) da un nome del listone.

    Il listone scrive "Martinez L.", "Esposito F.P.", "De Ketelaere": si cerca
    la parola più lunga, le iniziali servono a scegliere fra gli omonimi.
    """
    words = [w for w in re.split(r"[\s.'’\-]+", _ascii(name)) if w]
    long_words = sorted((w for w in words if len(w) >= 4), key=len, reverse=True)
    if not long_words:
        return "", ""
    term = long_words[0]
    # Le iniziali stanno dopo il cognome ("De" in "De Ketelaere" non lo è).
    initials = [w for w in words[words.index(term) + 1:] if len(w) <= 2 and w[:1].isalpha()]
    return term, (initials[0][0].upper() if initials else "")


def _moves(transfers):
    moves = []
    for block in transfers or []:
        moves.extend(block.get("transfers") or [])
    return moves


def _club(move, side):
    return ((move.get("teams") or {}).get(side) or {}).get("name") or ""


def _from_season_search(term, team, get):
    """Id del giocatore cercandolo tra quelli della Serie A (stagione in corso o precedente)."""
    for season in (_season(), _season() - 1):
        if time.monotonic() - _refused_seasons.get(season, -REFUSED_TTL) < REFUSED_TTL:
            continue
        try:
            players = _get("/players", {"search": term, "league": SERIE_A, "season": season}, get=get)
        except _SeasonRefused:
            _refused_seasons[season] = time.monotonic()
            continue
        for item in players:
            stats = item.get("statistics") or [{}]
            club = (stats[0].get("team") or {}).get("name") or ""
            # Con la squadra nota si accetta solo chi ci giocava: un omonimo
            # darebbe il club (e il compenso) sbagliato.
            if not team or same_club(club, team):
                return (item.get("player") or {}).get("id")
    return None


def _profile_candidates(term, initial, role, get):
    """Omonimi dall'anagrafica, i più probabili prima (iniziale e ruolo)."""
    try:
        profiles = _get("/players/profiles", {"search": term}, get=get)
    except _SeasonRefused:
        return []
    ranked = []
    for item in profiles:
        p = item.get("player") or {}
        if not p.get("id"):
            continue
        last = _ascii(p.get("lastname") or p.get("name") or "").lower()
        if term.lower() not in last:
            continue
        first = _ascii(p.get("firstname") or p.get("name") or "")
        pos = POSITIONS.get(p.get("position") or "")
        if role and pos and pos != role:
            continue
        score = 2 * bool(initial and first[:1].upper() == initial) + bool(role and pos == role)
        ranked.append((score, p["id"]))
    ranked.sort(key=lambda r: -r[0])
    return [pid for _, pid in ranked]


def lookup(name, team="", role="", *, get=requests.get):
    """Dove è andato il giocatore: ``({"club", "date"}, "")`` o ``(None, motivo)``.

    Solleva :class:`ApiFootballError` per i problemi che bloccano ogni ricerca
    (chiave mancante o rifiutata, limite di richieste, rete).
    """
    term, initial = _search_term(name)
    if not term:
        return None, "nome troppo corto per cercarlo su API-Football"

    moves = None
    player_id = _from_season_search(term, team, get)
    if player_id:
        moves = _moves(_get("/transfers", {"player": player_id}, get=get))
    else:
        candidates = _profile_candidates(term, initial, role, get)
        for pid in candidates[:MAX_CANDIDATES]:
            found = _moves(_get("/transfers", {"player": pid}, get=get))
            # Senza squadra si può fidarsi solo di un risultato unico.
            if (team and any(same_club(_club(m, "in"), team) or same_club(_club(m, "out"), team) for m in found)) \
                    or (not team and len(candidates) == 1):
                moves = found
                break
        if moves is None:
            return None, "giocatore non trovato su API-Football"

    if not moves:
        return None, "nessun trasferimento registrato su API-Football"
    last = max(moves, key=lambda t: t.get("date") or "")
    club = _club(last, "in")
    if not club:
        return None, "nessun trasferimento registrato su API-Football"
    if team and same_club(club, team):
        return None, f"per API-Football l'ultimo trasferimento è ancora verso {club}"
    return {"club": club, "date": last.get("date")}, ""


def find_destination(name, team="", *, get=requests.get):
    """{"club": nome, "date": "YYYY-MM-DD"} dell'ultimo trasferimento, o None."""
    try:
        found, _reason = lookup(name, team, get=get)
    except ApiFootballError:
        return None
    return found
