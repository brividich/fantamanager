"""Ranking UEFA per club (posizione), per i compensi dei giocatori ceduti (5.06).

Si scarica dal servizio pubblico che alimenta uefa.com; se la struttura cambia
o il sito non risponde, l'admin può incollare il ranking ("posizione;club"),
anche copiando direttamente la tabella della pagina uefa.com.
"""
import logging
import re
from datetime import date

import requests

logger = logging.getLogger("auctions.uefa")

URL = "https://comp.uefa.com/v2/coefficients"
# La pagina da cui l'admin copia la tabella quando il download non va.
RANKING_PAGE = "https://www.uefa.com/nationalassociations/uefarankings/club/"
# Per i compensi contano le prime 100 (5.05): tre pagine bastano e avanzano.
PAGE_SIZE = 100
MAX_PAGES = 3
# Il servizio sta dietro una CDN che respinge i client "non browser": ci si
# presenta come la pagina di uefa.com che lo interroga.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9,it;q=0.8",
    "Origin": "https://www.uefa.com",
    "Referer": "https://www.uefa.com/",
}


def _season_year():
    today = date.today()
    return today.year + 1 if today.month >= 7 else today.year


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _position(node):
    """La posizione di una voce del ranking: sul nodo stesso ("position",
    "rank") o in un suo oggetto di ranking ("overallRanking" è quello che usa
    uefa.com; le classifiche per singola stagione sono liste e non contano)."""
    for key in ("position", "rank", "ranking"):
        pos = _int(node.get(key))
        if pos:
            return pos
    subs = [node.get("overallRanking"), node.get("ranking")]
    subs += [v for k, v in node.items() if "ranking" in k.lower() and k != "overallRanking"]
    for sub in subs:
        if isinstance(sub, dict):
            for key in ("position", "rank"):
                pos = _int(sub.get(key))
                if pos:
                    return pos
    return None


def _club(node):
    """(nome, nazione) del club di una voce, o (None, "")."""
    member = node.get("member") or node.get("team") or node.get("club")
    if isinstance(member, dict):
        name = (member.get("displayName") or member.get("internationalName")
                or member.get("name") or member.get("displayOfficialName"))
        country = (member.get("countryName") or member.get("associationName")
                   or member.get("countryCode") or "")
        return (name if isinstance(name, str) else None), country
    name = node.get("displayName") or node.get("clubName") or node.get("teamName")
    return (name if isinstance(name, str) else None), ""


def _walk(node, out):
    """Raccoglie da un JSON qualunque le voci che hanno posizione e nome di un club."""
    if isinstance(node, dict):
        name, country = _club(node)
        pos = _position(node) if name else None
        if name and pos:
            out.append((name.strip(), pos, country))
            return out          # la voce è trovata: i suoi figli sono dettagli
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out)
    return out


def _describe(exc):
    """Perché il download non è andato, in parole dell'admin."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        return f"uefa.com ha rifiutato la richiesta ({status})"
    if status:
        return f"uefa.com ha risposto con un errore ({status})"
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "uefa.com non è raggiungibile da questo computer"
    if isinstance(exc, ValueError):
        return "la risposta di uefa.com non è leggibile"
    return "errore di rete"


def fetch(*, get=requests.get, year=None):
    """Scarica il ranking: ``(righe, motivo)``. ``righe`` = [(club, posizione, nazione)]
    ordinate per posizione; ``motivo`` spiega perché è vuoto (o "" se non lo è).

    Senza ``year`` prova la stagione in corso e, se non è ancora pubblicata
    (inizio stagione), quella prima."""
    years = [year] if year else [_season_year(), _season_year() - 1]
    reason = ""
    for season in years:
        found = {}
        try:
            for page in range(1, MAX_PAGES + 1):
                resp = get(URL, params={
                    "coefficientRange": "OVERALL", "coefficientType": "MEN_CLUB", "language": "EN",
                    "page": page, "pagesize": PAGE_SIZE, "seasonYear": season,
                }, timeout=20, headers=HEADERS)
                resp.raise_for_status()
                rows = _walk(resp.json(), [])
                fresh = [r for r in rows if r[0] not in found]
                for name, pos, country in fresh:
                    found[name] = (name, pos, country)
                # Pagina corta o già vista: il ranking è finito.
                if len(rows) < PAGE_SIZE or not fresh:
                    break
        except (requests.RequestException, ValueError) as exc:
            reason = _describe(exc)
            logger.warning("Ranking UEFA %s non disponibile: %s (%s)", season, reason, exc)
            if found:
                break
            continue
        if found:
            return sorted(found.values(), key=lambda r: r[1]), ""
        reason = reason or "uefa.com non ha restituito nessun club"
        logger.warning("Ranking UEFA %s: nessun club nella risposta", season)
    if found:
        return sorted(found.values(), key=lambda r: r[1]), ""
    return [], reason or "uefa.com non ha restituito nessun club"


def fetch_club_ranking(*, get=requests.get, year=None):
    """Solo le righe del ranking (vuoto se non scaricabile)."""
    return fetch(get=get, year=year)[0]


_POS = re.compile(r"^(\d{1,4})\s*[.°º)]?$")
_TRAIL = re.compile(r"(?:\s+[A-Z]{3})?(?:\s+-?\d+(?:[.,]\d+)?)*\s*$")


def _clean_name(name):
    """Toglie da un nome copiato dalla tabella la sigla della nazione e i punti."""
    name = _TRAIL.sub("", name.strip())
    return name.strip(" ;,\t-")


def parse_pasted(text):
    """Il ranking incollato a mano. Righe "12;Club", "12 Club", "Club;12" o
    "Club 12", e la tabella copiata da uefa.com (celle separate da tab, oppure
    posizione e club su righe diverse); sigla nazione e punti vengono ignorati."""
    rows, seen = [], set()
    pending = None      # una posizione da sola su una riga: il club è nella prossima

    def add(name, pos):
        name = _clean_name(name)
        if name and re.search(r"[A-Za-zÀ-ÿ]", name) and name.lower() not in seen:
            seen.add(name.lower())
            rows.append((name, pos, ""))

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        only_pos = _POS.match(line)
        if only_pos:
            pending = int(only_pos.group(1))
            continue
        if "\t" in line or ";" in line:
            cells = [c.strip() for c in re.split(r"[\t;]", line) if c.strip()]
            pos = next((int(m.group(1)) for c in cells for m in [_POS.match(c)] if m), None)
            name = next((c for c in cells if re.search(r"[A-Za-zÀ-ÿ]", c)), None)
            if name and (pos or pending):
                add(name, pos or pending)
                pending = None
            continue
        m = re.match(r"^(\d{1,4})\s*[.°º)]?[\s,]+(.+)$", line)
        if m:
            add(m.group(2), int(m.group(1)))
            pending = None
            continue
        m = re.match(r"^(.+?)[\s,]+(\d{1,4})$", line)
        if m:
            add(m.group(1), int(m.group(2)))
            pending = None
            continue
        if pending:
            add(line, pending)
            pending = None
    return rows
