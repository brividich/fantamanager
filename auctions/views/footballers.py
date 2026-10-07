"""Anagrafica dei calciatori: la stessa pagina in console e nell'app.

L'anagrafica è unica per tutto FantaManager; accanto a ogni calciatore si vede
cosa è nella lega che si sta guardando (svincolato, di chi è, o fuori listone).
L'aggiornamento da API-Football riguarda tutte le leghe: lo avvia un superuser.
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import Footballer, League, Player
from ..providers.apifootball import is_configured as apifootball_configured
from ..services import footballers
from .admin_market import _back
from .app import _redirect_login
from .common import (
    _app_ctx,
    current_league,
    manageable_leagues,
    staff_member_required,
    user_can_manage_league,
    user_can_manage_scope,
)

LEAGUE_FILTERS = ("free", "owned", "out")


def _pool(league):
    """Il listone di ``league``; senza lega quello di prima delle leghe."""
    return Player.objects.filter(league=league) if league is not None else Player.objects.filter(league__isnull=True)


def footballers_context(request, pool, base_url, league=None):
    """I dati di ``_footballers.html``: filtri, una pagina di calciatori e,
    per ognuno, il giocatore del listone ``pool`` collegato (se c'è).
    ``pool`` None: nessuna lega da mostrare accanto all'anagrafica.
    ``league`` è la lega di ``pool``, per dire se usa la lista generale."""
    q = (request.GET.get("q") or "").strip()
    role = (request.GET.get("role") or "").strip().upper()[:1]
    club = (request.GET.get("club") or "").strip()
    in_league = (request.GET.get("lega") or "").strip() if pool is not None else ""
    if in_league not in LEAGUE_FILTERS:
        in_league = ""
    gone = request.GET.get("usciti") == "1"

    registry = Footballer.objects.filter(in_serie_a=not gone)
    players = pool.filter(footballer__isnull=False) if pool is not None else None
    if q:
        registry = registry.filter(Q(name__icontains=q) | Q(club_name__icontains=q)
                                   | Q(league_players__name__icontains=q)).distinct()
    if role in ("P", "D", "C", "A"):
        registry = registry.filter(role=role)
    if club.isdigit():
        registry = registry.filter(club_api_id=int(club))
    if in_league == "free":
        registry = registry.filter(id__in=players.filter(owner__isnull=True).values("footballer_id"))
    elif in_league == "owned":
        registry = registry.filter(id__in=players.filter(owner__isnull=False).values("footballer_id"))
    elif in_league == "out":
        registry = registry.exclude(id__in=players.values("footballer_id"))

    page = Paginator(registry.order_by("name", "id"), 50).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    if players is not None:
        linked = {p.footballer_id: p for p in players.filter(footballer__in=rows).select_related("owner")}
        for f in rows:
            f.league_player = linked.get(f.id)

    keep = request.GET.copy()
    keep.pop("page", None)
    clubs = (Footballer.objects.filter(in_serie_a=True).exclude(club_api_id__isnull=True)
             .order_by("club_name").values_list("club_api_id", "club_name").distinct())
    return {
        "fb_rows": rows,
        "fb_page": page,
        "fb_filters": {"q": q, "role": role, "club": club, "lega": in_league, "usciti": gone},
        "fb_filter_qs": keep.urlencode(),
        "fb_clubs": list(clubs),
        "fb_total": Footballer.objects.filter(in_serie_a=True).count(),
        "fb_last_seen": Footballer.objects.order_by("-seen_at").values_list("seen_at", flat=True).first(),
        "fb_has_league": pool is not None,
        "fb_linked": players.count() if players is not None else 0,
        "fb_listone": pool.count() if pool is not None else 0,
        "fb_sync": footballers.sync_state(),
        "fb_can_sync": bool(request.user.is_authenticated and request.user.is_superuser),
        "fb_can_edit": bool(request.user.is_authenticated and request.user.is_superuser),
        "fb_roles": Footballer.Position.choices,
        "fb_league": league,
        "fb_can_manage_league": bool(league is not None and user_can_manage_league(request.user, league)),
        "fb_api_ready": apifootball_configured(),
        "fb_base": base_url,
    }


@staff_member_required
def admin_footballers(request):
    league = current_league(request)
    # Le rose di una lega le vede solo chi la gestisce.
    in_scope = user_can_manage_scope(request.user, league)
    ctx = footballers_context(request, _pool(league) if in_scope else None, reverse("admin_footballers"),
                              league if in_scope else None)
    ctx.update({
        "leagues": list(manageable_leagues(request.user)),
        "current_league": league if in_scope else None,
        "console_section": "Calciatori",
        "console_active": "footballers",
    })
    return render(request, "auctions/admin_footballers.html", ctx)


def app_footballers(request):
    participant, ctx = _app_ctx(request, "calciatori")
    if ctx is None:
        return _redirect_login(request, ctx)
    league = ctx["app_league"]
    # Senza lega il listone di prima delle leghe, ma solo per chi ci ha la squadra.
    pool = _pool(league) if league is not None or participant is not None else None
    ctx.update(footballers_context(request, pool, reverse("app_footballers"), league))
    return render(request, "auctions/app_footballers.html", ctx)


@staff_member_required
@require_POST
def admin_footballers_sync(request):
    """Avvia l'aggiornamento dell'anagrafica (vale per tutte le leghe)."""
    back = _back(request, reverse("admin_footballers"))
    if not request.user.is_superuser:
        messages.error(request, "L'anagrafica è comune a tutte le leghe: la aggiorna solo il superuser.")
    elif not apifootball_configured():
        messages.error(request, "API-Football non configurata: imposta APIFOOTBALL_KEY sul server e riavvia.")
    elif footballers.start_sync():
        messages.success(request, "Aggiornamento avviato: serve qualche minuto, ricarica la pagina per vedere l'esito.")
    else:
        messages.info(request, "C'è già un aggiornamento in corso.")
    return redirect(back)


@staff_member_required
@require_POST
def admin_footballer_edit(request, footballer_id):
    """Il superuser corregge ruolo e ruoli Mantra della lista generale."""
    back = _back(request, reverse("admin_footballers"))
    if not request.user.is_superuser:
        messages.error(request, "La lista generale è comune a tutte le leghe: la modifica solo il superuser.")
        return redirect(back)
    footballer = get_object_or_404(Footballer, pk=footballer_id)
    ok, msg = footballers.edit_roles(footballer, request.POST.get("role"), request.POST.get("mantra_roles"))
    (messages.success if ok else messages.error)(request, msg)
    return redirect(back)


@staff_member_required
@require_POST
def admin_league_default_listone(request, league_id):
    """La lega torna alla lista generale (lascia il listone caricato)."""
    league = get_object_or_404(League, pk=league_id)
    back = _back(request, f"{reverse('admin_footballers')}?league={league.id}")
    if not user_can_manage_league(request.user, league):
        messages.error(request, "Puoi cambiare il listone solo delle leghe che gestisci.")
        return redirect(back)
    res = footballers.use_default_listone(league)
    if res is None:
        messages.info(request, "La lega userà la lista generale appena sarà stata scaricata da API-Football.")
    else:
        messages.success(request, f"{league.name} ora usa la lista generale: {res['created']} calciatori aggiunti, "
                                  f"{res['updated']} aggiornati. Chi è in rosa resta dov'è.")
    return redirect(back)
