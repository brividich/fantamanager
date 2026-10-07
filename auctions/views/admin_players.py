"""Admin player management: listone, imports, assignments, and search."""
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import Auction, League, LeagueConfig, Participant, Player
from ..providers import importers
from .. import services
from .common import (
    broadcast_state,
    current_auction,
    current_league,
    league_scope_or_403,
    manageable_leagues,
    league_mismatch_json,
    managed_or_403,
    mixed_leagues,
    staff_member_required,
    target_league,
    user_can_manage_scope,
)


@staff_member_required
def admin_players(request):
    """The listone of one league, filtered and paginated server-side.

    500+ players used to render as one enormous page (tens of thousands of DOM
    nodes). Search, role, team and status now filter in the database and only a
    page of rows is sent; every filter travels in the querystring so pagination
    links keep them — the league included.
    """
    leagues = list(manageable_leagues(request.user))
    league = current_league(request)
    # No league picked: the global pool, which belongs to nobody but superusers.
    in_scope = user_can_manage_scope(request.user, league)

    if league is not None:
        all_players = Player.objects.filter(league=league)
    elif in_scope:
        all_players = Player.objects.filter(league__isnull=True)
    else:
        all_players = Player.objects.none()
    counts  = {r: all_players.filter(role=r).count() for r in ["P", "D", "C", "A"]}
    free_count = all_players.filter(owner__isnull=True).count()
    total_count = all_players.count()

    q      = (request.GET.get("q") or "").strip()
    role   = (request.GET.get("role") or "").strip().upper()[:1]
    team   = (request.GET.get("team") or "").strip()
    status = (request.GET.get("status") or "").strip()
    sort   = (request.GET.get("sort") or "name").strip()

    players = all_players.select_related("owner")
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

    ORDERS = {
        "name":  ["name"],
        "-name": ["-name"],
        "quota": ["initial_price", "name"],
        "-quota": ["-initial_price", "name"],
        "role":  ["role", "name"],
        "team":  ["team", "name"],
    }
    players = players.order_by(*ORDERS.get(sort, ORDERS["name"]))

    paginator = Paginator(players, 50)
    page = paginator.get_page(request.GET.get("page"))

    # Querystring without page/league, so the pager can re-append them.
    keep = request.GET.copy()
    for k in ("page",):
        keep.pop(k, None)
    filter_qs = keep.urlencode()

    teams = list(all_players.exclude(team="").order_by("team")
                 .values_list("team", flat=True).distinct())

    # Teams available as assignment targets (scoped to the current league).
    participants = Participant.objects.filter(is_active=True)
    if league is not None:
        participants = participants.filter(league=league)
    elif not in_scope:
        participants = participants.none()
    return render(request, "auctions/admin_players.html", {
        "players": page.object_list, "page_obj": page, "paginator": paginator,
        "counts": counts, "free_count": free_count, "total_count": total_count,
        "filters": {"q": q, "role": role, "team": team, "status": status, "sort": sort},
        "filter_qs": filter_qs, "teams": teams,
        "leagues": leagues, "current_league": league,
        "participants": participants.order_by("display_name"),
        # The stats card reports coverage. A server may provide its own stats
        # file (FANTAMANAGER_STATS_FILE) that imports apply by themselves;
        # otherwise the league uploads one.
        "stats_season": importers.server_stats_season(),
        "stats_server_file": importers.server_stats_path() is not None,
        "stats_covered": all_players.exclude(fanta_avg__isnull=True).count(),
        "console_section": "Giocatori",
        "console_active": "players",
        # Keeps the "Asta" tab pointing back at the auction being run.
        "selected": current_auction(request, league),
        # Set when the wizard (or a resumed session) bounced here: this league
        # has no listone yet.
        "need_listone": request.GET.get("need_listone") == "1",
        "need_listone_from": request.GET.get("from", ""),
    })


@staff_member_required
@require_POST
def admin_apply_photos(request):
    """Populate Player.photo_url from a URL template the admin provides.

    Optional ``listone_file`` first backfills missing ``ext_id`` by name match
    (so a pool imported before ids were captured still gets photos). ``template``
    is required (no third-party CDN by default); ``only_missing=0`` re-applies
    to everyone."""
    league, denied = league_scope_or_403(request, request.POST.get("league_id"))
    if denied:
        return denied
    template = (request.POST.get("template") or "").strip()
    if not template:
        return JsonResponse(
            {"ok": False, "error": "Indica l'indirizzo delle foto, con {id} al posto dell'id del giocatore."},
            status=400)
    only_missing = request.POST.get("only_missing", "1") == "1"

    backfilled = 0
    f = request.FILES.get("listone_file")
    if f is not None:
        rows, _errors = importers.parse_listone_file(f, f.name)
        backfilled = importers.backfill_ext_ids(rows, league=league)

    report = importers.apply_photos(league=league, template=template, only_missing=only_missing)
    report["ok"] = True
    report["backfilled_ext_ids"] = backfilled
    report["template"] = template
    return JsonResponse(report)


@staff_member_required
@require_POST
def admin_import_stats(request):
    """Merge a Fantacalcio "Statistiche" file onto the pool (season numbers).

    Additive: it only sets the stat fields (presences/media voto/fantamedia/
    goals/assists) on players already imported, matching by official id then by
    name. The base card still works for pools without a stats file.

    With no upload it re-applies the server's stats file
    (``FANTAMANAGER_STATS_FILE``), the same one an import seeds automatically;
    without one configured, a file is required.
    """
    league, denied = league_scope_or_403(request, request.POST.get("league_id"))
    if denied:
        return denied
    f = request.FILES.get("stats_file")
    if f:
        rows, errors = importers.parse_stats_file(f, f.name)
        source = f.name
    else:
        if importers.server_stats_path() is None:
            return JsonResponse(
                {"ok": False, "error": "Scegli il file delle statistiche da importare."}, status=400)
        rows, errors = importers.bundled_stats_rows()
        season = importers.server_stats_season()
        source = f"Statistiche del server{' ' + season if season else ''}"
        if not rows:
            return JsonResponse(
                {"ok": False, "error": "Statistiche del server non disponibili."}, status=500)
    report = importers.import_stats(rows, league=league)
    report["ok"] = True
    report["source"] = source
    report["errors"] = errors[:20]
    return JsonResponse(report)


@staff_member_required
@require_POST
def admin_import_players(request):
    # Import into a specific league's pool (None = legacy/global pool).
    league, denied = league_scope_or_403(request, request.POST.get("league_id"))
    if denied:
        return denied
    f = request.FILES.get("csv_file")
    if not f:
        return JsonResponse({"ok": False, "error": "Nessun file"}, status=400)

    replace = request.POST.get("replace") == "1"
    # When syncing (not replacing) the listone, drop free agents who are no
    # longer listed (left Serie A). Defaults on; owned players are never pruned.
    prune = request.POST.get("prune", "1") == "1"

    parsed, errors = importers.parse_listone_file(f, f.name)

    # Reconcile against the existing pool: matches existing players (preserving
    # roster ownership), creates free agents for the rest, and prunes departed
    # free agents — instead of blindly creating duplicate rows.
    report = importers.sync_players(parsed, league=league, replace=replace, prune=prune)
    return JsonResponse({
        "ok": True,
        "created": report["created"],
        "updated": report["updated"],
        "matched_owned": report["matched_owned"],
        "pruned": report["pruned"],
        "owned_not_in_listone": report["owned_not_in_listone"][:50],
        "left_serie_a": _left_serie_a(league, report),
        "errors": errors[:10],
    })


def _left_serie_a(league, report):
    """Chi è in rosa ma non è più nel listone ufficiale (5.05) e, con API-Football
    attiva, dove è andato: la ricerca parte da sola, per i primi segnalati."""
    if league is None or not report.get("flagged_left_serie_a"):
        return None
    from ..services import abroad

    detection = abroad.detect_all(league, limit=abroad.AUTO_DETECT_LIMIT)
    return {
        "flagged": report["flagged_left_serie_a"],
        "found": len(detection["found"]),
        "summary": abroad.detect_summary(detection),
        "url": f"{reverse('admin_contracts')}?league={league.id}",
    }


_ROSE_SOURCE_LABELS = {
    "fantapazz": "Fantapazz", "fantacalcio": "Fantacalcio.it", "generic": "Generico",
    "leghe_fantacalcio_id": "Leghe Fantacalcio",
}


def _resolve_rose_league(request):
    """League an import writes into, as ``(league, None)`` or ``(None, 403/404)``.

    The rose/players UI posts a FantaManager pk in ``league_id`` (unlike the
    Fantapazz flow, which posts an external id), so resolve by pk first and fall
    back to the only league when there is exactly one. Either way the user must
    manage the league it lands on."""
    raw = request.POST.get("league_id")
    fallback = None
    if not (raw or "").strip():
        leagues = League.objects.all()[:2]
        fallback = leagues[0] if len(leagues) == 1 else None
    return league_scope_or_403(request, raw, fallback)


@staff_member_required
@require_POST
def admin_import_rose(request):
    """Import team rosters (rose) from any supported export, auto-detecting the
    source (Fantapazz / Fantacalcio.it / generic). ``action=preview`` only reports
    what was found; otherwise it assigns ownership onto the listone pool.

    When the file also carries a full listone (the Fantacalcio.it flat export
    has a ``QUOT.`` column) it is synced first — non-destructively — so the free
    agents (svincolati) still appear at the auction."""
    league, denied = _resolve_rose_league(request)
    if denied:
        return denied
    f = request.FILES.get("rose_file")
    if not f:
        return JsonResponse({"ok": False, "error": "Nessun file"}, status=400)

    try:
        teams, listone, meta = importers.parse_rose_file(f, f.name, league=league)
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    if not teams:
        return JsonResponse({"ok": False, "error": "Nessuna squadra trovata nel file."}, status=400)

    source_label = _ROSE_SOURCE_LABELS.get(meta["source"], meta["source"])
    warning = ""
    if len(teams) <= 1:
        warning = ("Attenzione: nel file c'è una sola squadra. Verifica di aver "
                   "esportato le rose dell'intera lega e non solo la tua.")
    unmatched = meta.get("unmatched") or []
    if unmatched:
        names = ", ".join(sorted({row[0] for row in unmatched}))
        warning = (warning + " " if warning else "") + (
            f"{len(unmatched)} giocatori con Id non trovato nel listone di questa "
            f"lega (squadre coinvolte: {names}) — non sono stati importati. "
            "Verifica di aver caricato il listone giusto per questa lega."
        )

    if request.POST.get("action") == "preview":
        return JsonResponse({
            "ok": True, "preview": True,
            "source": meta["source"], "source_label": source_label,
            "teams": [
                {"name": t["name"], "credits": t["credits"], "n_players": len(t["players"]),
                 "players": t["players"][:5]}
                for t in teams
            ],
            "total_players": meta["n_players"],
            "n_listone": len(listone) if listone else 0,
            "warning": warning,
        })

    replace = request.POST.get("replace") == "1"
    budget  = league.budget if league is not None else LeagueConfig.get().budget

    listone_report = None
    # Default on: feed the file's listone so svincolati exist at the auction.
    if listone and request.POST.get("import_listone", "1") == "1":
        listone_report = importers.sync_players(listone, league=league, replace=False, prune=False)

    result = importers.import_rose_data(
        teams, replace=replace, league=league, default_budget=budget,
    )
    return JsonResponse({
        "ok": True,
        "source": meta["source"], "source_label": source_label,
        "teams": result["teams"], "players": result["players"],
        "listone_created": (listone_report or {}).get("created", 0),
        "warning": warning,
    })


@staff_member_required
@require_POST
def admin_delete_player(request, player_id):
    player, denied = managed_or_403(request, Player, player_id)
    if denied:
        return denied
    player.delete()
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def admin_clear_players(request):
    """Wipe a league's pool, or the global pool (no league: superusers only).

    Never every league's players at once: with no league the console shows the
    global pool, so that is what "Svuota" empties.
    """
    league, denied = league_scope_or_403(request, request.POST.get("league_id"))
    if denied:
        return denied
    Player.objects.filter(league=league).delete()
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def admin_release_player(request, player_id):
    """Admin svincolo: free any owned player, refunding per the auction policy."""
    player, denied = managed_or_403(request, Player, player_id)
    if denied:
        return denied
    auction_id = request.POST.get("auction_id") or None
    auction = None
    if auction_id:
        auction, denied = managed_or_403(request, Auction, auction_id)
        if denied:
            return denied
    if mixed_leagues(player, auction):
        return league_mismatch_json()
    result = services.release_player(player_id, auction_id=auction_id, by_admin=True)
    if result.get("ok") and auction is not None:
        broadcast_state(auction)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@staff_member_required
@require_POST
def admin_assign_player(request, player_id):
    """Admin manual assign / re-assign / price-correction of a player.

    POST ``participant_id`` (required), optional ``price`` (defaults to the
    player's listone quotazione) and ``note``. Broadcasts state when an
    ``auction_id`` is supplied so the live screens reflect the change."""
    participant_id = request.POST.get("participant_id")
    if not participant_id:
        return JsonResponse({"ok": False, "error": "Nessuna squadra selezionata"}, status=400)
    player, denied = managed_or_403(request, Player, player_id)
    if denied:
        return denied
    team, denied = managed_or_403(request, Participant, participant_id)
    if denied:
        return denied
    auction_id = request.POST.get("auction_id") or None
    auction = None
    if auction_id:
        auction, denied = managed_or_403(request, Auction, auction_id)
        if denied:
            return denied
    if mixed_leagues(player, team, auction):
        return league_mismatch_json()
    price = request.POST.get("price") or None
    result = services.assign_player(
        player_id, participant_id, price=price, by_admin=True,
        note=request.POST.get("note", ""),
    )
    if result.get("ok") and auction is not None:
        broadcast_state(auction)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@staff_member_required
def admin_player_search(request):
    """Free agents matching ``?q=`` in the console's league — max 20 rows.

    Rendering the whole listone into the page cost hundreds of <tr> on every
    load; the "prossimo giocatore" box now asks for what the admin is typing.
    Accent-blind, because mid-auction nobody reaches for the accented key.
    """
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": [], "total": 0})

    league = target_league(request)
    if not user_can_manage_scope(request.user, league):
        return JsonResponse({"ok": True, "results": [], "total": 0})
    qs = Player.objects.filter(owner__isnull=True)
    qs = qs.filter(league=league) if league is not None else qs.filter(league__isnull=True)

    needle = importers.normalize_name(q)
    rows, total = [], 0
    for p in qs.order_by("role", "name").only(
            "id", "name", "role", "team", "initial_price"):
        if needle not in importers.normalize_name(p.name) \
                and needle not in importers.normalize_name(p.team or ""):
            continue
        total += 1
        if len(rows) < 20:
            rows.append({
                "id": p.id, "name": p.name, "role": p.role,
                "team": p.team, "quotation": float(p.initial_price),
            })
    return JsonResponse({"ok": True, "results": rows, "total": total})
