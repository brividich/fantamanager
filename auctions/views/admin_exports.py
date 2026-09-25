"""Export views for league rosters: recap page, XLSX, CSV, Leghe Fantacalcio format."""
from django.http import HttpResponse
from django.shortcuts import render

from ..models import League
from .. import exporters
from .common import current_auction, staff_member_required, target_league


def _export_league(request):
    """Resolve the league to export (?league= pk, session tenant, or None for multi-tenant hub)."""
    return target_league(request)


def _export_filename(league, ext):
    base = (league.name if league else "rose").strip().replace(" ", "_")
    return f"{base}_rose.{ext}"


@staff_member_required
def admin_export(request):
    """Printable recap: standings + every team's roster, scoped to a league."""
    league = _export_league(request)
    standings = exporters.build_standings(league)
    return render(request, "auctions/export_recap.html", {
        "league": league,
        "leagues": list(League.objects.all()),
        "current_league": league,
        "selected": current_auction(request, league),
        "console_section": "Export rose",
        "console_active": "export",
        "standings": standings,
        "role_order": exporters.ROLE_ORDER,
        "role_labels": exporters.ROLE_LABELS,
    })


@staff_member_required
def admin_export_xlsx(request):
    league = _export_league(request)
    data = exporters.build_xlsx(league)
    resp = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="{_export_filename(league, "xlsx")}"'
    return resp


@staff_member_required
def admin_export_csv(request):
    league = _export_league(request)
    data = exporters.build_csv(league)
    resp = HttpResponse(data, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{_export_filename(league, "csv")}"'
    return resp


@staff_member_required
def admin_export_leghe(request):
    """CSV keyed on the official player Id for the Leghe Fantacalcio import."""
    league = _export_league(request)
    data = exporters.build_leghe_csv(league)
    base = (league.name if league else "rose").strip().replace(" ", "_")
    resp = HttpResponse(data, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{base}_leghe-fantacalcio.csv"'
    return resp
