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
from .scoring import compute_giornata

logger = logging.getLogger("auctions.voti_live")

# Le fonti live possibili. Un valore sconosciuto (anche il vecchio
# "fantacalcio_web", ancora nei form salvati nei browser) ripiega su API-Football.
PROVIDERS = {"apifootball": "API-Football", "simulation": "Simulazione"}
DEFAULT_PROVIDER = "apifootball"


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



def fetch_apifootball_live(giornata_num: int) -> List[Dict[str, Any]]:
    """Live ratings and events of a Serie A matchday from API-Football.

    Raises ``apifootball.ApiFootballError`` (missing key, plan, limits) so the
    caller can report it instead of showing an empty, silent sync.
    """
    return apifootball.matchday_live_rows(giornata_num)


def fetch_simulation_live(giornata_num: int) -> List[Dict[str, Any]]:
    """Generate realistic live provisional ratings for testing and off-hours demonstration."""
    import random
    rows = []
    sample_players = list(Player.objects.filter(abroad_list=False).values("name", "team", "role")[:80])
    for p in sample_players:
        has_played = random.random() > 0.15
        role = p.get("role") or "A"
        if not has_played:
            vote = None
            goals = 0
            assists = 0
            yellow = False
            red = False
        else:
            base = Decimal(random.choice(["5.5", "6.0", "6.0", "6.5", "6.5", "7.0", "7.5", "5.0"]))
            vote = base
            goals = 1 if role in ("A", "C") and random.random() < 0.20 else 0
            assists = 1 if role in ("C", "D") and random.random() < 0.15 else 0
            yellow = random.random() < 0.18
            red = random.random() < 0.02
        rows.append({
            "name": p["name"],
            "team": p["team"],
            "role": role,
            "vote": vote,
            "goals": goals,
            "goals_conceded": 1 if role == "P" and random.random() < 0.5 else 0,
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
        self.active_giornata_num: Optional[int] = None
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
            "active_giornata_num": self.active_giornata_num,
            "history": list(self.history),
        }

    def start_background(self, interval: int = 60, provider: str = DEFAULT_PROVIDER):
        with self._lock:
            self.interval_seconds = max(15, interval)
            self.provider = normalize_provider(provider)
            self.is_enabled = True
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

    def _worker_loop(self):
        from django.db import connection
        while not self._stop_event.is_set():
            # Wait interval in small slices to be responsive to stop_event
            for _ in range(self.interval_seconds):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

            if self.is_enabled and not self._stop_event.is_set():
                try:
                    connection.close()
                    self.sync_now(is_provisional=True)
                except Exception as e:
                    logger.exception("Errore nel background worker LiveSync: %s", e)
                    self.last_status = "ERROR"
                    self.last_message = f"Errore durante sync automatico: {e}"
                    self._record_event("AUTO_SYNC", "ERROR", self.last_message)
                finally:
                    connection.close()

    def sync_now(self, giornata_num: Optional[int] = None, is_provisional: bool = True,
                 leagues=None) -> Dict[str, Any]:
        """Execute one live synchronization pass across all relevant active seasons.

        ``leagues`` limits it to those leagues (a league admin's button); None
        is every league (the Supervisor and the background worker)."""
        self.last_status = "SYNCING"
        now = timezone.now()

        # Find target giornata
        target_num = giornata_num or self.active_giornata_num
        if not target_num:
            # Auto-detect from first OPEN, LIVE or LOCKED giornata across active seasons
            candidate = Giornata.objects.filter(
                status__in=[Giornata.Status.OPEN, Giornata.Status.LOCKED, Giornata.Status.LIVE]
            ).order_by("number").first()
            if candidate:
                target_num = candidate.number
            else:
                target_num = 1

        self.active_giornata_num = target_num

        # Fetch ratings from provider
        self.provider = normalize_provider(self.provider)
        if self.provider == "simulation":
            rows = fetch_simulation_live(target_num)
        else:
            try:
                rows = fetch_apifootball_live(target_num)
            except apifootball.ApiFootballError as exc:
                self.last_status = "ERROR"
                self.last_message = f"API-Football: {exc}."
                self._record_event("SYNC", "ERROR", self.last_message, 0, target_num)
                logger.warning(self.last_message)
                return {"updated": 0, "status": "ERROR", "message": str(exc)}

        if not rows:
            self.last_status = "IDLE"
            self.last_message = f"Nessun dato live ricevuto per la Giornata {target_num}."
            self._record_event("SYNC", "IDLE", self.last_message, 0, target_num)
            return {"updated": 0, "status": "NO_DATA"}

        # Distribute updates across all current seasons
        total_updated = 0
        seasons = Season.objects.filter(is_current=True)
        if leagues is not None:
            seasons = seasons.filter(league__in=leagues)
        elif not seasons.exists():
            seasons = Season.objects.all()[:1]

        with transaction.atomic():
            for season in seasons:
                giornata, _ = Giornata.objects.get_or_create(
                    season=season, number=target_num,
                    defaults={"status": Giornata.Status.LIVE}
                )

                # Skip if already finalized official
                if giornata.status == Giornata.Status.SCORED and is_provisional:
                    continue

                pool = Player.objects.filter(league=season.league) if season.league else Player.objects.all()
                league_players = list(pool.select_related("footballer"))
                # API-Football rows carry the player's id: the registry link
                # (Player.footballer) identifies him exactly, names only as fallback.
                by_api_id = {pl.footballer.api_id: pl for pl in league_players if pl.footballer_id}
                claimed_ids = set()

                for r in rows:
                    p = by_api_id.get(r.get("api_id"))
                    if p is not None and p.pk in claimed_ids:
                        p = None
                    if p is None:
                        p = _find_match(r, league_players, claimed_ids)
                    if not p:
                        continue
                    claimed_ids.add(p.pk)

                    raw_vote = r.get("vote")
                    final_vote = round_live_vote(raw_vote) if (is_provisional and raw_vote is not None) else raw_vote

                    perf, _ = PlayerPerformance.objects.update_or_create(
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
                            "live_source": self.provider,
                            "live_updated_at": now,
                        }
                    )
                    total_updated += 1

                # Recompute participant live scores
                compute_giornata(giornata, mark_scored=(not is_provisional))

        self.last_sync_time = now
        self.last_status = "SUCCESS"
        self.last_updated_count = total_updated
        mode_str = "Provvisori (LIVE)" if is_provisional else "Ufficiali Definitivi"
        self.last_message = f"G{target_num} sincronizzata ({mode_str}): {total_updated} calciatori aggiornati con {self.provider}."
        self._record_event("SYNC", "SUCCESS", self.last_message, total_updated, target_num)
        logger.info(self.last_message)

        return {
            "status": "SUCCESS",
            "giornata": target_num,
            "total_updated": total_updated,
            "mode": mode_str,
            "timestamp": now.isoformat(),
        }

    def consolidate_official(self, giornata_num: int, leagues=None) -> Dict[str, Any]:
        """Convert provisional live performances into official finalized scores.

        ``leagues`` limits it to those leagues: a matchday number is not the
        same weekend everywhere, so one league closing its giornata 5 must not
        close everybody else's. None = every league (Supervisor)."""
        giornate = Giornata.objects.filter(number=giornata_num)
        if leagues is not None:
            giornate = giornate.filter(season__league__in=leagues)
        count = 0
        with transaction.atomic():
            for g in giornate:
                PlayerPerformance.objects.filter(giornata=g).update(
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
