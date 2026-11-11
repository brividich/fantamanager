"""Report di mercato da stampare o scaricare: registro dell'asta live, scambi,
mercato della stagione.

Ogni report ha due indirizzi con lo stesso template e lo stesso CSV: la
console (``dashboard/…``, chi gestisce la lega) e l'app (``app/…``, le
squadre della lega e chi la gestisce dall'app). I servizi sono in
``services/reports.py``.
"""
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..models import Auction
from ..services import reports
from .common import _app_ctx, current_league, staff_member_required, user_can_manage_league

_FORBIDDEN = "Non hai i permessi per vedere i report di questa lega."


def _csv(data, name):
    resp = HttpResponse(data, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


def _console_league(request):
    league = current_league(request)
    if league is None:
        return None, redirect("admin_market_dashboard")
    if not user_can_manage_league(request.user, league):
        return None, HttpResponseForbidden(_FORBIDDEN)
    return league, None


def _app_league(request):
    _participant, ctx = _app_ctx(request, "mercato")
    league = ctx.get("app_league") if ctx else None
    if league is None:
        return None, redirect("app_login")
    return league, None


def _auction_in(league, auction_id):
    return get_object_or_404(Auction.objects.select_related("league"), pk=auction_id, league=league)


# -- 1. Registro dell'asta live ------------------------------------------------

def _render_live(request, auction, back_url, csv_url):
    ctx = reports.live_auction_log(auction)
    ctx.update(back_url=back_url, csv_url=csv_url)
    return render(request, "auctions/print_live_log.html", ctx)


@staff_member_required
def admin_report_auction(request, auction_id):
    auction = get_object_or_404(Auction.objects.select_related("league"), pk=auction_id)
    if not user_can_manage_league(request.user, auction.league):
        return HttpResponseForbidden(_FORBIDDEN)
    back = f"{reverse('admin_market_repair')}?league={auction.league_id}" if auction.league_id else reverse("admin_market_dashboard")
    return _render_live(request, auction, back, reverse("admin_report_auction_csv", args=[auction.id]))


@staff_member_required
def admin_report_auction_csv(request, auction_id):
    auction = get_object_or_404(Auction.objects.select_related("league"), pk=auction_id)
    if not user_can_manage_league(request.user, auction.league):
        return HttpResponseForbidden(_FORBIDDEN)
    return _csv(reports.live_auction_csv(auction), f"Registro_asta_{auction.id}.csv")


def app_report_auction(request, auction_id):
    league, resp = _app_league(request)
    if resp:
        return resp
    auction = _auction_in(league, auction_id)
    return _render_live(request, auction, f"{reverse('app_mercato')}#report",
                        reverse("app_report_auction_csv", args=[auction.id]))


def app_report_auction_csv(request, auction_id):
    league, resp = _app_league(request)
    if resp:
        return resp
    auction = _auction_in(league, auction_id)
    return _csv(reports.live_auction_csv(auction), f"Registro_asta_{auction.id}.csv")


# -- 2. Scambi -----------------------------------------------------------------

def _render_trades(request, league, back_url, csv_url):
    ctx = reports.trades_report(league)
    ctx.update(back_url=back_url, csv_url=csv_url)
    return render(request, "auctions/print_trades.html", ctx)


@staff_member_required
def admin_report_trades(request):
    league, resp = _console_league(request)
    if resp:
        return resp
    return _render_trades(request, league, f"{reverse('admin_market_trades')}?league={league.id}",
                          f"{reverse('admin_report_trades_csv')}?league={league.id}")


@staff_member_required
def admin_report_trades_csv(request):
    league, resp = _console_league(request)
    if resp:
        return resp
    return _csv(reports.trades_csv(league), f"Scambi_{league.id}.csv")


def app_report_trades(request):
    league, resp = _app_league(request)
    if resp:
        return resp
    return _render_trades(request, league, reverse("app_scambi"), reverse("app_report_trades_csv"))


def app_report_trades_csv(request):
    league, resp = _app_league(request)
    if resp:
        return resp
    return _csv(reports.trades_csv(league), f"Scambi_{league.id}.csv")


# -- 3. Mercato della stagione -------------------------------------------------

def _render_season(request, league, back_url, csv_url):
    ctx = reports.season_market_report(league)
    ctx.update(back_url=back_url, csv_url=csv_url)
    return render(request, "auctions/print_season_market.html", ctx)


@staff_member_required
def admin_report_season(request):
    league, resp = _console_league(request)
    if resp:
        return resp
    return _render_season(request, league, f"{reverse('admin_market_moves')}?league={league.id}",
                          f"{reverse('admin_report_season_csv')}?league={league.id}")


@staff_member_required
def admin_report_season_csv(request):
    league, resp = _console_league(request)
    if resp:
        return resp
    return _csv(reports.season_market_csv(league), f"Mercato_stagione_{league.id}.csv")


def app_report_season(request):
    league, resp = _app_league(request)
    if resp:
        return resp
    return _render_season(request, league, f"{reverse('app_mercato')}#report", reverse("app_report_season_csv"))


def app_report_season_csv(request):
    league, resp = _app_league(request)
    if resp:
        return resp
    return _csv(reports.season_market_csv(league), f"Mercato_stagione_{league.id}.csv")
