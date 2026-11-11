"""Calciatori: la stessa pagina in console e nell'app, con due schede.

- **Listone**: i giocatori della lega (``Player``), cioè quelli che vanno
  all'asta, con stato, quotazione e, per chi gestisce la lega, gli strumenti
  (importa listone, rose, schede, statistiche; assegna, svincola, elimina).
  Non dipende da API-Football: c'è anche quando l'API non risponde.
- **Anagrafica Serie A**: l'elenco unico per tutto FantaManager preso da
  API-Football; accanto a ogni calciatore si vede cosa è nella lega
  (svincolato, di chi è, o fuori listone). L'aggiornamento riguarda tutte le
  leghe: lo avvia un superuser.

Prima il listone era una pagina a parte («Giocatori», /dashboard/players/):
i vecchi indirizzi portano qui (``admin_players``).
"""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import remote
from ..models import Footballer, League, Participant, Player
from ..providers import importers
from ..providers.apifootball import is_configured as apifootball_configured
from ..services import footballers
from .admin_market import _back
from .app import _redirect_login
from .common import (
    _app_ctx,
    current_auction,
    current_league,
    in_app,
    manageable_leagues,
    staff_member_required,
    user_can_manage_league,
    user_can_manage_scope,
)

LEAGUE_FILTERS = ("free", "owned", "out")
VIEWS = ("listone", "anagrafica")
LISTONE_ORDERS = {
    "name": ["name"],
    "-name": ["-name"],
    "quota": ["initial_price", "name"],
    "-quota": ["-initial_price", "name"],
    "role": ["role", "name"],
    "team": ["team", "name"],
}


def _pool(league):
    """Il listone di ``league``; senza lega quello di prima delle leghe."""
    return Player.objects.filter(league=league) if league is not None else Player.objects.filter(league__isnull=True)


def _can_manage_pool(request, pool, league):
    """Chi vede gli strumenti del listone: chi gestisce la lega (il listone
    senza lega solo il superuser). Da internet serve anche il PIN della regia,
    come per le altre pagine di gestione: senza, i tasti fallirebbero."""
    user = request.user
    if pool is None or not user.is_authenticated:
        return False
    allowed = user_can_manage_league(user, league) if league is not None else user.is_superuser
    if allowed and remote.request_is_remote(request) and not request.session.get("regia_unlocked"):
        return False
    return bool(allowed)


def listone_context(request, pool, league, can_manage):
    """La scheda Listone: i giocatori della lega filtrati e paginati nel
    database (50 per pagina), conteggi per ruolo e, per chi gestisce la lega,
    i dati degli strumenti (squadre per «Assegna», statistiche)."""
    q = (request.GET.get("q") or "").strip()
    role = (request.GET.get("role") or "").strip().upper()[:1]
    team = (request.GET.get("team") or "").strip()
    status = (request.GET.get("status") or "").strip()
    if status not in ("free", "owned"):
        status = ""
    sort = (request.GET.get("sort") or "name").strip()
    if sort not in LISTONE_ORDERS:
        sort = "name"

    players = pool.select_related("owner")
    if q:
        players = players.filter(Q(name__icontains=q) | Q(team__icontains=q))
    if role in ("P", "D", "C", "A"):
        players = players.filter(role=role)
    if team:
        players = players.filter(team=team)
    if status == "free":
        players = players.filter(owner__isnull=True)
    elif status == "owned":
        players = players.filter(owner__isnull=False)
    page = Paginator(players.order_by(*LISTONE_ORDERS[sort]), 50).get_page(request.GET.get("page"))

    keep = request.GET.copy()
    keep.pop("page", None)
    total = pool.count()
    free = pool.filter(owner__isnull=True).count()
    ctx = {
        "lst_rows": page.object_list,
        "lst_page": page,
        "lst_filters": {"q": q, "role": role, "team": team, "status": status, "sort": sort},
        "lst_filter_qs": keep.urlencode(),
        "lst_teams": list(pool.exclude(team="").order_by("team").values_list("team", flat=True).distinct()),
        "lst_roles": [(r, pool.filter(role=r).count(), label) for r, label in
                      (("P", "Portieri"), ("D", "Difensori"), ("C", "Centrocampisti"), ("A", "Attaccanti"))],
        "lst_free": free,
        "lst_total": total,
        "lst_owned": total - free,
        "lst_can_manage": can_manage,
        "lst_mantra": bool(league is not None and league.is_mantra),
        # Arrivati dal wizard o da una sessione ripresa: la lega non ha ancora il listone.
        "need_listone": request.GET.get("need_listone") == "1",
        "need_listone_from": request.GET.get("from", ""),
    }
    if can_manage:
        teams = Participant.objects.filter(is_active=True)
        teams = teams.filter(league=league) if league is not None else teams.filter(league__isnull=True)
        ctx.update({
            "lst_participants": list(teams.order_by("display_name")),
            # Le statistiche: un file del server (FANTAMANAGER_STATS_FILE) si
            # applica da solo agli import; altrimenti la lega ne carica uno.
            "stats_season": importers.server_stats_season(),
            "stats_server_file": importers.server_stats_path() is not None,
            "stats_covered": pool.exclude(fanta_avg__isnull=True).count(),
        })
    return ctx


def footballers_context(request, pool, base_url, league=None, can_manage=None):
    """I dati di ``_footballers.html``: la scheda scelta (``?vista=``), i suoi
    filtri e una pagina di righe.

    Scheda Listone: i giocatori di ``pool`` (vedi ``listone_context``).
    Scheda Anagrafica: una pagina di calciatori e, per ognuno, il giocatore
    del listone ``pool`` collegato (se c'è).
    ``pool`` None: nessuna lega da mostrare (solo l'anagrafica).
    ``league`` è la lega di ``pool``, per dire se usa la lista generale.
    ``can_manage``: chi guarda può usare gli strumenti del listone (None = calcolalo)."""
    if can_manage is None:
        can_manage = _can_manage_pool(request, pool, league)
    vista = (request.GET.get("vista") or "").strip()
    if pool is None:
        vista = "anagrafica"
    elif vista not in VIEWS:
        vista = "listone"
    ctx = {"fb_view": vista}
    if vista == "listone":
        ctx.update(listone_context(request, pool, league, can_manage))
    ctx.update(_registry_context(request, pool, base_url, league, can_manage, vista))
    return ctx


def _registry_context(request, pool, base_url, league, can_manage, vista):
    """Scheda Anagrafica (e i numeri che servono anche all'altra scheda)."""
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

    page = Paginator(registry.order_by("name", "id") if vista == "anagrafica" else registry.none(),
                     50).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    if players is not None and rows:
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
        "fb_can_manage": can_manage,
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
        # La voce «Asta Live» della console resta sull'asta che si sta facendo.
        "selected": current_auction(request, league) if in_scope else None,
    })
    return render(request, "auctions/admin_footballers.html", ctx)


@staff_member_required
def admin_players(request):
    """Il vecchio indirizzo del listone («Giocatori»): ora è la scheda Listone
    di Calciatori. Porta lì con gli stessi filtri (lega, ruolo, stato…)."""
    target = reverse("app_footballers" if in_app(request) else "admin_footballers")
    query = request.META.get("QUERY_STRING", "")
    return redirect(f"{target}?{query}" if query else target)


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
