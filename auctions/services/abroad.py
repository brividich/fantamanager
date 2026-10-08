"""Giocatori usciti dalla Serie A (regolamento 5.05, 5.06, 5.09).

Flusso:
1. All'import del listone ufficiale, chi è in una rosa ma non c'è più viene
   segnalato (``Player.left_serie_a_at``).
   Oppure «Controlla tutte le rose» li trova prima del listone, dai
   trasferimenti dei club di Serie A su API-Football.
2. Subito dopo l'import (e con "Rileva" / "Rileva tutti") si cerca da soli il
   club di destinazione (API-Football) e la sua posizione nel ranking UEFA,
   che si scarica da uefa.com se manca o ha più di un mese; l'admin conferma o
   corregge.
3. L'admin chiude il caso:
   * ceduto a un club UEFA / a un campionato extra-UEFA (ranking FIFA della
     nazione) → la squadra incassa il compenso della tabella 5.06 e perde il
     giocatore;
   * svincolato o ritirato → 0 FM;
   * messo nella **lista ceduti temporanei** (5.09, max 3): resta della squadra
     fuori dagli slot fino a fine contratto, nessun rinnovo; alla scadenza (o se
     la squadra lo svincola in sede d'asta) incassa il compenso;
   * falso allarme → la segnalazione sparisce.
In ogni caso la perdita conta per le estensioni del tetto salariale (3.02).
"""
import logging
import re
import threading
import time
import unicodedata
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from ..models import ContractEvent, Player, UefaClubRank
from .sala import ensure_unlocked as _sala_guard

logger = logging.getLogger("auctions.abroad")

# [posizione massima (None = oltre), {ruolo: FM}]
UEFA_TABLE = [
    [5, {"P": 100, "D": 100, "C": 250, "A": 500}],
    [12, {"P": 80, "D": 80, "C": 200, "A": 300}],
    [20, {"P": 50, "D": 50, "C": 150, "A": 200}],
    [30, {"P": 40, "D": 40, "C": 80, "A": 150}],
    [50, {"P": 30, "D": 30, "C": 50, "A": 80}],
    [100, {"P": 20, "D": 20, "C": 25, "A": 40}],
    [None, {"P": 10, "D": 10, "C": 15, "A": 20}],
]
FIFA_TABLE = [
    [5, {"P": 50, "D": 50, "C": 70, "A": 100}],
    [10, {"P": 40, "D": 40, "C": 50, "A": 60}],
    [20, {"P": 20, "D": 20, "C": 25, "A": 30}],
    [None, {"P": 10, "D": 10, "C": 15, "A": 20}],
]
LIST_SLOTS = 3
# Giocatori rilevati da soli dopo l'import del listone e con «Rileva tutti»
# (2-5 richieste API l'uno: il piano gratuito ne concede 100 al giorno).
AUTO_DETECT_LIMIT = 6
DETECT_ALL_LIMIT = 15
# Il ranking UEFA cambia durante la stagione: oltre questa età si riscarica.
UEFA_MAX_AGE_DAYS = 30
# Se uefa.com non risponde, il ranking vecchio si usa e si riprova più tardi.
UEFA_RETRY_SECONDS = 3600
_uefa_refresh_failed_at = None


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|ac|as|sc|afc|ssc|club|calcio|sk|fk|if)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def compensation(role, kind, position):
    """FM dovuti per un giocatore del ``role`` finito a un club in ``position``."""
    if kind == "free":
        return Decimal("0")
    table = UEFA_TABLE if kind == "uefa" else FIFA_TABLE
    for limit, amounts in table:
        # 5.05: fuori dalle prime 100 del ranking UEFA vale la soglia minima.
        if limit is None or (position and position <= limit):
            return Decimal(amounts.get(role, 0))
    return Decimal("0")


def uefa_position(club):
    """Posizione nel ranking UEFA del club, dal ranking salvato (None se ignota)."""
    key = _norm(club)
    if not key:
        return None
    exact = UefaClubRank.objects.filter(norm_name=key).first()
    if exact:
        return exact.position
    for rank in UefaClubRank.objects.all():
        if key in rank.norm_name or rank.norm_name in key:
            return rank.position
    return None


def store_uefa_ranking(rows):
    """``rows`` = [(nome, posizione, nazione)]: sostituisce il ranking salvato."""
    UefaClubRank.objects.all().delete()
    UefaClubRank.objects.bulk_create([
        UefaClubRank(name=name, norm_name=_norm(name), position=pos, country=country or "")
        for name, pos, country in rows
    ])
    return len(rows)


def flag_missing(league, names_missing):
    """Segnala i giocatori in rosa spariti dal listone; toglie la segnalazione a chi è tornato."""
    now = timezone.now()
    owned = Player.objects.filter(owner__league=league, abroad_list=False)
    flagged = owned.filter(name__in=names_missing, left_serie_a_at__isnull=True).update(left_serie_a_at=now)
    owned.exclude(name__in=names_missing).filter(
        left_serie_a_at__isnull=False, left_rank_kind="", left_club=""
    ).update(left_serie_a_at=None, left_club="", left_rank_pos=None)
    return flagged


def flag_player(player_id):
    """Segnalazione manuale (la notizia arriva prima del nuovo listone)."""
    player = Player.objects.filter(pk=player_id, owner__isnull=False).first()
    if player is None:
        return {"ok": False, "message": "Giocatore non trovato in nessuna rosa."}
    if player.left_serie_a_at is None:
        player.left_serie_a_at = timezone.now()
        player.save(update_fields=["left_serie_a_at"])
    return {"ok": True, "player_name": player.name}


def uefa_ranking_date():
    """Quando è stato salvato il ranking UEFA (None se non ce n'è uno)."""
    return UefaClubRank.objects.aggregate(saved=Max("updated_at"))["saved"]


def uefa_ranking_stale(saved_at, now=None):
    return saved_at is not None and (now or timezone.now()) - saved_at > timedelta(days=UEFA_MAX_AGE_DAYS)


def ensure_uefa_ranking(*, fetcher=None):
    """Scarica il ranking UEFA se manca o ha più di ``UEFA_MAX_AGE_DAYS`` giorni.

    Ritorna "" quando c'è un ranking aggiornato da usare, altrimenti una nota
    per l'admin. Se l'aggiornamento non riesce si continua col ranking vecchio
    e per un'ora non si riprova (uefa.com può metterci parecchio a non rispondere).
    """
    global _uefa_refresh_failed_at
    saved_at = uefa_ranking_date()
    stale = uefa_ranking_stale(saved_at)
    if saved_at is not None and not stale:
        return ""
    old_note = f"ranking UEFA del {timezone.localtime(saved_at):%d/%m/%Y} non aggiornato" if stale else ""
    if stale and _uefa_refresh_failed_at is not None \
            and time.monotonic() - _uefa_refresh_failed_at < UEFA_RETRY_SECONDS:
        return old_note
    from ..providers import uefa

    rows, reason = (fetcher or uefa.fetch)()
    if rows:
        store_uefa_ranking(rows)
        _uefa_refresh_failed_at = None
        logger.info("Ranking UEFA %s: %s club", "aggiornato" if stale else "scaricato", len(rows))
        return ""
    reason = reason or "ranking UEFA non disponibile"
    if stale:
        _uefa_refresh_failed_at = time.monotonic()
        logger.warning("Ranking UEFA del %s non aggiornato: %s", saved_at, reason)
        return f"{old_note} ({reason}): si usa quello salvato"
    return f"ranking UEFA da caricare a mano ({reason})"


def detect(player_id, *, finder=None, fetch_ranking=True):
    """Prova a riempire club di destinazione e posizione ranking (API-Football + UEFA).

    ``fatal`` nella risposta dice che l'API è ferma per tutti (chiave, limite,
    rete): chi rileva in blocco si ferma lì.
    """
    from ..providers.apifootball import ApiFootballError, lookup

    player = Player.objects.get(pk=player_id)
    try:
        if finder is not None:
            found, reason = finder(player.name, player.team), ""
        else:
            found, reason = lookup(player.name, player.team, player.role)
    except ApiFootballError as exc:
        return {"ok": False, "fatal": True, "player_name": player.name, "message": str(exc)}
    if not found:
        return {"ok": False, "player_name": player.name,
                "message": f"{player.name}: {reason or 'destinazione non trovata'}. Indicala a mano."}
    ranking_problem = ensure_uefa_ranking() if fetch_ranking else ""
    player.left_club = found["club"][:120]
    pos = uefa_position(found["club"])
    if pos:
        player.left_rank_kind, player.left_rank_pos = "uefa", pos
    player.save(update_fields=["left_club", "left_rank_kind", "left_rank_pos"])
    return {"ok": True, "player_name": player.name, "club": player.left_club, "position": pos,
            "ranking_problem": ranking_problem}


def pending_detection(league):
    """Segnalati di cui non si conosce ancora il club di destinazione."""
    return Player.objects.filter(owner__league=league, left_serie_a_at__isnull=False, left_club="")


def detect_all(league, *, limit=None, finder=None, fetcher=None):
    """Rileva club e ranking UEFA per tutti i segnalati ancora senza destinazione.

    ``limit`` tiene basso il numero di richieste (il piano gratuito di
    API-Football ne ha 100 al giorno); chi resta si rileva col pulsante.
    """
    from ..providers.apifootball import is_configured

    todo = list(pending_detection(league).order_by("left_serie_a_at", "name"))
    report = {"found": [], "missing": [], "left": 0, "error": "", "ranking_problem": ""}
    if not todo:
        return report
    if finder is None and not is_configured():
        report["error"] = "API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia"
        report["left"] = len(todo)
        return report
    report["ranking_problem"] = ensure_uefa_ranking(fetcher=fetcher)
    for i, player in enumerate(todo):
        if limit and i >= limit:
            report["left"] = len(todo) - i
            break
        res = detect(player.id, finder=finder, fetch_ranking=False)
        if res.get("fatal"):
            report["error"] = res["message"]
            report["left"] = len(todo) - i
            break
        report["found" if res["ok"] else "missing"].append(res)
    logger.info("Rilevamento usciti dalla Serie A, lega %s: %s trovati, %s no, %s in attesa (%s)",
                league.id, len(report["found"]), len(report["missing"]), report["left"], report["error"])
    return report


def detect_summary(report):
    """Il resoconto di :func:`detect_all` in una riga per l'admin."""
    parts = []
    for res in report["found"]:
        where = f"ranking UEFA {res['position']}°" if res.get("position") else "posizione nel ranking da indicare"
        parts.append(f"{res['player_name']} → {res['club']} ({where})")
    if report["missing"]:
        parts.append("non trovati: " + ", ".join(r["player_name"] for r in report["missing"]))
    if report["left"]:
        parts.append(f"{report['left']} ancora da rilevare")
    if report["error"]:
        parts.append(report["error"])
    if report["ranking_problem"]:
        parts.append(report["ranking_problem"])
    return " · ".join(parts)



# --- Controllo di tutte le rose (API-Football, per club) -----------------------------

ROSTER_CHECK_KEY = "abroad-roster-check:{}"
# Oltre questo tempo un controllo "in corso" è stato interrotto (riavvio del server).
ROSTER_CHECK_STALE = timedelta(minutes=20)
_roster_checks = set()
_roster_checks_lock = threading.Lock()


def check_all_rosters(league, *, get=None, sleep=time.sleep, progress=None, today=None):
    """Cerca su API-Football chi, fra i giocatori in rosa di tutte le squadre,
    ha lasciato la Serie A, senza aspettare il nuovo listone.

    Una richiesta per club di Serie A (più una o due per riconoscere i club):
    chi risulta andato via viene segnalato con club di destinazione e posizione
    nel ranking UEFA, pronto da confermare come dopo l'import del listone.
    """
    import requests

    from ..providers import apifootball as af

    get = get or requests.get
    report = {"clubs": 0, "players": 0, "found": [], "unmatched": [], "error": "", "ranking_problem": ""}
    owned = (Player.objects.filter(owner__league=league, abroad_list=False, left_serie_a_at__isnull=True)
             .exclude(team="").select_related("owner"))
    by_club = {}
    for player in owned:
        by_club.setdefault(player.team, []).append(player)
    if not by_club:
        return report
    if not af.is_configured():
        report["error"] = "API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia"
        return report
    listone_teams = set(
        Player.objects.filter(Q(league=league) | Q(league__isnull=True))
        .exclude(team="").values_list("team", flat=True)
    )
    listone_teams |= set(by_club)
    try:
        clubs = af.paced(af.italian_clubs, sorted(listone_teams), get=get, sleep=sleep)
        ids = {tid for tid, _ in clubs.values()}
        names = {af._norm_club(t) for t in listone_teams}

        def is_serie_a(team):
            return team.get("id") in ids or af._norm_club(team.get("name") or "") in names

        todo = [club for club in sorted(by_club) if club in clubs]
        report["unmatched"] = [club for club in sorted(by_club) if club not in clubs]
        left_today = af._limits["day"]
        if left_today is not None and left_today < len(todo):
            raise af.ApiFootballError(
                f"restano solo {left_today} richieste API-Football per oggi, ne servono {len(todo)}: riprova domani")
        for i, club in enumerate(todo):
            if progress:
                progress(i, len(todo))
            club_id, _ = clubs[club]
            entries = af.paced(af.club_transfers, club_id, get=get, sleep=sleep)
            report["clubs"] += 1
            report["players"] += len(by_club[club])
            gone = af.departures(entries, by_club[club], club_id, is_serie_a, today=today)
            now = timezone.now()
            for player in by_club[club]:
                if player.id in gone:
                    # Segnalato subito: se il controllo si ferma a metà, quel che ha trovato resta.
                    Player.objects.filter(pk=player.id, left_serie_a_at__isnull=True).update(
                        left_serie_a_at=now, left_club=gone[player.id]["club"][:120])
                    report["found"].append({"player_id": player.id, "player_name": player.name,
                                            "team": player.owner.display_name, "club": gone[player.id]["club"],
                                            "date": gone[player.id]["date"], "position": None})
    except af.ApiFootballError as exc:
        report["error"] = str(exc)
    if report["found"]:
        report["ranking_problem"] = ensure_uefa_ranking()
        for res in report["found"]:
            pos = uefa_position(res["club"])
            if pos:
                res["position"] = pos
                Player.objects.filter(pk=res["player_id"]).update(left_rank_kind="uefa", left_rank_pos=pos)
    logger.info("Controllo rose lega %s: %s club, %s giocatori, %s usciti (%s)", league.id, report["clubs"],
                report["players"], len(report["found"]), report["error"])
    return report


def roster_check_summary(report):
    """Il resoconto di :func:`check_all_rosters` in una riga per l'admin."""
    parts = [f"controllati {report['players']} giocatori di {report['clubs']} club di Serie A"]
    if report["found"]:
        for res in report["found"]:
            where = f"ranking UEFA {res['position']}°" if res.get("position") else "posizione nel ranking da indicare"
            parts.append(f"{res['player_name']} ({res['team']}) → {res['club']} ({where})")
    elif not report["error"]:
        parts.append("nessuno ha lasciato la Serie A")
    if report["unmatched"]:
        parts.append("club non riconosciuti su API-Football: " + ", ".join(report["unmatched"]))
    if report["error"]:
        parts.append(report["error"])
    if report["ranking_problem"]:
        parts.append(report["ranking_problem"])
    return " · ".join(parts)


def roster_check_state(league):
    """Stato dell'ultimo controllo delle rose: None, in corso o concluso."""
    state = cache.get(ROSTER_CHECK_KEY.format(league.id))
    if state and state.get("status") == "running" and \
            timezone.now() - state["started_at"] > ROSTER_CHECK_STALE:
        state = {**state, "status": "interrupted"}
    return state


def _spawn(fn, *args):
    def run():
        try:
            fn(*args)
        finally:
            connection.close()  # la connessione di questo thread
    threading.Thread(target=run, daemon=True).start()


def start_roster_check(league):
    """Avvia il controllo in background (serve qualche minuto per stare nel
    limite al minuto del piano gratuito). False se ce n'è già uno in corso."""
    with _roster_checks_lock:
        if league.id in _roster_checks:
            return False
        _roster_checks.add(league.id)
    key = ROSTER_CHECK_KEY.format(league.id)
    started = timezone.now()
    cache.set(key, {"status": "running", "started_at": started, "done": 0, "total": 0}, 86400)

    def progress(done, total):
        cache.set(key, {"status": "running", "started_at": started, "done": done, "total": total}, 86400)

    def run(league_id):
        from ..models import League
        try:
            report = check_all_rosters(League.objects.get(pk=league_id), progress=progress)
            cache.set(key, {"status": "done", "started_at": started, "finished_at": timezone.now(),
                            "found": len(report["found"]), "error": report["error"],
                            "summary": roster_check_summary(report)}, 7 * 86400)
        except Exception:
            logger.exception("Controllo rose lega %s non riuscito", league_id)
            cache.set(key, {"status": "done", "started_at": started, "finished_at": timezone.now(),
                            "found": 0, "error": "errore interno",
                            "summary": "Controllo non riuscito: errore interno (vedi i log)."}, 7 * 86400)
        finally:
            with _roster_checks_lock:
                _roster_checks.discard(league_id)

    _spawn(run, league.id)
    return True

def _lose(player, amount, note):
    from .salary import add_credits

    owner = player.owner
    league = owner.league
    _sala_guard(league)
    ContractEvent.objects.create(
        league=league, player=player, player_name=player.name, participant=owner,
        participant_name=owner.display_name, kind=ContractEvent.Kind.LEFT,
        season=league.season_number, by_admin=True, note=note[:200],
    )
    if amount:
        add_credits(owner, amount, f"Cessione fuori dalla Serie A: {player.name}")
    player.owner = None
    player.cost = Decimal("0")
    player.contract_years = None
    player.renewal_declared = None
    player.abroad_list = False
    player.abroad_compensation = None
    player.left_serie_a_at = None
    player.save()
    return amount


@transaction.atomic
def resolve(player_id, outcome, *, club="", position=None):
    """Chiude una segnalazione. ``outcome``: uefa / fifa / free / list / dismiss."""
    player = Player.objects.select_for_update(of=("self",)).select_related("owner", "owner__league").get(pk=player_id)
    if player.owner_id is None:
        return {"ok": False, "message": "Il giocatore non è in nessuna rosa."}
    if outcome == "dismiss":
        player.left_serie_a_at = None
        player.left_club, player.left_rank_kind, player.left_rank_pos = "", "", None
        player.save(update_fields=["left_serie_a_at", "left_club", "left_rank_kind", "left_rank_pos"])
        return {"ok": True, "amount": 0, "outcome": outcome}

    kind = player.left_rank_kind or "uefa"
    if outcome in ("uefa", "fifa", "free"):
        kind = outcome
    try:
        pos = int(position) if position not in (None, "") else player.left_rank_pos
    except (TypeError, ValueError):
        return {"ok": False, "message": "Posizione nel ranking non valida."}
    if kind == "uefa" and not pos and club:
        pos = uefa_position(club)
    club = club or player.left_club
    amount = compensation(player.role, kind, pos)
    label = {"uefa": f"ranking UEFA {pos or '101+'}°", "fifa": f"ranking FIFA {pos or '21+'}°",
             "free": "svincolato o ritirato"}[kind]

    if outcome == "list":
        listed = Player.objects.filter(owner=player.owner, abroad_list=True).count()
        if listed >= LIST_SLOTS:
            return {"ok": False, "message": f"La lista ceduti temporanei è piena ({LIST_SLOTS} posti)."}
        player.abroad_list = True
        player.abroad_compensation = amount
        player.left_club, player.left_rank_kind, player.left_rank_pos = club, kind, pos
        player.left_serie_a_at = None
        player.save()
        return {"ok": True, "amount": 0, "outcome": outcome, "deferred": int(amount)}

    _lose(player, amount, f"{club or 'destinazione non indicata'} · {label}")
    return {"ok": True, "amount": int(amount), "outcome": kind}



def priced_departures(league):
    """Segnalati con il compenso già calcolabile (tipo di ranking noto)."""
    return (Player.objects.filter(owner__league=league, left_serie_a_at__isnull=False,
                                  left_rank_kind__in=("uefa", "fifa", "free"))
            .select_related("owner").order_by("owner__display_name", "name"))


def resolve_priced(league):
    """Conferma in blocco le uscite con il compenso calcolato, come «Conferma»
    su ognuna: la squadra incassa e perde il giocatore. Le altre restano."""
    done, total = [], 0
    for player in list(priced_departures(league)):
        team = player.owner.display_name
        res = resolve(player.id, player.left_rank_kind, club=player.left_club, position=player.left_rank_pos)
        if res.get("ok"):
            done.append({"player_name": player.name, "team": team, "amount": res["amount"]})
            total += res["amount"]
    return {"ok": True, "done": done, "total": total}

@transaction.atomic
def release_from_list(player_id, *, participant_id=None):
    """5.09: svincolo dalla lista ceduti in sede d'asta → incassa il compenso."""
    player = Player.objects.select_for_update(of=("self",)).select_related("owner", "owner__league").get(pk=player_id)
    if not player.abroad_list or player.owner_id is None:
        return {"ok": False, "message": "Il giocatore non è nella lista ceduti."}
    if participant_id is not None and int(participant_id) != player.owner_id:
        return {"ok": False, "message": "Non è un tuo giocatore."}
    amount = player.abroad_compensation or Decimal("0")
    _lose(player, amount, f"Svincolato dalla lista ceduti ({player.left_club or 'estero'})")
    return {"ok": True, "amount": int(amount)}


def expire_listed(league):
    """A fine contratto i giocatori in lista si perdono con il loro compenso (5.09)."""
    done = []
    for p in Player.objects.filter(owner__league=league, abroad_list=True, contract_years=0).select_related("owner"):
        amount = p.abroad_compensation or Decimal("0")
        name = p.name
        _lose(p, amount, f"Fine contratto in lista ceduti ({p.left_club or 'estero'})")
        done.append((name, int(amount)))
    return done
