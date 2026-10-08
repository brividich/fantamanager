"""Admin console view for managing Competitions, Tournaments, Calendars, and Standings."""
import logging
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import Competition, League
from ..services.competitions import (
    compute_competition_standings,
    ensure_league_season_and_competitions,
    setup_groups_knockout_competition,
    setup_knockout_competition,
    setup_round_robin_competition,
    setup_supercoppa,
)
from .common import (back_to_page, current_league, form_int, in_app, manageable_leagues, page_frame,
                     staff_member_required, user_can_manage_league)

logger = logging.getLogger(__name__)


@staff_member_required
def admin_competitions(request):
    """Console management for league competitions, brackets, calendars and standings."""
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    if not league:
        leagues = manageable_leagues(request.user)
        league = leagues.first() if leagues.exists() else None

    if not league:
        messages.warning(request, "Seleziona o crea prima una lega per gestire le competizioni.")
        return redirect("dashboard")

    season, competitions = ensure_league_season_and_competitions(league)

    # Refresh competitions list
    all_competitions = list(season.competitions.all().order_by("id")) if season else []

    selected_comp_id = request.GET.get("comp")
    selected_comp = None
    if selected_comp_id:
        selected_comp = next((c for c in all_competitions if str(c.id) == selected_comp_id), None)
    if not selected_comp and all_competitions:
        selected_comp = all_competitions[0]

    from ..services.competitions import get_competition_matchdays
    competition_data = None
    fixtures_by_giornata = []
    competition_matchdays = []
    if selected_comp:
        competition_data = compute_competition_standings(selected_comp)
        competition_matchdays = get_competition_matchdays(selected_comp)
        fixtures_by_giornata = [
            {"giornata": m["giornata"], "fixtures": m["fixtures"]}
            for m in competition_matchdays
            if m.get("kind") == "fixtures"
        ]

    teams = list(league.participants.filter(is_active=True).order_by("display_name"))
    giornate = list(season.giornate.all().order_by("number")) if season else []

    return render(request, "auctions/admin_competitions.html", {
        **page_frame(request, league, own_messages=True),
        # «Nuova Competizione» dall'app torna alle competizioni dell'app.
        "wz_from": "app" if in_app(request) else "console",
        "current_league": league,
        "leagues": manageable_leagues(request.user),
        "season": season,
        "competitions": all_competitions,
        "selected_competition": selected_comp,
        "competition_data": competition_data,
        "fixtures_by_giornata": fixtures_by_giornata,
        "competition_matchdays": competition_matchdays,
        "teams": teams,
        "giornate": giornate,
        "competition_types": Competition.Type.choices,
        "console_section": "Competizioni & Calendari",
        "console_active": "competitions",
    })


@staff_member_required
@require_POST
def admin_competition_create(request):
    """Create a new competition and generate its schedule if applicable."""
    league_id = request.POST.get("league_id")
    league = get_object_or_404(League, pk=league_id) if league_id else current_league(request)
    if league and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    from_app = request.POST.get("from") == "app" or request.POST.get("next") == "app"
    season, _ = ensure_league_season_and_competitions(league)
    name = (request.POST.get("name") or "").strip()
    kind = request.POST.get("kind") or Competition.Type.ROUND_ROBIN
    start_giornata = form_int(request.POST.get("start_giornata"), 1, min_value=1)
    end_giornata = request.POST.get("end_giornata")
    end_giornata = int(end_giornata) if end_giornata and end_giornata.isdigit() else None
    two_legged = request.POST.get("two_legged") == "1"

    if not name:
        messages.error(request, "Specificare un nome valido per la competizione.")
        if from_app:
            return redirect(f"{reverse('app_lega')}?tab=competizioni")
        return redirect(f"{reverse('admin_competitions')}?league={league.id}")

    try:
        settings_payload = {
            "start_giornata": start_giornata,
            "end_giornata": end_giornata,
            "two_legged": two_legged,
            "win_points": int(request.POST.get("win_points") or 3),
            "draw_points": int(request.POST.get("draw_points") or 1),
            "loss_points": int(request.POST.get("loss_points") or 0),
            "goal_threshold": float(request.POST.get("goal_threshold") or 66.0),
            "goal_step": float(request.POST.get("goal_step") or 6.0),
            "home_bonus": float(request.POST.get("home_bonus") or 0.0),
            "description": (request.POST.get("description") or "").strip(),
        }

        comp = Competition.objects.create(
            season=season,
            name=name,
            kind=kind,
            settings=settings_payload
        )

        # Generate schedule based on format
        if kind in (Competition.Type.ROUND_ROBIN, Competition.Type.SEASON_SPLIT):
            fixtures = setup_round_robin_competition(comp, start_giornata=start_giornata, end_giornata=end_giornata)
            messages.success(request, f"Competizione «{comp.name}» creata con successo ({len(fixtures)} partite generate).")
        elif kind == Competition.Type.KNOCKOUT:
            fixtures = setup_knockout_competition(comp, start_giornata=start_giornata, two_legged=two_legged)
            messages.success(request, f"Torneo a eliminazione «{comp.name}» creato con successo ({len(fixtures)} sfide a tabellone).")
        elif kind == Competition.Type.GROUPS_KNOCKOUT:
            fixtures = setup_groups_knockout_competition(comp, start_giornata=start_giornata, end_giornata=end_giornata)
            messages.success(request, f"Coppa a gironi «{comp.name}» creata con successo ({len(fixtures)} partite a calendario).")
        elif kind == Competition.Type.SUPERCOPPA:
            home_id = request.POST.get("home_id")
            away_id = request.POST.get("away_id")
            if home_id and away_id:
                setup_supercoppa(comp, int(home_id), int(away_id), giornata_num=start_giornata)
                messages.success(request, f"Supercoppa «{comp.name}» programmata per la Giornata {start_giornata}.")
            else:
                messages.success(request, f"Supercoppa «{comp.name}» creata (in attesa di definire le squadre sfidanti).")
        else:
            messages.success(request, f"Competizione a punti «{comp.name}» attivata con successo.")

        if request.POST.get("notify_teams") == "1":
            from ..services import mail
            report = mail.send_competition_notice(request, comp)
            if report.get("sent"):
                messages.success(request, f"Avviso alle squadre: {mail.report_message(report)}")

        if from_app:
            return redirect(f"{reverse('app_lega')}?comp={comp.id}&tab=competizioni")
        return redirect(f"{reverse('admin_competitions')}?league={league.id}&comp={comp.id}")
    except Exception as e:
        logger.exception("Errore creazione competizione: %s", e)
        messages.error(request, f"Errore durante la creazione della competizione: {e}")
        if from_app:
            return redirect(f"{reverse('app_lega')}?tab=competizioni")
        return redirect(f"{reverse('admin_competitions')}?league={league.id}")


@staff_member_required
@require_POST
def admin_competition_regenerate(request, comp_id):
    """Regenerate calendar fixtures for round robin or knockout cup."""
    comp = get_object_or_404(Competition, pk=comp_id)
    league = comp.season.league
    if league and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    start_giornata = form_int(request.POST.get("start_giornata"), comp.settings.get("start_giornata", 1), min_value=1)
    end_giornata = request.POST.get("end_giornata") or comp.settings.get("end_giornata")
    end_giornata = int(end_giornata) if end_giornata and str(end_giornata).isdigit() else None
    two_legged = request.POST.get("two_legged") == "1" if "two_legged" in request.POST else comp.settings.get("two_legged", False)

    try:
        if comp.kind in (Competition.Type.ROUND_ROBIN, Competition.Type.SEASON_SPLIT):
            fixtures = setup_round_robin_competition(comp, start_giornata=start_giornata, end_giornata=end_giornata)
            messages.success(request, f"Calendario rigenerato per «{comp.name}» ({len(fixtures)} partite).")
        elif comp.kind == Competition.Type.KNOCKOUT:
            fixtures = setup_knockout_competition(comp, start_giornata=start_giornata, two_legged=two_legged)
            messages.success(request, f"Tabellone rigenerato per «{comp.name}» ({len(fixtures)} sfide).")
        else:
            messages.info(request, f"La competizione «{comp.name}» si calcola automaticamente sui punteggi fantavoto.")
    except Exception as e:
        logger.exception("Errore rigenerazione calendario: %s", e)
        messages.error(request, f"Errore durante la rigenerazione: {e}")

    return redirect(back_to_page(request, "admin_competitions", f"?league={league.id}&comp={comp.id}"))


@staff_member_required
@require_POST
def admin_competition_delete(request, comp_id):
    """Delete a competition and its fixtures."""
    comp = get_object_or_404(Competition, pk=comp_id)
    league = comp.season.league
    if league and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    name = comp.name
    comp.delete()
    messages.success(request, f"Competizione «{name}» eliminata.")
    return redirect(back_to_page(request, "admin_competitions", f"?league={league.id}"))
