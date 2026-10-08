"""Anagrafica comune dei calciatori: aggiornata da API-Football, collegata ai listoni.

L'anagrafica (:class:`Footballer`) è una sola per tutto FantaManager: si
aggiorna con una richiesta per club di Serie A (``/players/squads``, che non
dipende dalla stagione e quindi va bene anche col piano gratuito) e ogni lega
vi collega i giocatori del proprio listone. Il collegamento è per club e nome
("Martinez L." dell'Inter è "Lautaro Martínez" dell'Inter); nei casi dubbi
non si collega nulla, meglio un giocatore senza scheda che quella sbagliata.
"""
import logging
import threading
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from ..models import Footballer, Player
from ..providers.apifootball import _ascii
from ..providers import importers
from .sala import ensure_unlocked as _sala_guard

logger = logging.getLogger("auctions.footballers")

SYNC_KEY = "footballers:sync"
SYNC_STALE = timedelta(minutes=30)
_sync_lock = threading.Lock()
_sync_running = set()


# --- Aggiornamento da API-Football --------------------------------------------------

def _clubs(af, get, sleep, report):
    """[(id, nome, logo)] dei club da scaricare e se l'elenco è quello ufficiale."""
    teams = af.paced(af.serie_a_teams, get=get, sleep=sleep)
    if teams:
        return teams, True
    # Il piano non concede l'elenco della Serie A: si riconoscono i club
    # dalle squadre dei listoni delle leghe.
    names = sorted(set(Player.objects.exclude(team="").values_list("team", flat=True)))
    mapping = af.paced(af.italian_clubs, names, get=get, sleep=sleep) if names else {}
    report["unmatched_clubs"] = [n for n in names if n not in mapping]
    return sorted({(tid, tname, "") for tid, tname in mapping.values()}, key=lambda t: t[1]), False


def sync_registry(*, get=None, sleep=time.sleep, progress=None):
    """Aggiorna l'anagrafica con le rose attuali dei club di Serie A.

    Chi non compare più in nessuna rosa resta in anagrafica ma esce dalla
    Serie A (``in_serie_a=False``), solo se il giro è stato completo: un
    aggiornamento interrotto non fa sparire nessuno.
    """
    import requests

    from ..providers import apifootball as af

    get = get or requests.get
    report = {"clubs": 0, "players": 0, "created": 0, "updated": 0, "left": 0, "linked": 0, "leagues": 0,
              "unmatched_clubs": [], "error": ""}
    if not af.is_configured():
        report["error"] = "API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia"
        return report
    complete = False
    try:
        teams, official = _clubs(af, get, sleep, report)
        if not teams:
            raise af.ApiFootballError("nessun club di Serie A trovato su API-Football")
        left_today = af._limits["day"]
        if left_today is not None and left_today < len(teams):
            raise af.ApiFootballError(
                f"restano solo {left_today} richieste API-Football per oggi, ne servono {len(teams)}: riprova domani")
        existing = {f.api_id: f for f in Footballer.objects.all()}
        seen = set()
        for i, (club_id, club_name, club_logo) in enumerate(teams):
            if progress:
                progress(i, len(teams))
            players = af.paced(af.squad, club_id, get=get, sleep=sleep)
            now = timezone.now()
            new, changed = [], []
            for p in players:
                if p["id"] in seen:  # in due rose (prestito appena registrato): vale la prima
                    continue
                seen.add(p["id"])
                values = {
                    "name": p["name"][:120], "position": p["position"], "age": p["age"],
                    "number": p["number"], "photo_url": p["photo"][:200],
                    "club_api_id": club_id, "club_name": club_name[:120],
                    "club_logo": club_logo[:200], "in_serie_a": True, "seen_at": now,
                }
                f = existing.get(p["id"])
                if f is None:
                    new.append(Footballer(api_id=p["id"], role=p["position"], **values))
                    continue
                for field, value in values.items():
                    setattr(f, field, value)
                # Il ruolo corretto a mano dal superuser non si tocca più.
                if not f.role_edited and p["position"]:
                    f.role = p["position"]
                changed.append(f)
            # Salvato club per club: se il giro si ferma a metà, quel che ha preso resta.
            Footballer.objects.bulk_create(new)
            Footballer.objects.bulk_update(changed, ["name", "position", "role", "age", "number", "photo_url",
                                                     "club_api_id", "club_name", "club_logo",
                                                     "in_serie_a", "seen_at"])
            for f in new:
                existing[f.api_id] = f
            report["clubs"] += 1
            report["players"] += len(players)
            report["created"] += len(new)
            report["updated"] += len(changed)
        complete = official or not report["unmatched_clubs"]
    except af.ApiFootballError as exc:
        report["error"] = str(exc)
    if complete:
        report["left"] = (Footballer.objects.filter(in_serie_a=True).exclude(api_id__in=seen)
                          .update(in_serie_a=False))
    if report["players"]:
        report["linked"] = link_players()
        report["leagues"] = apply_default_listoni()
    logger.info("Anagrafica calciatori: %s club, %s giocatori (%s nuovi), %s usciti, %s collegati, "
                "%s leghe sulla lista generale (%s)", report["clubs"], report["players"], report["created"],
                report["left"], report["linked"], report["leagues"], report["error"])
    return report


def sync_summary(report):
    """Il resoconto di :func:`sync_registry` in una riga."""
    parts = []
    if report["clubs"]:
        parts.append(f"{report['players']} calciatori da {report['clubs']} club di Serie A "
                     f"({report['created']} nuovi)")
        if report["left"]:
            parts.append(f"{report['left']} non sono più in Serie A")
        parts.append(f"{report['linked']} giocatori dei listoni collegati")
        if report["leagues"]:
            parts.append(f"lista generale aggiornata in {report['leagues']} leghe")
    if report["unmatched_clubs"]:
        parts.append("club non riconosciuti su API-Football: " + ", ".join(report["unmatched_clubs"]))
    if report["error"]:
        parts.append(report["error"])
    return " · ".join(parts) or "nessun calciatore scaricato"


# --- Collegamento dei listoni ---------------------------------------------------------

def _name_parts(name):
    """Come nel listone, dopo aver ridotto le lettere che API-Football scrive
    in originale ("Yıldız", "Højlund") a lettere latine semplici."""
    return importers._name_parts(_ascii(name))


def _initials_ok(p_short, f_tokens):
    """Le iniziali del listone ("L." di "Martinez L.") non smentiscono il nome API."""
    if not p_short or not f_tokens:
        return True
    return any(o.startswith(s) or s.startswith(o) for s in p_short for o in f_tokens)


def _score(player, footballer):
    """Quanto ``footballer`` somiglia a ``player`` (None = non è lui)."""
    if importers._norm(_ascii(player.name)) == importers._norm(_ascii(footballer.name)):
        return 100
    p_long, p_short = _name_parts(player.name)
    f_long, f_short = _name_parts(footballer.name)
    common = p_long & f_long
    if not common:
        return None
    # Le iniziali si confrontano col resto del nome API: "L." va con "Lautaro"
    # o con "L.", non con "Josep".
    if not _initials_ok(p_short, (f_long - common) | f_short):
        return None
    score = 3 * len(common)
    if p_short and (f_long - common) | f_short:
        score += 1
    if footballer.position and footballer.position == player.role:
        score += 2
    return score


def _best(player, candidates):
    """Il candidato migliore, solo se non ce n'è un altro altrettanto buono."""
    scored = sorted(((s, f) for f in candidates if (s := _score(player, f)) is not None),
                    key=lambda sf: -sf[0])
    if not scored or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def link_players(players=None):
    """Collega all'anagrafica i giocatori dei listoni che non lo sono ancora.

    Prima fra i calciatori dello stesso club; chi non si trova lì (ha cambiato
    squadra e il listone non lo sa ancora) solo se in tutta la Serie A c'è un
    unico calciatore col suo cognome e il suo ruolo. Riempie anche la foto,
    se il giocatore non ne ha. Ritorna quanti ne ha collegati.
    """
    from ..providers.apifootball import same_club

    registry = list(Footballer.objects.filter(in_serie_a=True))
    if not registry:
        return 0
    qs = Player.objects.all() if players is None else players
    todo = list(qs.filter(footballer__isnull=True))
    by_team = {}
    linked = 0
    for player in todo:
        if player.team not in by_team:
            by_team[player.team] = [f for f in registry if player.team and same_club(f.club_name, player.team)]
        match = _best(player, by_team[player.team])
        if match is None:
            p_long, p_short = _name_parts(player.name)
            same = [f for f in registry if p_long and f.position == player.role
                    and (f_parts := _name_parts(f.name))[0] >= p_long
                    and _initials_ok(p_short, (f_parts[0] - p_long) | f_parts[1])]
            match = same[0] if len(same) == 1 else None
        if match is None:
            continue
        fields = {"footballer": match}
        if not player.photo_url and match.photo_url:
            fields["photo_url"] = match.photo_url
        Player.objects.filter(pk=player.pk).update(**fields)
        linked += 1
    return linked


# --- La lista generale come listone di default ----------------------------------------

def default_role(footballer):
    return footballer.role or footballer.position or "A"


def apply_default_listone(league):
    """Porta la lista generale nel listone di ``league``, se la lega non ne ha
    caricato uno suo (``own_listone``). Chi è già in rosa resta di chi è.

    Ogni calciatore della lista generale diventa (o resta) un giocatore della
    lega con nome, ruolo, ruoli Mantra e club della lista. Chi è uscito dalla
    Serie A lascia il listone se è svincolato; se è in una rosa viene segnalato
    come per un listone caricato (5.05). Ritorna None se non c'è niente da
    fare, altrimenti ``{"created", "updated", "removed"}``.
    """
    _sala_guard(league)
    from ..models import Auction
    from .abroad import flag_missing

    if league is None or league.own_listone:
        return None
    registry = list(Footballer.objects.filter(in_serie_a=True))
    if not registry:
        return None
    pool = Player.objects.filter(league=league)
    link_players(pool)
    by_footballer = {p.footballer_id: p for p in pool.filter(footballer__isnull=False)}
    new, changed = [], []
    for f in registry:
        values = {"name": f.name, "role": default_role(f), "mantra_roles": f.mantra_roles,
                  "team": f.club_name}
        p = by_footballer.get(f.id)
        if p is None:
            new.append(Player(league=league, footballer=f, photo_url=f.photo_url, **values))
        elif any(getattr(p, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(p, k, v)
            changed.append(p)
    Player.objects.bulk_create(new)
    Player.objects.bulk_update(changed, ["name", "role", "mantra_roles", "team"])

    gone = pool.filter(footballer__in_serie_a=False)
    on_block = Auction.objects.exclude(player__isnull=True).values("player_id")
    removed, _ = gone.filter(owner__isnull=True).exclude(id__in=on_block).delete()
    flag_missing(league, list(gone.filter(owner__isnull=False).values_list("name", flat=True)))
    return {"created": len(new), "updated": len(changed), "removed": removed}


def apply_default_listoni():
    """La lista generale in tutte le leghe che la usano. Ritorna quante."""
    from ..models import League

    done = 0
    from .sala import is_locked
    for league in League.objects.filter(own_listone=False):
        if is_locked(league):      # asta in sala: il listone si aggiorna dopo
            continue
        if apply_default_listone(league) is not None:
            done += 1
    return done


def edit_roles(footballer, role, mantra_roles):
    """Il superuser corregge i ruoli della lista generale: valgono subito in
    tutte le leghe che la usano, e gli aggiornamenti da API-Football non li
    toccano più. Ritorna ``(ok, messaggio)``."""
    from .. import mantra

    roles = mantra.parse_roles(mantra_roles)
    role = (role or "").strip().upper()[:1] or mantra.classic_role(roles)
    if role not in Footballer.Position.values:
        return False, "Ruolo non valido: scegli P, D, C o A."
    footballer.role = role
    footballer.mantra_roles = ";".join(roles)
    footballer.role_edited = True
    footballer.save(update_fields=["role", "mantra_roles", "role_edited", "updated_at"])
    Player.objects.filter(footballer=footballer, league__own_listone=False).update(
        role=role, mantra_roles=footballer.mantra_roles)
    shown = f"{role}" + (f" ({footballer.roles_display})" if roles else "")
    return True, f"{footballer.name}: ruolo {shown} nella lista generale."


def use_default_listone(league):
    """La lega lascia la sua lista e torna a quella generale."""
    league.own_listone = False
    league.save(update_fields=["own_listone"])
    return apply_default_listone(league)


def on_league_created(sender, instance, created, raw=False, **kwargs):
    """Una lega nuova parte con la lista generale, senza caricare file."""
    if created and not raw:
        apply_default_listone(instance)


# --- Aggiornamento in background --------------------------------------------------------

def sync_state():
    """Stato dell'ultimo aggiornamento: None, in corso o concluso."""
    state = cache.get(SYNC_KEY)
    if state and state.get("status") == "running" and timezone.now() - state["started_at"] > SYNC_STALE:
        state = {**state, "status": "interrupted"}
    return state


def start_sync():
    """Avvia l'aggiornamento in background (una ventina di richieste al ritmo
    del piano gratuito: qualche minuto). False se ce n'è già uno in corso."""
    with _sync_lock:
        if _sync_running:
            return False
        _sync_running.add(True)
    started = timezone.now()
    cache.set(SYNC_KEY, {"status": "running", "started_at": started, "done": 0, "total": 0}, 86400)

    def progress(done, total):
        cache.set(SYNC_KEY, {"status": "running", "started_at": started, "done": done, "total": total}, 86400)

    def run():
        try:
            report = sync_registry(progress=progress)
            cache.set(SYNC_KEY, {"status": "done", "started_at": started, "finished_at": timezone.now(),
                                 "error": report["error"], "summary": sync_summary(report)}, 30 * 86400)
        except Exception:
            logger.exception("Aggiornamento anagrafica calciatori non riuscito")
            cache.set(SYNC_KEY, {"status": "done", "started_at": started, "finished_at": timezone.now(),
                                 "error": "errore interno",
                                 "summary": "Aggiornamento non riuscito: errore interno (vedi i log)."}, 30 * 86400)
        finally:
            connection.close()  # la connessione di questo thread
            with _sync_lock:
                _sync_running.clear()

    threading.Thread(target=run, daemon=True).start()
    return True
