"""Views for Matchday (Giornate) and Voti management, scoring, and Battle Royale."""
import logging
from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..models import Giornata, GiornataScore, League, PlayerPerformance, Season
from ..services.voti import compute_coppa_italia_battle_royale, import_voti_giornata, parse_voti_file
from .common import current_league, manageable_leagues, staff_member_required, user_can_manage_league

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

    selected_num = int(request.GET.get("giornata") or 1)
    current_giornata = next((g for g in giornate if g.number == selected_num), giornate[0] if giornate else None)

    scores = []
    battle_royale = []
    performances_count = 0
    if current_giornata:
        scores = list(current_giornata.scores.select_related("participant").order_by("-total"))
        battle_royale = compute_coppa_italia_battle_royale(current_giornata) if scores else []
        performances_count = current_giornata.performances.count()

    return render(request, "auctions/admin_giornate.html", {
        "current_league": league,
        "leagues": manageable_leagues(request.user),
        "season": season,
        "giornate": giornate,
        "current_giornata": current_giornata,
        "scores": scores,
        "battle_royale": battle_royale,
        "performances_count": performances_count,
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

    giornata_num = int(request.POST.get("giornata_number") or 1)
    season = Season.objects.filter(league=league, is_current=True).first()
    if not season:
        season = Season.objects.create(league=league, name=f"Stagione 2026/27 · {league.name}", is_current=True)

    giornata, _ = Giornata.objects.get_or_create(season=season, number=giornata_num)

    file_obj = request.FILES.get("voti_file")
    if not file_obj:
        messages.error(request, "Nessun file selezionato per il caricamento dei voti.")
        return redirect(f"/admin-auction/giornate/?giornata={giornata_num}")

    try:
        content = file_obj.read()
        parsed_rows = parse_voti_file(content, file_obj.name)
        if not parsed_rows:
            messages.error(request, "Il file caricato non contiene righe di voti riconoscibili o è vuoto.")
            return redirect(f"/admin-auction/giornate/?giornata={giornata_num}")

        report = import_voti_giornata(parsed_rows, giornata, league=league, recompute=True)
        messages.success(
            request,
            f"Voti Giornata {giornata_num} importati con successo: "
            f"{report['total_imported']} calciatori aggiornati ({report['unmatched']} non associati)."
        )
    except Exception as e:
        logger.exception("Errore durante l'importazione dei voti: %s", e)
        messages.error(request, f"Errore durante l'importazione del file voti: {e}")

    return redirect(f"/admin-auction/giornate/?giornata={giornata_num}")


admin_voti_import = admin_upload_voti

