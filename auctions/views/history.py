"""Storico della lega: console e app mostrano lo stesso partial
(_season_history.html) con i dati di ``services.history.league_history``."""
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from ..services.history import league_history
from .common import (_app_ctx, current_league, manageable_leagues, staff_member_required,
                     user_can_manage_league)


@staff_member_required
def admin_storico(request):
    league = current_league(request)
    if league is None:
        return redirect("dashboard")
    if not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")
    return render(request, "auctions/admin_storico.html", {
        "history": league_history(league),
        "current_league": league,
        "leagues": manageable_leagues(request.user),
        "console_section": "Storico",
        "console_active": "storico",
    })


def app_storico(request):
    _participant, ctx = _app_ctx(request, "lega")
    if ctx is None:
        from .app import _redirect_login
        return _redirect_login(request, ctx)
    league = ctx["app_league"]
    if league is None:
        return redirect("app_home")
    ctx["history"] = league_history(league)
    return render(request, "auctions/app_storico.html", ctx)
