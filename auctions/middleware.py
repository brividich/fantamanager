"""Middleware that keeps debug output off the public tunnel.

The desktop app deliberately runs with ``DEBUG=True``: one process serves HTTP,
WebSocket, static files *and* the uploaded logos/photos, which relies on
Django's development static/media serving. That is fine on a LAN, but once the
auction is published on a ``trycloudflare.com`` URL a crash would hand a full
traceback — source lines, settings, local variables — to anyone holding the
link. This turns unhandled exceptions into a plain 500 for requests that came
in through the tunnel, while local/LAN requests keep the useful debug page.
"""
from django.http import HttpResponse

from . import remote

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
