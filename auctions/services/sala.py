"""L'asta dal vivo su un PC in sala, per una lega che vive su un sito.

FantaManager gira in due modi, e questo modulo li fa parlare:

- **sul sito** (NAS, VPS) la lega vive tutto l'anno;
- **sul PC in sala** (app desktop) si fa l'asta, anche senza internet.

Il giro è questo. Sul sito l'admin della lega genera una chiave di
collegamento (``make_key``). Il PC, con indirizzo del sito e chiave, scarica la
lega (``snapshot``): da quel momento il sito blocca rose, crediti e listone di
quella lega (``lock``), così l'arbitro è uno solo. Il PC ne fa una copia sua
(``import_linked_league``) e ci gioca l'asta come con una lega qualunque.
Alla fine rimanda lo stato finale di rose e crediti (``results_payload``): il
sito lo applica così com'è (``apply_results``) e si sblocca.

Chi usa il PC da solo, senza sito, non passa mai di qui.
"""
import hashlib
import json
import logging
import secrets
import socket
import threading
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import League, Participant, Player, RosterLog
from ..models.participant import generate_access_code

logger = logging.getLogger("auctions.sala")

FORMAT = 1
KEY_PREFIX = "fmsala_"

LOCKED_MSG = ("Asta in corso in sala: rose, crediti e listone di questa lega sono bloccati "
              "finché il PC della sala non rimanda i risultati.")

# Le impostazioni della lega che servono all'asta sul PC.
LEAGUE_FIELDS = (
    "name", "budget", "slot_limits", "slots_p", "slots_d", "slots_c", "slots_a",
    "game_mode", "slots_gk", "slots_out", "gk_max_clubs",
    "contracts_enabled", "contract_rules", "season_number",
)
TEAM_FIELDS = ("display_name", "external_team_id", "short_name", "credits", "spent_credits", "is_active")
# Del giocatore il PC riceve tutto quello che serve all'asta; rimanda solo
# quello che l'asta cambia (ROSTER_FIELDS).
PLAYER_FIELDS = (
    "name", "role", "team", "initial_price", "photo_url", "ext_id", "mantra_roles",
    "fvm", "presences", "avg_vote", "fanta_avg", "goals", "assists",
)
ROSTER_FIELDS = (
    "cost", "contract_years", "renewal_declared", "loan_sessions_left", "acquired_at", "renewed_at",
)
_DECIMAL = {"budget", "credits", "spent_credits", "initial_price", "cost", "fvm", "avg_vote", "fanta_avg"}
_DATETIME = {"acquired_at", "renewed_at"}


# Tetti: una lega vera ne sta ben sotto; uno snapshot o un risultato oltre
# viene rifiutato tutto, prima di creare o cambiare qualsiasi cosa.
MAX_TEAMS = 40
MAX_PLAYERS = 2000
MAX_LOG_ENTRIES = 5000
MAX_RESPONSE_BYTES = 10 * 1024 * 1024     # quello che il PC legge dal sito
MAX_BODY_BYTES = 2 * 1024 * 1024          # quello che l'API del sito accetta dal PC

PC_ONLY_MESSAGE = ("Scaricare una lega, rimandare i risultati e sbloccarla si fa dall'app "
                   "FantaManager del PC in sala (Impostazioni → «Asta in sala»), non dal sito.")

_resolve = socket.getaddrinfo   # i test lo sostituiscono: nessuna rete vera


class SalaError(Exception):
    """Un'operazione della sala che non si può fare: il messaggio è per chi la fa."""


class LeagueLocked(SalaError):
    def __init__(self, league=None):
        super().__init__(LOCKED_MSG)
        self.league = league


# --- valori in JSON ----------------------------------------------------------

def _out(name, value):
    if value is None:
        return None
    if name in _DECIMAL:
        return str(value)
    if name in _DATETIME:
        return value.isoformat()
    return value


def _in(name, value):
    if value is None:
        return None
    if name in _DECIMAL:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise SalaError(f"Valore non valido per «{name}»: {value!r}.")
    if name in _DATETIME:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise SalaError(f"Data non valida per «{name}»: {value!r}.")
    return value


# --- sul sito: chiave e blocco -----------------------------------------------

def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def make_key(league, by=""):
    """Una chiave nuova per collegare il PC della sala (la vecchia non vale
    più). Si mostra una volta: il sito ne tiene solo l'impronta."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    sala = dict(league.sala or {})
    sala.update({"key_hash": _hash(key), "key_hint": key[-4:],
                 "key_at": timezone.now().isoformat(), "key_by": by[:80]})
    league.sala = sala
    league.save(update_fields=["sala", "updated_at"])
    return key


def revoke_key(league):
    sala = dict(league.sala or {})
    for k in ("key_hash", "key_hint", "key_at", "key_by"):
        sala.pop(k, None)
    league.sala = sala
    league.save(update_fields=["sala", "updated_at"])


def has_key(league):
    return bool((league.sala or {}).get("key_hash"))


def league_for_key(key):
    """La lega di questa chiave, o None."""
    key = (key or "").strip()
    if not key.startswith(KEY_PREFIX):
        return None
    return League.objects.filter(sala__key_hash=_hash(key)).first()


def lock_info(league):
    """Il blocco in corso (dict con id, quando, da chi) o None."""
    return (league.sala or {}).get("lock") if league is not None else None


def is_locked(league):
    return bool(lock_info(league))


def ensure_unlocked(league):
    """Da chiamare prima di cambiare rose, crediti o listone di una lega.

    ``league`` è la lega, il suo id, o None (nessuna lega: niente da fare).
    Rilegge la lega dal database: il blocco può essere arrivato da un attimo.
    """
    if league is None:
        return
    league_id = league if isinstance(league, int) else league.pk
    if league_id is None:
        return
    current = League.objects.filter(pk=league_id).values_list("sala", flat=True).first()
    if (current or {}).get("lock"):
        raise LeagueLocked(league)


def lock(league, by="", force=False):
    """Blocca la lega per l'asta in sala; ritorna l'id del blocco.

    Una lega già bloccata resta di chi l'ha bloccata, salvo ``force`` (il PC
    di prima si è perso: chi ha la chiave riparte da capo)."""
    from ..models import Auction
    if Auction.objects.filter(league=league, status__in=[Auction.Status.LIVE, Auction.Status.PAUSED]).exists():
        raise SalaError("Sul sito c'è un'asta in corso per questa lega: chiudila prima di portare l'asta in sala.")
    with transaction.atomic():
        league = League.objects.select_for_update().get(pk=league.pk)
        if is_locked(league) and not force:
            raise SalaError("La lega è già bloccata da un'altra asta in sala "
                            f"(dal {lock_info(league).get('at', '?')[:16].replace('T', ' ')}).")
        lock_id = uuid.uuid4().hex
        sala = dict(league.sala or {})
        sala["lock"] = {"id": lock_id, "at": timezone.now().isoformat(), "by": by[:80]}
        league.sala = sala
        league.save(update_fields=["sala", "updated_at"])
    return lock_id


def unlock(league, lock_id=None):
    """Toglie il blocco. Con ``lock_id`` solo se è proprio quel blocco."""
    with transaction.atomic():
        league = League.objects.select_for_update().get(pk=league.pk)
        current = lock_info(league)
        if not current:
            return False
        if lock_id is not None and current.get("id") != lock_id:
            raise SalaError("Questo blocco non è più valido: la lega è stata sbloccata o riscaricata.")
        sala = dict(league.sala or {})
        sala.pop("lock", None)
        sala.pop("live", None)
        league.sala = sala
        league.save(update_fields=["sala", "updated_at"])
    return True


# --- sul sito: la lega che va in sala e i risultati che tornano ---------------

def snapshot(league):
    """La lega come la riceve il PC: impostazioni, squadre, listone con le rose."""
    return {
        "format": FORMAT,
        "exported_at": timezone.now().isoformat(),
        "league": {"id": league.id, **{f: _out(f, getattr(league, f)) for f in LEAGUE_FIELDS}},
        "teams": [
            {"id": p.id, **{f: _out(f, getattr(p, f)) for f in TEAM_FIELDS}}
            for p in Participant.objects.filter(league=league).order_by("display_name", "id")
        ],
        "players": [
            {"id": pl.id, "owner": pl.owner_id,
             **{f: _out(f, getattr(pl, f)) for f in PLAYER_FIELDS + ROSTER_FIELDS}}
            for pl in Player.objects.filter(league=league).order_by("role", "name", "id")
        ],
    }


def apply_results(league, lock_id, payload):
    """Applica lo stato finale mandato dal PC e sblocca la lega.

    Tutto o niente: un dato che non torna (squadra o giocatore di un'altra
    lega, blocco scaduto) non cambia nulla. Ritorna quante squadre e quanti
    giocatori sono cambiati.
    """
    check_ceilings(teams=payload.get("teams") or [], players=payload.get("players") or [],
                   log=payload.get("log") or [])
    with transaction.atomic():
        league = League.objects.select_for_update().get(pk=league.pk)
        current = lock_info(league)
        if not current or current.get("id") != lock_id:
            raise SalaError("Questo blocco non è più valido: la lega è stata sbloccata o riscaricata "
                            "dal sito, i risultati non si possono applicare.")
        teams = {p.id: p for p in Participant.objects.select_for_update().filter(league=league)}
        players = {p.id: p for p in Player.objects.select_for_update().filter(league=league)}

        changed_teams = []
        for t in payload.get("teams") or []:
            team = teams.get(t.get("id"))
            if team is None:
                raise SalaError(f"La squadra {t.get('id')!r} non è di questa lega.")
            new = {f: _in(f, t.get(f)) for f in ("credits", "spent_credits")}
            if any(v is None for v in new.values()):
                raise SalaError(f"Crediti mancanti per «{team.display_name}».")
            if any(getattr(team, f) != v for f, v in new.items()):
                for f, v in new.items():
                    setattr(team, f, v)
                changed_teams.append(team)

        changed_players = []
        for pl in payload.get("players") or []:
            player = players.get(pl.get("id"))
            if player is None:
                raise SalaError(f"Il giocatore {pl.get('id')!r} non è di questa lega.")
            owner = pl.get("owner")
            if owner is not None and owner not in teams:
                raise SalaError(f"«{player.name}»: la squadra {owner!r} non è di questa lega.")
            new = {f: _in(f, pl.get(f)) for f in ROSTER_FIELDS}
            if new["cost"] is None:
                new["cost"] = Decimal("0")
            if player.owner_id != owner or any(getattr(player, f) != v for f, v in new.items()):
                player.owner_id = owner
                for f, v in new.items():
                    setattr(player, f, v)
                changed_players.append(player)

        Participant.objects.bulk_update(changed_teams, ["credits", "spent_credits"])
        Player.objects.bulk_update(changed_players, ["owner"] + list(ROSTER_FIELDS), batch_size=500)

        actions = set(RosterLog.Action.values)
        RosterLog.objects.bulk_create([
            RosterLog(
                participant=teams.get(e.get("team")),
                participant_name=str(e.get("team_name") or "")[:80],
                player_name=str(e.get("player_name") or "")[:120],
                player_role=str(e.get("player_role") or "")[:1],
                action=e.get("action") if e.get("action") in actions else RosterLog.Action.EDIT,
                credits_delta=_in("credits", e.get("credits_delta")) or Decimal("0"),
                by_admin=bool(e.get("by_admin")),
                note=("Asta in sala" + (f" · {e['note']}" if e.get("note") else ""))[:200],
            )
            for e in payload.get("log") or []
        ])

        sala = dict(league.sala or {})
        sala.pop("lock", None)
        sala.pop("live", None)
        sala["last_results"] = {"at": timezone.now().isoformat(), "teams": len(changed_teams),
                                "players": len(changed_players)}
        league.sala = sala
        league.save(update_fields=["sala", "updated_at"])
    return {"teams": len(changed_teams), "players": len(changed_players),
            "log": len(payload.get("log") or [])}


# --- sul sito: l'asta in sala raggiungibile da internet ------------------------

def set_live(league, lock_id, url, teams):
    """L'indirizzo dell'asta in sala (il tunnel del PC) e, per ogni squadra del
    sito, il suo codice sul PC: chi gioca da fuori entra dall'app del sito.
    ``url`` None: l'asta non è raggiungibile (tunnel chiuso)."""
    with transaction.atomic():
        league = League.objects.select_for_update().get(pk=league.pk)
        current = lock_info(league)
        if not current or current.get("id") != lock_id:
            raise SalaError("Questo blocco non è più valido: la lega è stata sbloccata o riscaricata.")
        sala = dict(league.sala or {})
        if url:
            url = _tunnel_url(url)
            if not url:
                raise SalaError("L'indirizzo dell'asta deve essere quello del tunnel del PC "
                                "(https://….trycloudflare.com): aggiorna FantaManager sul PC e riattiva "
                                "l'accesso da internet.")
            own = set(Participant.objects.filter(league=league).values_list("id", flat=True))
            sala["live"] = {
                "url": url,
                "teams": {str(k): str(v)[:64] for k, v in (teams or {}).items()
                          if str(k).isdigit() and int(k) in own and v},
                "at": timezone.now().isoformat(),
            }
        else:
            sala.pop("live", None)
        league.sala = sala
        league.save(update_fields=["sala", "updated_at"])


def _tunnel_url(url):
    """``url`` se è esattamente l'indirizzo di un tunnel del PC
    (``https://<nome>.trycloudflare.com``, ``remote._URL_RE``), altrimenti ""."""
    from ..remote import _URL_RE
    url = str(url or "").strip().rstrip("/")
    return url if _URL_RE.fullmatch(url) else ""


def live_info(league):
    return (league.sala or {}).get("live") if league is not None else None


def live_entry_url(participant):
    """Dove entra questa squadra nell'asta in sala, o None se non c'è."""
    if participant is None or participant.league_id is None:
        return None
    live = live_info(League.objects.filter(pk=participant.league_id).first())
    token = (live or {}).get("teams", {}).get(str(participant.id))
    base = _tunnel_url((live or {}).get("url"))
    return f"{base}/join/?{urlencode({'t': token})}" if token and base else None


# --- sul PC: la copia della lega e i risultati da rimandare -------------------

def link_info(league):
    """Il collegamento al sito di una lega scaricata sul PC, o None."""
    return (league.sala or {}).get("link") if league is not None else None


@transaction.atomic
def check_ceilings(*, teams=(), players=(), log=()):
    """``SalaError`` se squadre, giocatori o voci del registro superano i tetti."""
    for items, ceiling, what in ((teams, MAX_TEAMS, "squadre"), (players, MAX_PLAYERS, "giocatori"),
                                 (log, MAX_LOG_ENTRIES, "voci del registro")):
        if not isinstance(items, (list, tuple)):
            raise SalaError(f"Dati non validi: l'elenco delle {what} non è una lista.")
        if len(items) > ceiling:
            raise SalaError(f"Troppe {what} ({len(items)}, al massimo {ceiling}): non è una lega "
                            "di FantaManager. Controlla l'indirizzo del sito e la chiave.")


def import_linked_league(snap, *, site, key, lock_id, owner=None):
    """Crea sul PC la copia della lega scaricata, collegata al sito."""
    if not isinstance(snap, dict) or snap.get("format") != FORMAT:
        raise SalaError("Il sito usa un formato diverso: aggiorna FantaManager su entrambi.")
    check_ceilings(teams=snap.get("teams") or [], players=snap.get("players") or [])
    lg = snap.get("league") or {}
    league = League.objects.create(
        owner=owner,
        **{f: _in(f, lg[f]) for f in LEAGUE_FIELDS if f in lg and lg[f] is not None},
    )
    team_map = {}
    for t in snap.get("teams") or []:
        team = Participant.objects.create(
            league=league, access_code=generate_access_code(),
            **{f: _in(f, t[f]) for f in TEAM_FIELDS if f in t and t[f] is not None},
        )
        team_map[t["id"]] = team
    made = []
    remote_ids = []
    for pl in snap.get("players") or []:
        fields = {f: _in(f, pl[f]) for f in PLAYER_FIELDS + ROSTER_FIELDS if f in pl and pl[f] is not None}
        made.append(Player(league=league, owner=team_map.get(pl.get("owner")), **fields))
        remote_ids.append(pl["id"])
    Player.objects.bulk_create(made, batch_size=500)
    # PostgreSQL e SQLite recente rimettono gli id negli oggetti; altrimenti
    # si rileggono nell'ordine di creazione (la lega è nuova, sono solo questi).
    local_ids = [p.pk for p in made]
    if not all(local_ids):
        local_ids = list(Player.objects.filter(league=league).order_by("id").values_list("id", flat=True))
    league.sala = {"link": {
        "site": site.rstrip("/"), "key": key, "remote_league": lg.get("id"), "lock_id": lock_id,
        "downloaded_at": timezone.now().isoformat(),
        "teams": {str(t.id): rid for rid, t in team_map.items()},
        "players": {str(lid): rid for lid, rid in zip(local_ids, remote_ids)},
    }}
    league.save(update_fields=["sala", "updated_at"])
    return league


def results_payload(league):
    """Lo stato finale di rose e crediti della copia, con gli id del sito."""
    link = link_info(league)
    if not link:
        raise SalaError("Questa lega non è collegata a un sito.")
    team_ids = {int(k): v for k, v in link["teams"].items()}
    player_ids = {int(k): v for k, v in link["players"].items()}
    teams = Participant.objects.filter(league=league, id__in=team_ids)
    since = datetime.fromisoformat(link["downloaded_at"])
    return {
        "lock_id": link["lock_id"],
        "teams": [{"id": team_ids[t.id], "credits": str(t.credits), "spent_credits": str(t.spent_credits)}
                  for t in teams],
        "players": [
            {"id": player_ids[p.id], "owner": team_ids.get(p.owner_id),
             **{f: _out(f, getattr(p, f)) for f in ROSTER_FIELDS}}
            for p in Player.objects.filter(league=league, id__in=player_ids)
        ],
        "log": [
            {"team": team_ids.get(r.participant_id), "team_name": r.participant_name,
             "player_name": r.player_name, "player_role": r.player_role, "action": r.action,
             "credits_delta": str(r.credits_delta), "by_admin": r.by_admin, "note": r.note}
            for r in RosterLog.objects.filter(participant__league=league, created_at__gte=since)
                                      .order_by("created_at", "id")
        ],
    }


def mark_sent(league, report):
    sala = dict(league.sala or {})
    link = dict(sala.get("link") or {})
    link.update({"sent_at": timezone.now().isoformat(), "lock_id": None, "report": report})
    sala["link"] = link
    league.sala = sala
    league.save(update_fields=["sala", "updated_at"])


# --- sul PC: le chiamate al sito ---------------------------------------------

API_PATH = "/api/sala/v1/"


def _read_capped(resp):
    """Il corpo della risposta, letto a pezzi fino a ``MAX_RESPONSE_BYTES``."""
    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise SalaError("La risposta del sito è troppo grande per una lega: controlla "
                                "l'indirizzo del sito.")
            chunks.append(chunk)
    finally:
        resp.close()
    return b"".join(chunks)


def _call(site, key, action, data=None, timeout=30):
    """POST a ``<site>/api/sala/v1/<action>/`` con la chiave; il JSON di
    risposta, o ``SalaError`` con un messaggio da mostrare.

    Solo dall'app del PC: sul server sarebbe una richiesta verso un indirizzo
    scelto da chi compila il form. Anche sul PC solo verso indirizzi pubblici
    (``standings.url_is_fetchable``), senza seguire redirect, leggendo al più
    ``MAX_RESPONSE_BYTES``.
    """
    import requests

    from ..providers.standings import url_is_fetchable

    if not getattr(settings, "DESKTOP_APP", False):
        raise SalaError(PC_ONLY_MESSAGE)
    site = (site or "").strip().rstrip("/")
    if not site.startswith(("http://", "https://")):
        raise SalaError("Indirizzo del sito non valido: deve iniziare con http:// o https://.")
    url = f"{site}{API_PATH}{action}/"
    if not url_is_fetchable(url, _resolve):
        raise SalaError("Questo indirizzo non è un sito su internet (rete locale o indirizzo interno): "
                        "scrivi l'indirizzo pubblico del sito, per esempio https://fantamanager.example.it.")
    try:
        resp = requests.post(url, json=data or {}, timeout=timeout, allow_redirects=False, stream=True,
                             headers={"Authorization": f"Bearer {key}"})
    except requests.RequestException:
        raise SalaError("Il sito non risponde: controlla l'indirizzo e la connessione a internet.")
    if 300 <= resp.status_code < 400:
        resp.close()
        raise SalaError("Il sito ha risposto con un reindirizzamento: scrivi l'indirizzo esatto del "
                        "sito (con https:// e senza percorsi).")
    raw = _read_capped(resp)
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code == 401:
        raise SalaError("Chiave non valida: generane una nuova dalla pagina Impostazioni della lega sul sito.")
    if resp.status_code >= 400 or not body.get("ok"):
        raise SalaError(body.get("error") or f"Il sito ha risposto con un errore ({resp.status_code}).")
    return body


def connect(site, key, *, owner=None, force=False):
    """Scarica la lega dal sito (che la blocca) e ne crea la copia sul PC."""
    body = _call(site, key, "scarica", {"force": bool(force)})
    league = import_linked_league(body.get("snapshot"), site=site, key=key,
                                  lock_id=body.get("lock_id"), owner=owner)
    from .. import remote
    if remote.public_url():
        threading.Thread(target=_publish_one, args=(league, remote.public_url()), daemon=True).start()
    return league


def send_results(league):
    """Rimanda al sito rose e crediti finali; il sito li applica e si sblocca."""
    link = link_info(league)
    if not link or not link.get("lock_id"):
        raise SalaError("Non c'è niente da inviare: i risultati sono già stati inviati o la lega non è collegata.")
    body = _call(link["site"], link["key"], "risultati", results_payload(league))
    report = body.get("report") or {}
    mark_sent(league, report)
    return report


def release(league):
    """Sblocca la lega sul sito senza cambiare niente (asta annullata)."""
    link = link_info(league)
    if not link or not link.get("lock_id"):
        raise SalaError("La lega sul sito non è bloccata da questo PC.")
    _call(link["site"], link["key"], "sblocca", {"lock_id": link["lock_id"]})
    mark_sent(league, {"released": True})


# --- per le pagine -----------------------------------------------------------

def _when(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def view_info(league):
    """Lo stato «asta in sala» di una lega, pronto per la pagina Impostazioni."""
    s = league.sala or {}
    lock_ = s.get("lock") or {}
    link = s.get("link") or {}
    last = s.get("last_results") or {}
    return {
        "has_key": bool(s.get("key_hash")),
        "key_hint": s.get("key_hint", ""),
        "key_at": _when(s.get("key_at")),
        "locked": bool(lock_),
        "lock_at": _when(lock_.get("at")),
        "last_results_at": _when(last.get("at")),
        "last_results": last,
        "link": bool(link),
        "link_site": link.get("site", ""),
        "link_downloaded_at": _when(link.get("downloaded_at")),
        "link_open": bool(link.get("lock_id")),
        "link_sent_at": _when(link.get("sent_at")),
        "link_report": link.get("report") or {},
        "link_live_url": link.get("live_url", ""),
        "link_live_at": _when(link.get("live_at")),
        "link_live_error": link.get("live_error", ""),
        "live": bool(s.get("live")),
        "live_at": _when((s.get("live") or {}).get("at")),
        "live_teams": len((s.get("live") or {}).get("teams") or {}),
    }


def _open_links():
    """Le leghe del PC scaricate da un sito e non ancora rimandate."""
    return [lg for lg in League.objects.filter(sala__has_key="link") if (link_info(lg) or {}).get("lock_id")]


def _publish_one(league, url):
    link = link_info(league)
    tokens = dict(Participant.objects.filter(league=league).values_list("id", "public_token"))
    teams = {str(remote): tokens.get(int(local)) for local, remote in link["teams"].items()
             if tokens.get(int(local))}
    error = None
    try:
        _call(link["site"], link["key"], "live", {"lock_id": link["lock_id"], "url": url, "teams": teams},
              timeout=15)
    except SalaError as exc:
        error = str(exc)
        logger.warning("Asta in sala: indirizzo non comunicato a %s: %s", link["site"], exc)
    _record_live(league, url, error)
    return error


def _record_live(league, url, error):
    league.refresh_from_db()
    sala = dict(league.sala or {})
    current = dict(sala.get("link") or {})
    current.update({"live_url": url or "", "live_at": timezone.now().isoformat(), "live_error": error or ""})
    sala["link"] = current
    league.sala = sala
    league.save(update_fields=["sala", "updated_at"])


def publish_live(url, wait=False):
    """Sul PC: dice ai siti delle leghe scaricate dove si entra nell'asta
    (l'indirizzo del tunnel, None quando si chiude). Le chiamate partono in
    sottofondo: il tunnel non aspetta il sito. ``wait`` per i test."""
    leagues = _open_links()
    if not leagues:
        return
    if url and not _tunnel_url(url):
        # Solo l'indirizzo del tunnel del PC: il sito rifiuterebbe comunque.
        for league in leagues:
            _record_live(league, "", "L'indirizzo non è quello del tunnel del PC (https://….trycloudflare.com).")
        return
    def _run():
        for league in leagues:
            _publish_one(league, url)
    if wait:
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()
