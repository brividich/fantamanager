"""Auction queue (running order) management."""
import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from ..models import Auction, AuctionQueueItem, Player
from .sealed import _clear_sealed

# Per-role ordering for the two role-grouped call orders.
_ROLE_RANK_PDCA = {"P": 0, "D": 1, "C": 2, "A": 3}
_ROLE_RANK_ACDP = {"A": 0, "C": 1, "D": 2, "P": 3}
# Width of each per-role band of ``order`` values. Large enough that released
# players appended to a role never collide with the next role's band.
_QUEUE_BAND = 1_000_000
# Band reserved for the end-of-auction recovery round (AUCTION_END unsold policy).
_RECOVERY_BAND = _QUEUE_BAND * 10


def _is_role_grouped(call_order):
    return call_order in (Auction.CallOrder.PDCA, Auction.CallOrder.ACDP)


def _role_rank_map(call_order):
    if call_order == Auction.CallOrder.ACDP:
        return _ROLE_RANK_ACDP
    return _ROLE_RANK_PDCA


def _role_band(role, call_order):
    """Base ``order`` offset for ``role`` under the given call order.

    Role-grouped orders (PDCA / ACDP) reserve a wide band per role so released
    players can be appended to their own role; flat orders use a single band.
    """
    if not _is_role_grouped(call_order):
        return 0
    return _role_rank_map(call_order).get(role, 9) * _QUEUE_BAND


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def draw_letters(roles):
    """Sorteggia una lettera dell'alfabeto per ognuno dei ruoli passati.

    Il regolamento (§3.1 B) vuole che la lettura di ogni reparto parta da una
    lettera estratta a sorte, non sempre dalla A: cosi' i cognomi in fondo
    all'alfabeto non finiscono ogni anno all'asta a portafogli gia' vuoti.
    """
    return {role: random.choice(ALPHABET) for role in roles}


def _letter_key(name, letter):
    """Posizione di ``name`` in un alfabeto che comincia da ``letter``.

    Dopo la Z si riparte dalla A, quindi con la lettera M un cognome che inizia
    per B viene dopo uno che inizia per T - esattamente l'ordine in cui la sala
    legge il listone dopo l'estrazione.
    """
    first = (name or "").strip()[:1].upper()
    idx = ALPHABET.find(first)
    if idx < 0:
        # Cognomi che non iniziano per lettera latina (accenti, apostrofi): in
        # coda, senza far saltare l'ordinamento.
        return (1, 0, (name or "").lower())
    start = ALPHABET.find(letter.upper()) if letter else 0
    return (0, (idx - max(start, 0)) % 26, (name or "").lower())


def _ordered_free_agents(call_order, within_role_order, league=None, letters=None):
    """Free-agent players (owner is null) ordered per ``call_order``.

    For flat orders (ALPHA / RANDOM) the list is the final running order. For
    role-grouped orders (PDCA / ACDP) the list is sorted by the within-role
    tie-break only; ``build_queue`` applies the role banding on top. Scoped to
    ``league``'s pool; ``league=None`` selects the legacy/global pool.
    """
    players = list(Player.objects.filter(owner__isnull=True, league=league))
    if call_order == Auction.CallOrder.RANDOM:
        random.shuffle(players)
        return players
    if call_order == Auction.CallOrder.ALPHA:
        players.sort(key=lambda p: p.name.lower())
        return players

    # PDCA / ACDP — apply the within-role tie-break.
    if within_role_order == Auction.WithinRole.LETTER:
        letters = letters or {}
        players.sort(key=lambda p: _letter_key(p.name, letters.get(p.role, "A")))
        return players
    if within_role_order == Auction.WithinRole.RANDOM:
        random.shuffle(players)
    elif within_role_order == Auction.WithinRole.ALPHA:
        players.sort(key=lambda p: p.name.lower())
    else:  # QUOTA — quotazione high→low, name as a stable tie-break
        players.sort(key=lambda p: p.name.lower())
        players.sort(key=lambda p: -(p.initial_price or 0))
    return players


def build_queue(auction):
    """(Re)build the pending queue from the current free-agent pool.

    Honours ``call_order`` (PDCA / ACDP / alphabetical / random) and, for the
    role-grouped orders, the ``within_role_order`` tie-break. Players already on
    a roster are excluded; the player currently on the block (if any) is left
    untouched. Returns the number of pending items created.
    """
    auction.queue_items.filter(done=False).delete()
    # "Alfabetico da lettera estratta": si sorteggia adesso, una lettera per
    # ruolo, e si conserva sull'asta — la sala deve poterla vedere, e una
    # ricostruzione della coda a meta' asta non deve rimescolare l'ordine gia'
    # annunciato per i reparti ancora da fare.
    letters = auction.drawn_letters or {}
    if (auction.within_role_order == Auction.WithinRole.LETTER
            and _is_role_grouped(auction.call_order)):
        missing = [r for r in ("P", "D", "C", "A") if not letters.get(r)]
        if missing:
            letters = dict(letters)
            letters.update(draw_letters(missing))
            auction.drawn_letters = letters
            auction.save(update_fields=["drawn_letters", "updated_at"])
    players = _ordered_free_agents(
        auction.call_order, auction.within_role_order, auction.league, letters
    )
    current_id = auction.player_id

    if _is_role_grouped(auction.call_order):
        # Stable role order; within each role keep the within-role order above.
        rank = _role_rank_map(auction.call_order)
        players.sort(key=lambda p: rank.get(p.role, 9))

    # One INSERT and one UPDATE for the whole queue. Doing it player by player
    # meant ~500 round-trips inside a single write transaction, which on SQLite
    # holds the database long enough for a concurrent request to die with
    # "database is locked" — and that is exactly what a listone-sized queue is.
    existing = {
        it.player_id: it
        for it in AuctionQueueItem.objects.filter(auction=auction)
    }
    per_role_index = {}
    to_create, to_update = [], []
    for p in players:
        if p.id == current_id:
            continue
        band = _role_band(p.role, auction.call_order)
        idx = per_role_index.get(band, 0)
        per_role_index[band] = idx + 1
        item = existing.get(p.id)
        if item is None:
            to_create.append(AuctionQueueItem(
                auction=auction, player=p, role=p.role, order=band + idx, done=False,
            ))
        else:
            # Already knocked down in an earlier pass: rebuilding puts it back
            # in line, same as the previous update_or_create did.
            item.role, item.order, item.done = p.role, band + idx, False
            to_update.append(item)

    if to_create:
        AuctionQueueItem.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        AuctionQueueItem.objects.bulk_update(to_update, ["role", "order", "done"], batch_size=500)
    return len(to_create) + len(to_update)


def _next_pending(auction):
    return auction.queue_items.filter(done=False).order_by("order", "id").first()


def _role_rank_for_nav(auction, role):
    """Rank used for "next/prev role" navigation in MANUAL flow.

    Respects the auction's role-grouped order (PDCA / ACDP) so jumping by role
    follows the same sequence the admin sees in the queue; flat orders
    (alphabetical / random) fall back to the canonical P→D→C→A sequence.
    """
    return _role_rank_map(auction.call_order).get(role, 9)


def _role_jump_target(auction, *, forward):
    """Queue item to land on when stepping by ROLE in MANUAL flow.

    forward=True → the first PENDING player whose role comes strictly after the
    one on the block (skips whatever is left of the current role). forward=False
    → the last DONE player whose role comes strictly before the one on the block
    (recall the previous role). Returns ``None`` when no such role exists.
    """
    on_block = auction.player if auction.player_id else None
    cur_role = on_block.role if on_block else None
    if cur_role is None:
        cur_rank = -1 if forward else 99
    else:
        cur_rank = _role_rank_for_nav(auction, cur_role)

    if forward:
        items = auction.queue_items.filter(done=False).order_by("order", "id")
        return next(
            (it for it in items
             if _role_rank_for_nav(auction, it.role or it.player.role) > cur_rank),
            None,
        )
    items = auction.queue_items.filter(done=True).order_by("-order", "-id")
    return next(
        (it for it in items
         if _role_rank_for_nav(auction, it.role or it.player.role) < cur_rank),
        None,
    )


def _opening_price_for(auction, player):
    """Starting price for ``player`` going on the block, per the auction setting.

    BASE_ONE opens every lot at a fixed 1 credit (classic); QUOTAZIONE (default)
    opens at the player's listone quotazione. With no concrete player the prior
    starting_price is kept.
    """
    if player is None:
        return auction.starting_price
    if auction.opening_price_mode == Auction.OpeningPriceMode.BASE_ONE:
        return Decimal("1")
    # In una lega Mantra fa fede la quotazione Mantra del listone (colonna
    # "Qt.A M"), che per 161 giocatori su 532 differisce da quella Classic.
    price = player.price_for(auction.league)
    return price if price is not None else Decimal("1")


def _set_on_block(auction, player, *, arm_timer=False):
    """Put ``player`` on the block: reset price to their quotazione, clear bids.

    When ``arm_timer`` (continuous flow) the presentation timer starts at once
    so the lot counts down even before the first bid; otherwise ``ends_at`` is
    left untouched (the caller controls it — the clock starts on the first bid).
    """
    auction.player = player
    auction.starting_price = _opening_price_for(auction, player)
    auction.current_price = auction.starting_price
    auction.best_bid = None
    # Un lotto nuovo riparte alle grida, e le buste appena aperte spariscono
    # dallo schermo insieme al giocatore a cui appartenevano.
    _clear_sealed(auction)
    if arm_timer and player is not None:
        auction.ends_at = timezone.now() + timedelta(seconds=auction.duration_seconds)


def max_unsold_passes():
    """How many times an unsold lot is put back up before it is parked.

    Default 1: a player nobody bid on is offered once more, later in the run;
    if they go unsold again the lot is parked so the running order can move on.
    """
    return int(getattr(settings, "AUCTION_MAX_UNSOLD_PASSES", 1))


def enqueue_released_player(auction, player, *, unsold=False):
    """Append a player to the end of the queue so they come up again.

    For role-grouped call orders the player goes to the end of *their own role*
    band; otherwise to the absolute end. No-op if already queued as pending.

    ``unsold=True`` marks this as a lot that expired with no bids: the pass is
    counted and, once :func:`max_unsold_passes` is exceeded, the lot is parked
    (left ``done``) and ``None`` is returned instead of being re-queued. Without
    that cap the last unsold players of a role band re-offer each other forever
    and continuous play never reaches the next role. A deliberate re-offer (a
    release / svincolo) resets the counter — it is a fresh chance, not a rebound.

    Honours ``auction.unsold_policy``:
    - DISCARD: no re-queue at all, parked immediately as done.
    - AUCTION_END: queued in the end-of-auction recovery round (_RECOVERY_BAND).
    - ROLE_END: queued at the end of their own role band (default).
    """
    if auction is None or player is None:
        return None
    existing = auction.queue_items.filter(player=player, done=False).first()
    if existing is not None:
        return existing

    prior = auction.queue_items.filter(player=player).first()
    passes = prior.unsold_passes if prior is not None else 0
    if unsold:
        passes += 1
        if passes > max_unsold_passes():
            if prior is not None:
                prior.unsold_passes = passes
                prior.done = True
                prior.save(update_fields=["unsold_passes", "done"])
            return None

        policy = getattr(auction, "unsold_policy", Auction.UnsoldPolicy.ROLE_END)
        if policy == Auction.UnsoldPolicy.DISCARD:
            if prior is not None:
                prior.unsold_passes = passes
                prior.done = True
                prior.save(update_fields=["unsold_passes", "done"])
            return None
        elif policy == Auction.UnsoldPolicy.AUCTION_END:
            band = _RECOVERY_BAND
        else:
            band = _role_band(player.role, auction.call_order)
    else:
        passes = 0
        band = _role_band(player.role, auction.call_order)

    last = (
        auction.queue_items.filter(order__gte=band, order__lt=band + _QUEUE_BAND)
        .aggregate(m=Max("order"))["m"]
    )
    order = (last + 1) if last is not None else band
    item, _ = AuctionQueueItem.objects.update_or_create(
        auction=auction, player=player,
        defaults={"role": player.role, "order": order, "done": False,
                  "unsold_passes": passes},
    )
    return item


def get_queue_preview(auction, limit=20):
    """Return a serializable list of the next pending items in the queue."""
    if auction is None or auction.call_order == Auction.CallOrder.RANDOM:
        return []
    items = (
        auction.queue_items.filter(done=False)
        .select_related("player")
        .order_by("order", "id")[:limit]
    )
    result = []
    for it in items:
        p = it.player
        price = p.price_for(auction.league) if p else Decimal("1")
        result.append({
            "id": it.id,
            "player_id": p.id if p else None,
            "name": p.name if p else "",
            "role": it.role or (p.role if p else ""),
            "team": p.team if p else "",
            "price": int(price) if price is not None else 1,
            "order": it.order,
            "is_recovery": it.order >= _RECOVERY_BAND,
        })
    return result


@transaction.atomic
def prioritize_queue_item(auction, player_id):
    """Move a player to the very top of the pending queue.

    If the player is already in the queue (pending or done), their order is set
    to (min_order - 1) and marked done=False. If the player is a free agent not
    yet queued, an item is created at the front.
    """
    player = Player.objects.filter(pk=player_id, owner__isnull=True).first()
    if player is None:
        return None

    min_order = (
        auction.queue_items.filter(done=False)
        .aggregate(m=Min("order"))["m"]
    )
    new_order = (min_order - 1) if min_order is not None else 0

    item, _ = AuctionQueueItem.objects.update_or_create(
        auction=auction, player=player,
        defaults={"role": player.role, "order": new_order, "done": False},
    )
    return item


@transaction.atomic
def postpone_queue_item(auction, player_id):
    """Postpone a player to the end of their role band without counting as unsold.

    If the player is currently on the block without bids, they are returned to the
    queue and the block advances to the next player. If the player is in the pending
    queue, their order is shifted past the last player of their role (or recovery band).
    """
    player = Player.objects.filter(pk=player_id).first()
    if player is None:
        return None

    existing = auction.queue_items.filter(player=player).first()
    if existing and existing.order >= _RECOVERY_BAND:
        band = _RECOVERY_BAND
    else:
        band = _role_band(player.role, auction.call_order)

    last = (
        auction.queue_items.filter(order__gte=band, order__lt=band + _QUEUE_BAND)
        .aggregate(m=Max("order"))["m"]
    )
    new_order = (last + 1) if last is not None else band

    # If the player is currently on the block with no bids:
    if auction.player_id == player.id and (auction.best_bid is None or auction.best_bid.cancelled):
        AuctionQueueItem.objects.update_or_create(
            auction=auction, player=player,
            defaults={"role": player.role, "order": new_order, "done": False},
        )
        nxt = _next_pending(auction)
        auction.current_cycle = auction.current_cycle + 1
        auction.best_bid = None
        auction.ends_at = None
        auction.remaining_seconds = None
        if nxt is None:
            auction.player = None
            auction.status = Auction.Status.CLOSED
            auction.current_price = auction.starting_price
            auction.save()
            return auction
        nxt.done = True
        nxt.save(update_fields=["done"])
        _set_on_block(auction, nxt.player, arm_timer=auction.auto_advances)
        auction.status = Auction.Status.LIVE
        auction.save()
        return auction

    item, _ = AuctionQueueItem.objects.update_or_create(
        auction=auction, player=player,
        defaults={"role": player.role, "order": new_order, "done": False},
    )
    return item


@transaction.atomic
def exclude_queue_item(auction, player_id):
    """Exclude a player from the queue by marking them done.

    If the player is currently on the block with no bids, they are marked done
    and the lot advances to the next player.
    """
    player = Player.objects.filter(pk=player_id).first()
    if player is None:
        return None

    if auction.player_id == player.id and (auction.best_bid is None or auction.best_bid.cancelled):
        AuctionQueueItem.objects.filter(auction=auction, player=player).update(done=True)
        nxt = _next_pending(auction)
        auction.current_cycle = auction.current_cycle + 1
        auction.best_bid = None
        auction.ends_at = None
        auction.remaining_seconds = None
        if nxt is None:
            auction.player = None
            auction.status = Auction.Status.CLOSED
            auction.current_price = auction.starting_price
            auction.save()
            return auction
        nxt.done = True
        nxt.save(update_fields=["done"])
        _set_on_block(auction, nxt.player, arm_timer=auction.auto_advances)
        auction.status = Auction.Status.LIVE
        auction.save()
        return auction

    AuctionQueueItem.objects.filter(auction=auction, player=player).update(done=True)
    return True
