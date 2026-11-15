"""Il tabellone del budget: console e app mostrano lo stesso partial
(_tabellone.html) con i dati di ``services.economy.tabellone``. Lo vedono
tutti i membri della lega; chi la gestisce corregge i crediti."""
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from ..services.economy import tabellone
from .common import (_app_ctx, current_league, manageable_leagues, staff_member_required,
                     user_can_manage_league)


@staff_member_required
def admin_tabellone(request):
    league = current_league(request)
    if league is None:
        return redirect("dashboard")
    if not user_can_manage_league(request.user, league):
        return HttpResponseForbidden("Non hai i permessi per gestire questa lega.")
    return render(request, "auctions/admin_tabellone.html", {
        "tab": tabellone(league),
        "tab_league": league,
        "tab_can_manage": True,
        "current_league": league,
        "leagues": manageable_leagues(request.user),
        "console_section": "Tabellone budget",
        "console_active": "tabellone",
    })


def app_tabellone(request):
    _participant, ctx = _app_ctx(request, "lega")
    if ctx is None:
        from .app import _redirect_login
        return _redirect_login(request, ctx)
    league = ctx["app_league"]
    if league is None:
        return redirect("app_home")
    ctx.update({
        "tab": tabellone(league),
        "tab_league": league,
        "tab_can_manage": request.user.is_authenticated and user_can_manage_league(request.user, league),
    })
    return render(request, "auctions/app_tabellone.html", ctx)
