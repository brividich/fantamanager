"""Import delle rose da file (export «Rose Lega» di Fantapazz o Excel).

L'app non si collega più al sito di Fantapazz (niente login, cookie o browser
automatico): l'admin scarica l'export dalla sua lega e lo carica qui. Il nome
del modulo e degli URL resta quello di prima per non rompere i collegamenti.
"""
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from ..models import League
from ..providers import importers
from .common import (
    FORBIDDEN_LEAGUE_MSG,
    current_auction,
    current_league,
    manageable_leagues,
    staff_member_required,
    target_league,
    user_can_manage_scope,
)


def _resolve_import_league(request):
    """La lega FantaManager in cui scrive l'import."""
    raw = (request.POST.get("target_league_id") or "").strip()
    if raw.isdigit():
        league = League.objects.filter(pk=int(raw)).first()
        if league is not None:
            return league
    # Una lega FantaManager può ricordare l'id della lega Fantapazz da cui
    # arriva (League.external_id): si cerca solo tra quelle dell'utente.
    fp_league_id = (request.POST.get("league_id") or "").strip()
    if fp_league_id:
        league = manageable_leagues(request.user).filter(external_id=fp_league_id).first()
        if league is not None:
            return league
    return target_league(request)


def _import_league_or_403(request):
    """``(league, None)`` se l'utente gestisce la lega di destinazione; nessuna
    lega = il pool globale, solo superuser — altrimenti ``(None, 403)``."""
    league = _resolve_import_league(request)
    if not user_can_manage_scope(request.user, league):
        return None, JsonResponse({"ok": False, "error": FORBIDDEN_LEAGUE_MSG}, status=403)
    return league, None


@staff_member_required
@require_GET
def admin_fantapazz(request):
    league = current_league(request)
    return render(request, "auctions/admin_fantapazz.html", {
        "leagues": list(manageable_leagues(request.user)),
        "current_league": league,
        "console_section": "Importa",
        "console_active": "import",
        "selected": current_auction(request, league),
    })


@staff_member_required
@require_POST
def admin_fantapazz_import_rose(request):
    """Legge il file rose caricato e lo importa (``action=preview`` mostra soltanto)."""
    replace = request.POST.get("replace") == "1"
    upload = request.FILES.get("rose_file")
    if not upload:
        return JsonResponse({"ok": False, "error": "Scegli il file delle rose (.xls / .xlsx)."})
    try:
        teams = importers.parse_rose_xls(upload.read())
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"File non leggibile: {e}"})

    if not teams:
        return JsonResponse({"ok": False, "error": "Nessuna squadra trovata nel file."})

    warning = ""
    if len(teams) <= 1:
        warning = ("Attenzione: nel file c'è una sola squadra. Probabilmente è stato "
                   "scaricato l'export della tua rosa invece di quello dell'intera lega.")

    if request.POST.get("action") == "preview":
        return JsonResponse({
            "ok": True,
            "teams": [
                {"name": t["name"], "credits": t["credits"], "n_players": len(t["players"]),
                 "players": t["players"][:5]}
                for t in teams
            ],
            "total_players": sum(len(t["players"]) for t in teams),
            "warning": warning,
        })

    league, denied = _import_league_or_403(request)
    if denied:
        return denied
    result = importers.import_rose_data(teams, replace=replace, league=league)
    return JsonResponse({"ok": True, **result, "warning": warning})
