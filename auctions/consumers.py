"""WebSocket consumer for a single auction room."""
import asyncio
import json
import logging

from channels.consumer import get_handler_name
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from . import backup, health, services
from .models import Auction, Participant

logger = logging.getLogger(__name__)

# Ping threshold (ms) above which a latency warning is broadcast.
LATENCY_WARN_MS = 300
# Minimum seconds between latency warnings for the same participant.
LATENCY_WARN_COOLDOWN = 60

# Live reactions: the small allow-list of emoji a bidder can fling into the room
# (keeps the channel to lightweight table banter, never arbitrary payloads) and a
# per-connection cooldown so nobody can spam the big screen.
REACTION_ALLOWED = {"👍", "🔥", "😱", "😂", "💰", "👏", "😮", "🤡", "❤️", "🎉"}
REACTION_COOLDOWN = 0.4  # seconds between reactions from one connection

# Bids and envelopes from one connection: a small burst goes through (a double
# tap still gets its usual answer, "stai già vincendo" and the like), anything
# beyond it is answered here without touching the database. Each attempt that
# reaches place_bid takes the write lock, so a phone firing taps in a loop
# would otherwise slow every other team's bids down.
BID_BURST = 4             # attempts allowed back to back
# Offerte di un telefono nel server nello stesso momento: una in corso e una
# in attesa (il doppio tocco). Con il server indietro, i tocchi in più non
# si accodano per passare secondi dopo come rilanci che nessuno vuole più.
BID_QUEUE = 2
BID_REFILL_PER_SECOND = 4  # attempts regained per second


class _TokenBucket:
    def __init__(self, capacity, refill_per_second):
        self.capacity = capacity
        self.refill = refill_per_second
        self.tokens = float(capacity)
        self.stamp = None

    def take(self, now):
        if self.stamp is not None:
            self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.refill)
        self.stamp = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


_ROOM_TICKERS = {}
_ROOM_LOCK = asyncio.Lock()

# Lo stato dopo i rilanci: uno solo per raffica. Trenta squadre che premono
# insieme davano trenta stati completi in fila (una lettura del database e un
# invio a ogni telefono per ciascuno), e le offerte dietro aspettavano secondi.
# Ogni rilancio va subito a tutti (bid.new); lo stato parte una volta, poco
# dopo, con l'ultimo prezzo.
STATE_COALESCE_SECONDS = 0.05
_STATE_FLUSH = {}  # auction_id -> {"dirty": bool, "task": asyncio.Task | None}


def _request_state(auction_id, channel_layer, group_name):
    entry = _STATE_FLUSH.setdefault(auction_id, {"dirty": False, "task": None})
    entry["dirty"] = True
    if entry["task"] is None or entry["task"].done():
        entry["task"] = asyncio.create_task(_flush_state(auction_id, channel_layer, group_name, entry))


async def _flush_state(auction_id, channel_layer, group_name, entry):
    while entry["dirty"]:
        await asyncio.sleep(STATE_COALESCE_SECONDS)
        entry["dirty"] = False
        try:
            state = await _load_state(auction_id)
        except DatabaseError:
            # I telefoni lo richiedono comunque ogni 4 s (sync).
            logger.exception("auction %s: stato dopo i rilanci non letto", auction_id)
            continue
        await channel_layer.group_send(group_name, {"type": "state.update", "state": state})


@database_sync_to_async
def _load_state(auction_id):
    auction = Auction.objects.select_related(
        "best_bid", "best_bid__participant", "player", "league"
    ).filter(pk=auction_id).first()
    if auction is None:
        return {"type": "state", "status": "CLOSED"}
    return services.serialize_state(auction)


class RoomTicker:
    """Singleton background ticker for a single auction room.

    Runs exactly one async ticker loop per active auction room as long as at
    least one WebSocket client is connected, eliminating SQLite lock
    contention and duplicate tick processing.
    """
    _BACKUP_INTERVAL_SECONDS = 300

    def __init__(self, auction_id, channel_layer, group_name):
        self.auction_id = auction_id
        self.channel_layer = channel_layer
        self.group_name = group_name
        self.clients = 0
        self.task = None

    def start_if_needed(self):
        self.clients += 1
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run_ticker())

    def stop_if_empty(self):
        self.clients = max(0, self.clients - 1)
        if self.clients == 0 and self.task and not self.task.done():
            self.task.cancel()
            return True
        return False

    async def _broadcast_state(self, state=None):
        if state is None:
            state = await self._state()
        await self.channel_layer.group_send(
            self.group_name, {"type": "state.update", "state": state}
        )

    @database_sync_to_async
    def _state(self):
        auction = Auction.objects.select_related(
            "best_bid", "best_bid__participant", "player", "league"
        ).filter(pk=self.auction_id).first()
        if auction is None:
            return {"type": "state", "status": "CLOSED"}
        return services.serialize_state(auction)

    async def _run_ticker(self):
        interval = settings.TIMER_SYNC_INTERVAL_SECONDS
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._ticker_tick()
                    health.clear_ticker_error(self.auction_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "auction %s: ticker tick failed", self.auction_id
                    )
                    health.record_ticker_error(self.auction_id, exc)
        except asyncio.CancelledError:
            pass

    async def _ticker_tick(self):
        backup.backup_database_async(
            reason=backup.PERIODIC, min_interval=self._BACKUP_INTERVAL_SECONDS,
        )
        # Ogni chiamata dice quando il ticker ha chiesto: un'attesa in coda
        # dietro al database fermo non deve far scadere il lotto (stall.py).
        sealed = await database_sync_to_async(services.sealed_tick)(
            self.auction_id, as_of=timezone.now(),
        )
        if sealed is not None:
            await self._broadcast_state()

        closed = await database_sync_to_async(services.close_if_expired)(
            self.auction_id, as_of=timezone.now(),
        )
        if closed is not None:
            await self._broadcast_state()
            had_bid = await database_sync_to_async(services.lot_had_bid)(closed)
            await database_sync_to_async(services.finalize_expired)(
                self.auction_id
            )
            if had_bid and closed.auto_advances:
                pass
            else:
                await asyncio.sleep(max(0, closed.cycle_break_seconds))
                reset = await database_sync_to_async(services.reset_if_closed)(
                    self.auction_id
                )
                if reset is not None:
                    state = await database_sync_to_async(services.serialize_state)(
                        reset
                    )
                    await self._broadcast_state(state)
        else:
            stuck = await database_sync_to_async(services.stuck_closed_lot)(
                self.auction_id
            )
            if stuck is not None and not (
                await database_sync_to_async(services.lot_had_bid)(stuck)
                and stuck.auto_advances
            ):
                reset = await database_sync_to_async(services.reset_if_closed)(
                    self.auction_id
                )
                if reset is not None:
                    state = await database_sync_to_async(services.serialize_state)(
                        reset
                    )
                    await self._broadcast_state(state)


class AuctionConsumer(AsyncWebsocketConsumer):
    async def dispatch(self, message):
        """Come quello di channels, senza ``aclose_old_connections`` prima di
        ogni messaggio. Quello passa dal thread del database (lo stesso di
        offerte e ticker) anche per un'offerta degli altri o uno stato da
        girare al telefono: con il database in coda ogni telefono smetteva di
        ricevere, e in una raffica ogni messaggio a ogni telefono si metteva
        in fila con le offerte. Qui il database si tocca solo con
        ``database_sync_to_async``, che chiude già le connessioni vecchie
        prima e dopo ogni chiamata."""
        handler = getattr(self, get_handler_name(message), None)
        if handler is None:
            raise ValueError("No handler for message type %s" % message["type"])
        await handler(message)

    async def connect(self):
        self.auction_id    = int(self.scope["url_route"]["kwargs"]["auction_id"])
        self.group_name    = f"auction_{self.auction_id}"
        self.participant_id   = self.scope.get("session", {}).get("participant_id")
        self.participant_name = None
        self._last_latency_warn = 0  # monotonic seconds
        self._last_reaction = 0      # monotonic seconds
        self._bid_bucket = _TokenBucket(BID_BURST, BID_REFILL_PER_SECOND)
        self._bid_lock = asyncio.Lock()   # le offerte di questo telefono, una alla volta
        self._bids_queued = 0             # in corso più in attesa (vedi BID_QUEUE)
        self._tasks = set()               # offerte e sync in corso (vedi receive)
        self._sync_busy = False

        auction = await self._get_auction()
        if auction is None:
            await self.close()
            return

        if self.participant_id:
            p = await self._get_participant()
            if p:
                is_member = await database_sync_to_async(services.participates_in)(p, auction)
                if is_member:
                    self.participant_name = p.display_name
                else:
                    logger.warning(
                        "WebSocket: Participant #%s ('%s', league=%s) attempted to join auction #%s (league=%s) of a different league. Demoted to spectator.",
                        p.id, p.display_name, p.league_id, auction.id, auction.league_id,
                    )
                    self.participant_id = None
                    self.participant_name = None

        # Raggiungibile da internet (tunnel aperto, sito pubblico): chi non è una
        # squadra dell'asta entra solo se gestisce la lega o è il maxischermo
        # aperto col suo codice. In sala senza tunnel resta tutto com'era.
        if (getattr(settings, "PUBLIC_TOKENS_REQUIRED", False) and self.participant_name is None
                and not await self._may_watch(auction)):
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json(await self._state())
        await self._send_sealed_me()

        logger.info(
            "WebSocket connected: auction=%s, participant=%s, channel=%s",
            self.auction_id,
            self.participant_name or "spectator",
            self.channel_name,
        )

        async with _ROOM_LOCK:
            if self.auction_id not in _ROOM_TICKERS:
                _ROOM_TICKERS[self.auction_id] = RoomTicker(
                    self.auction_id, self.channel_layer, self.group_name
                )
            _ROOM_TICKERS[self.auction_id].start_if_needed()

    async def disconnect(self, code):
        logger.info(
            "WebSocket disconnected: auction=%s, participant=%s, code=%s",
            getattr(self, "auction_id", None),
            getattr(self, "participant_name", None) or "spectator",
            code,
        )
        async with _ROOM_LOCK:
            ticker = _ROOM_TICKERS.get(self.auction_id)
            if ticker:
                if ticker.stop_if_empty():
                    _ROOM_TICKERS.pop(self.auction_id, None)

        # Notify the room only if this was a participant connection.
        if self.participant_name and hasattr(self, "group_name"):
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "participant.offline",
                    "participant": self.participant_name,
                },
            )

        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    @database_sync_to_async
    def _may_watch(self, auction):
        """Chi può guardare l'asta senza essere una squadra: il maxischermo
        aperto col suo codice (screen.py lo segna nella sessione), lo staff,
        chi gestisce la lega."""
        if (self.scope.get("session") or {}).get(f"screen_ok_{auction.id}"):
            return True
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        from .views.common import user_can_manage_league
        return auction.league_id is not None and user_can_manage_league(user, auction.league)

    async def receive(self, text_data=None, bytes_data=None):
        # Quando il messaggio è arrivato: un'offerta conta da qui, anche se il
        # database la scrive più tardi (services/stall.py).
        received_at = timezone.now()
        # Anything a client sends is untrusted: a frame that is not a JSON
        # object is ignored instead of raising and dropping the connection.
        try:
            data = json.loads(text_data or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        action = data.get("action")

        # Offerte, buste e sync aspettano il database, che con tante offerte
        # insieme ha la coda: girano a parte, così intanto questa connessione
        # continua a ricevere le offerte degli altri e gli stati (altrimenti
        # le si accumulano e oltre 100 il canale le scarta). Le offerte di un
        # telefono restano in fila, una alla volta, nell'ordine in cui arrivano.
        if action in ("bid", "sealed_bid"):
            reason = self._early_reject()
            if reason:
                kind = "bid_rejected" if action == "bid" else "sealed_rejected"
                await self.send_json({"type": kind, "reason": reason})
                return
            self._bids_queued += 1   # subito: il prossimo tocco può arrivare prima che parta
            self._spawn(self._queued_bid(action, data, received_at))
        elif action == "sync":
            if not self._sync_busy:
                self._sync_busy = True
                self._spawn(self._handle_sync(data))
        elif action == "latency_warning":
            await self._handle_latency_warning(data)
        elif action == "reaction":
            await self._handle_reaction(data)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _early_reject(self):
        """Senza squadra o con troppi tocchi: si risponde subito, senza database.
        Il limite si conta all'arrivo, non quando l'offerta tocca il database."""
        if not self.participant_id:
            return "no_session"
        if self._bids_queued >= BID_QUEUE:
            return services.Reject.BID_PENDING
        if not self._bid_allowed():
            return services.Reject.RATE_LIMITED
        return None

    async def _queued_bid(self, action, data, received_at):
        try:
            async with self._bid_lock:
                if action == "bid":
                    await self._handle_bid(data, received_at)
                else:
                    await self._handle_sealed_bid(data, received_at)
        except DatabaseError as exc:
            # Il database non ha risposto (bloccato oltre l'attesa, disco
            # pieno...): la connessione resta su e la squadra sa cosa fare,
            # invece di vedersi cadere il telefono a metà lotto.
            logger.exception("auction %s: %s non riuscito sul database", self.auction_id, action)
            health.record_ticker_error(self.auction_id, exc)
            kind = "bid_rejected" if action == "bid" else "sealed_rejected"
            await self.send_json({"type": kind, "reason": services.Reject.SERVER_BUSY})
        except Exception:
            logger.exception("auction %s: %s non riuscito", self.auction_id, action)
        finally:
            self._bids_queued -= 1

    async def _handle_sync(self, data):
        try:
            state = await self._state()
            # The client numbers its syncs to time the round trip (and correct
            # its countdown by it): the answer carries the number back.
            sync_id = data.get("sync_id")
            if isinstance(sync_id, int) and not isinstance(sync_id, bool):
                state = {**state, "sync_id": sync_id}
            await self.send_json(state)
            await self._send_sealed_me()
        except DatabaseError:
            # Il prossimo sync arriva fra 4 s.
            logger.exception("auction %s: sync non riuscito sul database", self.auction_id)
        except Exception:
            logger.exception("auction %s: sync non riuscito", self.auction_id)
        finally:
            self._sync_busy = False

    # --- Bid handling -------------------------------------------------------

    def _bid_allowed(self):
        return self._bid_bucket.take(asyncio.get_event_loop().time())

    async def _handle_bid(self, data, received_at=None):
        """Squadra e limite dei tocchi li ha già guardati receive (_early_reject)."""
        increment = data.get("increment")
        meta = self.scope
        ip = None
        if meta.get("client"):
            ip = meta["client"][0]
        user_agent = ""
        for header, value in meta.get("headers", []):
            if header == b"user-agent":
                user_agent = value.decode("latin1", "ignore")
                break

        result = await database_sync_to_async(services.place_bid)(
            self.auction_id, self.participant_id, increment,
            user_agent=user_agent, ip_address=ip, received_at=received_at,
        )

        if result.accepted:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "bid.new",
                    "bid": services.serialize_bid(result.bid),
                    "extended": result.extended,
                },
            )
            _request_state(self.auction_id, self.channel_layer, self.group_name)
            await self.send_json(
                {"type": "bid_accepted", "bid": services.serialize_bid(result.bid)}
            )
        else:
            await self.send_json(
                {"type": "bid_rejected", "reason": result.reason}
            )

    # --- Asta alle buste ----------------------------------------------------

    async def _handle_sealed_bid(self, data, received_at=None):
        """La busta di questa squadra per il giro in corso.

        Passa dalla stessa socket dei rilanci, ma la risposta è personale: la
        cifra scritta non finisce nello stato che vedono tutti, altrimenti lo
        scrutinio segreto non sarebbe segreto. Alla stanza arriva solo il
        conteggio delle buste consegnate, dentro lo stato normale.
        """
        result = await database_sync_to_async(services.place_sealed_bid)(
            self.auction_id, self.participant_id, data.get("amount"),
            received_at=received_at,
        )
        if result.accepted:
            await self.send_json(
                {"type": "sealed_accepted", "amount": str(result.amount)}
            )
            await self._send_sealed_me()
            _request_state(self.auction_id, self.channel_layer, self.group_name)
        else:
            await self.send_json({"type": "sealed_rejected", "reason": result.reason})

    async def _send_sealed_me(self):
        """Lo stato dello scrutinio come lo vede questa squadra (busta propria
        inclusa). Nessun altro riceve questo messaggio."""
        if not self.participant_id:
            return
        payload = await self._sealed_me()
        if payload is not None:
            await self.send_json(payload)

    @database_sync_to_async
    def _sealed_me(self):
        auction = Auction.objects.select_related("player").filter(pk=self.auction_id).first()
        if auction is None:
            return None
        participant = Participant.objects.filter(pk=self.participant_id).first()
        data = services.sealed_status(auction, participant)
        data["type"] = "sealed_me"
        return data

    # --- Latency warning ----------------------------------------------------

    async def _handle_latency_warning(self, data):
        if not self.participant_name:
            return
        try:
            ping = int(float(data.get("ping", 0)))
        except (TypeError, ValueError, OverflowError):
            return
        if ping < LATENCY_WARN_MS:
            return

        now = asyncio.get_event_loop().time()
        if now - self._last_latency_warn < LATENCY_WARN_COOLDOWN:
            return
        self._last_latency_warn = now

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "participant.warning",
                "kind": "latency",
                "participant": self.participant_name,
                "ping": ping,
            },
        )

    # --- Live reactions -----------------------------------------------------

    async def _handle_reaction(self, data):
        emoji = data.get("emoji")
        if emoji not in REACTION_ALLOWED:
            return
        now = asyncio.get_event_loop().time()
        if now - self._last_reaction < REACTION_COOLDOWN:
            return
        self._last_reaction = now
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "reaction.new",
                "emoji": emoji,
                "participant": self.participant_name or "Ospite",
            },
        )

    # --- Group event handlers -----------------------------------------------

    async def bid_new(self, event):
        await self.send_json(
            {"type": "bid_new", "bid": event["bid"], "extended": event.get("extended", False)}
        )

    async def state_update(self, event):
        await self.send_json(event["state"])

    async def participant_offline(self, event):
        await self.send_json(
            {"type": "participant_offline", "participant": event["participant"]}
        )

    async def participant_warning(self, event):
        await self.send_json(
            {
                "type": "participant_warning",
                "kind": event["kind"],
                "participant": event["participant"],
                "ping": event.get("ping", 0),
            }
        )

    async def reaction_new(self, event):
        await self.send_json(
            {"type": "reaction", "emoji": event["emoji"], "participant": event["participant"]}
        )

    async def announcement(self, event):
        # Pushed from the admin HTTP view via the channel layer (the regista is
        # not a WS participant). ``level`` styles the banner (info / call).
        await self.send_json(
            {"type": "announcement", "text": event["text"], "level": event.get("level", "info")}
        )

    # --- Helpers ------------------------------------------------------------

    async def _broadcast_state(self):
        state = await self._state()
        await self.channel_layer.group_send(
            self.group_name, {"type": "state.update", "state": state}
        )

    # serialize_state touches best_bid, best_bid.participant, player and
    # league — without this every tick (every TIMER_SYNC_INTERVAL_SECONDS,
    # for every connected client) re-fetches each of those with a separate
    # query instead of one JOINed SELECT.
    _AUCTION_SELECT_RELATED = ("best_bid", "best_bid__participant", "player", "league")

    @database_sync_to_async
    def _get_auction(self):
        return Auction.objects.select_related(*self._AUCTION_SELECT_RELATED) \
            .filter(pk=self.auction_id).first()

    @database_sync_to_async
    def _get_participant(self):
        return Participant.objects.filter(pk=self.participant_id).first()

    @database_sync_to_async
    def _state(self):
        auction = Auction.objects.select_related(*self._AUCTION_SELECT_RELATED) \
            .filter(pk=self.auction_id).first()
        if auction is None:
            return {"type": "state", "status": "CLOSED"}
        return services.serialize_state(auction)

    async def send_json(self, payload):
        # Un telefono che se n'è appena andato (rete persa, pagina chiusa)
        # riceve ancora le offerte degli altri finché la stanza non lo sa:
        # non è un errore, il suo disconnect arriva subito dopo.
        try:
            await self.send(text_data=json.dumps(payload))
        except Exception:
            logger.debug("auction %s: messaggio a un telefono già uscito",
                         getattr(self, "auction_id", None), exc_info=True)
