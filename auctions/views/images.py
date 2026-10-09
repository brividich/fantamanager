"""Foto dei calciatori e stemmi dei club passati dal sito.

API-Football (e i modelli di URL che l'admin imposta per le foto) danno
indirizzi di immagini su server di terzi: se il browser le chiedesse da lì,
ogni pagina della rosa manderebbe a quel server l'IP di chi la guarda. Le pagine
mostrano invece ``/img/<firma>/``: il sito scarica l'immagine una volta (dal
suo indirizzo, non da quello del visitatore), la tiene in MEDIA_ROOT e la serve.

La firma (``django.core.signing``) fa sì che si scarichino solo gli indirizzi
che il sito stesso ha messo in pagina: non è un proxy aperto. Gli indirizzi
che puntano alla rete interna sono rifiutati come per la classifica remota.
"""
import hashlib
import logging
import socket

import requests
from django.conf import settings
from django.core import signing
from django.http import FileResponse, Http404
from django.views.decorators.cache import cache_control

logger = logging.getLogger("auctions.images")

SALT = "fm.remote-image"
MAX_BYTES = 1024 * 1024
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
         "image/svg+xml": ".svg"}


def local_url(url):
    """L'indirizzo sul sito per l'immagine esterna ``url`` ("" se vuoto)."""
    from django.urls import reverse

    url = (url or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        return url                     # già un file del sito (/media/…)
    return reverse("remote_image", args=[signing.dumps(url, salt=SALT, compress=True)])


def _cache_dir():
    path = settings.MEDIA_ROOT / "img-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch(url, get=None, resolve=None):
    from ..providers.standings import _safe_get

    resp = _safe_get(url, get or requests.get, resolve or socket.getaddrinfo)
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in TYPES:
        raise requests.RequestException(f"non è un'immagine ({ctype or 'tipo sconosciuto'})")
    body = resp.content
    if len(body) > MAX_BYTES:
        raise requests.RequestException("immagine troppo grande")
    return ctype, body


@cache_control(public=True, max_age=7 * 24 * 3600)
def remote_image(request, signed):
    try:
        url = signing.loads(signed, salt=SALT)
    except signing.BadSignature:
        raise Http404("Immagine sconosciuta")
    key = hashlib.sha256(url.encode()).hexdigest()
    folder = _cache_dir()
    for ctype, ext in TYPES.items():
        cached = folder / f"{key}{ext}"
        if cached.exists():
            return _serve(cached, ctype)
    try:
        ctype, body = _fetch(url)
    except requests.RequestException as exc:
        logger.info("Immagine esterna non scaricata: %s", exc)
        raise Http404("Immagine non disponibile")
    cached = folder / f"{key}{TYPES[ctype]}"
    cached.write_bytes(body)
    return _serve(cached, ctype)


def _serve(path, ctype):
    response = FileResponse(open(path, "rb"), content_type=ctype)
    response["X-Content-Type-Options"] = "nosniff"
    # Come i file caricati: un SVG non esegue niente.
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response
