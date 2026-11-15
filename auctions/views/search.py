"""La ricerca nella lega: calciatori, squadre, competizioni, mercati e aste
che corrispondono a quello che si scrive. La usano la ricerca rapida della
console («Cerca o salta a…», Ctrl+K) e il campo Cerca dell'app (Altro): stessa
view, stessi risultati; i link portano alla pagina della console o dell'app.

Nella console cerca nella lega che si sta gestendo; nell'app nella lega della
squadra (o in quella scelta in Regia). Solo dati che chi cerca può già vedere:
il listone, le squadre, le competizioni e i mercati della sua lega.
"""
from urllib.parse import quote

from django.http import JsonResponse
from django.urls import reverse

from ..models import Auction, Competition, MarketSession, Participant, Player
from ..providers.importers import normalize_name
from .common import _app_ctx, current_league, in_app, user_can_manage_league

PER_GROUP = 6


def _hit(needle, *texts):
    return any(needle in normalize_name(t or "") for t in texts)


def league_search(request):
    q = (request.GET.get("q") or "").strip()[:60]
    app = in_app(request)
    if app:
        _participant, ctx = _app_ctx(request, "altro")
        league = (ctx or {}).get("app_league")
        manages = bool(league is not None and request.user.is_authenticated
                       and user_can_manage_league(request.user, league))
    else:
        league = current_league(request) if request.user.is_authenticated else None
        manages = bool(league is not None and user_can_manage_league(request.user, league))
        if not manages:
            league = None
    needle = normalize_name(q)
    if league is None or len(needle) < 2:
        return JsonResponse({"ok": True, "q": q, "groups": []})
    lq = f"league={league.id}"
    groups = []

    # Calciatori: tutto il listone della lega (in rosa, svincolati, fuori listone).
    players = []
    for p in (Player.objects.filter(league=league).select_related("owner")
              .order_by("name").only("id", "name", "role", "team", "in_listone", "owner__display_name")):
        if not _hit(needle, p.name, p.team):
            continue
        status = p.owner.display_name if p.owner_id else ("svincolato" if p.in_listone else "fuori listone")
        base = reverse("app_footballers") if app else reverse("admin_footballers")
        players.append({"title": p.name, "role": p.role,
                        "detail": " · ".join(x for x in (p.team, status) if x),
                        "url": f"{base}?{lq}&vista=listone&q={quote(p.name)}"})
        if len(players) >= PER_GROUP:
            break
    if players:
        groups.append({"label": "Calciatori", "items": players})

    # Squadre: nome, sigla, allenatore, presidente.
    teams = []
    for t in Participant.objects.filter(league=league, is_active=True).order_by("display_name"):
        if not _hit(needle, t.display_name, t.short_name, t.coach_name, t.president_name):
            continue
        if app:
            url = reverse("app_print_team_sheet_id", args=[t.id])
        else:
            url = f"{reverse('admin_participants')}?{lq}"
        teams.append({"title": t.display_name, "detail": f"{t.remaining_credits:.0f} FM a disposizione",
                      "url": url})
        if len(teams) >= PER_GROUP:
            break
    if teams:
        groups.append({"label": "Squadre", "items": teams})

    comps = [{"title": c.name, "detail": c.get_kind_display(),
              "url": (reverse("app_lega") if app else f"{reverse('admin_competitions')}?{lq}&comp={c.id}")}
             for c in Competition.objects.filter(season__league=league, season__is_current=True).order_by("id")
             if _hit(needle, c.name)][:PER_GROUP]
    if comps:
        groups.append({"label": "Competizioni", "items": comps})

    markets = [{"title": s.title, "detail": s.get_status_display(),
                "url": (f"{reverse('app_mercato')}?session_id={s.id}" if app
                        else reverse("admin_market_session", args=[s.id]))}
               for s in MarketSession.objects.filter(league=league).order_by("-id")
               if _hit(needle, s.title)][:PER_GROUP]
    if markets:
        groups.append({"label": "Mercati", "items": markets})

    if manages and not app:
        auctions = [{"title": a.title, "detail": a.get_status_display(),
                     "url": f"{reverse('admin_dashboard')}?auction={a.id}"}
                    for a in Auction.objects.filter(league=league).order_by("-id")
                    if _hit(needle, a.title)][:PER_GROUP]
        if auctions:
            groups.append({"label": "Aste", "items": auctions})

    return JsonResponse({"ok": True, "q": q, "groups": groups})
