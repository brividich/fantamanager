"""API-Football (api-sports.io): club di destinazione di un giocatore uscito dalla Serie A.

La chiave va nella variabile d'ambiente ``APIFOOTBALL_KEY`` (mai nel codice o
nel database). Senza chiave, o se l'API non risponde, l'admin indica il club a
mano.

Il piano gratuito ha un limite giornaliero di richieste e non copre le
stagioni in corso: se la ricerca per stagione in Serie A viene rifiutata si
ripiega sull'anagrafica dei giocatori (``/players/profiles``), che non dipende
dalla stagione, e si riconosce il giocatore giusto dai suoi trasferimenti. Per
giocatore servono da 2 a 5 richieste.

Il controllo di tutte le rose invece va per club: i trasferimenti di un club
di Serie A (``/transfers?team=``) dicono in una richiesta chi dei suoi
giocatori è andato via, quindi basta una ventina di richieste, fatte al ritmo
consentito dal piano (il gratuito ne concede 10 al minuto).
"""
import logging
import os
import re
import time
import unicodedata
from datetime import date, timedelta

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


class RateLimited(ApiFootballError):
    """Troppe richieste in questo minuto: fra poco si può riprovare."""


class _SeasonRefused(Exception):
    pass


# Richieste rimaste secondo le intestazioni dell'ultima risposta (None = ignoto).
_limits = {"minute": None, "day": None}
LIMIT_HEADERS = {"minute": "x-ratelimit-remaining", "day": "x-ratelimit-requests-remaining"}


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
        _remember_limits(resp)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        logger.warning("API-Football %s: HTTP %s", path, status)
        if status in (401, 403):
            raise ApiFootballError("API-Football ha rifiutato la chiave (APIFOOTBALL_KEY)") from exc
        if status == 429:
            raise RateLimited("troppe richieste ad API-Football: riprova tra un minuto") from exc
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
            if "rateLimit" in errors:
                raise RateLimited("troppe richieste al minuto ad API-Football: riprova tra un minuto")
            if "requests" in errors:
                raise ApiFootballError("limite giornaliero di richieste API-Football raggiunto: riprova domani")
        # Un parametro che l'API non accetta vale solo per questa ricerca.
        return []
    return (data.get("response") if isinstance(data, dict) else None) or []


def _remember_limits(resp):
    headers = getattr(resp, "headers", None) or {}
    for key, header in LIMIT_HEADERS.items():
        value = str(headers.get(header, "")).strip()
        if value.isdigit():
            _limits[key] = int(value)


def paced(fn, *args, sleep=time.sleep, **kwargs):
    """Chiama ``fn`` senza superare il limite al minuto: se è esaurito aspetta
    che si liberi, e se l'API risponde comunque "troppe richieste" riprova."""
    for attempt in range(3):
        if _limits["minute"] == 0:
            sleep(61)
            _limits["minute"] = None
        try:
            return fn(*args, **kwargs)
        except RateLimited:
            if attempt == 2:
                raise
            sleep(61)


def is_configured():
    return bool(os.environ.get("APIFOOTBALL_KEY", "").strip())


# Lettere che la scomposizione Unicode non riduce a una lettera latina semplice.
_LATIN = str.maketrans({"ı": "i", "İ": "I", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
                        "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "þ": "th", "ð": "d"})


def _ascii(s):
    return unicodedata.normalize("NFKD", (s or "").translate(_LATIN)).encode("ascii", "ignore").decode()


def _norm_club(s):
    s = _ascii(s).lower()
    if "internazionale" in s:
        s = re.sub(r"\binternazionale\b", "inter", s)
    s = re.sub(r"\b(fc|cf|cfc|acf|bc|ac|as|sc|ssc|ss|us|afc|club|calcio|hellas|spa|srl|ssd|1907|1909|1913)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def same_club(club, team):
    """``club`` (nome API) è la squadra ``team`` del listone (nome o sigla)?"""
    a, b = _norm_club(club), _norm_club(team)
    if not a or not b:
        return False
    if len(b) <= 3:  # sigla (JUV, INT, …)
        return a.startswith(b)
    if len(a) <= 3:
        return b.startswith(a)
    return a == b or set(b.split()) <= set(a.split()) or set(a.split()) <= set(b.split())


def _search_term(name):
    """(parola da cercare, iniziale del nome) da un nome del listone.

    Il listone scrive "Martinez L.", "Esposito F.P.", "De Ketelaere": si cerca
    la parola più lunga del cognome, le iniziali servono a scegliere fra gli omonimi.
    """
    words = [w for w in re.split(r"[\s.'’\-]+", _ascii(name)) if w]
    long_words = sorted((w for w in words if len(w) >= 4), key=len, reverse=True)
    if not long_words:
        return "", ""
    term = long_words[0]
    term_idx = words.index(term)
    # Le iniziali di norma stanno dopo il cognome ("Martinez L.")
    initials = [w for w in words[term_idx + 1:] if len(w) <= 2 and w[:1].isalpha()]
    if not initials:
        # Se prima del cognome c'è un'iniziale (es. "L. Martinez"), purché non sia una particella nobiliare
        particles = {"de", "di", "da", "del", "van", "von", "le", "la", "el", "al", "st"}
        initials = [w for w in words[:term_idx] if len(w) <= 2 and w.lower() not in particles and w[:1].isalpha()]
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
    if not club or club.lower() in ("free agent", "free agency", "without club", "svincolato"):
        out_c = _club(last, "out")
        if out_c and (not team or same_club(out_c, team)):
            return {"club": "Svincolato", "date": last.get("date")}, ""
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


# --- Controllo di tutte le rose: per club di Serie A -------------------------------

def _season_refused_recently(season):
    return time.monotonic() - _refused_seasons.get(season, -REFUSED_TTL) < REFUSED_TTL


def italian_clubs(names, *, get=requests.get):
    """{squadra del listone: (id API, nome API)} per le squadre di Serie A.

    Prima l'elenco della Serie A (stagione in corso o precedente); se il piano
    non lo concede, o manca qualche neopromossa, i club italiani (``country``).
    Vale solo il nome identico (tolte sigle come AC/AS): "Inter" non è
    "Inter Miami" né "Inter U19".
    """
    wanted = {name: _norm_club(name) for name in names if _norm_club(name)}
    mapping = {}

    def match(teams):
        catalog = [((t.get("team") or {}).get("id"), (t.get("team") or {}).get("name") or "")
                   for t in teams if not (t.get("team") or {}).get("national")]
        for name, key in wanted.items():
            if name in mapping:
                continue
            same = [(tid, tname) for tid, tname in catalog if tid and (_norm_club(tname) == key or same_club(tname, name))]
            if not same and len(key) <= 3:  # sigla (JUV, INT, …): il nome più corto che comincia così
                same = sorted(((tid, tname) for tid, tname in catalog if tid and _norm_club(tname).startswith(key)),
                              key=lambda c: len(c[1]))
            if same:
                mapping[name] = same[0]

    for season in (_season(), _season() - 1):
        if len(mapping) == len(wanted) or _season_refused_recently(season):
            continue
        try:
            match(_get("/teams", {"league": SERIE_A, "season": season}, get=get))
        except _SeasonRefused:
            _refused_seasons[season] = time.monotonic()
    if len(mapping) < len(wanted):
        match(_get("/teams", {"country": "Italy"}, get=get))
    return mapping


def club_transfers(team_id, *, get=requests.get):
    """Tutti i trasferimenti dei giocatori passati dal club (una richiesta)."""
    return _get("/transfers", {"team": team_id}, get=get)


def departures(entries, players, club_id, is_serie_a, *, today=None, max_age_days=365):
    """Chi dei ``players`` (in rosa, di questo club secondo il listone) è andato via.

    ``entries`` è la risposta di :func:`club_transfers`. Vale l'ultimo
    trasferimento già avvenuto (non quelli annunciati per il futuro) dell'ultimo
    anno: se porta fuori dalla Serie A, il giocatore è uscito. Un nome che nei
    trasferimenti corrisponde a più giocatori si lascia stare.
    Ritorna ``{player.id: {"club", "date"}}``.
    """
    today_str = today.isoformat() if hasattr(today, "isoformat") else (today or date.today().isoformat())
    cutoff = (date.fromisoformat(today_str) - timedelta(days=max_age_days)).isoformat()
    people = []
    for entry in entries or []:
        name = (entry.get("player") or {}).get("name") or ""
        people.append(([w.lower() for w in re.split(r"[\s.'’\-]+", _ascii(name)) if w], entry))
    found = {}
    for player in players:
        term, initial = _search_term(player.name)
        if not term:
            p_words = [w for w in re.split(r"[\s.'’\-]+", _ascii(player.name)) if w]
            if not p_words:
                continue
            term = p_words[0]
            initial = p_words[1][:1].upper() if len(p_words) > 1 and len(p_words[1]) <= 2 else ""
        matches = [(words, entry) for words, entry in people if term.lower() in words]
        if len(matches) > 1 and initial:
            init_matches = [(words, entry) for words, entry in matches
                            if any(w[:1].upper() == initial for w in words if w.lower() != term.lower())]
            if init_matches:
                matches = init_matches
        if len(matches) != 1:
            continue
        moves = [m for m in matches[0][1].get("transfers") or []
                 if cutoff <= (m.get("date") or "")[:10] <= today_str]
        if not moves:
            continue
        last = max(moves, key=lambda m: (m.get("date") or "")[:10])
        teams = last.get("teams") or {}
        dest = teams.get("in") or {}
        out_team = teams.get("out") or {}
        dest_name = dest.get("name") or ""

        # Gestione svincolati / ritirati / contratti risolti
        if not dest_name or dest_name.lower() in ("free agent", "free agency", "without club", "svincolato", "none"):
            if out_team.get("id") == club_id or is_serie_a(out_team):
                found[player.id] = {"club": "Svincolato", "date": (last.get("date") or "")[:10]}
            continue

        if dest.get("id") == club_id or is_serie_a(dest):
            continue
        found[player.id] = {"club": dest_name, "date": (last.get("date") or "")[:10]}
    return found


# --- Anagrafica dei calciatori: le rose dei club di Serie A ----------------------

def serie_a_teams(*, get=requests.get):
    """[(id, nome, logo)] dei club della Serie A (stagione in corso o precedente).

    Vuoto se il piano non concede nessuna delle due stagioni: chi chiama
    ripiega sulle squadre del listone (:func:`italian_clubs`).
    """
    for season in (_season(), _season() - 1):
        if _season_refused_recently(season):
            continue
        try:
            teams = _get("/teams", {"league": SERIE_A, "season": season}, get=get)
        except _SeasonRefused:
            _refused_seasons[season] = time.monotonic()
            continue
        found = [((t.get("team") or {}).get("id"), (t.get("team") or {}).get("name") or "",
                  (t.get("team") or {}).get("logo") or "") for t in teams]
        found = [t for t in found if t[0]]
        if found:
            return found
    return []


def squad(team_id, *, get=requests.get):
    """La rosa attuale di un club (una richiesta, non dipende dalla stagione).

    ``[{"id", "name", "age", "number", "position", "photo"}]`` con
    ``position`` già tradotto nel ruolo del listone (P/D/C/A, "" se ignoto).
    """
    players = []
    for block in _get("/players/squads", {"team": team_id}, get=get):
        for p in block.get("players") or []:
            if not p.get("id"):
                continue
            players.append({
                "id": p["id"],
                "name": (p.get("name") or "").strip(),
                "age": p.get("age") if isinstance(p.get("age"), int) else None,
                "number": p.get("number") if isinstance(p.get("number"), int) else None,
                "position": POSITIONS.get(p.get("position") or "", ""),
                "photo": p.get("photo") or "",
            })
    return players


# --- Voti live: le partite di una giornata di Serie A ---------------------------
# Una giornata costa 1 richiesta per il calendario più 2 per ogni partita già
# iniziata (statistiche dei giocatori ed eventi, per le autoreti): fino a 21
# richieste a ogni giro. Il piano gratuito non copre la stagione in corso né
# regge un aggiornamento al minuto: per i voti live serve un piano a pagamento.
# Il "voto" è il rating di API-Football (scala 0-10, continuo), non il voto di
# un quotidiano: durante la diretta viene arrotondato al mezzo punto.

# Partite non ancora giocate o annullate: niente statistiche da chiedere.
NOT_PLAYED = {"TBD", "NS", "PST", "CANC", "ABD", "AWD", "WO"}
# Reparto delle statistiche di partita → ruolo del listone.
GAME_POSITIONS = {"G": "P", "D": "D", "M": "C", "F": "A"}


def _num(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def matchday_fixtures(round_number, *, season=None, get=requests.get):
    """[(fixture_id, status)] delle partite della giornata ``round_number``."""
    season = season or _season()
    fixtures = _get("/fixtures", {"league": SERIE_A, "season": season,
                                  "round": f"Regular Season - {int(round_number)}"}, get=get)
    out = []
    for f in fixtures:
        fx = f.get("fixture") or {}
        if fx.get("id"):
            out.append((fx["id"], ((fx.get("status") or {}).get("short") or "").upper()))
    return out


def _own_goals(fixture_id, get):
    """{api_id del giocatore: autoreti} dagli eventi della partita."""
    counts = {}
    for ev in _get("/fixtures/events", {"fixture": fixture_id}, get=get):
        if (ev.get("type") or "").lower() == "goal" and "own" in (ev.get("detail") or "").lower():
            pid = (ev.get("player") or {}).get("id")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
    return counts


def fixture_player_rows(fixture_id, *, get=requests.get):
    """Le righe voto/bonus/malus di una partita, nel formato di ``voti_live``."""
    own = _own_goals(fixture_id, get)
    rows = []
    # Gol segnati da ciascuna squadra: reti dei suoi giocatori + autogol avversari.
    # Si ricavano dalla stessa risposta, senza chiedere il risultato all'API.
    scored, own_by_team = {}, {}
    for team_block in _get("/fixtures/players", {"fixture": fixture_id}, get=get):
        team = ((team_block.get("team") or {}).get("name") or "").strip()
        scored.setdefault(team, 0)
        own_by_team.setdefault(team, 0)
        for entry in team_block.get("players") or []:
            player = entry.get("player") or {}
            stats = (entry.get("statistics") or [{}])[0] or {}
            games = stats.get("games") or {}
            goals = stats.get("goals") or {}
            cards = stats.get("cards") or {}
            pen = stats.get("penalty") or {}
            shots = stats.get("shots") or {}
            passes = stats.get("passes") or {}
            tackles = stats.get("tackles") or {}
            duels = stats.get("duels") or {}
            dribbles = stats.get("dribbles") or {}
            fouls = stats.get("fouls") or {}
            minutes = _num(games.get("minutes"))
            rating = games.get("rating")
            # Senza minuti giocati è un senza voto, anche se l'API manda un rating.
            vote = rating if (minutes > 0 and rating not in (None, "", "-")) else None
            own_goals = own.get(player.get("id"), 0)
            scored[team] += _num(goals.get("total"))
            own_by_team[team] += own_goals
            rows.append({
                "api_id": player.get("id"),
                "name": (player.get("name") or "").strip(),
                "team": team,
                "role": GAME_POSITIONS.get((games.get("position") or "").upper(), ""),
                "vote": vote,
                "goals": _num(goals.get("total")),
                "goals_conceded": _num(goals.get("conceded")),
                "own_goals": own_goals,
                "pen_scored": _num(pen.get("scored")),
                "pen_missed": _num(pen.get("missed")),
                "pen_saved": _num(pen.get("saved")),
                "assists": _num(goals.get("assists")),
                "yellow": _num(cards.get("yellow")) > 0,
                "red": _num(cards.get("red")) > 0,
                # Per il voto algoritmico (auctions/voto_algoritmico.py).
                "minutes": minutes,
                "saves": _num(goals.get("saves")),
                "shots_on": _num(shots.get("on")),
                "key_passes": _num(passes.get("key")),
                "tackles": _num(tackles.get("total")),
                "blocks": _num(tackles.get("blocks")),
                "interceptions": _num(tackles.get("interceptions")),
                "duels_won": _num(duels.get("won")),
                "dribbles_won": _num(dribbles.get("success")),
                "dribbled_past": _num(dribbles.get("past")),
                "fouls": _num(fouls.get("committed")),
                "pen_won": _num(pen.get("won")),
                "pen_committed": _num(pen.get("commited") or pen.get("committed")),
            })
    teams = list(scored)
    if len(teams) == 2:
        a, b = teams
        result = {a: (scored[a] + own_by_team[b], scored[b] + own_by_team[a]),
                  b: (scored[b] + own_by_team[a], scored[a] + own_by_team[b])}
        for row in rows:
            row["team_goals_for"], row["team_goals_against"] = result[row["team"]]
    return rows


def matchday_live_rows(round_number, *, season=None, get=requests.get):
    """Tutte le righe delle partite già iniziate della giornata ``round_number``.

    Solleva :class:`ApiFootballError` se la chiave manca, è rifiutata o il
    limite è finito: chi chiama lo mostra all'admin invece di tacere.
    """
    try:
        rows = []
        for fixture_id, status in matchday_fixtures(round_number, season=season, get=get):
            if status in NOT_PLAYED:
                continue
            rows.extend(paced(fixture_player_rows, fixture_id, get=get))
        return rows
    except _SeasonRefused as exc:
        raise ApiFootballError(
            "il piano API-Football non copre la stagione in corso: per i voti live serve un piano a pagamento"
        ) from exc
