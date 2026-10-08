"""Il server che si ferma non deve far scadere il timer.

Sul PC tutte le chiamate al database dell'asta passano da una sola coda (il
thread di ``database_sync_to_async``), e SQLite ha un solo scrittore alla
volta. Se qualcosa la tiene ferma — un import pesante dalla regia,
l'antivirus o un programma di backup che blocca il file — offerte, buste e
ticker aspettano con lei, ma l'orologio del lotto no: il lotto poteva
chiudersi mentre le offerte arrivate in tempo erano ancora in coda.

Ogni chiamata del live sa quando è partita: l'offerta quando è arrivata al
server, il ticker quando ha chiesto. La prima che parte dopo un fermo di
almeno ``STALL_MIN_SECONDS`` restituisce al timer del lotto (e delle buste) i
secondi persi; quelle in coda dietro di lei trovano il segno e non li
restituiscono una seconda volta.
"""
import logging
import threading

from .. import health

logger = logging.getLogger("auctions.stall")

STALL_MIN_SECONDS = 1.5

_LOCK = threading.Lock()
_MARKS = {}  # auction_id -> quando sono stati restituiti i secondi dell'ultimo fermo


def stalled(started_at, now):
    """La chiamata partita a ``started_at`` ha aspettato un fermo?"""
    return started_at is not None and (now - started_at).total_seconds() >= STALL_MIN_SECONDS


def give_back(auction, started_at, now):
    """Allunga i timer di ``auction`` del fermo subìto dalla chiamata partita a
    ``started_at``, una volta sola per fermo. ``auction`` va letta col lucchetto
    (select_for_update). Ritorna i campi cambiati, da salvare."""
    if not stalled(started_at, now):
        return []
    with _LOCK:
        mark = _MARKS.get(auction.pk)
        if mark is not None and started_at <= mark:
            return []  # questo fermo l'ha già restituito chi era in coda prima
        _MARKS[auction.pk] = now
    lost = now - started_at
    fields = []
    if auction.status != auction.Status.LIVE:
        return fields
    if auction.ends_at is not None and auction.ends_at > started_at:
        auction.ends_at += lost
        fields.append("ends_at")
    if auction.sealed_ends_at is not None and auction.sealed_ends_at > started_at:
        auction.sealed_ends_at += lost
        fields.append("sealed_ends_at")
    if fields:
        seconds = lost.total_seconds()
        health.record_stall(auction.pk, seconds)
        logger.warning("Asta #%s: database fermo per %.1fs, secondi restituiti al timer",
                       auction.pk, seconds)
    return fields
