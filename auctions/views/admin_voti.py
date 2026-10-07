"""Views for Matchday (Giornate) and Voti management, scoring, and Battle Royale."""
import logging
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import Formation, Giornata, League, Participant, Season
from ..services.competitions import season_name
from ..services.formation import (admin_save_matchday_formation, formation_state, is_editable,
                                  lock_formations, target_giornata)
from ..services.voti import compute_coppa_italia_battle_royale, import_voti_giornata, parse_voti_file
from .admin_market import _back
from .common import (current_league, form_int, manageable_leagues, staff_member_required,
                     user_can_manage_league)

logger = logging.getLogger(__name__)


_season_name = season_name


def _current_season(league):
    season, _ = Season.objects.get_or_create(league=league, is_current=True,
                                             defaults={"name": _season_name()})
    return season


def _giornate_url(request, giornata_num=None):
    """This page, on the console or in the app (the same partial runs in both)."""
    base = reverse("app_giornate") if request.path.startswith("/app/") else reverse("admin_giornate")
    return f"{base}?giornata={giornata_num}" if giornata_num else base


@staff_member_required
def admin_giornate(request):
    """Matchdays, votes and scoring — the console page and the app's (Regia)
    show the same partial, ``_giornate_manage.html``."""
    in_app = request.path.startswith("/app/")
    league = current_league(request)
    if league is not None and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    if not league:
        leagues = manageable_leagues(request.user)
        league = leagues.first() if leagues.exists() else None

    if not league:
        messages.warning(request, "Seleziona o crea prima una lega per gestire le giornate e i voti.")
        return redirect("app_regia" if in_app else "dashboard")

    season = _current_season(league)

    giornate = list(season.giornate.all().order_by("number"))
    if not giornate:
        # Pre-populate 38 matchdays
        for num in range(1, season.matchdays + 1):
            Giornata.objects.create(season=season, number=num)
        giornate = list(season.giornate.all().order_by("number"))

    selected_num = form_int(request.POST.get("giornata") or request.GET.get("giornata"), 0, min_value=0)
    if not selected_num:
        # Opened without a giornata: the one being played or to be lined up next.
        target = target_giornata(league)
        selected_num = target.number if target else 1
    current_giornata = next((g for g in giornate if g.number == selected_num), giornate[0] if giornate else None)

    if request.method == "POST" and current_giornata:
        sa_matchday = request.POST.get("serie_a_matchday")
        if sa_matchday and sa_matchday.isdigit():
            current_giornata.serie_a_matchday = int(sa_matchday)
            current_giornata.save()
            messages.success(request, f"Associazione Giornata Serie A aggiornata per la G{current_giornata.number}.")
        return redirect(_back(request, _giornate_url(request, current_giornata.number)))

    scores = []
    battle_royale = []
    performances_count = 0
    is_live = False
    live_count = 0
    official_count = 0
    lineups_saved = 0
    lineup_rows = []
    teams_count = Participant.objects.filter(league=league, is_active=True).count()
    if current_giornata:
        scores = list(current_giornata.scores.select_related("participant").order_by("-total"))
        battle_royale = compute_coppa_italia_battle_royale(current_giornata) if scores else []
        performances_count = current_giornata.performances.count()
        live_count = current_giornata.performances.filter(is_live=True).count()
        official_count = current_giornata.performances.filter(is_live=False).count()
        is_live = (current_giornata.status == Giornata.Status.LIVE) or (live_count > 0 and current_giornata.status != Giornata.Status.SCORED)
        lineups_saved = current_giornata.matchday_formations.count()
        lineup_rows = _lineup_rows(request, league, current_giornata)

    from ..services.voti_live import LiveSyncManager

    ctx = {}
    if in_app:
        from .common import _app_ctx
        _participant, app_ctx = _app_ctx(request, "regia")
        ctx.update(app_ctx or {})
        ctx.update({"app_league": league, "manages_app_league": True})
    base = _giornate_url(request)
    ctx.update({
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
        "lineups_editable": bool(current_giornata and is_editable(current_giornata)),
        "lineups_saved": lineups_saved,
        "lineup_rows": lineup_rows,
        "teams_count": teams_count,
        "live_sync_status": LiveSyncManager.get_instance().get_status(),
        "gv_base": base,
        "gv_league_q": f"league={league.id}",
        "gv_back": f"{base}?league={league.id}&giornata={current_giornata.number if current_giornata else 1}",
        "console_section": "Giornate & Voti",
        "console_active": "giornate",
    })
    return render(request, "auctions/app_giornate.html" if in_app else "auctions/admin_giornate.html", ctx)


def _formation_url(request, participant_id, giornata):
    name = "app_formation_edit" if request.path.startswith("/app/") else "admin_formation_edit"
    return f"{reverse(name, args=[participant_id])}?giornata={giornata.id}"


def _lineup_rows(request, league, giornata):
    """Every team's lineup for ``giornata``, for the list on the giornate page:
    its own copy if it has one, else the last saved lineup it would get."""
    copies = {mf.participant_id: mf for mf in giornata.matchday_formations.all()}
    templates = {f.participant_id: f for f in Formation.objects.filter(participant__league=league)}
    editable = is_editable(giornata)
    rows = []
    for team in Participant.objects.filter(league=league, is_active=True).order_by("display_name"):
        mf = copies.get(team.id)
        f = mf or templates.get(team.id)
        if mf is not None:
            state = "Salvata per la giornata" if editable else "Bloccata"
        elif f is not None:
            state = "Userà l'ultima salvata" if editable else "Mancante"
        else:
            state = "Nessuna formazione"
        rows.append({
            "team": team,
            "module": f.module if f else "",
            "starters": sum(1 for pid in (f.starter_ids or []) if pid) if f else 0,
            "state": state,
            "own": mf is not None,
            "edit_url": _formation_url(request, team.id, giornata),
        })
    return rows


@staff_member_required
def admin_formation_edit(request, participant_id):
    """The league admin edits one team's lineup for any giornata — also a
    locked or scored one (a correction; scores are recomputed). The pitch is the
    manager's own partial, _formation_pitch.html, in a console or app frame."""
    in_app = request.path.startswith("/app/")
    team = get_object_or_404(Participant.objects.select_related("league"), pk=participant_id)
    if team.league is None or not user_can_manage_league(request.user, team.league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")
    league = team.league
    gid = request.POST.get("giornata") or request.GET.get("giornata")
    giornata = get_object_or_404(Giornata, pk=form_int(gid, 0), season__league=league)

    if request.method == "POST":
        _mf, recomputed = admin_save_matchday_formation(
            team, giornata, request.POST.get("module", ""),
            request.POST.getlist("starter"), request.POST.getlist("bench"))
        if request.POST.get("save"):
            messages.success(request, f"Formazione di {team.display_name} per la Giornata {giornata.number} salvata"
                             + (": punteggi ricalcolati." if recomputed else "."))
        return redirect(_back(request, _formation_url(request, team.id, giornata)))

    ctx = {}
    if in_app:
        from .common import _app_ctx
        _participant, app_ctx = _app_ctx(request, "regia")
        ctx.update(app_ctx or {})
        ctx.update({"app_league": league, "manages_app_league": True})
    ctx.update(formation_state(team, giornata=giornata))
    teams = list(Participant.objects.filter(league=league, is_active=True).order_by("display_name"))
    ctx.update({
        "team": team,
        "giornata": giornata,
        "current_league": league,
        "leagues": manageable_leagues(request.user),
        "lineups_editable": is_editable(giornata),
        "has_scores": giornata.status in (Giornata.Status.LIVE, Giornata.Status.SCORED),
        "team_links": [(t, _formation_url(request, t.id, giornata)) for t in teams],
        "giornate_url": f"{_giornate_url(request)}?league={league.id}&giornata={giornata.number}",
        "fz_next": _formation_url(request, team.id, giornata),
        "console_section": "Giornate & Voti",
        "console_active": "giornate",
    })
    return render(request, "auctions/app_regia_formazione.html" if in_app else "auctions/admin_formazione.html", ctx)


@staff_member_required
@require_POST
def admin_giornata_lock(request):
    """Freeze every team's lineup for a giornata before it starts (it also
    happens by itself at the first live sync or votes import)."""
    league_id = request.POST.get("league_id")
    league = get_object_or_404(League, pk=league_id) if league_id else current_league(request)
    if league is None or not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")
    num = form_int(request.POST.get("giornata_number"), 1, min_value=1)
    giornata = get_object_or_404(Giornata, season=_current_season(league), number=num)
    if is_editable(giornata):
        lock_formations(giornata)
        messages.success(request, f"Formazioni della Giornata {num} bloccate: da adesso valgono così.")
    else:
        messages.info(request, f"Le formazioni della Giornata {num} erano già bloccate.")
    return redirect(_back(request, _giornate_url(request, num)))


@staff_member_required
@require_POST
def admin_upload_voti(request):
    """Handle upload of Excel/CSV official votes sheet."""
    league_id = request.POST.get("league_id")
    league = get_object_or_404(League, pk=league_id) if league_id else current_league(request)
    if league and not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")

    giornata_num = form_int(request.POST.get("giornata_number"), 1, min_value=1)
    giornata, _ = Giornata.objects.get_or_create(season=_current_season(league), number=giornata_num)
    back = _back(request, _giornate_url(request, giornata_num))

    file_obj = request.FILES.get("voti_file")
    if not file_obj:
        messages.error(request, "Nessun file selezionato per il caricamento dei voti.")
        return redirect(back)

    try:
        content = file_obj.read()
        parsed_rows = parse_voti_file(content, file_obj.name)
        if not parsed_rows:
            messages.error(request, "Il file caricato non contiene righe di voti riconoscibili o è vuoto.")
            return redirect(back)

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

    return redirect(_back(request, _giornate_url(request, giornata_num)))


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

    return redirect(_back(request, _giornate_url(request, giornata_num)))


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
    return redirect(_back(request, _giornate_url(request, giornata_num)))


