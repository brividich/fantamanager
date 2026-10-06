"""Aste rimaste a metà: un lotto scaduto mai chiuso, buste scadute mai aperte,
un lotto chiuso mai avanzato.

Succede quando il server si ferma (o cadono tutti i collegamenti) a metà
lotto: il ticker che chiude e fa avanzare vive solo finché qualcuno è
collegato. Al primo che si ricollega si sistema da sé; da qui il Supervisor lo
vede subito dopo un riavvio e lo sistema con un clic, facendo lo stesso giro
di un tick.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import Auction
from .lifecycle import (
    close_if_expired, finalize_expired, lot_had_bid, reset_if_closed, stuck_closed_lot,
)
from .sealed import sealed_tick


def _grace():
    # A running ticker gets there within a few of its own intervals.
    return timedelta(seconds=max(10.0, 3 * settings.TIMER_SYNC_INTERVAL_SECONDS))


def auction_issue(auction, now=None):
    """What is left halfway on this auction, as a short Italian label, or None."""
    now = now or timezone.now()
    late = now - _grace()
    if auction.sealed_open and auction.sealed_ends_at and auction.sealed_ends_at <= late:
        return "Buste scadute da aprire"
    if auction.status == Auction.Status.LIVE and auction.ends_at and auction.ends_at <= late:
        return "Lotto scaduto e mai chiuso"
    if auction.status == Auction.Status.CLOSED and auction.ends_at is not None:
        if stuck_closed_lot(auction.id) is None:
            return None
        if lot_had_bid(auction) and auction.auto_advances:
            return "Lotto aggiudicato in attesa di «Prosegui»"
        return "Lotto chiuso e mai avanzato"
    return None


def pending_recovery(exclude_ids=()):
    """The auctions with something left halfway: ``[(auction, label)]``.
    ``exclude_ids``: auctions a live ticker is already looking after."""
    now = timezone.now()
    out = []
    qs = (Auction.objects.filter(status__in=[Auction.Status.LIVE, Auction.Status.CLOSED])
          .exclude(pk__in=list(exclude_ids))
          .select_related("league", "player", "best_bid"))
    for auction in qs:
        label = auction_issue(auction, now)
        if label:
            out.append((auction, label))
    return out


def recover_auction(auction_id):
    """One tick by hand: open expired envelopes, close the expired lot, charge
    the winner, and move on — except a knocked-down lot on an auto-advancing
    flow, which waits for the regia's «Prosegui» as always. Returns the
    auction as it ends up."""
    sealed_tick(auction_id)
    if close_if_expired(auction_id) is not None:
        finalize_expired(auction_id)
    auction = Auction.objects.select_related("best_bid").get(pk=auction_id)
    if auction.status == Auction.Status.CLOSED and auction.ends_at is not None:
        finalize_expired(auction_id)
        if not (lot_had_bid(auction) and auction.auto_advances):
            reset_if_closed(auction_id)
    return Auction.objects.get(pk=auction_id)
