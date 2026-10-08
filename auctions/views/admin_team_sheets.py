"""Scheda squadra e lista rinnovi: download (Excel e stampa), import, dati di testata."""
import json

from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .. import team_sheets
from ..models import Participant
from ..providers import team_sheet_import
from ..uploads import UploadRejected, clean_image
from .common import (
    FORBIDDEN_LEAGUE_MSG,
    league_scope_or_403,
    managed_or_403,
    safe_next,
    staff_member_required,
    target_league,
    user_can_manage_scope,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Un PDF di scheda pesa qualche centinaio di KB; oltre questo non è una scheda.
MAX_SHEET_BYTES = 15 * 1024 * 1024
MAX_SHEET_FILES = 40


def _league_or_403(request):
    league = target_league(request)
    if league is None or not user_can_manage_scope(request.user, league):
        return None, HttpResponseForbidden(FORBIDDEN_LEAGUE_MSG if league else "Scegli prima una lega.")
    return league, None


def _team_ids(request, league):
    raw = (request.GET.get("team") or "").strip()
    if not raw.isdigit():
        return None
    team = Participant.objects.filter(pk=int(raw), league=league).first()
    return [team.pk] if team else None


def _slug(text):
    return "".join(c if c.isalnum() else "_" for c in (text or "")).strip("_") or "lega"


@staff_member_required
def admin_export_team_sheets(request):
    """Le schede squadra in Excel: tutte, o una sola con ``?team=``."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    ids = _team_ids(request, league)
    data = team_sheets.build_team_sheets_xlsx(league, ids)
    name = _slug(Participant.objects.get(pk=ids[0]).display_name) if ids else _slug(league.name)
    resp = HttpResponse(data, content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="{name}_scheda{"" if ids else "_squadre"}.xlsx"'
    return resp


@staff_member_required
def admin_print_team_sheets(request):
    """Le stesse schede in HTML A4, per stampare o salvare in PDF dal browser."""
    league, denied = _league_or_403(request)
    if denied:
        return denied
    return render(request, "auctions/print_team_sheets.html", {
        "league": league,
        "sheets": team_sheets.team_sheets(league, _team_ids(request, league)),
    })


@staff_member_required
def admin_export_renewals(request):
    league, denied = _league_or_403(request)
    if denied:
        return denied
    data = team_sheets.build_renewals_xlsx(league)
    season = team_sheets.season_label(team_sheets.season_start())
    resp = HttpResponse(data, content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="Rinnovi_{season}_{_slug(league.name)}.xlsx"'
    return resp


@staff_member_required
def admin_print_renewals(request):
    league, denied = _league_or_403(request)
    if denied:
        return denied
    return render(request, "auctions/print_renewals.html", {
        "league": league,
        "season": team_sheets.season_label(team_sheets.season_start()),
        "blocks": team_sheets.renewal_rows(league),
    })


@staff_member_required
@require_POST
def admin_import_team_sheets(request):
    """Schede squadra (PDF o Excel, anche più file insieme) → rose della lega.

    ``action=preview`` fa l'import per intero dentro una transazione annullata
    e restituisce il resoconto; senza, lo applica. ``mapping`` (JSON) sceglie a
    mano la squadra di ogni scheda: {"0": 12, "1": "new", "2": "skip"}.
    """
    league, denied = league_scope_or_403(request, request.POST.get("league_id"), target_league(request))
    if denied:
        return denied
    if league is None:
        return JsonResponse({"ok": False, "error": "Scegli prima la lega."}, status=400)
    files = request.FILES.getlist("sheet_files")
    if not files:
        return JsonResponse({"ok": False, "error": "Nessun file."}, status=400)
    if len(files) > MAX_SHEET_FILES:
        return JsonResponse({"ok": False, "error": f"Al massimo {MAX_SHEET_FILES} file per volta."}, status=400)

    sheets, errors = [], []
    for f in files:
        if f.size > MAX_SHEET_BYTES:
            errors.append(f"{f.name}: file troppo grande.")
            continue
        try:
            found = team_sheet_import.parse_team_sheet_file(f.read(), f.name)
        except team_sheet_import.SheetError as exc:
            errors.append(str(exc))
            continue
        if not found:
            errors.append(f"{f.name}: nessuna scheda squadra riconosciuta "
                          "(serve la tabella con CALCIATORE / SQUADRA / SPESA / ANNI).")
        sheets.extend(found)
    if not sheets:
        return JsonResponse({"ok": False, "error": " ".join(errors) or "Nessuna scheda trovata."}, status=400)

    try:
        mapping = json.loads(request.POST.get("mapping") or "{}")
    except ValueError:
        mapping = {}
    preview = request.POST.get("action") == "preview"
    report = team_sheet_import.apply_team_sheets(
        sheets, league,
        mapping=mapping if isinstance(mapping, dict) else {},
        replace=request.POST.get("replace", "1") == "1",
        overwrite_images=request.POST.get("overwrite_images") == "1",
        dry_run=preview,
    )
    report.update({"ok": True, "preview": preview, "errors": errors,
                   "contracts_on": league.contracts_enabled})
    return JsonResponse(report)


@staff_member_required
@require_POST
def admin_participant_profile(request, participant_id):
    """Testata della scheda: sigla, presidente, allenatore, stadio, palmarès, maglie."""
    p, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    post = request.POST
    p.short_name = post.get("short_name", "").strip()[:30]
    p.president_name = post.get("president_name", "").strip()[:80]
    p.coach_name = post.get("coach_name", "").strip()[:80]
    p.stadium = post.get("stadium", "").strip()[:80]
    p.stadium_capacity = _positive(post.get("stadium_capacity"))
    founded = _positive(post.get("founded"))
    p.founded = founded if founded and 1800 <= founded <= 2100 else None
    honours = []
    for label, count in zip(post.getlist("honour_label"), post.getlist("honour_count")):
        label = label.strip().upper()[:60]
        if label:
            honours.append([label, _positive(count) or 0])
    p.honours = honours
    fallback = f"/dashboard/participants/?league={p.league_id}" if p.league_id else "/dashboard/participants/"
    for key in ("logo", "kit_home", "kit_away"):
        if key in request.FILES:
            try:
                setattr(p, key, clean_image(request.FILES[key]))
            except UploadRejected as exc:
                messages.error(request, str(exc))
                return redirect(safe_next(request, fallback))
        elif post.get(f"clear_{key}") == "1":
            setattr(p, key, None)
    p.save()
    messages.success(request, f"Scheda di «{p.display_name}» aggiornata.")
    return redirect(safe_next(request, fallback))


def _positive(raw):
    raw = str(raw or "").replace(".", "").strip()
    return int(raw) if raw.isdigit() else None

