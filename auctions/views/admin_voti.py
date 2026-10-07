"""Views for Matchday (Giornate) and Voti management, scoring, and Battle Royale."""
import logging
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..models import Giornata, League, Season
from ..services.voti import compute_coppa_italia_battle_royale, import_voti_giornata, parse_voti_file
from .common import (current_league, form_int, manageable_leagues, staff_member_required,
                     user_can_manage_league)

logger = logging.getLogger(__name__)


@staff_member_required
def admin_giornate(request):
    """Console for matchdays, upload of votes, and championship scoring."""
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    if not league:
        leagues = manageable_leagues(request.user)
        league = leagues.first() if leagues.exists() else None

    if not league:
        messages.warning(request, "Seleziona o crea prima una lega per gestire le giornate e i voti.")
        return redirect("dashboard")

    season, _ = Season.objects.get_or_create(
        league=league,
        is_current=True,
        defaults={"name": f"Stagione 2026/27 · {league.name}"}
    )

    giornate = list(season.giornate.all().order_by("number"))
    if not giornate:
        # Pre-populate 38 matchdays
        for num in range(1, season.matchdays + 1):
            Giornata.objects.create(season=season, number=num)
        giornate = list(season.giornate.all().order_by("number"))

    selected_num = form_int(request.GET.get("giornata"), 1, min_value=1)
    current_giornata = next((g for g in giornate if g.number == selected_num), giornate[0] if giornate else None)

    if request.method == "POST" and current_giornata:
        sa_matchday = request.POST.get("serie_a_matchday")
        if sa_matchday and sa_matchday.isdigit():
            current_giornata.serie_a_matchday = int(sa_matchday)
            current_giornata.save()
            messages.success(request, f"Associazione Giornata Serie A aggiornata per la G{current_giornata.number}.")
        return redirect(f"/app/giornate/?giornata={current_giornata.number}")

    scores = []
    battle_royale = []
    performances_count = 0
    is_live = False
    live_count = 0
    official_count = 0
    if current_giornata:
        scores = list(current_giornata.scores.select_related("participant").order_by("-total"))
        battle_royale = compute_coppa_italia_battle_royale(current_giornata) if scores else []
        performances_count = current_giornata.performances.count()
        live_count = current_giornata.performances.filter(is_live=True).count()
        official_count = current_giornata.performances.filter(is_live=False).count()
        is_live = (current_giornata.status == Giornata.Status.LIVE) or (live_count > 0 and current_giornata.status != Giornata.Status.SCORED)

    from ..services.voti_live import LiveSyncManager

    return render(request, "auctions/admin_giornate.html", {
        "current_league": league,
        "leagues": manageable_leagues(request.user),
        "season": season,
        "giornate": giornate,
        "current_giornata": current_giornata,
        "scores": scores,
        "battle_royale": battle_royale,
        "performances_count": performances_count,
        "is_live": is_live,
        "live_count": live_count,
        "official_count": official_count,
        "live_sync_status": LiveSyncManager.get_instance().get_status(),
        "console_section": "Giornate & Voti",
        "console_active": "giornate",
    })


@staff_member_required
@require_POST
def admin_upload_voti(request):
    """Handle upload of Excel/CSV official votes sheet."""
    league_id = request.POST.get("league_id")
    league = get_object_or_404(League, pk=league_id) if league_id else current_league(request)
    if league and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    giornata_num = form_int(request.POST.get("giornata_number"), 1, min_value=1)
    season = Season.objects.filter(league=league, is_current=True).first()
    if not season:
        season = Season.objects.create(league=league, name=f"Stagione 2026/27 · {league.name}", is_current=True)

    giornata, _ = Giornata.objects.get_or_create(season=season, number=giornata_num)

    file_obj = request.FILES.get("voti_file")
    if not file_obj:
        messages.error(request, "Nessun file selezionato per il caricamento dei voti.")
        return redirect(f"/app/giornate/?giornata={giornata_num}")

    try:
        content = file_obj.read()
        parsed_rows = parse_voti_file(content, file_obj.name)
        if not parsed_rows:
            messages.error(request, "Il file caricato non contiene righe di voti riconoscibili o è vuoto.")
            return redirect(f"/app/giornate/?giornata={giornata_num}")

        report = import_voti_giornata(parsed_rows, giornata, league=league, recompute=True)
        # Mark as official
        giornata.performances.filter(giornata=giornata).update(is_live=False, live_source="official_upload")
        messages.success(
            request,
            f"Voti Ufficiali Giornata {giornata_num} importati con successo: "
            f"{report['total_imported']} calciatori consolidati in via definitiva."
        )
    except Exception as e:
        logger.exception("Errore durante l'importazione dei voti: %s", e)
        messages.error(request, f"Errore durante l'importazione del file voti: {e}")

    return redirect(f"/app/giornate/?giornata={giornata_num}")


admin_voti_import = admin_upload_voti


def _live_league(request):
    """The league the giornate page is on, if this user manages it: the live
    buttons act on that league only, never on every league at once."""
    league = current_league(request)
    if league is None:
        league = manageable_leagues(request.user).first()
    if league is None or not user_can_manage_league(request.user, league):
        return None
    return league


@staff_member_required
@require_POST
def admin_live_voti_sync(request):
    """Trigger on-demand live matchday rating synchronization."""
    from ..services.voti_live import PROVIDERS, LiveSyncManager, normalize_provider
    league = _live_league(request)
    if league is None:
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")
    giornata_num = form_int(request.POST.get("giornata_number"), 1, min_value=1)
    provider = normalize_provider(request.POST.get("provider"))

    mgr = LiveSyncManager.get_instance()
    mgr.provider = provider
    res = mgr.sync_now(giornata_num=giornata_num, is_provisional=True, leagues=[league])

    if res.get("status") == "SUCCESS":
        messages.success(
            request,
            f"🔴 Sync Live completato: {res.get('total_updated')} calciatori aggiornati in tempo reale per Giornata {giornata_num} ({PROVIDERS[provider]})."
        )
    elif res.get("status") == "ERROR":
        messages.error(request, f"Sync Live non riuscito: {res.get('message')}.")
    else:
        messages.warning(request, f"Sync Live: {res.get('status')} - nessun dato disponibile al momento per G{giornata_num}.")

    return redirect(f"/app/giornate/?giornata={giornata_num}")


@staff_member_required
@require_POST
def admin_live_voti_consolidate(request):
    """Consolidate provisional live votes into official final scored matchday."""
    from ..services.voti_live import LiveSyncManager
    league = _live_league(request)
    if league is None:
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")
    giornata_num = form_int(request.POST.get("giornata_number"), 1, min_value=1)
    res = LiveSyncManager.get_instance().consolidate_official(giornata_num, leagues=[league])
    messages.success(request, f"✅ Giornata {giornata_num} consolidata ufficialmente su voti definitivi ({res.get('giornate_count')} leghe chiuse).")
    return redirect(f"/app/giornate/?giornata={giornata_num}")


