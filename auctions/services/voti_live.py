"""Live Voti and Matchday Background Sync Service for FantaManager.

Handles real-time matchday synchronization (provisional ratings and in-game
events from API-Football, a licensed data provider), background auto-sync every
60 seconds, and consolidation into official final scores. The official votes of
a matchday still come from a file the league uploads (``services.voti``): no
web page of a third-party fantasy site is ever read.
"""
import logging
import threading
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from django.db import transaction
from django.utils import timezone

from ..models import Giornata, Player, PlayerPerformance, Season
from ..providers import apifootball
from ..providers.importers import _find_match
from . import voto_algo
from .scoring import compute_giornata

logger = logging.getLogger("auctions.voti_live")

# Le fonti live possibili. Un valore sconosciuto (anche il vecchio
# "fantacalcio_web", ancora nei form salvati nei browser) ripiega su API-Football.
PROVIDERS = {"apifootball": "API-Football", "simulation": "Simulazione"}
DEFAULT_PROVIDER = "apifootball"
# Attesa massima fra due passate del sync in background dopo errori di fila.
MAX_BACKOFF = 30 * 60


def normalize_provider(value: Optional[str]) -> str:
    return value if value in PROVIDERS else DEFAULT_PROVIDER


def _parse_dec_safe(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    s = str(val).replace(",", ".").strip()
    if not s or s.lower() in ("s.v.", "sv", "-", "*", "null", "none"):
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _parse_int_safe(val: Any) -> int:
    d = _parse_dec_safe(val)
    return int(d) if d is not None else 0


def round_live_vote(val: Any) -> Optional[Decimal]:
    """Round continuous live ratings (from live tables or Sofascore) to the nearest 0.5 step.

    Rules & Examples:
        7.2 -> 7.0
        6.8 -> 7.0
        6.4 -> 6.5
        6.6 -> 6.5
        6.1 -> 6.0
        7.3 -> 7.5
    """
    if val is None:
        return None
    try:
        f_val = float(str(val).replace(",", ".").strip())
    except Exception:
        return None
    rounded = round(f_val * 2) / 2
    return Decimal(str(rounded))



def fetch_apifootball_live(giornata_num: int, patient: bool = False) -> List[Dict[str, Any]]:
    """Live ratings and events of a Serie A matchday from API-Football.

    Raises ``apifootball.ApiFootballError`` (missing key, plan, limits) so the
    caller can report it instead of showing an empty, silent sync.
    ``patient``: the background sync may wait out the per-minute limit; a page
    never does (it says to retry in a minute).
    """
    if patient:
        return apifootball.matchday_live_rows(giornata_num, sleep=time.sleep)
    return apifootball.matchday_live_rows(giornata_num)


def simulation_allowed(user) -> bool:
    """Voti finti a caso: solo per le prove (DEBUG) o per il superadmin."""
    from django.conf import settings
    return bool(settings.DEBUG or getattr(user, "is_superuser", False))


def fetch_simulation_live(giornata_num: int, leagues=None) -> List[Dict[str, Any]]:
    """Generate realistic live provisional ratings for testing and off-hours demonstration.

    Le squadre di Serie A dei giocatori si affrontano a coppie: ogni riga ha
    minuti e risultato della squadra (``team_goals_for``/``team_goals_against``),
    così anche il voto algoritmico si prova senza chiave API. I giocatori sono
    solo quelli delle leghe ``leagues`` (None: le leghe con una stagione in
    corso), mai di altre leghe."""
    import random
    pool = Player.objects.filter(abroad_list=False)
    if leagues is not None:
        pool = pool.filter(league__in=leagues)
    else:
        pool = pool.filter(league__seasons__is_current=True).distinct()
    sample_players = list(pool.order_by("id").values("name", "team", "role")[:80])
    clubs = sorted({p["team"] or "" for p in sample_players})
    # Coppie di club; un club spaiato gioca contro un avversario fuori elenco.
    result = {}
    for i in range(0, len(clubs), 2):
        home = clubs[i]
        away = clubs[i + 1] if i + 1 < len(clubs) else None
        gh, ga = random.choice([0, 1, 1, 2, 2, 3]), random.choice([0, 0, 1, 1, 2])
        result[home] = (gh, ga)
        if away is not None:
            result[away] = (ga, gh)
    rows = []
    for p in sample_players:
        has_played = random.random() > 0.15
        role = p.get("role") or "A"
        gf, gc = result.get(p["team"] or "", (0, 0))
        if not has_played:
            vote = None
            minutes = 0
            goals = 0
            assists = 0
            yellow = False
            red = False
        else:
            base = Decimal(random.choice(["5.5", "6.0", "6.0", "6.5", "6.5", "7.0", "7.5", "5.0"]))
            vote = base
            minutes = 90 if role == "P" else random.choice([90, 90, 90, 90, 80, 70, 60, 30, 15])
            goals = 1 if role in ("A", "C") and gf and random.random() < 0.20 else 0
            assists = 1 if role in ("C", "D") and gf and random.random() < 0.15 else 0
            yellow = random.random() < 0.18
            red = random.random() < 0.02
        rows.append({
            "name": p["name"],
            "team": p["team"],
            "role": role,
            "vote": vote,
            "minutes": minutes,
            "team_goals_for": gf,
            "team_goals_against": gc,
            "goals": goals,
            "goals_conceded": gc if role == "P" and minutes else 0,
            "own_goals": 0,
            "pen_scored": 0,
            "pen_missed": 0,
            "pen_saved": 0,
            "assists": assists,
            "yellow": yellow,
            "red": red,
        })
    return rows


class LiveSyncManager:
    """Thread-safe singleton managing real-time live matchday polling."""
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.is_enabled: bool = False
        self.interval_seconds: int = 60
        self.provider: str = DEFAULT_PROVIDER   # 'apifootball' | 'simulation'
        self.last_sync_time: Optional[timezone.datetime] = None
        self.last_status: str = "IDLE"           # 'IDLE' | 'SUCCESS' | 'ERROR' | 'SYNCING'
        self.last_message: str = "In attesa di attivazione o sincronizzazione."
        self.last_updated_count: int = 0
        # Giornata fissata dal Supervisor per il sync in background (None = per
        # ogni lega quella che sta giocando) e l'ultima sincronizzata.
        self.active_giornata_num: Optional[int] = None
        self.last_giornata_num: Optional[int] = None
        # Errori di fila del sync in background: l'attesa raddoppia (fino a MAX_BACKOFF).
        self.failures: int = 0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.history: List[Dict[str, Any]] = []

    @classmethod
    def get_instance(cls) -> "LiveSyncManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = LiveSyncManager()
            return cls._instance

    def _record_event(self, action: str, status: str, message: str, count: int = 0, giornata_num: Optional[int] = None):
        entry = {
            "time": timezone.now().strftime("%d/%m/%Y %H:%M:%S"),
            "action": action,
            "status": status,
            "message": message,
            "count": count,
            "giornata": giornata_num or self.active_giornata_num,
            "provider": self.provider,
        }
        self.history.insert(0, entry)
        if len(self.history) > 50:
            self.history = self.history[:50]

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_enabled": self.is_enabled,
            "interval_seconds": self.interval_seconds,
            "provider": self.provider,
            "last_sync_time": self.last_sync_time.strftime("%d/%m/%Y %H:%M:%S") if self.last_sync_time else "Mai",
            "last_status": self.last_status,
            "last_message": self.last_message,
            "last_updated_count": self.last_updated_count,
            "active_giornata_num": self.active_giornata_num or self.last_giornata_num,
            "next_wait_seconds": self._wait_seconds(),
            "history": list(self.history),
        }

    def start_background(self, interval: int = 60, provider: str = DEFAULT_PROVIDER):
        with self._lock:
            self.interval_seconds = max(15, interval)
            self.provider = normalize_provider(provider)
            self.is_enabled = True
            self.failures = 0
            self._stop_event.clear()
            self._record_event("START", "SUCCESS", f"Polling live avviato (ogni {self.interval_seconds}s con {self.provider}).")

            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="LiveVotiWorker")
                self._thread.start()
                logger.info("LiveSyncManager background thread avviato (intervallo: %ss, provider: %s)", self.interval_seconds, self.provider)

    def stop_background(self):
        with self._lock:
            self.is_enabled = False
            self._stop_event.set()
            self._record_event("STOP", "IDLE", "Polling live arrestato dall'amministratore.")
            logger.info("LiveSyncManager background thread arrestato.")

    def _wait_seconds(self) -> int:
        """Attesa prima della prossima passata: l'intervallo, raddoppiato a ogni
        errore di fila (un'API ferma non si interroga ogni minuto, il piano
        gratuito ha 100 richieste al giorno)."""
        return min(self.interval_seconds * (2 ** min(self.failures, 6)), max(self.interval_seconds, MAX_BACKOFF))

    def _worker_loop(self):
        from django.db import connection
        while not self._stop_event.is_set():
            # Wait interval in small slices to be responsive to stop_event
            for _ in range(self._wait_seconds()):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

            if self.is_enabled and not self._stop_event.is_set():
                try:
                    connection.close()
                    res = self.sync_now(giornata_num=self.active_giornata_num, is_provisional=True,
                                        background=True)
                    self.failures = self.failures + 1 if res.get("status") == "ERROR" else 0
                except Exception as e:
                    logger.exception("Errore nel background worker LiveSync: %s", e)
                    self.failures += 1
                    self.last_status = "ERROR"
                    self.last_message = f"Errore durante sync automatico: {e}"
                    self._record_event("AUTO_SYNC", "ERROR", self.last_message)
                finally:
                    connection.close()

    def _plan(self, giornata_num, leagues, serie_a_round, is_provisional, background):
        """[(season, giornata, giornata di Serie A)] da sincronizzare, lega per lega.

        Con ``giornata_num`` la giornata con quel numero (creata se manca, come
        sempre); senza, quella che la lega sta giocando (bloccata o live, se no
        la prossima aperta). Ogni lega chiede all'API la *sua* giornata di
        Serie A (``serie_a_matchday``): una lega partita dopo non è allineata.
        In automatico (``background``) non si toccano le giornate con i
        punteggi scritti a mano. Ritorna anche le leghe saltate e perché."""
        from ..models import GiornataScore

        seasons = Season.objects.filter(is_current=True).select_related("league")
        if leagues is not None:
            seasons = seasons.filter(league__in=leagues)
        elif not seasons.exists():
            seasons = Season.objects.all()[:1]
        plan, skipped = [], []
        for season in seasons:
            if giornata_num:
                giornata, _ = Giornata.objects.get_or_create(
                    season=season, number=giornata_num, defaults={"status": Giornata.Status.LIVE})
            else:
                giornate = season.giornate.order_by("number")
                giornata = (giornate.filter(status__in=[Giornata.Status.LIVE, Giornata.Status.LOCKED]).first()
                            or giornate.filter(status=Giornata.Status.OPEN).first())
                if giornata is None:
                    continue
            if giornata.status == Giornata.Status.SCORED and is_provisional:
                continue
            if background and GiornataScore.objects.filter(giornata=giornata, breakdown__manual=True).exists():
                skipped.append(f"{season.league or season}: punteggi a mano")
                continue
            plan.append((season, giornata, serie_a_round or giornata.serie_a_matchday or giornata.number))
        return plan, skipped

    def _fetch(self, rounds, leagues, background):
        """Le righe live di ogni giornata di Serie A, una chiamata per giornata.
        Ritorna ``(righe per giornata, errori per giornata)``."""
        rows_by_round, errors = {}, {}
        for rnd in sorted(rounds):
            if self.provider == "simulation":
                rows_by_round[rnd] = fetch_simulation_live(rnd, leagues=leagues)
                continue
            try:
                rows_by_round[rnd] = fetch_apifootball_live(rnd, patient=background)
            except apifootball.ApiFootballError as exc:
                errors[rnd] = str(exc)
                if apifootball.outage():
                    # Ferma per tutti: inutile chiedere le altre giornate.
                    for other in rounds:
                        errors.setdefault(other, str(exc))
                    break
        return rows_by_round, errors

    def sync_now(self, giornata_num: Optional[int] = None, is_provisional: bool = True,
                 leagues=None, serie_a_round: Optional[int] = None,
                 background: bool = False) -> Dict[str, Any]:
        """Una passata di voti live su tutte le leghe con una stagione in corso.

        ``leagues`` la limita a quelle leghe (il tasto dell'admin di una
        lega); None = tutte (Supervisor e sync in background).
        ``giornata_num``: la giornata di ogni lega con quel numero; None = la
        giornata che ogni lega sta giocando.
        ``serie_a_round``: la giornata di Serie A da chiedere, se non è quella
        della giornata (``Giornata.serie_a_matchday``).
        ``background``: il sync automatico. Può aspettare il limite al minuto
        dell'API e non tocca le giornate con i punteggi scritti a mano."""
        self.last_status = "SYNCING"
        now = timezone.now()
        self.provider = normalize_provider(self.provider)

        plan, skipped = self._plan(giornata_num, leagues, serie_a_round, is_provisional, background)
        shown_num = giornata_num or (plan[0][1].number if plan else None)
        self.last_giornata_num = shown_num
        if not plan and not skipped and self.provider != "simulation" and not apifootball.is_configured():
            # Niente da fare, ma la chiave che manca va detta (non un silenzio).
            return self._fail("API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia",
                              giornata_num, hint=True)
        if not plan:
            self.last_status = "IDLE"
            self.last_message = ("Nessuna giornata da sincronizzare"
                                 + (f" ({'; '.join(skipped)})" if skipped else "") + ".")
            self._record_event("SYNC", "IDLE", self.last_message, 0, shown_num)
            return {"updated": 0, "status": "NO_DATA", "giornata": shown_num}

        rows_by_round, errors = self._fetch({rnd for _s, _g, rnd in plan}, leagues, background)
        if errors and not any(rows_by_round.values()):
            return self._fail(next(iter(errors.values())), shown_num, hint=True)
        if not any(rows_by_round.values()):
            self.last_status = "IDLE"
            self.last_message = f"Nessun dato live ricevuto per la Giornata {shown_num}."
            self._record_event("SYNC", "IDLE", self.last_message, 0, shown_num)
            return {"updated": 0, "status": "NO_DATA", "giornata": shown_num}

        total_updated = 0
        synced = []
        with transaction.atomic():
            for season, giornata, rnd in plan:
                rows = rows_by_round.get(rnd) or []
                if not rows:
                    continue
                total_updated += self._apply(season, giornata, rows, is_provisional, now)
                # Recompute participant live scores
                compute_giornata(giornata, mark_scored=(not is_provisional))
                synced.append(giornata)

        self.last_sync_time = now
        self.last_status = "SUCCESS"
        self.last_updated_count = total_updated
        mode_str = "Provvisori (LIVE)" if is_provisional else "Ufficiali Definitivi"
        where = (f"G{shown_num}" if giornata_num or len(synced) == 1
                 else f"{len(synced)} leghe")
        self.last_message = f"{where} sincronizzata ({mode_str}): {total_updated} calciatori aggiornati con {self.provider}."
        if errors:
            self.last_message += " Non riuscite: " + "; ".join(f"Serie A G{r}: {e}" for r, e in errors.items()) + "."
        if skipped:
            self.last_message += " Saltate: " + "; ".join(skipped) + "."
        self._record_event("SYNC", "SUCCESS", self.last_message, total_updated, shown_num)
        logger.info(self.last_message)

        return {
            "status": "SUCCESS",
            "giornata": shown_num,
            "total_updated": total_updated,
            "mode": mode_str,
            "timestamp": now.isoformat(),
            "errors": errors,
            "skipped": skipped,
        }

    def _fail(self, message, giornata_num, hint=False):
        self.last_status = "ERROR"
        self.last_message = f"API-Football: {message}." + (
            " Intanto i voti si caricano da file o i punteggi si scrivono a mano (scheda Voti)." if hint else "")
        self._record_event("SYNC", "ERROR", self.last_message, 0, giornata_num)
        logger.warning(self.last_message)
        return {"updated": 0, "status": "ERROR", "message": message, "giornata": giornata_num}

    def _apply(self, season, giornata, rows, is_provisional, now):
        """Scrive le righe live nella giornata di una lega. Ritorna quante."""
        # Una sola chiamata API per giornata: il voto si sceglie qui,
        # lega per lega, secondo la sua fonte del voto base.
        algo_rules = (voto_algo.algo_rules_for(season)
                      if voto_algo.is_algo_source(voto_algo.vote_source_for(season)) else None)
        pool = Player.objects.filter(league=season.league) if season.league else Player.objects.all()
        league_players = list(pool.select_related("footballer"))
        # API-Football rows carry the player's id: the registry link
        # (Player.footballer) identifies him exactly, names only as fallback.
        by_api_id = {pl.footballer.api_id: pl for pl in league_players if pl.footballer_id}
        claimed_ids = set()
        updated = 0
        for r in rows:
            p = by_api_id.get(r.get("api_id"))
            if p is not None and p.pk in claimed_ids:
                p = None
            if p is None:
                p = _find_match(r, league_players, claimed_ids)
            if not p:
                continue
            claimed_ids.add(p.pk)

            if algo_rules is not None:
                # Il ruolo è quello della lega (P/D/C/A, anche in Mantra), non dell'API.
                final_vote, detail = voto_algo.algo_vote(r, p.role, algo_rules)
                source = voto_algo.ALGO_LIVE_SOURCE
            else:
                raw_vote = r.get("vote")
                final_vote = round_live_vote(raw_vote) if (is_provisional and raw_vote is not None) else raw_vote
                detail, source = None, self.provider

            PlayerPerformance.objects.update_or_create(
                giornata=giornata,
                player=p,
                defaults={
                    "vote": final_vote,
                    "goals": r.get("goals", 0),
                    "assists": r.get("assists", 0),
                    "own_goals": r.get("own_goals", 0),
                    "pen_scored": r.get("pen_scored", 0),
                    "pen_missed": r.get("pen_missed", 0),
                    "pen_saved": r.get("pen_saved", 0),
                    "goals_conceded": r.get("goals_conceded", 0),
                    "yellow": r.get("yellow", False),
                    "red": r.get("red", False),
                    "is_live": is_provisional,
                    "live_source": source,
                    "live_updated_at": now,
                    "vote_detail": detail,
                }
            )
            updated += 1
        return updated

    def consolidate_official(self, giornata_num: int, leagues=None) -> Dict[str, Any]:
        """Convert provisional live performances into official finalized scores.

        ``leagues`` limits it to those leagues: a matchday number is not the
        same weekend everywhere, so one league closing its giornata 5 must not
        close everybody else's. None = every league (Supervisor).

        Una lega con il voto algoritmico definitivo chiude sui voti
        dell'algoritmo così come sono (restano ``live_source="algoritmico"``),
        senza aspettare un file."""
        giornate = Giornata.objects.filter(number=giornata_num).select_related("season")
        if leagues is not None:
            giornate = giornate.filter(season__league__in=leagues)
        count = 0
        with transaction.atomic():
            for g in giornate:
                perfs = PlayerPerformance.objects.filter(giornata=g)
                if voto_algo.vote_source_for(g.season) == "algoritmico":
                    perfs.filter(live_source=voto_algo.ALGO_LIVE_SOURCE).update(is_live=False)
                    perfs = perfs.exclude(live_source=voto_algo.ALGO_LIVE_SOURCE)
                perfs.update(
                    is_live=False,
                    live_source="official_consolidated"
                )
                compute_giornata(g, mark_scored=True)
                count += 1

        self.last_status = "SUCCESS"
        self.last_message = f"Giornata {giornata_num} consolidata ufficialmente ({count} leghe chiuse su dati definitivi)."
        self._record_event("CONSOLIDATE", "SUCCESS", self.last_message, count, giornata_num)
        logger.info(self.last_message)
        return {"status": "CONSOLIDATED", "giornate_count": count}
