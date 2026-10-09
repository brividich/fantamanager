"""Le porte del sito per il PC della sala (services/sala.py).

Il PC si presenta con la chiave della lega (``Authorization: Bearer …``),
generata dall'admin nella pagina Impostazioni: niente sessione né cookie,
quindi niente CSRF. Ogni risposta è JSON con ``ok`` e, se qualcosa non va,
``error`` in italiano, che il PC mostra così com'è.
"""
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..services import sala


def _league_or_401(request):
    auth = request.headers.get("Authorization", "")
    key = auth[7:].strip() if auth.startswith("Bearer ") else ""
    return sala.league_for_key(key)


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _api(view):
    """Chiave valida e corpo JSON, o la risposta d'errore."""
    @csrf_exempt
    @require_POST
    def wrapped(request):
        league = _league_or_401(request)
        if league is None:
            return JsonResponse({"ok": False, "error": "Chiave non valida."}, status=401)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > sala.MAX_BODY_BYTES or len(request.body) > sala.MAX_BODY_BYTES:
            return JsonResponse({"ok": False, "error": (
                "Richiesta troppo grande per una lega: aggiorna FantaManager sul PC e riprova.")},
                status=413)
        data = _body(request)
        if data is None:
            return JsonResponse({"ok": False, "error": "Richiesta non valida."}, status=400)
        try:
            return view(request, league, data)
        except sala.SalaError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=409)
    return wrapped


@_api
def sala_status(request, league, data):
    return JsonResponse({"ok": True, "league": league.name, "lock": sala.lock_info(league)})


@_api
def sala_download(request, league, data):
    lock_id = sala.lock(league, by="PC della sala", force=bool(data.get("force")))
    league.refresh_from_db()
    return JsonResponse({"ok": True, "lock_id": lock_id, "snapshot": sala.snapshot(league)})


@_api
def sala_results(request, league, data):
    report = sala.apply_results(league, data.get("lock_id"), data)
    return JsonResponse({"ok": True, "report": report})


@_api
def sala_live(request, league, data):
    sala.set_live(league, data.get("lock_id"), data.get("url"), data.get("teams"))
    return JsonResponse({"ok": True})


@_api
def sala_release(request, league, data):
    sala.unlock(league, data.get("lock_id"))
    return JsonResponse({"ok": True})
