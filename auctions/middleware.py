"""Middleware that keeps debug output off the public tunnel.

The desktop app deliberately runs with ``DEBUG=True``: one process serves HTTP,
WebSocket, static files *and* the uploaded logos/photos, which relies on
Django's development static/media serving. That is fine on a LAN, but once the
auction is published on a ``trycloudflare.com`` URL a crash would hand a full
traceback — source lines, settings, local variables — to anyone holding the
link. This turns unhandled exceptions into a plain 500 for requests that came
in through the tunnel, while local/LAN requests keep the useful debug page.

``FriendlyErrorPages`` gives the short plain-text refusals views return a real
page when a person, not a script, is on the other end.
"""
import re

from django.http import HttpResponse
from django.template.loader import render_to_string

from . import remote

MOBILE_USER_AGENT_RE = re.compile(
    r"(iphone|ipod|blackberry|android.*mobile|mobile.*firefox|iemobile|opera mini|webos|windows phone)",
    re.IGNORECASE,
)


class DeviceRoutingMiddleware:
    """Detects whether incoming requests come from a smartphone (mobile) or PC (desktop).

    Sets `request.is_mobile = True|False`.
    Supports manual override via ?view=app or ?view=web or ?view=auto stored in session.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if hasattr(request, "session"):
            view_param = (request.GET.get("view") or "").strip().lower()
            if view_param in ("app", "mobile"):
                request.session["view_mode"] = "app"
            elif view_param in ("web", "desktop"):
                request.session["view_mode"] = "web"
            elif view_param == "auto":
                request.session.pop("view_mode", None)

            view_mode = request.session.get("view_mode")
        else:
            view_mode = None

        if view_mode == "app":
            request.is_mobile = True
        elif view_mode == "web":
            request.is_mobile = False
        else:
            user_agent = request.META.get("HTTP_USER_AGENT", "")
            request.is_mobile = bool(MOBILE_USER_AGENT_RE.search(user_agent))

        return self.get_response(request)


_PLAIN_500 = (
    "<!doctype html><meta charset='utf-8'>"
    "<title>Errore</title>"
    "<div style=\"font:16px/1.6 system-ui,sans-serif;max-width:32em;margin:12vh auto;"
    "padding:0 1.5em;color:#e7e5e4;background:#101318\">"
    "<h1 style='font-size:1.3em'>Qualcosa è andato storto</h1>"
    "<p>La regia sul PC che ospita l'asta vede il dettaglio dell'errore. "
    "Riprova, o chiedi a chi conduce l'asta.</p></div>"
)


class RemoteErrorShield:
    """Generic 500 for tunnel requests; untouched behaviour locally."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not remote.request_is_remote(request):
            return None          # local: let the debug page through
        return HttpResponse(_PLAIN_500, status=500, content_type="text/html; charset=utf-8")


# Status -> (heading, message when the view gave none).
_WRAPPED = {
    400: ("Richiesta non valida", "Il link o i dati inviati non sono validi."),
    403: ("Non puoi aprire questa pagina", "Non hai i permessi per aprire questa pagina."),
    404: ("Pagina non trovata", "L'indirizzo non esiste o la pagina è stata spostata."),
    405: ("Azione non disponibile qui",
          "Questa azione parte dai pulsanti dell'app, non aprendo l'indirizzo."),
    429: ("Troppi tentativi", "Riprova tra qualche minuto."),
}


def _is_navigation(request):
    """A person opening a page, as opposed to the app's own fetch() calls."""
    mode = request.headers.get("Sec-Fetch-Mode")
    if mode:
        return mode == "navigate"
    # Browsers without Fetch Metadata: go by what they ask for.
    return ("text/html" in request.headers.get("Accept", "")
            and request.headers.get("X-Requested-With") != "XMLHttpRequest")


class FriendlyErrorPages:
    """A real error page, instead of one line of text on white.

    Many views refuse with ``HttpResponseForbidden("Non hai i permessi…")``,
    and ``require_POST`` answers an empty 405. The app's own fetch() calls read
    that text, so it stays; but a person who followed a link got a blank page.
    For browser navigations only, a short plain answer is wrapped in the
    app's error page, keeping its message and status.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        status = response.status_code
        if status not in _WRAPPED or response.streaming or not _is_navigation(request):
            return response
        if not response.get("Content-Type", "").startswith("text/html"):
            return response
        text = response.content.decode(response.charset or "utf-8", "replace").strip()
        if len(text) > 400 or "<" in text:      # already a page, or markup: leave it
            return response
        heading, default = _WRAPPED[status]
        html = render_to_string("errors/wrapped.html", {
            "code": status, "heading": heading, "message": text or default,
        }, request=request)
        wrapped = HttpResponse(html, status=status, content_type="text/html; charset=utf-8")
        for header in ("Allow", "Retry-After"):
            if header in response:
                wrapped[header] = response[header]
        return wrapped


class UploadSizeLimit:
    """Refuses a request body larger than ``FM_MAX_REQUEST_BYTES`` before any
    view parses it: Django caps form fields, not uploaded files, and one huge
    spreadsheet would hold the single server process."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        limit = getattr(settings, "FM_MAX_REQUEST_BYTES", 0)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if limit and length > limit:
            mb = limit // (1024 * 1024)
            return HttpResponse(f"File troppo grande: il massimo è {mb} MB.", status=413,
                                content_type="text/plain; charset=utf-8")
        return self.get_response(request)
